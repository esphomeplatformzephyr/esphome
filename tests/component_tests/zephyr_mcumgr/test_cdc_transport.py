"""Tests for which variants zephyr_mcumgr accepts the CDC transports on."""

from collections.abc import Callable
from pathlib import Path

from esphome.config import load_config
from esphome.core import CORE


def test_cdc_rejected_on_a_variant_without_usb(
    component_config_path: Callable[[str], Path],
) -> None:
    """nRF54L15 has no USB device, so CDC fails at config time, not at build time."""
    CORE.config_path = component_config_path("nrf54l15_cdc.yaml")
    res = load_config({})
    assert any(
        "'CDC' is not available on this variant" in str(e) for e in res.errors
    ), res.errors


def test_uart_accepted_on_a_variant_without_usb(
    component_config_path: Callable[[str], Path],
) -> None:
    """The same variant still takes a hardware UART."""
    CORE.config_path = component_config_path("nrf54l15_uart0.yaml")
    res = load_config({})
    assert not res.errors, res.errors
