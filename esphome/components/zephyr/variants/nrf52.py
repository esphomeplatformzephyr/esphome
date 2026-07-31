import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.const import (
    CONF_ADVANCED,
    CONF_BOARD,
    CONF_FRAMEWORK,
    CONF_SOURCE,
    KEY_FRAMEWORK_VERSION,
    ThreadModel,
    Toolchain,
)
from esphome.types import ConfigType

from ..const import BOOTLOADER_MCUBOOT, ZEPHYR_VARIANT_NRF52
from . import (
    MAINLINE,
    NCS,
    ZephyrVariant,
    qualify_board,
    resolve_framework_version,
    set_core_data,
)

_DEFAULT_BOARD = "adafruit_feather_nrf52840"
# adafruit_feather_nrf52840_mcuboot_usb (custom_zephyr_boards) is the same hardware
# with boot_partition grown to fit MCUboot's USB CDC-ACM serial recovery -- only
# needed if mcuboot_serial_recovery: true below.
_VALID_BOARDS = [_DEFAULT_BOARD, "adafruit_feather_nrf52840_mcuboot_usb", "xiao_ble"]

CONF_MCUBOOT_SERIAL_RECOVERY = "mcuboot_serial_recovery"

_ADVANCED_SCHEMA = cv.Schema(
    {
        cv.Optional(CONF_MCUBOOT_SERIAL_RECOVERY, default=False): cv.boolean,
    }
)

# GPIO -> nRF52840 SAADC analog-input name. Fixed silicon fact (AIN0-AIN7 datasheet
# pin assignment), independently defined here rather than imported from
# esphome/components/nrf52/const.py's identical AIN_TO_GPIO -- that module belongs to
# the separate NCS-based `platform: nrf52`, and this variant is deliberately
# independent of it (see issue #11: avoiding entanglement between the two was the
# point of building this as a mainline-Zephyr variant in the first place).
# Cross-checked against this exact board's own devicetree: the vbatt divider node
# in adafruit_feather_nrf52840_common.dtsi uses `io-channels = <&adc 5>` (AIN5),
# which is GPIO 29 here -- matching Adafruit's documented P0.29 battery-sense pin.
_ADC_AIN_MAP = {
    2: "AIN0",
    3: "AIN1",
    4: "AIN2",
    5: "AIN3",
    28: "AIN4",
    29: "AIN5",
    30: "AIN6",
    31: "AIN7",
}

# Registry entries — collected by variants/__init__.py
VARIANT_NAME = ZEPHYR_VARIANT_NRF52
VARIANT = ZephyrVariant(
    # NCS (nRF Connect SDK) is the default -- Nordic's own vendor SDK, which is where
    # real hardware support/testing effort for this chip is expected to concentrate.
    # Mainline Zephyr stays available as an alternate (framework: type: zephyr) for
    # anyone who wants to avoid NCS's licensing/tooling footprint, or who hit a
    # regression only present on one side.
    sdk=NCS,
    sdk_name="ncs",
    alt_sdks={"zephyr": MAINLINE},
    family="nordic",
    boards=_VALID_BOARDS,
    valid_toolchains=(Toolchain.SDK_ZEPHYR,),
    toolchain="arm-zephyr-eabi",
    # OpenThread included: issue #11's tracked entanglement was specifically about
    # platform: nrf52's NCS-based OpenThread/Zigbee stack coupling -- picking
    # framework: type: zephyr (mainline) sidesteps that entirely, the same OpenThread
    # source build the esp32-family variants already use. The default
    # framework: type: ncs here uses NCS's own OpenThread, so that original coupling
    # concern applies again there.
    transports=frozenset({"openthread", "ble"}),
    soc="nrf52840",
    # No "scratch": neither board defines a scratch_partition (upstream's stock
    # nrf52840 layout never had one either), and move/offset don't need one.
    swap_methods=frozenset({"move", "offset"}),
    adc_ain_map=_ADC_AIN_MAP,
)


def config_schema(config: ConfigType) -> ConfigType:
    config = dict(config)
    if CONF_BOARD not in config:
        config[CONF_BOARD] = _DEFAULT_BOARD
    board = config[CONF_BOARD]
    if board not in _VALID_BOARDS:
        raise cv.Invalid(
            f"Board {board!r} is not supported by the nrf52 zephyr variant yet. "
            f"Supported boards: {_VALID_BOARDS!r}",
            [CONF_BOARD],
        )
    config[CONF_ADVANCED] = _ADVANCED_SCHEMA(config.get(CONF_ADVANCED, {}))
    config[CONF_BOARD] = qualify_board(VARIANT, config[CONF_BOARD])
    version_str, framework_ver, sdk_name, _ = resolve_framework_version(
        VARIANT, "nrf52", config, "nRF52840 support"
    )
    set_core_data(
        VARIANT_NAME,
        config[CONF_BOARD],
        BOOTLOADER_MCUBOOT,
        framework_ver,
        config,
        framework_type=sdk_name,
        sdk_source=config[CONF_FRAMEWORK].get(CONF_SOURCE),
    )
    config[KEY_FRAMEWORK_VERSION] = version_str
    return config


async def to_code(config: ConfigType) -> None:
    from .. import (
        zephyr_add_overlay,
        zephyr_add_prj_conf,
        zephyr_setup_preferences,
        zephyr_to_code,
    )

    zephyr_to_code(config)
    cg.add_build_flag("-DUSE_ZEPHYR_VARIANT_NRF52")
    cg.add_define("ESPHOME_BOARD", config[CONF_BOARD])
    cg.add_define("ESPHOME_VARIANT", "NRF52")
    cg.add_define(ThreadModel.SINGLE)
    zephyr_setup_preferences()
    zephyr_add_prj_conf("REBOOT", True)
    zephyr_add_prj_conf("HWINFO", True)

    # RSA-2048 (mcuboot's default) is code-size heavy; ECDSA-P256 has a much
    # smaller footprint. Kept unconditional -- strictly smaller either way,
    # independent of whether serial recovery is enabled.
    zephyr_add_prj_conf("BOOT_SIGNATURE_TYPE_RSA", False, image="mcuboot")
    zephyr_add_prj_conf("BOOT_SIGNATURE_TYPE_ECDSA_P256", True, image="mcuboot")

    if config[CONF_BOARD].startswith("xiao_ble"):
        # xiao_ble's upstream devicetree ships a fixed UF2/SoftDevice-coexistence
        # partition table (SoftDevice/code_partition/storage_partition/boot_partition)
        # with no slot0/slot1 labels -- sysbuild's dynamic Partition Manager needs
        # those when building MCUboot as a child image. Pin the app slots explicitly;
        # MCUboot itself is placed by the Partition Manager via
        # PM_PARTITION_SIZE_MCUBOOT.
        mcuboot_size = 0x9000
        storage_size = 0x8000
        total_flash_size = 0x100000  # nRF52840: 1 MB flash
        slot0_start = mcuboot_size
        slot_size = (
            (total_flash_size - mcuboot_size - storage_size) // 2 // 0x1000
        ) * 0x1000
        slot1_start = slot0_start + slot_size
        storage_start = slot1_start + slot_size

        def _mcuboot_partition_overlay() -> str:
            def part(name, start, size):
                return f"""
                {name}: partition@{start:x} {{
                    reg = <0x{start:x} 0x{size:x}>;
                }};"""

            return f"""
                /delete-node/ &reserved_partition_0;
                /delete-node/ &code_partition;
                /delete-node/ &storage_partition;
                /delete-node/ &boot_partition;

                &flash0 {{
                    partitions {{
                        compatible = "fixed-partitions";
                        #address-cells = <1>;
                        #size-cells = <1>;
                        {part("boot_partition", 0, mcuboot_size)}
                        {part("slot0_partition", slot0_start, slot_size)}
                        {part("slot1_partition", slot1_start, slot_size)}
                        {part("storage_partition", storage_start, storage_size)}
                    }};
                }};
            """

        def _code_partition_overlay(partition: str) -> str:
            return f"""
                / {{
                    chosen {{
                        zephyr,code-partition = &{partition};
                    }};
                }};
                """

        zephyr_add_overlay(_mcuboot_partition_overlay())
        zephyr_add_overlay(_mcuboot_partition_overlay(), "mcuboot")
        # MCUboot's own build resolves its load offset from
        # `zephyr,code-partition` too (not just the app's) -- each image needs
        # its own partition here, or MCUboot links itself at the same address
        # as the app it's supposed to boot.
        zephyr_add_overlay(_code_partition_overlay("slot0_partition"))
        zephyr_add_overlay(_code_partition_overlay("boot_partition"), "mcuboot")
        zephyr_add_prj_conf("USB_DEVICE_STACK", False, image="mcuboot")
        zephyr_add_prj_conf("CONSOLE", False, image="mcuboot")
        from esphome.components.zephyr import HexValue

        zephyr_add_prj_conf(
            "PM_PARTITION_SIZE_MCUBOOT", HexValue(mcuboot_size), image="mcuboot"
        )

    if config[CONF_ADVANCED][CONF_MCUBOOT_SERIAL_RECOVERY]:
        # Needs the board's USB CDC-ACM devicetree node (only
        # adafruit_feather_nrf52840_mcuboot_usb has one) and enough
        # boot_partition headroom for the USB stack -- if neither is true,
        # this fails loudly at compile/link time, not silently.
        zephyr_add_prj_conf("MCUBOOT_SERIAL", True, image="mcuboot")
        zephyr_add_prj_conf("BOOT_SERIAL_UART", False, image="mcuboot")
        zephyr_add_prj_conf("BOOT_SERIAL_CDC_ACM", True, image="mcuboot")
        # No recovery button declared on this board -- wait for a DFU
        # connection for 5s after every boot instead.
        zephyr_add_prj_conf("BOOT_SERIAL_ENTRANCE_GPIO", False, image="mcuboot")
        zephyr_add_prj_conf("BOOT_SERIAL_WAIT_FOR_DFU", True, image="mcuboot")
        zephyr_add_prj_conf("BOOT_SERIAL_WAIT_FOR_DFU_TIMEOUT", 5000, image="mcuboot")
        # CDC-ACM and the UART console can't share the same UART device.
        zephyr_add_prj_conf("UART_CONSOLE", False, image="mcuboot")
        zephyr_add_prj_conf("CONSOLE", False, image="mcuboot")
