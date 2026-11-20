"""Zephyr variants of the rp2 platform, which hands them to the zephyr component."""

from types import ModuleType

from . import rp2040, rp2350

# Variant name -> its module, which exports VARIANT_NAME and a ZephyrVariant named VARIANT
VARIANT_MODULES: dict[str, ModuleType] = {
    mod.VARIANT_NAME: mod for mod in (rp2040, rp2350)
}
