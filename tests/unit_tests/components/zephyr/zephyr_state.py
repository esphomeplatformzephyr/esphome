"""Shared builder for CORE.data[KEY_ZEPHYR] in unit tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from esphome.components import esp32, nrf, nxp, renesas, rp2, silabs, stm32
from esphome.components.zephyr import ZephyrData, dts_lookup
from esphome.components.zephyr.const import KEY_FAMILY, KEY_VARIANT, KEY_ZEPHYR
from esphome.components.zephyr.variants import VARIANTS, set_core_data
import esphome.config_validation as cv
from esphome.const import CONF_BOARD, KEY_CORE, Framework, Platform, PlatformFramework
from esphome.core import CORE

_PLATFORM_FAMILIES = {
    Platform.ESP32: esp32.ZEPHYR_FAMILIES,
    Platform.NRF: nrf.ZEPHYR_FAMILIES,
    Platform.NXP: nxp.ZEPHYR_FAMILIES,
    Platform.RENESAS: renesas.ZEPHYR_FAMILIES,
    Platform.RP2: rp2.ZEPHYR_FAMILIES,
    Platform.SILABS: silabs.ZEPHYR_FAMILIES,
    Platform.STM32: stm32.ZEPHYR_FAMILIES,
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
    """Return a full ZephyrData dict, built by production code so no key can drift.

    Does not touch CORE.data; assign the result to CORE.data[KEY_ZEPHYR]. Leaves
    CORE.toolchain set to Zephyr, as a Zephyr build does.
    """
    previous = CORE.data.get(KEY_ZEPHYR)
    previous_core = dict(CORE.data.get(KEY_CORE, {}))
    CORE.data.setdefault(KEY_CORE, {})
    set_core_data(
        variant if variant in VARIANTS else next(iter(VARIANTS)),
        {CONF_BOARD: "some_board"},
        cv.Version(4, 5, 0),
        framework_type=framework_type,
        bootloader="none",
    )
    data: dict[str, Any] = dict(CORE.data.pop(KEY_ZEPHYR))
    CORE.data[KEY_CORE] = previous_core
    if previous is not None:
        CORE.data[KEY_ZEPHYR] = previous
    assert set(data) == ZephyrData.__required_keys__ | ZephyrData.__optional_keys__
    variant_info = VARIANTS.get(variant) if variant is not None else None
    data[KEY_VARIANT] = variant
    data[KEY_FAMILY] = variant_info.family if variant_info is not None else None
    data.update(overrides)
    return data


def use_empty_devicetree(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every board a devicetree that reads fine but has no nodes."""
    edt = SimpleNamespace(scc_order=[], chosen_node=lambda name: None)
    monkeypatch.setattr(dts_lookup, "_get_edt", lambda board: edt)
