from importlib import import_module
from typing import TYPE_CHECKING, Any, Final

import esphome.codegen as cg

if TYPE_CHECKING:
    from ..zephyr_rtos.const import KEY_FRAMEWORK_TYPE, KEY_SINGLE_SLOT, SEGGER_RTT_UART  # noqa: F401

BOOTLOADER_MCUBOOT = "mcuboot"

KEY_BOOTLOADER: Final = "bootloader"
KEY_EXTRA_BUILD_FILES: Final = "extra_build_files"
KEY_OVERLAY: Final = "overlay"
KEY_OVERLAY_BUILDER: Final = "overlay_builder"
KEY_PM_STATIC: Final = "pm_static"
KEY_KCONFIG: Final = "kconfig"
KEY_PRJ_CONF: Final = "prj_conf"
KEY_ZEPHYR = "zephyr"
KEY_ZEPHYR_RTOS: Final = "zephyr_rtos"
KEY_BOARD: Final = "board"
KEY_SYSBUILD: Final = "sysbuild"

zephyr_ns = cg.esphome_ns.namespace("zephyr")


# zephyr_rtos constants, loaded on first use since zephyr_rtos imports this package
_ZEPHYR_RTOS_CONSTS = frozenset(
    {"KEY_FRAMEWORK_TYPE", "KEY_SINGLE_SLOT", "SEGGER_RTT_UART"}
)


def __getattr__(name: str) -> Any:
    if name in _ZEPHYR_RTOS_CONSTS:
        return getattr(import_module("..zephyr_rtos.const", __package__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
