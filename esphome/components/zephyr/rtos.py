"""Forwarders to zephyr_rtos for features only its builds have.

Helpers that use the build's store: queries answer None (or empty) for nrf52;
actions raise, since reaching one from an nrf52 path is a bug. Devicetree, RTT,
MCUboot and upload helpers forward as they are, since some run before the store
exists.
"""

from importlib import import_module
from types import ModuleType
from typing import TYPE_CHECKING

from esphome.core import CORE, EsphomeError
from esphome.types import ConfigType

from .const import KEY_ZEPHYR_RTOS

if TYPE_CHECKING:
    from ..zephyr_rtos.variants import ZephyrSDK, ZephyrVariant
    from . import PrjConfValueType


def _zephyr_rtos() -> ModuleType | None:
    if KEY_ZEPHYR_RTOS not in CORE.data:
        return None
    from .. import zephyr_rtos

    return zephyr_rtos


def _require_zephyr_rtos(name: str) -> ModuleType:
    if (rtos := _zephyr_rtos()) is None:
        raise EsphomeError(f"{name}() needs a Zephyr RTOS platform")
    return rtos


def zephyr_variant() -> str | None:
    return rtos.zephyr_variant() if (rtos := _zephyr_rtos()) else None


def zephyr_variant_family() -> str | None:
    return rtos.zephyr_variant_family() if (rtos := _zephyr_rtos()) else None


def zephyr_framework_type() -> str | None:
    return rtos.zephyr_framework_type() if (rtos := _zephyr_rtos()) else None


def zephyr_family_uart_drivers() -> frozenset[str]:
    return (
        rtos.zephyr_family_uart_drivers() if (rtos := _zephyr_rtos()) else frozenset()
    )


def zephyr_cdc_acm_label(id: int) -> str:
    if rtos := _zephyr_rtos():
        return rtos.zephyr_cdc_acm_label(id)
    return f"cdc_acm_uart{id}"


def zephyr_raise_prj_conf_min(name: str, minimum: int, image: str = "") -> None:
    _require_zephyr_rtos("zephyr_raise_prj_conf_min").zephyr_raise_prj_conf_min(
        name, minimum, image
    )


def zephyr_add_sysbuild_conf(
    name: str, value: "PrjConfValueType", required: bool = True
) -> None:
    _require_zephyr_rtos("zephyr_add_sysbuild_conf").zephyr_add_sysbuild_conf(
        name, value, required
    )


def zephyr_add_user(key: str, value: str) -> None:
    _require_zephyr_rtos("zephyr_add_user").zephyr_add_user(key, value)


def zephyr_add_blobs(module: str, allow_regex: str, sentinel_name: str) -> None:
    _require_zephyr_rtos("zephyr_add_blobs").zephyr_add_blobs(
        module, allow_regex, sentinel_name
    )


def request_zephyr_module(capability: str) -> None:
    _require_zephyr_rtos("request_zephyr_module").request_zephyr_module(capability)


def zephyr_require_partition(label: str, reason: str) -> None:
    _require_zephyr_rtos("zephyr_require_partition").zephyr_require_partition(
        label, reason
    )


def zephyr_configure_net_contexts() -> None:
    _require_zephyr_rtos(
        "zephyr_configure_net_contexts"
    ).zephyr_configure_net_contexts()


def zephyr_setup_i2c_pinctrl(
    board: str, bus_label: str, sda: int | None, scl: int | None
) -> tuple[str, str]:
    return _require_zephyr_rtos("zephyr_setup_i2c_pinctrl").zephyr_setup_i2c_pinctrl(
        board, bus_label, sda, scl
    )


def zephyr_setup_spi_pinctrl(
    board: str,
    bus_label: str,
    clk: int | None = None,
    miso: int | None = None,
    mosi: int | None = None,
    data_pins: list[int] | None = None,
) -> None:
    _require_zephyr_rtos("zephyr_setup_spi_pinctrl").zephyr_setup_spi_pinctrl(
        board, bus_label, clk, miso, mosi, data_pins
    )


def zephyr_setup_uart_pinctrl(
    board: str,
    port_label: str,
    tx_pin: int | None,
    rx_pin: int | None,
    baud_rate: int,
    node_known: bool = True,
) -> tuple[str, str]:
    return _require_zephyr_rtos("zephyr_setup_uart_pinctrl").zephyr_setup_uart_pinctrl(
        board, port_label, tx_pin, rx_pin, baud_rate, node_known
    )


def _module(name: str) -> ModuleType:
    return import_module(f"..zephyr_rtos.{name}", __package__)


def resolve_sdk(
    variant: "ZephyrVariant", framework_type: str | None
) -> "tuple[str, ZephyrSDK]":
    return _module("variants").resolve_sdk(variant, framework_type)


def reset_over_usb(config: ConfigType, port: str, timeout: float = 10.0) -> bool:
    return import_module("..zephyr_rtos", __package__).reset_over_usb(
        config, port, timeout
    )


def validate_dts_label_exists(platform: str, board: str, label: str) -> bool:
    return _module("dts_lookup").validate_dts_label_exists(platform, board, label)


def board_has_pm_states(board: str) -> bool:
    return _module("dts_lookup").board_has_pm_states(board)


def board_has_cpu_freq_pstates(board: str) -> bool:
    return _module("dts_lookup").board_has_cpu_freq_pstates(board)


def get_pinctrl_states(board: str, label: str) -> list[tuple[str, list[str]]] | None:
    return _module("dts_lookup").get_pinctrl_states(board, label)


def get_pinctrl_group_property(
    board: str, pinctrl_label: str, group_name: str, property_name: str
) -> list[int] | None:
    return _module("dts_lookup").get_pinctrl_group_property(
        board, pinctrl_label, group_name, property_name
    )


def resolve_zephyr_bus(
    platform: str, board: str, override_key: str, override: str | None = None
) -> str:
    return _module("dts_lookup").resolve_zephyr_bus(
        platform, board, override_key, override
    )


def get_can_controller_labels(board: str) -> list[str] | None:
    return _module("dts_lookup").get_can_controller_labels(board)


def validate_uart_label_override(value: str) -> str | None:
    return _module("dts_lookup").validate_uart_label_override(value)


def normalize_dts_label(value: str) -> str:
    return _module("dts_lookup").normalize_dts_label(value)


def resolve_uart_node_label(
    board: str, hw_uart: str, static_labels: dict[str, str]
) -> str:
    return _module("dts_lookup").resolve_uart_node_label(board, hw_uart, static_labels)


def rtt_channel(port: str) -> int | None:
    return _module("rtt").rtt_channel(port)


def rtt_label_channel(label: str) -> int | None:
    return _module("rtt").rtt_label_channel(label)


def claim_rtt_channel(channel: int, owner: str) -> None:
    _module("rtt").claim_rtt_channel(channel, owner)


def validate_rtt_port(port: str) -> str:
    return _module("rtt").validate_rtt_port(port)


def enable_rtt_node(channel: int, owner: str) -> str:
    return _module("rtt").enable_rtt_node(channel, owner)


def validate_swap_method(config: ConfigType) -> ConfigType:
    return _module("mcuboot").validate_swap_method(config)


def apply_swap_method(config: ConfigType) -> None:
    _module("mcuboot").apply_swap_method(config)


def zephyr_skip_bootloader_supported() -> bool:
    return _module("mcuboot").zephyr_skip_bootloader_supported()
