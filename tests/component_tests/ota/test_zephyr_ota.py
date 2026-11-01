"""OTA rollback on Zephyr RTOS builds depends on safe_mode confirming the image."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from esphome.core import CORE

ROLLBACK_YAML = "tests/component_tests/ota/test_zephyr_ota_rollback.yaml"
NO_SAFE_MODE_YAML = "tests/component_tests/ota/test_zephyr_ota_no_safe_mode.yaml"


def _defines() -> set[str]:
    return {define.name for define in CORE.defines}


def test_rollback_with_safe_mode(
    generate_main: Callable[[str], str], caplog: pytest.LogCaptureFixture
) -> None:
    generate_main(ROLLBACK_YAML)
    assert {"USE_OTA_MCUBOOT", "USE_OTA_ROLLBACK"} <= _defines()
    assert "OTA rollback requires safe_mode" not in caplog.text


def test_no_rollback_without_safe_mode(
    generate_main: Callable[[str], str], caplog: pytest.LogCaptureFixture
) -> None:
    """hal.cpp then confirms the image at boot, so MCUboot keeps it."""
    generate_main(NO_SAFE_MODE_YAML)
    assert "USE_OTA_MCUBOOT" in _defines()
    assert "USE_OTA_ROLLBACK" not in _defines()
    assert "OTA rollback requires safe_mode, disabling rollback support" in caplog.text
