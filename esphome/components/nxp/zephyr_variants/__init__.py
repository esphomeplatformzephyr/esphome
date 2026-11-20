"""Zephyr variants of the nxp platform, which hands them to the zephyr component."""

from types import ModuleType

from . import mcxn236

# Variant name -> its module, which exports a ZephyrVariant named VARIANT
VARIANT_MODULES: dict[str, ModuleType] = {mod.VARIANT.name: mod for mod in (mcxn236,)}
