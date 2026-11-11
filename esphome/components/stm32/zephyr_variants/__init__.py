"""Zephyr variants of the stm32 platform; the zephyr component loads them."""

# Modules here that define a variant. Each exports VARIANT_NAME and a ZephyrVariant
# named VARIANT; a family's module is <family>_family.
VARIANT_MODULES = [
    "stm32l4",
    "stm32f4",
    "stm32wb55",
    "stm32f1",
    "stm32u5",
    "stm32c5",
]
