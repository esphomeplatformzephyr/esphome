"""Zephyr variants of the nrf platform; the zephyr component loads them."""

# Modules here that define a variant. Each exports VARIANT_NAME and a ZephyrVariant
# named VARIANT; a family's module is <family>_family.
VARIANT_MODULES = [
    "nrf52",
    "nrf5340",
    "nrf54l15",
    "nrf54lm20a",
]
