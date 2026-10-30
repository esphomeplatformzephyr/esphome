"""The esp32-family Zephyr variants restate ESP-IDF's ADC1 pin->channel table, which
the adc component also carries; the two must not drift apart."""

from __future__ import annotations

import pytest

from esphome.components.adc import ESP32_VARIANT_ADC1_PIN_TO_CHANNEL
from esphome.components.esp32.const import (
    VARIANT_ESP32,
    VARIANT_ESP32C3,
    VARIANT_ESP32C5,
    VARIANT_ESP32C6,
    VARIANT_ESP32H2,
    VARIANT_ESP32S3,
)
from esphome.components.renesas.const import VARIANT_RA4M1
from esphome.components.zephyr_rtos.variants import VARIANTS


@pytest.mark.parametrize(
    "variant",
    [
        VARIANT_ESP32,
        VARIANT_ESP32C3,
        VARIANT_ESP32C5,
        VARIANT_ESP32C6,
        VARIANT_ESP32H2,
        VARIANT_ESP32S3,
    ],
)
def test_adc1_channel_map_matches_esp_idf(variant: str) -> None:
    idf_map = {
        pin: int(str(channel).rsplit("_", 1)[1])
        for pin, channel in ESP32_VARIANT_ADC1_PIN_TO_CHANNEL[variant].items()
    }
    assert VARIANTS[variant].adc1_channel_map == idf_map


def test_ra4m1_adc_map_matches_uno_r4_analog_header() -> None:
    """Keys are port*16+pin, like every RA pin. Expected values are Zephyr's
    boards/arduino/uno_r4 io-channel-map: A0..A5."""
    ain = VARIANTS[VARIANT_RA4M1].adc_ain_map
    assert ain[0 * 16 + 14] == "AN009"  # A0, P014
    assert ain[0 * 16 + 0] == "AN000"  # A1, P000
    assert ain[0 * 16 + 1] == "AN001"  # A2, P001
    assert ain[0 * 16 + 2] == "AN002"  # A3, P002
    assert ain[1 * 16 + 1] == "AN021"  # A4, P101
    assert ain[1 * 16 + 0] == "AN022"  # A5, P100
