"""Zephyr variants of the esp32 platform, which hands them to the zephyr component."""

from types import ModuleType

from . import esp32, esp32_c3, esp32_c5, esp32_c6, esp32_h2, esp32_s3

# Variant name -> its module, which exports a ZephyrVariant named VARIANT
VARIANT_MODULES: dict[str, ModuleType] = {
    mod.VARIANT.name: mod
    for mod in (esp32, esp32_h2, esp32_c6, esp32_c5, esp32_c3, esp32_s3)
}
