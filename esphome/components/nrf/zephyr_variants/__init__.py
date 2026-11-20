"""Zephyr variants of the nrf platform, which hands them to the zephyr component."""

from types import ModuleType

from . import nrf52, nrf54l15, nrf54lm20a, nrf5340

# Variant name -> its module, which exports a ZephyrVariant named VARIANT
VARIANT_MODULES: dict[str, ModuleType] = {
    mod.VARIANT.name: mod for mod in (nrf52, nrf5340, nrf54l15, nrf54lm20a)
}
