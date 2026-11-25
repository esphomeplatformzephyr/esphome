"""Code shared by the STM32 variants."""

import esphome.codegen as cg
from esphome.components.zephyr import zephyr_add_prj_conf, zephyr_variant_name
from esphome.components.zephyr.const import SEGGER_RTT_UART
from esphome.components.zephyr.variants import mcuboot_or_none
from esphome.const import CONF_FRAMEWORK, CONF_LOG_LEVEL
from esphome.types import ConfigType

from ..const import VARIANT_STM32C5, VARIANT_STM32F1, VARIANT_STM32F4

FAMILY = "stm32"

# Software UART drivers (devicetree compatible strings) that work on this family,
# on top of its hardware UARTs.
UART_DRIVERS: frozenset[str] = frozenset({SEGGER_RTT_UART})

# Vendor HAL west projects this family builds against on mainline Zephyr; the other
# vendor HALs are not fetched.
ADDITIONAL_FRAMEWORK_PROJECTS: frozenset[str] = frozenset({"hal_stm32"})
# CPU architecture (see ZephyrVariant.cpu_arch)
CPU_ARCH = "arm"
# Kind of USB UART (see ZephyrVariant.usb_uart_type)
USB_UART_TYPE = "cdc_acm"
# See ZephyrVariant.has_poweroff
HAS_POWEROFF: bool = True
# Board devicetrees #include from this HAL, so the devicetree fetch needs it
DTS_HAL_MODULES: tuple[str, ...] = ("hal_stm32",)


def to_code(config: ConfigType) -> None:
    """REQUIRES_FULL_LIBCPP selects the GNU C++ library; Zephyr's default minimal
    library has no STL, which ESPHome's headers need."""
    zephyr_add_prj_conf("CPP", True)
    zephyr_add_prj_conf("REQUIRES_FULL_LIBCPP", True)
    # Off by default, which leaves floating point in software
    # STM32F1 is Cortex-M3, which has no FPU.
    if zephyr_variant_name() != VARIANT_STM32F1:
        zephyr_add_prj_conf("FPU", True)
    # random_bytes() uses sys_rand_get(), which requires the entropy subsystem.
    # STM32F4 is a whole chip family, not a single SoC -- RNG presence varies per
    # member (F401/F411 have none, F405/F410/F412 and larger do), so stm32f4.py
    # resolves this itself from the board's own DTS instead of a blanket default.
    # STM32F1 has no true RNG on any family member (dts/arm/st/f1 has no rng@ node
    # at all), so stm32f1.py always uses TEST_RANDOM_GENERATOR unconditionally.
    # STM32C5's SoC dtsi ships its RNG disabled; stm32c5.py enables it per board.
    if zephyr_variant_name() not in (
        VARIANT_STM32C5,
        VARIANT_STM32F1,
        VARIANT_STM32F4,
    ):
        zephyr_add_prj_conf("ENTROPY_GENERATOR", True)
    # ARM Cortex-M's ARCH_HAS_STACKWALK only defaults on when EXTRA_EXCEPTION_INFO
    # is also set (arch/arm/core/Kconfig selects the dependency it needs).
    log_level = config[CONF_FRAMEWORK][CONF_LOG_LEVEL]
    if log_level != "NONE":
        zephyr_add_prj_conf("EXTRA_EXCEPTION_INFO", True)
        zephyr_add_prj_conf("EXCEPTION_STACK_TRACE", True)
    # Consumed by C++ code shared across every stm32-family variant (core.cpp, etc.).
    cg.add_build_flag("-DUSE_ZEPHYR_VARIANT_FAMILY_STM32")


def bootloader(advanced: ConfigType) -> str:
    """Return the bootloader set_variant_core_data() gets, rebuilt from the cached advanced: block."""
    return mcuboot_or_none(advanced)
