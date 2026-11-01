"""Tests for the MCUboot swap-size check after a Zephyr RTOS build."""

from pathlib import Path
from unittest.mock import patch

import pytest

from esphome.components.zephyr_rtos import mcuboot
from esphome.components.zephyr_rtos.mcuboot import (
    _mcuboot_swap_limit,
    check_mcuboot_swap_limit,
)
from esphome.core import EsphomeError

# Pico W with swap using offset: two 0xd0000 slots of 4 KB sectors, 1 byte writes
PICO_W_LAYOUT = {
    "slot0": 0xD0000,
    "slot1": 0xD0000,
    "sector": 4096,
    "write_size": 1,
    "scratch": None,
}
PICO_W_LIMIT = 0xD0000 - 2 * 4096


@pytest.mark.parametrize(
    ("args", "limit"),
    [
        # Upstream's nRF52 ItsyBitsy case: a 97 sector slot swaps in 95 sectors
        (("move", 0x61000, 0x61000, 4096, 4, 128, 8), 389_120),
        # Offset: the trailer sector and the padding sector come off the secondary slot
        (("offset", 0xD0000, 0xD0000, 4096, 1, 208, 8), PICO_W_LIMIT),
        (("offset", 0xD1000, 0xD0000, 4096, 1, 209, 8), PICO_W_LIMIT),
        # Scratch, trailer within the last sector: 16 * 3 * 4 + 48 bytes come off
        (("scratch", 0x10000, 0x10000, 4096, 4, 16, 8, 4096), 0x10000 - 240),
        # Scratch, trailer over 7 sectors with 48 bytes in the first: 24 bytes of padding
        (("scratch", 0x10000, 0x10000, 4096, 8, 1024, 8, 4096), 0x10000 - 24_624 - 24),
        # A scratch area smaller than a sector cannot swap at all
        (("scratch", 0x10000, 0x10000, 4096, 4, 16, 8, 2048), 0),
    ],
)
def test_mcuboot_swap_limit(args: tuple, limit: int) -> None:
    assert _mcuboot_swap_limit(*args) == limit


def _mcuboot_build(tmp_path: Path, config: str, image_size: int | None) -> Path:
    config_path = tmp_path / "mcuboot" / "zephyr" / ".config"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(config)
    image = tmp_path / "zephyr.signed.bin"
    if image_size is not None:
        image.write_bytes(b"\0" * image_size)
    return image


OFFSET_AUTO = "CONFIG_BOOT_SWAP_USING_OFFSET=y\nCONFIG_BOOT_MAX_IMG_SECTORS_AUTO=y\n"


@pytest.mark.parametrize(
    ("image_size", "error"),
    [
        (PICO_W_LIMIT, None),
        (
            PICO_W_LIMIT + 1,
            (
                f"{PICO_W_LIMIT + 1} bytes, but MCUboot can only swap in "
                f"{PICO_W_LIMIT} bytes.*at least 1 bytes"
            ),
        ),
    ],
)
def test_check_mcuboot_swap_limit(
    tmp_path: Path, image_size: int, error: str | None
) -> None:
    image = _mcuboot_build(tmp_path, OFFSET_AUTO, image_size)
    with patch.object(mcuboot, "_read_slot_layout", return_value=PICO_W_LAYOUT):
        if error is None:
            check_mcuboot_swap_limit(tmp_path, image)
        else:
            with pytest.raises(EsphomeError, match=error):
                check_mcuboot_swap_limit(tmp_path, image)


@pytest.mark.parametrize(
    "config",
    [
        None,  # no MCUboot in the build
        "CONFIG_SINGLE_APPLICATION_SLOT=y\n",
        "CONFIG_BOOT_UPGRADE_ONLY=y\n",
    ],
)
def test_check_mcuboot_swap_limit_without_a_swap(
    tmp_path: Path, config: str | None
) -> None:
    image = tmp_path / "zephyr.signed.bin"
    if config is not None:
        image = _mcuboot_build(tmp_path, config, 10_000_000)
    with patch.object(mcuboot, "_read_slot_layout") as read_layout:
        check_mcuboot_swap_limit(tmp_path, image)
    read_layout.assert_not_called()


def test_check_mcuboot_swap_limit_warns_without_the_image(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    image = _mcuboot_build(tmp_path, OFFSET_AUTO, None)
    check_mcuboot_swap_limit(tmp_path, image)
    assert "Skipping the MCUboot slot size check" in caplog.text


def test_check_mcuboot_swap_limit_warns_without_the_devicetree(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    image = _mcuboot_build(tmp_path, OFFSET_AUTO, 10_000_000)
    check_mcuboot_swap_limit(tmp_path, image)
    assert "Skipping the MCUboot slot size check" in caplog.text


def test_check_mcuboot_swap_limit_uses_the_configured_sector_count(
    tmp_path: Path,
) -> None:
    """CONFIG_BOOT_MAX_IMG_SECTORS sizes the trailer when the auto count is off."""
    config = "CONFIG_BOOT_SWAP_USING_MOVE=y\nCONFIG_BOOT_MAX_IMG_SECTORS=128\n"
    image = _mcuboot_build(tmp_path, config, 389_121)
    layout = {
        "slot0": 0x61000,
        "slot1": 0x61000,
        "sector": 4096,
        "write_size": 4,
        "scratch": None,
    }
    with (
        patch.object(mcuboot, "_read_slot_layout", return_value=layout),
        pytest.raises(EsphomeError, match="can only swap in 389120 bytes"),
    ):
        check_mcuboot_swap_limit(tmp_path, image)
