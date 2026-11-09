"""Tests for zephyr_rtos uploads over zephyr_mcumgr's BLE and UDP transports."""

from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from esphome.components import esp32, rp2
from esphome.components.zephyr_rtos import has_network_ota, upload_program
from esphome.components.zephyr_rtos.const import KEY_ZEPHYR_RTOS
from esphome.const import (
    CONF_FRAMEWORK,
    KEY_CORE,
    KEY_FRAMEWORK_VERSION,
    KEY_TARGET_FRAMEWORK,
    KEY_TARGET_PLATFORM,
    PLATFORM_ESP32,
    PLATFORM_NRF,
    PLATFORM_RP2,
    Toolchain,
)
from esphome.core import CORE
from tests.unit_tests.components.zephyr_rtos.zephyr_state import empty_zephyr_data

BLE_MAC = "AA:BB:CC:DD:EE:FF"


def _mcumgr_config(transport: dict) -> dict:
    return {
        PLATFORM_NRF: {CONF_FRAMEWORK: {}},
        "ota": [{"platform": "zephyr_mcumgr", "transport": transport}],
    }


def _state(tmp_path: Path) -> None:
    CORE.data[KEY_ZEPHYR_RTOS] = empty_zephyr_data(variant="NRF54L15")
    CORE.data[KEY_CORE] = {
        KEY_FRAMEWORK_VERSION: "4.5.0",
        KEY_TARGET_PLATFORM: PLATFORM_NRF,
        KEY_TARGET_FRAMEWORK: "zephyr",
    }
    CORE.config_path = tmp_path / "test.yaml"
    CORE.build_path = tmp_path / "build"
    CORE.name = "test"
    CORE.toolchain = Toolchain.ZEPHYR


def _upload(
    config: dict, host: str, args: object = None
) -> tuple[bool, AsyncMock, AsyncMock]:
    with (
        patch("esphome.components.nrf52._smpmgr_upload", new=AsyncMock()) as mock_smp,
        patch(
            "esphome.components.nrf52.ota.smpmgr_scan",
            new=AsyncMock(return_value=BLE_MAC),
        ) as mock_scan,
    ):
        result = upload_program(config, args, host)
    return result, mock_smp, mock_scan


@pytest.mark.parametrize(
    ("transport", "expected"), [({"udp": True}, True), ({"ble": True}, False)]
)
def test_has_network_ota(transport: dict, expected: bool) -> None:
    assert has_network_ota(_mcumgr_config(transport)) is expected


@pytest.mark.parametrize(
    ("module", "platform", "framework", "expected"),
    [
        (esp32, PLATFORM_ESP32, "zephyr", True),
        (esp32, PLATFORM_ESP32, "esp-idf", False),
        (rp2, PLATFORM_RP2, "zephyr", True),
        (rp2, PLATFORM_RP2, "arduino", False),
    ],
)
def test_has_network_ota_needs_zephyr_rtos(
    module: ModuleType, platform: str, framework: str, expected: bool
) -> None:
    CORE.data[KEY_CORE] = {
        KEY_TARGET_PLATFORM: platform,
        KEY_TARGET_FRAMEWORK: framework,
    }
    assert module.has_network_ota(_mcumgr_config({"udp": True})) is expected


@pytest.mark.parametrize("host", [BLE_MAC, "BLE"])
def test_ble_uploads_over_smp(tmp_path: Path, host: str) -> None:
    _state(tmp_path)
    result, mock_smp, mock_scan = _upload(_mcumgr_config({"ble": True}), host)
    assert result is True
    assert mock_scan.await_count == (host == "BLE")
    mock_smp.assert_awaited_once_with(BLE_MAC, str(CORE.firmware_bin))


def test_udp_uploads_over_smp(tmp_path: Path) -> None:
    _state(tmp_path)
    result, mock_smp, _ = _upload(_mcumgr_config({"udp": True}), "fd00::1")
    assert result is True
    mock_smp.assert_awaited_once_with("fd00::1", str(CORE.firmware_bin), udp=True)


def test_udp_uploads_the_requested_file(tmp_path: Path) -> None:
    _state(tmp_path)
    args = SimpleNamespace(file="other.bin")
    result, mock_smp, _ = _upload(_mcumgr_config({"udp": True}), "fd00::1", args)
    assert result is True
    mock_smp.assert_awaited_once_with("fd00::1", "other.bin", udp=True)


def test_network_host_without_udp_falls_through(tmp_path: Path) -> None:
    _state(tmp_path)
    result, mock_smp, _ = _upload(_mcumgr_config({"ble": True}), "fd00::1")
    assert result is False
    mock_smp.assert_not_awaited()
