"""Renesas chips on the Zephyr RTOS."""

from esphome.components.zephyr_rtos import (  # noqa: F401
    copy_files,
    process_stacktrace,
    run_compile,
    upload_program,
    zephyr_rtos_platform_config,
    zephyr_rtos_to_code,
)
from esphome.const import PLATFORM_RENESAS
from esphome.core import CoroPriority, coroutine_with_priority
from esphome.types import ConfigType

CODEOWNERS = ["@rwrozelle"]
DOMAIN = "renesas"
IS_TARGET_PLATFORM = True
AUTO_LOAD = ["zephyr_rtos"]

FAMILIES = frozenset({"renesas"})

CONFIG_SCHEMA = zephyr_rtos_platform_config(PLATFORM_RENESAS, FAMILIES)


@coroutine_with_priority(CoroPriority.PLATFORM)
async def to_code(config: ConfigType) -> None:
    await zephyr_rtos_to_code(config)
