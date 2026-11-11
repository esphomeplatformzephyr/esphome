"""STMicroelectronics STM32 chips on the Zephyr RTOS."""

from esphome.components.zephyr import (  # noqa: F401
    get_download_types,
    process_stacktrace,
    run_compile,
    upload_program,
    zephyr_toolchain_config,
    zephyr_toolchain_copy_files as copy_files,
    zephyr_toolchain_to_code as to_code,
)
from esphome.const import PLATFORM_STM32

CODEOWNERS = ["@rwrozelle"]
DOMAIN = "stm32"
IS_TARGET_PLATFORM = True
AUTO_LOAD = ["zephyr", "preferences"]

FAMILIES = frozenset({"stm32"})

CONFIG_SCHEMA = zephyr_toolchain_config(PLATFORM_STM32, FAMILIES)
