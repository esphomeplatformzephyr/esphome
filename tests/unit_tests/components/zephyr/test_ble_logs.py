"""Tests for Zephyr RTOS logs over BLE (ble_nus), handled as on nrf52."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from esphome.components.zephyr import show_logs
from esphome.core import CORE

BLE_MAC = "AA:BB:CC:DD:EE:FF"
_BLE_LOGGER = "esphome.components.nrf52.ble_logger"


@pytest.mark.parametrize(
    ("device", "connects_to"),
    [("BLE", BLE_MAC), (BLE_MAC, BLE_MAC), ("/dev/ttyACM0", None)],
)
def test_show_logs(device: str, connects_to: str | None) -> None:
    CORE.name = "test"
    with (
        patch(
            f"{_BLE_LOGGER}.logger_scan",
            new=AsyncMock(return_value=SimpleNamespace(address=BLE_MAC)),
        ) as scan,
        patch(f"{_BLE_LOGGER}.logger_connect", new=AsyncMock()) as connect,
    ):
        handled = show_logs({}, object(), [device])
    assert handled is (connects_to is not None)
    assert scan.await_count == (device == "BLE")
    if connects_to is None:
        connect.assert_not_awaited()
    else:
        connect.assert_awaited_once_with(connects_to)
