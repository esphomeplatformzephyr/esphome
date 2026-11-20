"""Shared builder for CORE.data[KEY_ZEPHYR] in unit tests."""

from __future__ import annotations

from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from esphome.components.esp32 import zephyr_variants as esp32_variants
from esphome.components.nrf import zephyr_variants as nrf_variants
from esphome.components.nxp import zephyr_variants as nxp_variants
from esphome.components.renesas import zephyr_variants as renesas_variants
from esphome.components.rp2 import zephyr_variants as rp2_variants
from esphome.components.silabs import zephyr_variants as silabs_variants
from esphome.components.stm32 import zephyr_variants as stm32_variants
from esphome.components.zephyr import ZephyrData, dts_lookup
from esphome.components.zephyr.const import (
    KEY_FAMILY,
    KEY_VARIANT,
    KEY_VARIANT_INFO,
    KEY_VARIANT_MODULE,
    KEY_ZEPHYR,
)
from esphome.components.zephyr.variants import (
    ZephyrVariant,
    ZephyrVariantResolved,
    set_variant_core_data,
)
import esphome.config_validation as cv
from esphome.const import CONF_BOARD, KEY_CORE, Framework, Platform, PlatformFramework
from esphome.core import CORE

_PLATFORM_VARIANT_MODULES: dict[Platform, dict[str, ModuleType]] = {
    Platform.ESP32: esp32_variants.VARIANT_MODULES,
    Platform.NRF: nrf_variants.VARIANT_MODULES,
    Platform.NXP: nxp_variants.VARIANT_MODULES,
    Platform.RENESAS: renesas_variants.VARIANT_MODULES,
    Platform.RP2: rp2_variants.VARIANT_MODULES,
    Platform.SILABS: silabs_variants.VARIANT_MODULES,
    Platform.STM32: stm32_variants.VARIANT_MODULES,
}
# Every platform's variants, for tests that cover them all
VARIANT_MODULES: dict[str, ModuleType] = {
    name: mod
    for modules in _PLATFORM_VARIANT_MODULES.values()
    for name, mod in modules.items()
}
VARIANTS: dict[str, ZephyrVariant] = {
    name: mod.VARIANT for name, mod in VARIANT_MODULES.items()
}


def zephyr_platform_framework(variant: str) -> PlatformFramework:
    """The <platform>_ZEPHYR pair whose platform builds variant."""
    platform = next(
        p for p, mods in _PLATFORM_VARIANT_MODULES.items() if variant in mods
    )
    return PlatformFramework((platform, Framework.ZEPHYR))


def empty_zephyr_data(
    variant: str | None = None,
    framework_type: str = "zephyr",
    **overrides: Any,
) -> dict[str, Any]:
    """Return a full ZephyrData dict, built by production code so no key can drift.

    Does not touch CORE.data; assign the result to CORE.data[KEY_ZEPHYR]. Leaves
    CORE.toolchain set to Zephyr, as a Zephyr build does.
    """
    previous = CORE.data.get(KEY_ZEPHYR)
    previous_core = dict(CORE.data.get(KEY_CORE, {}))
    CORE.data.setdefault(KEY_CORE, {})
    name = variant if variant in VARIANTS else next(iter(VARIANTS))
    set_variant_core_data(
        ZephyrVariantResolved(
            VARIANTS[name],
            VARIANTS[name].manifest_for(framework_type),
            cv.Version(4, 5, 0),
        ),
        {CONF_BOARD: "some_board"},
        bootloader="none",
    )
    data: dict[str, Any] = dict(CORE.data.pop(KEY_ZEPHYR))
    # Set by the platform's schema, not set_variant_core_data
    data[KEY_VARIANT_MODULE] = VARIANT_MODULES[name]
    CORE.data[KEY_CORE] = previous_core
    if previous is not None:
        CORE.data[KEY_ZEPHYR] = previous
    assert set(data) == ZephyrData.__required_keys__ | ZephyrData.__optional_keys__
    variant_info = VARIANTS.get(variant) if variant is not None else None
    data[KEY_VARIANT] = variant
    data[KEY_VARIANT_INFO] = variant_info
    data[KEY_VARIANT_MODULE] = VARIANT_MODULES.get(variant)
    data[KEY_FAMILY] = (
        variant_info.family_module.FAMILY
        if variant_info is not None and variant_info.family_module
        else None
    )
    data.update(overrides)
    return data


def use_empty_devicetree(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every board a devicetree that reads fine but has no nodes."""
    edt = SimpleNamespace(scc_order=[], chosen_node=lambda name: None)
    monkeypatch.setattr(dts_lookup, "_get_edt", lambda board: edt)
