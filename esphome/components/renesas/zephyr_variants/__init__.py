"""Zephyr variants of the renesas platform, which hands them to the zephyr component."""

from types import ModuleType

from . import ra4m1

# Variant name -> its module, which exports a ZephyrVariant named VARIANT
VARIANT_MODULES: dict[str, ModuleType] = {mod.VARIANT.name: mod for mod in (ra4m1,)}
