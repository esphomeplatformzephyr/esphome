from collections.abc import Callable
from importlib import import_module
from pathlib import Path
import textwrap
from typing import TYPE_CHECKING, Any, NamedTuple, TypedDict

import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.const import (
    CONF_BOARD,
    CONF_VARIANT,
    KEY_CORE,
    KEY_FRAMEWORK_VERSION,
    KEY_TARGET_PLATFORM,
    PLATFORM_ZEPHYR,
)
from esphome.core import CORE, CoroPriority, coroutine_with_priority
from esphome.helpers import copy_file_if_changed, write_file_if_changed
from esphome.types import ConfigType
from esphome.writer import clean_cmake_cache

from .const import (
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
from .rtos import (  # noqa: F401
    apply_swap_method,
    board_has_cpu_freq_pstates,
    board_has_pm_states,
    claim_rtt_channel,
    enable_rtt_node,
    get_can_controller_labels,
    get_pinctrl_group_property,
    get_pinctrl_states,
    normalize_dts_label,
    request_zephyr_module,
    reset_over_usb,
    resolve_sdk,
    resolve_uart_node_label,
    resolve_zephyr_bus,
    rtt_channel,
    rtt_label_channel,
    validate_dts_label_exists,
    validate_rtt_port,
    validate_swap_method,
    validate_uart_label_override,
    zephyr_add_blobs,
    zephyr_add_sysbuild_conf,
    zephyr_add_user,
    zephyr_cdc_acm_label,
    zephyr_configure_net_contexts,
    zephyr_family_uart_drivers,
    zephyr_framework_type,
    zephyr_raise_prj_conf_min,
    zephyr_require_partition,
    zephyr_setup_i2c_pinctrl,
    zephyr_setup_spi_pinctrl,
    zephyr_setup_uart_pinctrl,
    zephyr_skip_bootloader_supported,
    zephyr_variant,
    zephyr_variant_family,
)

if TYPE_CHECKING:
    from ..zephyr_rtos.rtt import DEBUG_OWNER, RTT_PORTS  # noqa: F401
    from ..zephyr_rtos.variants import VARIANTS  # noqa: F401

CODEOWNERS = ["@tomaszduda23"]
DOMAIN = "zephyr"

# The zephyr: key forwards to zephyr_rtos until each platform owns its own key.
IS_TARGET_PLATFORM = True


def AUTO_LOAD(config: ConfigType) -> list[str]:
    return ["preferences", "zephyr_rtos"] if CONF_VARIANT in config else []


def CONFIG_SCHEMA(config: ConfigType) -> ConfigType:
    # Auto-loaded by nrf52, which owns the target platform
    target_platform = CORE.data.get(KEY_CORE, {}).get(KEY_TARGET_PLATFORM)
    if target_platform not in (None, PLATFORM_ZEPHYR):
        return config
    from ..zephyr_rtos import zephyr_rtos_config_schema
    from ..zephyr_rtos.variants import VARIANTS

    families = frozenset(info.family for info in VARIANTS.values())
    return zephyr_rtos_config_schema(_nest_flat_config(config), families)


# Keys the flat zephyr: block keeps at the top level; the rest move under framework:
_PLATFORM_KEYS = ("variant", "board", "watchdog_timeout", "framework")
# Flat zephyr: keys that move under framework: advanced:
_ADVANCED_KEYS = ("west_version", "ninja_version")


def _nest_flat_config(config: ConfigType) -> ConfigType:
    """Move the flat zephyr: block's Zephyr-only options under framework:."""
    if not isinstance(config, dict):
        return config
    nested = {key: value for key, value in config.items() if key in _PLATFORM_KEYS}
    framework = dict(nested.get("framework") or {})
    advanced = dict(config.get("advanced") or {})
    for key, value in config.items():
        if key in _ADVANCED_KEYS:
            advanced[key] = value
        elif key not in _PLATFORM_KEYS and key != "advanced":
            framework[key] = value
    if advanced:
        framework["advanced"] = advanced
    nested["framework"] = framework
    return nested


@coroutine_with_priority(CoroPriority.PLATFORM)
async def to_code(config: ConfigType) -> None:
    if not CORE.is_zephyr:
        return
    from ..zephyr_rtos import zephyr_rtos_to_code

    await zephyr_rtos_to_code(config)


_ZEPHYR_RTOS_HOOKS = frozenset(
    {
        "upload_program",
        "run_compile",
        "process_stacktrace",
        "STACKTRACE_ZEPHYR_PC_LR_RE",
    }
)

# zephyr_rtos data, loaded on first use since zephyr_rtos imports this package
_ZEPHYR_RTOS_DATA = {
    "VARIANTS": "variants",
    "RTT_PORTS": "rtt",
    "DEBUG_OWNER": "rtt",
}


def __getattr__(name: str) -> Any:
    if name in _ZEPHYR_RTOS_HOOKS:
        return getattr(import_module("..zephyr_rtos", __package__), name)
    if (module := _ZEPHYR_RTOS_DATA.get(name)) is not None:
        return getattr(import_module(f"..zephyr_rtos.{module}", __package__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
    # zephyr_rtos builds keep their own store, which extends ZephyrData
    if (data := CORE.data.get(KEY_ZEPHYR_RTOS)) is not None:
        return data
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


def zephyr_add_overlay(content: str, image: str = "") -> None:
    data = zephyr_data()
    if image not in data[KEY_OVERLAY]:
        data[KEY_OVERLAY][image] = ""
    elif KEY_ZEPHYR_RTOS in CORE.data:
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
    if KEY_ZEPHYR_RTOS in CORE.data:
        from ..zephyr_rtos import copy_files as rtos_copy_files

        rtos_copy_files()
        return
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
