"""nrf5340: Wi-Fi on NCS asks for the nRF7002 driver and supplicant west projects."""

from __future__ import annotations

import pytest

from esphome.components.nrf.zephyr_variants import nrf5340
from esphome.components.nrf52.framework import wanted_west_projects
from esphome.components.zephyr.const import KEY_ZEPHYR
import esphome.config_validation as cv
from esphome.const import KEY_CORE, KEY_FRAMEWORK_VERSION
from esphome.core import CORE
from tests.unit_tests.components.zephyr.zephyr_state import empty_zephyr_data

_WIFI_PROJECTS = {"nrf_wifi", "hostap"}


@pytest.mark.parametrize(
    ("framework_type", "config", "fetched"),
    [
        ("ncs", {"wifi": {}}, True),
        ("ncs", {}, False),
        ("zephyr", {"wifi": {}}, False),
    ],
)
def test_wifi_projects_only_for_ncs_wifi(
    framework_type: str, config: dict, fetched: bool
) -> None:
    CORE.data[KEY_ZEPHYR] = empty_zephyr_data("NRF5340", framework_type=framework_type)
    CORE.data[KEY_CORE][KEY_FRAMEWORK_VERSION] = cv.Version(3, 4, 1)
    CORE.config = config

    nrf5340.to_code({"board": "nrf7002dk/nrf5340/cpuapp"})

    projects = wanted_west_projects()
    assert (projects & _WIFI_PROJECTS) == (_WIFI_PROJECTS if fetched else set())
