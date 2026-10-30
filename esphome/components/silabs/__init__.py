"""Silicon Labs chips on the Zephyr RTOS."""

from esphome.components.zephyr_rtos import (  # noqa: F401
    copy_files,
    process_stacktrace,
    run_compile,
    upload_program,
    zephyr_rtos_platform_config,
)
from esphome.const import PLATFORM_SILABS

CODEOWNERS = ["@rwrozelle"]
DOMAIN = "silabs"
IS_TARGET_PLATFORM = True
AUTO_LOAD = ["zephyr_rtos"]

FAMILIES = frozenset({"silabs", "silabs_siwx91x"})

CONFIG_SCHEMA = zephyr_rtos_platform_config(PLATFORM_SILABS, FAMILIES)
