"""Tests for the stm32: platform schema."""

import pytest

from esphome.components import stm32
from esphome.components.zephyr import zephyr_toolchain_schema
import esphome.config_validation as cv
from esphome.const import CONF_VARIANT
from esphome.schema_extractors import SCHEMA_EXTRACT


def test_schema_extract_gives_the_platform_schema() -> None:
    schema = stm32.CONFIG_SCHEMA(SCHEMA_EXTRACT)

    assert isinstance(schema, cv.Schema)
    assert schema({CONF_VARIANT: "stm32c5"})[CONF_VARIANT] == "STM32C5"


def test_toolchain_schema_lists_only_the_families_variants() -> None:
    with pytest.raises(cv.Invalid, match="Unknown value"):
        zephyr_toolchain_schema(frozenset({"other"}))({CONF_VARIANT: "STM32C5"})
