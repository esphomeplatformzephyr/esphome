"""Platform get_download_types contract for never-built configs.

Wizard-written and upload/logs-fallback sidecars record no
firmware_bin_path; the download panel must get an empty list for them,
not entries pointing at files that were never built.
"""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any

import pytest

from esphome.storage_json import StorageJSON

PLATFORMS = [
    "esp32",
    "esp8266",
    "rp2",
    "libretiny",
    "nrf52",
    "nrf",
    "nxp",
    "renesas",
    "silabs",
    "stm32",
]


def _download_types(platform: str, storage: StorageJSON) -> list[dict[str, Any]]:
    return import_module(f"esphome.components.{platform}").get_download_types(storage)


def _wizard_storage() -> StorageJSON:
    return StorageJSON.from_wizard(
        name="test_device",
        friendly_name="Test Device",
        address="test_device.local",
        platform="ESP32",
    )


@pytest.mark.parametrize("platform", PLATFORMS)
def test_no_firmware_path_yields_no_downloads(platform: str) -> None:
    """No recorded firmware path means nothing was built; no downloads."""
    assert _download_types(platform, _wizard_storage()) == []


@pytest.mark.parametrize("platform", PLATFORMS)
def test_recorded_firmware_path_yields_downloads(platform: str, tmp_path: Path) -> None:
    """With a firmware path recorded, every platform offers entries in
    the documented title/description/file/download shape."""
    storage = _wizard_storage()
    storage.firmware_bin_path = tmp_path / "firmware.bin"

    types = _download_types(platform, storage)

    assert types
    assert all(
        {"title", "description", "file", "download"} <= entry.keys() for entry in types
    )


def test_esp32_skip_bootloader_tree_offers_no_factory_entry(tmp_path: Path) -> None:
    """A --skip-bootloader tree has no factory image; do not offer one.

    An unreadable tree (PlatformIO, capability probes passing a bare
    path) reads as a full build and keeps the entry.
    """
    from esphome.espidf.toolchain import SKIP_BOOTLOADER_DEFINE

    storage = _wizard_storage()
    storage.firmware_bin_path = tmp_path / "firmware.bin"

    files = [entry["file"] for entry in _download_types("esp32", storage)]
    assert files == ["firmware.factory.bin", "firmware.ota.bin"]

    cache = tmp_path / "CMakeCache.txt"
    cache.write_text(f"{SKIP_BOOTLOADER_DEFINE}:UNINITIALIZED=1\n")
    files = [entry["file"] for entry in _download_types("esp32", storage)]
    assert files == ["firmware.ota.bin"]

    # A capability probe passes the path as a plain string.
    storage.firmware_bin_path = str(tmp_path / "firmware.bin")
    files = [entry["file"] for entry in _download_types("esp32", storage)]
    assert files == ["firmware.ota.bin"]

    cache.write_text(f"{SKIP_BOOTLOADER_DEFINE}:UNINITIALIZED=0\n")
    files = [entry["file"] for entry in _download_types("esp32", storage)]
    assert files == ["firmware.factory.bin", "firmware.ota.bin"]


def _nrf52_files(tmp_path: Path, *built: str) -> list[str]:
    """The files nrf52 offers for a build directory holding *built*."""
    (tmp_path / "zephyr").mkdir()
    for name in built:
        (tmp_path / name).touch()
    storage = _wizard_storage()
    storage.firmware_bin_path = tmp_path / "firmware.bin"
    return [entry["file"] for entry in _download_types("nrf52", storage)]


@pytest.mark.parametrize(
    ("built", "expected"),
    [
        # Adafruit bootloader with the mcumgr OTA: MCUboot is chained behind it.
        (
            ["zephyr/zephyr.uf2", "firmware.zip", "zephyr/app_update.bin"],
            ["zephyr/zephyr.uf2", "firmware.zip", "zephyr/app_update.bin"],
        ),
        (["zephyr/zephyr.uf2", "firmware.zip"], ["zephyr/zephyr.uf2", "firmware.zip"]),
        # The DFU package is offered only when the build made one
        (["zephyr/zephyr.uf2"], ["zephyr/zephyr.uf2"]),
        (
            ["zephyr/merged.hex", "zephyr/app_update.bin"],
            ["zephyr/merged.hex", "zephyr/app_update.bin"],
        ),
        (["zephyr/merged.hex"], ["zephyr/merged.hex"]),
        # The dongle's Open DFU bootloader has no UF2 family, so its DFU package rides beside the HEX
        (["zephyr/merged.hex", "firmware.zip"], ["zephyr/merged.hex", "firmware.zip"]),
        ([], ["zephyr/zephyr.hex"]),
    ],
)
def test_nrf52_offers_the_mcumgr_image_whenever_it_was_built(
    tmp_path: Path, built: list[str], expected: list[str]
) -> None:
    """The mcumgr update image is offered beside a UF2 as well as beside a HEX."""
    assert _nrf52_files(tmp_path, *built) == expected


def _zephyr_files(
    tmp_path: Path, *built: str, platform: str = "nrf", framework: str | None = None
) -> list[str]:
    """The files a Zephyr RTOS platform offers for a build holding *built*."""
    app_dir = tmp_path / ".west_build" / "zephyr" / "zephyr"
    app_dir.mkdir(parents=True)
    for name in built:
        (tmp_path / name).touch()
    storage = _wizard_storage()
    storage.firmware_bin_path = app_dir / "zephyr.signed.bin"
    storage.framework = framework
    return [entry["file"] for entry in _download_types(platform, storage)]


APP = ".west_build/zephyr/zephyr/"


@pytest.mark.parametrize(
    ("built", "expected"),
    [
        # RP2040/RP2350 with MCUboot
        (
            [f"{APP}zephyr.uf2", f"{APP}zephyr.hex", f"{APP}zephyr.signed.bin"],
            ["zephyr.uf2", "zephyr.signed.bin"],
        ),
        # Most families with MCUboot: the HEX is the app only, as nrf52's on SDK 3.4+
        (
            [f"{APP}zephyr.hex", f"{APP}zephyr.signed.bin"],
            ["zephyr.hex", "zephyr.signed.bin"],
        ),
        # nRF52 with an Adafruit bootloader: its serial DFU package sits in the build directory
        (
            [f"{APP}zephyr.uf2", "firmware.zip"],
            ["zephyr.uf2", "../../../firmware.zip"],
        ),
        # No MCUboot, so no app update image
        ([f"{APP}zephyr.hex"], ["zephyr.hex"]),
    ],
)
def test_zephyr_rtos_offers_what_the_build_made(
    tmp_path: Path, built: list[str], expected: list[str]
) -> None:
    assert _zephyr_files(tmp_path, *built) == expected


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        # ESP32 Zephyr builds make no HEX file
        ("esp32", ["zephyr.signed.bin"]),
        ("rp2", ["zephyr.hex", "zephyr.signed.bin"]),
    ],
)
def test_zephyr_builds_on_esp32_and_rp2_use_the_zephyr_list(
    tmp_path: Path, platform: str, expected: list[str]
) -> None:
    files = _zephyr_files(
        tmp_path, f"{APP}zephyr.signed.bin", platform=platform, framework="zephyr"
    )
    assert files == expected
