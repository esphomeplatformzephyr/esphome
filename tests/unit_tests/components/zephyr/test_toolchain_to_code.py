"""Tests for the Zephyr toolchain code generation, zephyr_toolchain_to_code()."""

import asyncio
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest

from esphome.components import stm32
from esphome.components.zephyr import zephyr_data, zephyr_toolchain_to_code
from esphome.components.zephyr.const import KEY_PRJ_CONF
from esphome.components.zephyr.variants import ZEPHYR_MAINLINE
from esphome.const import KEY_CORE
from esphome.core import CORE, EsphomeError
from esphome.types import ConfigType


@pytest.fixture
def config() -> ConfigType:
    """A validated STM32C5 platform config."""
    CORE.data[KEY_CORE] = {}
    CORE.config = {}
    return stm32.CONFIG_SCHEMA({"variant": "STM32C5", "board": "nucleo_c5a3zg"})


@pytest.fixture
def install() -> Iterator[MagicMock]:
    # The jobs code generation schedules are not run here, so none are scheduled
    with (
        patch("esphome.components.zephyr.framework.check_and_install") as mock,
        patch.object(CORE, "add_job"),
    ):
        yield mock


def _board(valid: bool = True, revision: bool | None = None) -> object:
    return patch.multiple(
        "esphome.components.zephyr.dts_lookup",
        validate_board=MagicMock(return_value=valid),
        validate_board_revision=MagicMock(return_value=revision),
        log_board_capabilities=MagicMock(),
    )


def _prj_conf() -> dict[str, object]:
    return {
        name.removeprefix("CONFIG_"): value
        for name, (value, _) in zephyr_data()[KEY_PRJ_CONF][""].items()
    }


def test_installs_then_runs_base_family_and_variant(
    config: ConfigType, install: MagicMock
) -> None:
    with (
        _board(),
        patch(
            "esphome.components.stm32.zephyr_variants.stm32c5.get_rng_node_label",
            return_value="rng",
        ),
    ):
        asyncio.run(zephyr_toolchain_to_code(config))

    install.assert_called_once_with(
        ZEPHYR_MAINLINE, "v4.5.0", frozenset({"hal_stm32"}), None, None, None
    )
    prj_conf = _prj_conf()
    assert prj_conf["STD_CPP20"] is True  # base
    assert prj_conf["MAIN_STACK_SIZE"] == 4096  # not the base's 2048
    assert prj_conf["FPU"] is True  # family
    assert prj_conf["REQUIRES_FULL_LIBCPP"] is True  # family
    assert prj_conf["REBOOT"] is True  # variant
    assert "-DUSE_ZEPHYR" in CORE.build_flags


@pytest.mark.parametrize(
    ("valid", "revision", "message"),
    [
        pytest.param(False, None, "was not found", id="unknown_board"),
        pytest.param(True, False, "revision that is not valid", id="bad_revision"),
    ],
)
def test_rejects_the_board(
    config: ConfigType,
    install: MagicMock,
    valid: bool,
    revision: bool | None,
    message: str,
) -> None:
    with _board(valid, revision), pytest.raises(EsphomeError, match=message):
        asyncio.run(zephyr_toolchain_to_code(config))
    # The board is looked up in the installed workspace
    install.assert_called_once()
