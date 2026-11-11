"""Zephyr variants of the esp32 platform; the zephyr component loads them."""

# Modules here that define a variant. Each exports VARIANT_NAME and a ZephyrVariant
# named VARIANT; a family's module is <family>_family.
VARIANT_MODULES = [
    "esp32",
    "esp32_h2",
    "esp32_c6",
    "esp32_c5",
    "esp32_c3",
    "esp32_s3",
]
