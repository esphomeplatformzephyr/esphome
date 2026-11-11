"""Silicon Labs chips on the Zephyr RTOS."""

from esphome.components.zephyr import (  # noqa: F401
    get_download_types,
    has_network_ota,
    process_stacktrace,
    run_compile,
    upload_program,
    zephyr_toolchain_config,
    zephyr_toolchain_copy_files as copy_files,
    zephyr_toolchain_to_code as to_code,
)
from esphome.const import PLATFORM_SILABS

CODEOWNERS = ["@rwrozelle"]
DOMAIN = "silabs"
IS_TARGET_PLATFORM = True
AUTO_LOAD = ["zephyr", "preferences"]

FAMILIES = frozenset({"silabs", "silabs_siwx91x"})

CONFIG_SCHEMA = zephyr_toolchain_config(PLATFORM_SILABS, FAMILIES)
