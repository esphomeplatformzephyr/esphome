"""Nordic nRF chips on the Zephyr RTOS."""

from esphome.components.zephyr_rtos import (  # noqa: F401
    copy_files,
    process_stacktrace,
    run_compile,
    upload_program,
    zephyr_rtos_platform_config,
)
from esphome.const import PLATFORM_NRF

CODEOWNERS = ["@rwrozelle"]
DOMAIN = "nrf"
IS_TARGET_PLATFORM = True
AUTO_LOAD = ["zephyr_rtos"]

FAMILIES = frozenset({"nordic"})

CONFIG_SCHEMA = zephyr_rtos_platform_config(PLATFORM_NRF, FAMILIES)
