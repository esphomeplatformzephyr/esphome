"""Tests for the Zephyr RTOS platform components."""

from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest

from esphome.components import logger
from esphome.components.zephyr import zephyr_data
from esphome.components.zephyr.const import KEY_OVERLAY, KEY_PRJ_CONF
from esphome.config import read_config
import esphome.config_validation as cv
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
    assert CORE.using_toolchain_zephyr
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


@pytest.mark.parametrize(
    ("platform", "variant", "expected"),
    [
        (PLATFORM_ESP32, "ESP32C3", ["esp32_c3_zephyr", "esp32_zephyr"]),
        (PLATFORM_ESP32, "ESP32", ["esp32_zephyr"]),
        (PLATFORM_RP2, "RP2040", ["rp2_2040_zephyr", "rp2_zephyr"]),
        (PLATFORM_NRF, "NRF54L15", ["nrf_54l15", "nrf"]),
        (PLATFORM_NXP, "MCXN236", ["nxp_mcxn236", "nxp"]),
        (PLATFORM_STM32, "STM32L4", ["stm32_l4", "stm32"]),
    ],
)
def test_split_default_keys(platform: str, variant: str, expected: list[str]) -> None:
    CORE.data[KEY_CORE] = {KEY_TARGET_PLATFORM: platform}
    with patch("esphome.components.zephyr.zephyr_variant", return_value=variant):
        assert cv._zephyr_rtos_default_keys() == expected


@pytest.mark.parametrize(
    ("fixture", "vout", "uicr_erase", "adafruit", "reg1_mode"),
    [
        # Index into 1.8, 2.1, 2.4, 2.7, 3.0, 3.3 V, as platform: nrf52
        ("nrf_reg0.yaml", "5", True, False, "NRF5X_REG_MODE_DCDC"),
        ("nrf_reg0_adafruit.yaml", "4", False, True, "NRF5X_REG_MODE_LDO"),
        # Without either option the board's own devicetree decides
        ("nrf.yaml", None, False, False, None),
    ],
)
def test_nrf52_regulators(
    generate_main: Callable[[str | Path], str],
    fixture: str,
    vout: str | None,
    uicr_erase: bool,
    adafruit: bool,
    reg1_mode: str | None,
) -> None:
    generate_main(CONFIG_DIR / fixture)
    overlay = "".join(zephyr_data()[KEY_OVERLAY].values())
    if reg1_mode is None:
        assert "&reg1" not in overlay
    else:
        assert f"&reg1 {{ regulator-initial-mode = <{reg1_mode}>; }};" in overlay
    defines = {d.name: str(d.value) for d in CORE.defines}
    assert defines.get("USE_NRF52_REG0_VOUT") == vout
    assert ("USE_NRF52_UICR_ERASE" in defines) is uicr_erase
    assert ("USE_ZEPHYR_NRF52_ADAFRUIT_BOOTLOADER" in defines) is adafruit
    # The debug component's bootloader report, as platform: nrf52's
    assert ("USE_BOOTLOADER_MCUBOOT" in defines) is not adafruit
    assert defines.get("USE_SOFTDEVICE_ID") == ("140" if adafruit else None)
    assert defines.get("USE_SOFTDEVICE_VERSION") == ("6" if adafruit else None)
    prj_conf = zephyr_data()[KEY_PRJ_CONF][""]
    assert ("CONFIG_NRFX_NVMC" in prj_conf) is uicr_erase
