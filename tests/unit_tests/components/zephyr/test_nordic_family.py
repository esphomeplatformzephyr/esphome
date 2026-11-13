"""nordic_family: the random number source, and the nRF54L I2C port check."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from esphome.components.nrf.zephyr_variants import nordic_family
from esphome.components.zephyr import zephyr_data
from esphome.components.zephyr.const import KEY_PRJ_CONF, KEY_ZEPHYR
from esphome.core import CORE, EsphomeError
from tests.unit_tests.components.zephyr.zephyr_state import empty_zephyr_data

_WARNING = "insecure software one is used"


def _run(framework_type: str, config: dict) -> None:
    CORE.data[KEY_ZEPHYR] = empty_zephyr_data("NRF5340", framework_type=framework_type)
    CORE.config = config
    nordic_family.to_code({"framework": {"log_level": "ERROR"}})


@pytest.mark.parametrize(
    ("framework_type", "config", "kconfig", "warns"),
    [
        ("zephyr", {"network": {}}, "TEST_RANDOM_GENERATOR", True),
        ("zephyr", {}, "TEST_RANDOM_GENERATOR", False),
        ("ncs", {"network": {}}, "ENTROPY_GENERATOR", False),
    ],
)
def test_nrf5340_random_source(
    caplog: pytest.LogCaptureFixture,
    framework_type: str,
    config: dict,
    kconfig: str,
    warns: bool,
) -> None:
    _run(framework_type, config)

    prj_conf = zephyr_data()[KEY_PRJ_CONF][""]
    assert {k for k in prj_conf if k.endswith("_GENERATOR")} == {f"CONFIG_{kconfig}"}
    assert (_WARNING in caplog.text) is warns


def _psel(fun: int, pin: int) -> int:
    """NRF_PSEL(fun, pin // 32, pin % 32)."""
    return (fun << 24) | pin


_SDA, _SCL = 12, 11  # NRF_FUN_TWIM_SDA, NRF_FUN_TWIM_SCL


def _check(
    variant: str,
    sda: int | None,
    scl: int | None,
    board_psels: list[int] | None = None,
) -> None:
    CORE.data[KEY_ZEPHYR] = empty_zephyr_data(variant)
    with (
        patch(
            "esphome.components.zephyr.dts_lookup.get_pinctrl_states",
            return_value=[("i2c21_default", ["group1"]), ("i2c21_sleep", ["group1"])],
        ),
        patch(
            "esphome.components.zephyr.dts_lookup.get_pinctrl_group_property",
            return_value=board_psels,
        ),
    ):
        nordic_family.i2c_check_pins("i2c21", sda, scl, variant)


@pytest.mark.parametrize(
    ("variant", "sda", "scl"),
    [
        ("NRF54L15", 32 + 10, 32 + 11),  # P1
        ("NRF54LM20A", 96 + 1, 96 + 2),  # P3, the LM20's second port of domain 20
        ("NRF52", 4, 32 + 5),  # no port rule outside the nRF54L
    ],
)
def test_i2c_check_pins_accepts(variant: str, sda: int, scl: int) -> None:
    _check(variant, sda, scl)


@pytest.mark.parametrize(
    ("variant", "sda", "scl", "match"),
    [
        ("NRF54L15", 4, 32 + 11, r"sda: P0\.04 is not in the power domain .* P1$"),
        ("NRF54LM20A", 32 + 1, 64 + 2, r"scl: P2\.02 .* use a pin on P1 or P3"),
        ("NRF54LM20A", 32 + 1, 96 + 2, "must be on the same GPIO port"),
    ],
)
def test_i2c_check_pins_rejects(variant: str, sda: int, scl: int, match: str) -> None:
    with pytest.raises(EsphomeError, match=match):
        _check(variant, sda, scl)


@pytest.mark.parametrize(("board_scl", "ok"), [(96 + 5, True), (32 + 5, False)])
def test_i2c_check_pins_one_pin_against_the_board_wiring(
    board_scl: int, ok: bool
) -> None:
    """Only sda: is given; scl: stays on the board's pin, which must share its port."""
    psels = [_psel(_SDA, 96 + 4), _psel(_SCL, board_scl)]
    if ok:
        _check("NRF54LM20A", 96 + 1, None, psels)
    else:
        with pytest.raises(EsphomeError, match="must be on the same GPIO port"):
            _check("NRF54LM20A", 96 + 1, None, psels)
