"""Tests for the Zephyr RTOS platform components."""

from collections.abc import Callable
from pathlib import Path

import pytest

from esphome.components import logger
from esphome.components.zephyr import zephyr_data
from esphome.components.zephyr.const import KEY_PRJ_CONF
from esphome.config import read_config
from esphome.const import (
    KEY_CORE,
    KEY_TARGET_FRAMEWORK,
    KEY_TARGET_PLATFORM,
    PLATFORM_ESP32,
    PLATFORM_NRF,
    PLATFORM_NXP,
    PLATFORM_RENESAS,
    PLATFORM_RP2,
    PLATFORM_SILABS,
    PLATFORM_STM32,
)
from esphome.core import CORE

CONFIG_DIR = Path(__file__).parent / "config"


@pytest.mark.parametrize(
    "platform",
    [
        PLATFORM_ESP32,
        PLATFORM_NRF,
        PLATFORM_NXP,
        PLATFORM_RENESAS,
        PLATFORM_RP2,
        PLATFORM_SILABS,
        PLATFORM_STM32,
    ],
)
def test_platform_generates_zephyr_build(
    generate_main: Callable[[str | Path], str], platform: str
) -> None:
    main_cpp = generate_main(CONFIG_DIR / f"{platform}.yaml")

    assert CORE.data[KEY_CORE][KEY_TARGET_PLATFORM] == platform
    assert CORE.data[KEY_CORE][KEY_TARGET_FRAMEWORK] == "zephyr"
    assert CORE.using_zephyr_rtos
    assert "logger::Logger" in main_cpp
    assert zephyr_data()[KEY_PRJ_CONF][""]["CONFIG_TEST_OPTION"] == (True, True)
    assert "logger_zephyr.cpp" not in logger.FILTER_SOURCE_FILES()


def test_variant_from_another_family_is_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "wrong_family.yaml"
    config.write_text(
        (CONFIG_DIR / "stm32.yaml").read_text().replace("STM32F4", "NRF52")
    )
    CORE.config_path = config

    assert read_config({}) is None
    assert "Unknown value 'NRF52'" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("snippet", "message"),
    [
        (
            "esp32_touch:\n",
            "Component esp32_touch is not available on esp32 with framework: type: zephyr",
        ),
        (
            "output:\n  - platform: ledc\n    pin: 2\n    id: o1\n",
            "Component output.ledc is not available on esp32 with framework: type: zephyr",
        ),
        (
            "deep_sleep:\n  esp32_ext1_wakeup:\n    pins: [2]\n    mode: ALL_LOW\n",
            "This feature is not available on esp32 with framework: type: zephyr",
        ),
    ],
)
def test_esp32_only_features_are_rejected_on_zephyr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], snippet: str, message: str
) -> None:
    config = tmp_path / "esp32_only.yaml"
    config.write_text((CONFIG_DIR / "esp32.yaml").read_text() + snippet)
    CORE.config_path = config

    assert read_config({}) is None
    assert message in capsys.readouterr().out
