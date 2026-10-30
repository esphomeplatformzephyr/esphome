"""Shared builder for CORE.data[KEY_ZEPHYR_RTOS] in unit tests."""

from __future__ import annotations

from typing import Any

from esphome.components import esp32, nrf, nxp, renesas, rp2, silabs, stm32
from esphome.components.zephyr_rtos import ZephyrRtosData
from esphome.components.zephyr_rtos.const import KEY_ZEPHYR_RTOS
from esphome.components.zephyr_rtos.variants import VARIANTS, set_core_data
import esphome.config_validation as cv
from esphome.const import KEY_CORE, Framework, Platform, PlatformFramework
from esphome.core import CORE

_PLATFORM_FAMILIES = {
    Platform.ESP32: esp32.ZEPHYR_FAMILIES,
    Platform.NRF: nrf.FAMILIES,
    Platform.NXP: nxp.FAMILIES,
    Platform.RENESAS: renesas.FAMILIES,
    Platform.RP2: rp2.ZEPHYR_FAMILIES,
    Platform.SILABS: silabs.FAMILIES,
    Platform.STM32: stm32.FAMILIES,
}


def zephyr_platform_framework(variant: str) -> PlatformFramework:
    """The <platform>_ZEPHYR pair whose platform builds variant."""
    family = VARIANTS[variant].family
    platform = next(p for p, fams in _PLATFORM_FAMILIES.items() if family in fams)
    return PlatformFramework((platform, Framework.ZEPHYR))


def empty_zephyr_data(
    variant: str | None = None,
    framework_type: str = "zephyr",
    **overrides: Any,
) -> dict[str, Any]:
    """Return a full ZephyrRtosData dict, built by production code so no key can drift.

    Does not touch CORE.data; assign the result to CORE.data[KEY_ZEPHYR_RTOS].
    """
    previous = CORE.data.get(KEY_ZEPHYR_RTOS)
    previous_core = dict(CORE.data.get(KEY_CORE, {}))
    previous_toolchain = CORE.toolchain
    CORE.data.setdefault(KEY_CORE, {})
    set_core_data(
        variant if variant in VARIANTS else next(iter(VARIANTS)),
        "some_board",
        "none",
        cv.Version(4, 5, 0),
        {},
        framework_type=framework_type,
    )
    data: dict[str, Any] = dict(CORE.data.pop(KEY_ZEPHYR_RTOS))
    CORE.data[KEY_CORE] = previous_core
    CORE.toolchain = previous_toolchain
    if previous is not None:
        CORE.data[KEY_ZEPHYR_RTOS] = previous
    assert set(data) == ZephyrRtosData.__required_keys__
    variant_info = VARIANTS.get(variant) if variant is not None else None
    data["variant"] = variant
    data["family"] = variant_info.family if variant_info is not None else None
    data.update(overrides)
    return data
