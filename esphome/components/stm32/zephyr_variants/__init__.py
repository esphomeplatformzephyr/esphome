"""Zephyr variants of the stm32 platform, which hands them to the zephyr component."""

from types import ModuleType

from . import stm32c5, stm32f1, stm32f4, stm32l4, stm32u5, stm32wb55

# Variant name -> its module, which exports a ZephyrVariant named VARIANT
VARIANT_MODULES: dict[str, ModuleType] = {
    mod.VARIANT.name: mod
    for mod in (stm32l4, stm32f4, stm32wb55, stm32f1, stm32u5, stm32c5)
}
