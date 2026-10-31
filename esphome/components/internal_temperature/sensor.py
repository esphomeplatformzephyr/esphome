import logging

import esphome.codegen as cg
from esphome.components import sensor
from esphome.components.esp32 import get_esp32_variant, include_builtin_idf_component
from esphome.components.esp32.const import VARIANT_ESP32
from esphome.components.zephyr import (
    VARIANTS,
    zephyr_add_overlay,
    zephyr_add_prj_conf,
    zephyr_variant,
)
from esphome.config_helpers import filter_source_files_from_platform
import esphome.config_validation as cv
from esphome.const import (
    DEVICE_CLASS_TEMPERATURE,
    ENTITY_CATEGORY_DIAGNOSTIC,
    PLATFORM_BK72XX,
    PLATFORM_ESP32,
    PLATFORM_LN882X,
    PLATFORM_NRF52,
    PLATFORM_RP2,
    STATE_CLASS_MEASUREMENT,
    UNIT_CELSIUS,
    PlatformFramework,
)
from esphome.core import CORE
from esphome.types import ConfigType

_LOGGER = logging.getLogger(__name__)

internal_temperature_ns = cg.esphome_ns.namespace("internal_temperature")
InternalTemperatureSensor = internal_temperature_ns.class_(
    "InternalTemperatureSensor", sensor.Sensor, cg.PollingComponent
)

CONFIG_SCHEMA = cv.All(
    sensor.sensor_schema(
        InternalTemperatureSensor,
        unit_of_measurement=UNIT_CELSIUS,
        accuracy_decimals=1,
        device_class=DEVICE_CLASS_TEMPERATURE,
        state_class=STATE_CLASS_MEASUREMENT,
        entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
    ).extend(cv.polling_component_schema("60s")),
    cv.only_on_or_zephyr_rtos(
        [
            PLATFORM_ESP32,
            PLATFORM_RP2,
            PLATFORM_BK72XX,
            PLATFORM_NRF52,
            PLATFORM_LN882X,
        ]
    ),
)


async def to_code(config: ConfigType) -> None:
    var = await sensor.new_sensor(config)
    await cg.register_component(var, config)

    if CORE.is_esp32 and get_esp32_variant() == VARIANT_ESP32:
        # temprature_sens_read() lives in the esp_phy blob, which is excluded by default
        include_builtin_idf_component("esp_phy")

    if CORE.using_zephyr and CORE.is_nrf52:
        zephyr_add_prj_conf("SENSOR", True)
        zephyr_add_prj_conf("TEMP_NRF5", True)
    elif CORE.using_zephyr_rtos:
        # Every Zephyr sensor driver needs it; the driver itself follows its node's status
        zephyr_add_prj_conf("SENSOR", True)
        if nodes := VARIANTS[zephyr_variant()].die_temp_nodes:
            for label in nodes:
                zephyr_add_overlay(f'&{label} {{ status = "okay"; }};')
            cg.add_define("INTERNAL_TEMPERATURE_NODE_LABEL", cg.RawExpression(nodes[0]))
        else:
            _LOGGER.warning(
                "internal_temperature is not implemented for Zephyr variant '%s'; it "
                "reads the sensor that a 'die-temp0' devicetree alias points to, which "
                "the board or your own 'framework: overlays:' must provide",
                zephyr_variant(),
            )


FILTER_SOURCE_FILES = filter_source_files_from_platform(
    {
        "internal_temperature_esp32.cpp": {
            PlatformFramework.ESP32_ARDUINO,
            PlatformFramework.ESP32_IDF,
        },
        "internal_temperature_rp2.cpp": {PlatformFramework.RP2_ARDUINO},
        "internal_temperature_bk72xx.cpp": {
            PlatformFramework.BK72XX_ARDUINO,
        },
        "internal_temperature_ln882x.cpp": {
            PlatformFramework.LN882X_ARDUINO,
        },
        "internal_temperature_zephyr.cpp": {
            PlatformFramework.ESP32_ZEPHYR,
            PlatformFramework.NRF52_ZEPHYR,
            PlatformFramework.NRF_ZEPHYR,
            PlatformFramework.NXP_ZEPHYR,
            PlatformFramework.RENESAS_ZEPHYR,
            PlatformFramework.RP2_ZEPHYR,
            PlatformFramework.SILABS_ZEPHYR,
            PlatformFramework.STM32_ZEPHYR,
        },
    }
)
