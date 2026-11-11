from typing import Any

from esphome import pins
from esphome.const import PLATFORM_NXP
from esphome.cpp_generator import MockObj
from esphome.schema_extractors import SCHEMA_EXTRACT, schema_extractor
from esphome.types import ConfigType


# Zephyr is the only framework so far; another one picks its own pin schema and
# pin code here by CORE.toolchain.
@schema_extractor("schema")
def _pin_schema(value: Any) -> Any:
    from esphome.components.zephyr.gpio import ZEPHYR_PIN_SCHEMA

    if value is SCHEMA_EXTRACT:
        return ZEPHYR_PIN_SCHEMA
    return ZEPHYR_PIN_SCHEMA(value)


@pins.PIN_SCHEMA_REGISTRY.register(PLATFORM_NXP, _pin_schema)
async def nxp_pin_to_code(config: ConfigType) -> MockObj:
    from esphome.components.zephyr.gpio import zephyr_pin_to_code

    return await zephyr_pin_to_code(config)
