import logging

from esphome import automation
import esphome.codegen as cg
from esphome.components.const import CONF_ON_STATE_CHANGE
from esphome.config_helpers import (
    filter_source_files_from_defines,
    filter_source_files_from_platform,
)
import esphome.config_validation as cv
from esphome.const import (
    CONF_DISABLED,
    CONF_ESPHOME,
    CONF_ON_ERROR,
    CONF_OTA,
    CONF_PLATFORM,
    CONF_SAFE_MODE,
    PlatformFramework,
)
from esphome.core import CORE, coroutine_with_priority
from esphome.coroutine import CoroPriority
from esphome.cpp_generator import MockObj
from esphome.types import ConfigType, TemplateArgsType

_LOGGER = logging.getLogger(__name__)

OTA_STATE_LISTENER_KEY = "ota_state_listener"

CODEOWNERS = ["@esphome/core"]
DOMAIN = "ota"


def AUTO_LOAD() -> list[str]:
    # Every backend computes an MD5 over the transferred image, except nrf52, whose
    # zephyr_mcumgr OTA path doesn't use this component's backend at all.
    components = ["safe_mode"]
    if not CORE.is_nrf52:
        components.append("md5")
    if CORE.is_esp32:
        components.extend(["watchdog"])
    return components


IS_PLATFORM_COMPONENT = True

CONF_ON_ABORT = "on_abort"
CONF_ON_BEGIN = "on_begin"
CONF_ON_END = "on_end"
CONF_ON_PROGRESS = "on_progress"
CONF_SWAP_METHOD = "swap_method"

# Shared by every ota: platform that can run on Zephyr (platform: esphome and
# platform: zephyr_mcumgr both end up pushing the very same MCUboot sysbuild
# MCUBOOT_MODE_SWAP_* Kconfig choice symbol -- see
# esphome.components.zephyr). Defined here, not in the zephyr package,
# so that platform: esphome -- built on every platform, not just Zephyr -- can
# use it without importing esphome.components.zephyr at module-load time.
SWAP_METHOD_SCHEMA = {
    cv.SplitDefault(
        CONF_SWAP_METHOD,
        esp32_zephyr="offset",
        nrf="offset",
        nrf_52="move",
        nrf_54l15="move",
        nrf_54lm20a="move",
        nxp="offset",
        renesas="offset",
        rp2_zephyr="offset",
        silabs="offset",
        stm32="offset",
    ): cv.one_of("scratch", "move", "offset", lower=True),
}


def reject_swap_method_off_zephyr(config: ConfigType) -> ConfigType:
    """swap_method: is only applied on Zephyr RTOS builds; reject it elsewhere."""
    if not CORE.using_toolchain_zephyr and CONF_SWAP_METHOD in config:
        raise cv.Invalid(
            f"'{CONF_SWAP_METHOD}' is only available when using the zephyr framework "
            "on a supported platform",
            path=[CONF_SWAP_METHOD],
        )
    return config


ota_ns = cg.esphome_ns.namespace("ota")
OTAComponent = ota_ns.class_("OTAComponent", cg.Component)
OTAState = ota_ns.enum("OTAState")

_request_state_callback_slot = cg.slot_counter("OTA_STATE_CALLBACK_COUNT")

# Callback parameters, shared by every key and every OTA component
_STATE_CALLBACK_PARAMS: TemplateArgsType = [
    (OTAState, "state"),
    (cg.float_, "progress"),
    (cg.uint8, "error"),
    (OTAComponent.operator("ptr"), "component"),
]
# conf key: (automation args, forwarded expressions, state filter)
_STATE_AUTOMATIONS: dict[str, tuple[TemplateArgsType, list[str], MockObj | None]] = {
    CONF_ON_STATE_CHANGE: ([(OTAState, "state")], ["state"], None),
    CONF_ON_ABORT: ([], [], ota_ns.OTA_ABORT),
    CONF_ON_BEGIN: ([], [], ota_ns.OTA_STARTED),
    CONF_ON_PROGRESS: ([(cg.float_, "x")], ["progress"], ota_ns.OTA_IN_PROGRESS),
    CONF_ON_END: ([], [], ota_ns.OTA_COMPLETED),
    CONF_ON_ERROR: ([(cg.uint8, "x")], ["error"], ota_ns.OTA_ERROR),
}


def _mcuboot_slot_writers(config: ConfigType) -> list[ConfigType]:
    """The OTA platforms that write into MCUboot's secondary slot; the
    Adafruit bootloader runs adafruit_ble's update itself."""
    return [c for c in config if c[CONF_PLATFORM] != "adafruit_ble"]


def _ota_final_validate(config: ConfigType) -> None:
    if len(config) < 1:
        raise cv.Invalid(
            f"At least one platform must be specified for '{CONF_OTA}'; add '{CONF_PLATFORM}: {CONF_ESPHOME}' for original OTA functionality"
        )
    # CORE.using_toolchain_zephyr is the Zephyr RTOS variant dispatch (esp32_h2, esp32_c6, ...);
    # it excludes nrf52, which also builds on Zephyr but validates its own bootloader separately.
    if CORE.using_toolchain_zephyr and _mcuboot_slot_writers(config):
        from esphome.components.zephyr import zephyr_data  # noqa: PLC0415
        from esphome.components.zephyr.const import (  # noqa: PLC0415
            BOOTLOADER_MCUBOOT,
            KEY_BOOTLOADER,
            KEY_SINGLE_SLOT,
        )

        bootloader = zephyr_data()[KEY_BOOTLOADER]
        if bootloader != BOOTLOADER_MCUBOOT:
            raise cv.Invalid(
                f"OTA requires the MCUboot bootloader (current: {bootloader or 'none'}); "
                f"set 'advanced: bootloader: {BOOTLOADER_MCUBOOT}'"
            )

        if zephyr_data()[KEY_SINGLE_SLOT]:
            # No secondary slot to write into instead of the one currently executing --
            # live OTA (any platform) risks corrupting the running image mid-transfer,
            # not just losing revert-on-power-loss safety. Serial/wired flashing only.
            raise cv.Invalid(
                f"'{CONF_OTA}:' is not supported with 'single_slot: true' -- there is no "
                f"secondary slot for OTA to write into, and writing into the slot "
                f"currently executing risks crashing mid-update. Flash over serial/USB "
                f"instead."
            )

        # platform: esphome and platform: zephyr_mcumgr can both be configured at
        # once, each with its own swap_method:. They're validated independently,
        # but both push the same MCUboot sysbuild swap-mode choice symbol -- a
        # mismatch would otherwise pass validation and silently write two
        # different, mutually-exclusive symbols =y into sysbuild.conf.
        methods = {
            ota_conf[CONF_PLATFORM]: ota_conf[CONF_SWAP_METHOD]
            for ota_conf in config
            if CONF_SWAP_METHOD in ota_conf
        }
        if len(set(methods.values())) > 1:
            raise cv.Invalid(
                f"'{CONF_SWAP_METHOD}:' must be the same across every '{CONF_OTA}:' "
                f"entry, got {methods}"
            )


FINAL_VALIDATE_SCHEMA = _ota_final_validate

BASE_OTA_SCHEMA = cv.Schema(
    {cv.Optional(key): automation.validate_automation({}) for key in _STATE_AUTOMATIONS}
)


@coroutine_with_priority(CoroPriority.OTA_UPDATES)
async def to_code(config: ConfigType) -> None:
    cg.add_define("USE_OTA")
    CORE.add_job(final_step)

    if CORE.is_rp2 and CORE.using_arduino:
        cg.add_library("Updater", None)

    if CORE.using_toolchain_zephyr and _mcuboot_slot_writers(config):
        from esphome.components.zephyr import (  # noqa: PLC0415
            zephyr_add_prj_conf,
            zephyr_add_sysbuild_conf,
        )

        # Streams the image into MCUboot's secondary flash slot.
        # Same Kconfig chain zephyr_mcumgr/ota/__init__.py enables for its own OTA path.
        zephyr_add_prj_conf("STREAM_FLASH", True)
        zephyr_add_prj_conf("FLASH_MAP", True)
        zephyr_add_prj_conf("FLASH", True)
        zephyr_add_prj_conf("IMG_MANAGER", True)
        zephyr_add_prj_conf("IMG_ERASE_PROGRESSIVELY", True)
        zephyr_add_prj_conf("BOOTLOADER_MCUBOOT", True)
        zephyr_add_sysbuild_conf("BOOTLOADER_MCUBOOT", True)
        cg.add_define("USE_OTA_MCUBOOT")
        # safe_mode confirms the new image after a verified-good boot; without it,
        # zephyr/hal.cpp confirms it at boot and a bad image is not rolled back
        if CORE.config.get(CONF_SAFE_MODE, {}).get(CONF_DISABLED, True):
            _LOGGER.warning(
                "OTA rollback requires safe_mode, disabling rollback support"
            )
        else:
            cg.add_define("USE_OTA_ROLLBACK")


async def ota_to_code(var: MockObj, config: ConfigType) -> None:
    await cg.past_safe_mode()
    use_state_callback = False
    for key, (args, forward, state) in _STATE_AUTOMATIONS.items():
        # Checked per callback, as each trigger did, so one that marks it failed stops the rest
        when = "component == {parent} && !component->is_failed()"
        if state is not None:
            when += f" && state == {state}"
        for conf in config.get(key, []):
            _request_state_callback_slot()
            callback = await automation.build_trigger_callback(
                args, conf, _STATE_CALLBACK_PARAMS, forward, when, var
            )
            cg.add(MockObj(f"{OTAComponent}::add_state_callback")(callback))
            use_state_callback = True
    if use_state_callback:
        request_ota_component_state_listeners()


GLOBAL_LISTENER_COUNT_DEFINE = "ESPHOME_OTA_GLOBAL_LISTENER_COUNT"
_request_global_listener_slot = cg.slot_counter(GLOBAL_LISTENER_COUNT_DEFINE)


def request_ota_component_state_listeners() -> None:
    """Compile in OTAComponent::add_state_listener() without a global listener slot."""
    CORE.data[OTA_STATE_LISTENER_KEY] = True


def request_ota_state_listeners() -> None:
    """Request that OTA state listeners be compiled in.

    Components that need to be notified about OTA state changes (start, progress,
    complete, error) should call this function during their code generation.
    This enables the add_state_listener() API on OTAComponent.
    Each call also reserves one add_global_state_listener() slot, so call it once
    per instance that registers.
    """
    CORE.data[OTA_STATE_LISTENER_KEY] = True
    _request_global_listener_slot()


@coroutine_with_priority(CoroPriority.FINAL)
async def final_step() -> None:
    """Final code generation step to configure optional OTA features."""
    if CORE.data.get(OTA_STATE_LISTENER_KEY, False):
        cg.add_define("USE_OTA_STATE_LISTENER")
        # Not a hub, so cg.slot_list would not emit the zero count
        if cg.get_slot_count(GLOBAL_LISTENER_COUNT_DEFINE) == 0:
            cg.add_define(GLOBAL_LISTENER_COUNT_DEFINE, 0)


_filter_backend_source_files = filter_source_files_from_platform(
    {
        "ota_backend_esp_idf.cpp": {
            PlatformFramework.ESP32_ARDUINO,
            PlatformFramework.ESP32_IDF,
        },
        "ota_backend_esp8266.cpp": {PlatformFramework.ESP8266_ARDUINO},
        "ota_backend_arduino_rp2.cpp": {PlatformFramework.RP2_ARDUINO},
        "ota_backend_arduino_libretiny.cpp": {
            PlatformFramework.BK72XX_ARDUINO,
            PlatformFramework.RTL87XX_ARDUINO,
            PlatformFramework.LN882X_ARDUINO,
        },
        "ota_backend_host.cpp": {PlatformFramework.HOST_NATIVE},
        "ota_backend_zephyr.cpp": {
            PlatformFramework.ESP32_ZEPHYR,
            PlatformFramework.NRF_ZEPHYR,
            PlatformFramework.NXP_ZEPHYR,
            PlatformFramework.RENESAS_ZEPHYR,
            PlatformFramework.RP2_ZEPHYR,
            PlatformFramework.SILABS_ZEPHYR,
            PlatformFramework.STM32_ZEPHYR,
        },
    }
)


# USE_OTA_SIGNED_VERIFICATION_MULTI_KEY is set only on ESP32/IDF;
# USE_OTA_PARTITIONS is set by the esphome OTA platform when
# allow_partition_access is enabled.
# USE_OTA_MCUBOOT is set on Zephyr RTOS builds when an OTA platform writes MCUboot's slot.
_filter_define_source_files = filter_source_files_from_defines(
    {
        "ota_signature_esp_idf.cpp": "USE_OTA_SIGNED_VERIFICATION_MULTI_KEY",
        "ota_bootloader_esp_idf.cpp": "USE_OTA_PARTITIONS",
        "ota_partitions_esp_idf.cpp": "USE_OTA_PARTITIONS",
        "ota_backend_zephyr.cpp": "USE_OTA_MCUBOOT",
    }
)


def FILTER_SOURCE_FILES() -> list[str]:
    return _filter_backend_source_files() + _filter_define_source_files()
