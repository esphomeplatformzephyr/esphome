"""Zephyr variants of the silabs platform, which hands them to the zephyr component."""

from types import ModuleType

from . import efr32bg27, efr32mg24, simg301, siwx917

# Variant name -> its module, which exports a ZephyrVariant named VARIANT
VARIANT_MODULES: dict[str, ModuleType] = {
    mod.VARIANT.name: mod for mod in (efr32mg24, simg301, efr32bg27, siwx917)
}
