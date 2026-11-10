"""Tests for the placement of the zephyr_coredump flash partition."""

import asyncio
from collections.abc import Callable
import logging
from pathlib import Path
import re
from unittest.mock import AsyncMock, patch

import pytest

from esphome.components.nrf52 import set_core_data
from esphome.components.nrf52.boards import BOOTLOADER_CONFIG
from esphome.components.nrf52.const import FLASH_SECTOR_SIZE
from esphome.components.zephyr import zephyr_data
from esphome.components.zephyr_coredump import (
    CONFIG_SCHEMA,
    DUMP_THREADS_MIN_VERSION,
    PARTITION_SIZE,
    _partition_address,
    to_code,
)
from esphome.components.zephyr_rtos.const import (
    KEY_OVERLAY,
    KEY_PARTITION_REQUIREMENTS,
    KEY_PM_STATIC,
    KEY_PRJ_CONF,
    KEY_ZEPHYR_RTOS,
)
import esphome.config_validation as cv
from esphome.const import (
    CONF_ID,
    KEY_CORE,
    KEY_FRAMEWORK_VERSION,
    KEY_TARGET_FRAMEWORK,
    KEY_TARGET_PLATFORM,
)
from esphome.core import CORE, ID
from tests.unit_tests.components.zephyr_rtos.zephyr_state import (
    empty_zephyr_data,
    zephyr_platform_framework,
)


def _core_data(
    board: str,
    bootloader: str,
    testing_mode: bool = False,
    version: cv.Version = DUMP_THREADS_MIN_VERSION,
) -> None:
    CORE.data[KEY_CORE] = {KEY_FRAMEWORK_VERSION: version}
    CORE.testing_mode = testing_mode
    set_core_data({"board": board, "bootloader": bootloader})


@pytest.mark.parametrize(
    ("board", "bootloader", "testing_mode", "expected"),
    [
        # Below the Adafruit bootloader at 0xF4000
        ("adafruit_itsybitsy_nrf52840", "adafruit_nrf52_sd140_v6", False, 0xEC000),
        # Below the factory Open DFU bootloader at 0xE0000
        ("nrf52840dongle", "nrf", False, 0xD8000),
        # Nothing reserved: the end of the 1 MB flash
        ("adafruit_feather_nrf52840", "mcuboot", False, 0xF8000),
        # An nRF52840 board whose name does not say so
        ("xiao_ble", "mcuboot", False, 0xF8000),
        # Testing mode moves the bootloader to the end of the faked 4 MB flash
        ("adafruit_itsybitsy_nrf52840", "adafruit_nrf52_sd140_v6", True, 0x3EC000),
        ("adafruit_feather_nrf52840", "mcuboot", True, 0x3F8000),
        # Below the Adafruit bootloader at 0x74000 on the 512 KB nRF52832
        ("adafruit_feather_nrf52832", "adafruit_nrf52_sd132_v6", False, 0x6C000),
        # Bank 1 starts mid page at 0x44800, so the partition moves down to the page below
        ("adafruit_feather_nrf52832", "adafruit_nrf52_sd132_v2", False, 0x3C000),
        ("adafruit_feather_nrf52832", "adafruit_nrf52_sd132_v2", True, 0x3BC000),
        # Nothing reserved: the end of the 512 KB flash
        ("nrf52dk/nrf52832", "mcuboot", False, 0x78000),
    ],
)
def test_partition_address(
    board: str, bootloader: str, testing_mode: bool, expected: int
) -> None:
    _core_data(board, bootloader, testing_mode)
    assert _partition_address() == expected
    assert PARTITION_SIZE % FLASH_SECTOR_SIZE == 0


@pytest.mark.parametrize("testing_mode", [False, True])
@pytest.mark.parametrize("bootloader", sorted(BOOTLOADER_CONFIG))
def test_partition_is_page_aligned(bootloader: str, testing_mode: bool) -> None:
    board = (
        "adafruit_feather_nrf52832"
        if "sd132_v" in bootloader
        else "adafruit_itsybitsy_nrf52840"
    )
    _core_data(board, bootloader, testing_mode)
    assert _partition_address() % FLASH_SECTOR_SIZE == 0


def test_partition_size_is_the_same_on_every_sdk() -> None:
    _core_data("adafruit_feather_nrf52840", "mcuboot", version=cv.Version(2, 6, 1))
    assert _partition_address() == 0x100000 - PARTITION_SIZE


def test_unknown_flash_size_rejected() -> None:
    _core_data("bbc_microbit", "mcuboot")
    with pytest.raises(
        cv.Invalid, match="only nRF52832 or nRF52840 boards are supported"
    ):
        _partition_address()


def test_unknown_variant_flash_size_rejected() -> None:
    _core_data("nrf54l15dk", "mcuboot")
    with pytest.raises(cv.Invalid, match="does not know the flash size of board"):
        _partition_address()


def test_padding_keeps_one_app_gap(
    generate_main: Callable[[str | Path], str],
    component_config_path: Callable[[str], Path],
) -> None:
    """Page alignment leaves a gap below bank 1, which stays reserved."""
    generate_main(component_config_path("feather52832_sd132_v2.yaml"))
    sections = {s.name: s for s in zephyr_data()[KEY_PM_STATIC]}
    coredump = sections["coredump_partition"]
    padding = sections["coredump_padding"]
    assert coredump.address % FLASH_SECTOR_SIZE == 0
    assert padding.address == coredump.end_address
    assert padding.end_address == sections["Adafruit_nRF52_Bootloader_bank1"].address


def test_no_padding_when_aligned(
    generate_main: Callable[[str | Path], str],
    component_config_path: Callable[[str], Path],
) -> None:
    generate_main(component_config_path("mcumgr_adafruit.yaml"))
    assert all(s.name != "coredump_padding" for s in zephyr_data()[KEY_PM_STATIC])


def test_bootloader_without_layout_rejected() -> None:
    """The generic adafruit bootloader registers no sections, so the partition would
    land inside the bootloader if it were treated like MCUboot."""
    _core_data("adafruit_itsybitsy_nrf52840", "adafruit")
    with pytest.raises(cv.Invalid, match="flash layout of bootloader 'adafruit'"):
        _partition_address()


def test_mcumgr_slot_ends_below_partition(
    generate_main: Callable[[str | Path], str],
    component_config_path: Callable[[str], Path],
) -> None:
    """The OTA image slot1 must end at or before the coredump partition."""
    generate_main(component_config_path("mcumgr_adafruit.yaml"))
    # The logger's crash handler hooks are compiled in for the dump
    assert any(d.name == "USE_CRASH_HANDLER" for d in CORE.defines)
    sections = {s.name: s for s in zephyr_data()[KEY_PM_STATIC]}
    coredump = sections["coredump_partition"]
    assert coredump.address == 0xEC000
    match = re.search(
        r"slot1_partition: partition@\w+ \{\s*reg = <(0x\w+) (0x\w+)>",
        zephyr_data()[KEY_OVERLAY][""],
    )
    assert match is not None
    start, size = (int(v, 16) for v in match.groups())
    assert start + size <= coredump.address


def test_mcumgr_warns_that_the_slots_move(
    caplog: pytest.LogCaptureFixture,
    generate_main: Callable[[str | Path], str],
    component_config_path: Callable[[str], Path],
) -> None:
    with caplog.at_level(logging.WARNING):
        generate_main(component_config_path("mcumgr_adafruit.yaml"))
    assert "zephyr_coredump moves the OTA image slots" in caplog.text


def _zephyr_core_data(variant: str) -> None:
    platform, framework = zephyr_platform_framework(variant).value
    CORE.data[KEY_CORE] = {
        KEY_TARGET_PLATFORM: platform.value,
        KEY_TARGET_FRAMEWORK: framework.value,
    }
    CORE.data[KEY_ZEPHYR_RTOS] = empty_zephyr_data(variant=variant)


@pytest.mark.parametrize("variant", ["ESP32H2", "NRF54L15", "ESP32S3"])
def test_zephyr_platform_accepted(variant: str) -> None:
    _zephyr_core_data(variant)
    assert CONF_ID in CONFIG_SCHEMA({})


@pytest.mark.parametrize(
    ("variant", "dump_option"),
    [
        ("NRF54L15", "CONFIG_DEBUG_COREDUMP_MEMORY_DUMP_THREADS"),
        ("ESP32H2", "CONFIG_DEBUG_COREDUMP_MEMORY_DUMP_THREADS"),
        ("ESP32", "CONFIG_DEBUG_COREDUMP_MEMORY_DUMP_MIN"),
        ("ESP32S3", "CONFIG_DEBUG_COREDUMP_MEMORY_DUMP_MIN"),
    ],
)
def test_zephyr_to_code(variant: str, dump_option: str) -> None:
    """Zephyr RTOS requires a devicetree partition instead of placing one."""
    _zephyr_core_data(variant)
    with (
        patch("esphome.codegen.new_Pvariable"),
        patch("esphome.codegen.register_component", new=AsyncMock()),
    ):
        asyncio.run(to_code({CONF_ID: ID("coredump", is_declaration=True)}))

    prj_conf = zephyr_data()[KEY_PRJ_CONF][""]
    assert prj_conf["CONFIG_DEBUG_COREDUMP"][0] is True
    assert prj_conf["CONFIG_DEBUG_COREDUMP_BACKEND_FLASH_PARTITION"][0] is True
    assert prj_conf[dump_option][0] is True
    assert "CONFIG_PARTITION_MANAGER_ENABLED" not in prj_conf
    assert zephyr_data()[KEY_PARTITION_REQUIREMENTS] == [
        ("coredump_partition", "zephyr_coredump")
    ]
    assert any(d.name == "USE_CRASH_HANDLER" for d in CORE.defines)
    assert not any(d.name == "USE_ZEPHYR_COREDUMP_SP_FIX" for d in CORE.defines)
