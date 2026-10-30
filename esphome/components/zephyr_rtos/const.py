import re
from typing import Final

import esphome.codegen as cg
from esphome.components.zephyr.const import (  # noqa: F401  # pylint: disable=unused-import
    BOOTLOADER_MCUBOOT,
    KEY_BOARD,
    KEY_BOOTLOADER,
    KEY_EXTRA_BUILD_FILES,
    KEY_KCONFIG,
    KEY_OVERLAY,
    KEY_OVERLAY_BUILDER,
    KEY_PM_STATIC,
    KEY_PRJ_CONF,
    KEY_SYSBUILD,
    KEY_ZEPHYR,
    KEY_ZEPHYR_RTOS,
    zephyr_ns,
)
import esphome.config_validation as cv

zephyr_rtos_ns = cg.esphome_ns.namespace("zephyr_rtos")

# advanced: bootloader: -- shared by every variant whose default board can boot
# without MCUboot (rp2040, rp2350, ra4m1, stm32l4), so MCUboot is opt-in rather than
# assumed.
CONF_BOOTLOADER = "bootloader"
BOOTLOADER_NONE = "none"

# advanced: runner: -- free-text west flash runner override. Valid names are
# board/SDK-specific and only knowable after CMake configure, so this is
# intentionally unvalidated here (see build_zephyr.log_available_runners()).
CONF_RUNNER = "runner"
CONF_MAIN_STACK_SIZE = "main_stack_size"
CONF_WEST_VERSION = "west_version"
CONF_NINJA_VERSION = "ninja_version"

# Base advanced: schema shared by every variant; extend() on extra per-variant keys.
ADVANCED_SCHEMA = cv.Schema(
    {
        cv.Optional(CONF_RUNNER): cv.string_strict,
        cv.Optional(CONF_MAIN_STACK_SIZE): cv.int_range(min=4096),
        # Override requirements_west.txt's pinned west/ninja version, decoupled from
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

KEY_RUNNER: Final = "runner"
KEY_PARTITION_REQUIREMENTS: Final = "partition_requirements"
KEY_BOOT_LAYOUT: Final = "boot_layout"
KEY_SYSBUILD_CONF: Final = "sysbuild_conf"
KEY_BOARD_ROOT: Final = "board_root"
KEY_USER: Final = "user"
KEY_FRAMEWORK_TYPE: Final = "framework_type"
KEY_MODULE_REQUESTS: Final = "module_requests"
KEY_MODULE_OVERRIDES: Final = "module_overrides"
# Stashed onto the `framework: source:` dict itself (not ZephyrData) by
# dts_fetch.resolve_sdk_source_version(), which resolves a moving git ref (branch,
# or an omitted ref: meaning "default branch") to one concrete commit SHA per run.
# The to_code()-time consumers (dts_fetch.fetch_board_dts(), framework_west's SDK
# install) read this instead of independently re-resolving the moving ref against
# their own timer, so a single build can't silently mix commits across the three.
KEY_SDK_SOURCE_RESOLVED_REF: Final = "resolved_ref"

# A raw commit SHA (e.g. a fork-pinned boards revision, or a resolved sdk_source: ref)
# can't be passed to `git clone --branch` or `west init --mr`.
COMMIT_SHA_RE: Final = re.compile(r"^[0-9a-f]{40}$")

CONF_BOARD_SOURCE = "board_source"
CONF_KCONFIG_OPTIONS = "kconfig_options"
CONF_OVERLAYS = "overlays"
# framework: type: silabs only -- overrides commander_setup.py's pinned default. Its own
# release cadence is independent of the silabs SDK version, same reasoning as
# west_version:/ninja_version: vs. the Zephyr version.
CONF_COMMANDER_VERSION = "commander_version"
CONF_SNIPPETS = "snippets"
KEY_SNIPPETS: Final = "snippets"
CONF_SINGLE_SLOT = "single_slot"
KEY_SINGLE_SLOT: Final = "single_slot"
CONF_SHIELDS = "shields"
CONF_SHIELD_SOURCE = "shield_source"
KEY_SHIELDS: Final = "shields"
KEY_SHIELD_ROOT: Final = "shield_root"
CONF_SNIPPET_SOURCE = "snippet_source"
KEY_SNIPPET_ROOT: Final = "snippet_root"
CONF_MODULES = "modules"

# zephyr: version: "recommended" -- explicit alias for the variant's default_version,
# same as omitting `version:` entirely.
VERSION_RECOMMENDED: Final = "recommended"

# Devicetree compatible of Zephyr's SEGGER RTT UART driver
SEGGER_RTT_UART = "segger,rtt-uart"
