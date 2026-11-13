import asyncio
from collections.abc import Callable
import logging
import os
from pathlib import Path
import re
import subprocess
import textwrap
from typing import Any, NamedTuple, NotRequired, TypedDict

import yaml

from esphome import git
import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.const import (
    CONF_ADVANCED,
    CONF_BOARD,
    CONF_FRAMEWORK,
    CONF_LOG_LEVEL,
    CONF_NAME,
    CONF_OTA,
    CONF_PASSWORD,
    CONF_PATH,
    CONF_PLATFORM,
    CONF_REF,
    CONF_REFRESH,
    CONF_SOURCE,
    CONF_TYPE,
    CONF_URL,
    CONF_USERNAME,
    CONF_VARIANT,
    CONF_VERSION,
    CONF_WATCHDOG_TIMEOUT,
    CONF_WIFI,
    KEY_CORE,
    KEY_FRAMEWORK_VERSION,
    KEY_TARGET_FRAMEWORK,
    KEY_TARGET_PLATFORM,
    TYPE_GIT,
    TYPE_LOCAL,
)
from esphome.core import CORE, CoroPriority, EsphomeError, coroutine_with_priority
from esphome.cpp_generator import MockObj
from esphome.helpers import copy_file_if_changed, rmtree, write_file_if_changed
from esphome.storage_json import StorageJSON
from esphome.types import ConfigType
from esphome.writer import clean_cmake_cache

from .board_revision import declared_revisions, parse_board_string, resolve_revision
from .const import (
    BOOTLOADER_MCUBOOT,
    CONF_BOARD_SOURCE,
    CONF_COMMANDER_VERSION,
    CONF_KCONFIG_OPTIONS,
    CONF_MAIN_STACK_SIZE,
    CONF_MODULES,
    CONF_OVERLAYS,
    CONF_RUNNER,
    CONF_SHIELD_SOURCE,
    CONF_SHIELDS,
    CONF_SINGLE_SLOT,
    CONF_SNIPPET_SOURCE,
    CONF_SNIPPETS,
    KEY_BOARD,
    KEY_BOARD_ROOT,
    KEY_BOOT_LAYOUT,
    KEY_BOOTLOADER,
    KEY_EXTRA_BUILD_FILES,
    KEY_FRAMEWORK_TYPE,
    KEY_KCONFIG,
    KEY_MODULE_OVERRIDES,
    KEY_MODULE_REQUESTS,
    KEY_OVERLAY,
    KEY_OVERLAY_BUILDER,
    KEY_PARTITION_REQUIREMENTS,
    KEY_PM_STATIC,
    KEY_PRJ_CONF,
    KEY_RUNNER,
    KEY_SDK_SOURCE_RESOLVED_REF,
    KEY_SHIELD_ROOT,
    KEY_SHIELDS,
    KEY_SINGLE_SLOT,
    KEY_SNIPPET_ROOT,
    KEY_SNIPPETS,
    KEY_SYSBUILD,
    KEY_SYSBUILD_CONF,
    KEY_USER,
    KEY_ZEPHYR,
    zephyr_ns,
)
from .dts_lookup import (  # noqa: F401
    board_has_cpu_freq_pstates,
    board_has_pm_states,
    get_can_controller_labels,
    get_pinctrl_group_property,
    get_pinctrl_states,
    normalize_dts_label,
    resolve_uart_node_label,
    resolve_zephyr_bus,
    validate_dts_label_exists,
    validate_uart_label_override,
)
from .gpio import zephyr_pin_to_code as _zephyr_pin_to_code  # noqa: F401
from .mcuboot import (  # noqa: F401
    apply_swap_method,
    validate_swap_method,
    zephyr_skip_bootloader_supported,
)
from .partitions import (  # noqa: F401
    BootLayout,
    render_sysbuild_cmake,
    zephyr_require_partition,
    zephyr_set_boot_layout,
)
from .pinctrl import (  # noqa: F401
    zephyr_setup_i2c_pinctrl,
    zephyr_setup_spi_pinctrl,
    zephyr_setup_uart_pinctrl,
)
from .rtt import (  # noqa: F401
    DEBUG_OWNER,
    RTT_PORTS,
    claim_rtt_channel,
    enable_rtt_node,
    rtt_channel,
    rtt_label_channel,
    validate_rtt_port,
)
from .usb_baud_rate import CONF_USB_BAUD_RATE
from .variants import (
    NCS,
    VARIANTS,
    ZEPHYR_MAINLINE,
    ZephyrModule,
    ZephyrModuleTemplate,
    ZephyrSDK,
    get_family_module,
    get_variant_module,
    resolve_sdk,
    set_core_data,
    variant_value,
)

CODEOWNERS = ["@tomaszduda23", "@rwrozelle"]
DOMAIN = "zephyr"


class HexValue:
    """Wrap an integer so it is written as 0x... in prj.conf (required for hex Kconfig types)."""

    def __init__(self, value: int) -> None:
        self.value = value

    def __eq__(self, other: object) -> bool:
        if isinstance(other, HexValue):
            return self.value == other.value
        return NotImplemented

    def __repr__(self) -> str:
        return f"HexValue(0x{self.value:X})"

    def __str__(self) -> str:
        return f"0x{self.value:X}"


PrjConfValueType = bool | str | int | HexValue


class Section:
    def __init__(self, name: str, address: int, size: int, region: str) -> None:
        self.name = name
        self.address = address
        self.size = size
        self.region = region
        self.end_address = self.address + self.size

    def __str__(self) -> str:
        return (
            f"{self.name}:\n"
            f"  address: 0x{self.address:X}\n"
            f"  end_address: 0x{self.end_address:X}\n"
            f"  region: {self.region}\n"
            f"  size: 0x{self.size:X}"
        )


class CdcAcmUser(NamedTuple):
    name: str
    # Only writes with uart_poll_out() and never sets the IRQ callback (the logger)
    write_only: bool
    # Can share the port with write-only users (protocols that skip unframed text)
    allow_write_only: bool


class ZephyrData(TypedDict):
    board: str
    bootloader: str
    prj_conf: dict[str, dict[str, tuple[PrjConfValueType, bool]]]
    overlay: dict[str, str]
    extra_build_files: dict[str, Path]
    pm_static: list[Section]
    kconfig: str
    sysbuild: bool
    overlay_builder: list[Callable[[], str]]
    # CDC ACM port number -> components that use it
    cdc_acm_users: dict[int, list[CdcAcmUser]]
    # Zephyr RTOS builds only; nrf52 builds do not set these
    # Resolved framework: board_source: dir, or None if unset
    board_root: NotRequired[Path | None]
    # Resolved framework: source:, or None if unset
    sdk_source: NotRequired[ConfigType | None]
    variant: NotRequired[str | None]
    # Variant's ZephyrVariant.family (e.g. "esp32"), or None if unfamilied
    family: NotRequired[str | None]
    # Resolved framework: type: (e.g. "zephyr", "ncs")
    framework_type: NotRequired[str]
    sysbuild_conf: NotRequired[dict[str, tuple[PrjConfValueType, bool]]]
    # (label, reason) partitions components need; see partitions.zephyr_require_partition()
    partition_requirements: NotRequired[list[tuple[str, str]]]
    # None = where the first image starts is not checked
    boot_layout: NotRequired[BootLayout | None]
    user: NotRequired[dict[str, list[str]]]
    fake_board_manifest: NotRequired[str | None]
    # Local path to zephyr/ tree root with boards/; set by dts_fetch
    dts_base_path: NotRequired[str | None]
    # "" = unchecked; None = not found; else = executable path
    cpp_path: NotRequired[str | None]
    # board -> abs path str, "" = not found
    board_dir_cache: NotRequired[dict[str, str]]
    # None = not yet computed
    dts_include_paths: NotRequired[list[str] | None]
    # (board, shields, snippets) -> EDT | _NOT_FOUND; cleared each run. Value stays
    # `object` since edtlib.EDT isn't imported here (dts_lookup.py's own concern).
    board_edt_cache: NotRequired[
        dict[tuple[str, tuple[str, ...], tuple[str, ...]], object]
    ]
    # board -> list[str] | _NOT_FOUND; cleared each run
    board_yaml_cache: NotRequired[dict[str, object]]
    # None = use zephyr/requirements.txt's pinned version
    west_version: NotRequired[str | None]
    # None = use zephyr/requirements.txt's pinned version
    ninja_version: NotRequired[str | None]
    # framework: snippets: -- one `-S <name>` per entry to `west build`
    snippets: NotRequired[list[str]]
    # framework: single_slot: -- see mcuboot.apply_single_slot()
    single_slot: NotRequired[bool]
    # framework: shields: -- one `-DSHIELD=` entry per item
    shields: NotRequired[list[str]]
    # Resolved framework: shield_source: dir, or board_root, or None
    shield_root: NotRequired[Path | None]
    # Resolved framework: snippet_source: dir, or board_root, or None
    snippet_root: NotRequired[Path | None]
    # capability -> template, requested via request_zephyr_module(). Not yet resolved
    # to a version -- a framework: modules: override (module_overrides) may still apply
    # before final resolution in run_compile(), so resolving eagerly here could raise
    # for a version gap the user's own override was going to fill.
    module_requests: NotRequired[dict[str, ZephyrModuleTemplate]]
    # module name -> a full user-authored ZephyrModule (framework: modules: with source:),
    # or a plain version string overriding a component-requested module's default.
    module_overrides: NotRequired[dict[str, ZephyrModule | str]]
    # (west_module, allow_regex, sentinel_name) entries added via zephyr_add_blobs() --
    # transport-conditional blob fetches, on top of the variant's own static `blobs`.
    blobs: NotRequired[list[tuple[str, str, str]]]
    # framework: advanced: runner: -- west flash runner override
    runner: NotRequired[str | None]
    # RTT channel -> its user ("logger", "uart 'id'"), claimed by rtt.enable_rtt_node()
    rtt_channels: NotRequired[dict[int, str]]


def zephyr_set_core_data(config: ConfigType) -> None:
    CORE.data[KEY_ZEPHYR] = ZephyrData(
        board=config[CONF_BOARD],
        bootloader=config[KEY_BOOTLOADER],
        prj_conf={},
        overlay={
            "": "",
        },  # set empty to make sure that overlay is cleared after config change
        overlay_builder=[],
        cdc_acm_users={},
        extra_build_files={},
        pm_static=[],
        kconfig="",
        # When OTA is disabled, the image is built without a bootloader even if the
        # config says `bootloader: mcuboot`, so the image can be smaller. This was
        # the default behaviour in SDK 2.6.1.
        sysbuild=False,
    )


def zephyr_data() -> ZephyrData:
    return CORE.data[KEY_ZEPHYR]


def zephyr_add_prj_conf(
    name: str,
    value: PrjConfValueType,
    required: bool = True,
    image: str = "",
) -> None:
    """Set an zephyr prj conf value."""
    if not name.startswith("CONFIG_"):
        name = "CONFIG_" + name
    if image not in zephyr_data()[KEY_PRJ_CONF]:
        zephyr_data()[KEY_PRJ_CONF][image] = {}
    prj_conf = zephyr_data()[KEY_PRJ_CONF][image]
    if name not in prj_conf:
        prj_conf[name] = (value, required)
        return
    old_value, old_required = prj_conf[name]
    if old_value != value and old_required:
        raise ValueError(
            f"{name} already set with value '{old_value}', cannot set again to '{value}'"
        )
    if required:
        prj_conf[name] = (value, required)


def zephyr_add_prj_conf_min(name: str, value: int, image: str = "") -> None:
    """Require a numeric zephyr prj conf value of at least ``value``.

    For sizes several components ask for, such as the main stack: the largest
    request wins instead of two different requests failing the build.
    """
    if not name.startswith("CONFIG_"):
        name = "CONFIG_" + name
    prj_conf = zephyr_data()[KEY_PRJ_CONF].setdefault(image, {})
    if name in prj_conf:
        value = max(value, prj_conf[name][0])
    prj_conf[name] = (value, True)


def zephyr_add_overlay(content: str, image: str = "") -> None:
    data = zephyr_data()
    if image not in data[KEY_OVERLAY]:
        data[KEY_OVERLAY][image] = ""
    elif CORE.using_toolchain_zephyr:
        # A chunk without a trailing newline would merge onto the next chunk's first line
        data[KEY_OVERLAY][image] += "\n"
    data[KEY_OVERLAY][image] += textwrap.dedent(content)


def zephyr_add_overlay_builder(func: Callable[[], str]) -> None:
    data = zephyr_data()
    if func not in data[KEY_OVERLAY_BUILDER]:
        data[KEY_OVERLAY_BUILDER].append(func)


def add_extra_build_file(filename: str, path: Path) -> bool:
    """Add an extra build file to the project."""
    extra_build_files = zephyr_data()[KEY_EXTRA_BUILD_FILES]
    if filename not in extra_build_files:
        extra_build_files[filename] = path
        return True
    return False


def add_extra_script(stage: str, filename: str, path: Path) -> None:
    """Add an extra script to the project."""
    key = f"{stage}:{filename}"
    if add_extra_build_file(filename, path):
        cg.add_platformio_option("extra_scripts", [key])


def zephyr_to_code(config: ConfigType) -> None:
    cg.add_build_flag("-DUSE_ZEPHYR")
    # Zephyr's base warnings include -Wdouble-promotion, which every float passed to a log call
    # trips; the promotion is how variadic arguments work, so the warning only adds noise
    cg.add_build_flag("-Wno-double-promotion")
    cg.add_define("USE_NATIVE_64BIT_TIME")
    # The settings subsystem finds stored preferences by key, so key migration is possible
    cg.add_define("USE_PREFERENCE_KEY_LOOKUP")
    cg.set_cpp_standard("gnu++20")
    # c++ support
    zephyr_add_prj_conf("FPU", True)
    zephyr_add_prj_conf("STD_CPP20", True)
    # random_bytes() uses sys_rand_get() which requires the entropy subsystem
    zephyr_add_prj_conf("ENTROPY_GENERATOR", True)

    # <err> os: ***** USAGE FAULT *****
    # <err> os:   Illegal load of EXC_RETURN into PC
    zephyr_add_prj_conf("MAIN_STACK_SIZE", 2048, required=False)

    CORE.add_job(_cdc_acm_to_code)


@coroutine_with_priority(CoroPriority.FINAL)
async def _cdc_acm_to_code() -> None:
    # Runs last so these required options override the optional defaults that
    # platforms (nrf52) set during their own to_code
    _cdc_acm_add_ports()


def zephyr_setup_preferences() -> None:
    cg.add(zephyr_ns.setup_preferences())
    zephyr_add_prj_conf("SETTINGS", True)
    zephyr_add_prj_conf("NVS", True)
    zephyr_add_prj_conf("FLASH_MAP", True)
    zephyr_add_prj_conf("FLASH", True)


def _format_prj_conf_val(value: PrjConfValueType) -> str:
    if isinstance(value, bool):
        return "y" if value else "n"
    if isinstance(value, HexValue):
        return hex(value.value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return f'"{value}"'
    raise ValueError


def zephyr_claim_cdc_acm(
    id: int,
    user: str,
    *,
    write_only: bool = False,
    allow_write_only: bool = False,
) -> None:
    """Reserve CDC ACM port `id` for `user`. Call from FINAL_VALIDATE_SCHEMA.

    zephyr_to_code() later adds the device tree node and Kconfig options for
    every claimed port, so components do not set up the port themselves.

    Components on the same port share one device, and the device has only one
    IRQ callback slot: the component set up last takes all received data from
    the other. So a port normally has one user. The exception is a `write_only`
    user (the logger), which never sets the IRQ callback; it may share the port
    with a user that sets `allow_write_only` because its protocol skips text
    that is not part of its own frames (mcumgr).
    """
    new = CdcAcmUser(user, write_only, allow_write_only)
    users = zephyr_data()["cdc_acm_users"].setdefault(id, [])
    for other in users:
        if other.name == user:
            return
        if (new.write_only and other.allow_write_only) or (
            other.write_only and new.allow_write_only
        ):
            continue
        raise cv.Invalid(
            f"USB CDC ACM port {id} (cdc_acm_uart{id}) is used by both "
            f"'{other.name}' and '{user}'. Each port can only be used by one component."
        )
    users.append(new)


def _cdc_acm_add_ports() -> None:
    """Set up every CDC ACM port claimed with zephyr_claim_cdc_acm()."""
    users = zephyr_data()["cdc_acm_users"]
    if not users:
        return
    framework_ver: cv.Version = CORE.data[KEY_CORE][KEY_FRAMEWORK_VERSION]
    if CORE.is_nrf52 and framework_ver >= cv.Version(3, 2, 0):
        zephyr_add_prj_conf("CONFIG_USB_DEVICE_STACK_NEXT", False)
    zephyr_add_prj_conf("USB_DEVICE_STACK", True)
    zephyr_add_prj_conf("USB_CDC_ACM", True)
    zephyr_add_prj_conf("SERIAL", True)
    # prevent device to go to susspend, without this communication stop working in python
    # there should be a way to solve it
    zephyr_add_prj_conf("USB_DEVICE_REMOTE_WAKEUP", False)
    # prevent logging when buffer is full
    zephyr_add_prj_conf("USB_CDC_ACM_LOG_LEVEL_WRN", True)
    nodes = "".join(
        f"""
                cdc_acm_uart{id}: cdc_acm_uart{id} {{
                    compatible = "zephyr,cdc-acm-uart";
                }};"""
        for id in sorted(users)
    )
    zephyr_add_overlay(
        f"""
            &zephyr_udc0 {{{nodes}
            }};
        """
    )


def zephyr_add_kconfig(kconfig: str) -> None:
    zephyr_data()[KEY_KCONFIG] += textwrap.dedent(kconfig) + "\n"


def zephyr_add_pm_static(sections: list[Section]) -> None:
    zephyr_data()[KEY_PM_STATIC].extend(sections)


def _write_file_if_changed_or_remove_when_empty(path: Path, content: str) -> bool:
    """Write content to path, or remove a stale file when content is empty.

    Returns True if the file changed on disk.
    """
    if content:
        return write_file_if_changed(path, content)
    if path.is_file():
        path.unlink()
        return True
    return False


def copy_files() -> None:
    for builder_func in zephyr_data()[KEY_OVERLAY_BUILDER]:
        overlay_contents = builder_func()
        zephyr_add_overlay(overlay_contents)

    changed = False

    for image, want_opts in zephyr_data()[KEY_PRJ_CONF].items():
        prj_conf = (
            "\n".join(
                f"{name}={_format_prj_conf_val(value[0])}"
                for name, value in sorted(want_opts.items())
            )
            + "\n"
        )

        if image:
            path = CORE.relative_build_path(f"zephyr/sysbuild/{image}.conf")
        else:
            path = CORE.relative_build_path("zephyr/prj.conf")

        changed |= write_file_if_changed(path, prj_conf)

    for image, content in zephyr_data()[KEY_OVERLAY].items():
        if image:
            path = CORE.relative_build_path(f"zephyr/sysbuild/{image}.overlay")
        else:
            path = CORE.relative_build_path("zephyr/app.overlay")
        changed |= write_file_if_changed(path, content)

    for filename, path in zephyr_data()[KEY_EXTRA_BUILD_FILES].items():
        changed |= copy_file_if_changed(
            path,
            CORE.relative_build_path(filename),
        )

    pm_static = "\n".join(str(item) for item in zephyr_data()[KEY_PM_STATIC])
    changed |= _write_file_if_changed_or_remove_when_empty(
        CORE.relative_build_path("zephyr/pm_static.yml"), pm_static
    )

    kconfig = zephyr_data()[KEY_KCONFIG]
    if kconfig:
        kconfig = (
            textwrap.dedent(
                """
                menu "Zephyr"
                source "Kconfig.zephyr"
                endmenu
                """
            )
            + "\n"
            + kconfig
        )
    changed |= _write_file_if_changed_or_remove_when_empty(
        CORE.relative_build_path("zephyr/Kconfig"), kconfig
    )

    sysbuild_conf = ""
    if zephyr_data()[KEY_SYSBUILD]:
        sysbuild_conf = "SB_CONFIG_BOOTLOADER_MCUBOOT=y\n"
    changed |= _write_file_if_changed_or_remove_when_empty(
        CORE.relative_build_path("zephyr/sysbuild.conf"), sysbuild_conf
    )

    if changed:
        # A configure-time input changed; drop the CMake cache so the build
        # can't reuse stale configure results (the native sdk-nrf toolchain
        # rebuilds pristine when the cache is missing).
        clean_cmake_cache()


_LOGGER = logging.getLogger(__name__)

# Mirrors esp32's LOG_LEVELS_IDF -- Zephyr's own native logging is a separate
# concern from ESPHome's `logger:` component.
LOG_LEVELS_ZEPHYR = [
    "NONE",
    "ERROR",
    "WARN",
    "INFO",
    "DEBUG",
    "VERBOSE",
]

# Zephyr's CONFIG_LOG_DEFAULT_LEVEL is a plain int (0=off .. 4=debug), unlike
# ESP-IDF's per-level bool choice -- DEBUG/VERBOSE both collapse to Zephyr's max.
_ZEPHYR_LOG_LEVEL_TO_INT = {
    "NONE": 0,
    "ERROR": 1,
    "WARN": 2,
    "INFO": 3,
    "DEBUG": 4,
    "VERBOSE": 4,
}


def _using_zephyr_toolchain() -> bool:
    """True once a Zephyr RTOS platform has set up the store; False for nrf52."""
    return CORE.using_toolchain_zephyr and KEY_ZEPHYR in CORE.data


def _require_zephyr_toolchain(name: str) -> None:
    # Reaching one of these from an nrf52 path is a bug
    if not _using_zephyr_toolchain():
        raise EsphomeError(
            f"{name}() is only available when using the zephyr framework "
            "on a supported platform"
        )


def zephyr_variant() -> str | None:
    """Return the active Zephyr variant name, or None if not yet set."""
    return zephyr_data().get("variant") if _using_zephyr_toolchain() else None


def zephyr_framework_type() -> str | None:
    """Return the active `framework: type:` (e.g. "zephyr", "ncs"), or None if
    not yet set."""
    if not _using_zephyr_toolchain():
        return None
    return zephyr_data().get(KEY_FRAMEWORK_TYPE)


def zephyr_variant_family() -> str | None:
    """Return the active variant's silicon family (e.g. "esp32"), or None if unset/unfamilied."""
    return zephyr_data().get("family") if _using_zephyr_toolchain() else None


def zephyr_usb_uart_type() -> str | None:
    """The active variant's kind of USB UART ("cdc_acm", "serial_jtag"), or None."""
    if (variant := zephyr_variant()) is None:
        return None
    return variant_value(variant, "usb_uart_type")


def zephyr_only_on_variant(*variant_names: str):
    """Return a cv validator that requires the Zephyr variant to be one of variant_names."""

    def validator(value):
        variant = zephyr_variant()
        if variant not in variant_names:
            listed = ", ".join(repr(v) for v in variant_names)
            raise cv.Invalid(
                f"This feature requires Zephyr variant {listed}, "
                f"but the current variant is {variant!r}"
            )
        return value

    return validator


def zephyr_set_prj_conf_override(
    name: str, value: PrjConfValueType, image: str = ""
) -> None:
    """Unconditionally set a zephyr prj conf value, replacing anything set by a component.

    Used only for user-supplied `kconfig_options:` (YAML), which must always win over a
    value set by component Python -- see the CoroPriority.FINAL job that calls this.
    """
    if not name.startswith("CONFIG_"):
        name = "CONFIG_" + name
    if image not in zephyr_data()[KEY_PRJ_CONF]:
        zephyr_data()[KEY_PRJ_CONF][image] = {}
    zephyr_data()[KEY_PRJ_CONF][image][name] = (value, True)


def zephyr_set_sysbuild_conf_override(name: str, value: PrjConfValueType) -> None:
    """Unconditionally set a sysbuild-level Kconfig value (SB_CONFIG_*), replacing
    anything set by a component's own zephyr_add_sysbuild_conf() call.

    Same always-wins precedent as zephyr_set_prj_conf_override(), but for
    zephyr/sysbuild.conf (SB_CONFIG_*, e.g. a vendor SDK's own `choice` default in a
    Kconfig.sysbuild file) rather than an image's own CONFIG_* prj.conf -- a user-supplied
    `kconfig_options:` name is routed here automatically when it already starts with
    SB_CONFIG_.
    """
    zephyr_data()[KEY_SYSBUILD_CONF][name] = (value, True)


def request_zephyr_module(capability: str) -> None:
    """Component-facing: record that `capability` (e.g. "zigbee") is needed as an
    additive west module. Not resolved to a concrete version here -- a user's
    `framework: modules:` override may still apply, so failing on a missing default
    version has to wait until resolve_zephyr_modules() (see there for why)."""
    _require_zephyr_toolchain("request_zephyr_module")
    _, sdk = resolve_sdk(VARIANTS[zephyr_variant()], zephyr_framework_type())
    template = sdk.modules.get(capability)
    if template is None:
        raise EsphomeError(
            f"The '{capability}' module isn't available for "
            f"framework: type: {zephyr_framework_type()}."
        )
    requests = zephyr_data()[KEY_MODULE_REQUESTS]
    if (existing := requests.get(capability)) is not None and existing != template:
        raise EsphomeError(
            f"The '{capability}' module was already requested with a different source"
        )
    requests[capability] = template


def zephyr_set_module_override(name: str, override: ZephyrModule | str) -> None:
    """Unconditionally record a `framework: modules:` override, replacing anything a
    component requested. `override` is a full ZephyrModule (source: given -- a
    brand-new or replaced module) or a plain version string (version-only tweak of a
    component-requested module). Always wins -- same precedent as
    zephyr_set_prj_conf_override for kconfig_options:.
    """
    zephyr_data()[KEY_MODULE_OVERRIDES][name] = override


def resolve_zephyr_modules() -> list[ZephyrModule]:
    """Combine module_requests with any module_overrides into the final set of
    modules to fetch. Called once, from run_compile() -- by then every component's
    to_code() and the framework: modules: override job have both already run, so an
    override is guaranteed visible before a request's version gets resolved."""
    root_version = CORE.data[KEY_CORE][KEY_FRAMEWORK_VERSION]
    overrides = dict(zephyr_data()[KEY_MODULE_OVERRIDES])
    resolved: dict[str, ZephyrModule] = {}
    seen_templates: dict[str, ZephyrModuleTemplate] = {}
    for capability, template in zephyr_data()[KEY_MODULE_REQUESTS].items():
        if (
            existing := seen_templates.get(template.name)
        ) is not None and existing != template:
            raise EsphomeError(
                f"Two different module requests both resolve to '{template.name}' "
                f"(most recently '{capability}') -- capabilities must map to "
                "distinct modules"
            )
        seen_templates[template.name] = template
        override = overrides.pop(template.name, None)
        if isinstance(override, ZephyrModule):
            resolved[template.name] = override
        else:
            resolved[template.name] = template.resolve(root_version, override)
    for name, override in overrides.items():
        if not isinstance(override, ZephyrModule):
            hint = ""
            if requested_names := sorted(t.name for t in seen_templates.values()):
                hint = (
                    " -- note this must be the module's own name, not a component's "
                    f"capability name (currently requested: {', '.join(requested_names)})"
                )
            raise EsphomeError(
                f"framework: modules: '{name}' has no source and wasn't requested by "
                f"any component{hint}"
            )
        resolved[name] = override
    return list(resolved.values())


def zephyr_configure_net_contexts() -> None:
    """Size NET_MAX_CONTEXTS from real socket demand. Mirrors esp32's
    _configure_lwip_max_sockets()/LWIP_MAX_SOCKETS."""
    _require_zephyr_toolchain("zephyr_configure_net_contexts")
    from esphome.components.socket import get_socket_counts  # noqa: PLC0415

    sc = get_socket_counts()
    total_sockets = max(sc.tcp + sc.udp + sc.tcp_listen, 10)
    zephyr_add_prj_conf("NET_MAX_CONTEXTS", total_sockets)
    zephyr_add_prj_conf("ZVFS_POLL_MAX", total_sockets)


def zephyr_add_sysbuild_conf(
    name: str,
    value: PrjConfValueType,
    required: bool = True,
) -> None:
    """Set a sysbuild-level Kconfig value (e.g. SB_CONFIG_BOOTLOADER_MCUBOOT).

    Distinct from zephyr_add_prj_conf(): sysbuild Kconfig (SB_CONFIG_*) controls
    which child images sysbuild builds at all (e.g. whether an MCUboot image
    exists) and lives in its own zephyr/sysbuild.conf file, separate from any
    image's own CONFIG_* prj.conf.
    """
    _require_zephyr_toolchain("zephyr_add_sysbuild_conf")
    if not name.startswith("SB_CONFIG_"):
        name = "SB_CONFIG_" + name
    sysbuild_conf = zephyr_data()[KEY_SYSBUILD_CONF]
    if name not in sysbuild_conf:
        sysbuild_conf[name] = (value, required)
        return
    old_value, old_required = sysbuild_conf[name]
    if old_value != value and old_required:
        raise ValueError(
            f"{name} already set with value '{old_value}', cannot set again to '{value}'"
        )
    if required:
        sysbuild_conf[name] = (value, required)


def zephyr_add_blobs(module: str, allow_regex: str, sentinel_name: str) -> None:
    """Request a `west blobs fetch`, gated on a transport actually being configured
    (unlike a variant's static `blobs`, which always runs)."""
    _require_zephyr_toolchain("zephyr_add_blobs")
    spec = (module, allow_regex, sentinel_name)
    if spec not in zephyr_data()["blobs"]:
        zephyr_data()["blobs"].append(spec)


def zephyr_family_uart_drivers() -> frozenset[str]:
    """Software UART drivers (devicetree compatibles) the active family supports.
    Empty without a family (platform: nrf52)."""
    if (family := zephyr_variant_family()) is None:
        return frozenset()
    return get_family_module(family).UART_DRIVERS


def zephyr_additional_framework_projects(sdk: ZephyrSDK) -> frozenset[str]:
    """The framework projects this build needs beyond the framework's required ones:
    the family's vendor HALs on mainline, the projects components ask for on NCS."""
    if sdk is NCS:
        # The same list as nrf52's: components add to it with include_west_project()
        from esphome.components.nrf52.framework import wanted_west_projects  # noqa: PLC0415

        projects = wanted_west_projects()
        # Sysbuild builds the MCUboot image from the mcuboot project
        sysbuild_conf = zephyr_data()[KEY_SYSBUILD_CONF]
        if sysbuild_conf.get("SB_CONFIG_BOOTLOADER_MCUBOOT", (False,))[0]:
            projects.add("mcuboot")
        return frozenset(projects)
    if sdk is ZEPHYR_MAINLINE and (family := zephyr_variant_family()) is not None:
        return get_family_module(family).ADDITIONAL_FRAMEWORK_PROJECTS
    return frozenset()


def _base_to_code(config: ConfigType) -> None:
    cg.add_build_flag("-DUSE_ZEPHYR")
    # Zephyr's base warnings include -Wdouble-promotion, which every float passed to a log call
    # trips; the promotion is how variadic arguments work, so the warning only adds noise
    cg.add_build_flag("-Wno-double-promotion")
    cg.add_define("USE_NATIVE_64BIT_TIME")
    # The settings subsystem finds stored preferences by key, so key migration is possible
    cg.add_define("USE_PREFERENCE_KEY_LOOKUP")
    cg.set_cpp_standard("gnu++20")
    # USE_ZEPHYR_FRAMEWORK_ZEPHYR not defined, no need in code so far.
    # USE_ZEPHYR_FRAMEWORK_ZIGBEE is set by zigbee_zephyr.py itself, alongside its
    # request_zephyr_module("zigbee") call -- this runs before zigbee:'s own
    # to_code() in the normal dependency order, so the module request wouldn't be
    # visible yet if it were decided here instead.
    if zephyr_framework_type() == "ncs":
        cg.add_define("USE_ZEPHYR_FRAMEWORK_NCS")
    cg.add_define("ESPHOME_PLATFORM_NAME", CORE.target_platform.upper())
    # Without this, printf-style %f prints "*float*" on chips without an FPU (FPU enables it by default).
    zephyr_add_prj_conf("CBPRINTF_FP_SUPPORT", True)

    if zephyr_data()[KEY_SINGLE_SLOT]:
        from . import mcuboot  # noqa: PLC0415

        mcuboot.apply_single_slot()

    zephyr_add_prj_conf("STD_CPP20", True)
    # <err> os: ***** USAGE FAULT *****
    # <err> os:   Illegal load of EXC_RETURN into PC
    _main_stack_to_code(config)
    zephyr_add_prj_conf("SYSTEM_WORKQUEUE_STACK_SIZE", 2048, required=False)
    if CONF_WIFI in CORE.config:
        # Doubles Zephyr's default stack size for WiFi's dynamically-spawned worker
        # threads. Gated on wifi: since DYNAMIC_THREAD is only selected when WiFi is enabled.
        zephyr_add_prj_conf("DYNAMIC_THREAD_STACK_SIZE", 2048, required=False)

    _watchdog_to_code(config)

    # As nrf52: off unless a component needs them, since board defconfigs turn
    # them on (UART pins, flash, idle current). Weak, so logger/uart/kconfig_options win.
    for option in (
        "USB_DEVICE_STACK",
        "SERIAL",
        "CONSOLE",
        "UART_CONSOLE",
        "PRINTK",
        "BOOT_BANNER",
    ):
        zephyr_add_prj_conf(option, False, False)

    log_level = config[CONF_FRAMEWORK][CONF_LOG_LEVEL]
    if log_level != "NONE":
        zephyr_add_prj_conf("LOG", True)
        zephyr_add_prj_conf("LOG_DEFAULT_LEVEL", _ZEPHYR_LOG_LEVEL_TO_INT[log_level])
        # Fits a whole fault dump until LOG_PANIC(); 1024 loses its first lines
        zephyr_add_prj_conf("LOG_BUFFER_SIZE", 2048, required=False)
        # Normally pulled in transitively by LOG_BACKEND_UART, which the logger component
        # disables in favor of its own backend, so it must be set explicitly here.
        zephyr_add_prj_conf("LOG_OUTPUT", True)

        # Both default to y already, set explicitly so a crash dumps a backtrace instead
        # of looking like a silent reboot.
        zephyr_add_prj_conf("EXCEPTION_DEBUG", True)

    CORE.add_job(_kconfig_options_to_code, config)
    CORE.add_job(_modules_to_code, config)
    CORE.add_job(_overlays_to_code, config)
    CORE.add_job(_zephyr_toolchain_cdc_acm_to_code)


def _main_stack_to_code(config: ConfigType) -> None:
    family = zephyr_variant_family()
    default_stack = (
        getattr(get_family_module(family), "MAIN_STACK_SIZE", 4096) if family else 4096
    )
    zephyr_add_prj_conf(
        "MAIN_STACK_SIZE",
        config[CONF_FRAMEWORK][CONF_ADVANCED].get(CONF_MAIN_STACK_SIZE, default_stack),
        required=False,
    )


def _watchdog_to_code(config: ConfigType) -> None:
    timeout_ms = int(config[CONF_WATCHDOG_TIMEOUT].total_milliseconds)
    # 0 disables the watchdog: forced off so a board defconfig can't build a driver that
    # nothing arms or feeds.
    if timeout_ms == 0:
        zephyr_add_prj_conf("WATCHDOG", False)
    else:
        zephyr_add_prj_conf("WATCHDOG", True)
        zephyr_add_prj_conf("WDT_DISABLE_AT_BOOT", False)
        cg.add_define("USE_WATCHDOG")
        cg.add_define("USE_ZEPHYR_WATCHDOG_TIMEOUT_MS", timeout_ms)
        # hal.cpp needs DT_ALIAS(watchdog0); some boards never define it
        # themselves -- found generically, not per-family, so new vendors
        # with the same gap are covered without new code here.
        from .dts_lookup import get_watchdog_node_label

        wdt_label, wdt_already_working = get_watchdog_node_label(
            zephyr_data()[KEY_BOARD]
        )
        if wdt_label is not None and not wdt_already_working:
            zephyr_add_overlay(
                f'&{wdt_label} {{ status = "okay"; }};\n'
                f"/ {{ aliases {{ watchdog0 = &{wdt_label}; }}; }};"
            )
        # Identifies the stalled thread on the console from the watchdog callback (see
        # hal.cpp) -- without THREAD_NAME, k_thread_name_get() just returns NULL.
        zephyr_add_prj_conf("THREAD_NAME", True)


@coroutine_with_priority(CoroPriority.FINAL)
async def _modules_to_code(config: ConfigType) -> None:
    # Runs at the lowest priority so every component has already requested its own
    # modules -- a user-supplied framework: modules: entry must always win, and a
    # version-only override needs every request already recorded to have something to
    # attach to (final resolution happens later still, in resolve_zephyr_modules()).
    _apply_module_overrides(config[CONF_FRAMEWORK][CONF_MODULES])


def _apply_module_overrides(module_confs: list[ConfigType]) -> None:
    for module_conf in module_confs:
        name = module_conf[CONF_NAME]
        source = module_conf.get(CONF_SOURCE)
        if source is None:
            zephyr_set_module_override(name, module_conf[CONF_VERSION])
        elif source[CONF_TYPE] == TYPE_LOCAL:
            # cv.LOCAL_SCHEMA's path: already went through cv.directory -- an
            # already-resolved absolute Path, not a raw string to resolve again.
            zephyr_set_module_override(
                name, ZephyrModule(name=name, local_path=source[CONF_PATH])
            )
        else:
            zephyr_set_module_override(
                name,
                ZephyrModule(
                    name=name,
                    manifest_url=source[CONF_URL],
                    revision=source[CONF_REF],
                    refresh=source[CONF_REFRESH].total_seconds,
                ),
            )


@coroutine_with_priority(CoroPriority.FINAL)
async def _kconfig_options_to_code(config: ConfigType) -> None:
    # Runs at the lowest priority so every component has already set its own prj.conf
    # defaults -- user-supplied kconfig_options: must always win, so this overrides
    # unconditionally instead of going through zephyr_add_prj_conf()'s conflict check.
    for name, value in config[CONF_FRAMEWORK][CONF_KCONFIG_OPTIONS].items():
        if isinstance(value, dict):
            for image_name, image_value in value.items():
                zephyr_set_prj_conf_override(image_name, image_value, image=name)
        elif name.startswith("SB_CONFIG_"):
            zephyr_set_sysbuild_conf_override(name, value)
        else:
            zephyr_set_prj_conf_override(name, value)


@coroutine_with_priority(CoroPriority.FINAL)
async def _overlays_to_code(config: ConfigType) -> None:
    # Runs at the lowest priority so user-supplied overlay content is appended after
    # every component's own generated overlays (e.g. a partition table redefinition
    # needs to come after anything else touching &flash0).
    overlays = config[CONF_FRAMEWORK][CONF_OVERLAYS]
    if app_overlay := overlays.get("app"):
        zephyr_add_overlay(app_overlay)
    if mcuboot_overlay := overlays.get("mcuboot"):
        zephyr_add_overlay(mcuboot_overlay, image="mcuboot")


@coroutine_with_priority(CoroPriority.FINAL)
async def _zephyr_toolchain_cdc_acm_to_code() -> None:
    # Runs last so these required options override the optional defaults set by
    # _base_to_code() and the variants
    _zephyr_toolchain_cdc_acm_add_ports()


def zephyr_cdc_acm_label(id: int) -> str:
    """Return the devicetree label of CDC ACM port `id`, for
    `zephyr,console`/`zephyr,shell-uart` and DT_NODELABEL().

    Port 0 is the board's own `zephyr,cdc-acm-uart` node when it declares one -- e.g.
    boards/common/usb/cdc_acm_serial.dtsi, labeled `board_cdc_acm_uart` -- instead of a
    second, separate USB interface. Two CDC-ACM interfaces on the same device is needless
    USB complexity, and means "which port do I talk to" changes depending on whether
    MCUboot (which only ever sees the board's own node) or the app is currently running.
    """
    if id == 0 and _using_zephyr_toolchain():
        from .dts_lookup import get_existing_cdc_acm_uart_label  # noqa: PLC0415

        if (
            label := get_existing_cdc_acm_uart_label(zephyr_data()[KEY_BOARD])
        ) is not None:
            return label
    return f"cdc_acm_uart{id}"


def _zephyr_toolchain_cdc_acm_add_ports() -> None:
    """Set up every CDC ACM port claimed with zephyr_claim_cdc_acm(): the device_next USB
    stack, and a node merged into the devicetree only for ports the board does not
    provide itself."""
    users = zephyr_data()["cdc_acm_users"]
    if not users:
        return
    zephyr_add_prj_conf("CONFIG_USB_DEVICE_STACK_NEXT", True)
    # Zephyr's boot-time setup registers only port 0; usb_device.cpp registers every port
    multi_port = max(users) > 0
    zephyr_add_prj_conf("CONFIG_CDC_ACM_SERIAL_INITIALIZE_AT_BOOT", not multi_port)
    if multi_port:
        cg.add_define("USE_ZEPHYR_USB_DEVICE")
    zephyr_add_prj_conf("SERIAL", True)
    nodes = "".join(
        f"""
                {label}: {label} {{
                    compatible = "zephyr,cdc-acm-uart";
                }};"""
        for id in sorted(users)
        if (label := zephyr_cdc_acm_label(id)) == f"cdc_acm_uart{id}"
    )
    if nodes:
        zephyr_add_overlay(
            f"""
            &zephyr_udc0 {{{nodes}
            }};
        """
        )


async def zephyr_add_usb_baud_rate(config: ConfigType) -> MockObj:
    """Ensure the USB serial port exists and that baud rates the host sets on it are
    reported (see usb_baud_rate.h). Returns the UsbBaudRate component for consumers to
    subscribe to. config must hold the CONF_USB_BAUD_RATE id declared at validation (the
    Pico family keeps it in advanced:), because components are counted then."""
    cg.add_define("USE_ZEPHYR_USB_BAUD_RATE")
    # Reads the line settings only: no IRQ callback and no output, so it shares port 0 with anything.
    # Claimed here rather than at validation since its users are variant code, before the FINAL port setup
    zephyr_claim_cdc_acm(0, "usb_baud_rate", write_only=True, allow_write_only=True)
    # Needed to read the rate the host set.
    zephyr_add_prj_conf("UART_LINE_CTRL", True)
    port = MockObj(f"DEVICE_DT_GET(DT_NODELABEL({zephyr_cdc_acm_label(0)}))")
    var = cg.new_Pvariable(config[CONF_USB_BAUD_RATE], port)
    await cg.register_component(var, {})
    return var


def zephyr_add_user(key, value):
    _require_zephyr_toolchain("zephyr_add_user")
    user = zephyr_data()[KEY_USER]
    if key not in user:
        user[key] = []
    user[key] += [value]
    # Idempotent: zephyr_add_overlay_builder() only appends a given func once, so every
    # caller registering the same _render_user_overlay reference collapses to one deferred
    # render covering every key -- see zephyr_add_overlay_builder's copy_files() consumer.
    zephyr_add_overlay_builder(_render_user_overlay)


def _render_user_overlay() -> str:
    user = zephyr_data()[KEY_USER]
    if not user:
        return ""
    entries = " ".join(f"{key} = {', '.join(value)};" for key, value in user.items())
    return f"""
        / {{
            zephyr,user {{
                {entries}
            }};
        }};
    """


def _remove_stale_image_files(written: set[Path]) -> bool:
    """Remove app.overlay and sysbuild/<image>.conf/.overlay files this config did not write.

    Zephyr picks these files up by name, so one left by an earlier config in the same
    build directory still applies. Returns True if a file was removed.
    """
    candidates = [CORE.relative_build_path("zephyr/app.overlay")]
    sysbuild_dir = CORE.relative_build_path("zephyr/sysbuild")
    if sysbuild_dir.is_dir():
        candidates += [
            p for p in sysbuild_dir.iterdir() if p.suffix in (".conf", ".overlay")
        ]
    removed = False
    for path in candidates:
        if path not in written and path.is_file():
            path.unlink()
            removed = True
    return removed


def zephyr_toolchain_copy_files() -> None:
    changed = False
    written: set[Path] = set()

    for builder_func in zephyr_data()[KEY_OVERLAY_BUILDER]:
        overlay_contents = builder_func()
        zephyr_add_overlay(overlay_contents)

    if manifest := zephyr_data()["fake_board_manifest"]:
        changed |= write_file_if_changed(
            CORE.relative_build_path(f"boards/{zephyr_data()[KEY_BOARD]}.json"),
            manifest,
        )

    for image, want_opts in zephyr_data()[KEY_PRJ_CONF].items():
        prj_conf = (
            "\n".join(
                f"{name}={_format_prj_conf_val(value[0])}"
                for name, value in sorted(want_opts.items())
            )
            + "\n"
        )

        if image:
            path = CORE.relative_build_path(f"zephyr/sysbuild/{image}.conf")
        else:
            path = CORE.relative_build_path("zephyr/prj.conf")

        written.add(path)
        changed |= write_file_if_changed(path, prj_conf)

    for image, content in zephyr_data()[KEY_OVERLAY].items():
        if image:
            path = CORE.relative_build_path(f"zephyr/sysbuild/{image}.overlay")
            if (
                image == "mcuboot"
                and zephyr_data()[KEY_BOOTLOADER] == BOOTLOADER_MCUBOOT
            ):
                # Writing any overlay file for the mcuboot sysbuild image replaces its
                # normal auto-detected overlay chain (Zephyr sysbuild convention),
                # which silently drops MCUboot's own
                # zephyr,code-partition = &boot_partition; setting -- restate it here,
                # once, for every caller that writes to this image's overlay, rather
                # than relying on each one to remember to.
                content += (
                    "\n/ { chosen { zephyr,code-partition = &boot_partition; }; };\n"
                )
        else:
            path = CORE.relative_build_path("zephyr/app.overlay")
        written.add(path)
        changed |= write_file_if_changed(path, content)

    changed |= _remove_stale_image_files(written)

    for filename, path in zephyr_data()[KEY_EXTRA_BUILD_FILES].items():
        changed |= copy_file_if_changed(
            path,
            CORE.relative_build_path(filename),
        )

    pm_static = "\n".join(str(item) for item in zephyr_data()[KEY_PM_STATIC])
    changed |= _write_file_if_changed_or_remove_when_empty(
        CORE.relative_build_path("zephyr/pm_static.yml"), pm_static
    )

    kconfig = zephyr_data()[KEY_KCONFIG]
    if kconfig:
        kconfig = (
            textwrap.dedent(
                """
                menu "Zephyr"
                source "Kconfig.zephyr"
                endmenu
                """
            )
            + "\n"
            + kconfig
        )
    changed |= _write_file_if_changed_or_remove_when_empty(
        CORE.relative_build_path("zephyr/Kconfig"), kconfig
    )

    sysbuild_conf_opts = zephyr_data()[KEY_SYSBUILD_CONF]
    sysbuild_conf_lines = [
        f"{name}={_format_prj_conf_val(value[0])}"
        for name, value in sorted(sysbuild_conf_opts.items())
    ]
    sysbuild_conf = "\n".join(sysbuild_conf_lines) + "\n" if sysbuild_conf_lines else ""
    changed |= _write_file_if_changed_or_remove_when_empty(
        CORE.relative_build_path("zephyr/sysbuild.conf"), sysbuild_conf
    )

    # Flash layout checks. A change drops the CMake cache, which a new file needs
    # anyway to replace Zephyr's sysbuild template.
    sysbuild_cmake = render_sysbuild_cmake(
        zephyr_data()[KEY_BOOT_LAYOUT],
        zephyr_data()[KEY_PARTITION_REQUIREMENTS],
    )
    changed |= _write_file_if_changed_or_remove_when_empty(
        CORE.relative_build_path("zephyr/sysbuild/CMakeLists.txt"), sysbuild_cmake
    )

    if changed:
        # A configure-time input changed; drop the CMake cache so the build can't reuse
        # stale configure results -- see clean_cmake_cache() in esphome/writer.py.
        clean_cmake_cache()


# Watchdog range mirrors esp32's own CONF_WATCHDOG_TIMEOUT validator (esp32/__init__.py) --
# confirmed working on real ESP32-H2 hardware under Zephyr. 0 is a special case, not part of
# that shared range: it disables the watchdog entirely (no CONFIG_WATCHDOG, no wdt_setup()
# call at all -- see the zephyr_variant() watchdog codegen block below), rather than
# requesting an unusably short real timeout.
def _validate_watchdog_timeout(value):
    # Bare 0 has no unit, so it wouldn't parse as a normal TimePeriod otherwise.
    # Only the 60s ceiling is checked here; the 5s floor is variant-aware (some
    # variants can't reach it) and enforced later once VARIANTS[variant] is known.
    if isinstance(value, (int, float)) and value == 0:
        return cv.TimePeriod(seconds=0)
    period = cv.positive_time_period_milliseconds(value)
    if period == cv.TimePeriod(seconds=0):
        return period
    return cv.Range(max=cv.TimePeriod(seconds=60))(period)


_WATCHDOG_TIMEOUT_VALIDATOR = _validate_watchdog_timeout

# Like cv.SOURCE_SCHEMA's git shape, but `url` is optional: `framework: source: {ref: ...}`
# alone is enough to pin an alpha/pre-release branch of the selected framework's own
# official manifest (see resolve_framework_version(), which fills in the default).
_FRAMEWORK_SOURCE_GIT_SCHEMA = cv.Schema(
    {
        cv.Optional(CONF_URL): cv.url,
        cv.Optional(CONF_REF): cv.git_ref,
        cv.Optional(CONF_USERNAME): cv.string,
        cv.Optional(CONF_PASSWORD): cv.string,
        cv.Optional(CONF_PATH): cv.string,
    }
)
_FRAMEWORK_SOURCE_SCHEMA = cv.Any(
    cv.validate_source_shorthand,
    cv.typed_schema(
        {
            TYPE_GIT: _FRAMEWORK_SOURCE_GIT_SCHEMA,
            TYPE_LOCAL: cv.LOCAL_SCHEMA,
        }
    ),
)

# Own refresh: knob (see _FRAMEWORK_SCHEMA's comment) -- board_source: is a different git
# source than framework: source:, so it doesn't share a cadence with it. Unlike
# framework: source:, board_source: has no "official" default url to fall back to, so it
# keeps requiring an explicit url like the generic cv.SOURCE_SCHEMA it's otherwise
# modeled on.
_BOARD_SOURCE_GIT_SCHEMA = cv.Schema(
    {
        cv.Required(CONF_URL): cv.url,
        cv.Optional(CONF_REF): cv.git_ref,
        cv.Optional(CONF_USERNAME): cv.string,
        cv.Optional(CONF_PASSWORD): cv.string,
        cv.Optional(CONF_PATH): cv.string,
        cv.Optional(CONF_REFRESH, default="1d"): cv.All(cv.string, cv.source_refresh),
    }
)
_BOARD_SOURCE_SCHEMA = cv.Any(
    cv.validate_source_shorthand,
    cv.typed_schema(
        {
            TYPE_GIT: _BOARD_SOURCE_GIT_SCHEMA,
            TYPE_LOCAL: cv.LOCAL_SCHEMA,
        }
    ),
)
# cv.validate_source_shorthand (e.g. board_source: github://user/repo) resolves through
# the generic cv.SOURCE_SCHEMA, not the schema above, so a shorthand board_source never
# gets this refresh: default baked in -- _resolve_board_source() falls back to this
# constant for that case instead of relying on the schema to inject it.
_DEFAULT_REFRESH = cv.All(cv.string, cv.source_refresh)("1d")

# shield_source: is only needed when a shield lives outside board_source:'s repo (the
# uncommon case) -- same git/local shape as board_source:, so reuse its git schema
# rather than duplicating it.
_SHIELD_SOURCE_SCHEMA = cv.Any(
    cv.validate_source_shorthand,
    cv.typed_schema(
        {
            TYPE_GIT: _BOARD_SOURCE_GIT_SCHEMA,
            TYPE_LOCAL: cv.LOCAL_SCHEMA,
        }
    ),
)

# snippet_source: mirrors shield_source: -- only needed when a snippet lives outside
# board_source:'s repo (the uncommon case); same git/local shape.
_SNIPPET_SOURCE_SCHEMA = cv.Any(
    cv.validate_source_shorthand,
    cv.typed_schema(
        {
            TYPE_GIT: _BOARD_SOURCE_GIT_SCHEMA,
            TYPE_LOCAL: cv.LOCAL_SCHEMA,
        }
    ),
)

# A user-authored additive west module -- no username:/password: (unlike
# board_source:/shield_source:/snippet_source:, these are fetched by
# framework.install_git_modules(), not ESPHome's own git.clone_or_update cache).
_MODULE_GIT_SCHEMA = cv.Schema(
    {
        cv.Required(CONF_URL): cv.url,
        cv.Optional(CONF_REF, default="main"): cv.git_ref,
        cv.Optional(CONF_REFRESH, default="1d"): cv.All(cv.string, cv.source_refresh),
    }
)
_MODULE_SOURCE_SCHEMA = cv.typed_schema(
    {
        TYPE_GIT: _MODULE_GIT_SCHEMA,
        TYPE_LOCAL: cv.LOCAL_SCHEMA,
    }
)
# source: defines a brand-new (or replaced) module; version: alone is a version-only
# tweak of a module a component already requested -- see resolve_zephyr_modules(). At
# least one of the two is required, or the entry does nothing.
_MODULE_SCHEMA = cv.All(
    cv.Schema(
        {
            cv.Required(CONF_NAME): cv.string_strict,
            cv.Optional(CONF_SOURCE): _MODULE_SOURCE_SCHEMA,
            cv.Optional(CONF_VERSION): cv.string_strict,
        }
    ),
    cv.has_at_least_one_key(CONF_SOURCE, CONF_VERSION),
)

# `type:` isn't validated against the variant's own sdk names here -- which variant is
# selected isn't known until the platform schema's CONF_VARIANT key is read, and this
# schema is applied independently of key order. resolve_framework_version() (called
# per-variant, after the platform schema) does that check, along with version:/source:
# mutual exclusion and everything else that needs the variant to already be known.
_FRAMEWORK_SCHEMA = cv.Schema(
    {
        cv.Optional(CONF_TYPE): cv.string_strict,
        # Also accepts the literal "recommended", an explicit alias for the selected
        # sdk's default_version (same effect as omitting this key).
        cv.Optional(CONF_VERSION): cv.string_strict,
        # Overrides the selected sdk's own official manifest with a fork/branch/ref or a
        # `local:` path to an already-initialized west workspace. `url` may be omitted to
        # target a branch (e.g. an alpha/pre-release) of the sdk's own official manifest.
        cv.Optional(CONF_SOURCE): _FRAMEWORK_SOURCE_SCHEMA,
        # How often a git source: is re-checked for updates (via `git pull`) -- own knob,
        # independent of board_source:'s (a different git source with its own cadence).
        cv.Optional(CONF_REFRESH, default="1d"): cv.All(cv.string, cv.source_refresh),
        cv.Optional(CONF_BOARD_SOURCE): _BOARD_SOURCE_SCHEMA,
        # Zephyr shields (physical add-on boards, e.g. an nRF7002 Wi-Fi/Zigbee
        # companion board) -- one `-DSHIELD=` entry per item, natively space/semicolon
        # stackable, hence a list rather than a single value.
        cv.Optional(CONF_SHIELDS, default=[]): cv.ensure_list(cv.string_strict),
        # Only needed when a shield doesn't live under board_source:'s (or the SDK's
        # own) root -- see _resolve_shield_source().
        cv.Optional(CONF_SHIELD_SOURCE): _SHIELD_SOURCE_SCHEMA,
        # Only needed when a snippet doesn't live under board_source:'s (or the
        # SDK's own) root -- see _resolve_snippet_source().
        cv.Optional(CONF_SNIPPET_SOURCE): _SNIPPET_SOURCE_SCHEMA,
        # Zephyr's own native logging verbosity (CONFIG_LOG_DEFAULT_LEVEL) -- separate
        # from ESPHome's own `logger: level:`, mirroring esp32's `framework: log_level:`.
        cv.Optional(CONF_LOG_LEVEL, default="ERROR"): cv.one_of(
            *LOG_LEVELS_ZEPHYR, upper=True
        ),
        # Loose passthrough here -- shape differs per variant, strictly validated by
        # the variant's own config_schema.
        cv.Optional(CONF_ADVANCED, default={}): dict,
        # Raw Kconfig passthrough -- Zephyr's equivalent of esp32's sdkconfig_options. A
        # dict value (e.g. `mcuboot:`) targets that sysbuild child image's own prj.conf.
        # A name already prefixed SB_CONFIG_ instead targets zephyr/sysbuild.conf itself
        # (the sysbuild superproject's own Kconfig tree, e.g. a vendor SDK's `choice`
        # default in its own Kconfig.sysbuild) -- see zephyr_set_sysbuild_conf_override().
        cv.Optional(CONF_KCONFIG_OPTIONS, default={}): {
            cv.string_strict: cv.Any(
                cv.boolean,
                cv.int_,
                cv.string_strict,
                {cv.string_strict: cv.Any(cv.boolean, cv.int_, cv.string_strict)},
            )
        },
        # Raw passthrough to `west build -S <name>`, one per entry -- Zephyr's snippet
        # mechanism for devicetree/Kconfig fragments selected by name (e.g. Espressif's
        # own espressif-flash-4M/flash-8M/... board-variant snippets), a different layer
        # from kconfig_options: above. Lets a stock upstream board cover a differently
        # sized flash/PSRAM SKU without forking a whole custom board.
        cv.Optional(CONF_SNIPPETS, default=[]): cv.ensure_list(cv.string_strict),
        # No secondary slot -- serial/wired flashing only, no live OTA path.
        cv.Optional(CONF_SINGLE_SLOT, default=False): cv.boolean,
        # Raw devicetree overlay passthrough -- lets a stock upstream board's DT be
        # patched (e.g. a partition table redefined for MCUboot) without forking a
        # custom board. "mcuboot" targets the sysbuild bootloader child image.
        cv.Optional(CONF_OVERLAYS, default={}): cv.Schema(
            {
                cv.Optional("app"): cv.string,
                cv.Optional("mcuboot"): cv.string,
            }
        ),
        # Additive west modules -- either a user's own out-of-tree module (source:),
        # or a version-only override of a module a component already requested (e.g.
        # zigbee:'s ncs-zigbee) -- see resolve_zephyr_modules().
        cv.Optional(CONF_MODULES, default=[]): cv.ensure_list(_MODULE_SCHEMA),
    }
)


def zephyr_framework_schema() -> cv.Schema:
    """The framework: schema of a Zephyr RTOS build, for esp32/rp2 schema extraction."""
    return _FRAMEWORK_SCHEMA


def zephyr_toolchain_schema(families: frozenset[str]) -> cv.Schema:
    """The <platform>: schema, limited to the variants of `families`."""
    return cv.Schema(
        {
            cv.Required(CONF_VARIANT): cv.one_of(
                *(name for name, info in VARIANTS.items() if info.family in families),
                upper=True,
            ),
            cv.Optional(CONF_BOARD): cv.string_strict,
            cv.Optional(CONF_WATCHDOG_TIMEOUT): _WATCHDOG_TIMEOUT_VALIDATOR,
            # Which SDK to build against (type:), its version, where to fetch it from
            # (source:), and the Zephyr-only options -- see resolve_framework_version()
            # in variants/__init__.py for how type:/version:/source: are resolved.
            cv.Optional(CONF_FRAMEWORK, default={}): _FRAMEWORK_SCHEMA,
        }
    )


def _resolve_board_source(config: ConfigType, board: str) -> Path:
    """Resolve framework: board_source: into a local BOARD_ROOT directory."""
    conf = config[CONF_BOARD_SOURCE]
    if conf[CONF_TYPE] == TYPE_GIT:
        with cv.prepend_path([CONF_FRAMEWORK, CONF_BOARD_SOURCE]):
            root, _ = git.clone_or_update(
                url=conf[CONF_URL],
                ref=conf.get(CONF_REF),
                refresh=conf.get(CONF_REFRESH, _DEFAULT_REFRESH),
                domain=KEY_ZEPHYR,
                username=conf.get(CONF_USERNAME),
                password=conf.get(CONF_PASSWORD),
            )
            if path := conf.get(CONF_PATH):
                root = root / path
    elif conf[CONF_TYPE] == TYPE_LOCAL:
        root = Path(CORE.relative_config_path(conf[CONF_PATH]))
    else:
        raise NotImplementedError

    # board.yml usually lives under boards/<vendor>/<bare name>/, even if `board:` is
    # fully qualified and/or carries an "@<revision>" suffix -- but "bare name" isn't
    # universal: most vendors (Renesas, Nordic, Raspberry Pi, ...) name the directory
    # identically to board.yml's own `name:`, but some (e.g. Arduino's `uno_r4`, whose
    # board.yml declares `name: arduino_uno_r4`) drop the vendor prefix from the
    # directory instead. Try the fast, common-case glob first; fall back to scanning
    # every board.yml under boards/ for a matching `name:` field, same fallback
    # dts_lookup.py's own _find_board_dir() already relies on for this reason.
    parts = parse_board_string(board)
    board_yml_matches = list(root.glob(f"boards/*/{parts.name}/board.yml"))
    if not board_yml_matches:
        for board_yml in root.glob("boards/*/**/board.yml"):
            try:
                doc = yaml.safe_load(board_yml.read_text())
            except (OSError, yaml.YAMLError):
                continue
            if not isinstance(doc, dict):
                continue
            # Two board.yml schemas coexist upstream: the older singular "board:" and
            # newer HWMv2 "boards:" (a list) -- same distinction dts_lookup.py's own
            # _find_board_dir() fallback scanner handles.
            entries = [doc["board"]] if "board" in doc else doc.get("boards", [])
            if any(
                isinstance(entry, dict) and entry.get("name") == parts.name
                for entry in entries
            ):
                board_yml_matches = [board_yml]
                break
    if not board_yml_matches:
        raise cv.Invalid(
            f"Could not find boards/*/{parts.name}/board.yml under this board_source. "
            f"Please check the source contains that board.",
            [CONF_FRAMEWORK, CONF_BOARD_SOURCE],
        )
    if parts.revision is not None:
        board_dir = board_yml_matches[0].parent
        resolved, declares_revisions = resolve_revision(board_dir, parts.revision)
        if not declares_revisions:
            raise cv.Invalid(
                f"Board {parts.name!r} does not declare any revisions, but "
                f"'{CONF_BOARD}' requested '@{parts.revision}'.",
                [CONF_BOARD],
            )
        if resolved is None:
            revisions = declared_revisions(board_dir)
            raise cv.Invalid(
                f"Revision {parts.revision!r} is not valid for board {parts.name!r}. "
                f"Declared revisions: {', '.join(revisions)}",
                [CONF_BOARD],
            )
    return root


def _resolve_extra_source(
    config: ConfigType,
    items: list[str],
    board_root: Path | None,
    *,
    source_key: str,
    items_key: str,
    marker_relpath: Callable[[str], str],
    kind_label: str,
) -> Path | None:
    """Resolve a framework: <kind>_source: into a local root directory.

    Shared by _resolve_shield_source()/_resolve_snippet_source() -- both mirror
    board_source:'s own git/local resolution exactly, differing only in the config
    key and the per-item existence-check path. <kind>_source: is optional even when
    <items>: is set -- the root defaults to board_root (a shield/snippet
    accompanying a custom board conventionally lives alongside that root's boards/,
    mirroring Zephyr's own repo layout and its own SHIELD_ROOT/SNIPPET_ROOT
    default), or to None (let west/dts_lookup search the SDK's own tree) when
    board_source: wasn't set either.

    Existence is validated against whichever root is ultimately used -- including
    the board_root fallback, not just an explicit <kind>_source: override -- so a
    typo'd or genuinely missing item is caught here at config time instead of
    silently falling through to a debug-only "not found" warning during DTS
    validation.
    """
    explicit = source_key in config
    if not explicit:
        root = board_root
    else:
        conf = config[source_key]
        if conf[CONF_TYPE] == TYPE_GIT:
            with cv.prepend_path([CONF_FRAMEWORK, source_key]):
                root, _ = git.clone_or_update(
                    url=conf[CONF_URL],
                    ref=conf.get(CONF_REF),
                    refresh=conf.get(CONF_REFRESH, _DEFAULT_REFRESH),
                    domain=KEY_ZEPHYR,
                    username=conf.get(CONF_USERNAME),
                    password=conf.get(CONF_PASSWORD),
                )
                if path := conf.get(CONF_PATH):
                    root = root / path
        elif conf[CONF_TYPE] == TYPE_LOCAL:
            root = Path(CORE.relative_config_path(conf[CONF_PATH]))
        else:
            raise NotImplementedError

    if root is None:
        # No override and no board_source: either -- nothing to validate against,
        # same as a bare `board:` with no board_source: (checked later, at DTS
        # validation time, against the SDK's own tree instead).
        return None

    error_path = [CONF_FRAMEWORK, source_key if explicit else items_key]
    source_desc = (
        f"this {source_key}"
        if explicit
        else f"'{CONF_BOARD_SOURCE}' (the default root, since '{source_key}' wasn't set)"
    )
    for item in items:
        if not (root / marker_relpath(item)).is_file():
            raise cv.Invalid(
                f"Could not find {marker_relpath(item)} under {source_desc}. "
                f"Please check the source contains that {kind_label}, or set "
                f"'{source_key}' explicitly.",
                error_path,
            )
    return root


def _resolve_shield_source(
    config: ConfigType, shields: list[str], board_root: Path | None
) -> Path | None:
    """Resolve framework: shield_source: into a local SHIELD_ROOT directory."""
    return _resolve_extra_source(
        config,
        shields,
        board_root,
        source_key=CONF_SHIELD_SOURCE,
        items_key=CONF_SHIELDS,
        marker_relpath=lambda shield: f"boards/shields/{shield}/shield.yml",
        kind_label="shield",
    )


def _resolve_snippet_source(
    config: ConfigType, snippets: list[str], board_root: Path | None
) -> Path | None:
    """Resolve framework: snippet_source: into a local SNIPPET_ROOT directory."""
    return _resolve_extra_source(
        config,
        snippets,
        board_root,
        source_key=CONF_SNIPPET_SOURCE,
        items_key=CONF_SNIPPETS,
        marker_relpath=lambda snippet: f"snippets/{snippet}/snippet.yml",
        kind_label="snippet",
    )


def _reject_kconfig_watchdog(kconfig_options: ConfigType) -> None:
    # kconfig_options win over every component setting, and a watchdog that is built but never
    # armed or fed resets any chip whose watchdog already runs at boot
    for name, value in kconfig_options.items():
        if (
            name.removeprefix("CONFIG_") == "WATCHDOG"
            and not isinstance(value, dict)
            and value in (True, "y", "Y")
        ):
            raise cv.Invalid(
                f"'{CONF_KCONFIG_OPTIONS}: {name}' conflicts with "
                f"'{CONF_WATCHDOG_TIMEOUT}: 0' -- set '{CONF_WATCHDOG_TIMEOUT}' instead",
                [CONF_FRAMEWORK, CONF_KCONFIG_OPTIONS, name],
            )


def zephyr_config_schema(
    config: ConfigType, families: frozenset[str], platform: str
) -> ConfigType:
    """Validate a <platform>: block whose variants come from `families`."""
    core = CORE.data.setdefault(KEY_CORE, {})
    core[KEY_TARGET_PLATFORM] = platform
    core[KEY_TARGET_FRAMEWORK] = KEY_ZEPHYR
    config = zephyr_toolchain_schema(families)(config)
    framework = config[CONF_FRAMEWORK]
    variant = config[CONF_VARIANT]
    max_timeout_ms = VARIANTS[variant].watchdog_max_timeout_ms
    # A variant whose ceiling sits below the normal 5s floor (e.g. RA4M1's
    # 5000ms) can't reach 5s either -- push the floor down to match.
    min_timeout_ms = 5000
    if max_timeout_ms is not None and max_timeout_ms < min_timeout_ms:
        min_timeout_ms = max_timeout_ms
    explicit_timeout = config.get(CONF_WATCHDOG_TIMEOUT)
    if explicit_timeout is not None and explicit_timeout.total_milliseconds != 0:
        explicit_ms = explicit_timeout.total_milliseconds
        if explicit_ms < min_timeout_ms or (
            max_timeout_ms is not None and explicit_ms > max_timeout_ms
        ):
            raise cv.Invalid(
                f"'{CONF_WATCHDOG_TIMEOUT}' of {explicit_timeout} is outside what "
                f"variant '{variant}' can actually arm ({min_timeout_ms}-"
                f"{max_timeout_ms if max_timeout_ms is not None else 60000} ms) -- "
                "use 0 to disable the watchdog instead, or a value in that range",
                [CONF_WATCHDOG_TIMEOUT],
            )
    # 10s: covers ZBOSS's heavy CPU usage during zigbee startup without needing a
    # separate zigbee-conditional bump (platform: nrf52's own equivalent) -- the
    # plain default was arbitrary anyway. Clamped down to the variant's own
    # achievable ceiling when that's lower (see watchdog_max_timeout_ms).
    default_timeout = cv.TimePeriod(seconds=10)
    if (
        max_timeout_ms is not None
        and max_timeout_ms < default_timeout.total_milliseconds
    ):
        default_timeout = cv.TimePeriod(milliseconds=max_timeout_ms)
    config.setdefault(CONF_WATCHDOG_TIMEOUT, default_timeout)
    if config[CONF_WATCHDOG_TIMEOUT].total_milliseconds == 0:
        _reject_kconfig_watchdog(framework[CONF_KCONFIG_OPTIONS])
    board_root = None
    if CONF_BOARD_SOURCE in framework:
        if CONF_BOARD not in config:
            raise cv.Invalid(
                f"'{CONF_BOARD_SOURCE}' requires '{CONF_BOARD}' to also be set",
                [CONF_FRAMEWORK, CONF_BOARD_SOURCE],
            )
        board_root = _resolve_board_source(framework, config[CONF_BOARD])
    elif CONF_BOARD in config:
        valid_boards = VARIANTS[variant].boards
        board = config[CONF_BOARD]
        if valid_boards and board not in valid_boards:
            raise cv.Invalid(
                f"Board {board!r} is not valid for variant {variant!r}. "
                f"See https://esphome.io/components/{platform}/ for valid boards.",
                [CONF_BOARD],
            )
    shields = framework[CONF_SHIELDS]
    if CONF_SHIELD_SOURCE in framework and not shields:
        raise cv.Invalid(
            f"'{CONF_SHIELD_SOURCE}' requires '{CONF_SHIELDS}' to also be set",
            [CONF_FRAMEWORK, CONF_SHIELD_SOURCE],
        )
    shield_root = (
        _resolve_shield_source(framework, shields, board_root) if shields else None
    )
    snippets = framework[CONF_SNIPPETS]
    if CONF_SNIPPET_SOURCE in framework and not snippets:
        raise cv.Invalid(
            f"'{CONF_SNIPPET_SOURCE}' requires '{CONF_SNIPPETS}' to also be set",
            [CONF_FRAMEWORK, CONF_SNIPPET_SOURCE],
        )
    snippet_root = (
        _resolve_snippet_source(framework, snippets, board_root) if snippets else None
    )
    config = get_variant_module(variant).config_schema(config)
    framework = config[CONF_FRAMEWORK]
    if (
        framework[CONF_SINGLE_SLOT]
        and zephyr_data()[KEY_BOOTLOADER] != BOOTLOADER_MCUBOOT
    ):
        raise cv.Invalid(
            f"'{CONF_SINGLE_SLOT}: true' requires the MCUboot bootloader "
            f"(set 'framework: advanced: bootloader: {BOOTLOADER_MCUBOOT}' for this "
            "variant)",
            [CONF_FRAMEWORK, CONF_SINGLE_SLOT],
        )
    zephyr_data()[KEY_BOARD_ROOT] = board_root
    zephyr_data()[KEY_SHIELDS] = shields
    zephyr_data()[KEY_SHIELD_ROOT] = shield_root
    zephyr_data()[KEY_SNIPPET_ROOT] = snippet_root
    return config


# The platform's to_code on the Zephyr toolchain. Not this component's own to_code,
# which would add a section comment to every nrf52 main.cpp.
@coroutine_with_priority(CoroPriority.PLATFORM)
async def zephyr_toolchain_to_code(config: ConfigType) -> None:
    from .dts_fetch import fetch_board_dts

    variant = zephyr_data()["variant"]
    sdk_name, sdk = resolve_sdk(
        VARIANTS[variant], zephyr_data().get(KEY_FRAMEWORK_TYPE)
    )
    await fetch_board_dts(
        variant,
        sdk_name,
        sdk,
        zephyr_data()["sdk_source"],
        family=zephyr_variant_family(),
    )

    from .dts_lookup import (
        log_board_capabilities,
        validate_board,
        validate_board_revision,
    )

    board = zephyr_data()["board"]
    board_valid = validate_board(board)
    if board_valid is False:
        raise EsphomeError(
            f"Board '{board}' was not found. Check the board name, or "
            f"'{CONF_BOARD_SOURCE}' if using a custom board."
        )
    if board_valid is None:
        _LOGGER.warning(
            "[zephyr] Could not verify board '%s' exists -- board DTS files were "
            "unavailable (see earlier warning). If '%s' is misspelled, this will "
            "fail later inside the build instead of here.",
            board,
            board,
        )
    elif validate_board_revision(board) is False:
        raise EsphomeError(
            f"Board '{board}' requests a revision that is not valid for that board. "
            f"Check the '@<revision>' suffix in '{CONF_BOARD}'."
        )
    log_board_capabilities(
        board,
        variant,
        VARIANTS[variant],
        str(CORE.data[KEY_CORE][KEY_FRAMEWORK_VERSION]),
        zephyr_data()["board_root"],
        zephyr_data()[KEY_SHIELDS],
    )

    _base_to_code(config)
    family = zephyr_variant_family()
    if family is not None:
        get_family_module(family).to_code(config)
    await get_variant_module(variant).to_code(config)


def _restore_upload_data(zephyr_config: ConfigType) -> None:
    """Rebuild what upload reads from zephyr_data() out of the cached validated
    config, which skips validation. Re-validating that dict isn't an option: validation
    adds keys its own schema rejects, and would re-resolve moving git refs the build
    already pinned."""
    if KEY_FRAMEWORK_VERSION not in CORE.data.get(KEY_CORE, {}):
        raise EsphomeError(
            "Zephyr framework version is missing from the build metadata; "
            "please re-validate and recompile."
        )
    variant = zephyr_config[CONF_VARIANT]
    variant_data = VARIANTS[variant]
    framework = zephyr_config[CONF_FRAMEWORK]
    advanced = framework.get(CONF_ADVANCED, {})
    sdk_source = framework.get(CONF_SOURCE)
    if sdk_source is not None:
        # The cache holds paths as strings; the schema turns them back into Paths.
        resolved_ref = sdk_source.get(KEY_SDK_SOURCE_RESOLVED_REF)
        sdk_source = _FRAMEWORK_SOURCE_SCHEMA(
            {k: v for k, v in sdk_source.items() if k != KEY_SDK_SOURCE_RESOLVED_REF}
        )
        if resolved_ref is not None:
            sdk_source[KEY_SDK_SOURCE_RESOLVED_REF] = resolved_ref
    set_core_data(
        variant,
        zephyr_config[CONF_BOARD],
        get_family_module(variant_data.family).bootloader(advanced)
        if variant_data.family
        else "",
        CORE.data[KEY_CORE][KEY_FRAMEWORK_VERSION],
        zephyr_config,
        framework_type=resolve_sdk(variant_data, framework.get(CONF_TYPE))[0],
        sdk_source=sdk_source,
        runner=advanced.get(CONF_RUNNER),
    )
    _apply_module_overrides(
        cv.ensure_list(_MODULE_SCHEMA)(framework.get(CONF_MODULES, []))
    )


def _variant_hook(name: str, default: Any = None) -> Any:
    """The active variant's module attribute `name`, the way its family and variant
    modules plug optional behaviour into upload, reset and compile."""
    if (variant := zephyr_variant()) is None:
        return default
    return getattr(get_variant_module(variant), name, default)


def _family_hook(name: str, default: Any = None) -> Any:
    """The active family's module attribute `name`; see _variant_hook()."""
    if (family := zephyr_variant_family()) is None:
        return default
    return getattr(get_family_module(family), name, default)


def reset_over_usb(config: ConfigType, port: str, timeout: float = 10.0) -> bool:
    """Restart a running device by opening its USB serial port at 2001 baud, then wait
    for the port to disappear and come back. Returns False, sending nothing, if the
    firmware has no handler for it (see dfu.cpp, bootsel_touch.cpp)."""
    import time  # noqa: PLC0415

    import serial  # noqa: PLC0415

    from esphome.util import get_serial_ports  # noqa: PLC0415

    if KEY_ZEPHYR not in CORE.data:
        # Best effort: logs do not need a compiled build, so do not fail them.
        try:
            _restore_upload_data(config[CORE.target_platform])
        except (KeyError, EsphomeError) as err:
            _LOGGER.debug("Skipping USB reset: %s", err)
            return False

    has_usb_reset = _variant_hook("has_usb_reset") or _family_hook("has_usb_reset")
    if has_usb_reset is None or not has_usb_reset(config):
        return False
    try:
        with serial.Serial(port, baudrate=2001):
            pass
    except serial.SerialException as err:
        _LOGGER.warning("Could not open %s at 2001 baud: %s", port, err)
        return False
    deadline = time.monotonic() + timeout
    gone = False
    while time.monotonic() < deadline:
        present = port in (p.path for p in get_serial_ports())
        if gone and present:
            return True
        gone = gone or not present
        time.sleep(0.5)
    return False


def get_download_types(storage_json: StorageJSON) -> list[dict[str, str]]:
    """Binary-download entries for a built Zephyr RTOS firmware, as nrf52's for SDK 3.4+.

    Files are relative to the recorded firmware, the MCUboot app image's directory.
    """
    # No recorded firmware path means nothing was built; no downloads.
    if storage_json.firmware_bin_path is None:
        return []
    app_dir = Path(storage_json.firmware_bin_path).parent
    if (app_dir / "zephyr.uf2").is_file():
        types = [
            {
                "title": "UF2 package (recommended)",
                "description": "For flashing via a UF2 bootloader as a flash drive.",
                "file": "zephyr.uf2",
                "download": f"{storage_json.name}.uf2",
            },
        ]
    else:
        types = [
            {
                "title": "HEX package",
                "description": "For flashing via a debug probe.",
                "file": "zephyr.hex",
                "download": f"{storage_json.name}.hex",
            },
        ]
    # nRF52 serial DFU package, written next to the build's zephyr/ directory
    dfu_package = Path("..", "..", "..", "firmware.zip")
    if (app_dir / dfu_package).is_file():
        types.append(
            {
                "title": "DFU package",
                "description": "For flashing via nrfutil using USB CDC.",
                "file": dfu_package.as_posix(),
                "download": f"dfu-{storage_json.name}.zip",
            }
        )
    if (app_dir / "zephyr.signed.bin").is_file():
        types.append(
            {
                "title": "App update package",
                "description": "For OTA updating a device.",
                "file": "zephyr.signed.bin",
                "download": f"app-{storage_json.name}.img",
            }
        )
    return types


def show_logs(config: ConfigType, args, devices: list[str]) -> bool:
    """Logs over BLE (ble_nus), as nrf52; False for any other device."""
    from esphome.components.nrf52 import show_logs as nrf52_show_logs

    return nrf52_show_logs(config, args, devices)


def has_network_ota(config: ConfigType) -> bool:
    """True when zephyr_mcumgr OTA listens on UDP."""
    from esphome.components.nrf52.const import CONF_TRANSPORT, CONF_UDP

    return any(
        ota.get(CONF_PLATFORM) == "zephyr_mcumgr"
        and ota.get(CONF_TRANSPORT, {}).get(CONF_UDP)
        for ota in config.get(CONF_OTA, [])
    )


def _upload_mcumgr(config: ConfigType, args, host: str) -> bool:
    """Upload over zephyr_mcumgr's BLE or UDP transport; False when neither applies."""
    from esphome.components.nrf52.const import CONF_TRANSPORT, CONF_UDP

    transport = next(
        (
            ota.get(CONF_TRANSPORT, {})
            for ota in config.get(CONF_OTA, [])
            if ota.get(CONF_PLATFORM) == "zephyr_mcumgr"
        ),
        None,
    )
    if transport is None:
        return False

    from esphome.components import network
    from esphome.components.nrf52 import _resolve_smp_udp_hosts, _upload_over_udp
    from esphome.components.nrf52.ble_logger import is_ble_address
    from esphome.upload_targets import PortType, get_port_type

    from ..zephyr_mcumgr.ota import CONF_BLE

    file = getattr(args, "file", None) or str(CORE.firmware_bin)
    if transport.get(CONF_BLE) and (host == "BLE" or is_ble_address(host)):
        asyncio.run(_upload_mcumgr_ble(host, file))
        return True
    if transport.get(CONF_UDP) and get_port_type(host) == PortType.NETWORK:
        ipv4 = config.get(network.DOMAIN, {}).get(network.CONF_ENABLE_IPV4, False)
        addresses = _resolve_smp_udp_hosts(host, ipv4)
        asyncio.run(_upload_over_udp(host, addresses, file))
        return True
    return False


async def _upload_mcumgr_ble(host: str, file: str) -> None:
    from esphome.components.nrf52 import _smpmgr_upload
    from esphome.components.nrf52.ota import smpmgr_scan

    address = await smpmgr_scan(CORE.name) if host == "BLE" else host
    await _smpmgr_upload(address, file)


def upload_program(config: ConfigType, args, host: str) -> bool:
    if KEY_ZEPHYR not in CORE.data:
        zephyr_config = config.get(CORE.target_platform)
        if not zephyr_config:
            raise EsphomeError(
                "Zephyr platform configuration is missing; "
                "please re-validate and recompile."
            )
        _restore_upload_data(zephyr_config)

    from esphome.upload_targets import PortType, get_port_type

    if host == "PYOCD" or get_port_type(host) in (PortType.SERIAL, PortType.BOOTSEL):
        from .build_zephyr import check_bootloader_built

        # Every non-OTA path writes the bootloader too
        check_bootloader_built(CORE.relative_build_path(".west_build"))

    if host == "BOOTSEL":
        upload_bootsel = _family_hook("upload_bootsel")
        return upload_bootsel is not None and upload_bootsel()

    if host == "PYOCD":
        if not _family_hook("PYOCD_UPLOAD", True):
            return False

        configured_runner = zephyr_data().get(KEY_RUNNER)
        if configured_runner and configured_runner != "pyocd":
            _LOGGER.info(
                "Ignoring configured advanced.runner '%s' -- --device PYOCD always "
                "forces the pyocd runner",
                configured_runner,
            )

        from .build_zephyr import run_west_flash_pyocd
        from .framework import check_and_install as west_install

        version = str(CORE.data[KEY_CORE][KEY_FRAMEWORK_VERSION])
        variant_data = VARIANTS[zephyr_variant()]
        _, sdk = resolve_sdk(variant_data, zephyr_data().get(KEY_FRAMEWORK_TYPE))
        python_bin, framework_path, west_env = west_install(
            sdk,
            version,
            zephyr_data()["west_version"],
            zephyr_data()["ninja_version"],
            zephyr_data()["sdk_source"],
            projects=zephyr_additional_framework_projects(sdk),
        )
        build_dir = CORE.relative_build_path(".west_build")
        if not run_west_flash_pyocd(python_bin, framework_path, west_env, build_dir):
            raise EsphomeError("Zephyr pyocd flash failed")
        return True

    # A variant's own bootloader (TinyUF2, an Adafruit nRF52 bootloader) goes first;
    # None means it did not handle this upload.
    if (upload_bootloader := _variant_hook("upload_bootloader")) is not None and (
        result := upload_bootloader(config, args, host)
    ) is not None:
        return result

    if _upload_mcumgr(config, args, host):
        return True

    # A family with its own upload flow (rpi_pico's 1200-baud touch, esp32's
    # esptool runner) decides the result.
    if (family_upload := _family_hook("upload_program")) is not None:
        return family_upload(config, args, host)

    # Every other family is flashed by the board's default west runner (jlink,
    # pyocd, etc.) rather than over a selected serial port.
    if get_port_type(host) != PortType.SERIAL:
        return False

    from .build_zephyr import resolve_dev_id, run_west_flash_generic
    from .framework import check_and_install as west_install

    version = str(CORE.data[KEY_CORE][KEY_FRAMEWORK_VERSION])
    variant_data = VARIANTS[zephyr_variant()]
    _, sdk = resolve_sdk(variant_data, zephyr_data().get(KEY_FRAMEWORK_TYPE))
    python_bin, framework_path, west_env = west_install(
        sdk,
        version,
        zephyr_data()["west_version"],
        zephyr_data()["ninja_version"],
        zephyr_data()["sdk_source"],
        projects=zephyr_additional_framework_projects(sdk),
    )
    build_dir = CORE.relative_build_path(".west_build")
    configured_runner = zephyr_data().get(KEY_RUNNER)
    # Disambiguate which attached probe to flash when the effective runner supports
    # device IDs -- see resolve_dev_id() for why this can't just always be forwarded,
    # and #85 for the multi-probe problem it fixes.
    dev_id = resolve_dev_id(
        python_bin,
        framework_path,
        build_dir,
        host,
        runner_override=configured_runner,
    )
    if dev_id:
        _LOGGER.info("Selecting probe %s (port %s) for west flash", dev_id, host)
    if not run_west_flash_generic(
        python_bin,
        framework_path,
        west_env,
        build_dir,
        dev_id,
        runner=configured_runner,
    ):
        raise EsphomeError("Zephyr west flash failed")
    return True


def run_compile(args, config: ConfigType) -> bool:

    from .library import generate_zephyr_modules

    variant = zephyr_data()["variant"]
    extra_modules = generate_zephyr_modules(list(CORE.platformio_libraries.values()))

    if not CORE.using_toolchain_zephyr:
        return False

    from .build_zephyr import generate_cmake_lists, run_west_blobs_fetch, run_west_build
    from .framework import (
        check_and_install as west_install,
        install_git_modules,
        install_toolchain,
    )

    variant_data = VARIANTS[variant]
    sdk_name, sdk = resolve_sdk(variant_data, zephyr_data().get(KEY_FRAMEWORK_TYPE))
    version = str(CORE.data[KEY_CORE][KEY_FRAMEWORK_VERSION])
    modules = resolve_zephyr_modules()
    # Modules join the build like a converted PlatformIO library, not the workspace
    extra_modules.extend(m.local_path for m in modules if m.local_path is not None)
    extra_modules.extend(install_git_modules(modules, sdk.source.url))
    python_bin, framework_path, west_env = west_install(
        sdk,
        version,
        zephyr_data()["west_version"],
        zephyr_data()["ninja_version"],
        zephyr_data()["sdk_source"],
        projects=zephyr_additional_framework_projects(sdk),
    )
    if sdk_name == "silabs":
        from .commander_setup import check_and_install as commander_install

        commander_version = (
            config[CORE.target_platform][CONF_FRAMEWORK]
            .get(CONF_ADVANCED, {})
            .get(CONF_COMMANDER_VERSION)
        )
        commander_dir = commander_install(commander_version)
        west_env["PATH"] = f"{commander_dir}{os.pathsep}{west_env['PATH']}"
    sdk_dir = install_toolchain(framework_path, variant_data.toolchain)
    blob_specs = list(zephyr_data()["blobs"])
    if variant_data.blobs:
        blob_specs.append(variant_data.blobs)
    for module, allow_regex, sentinel_name in blob_specs:
        run_west_blobs_fetch(
            python_bin, framework_path, west_env, module, allow_regex, sentinel_name
        )
    cmake_lists_changed = generate_cmake_lists()
    board = zephyr_data()[KEY_BOARD]

    # West can't detect a missing CMake cache itself -- its pristine modes read
    # ZEPHYR_BASE from the very cache that was dropped. build_dir here must match
    # run_west_build()'s own build_dir selection exactly.
    build_dir = CORE.relative_build_path(".west_build")
    if (
        cmake_lists_changed or not (build_dir / "CMakeCache.txt").is_file()
    ) and build_dir.is_dir():
        _LOGGER.info("Build inputs changed, cleaning %s", build_dir)
        rmtree(build_dir)

    run_west_build(
        python_bin,
        framework_path,
        board,
        west_env,
        sdk_install_dir=sdk_dir,
        extra_modules=extra_modules,
        board_root=zephyr_data().get(KEY_BOARD_ROOT),
        snippets=zephyr_data()[KEY_SNIPPETS],
        shield_root=zephyr_data().get(KEY_SHIELD_ROOT),
        shields=zephyr_data()[KEY_SHIELDS],
        snippet_root=zephyr_data().get(KEY_SNIPPET_ROOT),
        requested_runner=zephyr_data().get(KEY_RUNNER),
    )
    from .mcuboot import check_mcuboot_swap_limit

    check_mcuboot_swap_limit(build_dir, CORE.firmware_bin)
    if (post_build := _variant_hook("post_build")) is not None:
        post_build(python_bin, west_env)

    from esphome.build_helpers.idedata import warn_if_idedata_missing

    from .toolchain import get_idedata

    warn_if_idedata_missing(get_idedata)
    return True


def _addr2line(addr2line: str, elf: Path, addr: str) -> str:
    try:
        result = subprocess.run(
            [addr2line, "-e", elf, addr],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip().splitlines()[0]
    except Exception as err:  # noqa: BLE001  # pylint: disable=broad-except
        _LOGGER.error("Running command failed: %s", err)
    return ""


# The PC bound matches the gate in platform_hooks.STACKTRACE_GATES;
# the logger prints both registers with %08x, so a real PC is always
# 8 digits. tests/unit_tests/test_stacktrace.py guards against drift.
STACKTRACE_ZEPHYR_PC_LR_RE = re.compile(r"PC=(0x[0-9a-fA-F]{3,})\s+LR=(0x[0-9a-fA-F]+)")


def process_stacktrace(config: ConfigType, line: str, backtrace_state: bool) -> bool:
    if "Last crash:" in line:
        return True
    if backtrace_state:
        match = STACKTRACE_ZEPHYR_PC_LR_RE.search(line)
        if match:
            pc = match.group(1)
            lr = match.group(2)
            from esphome.analyze_memory.toolchain import find_tool

            addr2line = find_tool("addr2line")
            if addr2line is None:
                return False
            # --sysbuild nests the app image under an extra "zephyr" domain
            # directory on top of Zephyr's own standard "zephyr" build-output
            # subdirectory (same layout as esphome/core/__init__.py's firmware_bin).
            elf = CORE.relative_build_path(
                ".west_build", "zephyr", "zephyr", "zephyr.elf"
            )
            if not elf.exists():
                _LOGGER.warning("%s does not exists", elf)
                return False
            _LOGGER.error("=== CRASH ===")
            _LOGGER.error("PC: %s", _addr2line(addr2line, elf, pc))
            _LOGGER.error("LR: %s", _addr2line(addr2line, elf, lr))
    return False
