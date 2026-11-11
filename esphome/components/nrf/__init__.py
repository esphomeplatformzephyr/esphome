"""Nordic nRF chips on the Zephyr RTOS."""

from esphome.components.zephyr import (  # noqa: F401
    get_download_types,
    has_network_ota,
    process_stacktrace,
    run_compile,
    show_logs,
    upload_program,
    zephyr_toolchain_config,
    zephyr_toolchain_copy_files as copy_files,
    zephyr_toolchain_to_code as to_code,
)
from esphome.const import PLATFORM_NRF

CODEOWNERS = ["@rwrozelle"]
DOMAIN = "nrf"
IS_TARGET_PLATFORM = True
AUTO_LOAD = ["zephyr", "preferences"]

FAMILIES = frozenset({"nordic"})

CONFIG_SCHEMA = zephyr_toolchain_config(PLATFORM_NRF, FAMILIES)
