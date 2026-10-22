import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.const import (
    CONF_BOARD,
    CONF_FRAMEWORK,
    CONF_SOURCE,
    ThreadModel,
    Toolchain,
)
from esphome.types import ConfigType

from ..const import BOOTLOADER_MCUBOOT, CONF_BOARD_SOURCE, ZEPHYR_VARIANT_SIMG301
from ..partitions import BootLayout
from . import (
    MAINLINE,
    ZephyrVariant,
    qualify_board,
    resolve_framework_version,
    set_core_data,
)

# SixG301 Explorer Kit (BRD2719A). Out-of-tree board, not yet upstream (zephyr
# issue #121047, board-add PR #122690) -- needs `zephyr: board_source:` until one
# merges, e.g. https://github.com/esphomeplatformzephyr/custom_zephyr_boards. Bare
# name; no SoC/qualifier segment, same as efr32mg24's xg24_ek2703a.
_DEFAULT_BOARD = "simg301_ek2719a"

# EUSART0 is this SoC's only UART, and its DBUS crossbar only reaches ports A (9 pins)
# and B (4 pins) -- unlike EFR32 Series 2's full crossbar (six301-pinctrl.h vs.
# efr32mg24.py's _GPIO_MATRIX_PINS). Packed the same way: port = p // 16, pin = p % 16.
_EUSART0_PINS = frozenset([*range(9), *range(16, 20)])

# Registry entries — collected by variants/__init__.py
VARIANT_NAME = ZEPHYR_VARIANT_SIMG301
VARIANT = ZephyrVariant(
    # Resets from the start of flash (simg301_ek2719a.dts boot_partition@0).
    boot=BootLayout(0x0),
    sdk=MAINLINE,
    sdk_name="zephyr",
    # silabs_s3/six301 SoC support merged after v4.4.2 and isn't tagged yet -- 4.4.3
    # isn't a real tag, it just forces `framework: source:` until one ships.
    min_version_override=cv.Version(4, 4, 3),
    family="silabs",
    valid_toolchains=(Toolchain.SDK_ZEPHYR,),
    toolchain="arm-zephyr-eabi",
    # BLE confirmed (bt_hci_silabs enabled upstream, same radio IP as EFR32 Series 2).
    # ieee802154 exists on this SoC too but is unverified on real hardware, not claimed.
    transports=frozenset({"ble"}),
    blobs=("hal_silabs", ".*", ".blobs_hal_silabs_ready"),
    gpio_port_width=16,
    gpio_port_labels=("a", "b", "c", "d"),
    # No scratch partition in the flash layout (boot/image-0/image-1/storage only),
    # same as efr32mg24 -- move/offset need no scratch, "swap" would.
    swap_methods=frozenset({"move", "offset"}),
    # Only eusart0 exists at SoC level, not uart0/uart1 -- DTS discovery must resolve
    # its real label instead of the generic default.
    uart_node_labels={},
    # Series-3 ADC uses a different compatible ("silabs,series3-adc") than EFR32
    # Series 2's IADC -- channel macro naming unconfirmed, left unset rather than guessed.
    adc_ain_map={},
    uart_valid_pins={"tx": _EUSART0_PINS, "rx": _EUSART0_PINS},
    # EUSART0 can also run as an SPI master (Simplicity SDK ships a spidrv_eusart
    # config for this board), but the clk/mosi/miso macros aren't confirmed yet.
    spi_valid_pins={},
)


def config_schema(config: ConfigType) -> ConfigType:
    config = dict(config)
    if CONF_BOARD not in config:
        config[CONF_BOARD] = _DEFAULT_BOARD
    config[CONF_BOARD] = qualify_board(VARIANT, config[CONF_BOARD])
    if CONF_BOARD_SOURCE not in config:
        raise cv.Invalid(
            f"SIMG301's board ({config[CONF_BOARD]!r}) isn't in mainline Zephyr yet -- "
            "see the note at the top of this variant's own source for the tracking "
            f"issue/PR. Set `zephyr: {CONF_BOARD_SOURCE}:` to a tree that has it, e.g. "
            "https://github.com/esphomeplatformzephyr/custom_zephyr_boards.",
            path=[CONF_BOARD_SOURCE],
        )
    _, framework_ver, sdk_name, _ = resolve_framework_version(
        VARIANT,
        "simg301",
        config,
        "silabs_s3/six301 SoC support merged after v4.4.2 -- set framework: source: "
        "until a release ships it",
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
    return config


async def to_code(config: ConfigType) -> None:
    from .. import (
        zephyr_add_prj_conf,
        zephyr_add_sysbuild_conf,
        zephyr_setup_preferences,
    )

    cg.add_build_flag("-DUSE_ZEPHYR_VARIANT_SIMG301")
    cg.add_define("ESPHOME_BOARD", config[CONF_BOARD])
    cg.add_define("ESPHOME_VARIANT", "SIMG301")
    cg.add_define(ThreadModel.SINGLE)
    zephyr_setup_preferences()
    zephyr_add_prj_conf("REBOOT", True)
    zephyr_add_prj_conf("HWINFO", True)

    # simg301_ek2719a.dts hardcodes the MCUboot partition layout unconditionally, same
    # as efr32mg24's xg24_ek2703a -- the app can only ever boot via MCUboot into slot0.
    zephyr_add_sysbuild_conf("BOOTLOADER_MCUBOOT", True)
    zephyr_add_sysbuild_conf("BOOT_SIGNATURE_TYPE_ECDSA_P256", True)
