"""internal_temperature code generation on Zephyr RTOS builds."""

import logging
from unittest.mock import AsyncMock, Mock, call, patch

import pytest

from esphome.components.internal_temperature import sensor as internal_temperature
from esphome.components.zephyr_rtos.const import KEY_ZEPHYR_RTOS
from esphome.const import KEY_CORE, KEY_TARGET_FRAMEWORK, KEY_TARGET_PLATFORM
from esphome.core import CORE
from tests.unit_tests.components.zephyr_rtos.zephyr_state import (
    empty_zephyr_data,
    zephyr_platform_framework,
)


def _set_variant(variant: str) -> None:
    platform, _ = zephyr_platform_framework(variant).value
    CORE.data[KEY_CORE] = {
        KEY_TARGET_PLATFORM: platform,
        KEY_TARGET_FRAMEWORK: "zephyr",
    }
    CORE.data[KEY_ZEPHYR_RTOS] = empty_zephyr_data(variant=variant)


async def _run_to_code() -> tuple[Mock, Mock, Mock]:
    with (
        patch.object(internal_temperature.sensor, "new_sensor", AsyncMock()),
        patch.object(internal_temperature.cg, "register_component", AsyncMock()),
        patch.object(internal_temperature, "zephyr_add_prj_conf") as prj_conf,
        patch.object(internal_temperature, "zephyr_add_overlay") as overlay,
        patch.object(internal_temperature.cg, "add_define") as add_define,
    ):
        await internal_temperature.to_code({})
    return prj_conf, overlay, add_define


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("variant", "labels"),
    [
        ("ESP32C6", ["coretemp"]),
        ("NRF54L15", ["temp"]),
        ("RP2040", ["die_temp", "adc"]),
        ("STM32F4", ["die_temp", "adc1"]),
    ],
)
async def test_variant_nodes_are_turned_on(variant: str, labels: list[str]) -> None:
    """Each die_temp_nodes label gets only a status overlay; the first names the sensor."""
    _set_variant(variant)

    prj_conf, overlay, add_define = await _run_to_code()

    prj_conf.assert_called_once_with("SENSOR", True)
    assert overlay.call_args_list == [
        call(f'&{label} {{ status = "okay"; }};') for label in labels
    ]
    add_define.assert_called_once()
    name, value = add_define.call_args.args
    assert name == "INTERNAL_TEMPERATURE_NODE_LABEL"
    assert str(value) == labels[0]


@pytest.mark.asyncio
async def test_variant_without_nodes_warns(caplog: pytest.LogCaptureFixture) -> None:
    """No die_temp_nodes: no overlay or define, and a warning about die-temp0."""
    _set_variant("NRF5340")

    with caplog.at_level(logging.WARNING):
        prj_conf, overlay, add_define = await _run_to_code()

    prj_conf.assert_called_once_with("SENSOR", True)
    overlay.assert_not_called()
    add_define.assert_not_called()
    assert "not implemented for Zephyr variant 'NRF5340'" in caplog.text
    assert "die-temp0" in caplog.text
