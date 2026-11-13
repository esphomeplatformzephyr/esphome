from collections.abc import Callable
import re
from typing import Any, Final

import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.core import ID
from esphome.cpp_generator import MockObjClass

BOOTLOADER_MCUBOOT = "mcuboot"
BOOTLOADER_NONE = "none"

# A raw commit SHA (e.g. a fork-pinned boards revision, or a resolved sdk_source: ref)
# can't be passed to `git clone --branch` or `west init --mr`.
COMMIT_SHA_RE: Final = re.compile(r"^[0-9a-f]{40}$")

CONF_BOARD_SOURCE = "board_source"
# advanced: bootloader: -- shared by every variant whose default board can boot
# without MCUboot (rp2040, rp2350, ra4m1, stm32l4), so MCUboot is opt-in rather than
# assumed.
CONF_BOOTLOADER = "bootloader"
# framework: type: silabs only -- overrides commander_setup.py's pinned default. Its own
# release cadence is independent of the silabs SDK version, same reasoning as
# west_version:/ninja_version: vs. the Zephyr version.
CONF_COMMANDER_VERSION = "commander_version"
CONF_KCONFIG_OPTIONS = "kconfig_options"
CONF_MAIN_STACK_SIZE = "main_stack_size"
CONF_MODULES = "modules"
CONF_NINJA_VERSION = "ninja_version"
CONF_OVERLAYS = "overlays"
# advanced: runner: -- free-text west flash runner override. Valid names are
# board/SDK-specific and only knowable after CMake configure, so this is
# intentionally unvalidated here (see build_zephyr.log_available_runners()).
CONF_RUNNER = "runner"
CONF_SHIELDS = "shields"
CONF_SHIELD_SOURCE = "shield_source"
CONF_SINGLE_SLOT = "single_slot"
CONF_SNIPPETS = "snippets"
CONF_SNIPPET_SOURCE = "snippet_source"
CONF_WEST_VERSION = "west_version"

KEY_BOARD: Final = "board"
KEY_BOARD_ROOT: Final = "board_root"
KEY_BOOTLOADER: Final = "bootloader"
KEY_BOOT_LAYOUT: Final = "boot_layout"
KEY_EXTRA_BUILD_FILES: Final = "extra_build_files"
KEY_FRAMEWORK_TYPE: Final = "framework_type"
KEY_KCONFIG: Final = "kconfig"
KEY_MODULE_OVERRIDES: Final = "module_overrides"
KEY_MODULE_REQUESTS: Final = "module_requests"
KEY_OVERLAY: Final = "overlay"
KEY_OVERLAY_BUILDER: Final = "overlay_builder"
KEY_PARTITION_REQUIREMENTS: Final = "partition_requirements"
KEY_PM_STATIC: Final = "pm_static"
KEY_PRJ_CONF: Final = "prj_conf"
KEY_RUNNER: Final = "runner"
# Stashed onto the `framework: source:` dict itself (not ZephyrData) by
# dts_fetch.resolve_sdk_source_version(), which resolves a moving git ref (branch,
# or an omitted ref: meaning "default branch") to one concrete commit SHA per run.
# The to_code()-time consumers (dts_fetch.fetch_board_dts(), framework.py's SDK
# install) read this instead of independently re-resolving the moving ref against
# their own timer, so a single build can't silently mix commits across the three.
KEY_SDK_SOURCE_RESOLVED_REF: Final = "resolved_ref"
KEY_SHIELDS: Final = "shields"
KEY_SHIELD_ROOT: Final = "shield_root"
KEY_SINGLE_SLOT: Final = "single_slot"
KEY_SNIPPETS: Final = "snippets"
KEY_SNIPPET_ROOT: Final = "snippet_root"
KEY_SYSBUILD: Final = "sysbuild"
KEY_SYSBUILD_CONF: Final = "sysbuild_conf"
KEY_USER: Final = "user"
KEY_ZEPHYR = "zephyr"

# Devicetree compatible of Zephyr's SEGGER RTT UART driver
SEGGER_RTT_UART = "segger,rtt-uart"

# framework: version: "recommended" -- explicit alias for the variant's default_version,
# same as omitting `version:` entirely.
VERSION_RECOMMENDED: Final = "recommended"

zephyr_ns = cg.esphome_ns.namespace("zephyr")

# Base advanced: schema shared by every variant; extend() on extra per-variant keys.
ADVANCED_SCHEMA = cv.Schema(
    {
        cv.Optional(CONF_RUNNER): cv.string_strict,
        cv.Optional(CONF_MAIN_STACK_SIZE): cv.int_range(min=4096),
        # Override zephyr/requirements.txt's pinned west/ninja version, decoupled from
        # framework: version: since their release cadence isn't tied to Zephyr's.
        cv.Optional(CONF_WEST_VERSION): cv.string_strict,
        cv.Optional(CONF_NINJA_VERSION): cv.string_strict,
    }
)

BOOTLOADER_SCHEMA = cv.Schema(
    {
        cv.Optional(CONF_BOOTLOADER, default=BOOTLOADER_NONE): cv.one_of(
            BOOTLOADER_NONE, BOOTLOADER_MCUBOOT, lower=True
        ),
    }
)


def declare_component_id(component: MockObjClass) -> Callable[[Any], ID]:
    """cv.declare_id that also accepts an ID an earlier validation already declared, so a
    validated config can be validated again (the cached config is fed back through the
    schema)."""
    declare = cv.declare_id(component)

    def validator(value: Any) -> ID:
        return value if isinstance(value, ID) else declare(value)

    return validator
