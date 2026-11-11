"""Zephyr variants of the rp2 platform; the zephyr component loads them."""

# Modules here that define a variant. Each exports VARIANT_NAME and a ZephyrVariant
# named VARIANT; a family's module is <family>_family.
VARIANT_MODULES = [
    "rp2040",
    "rp2350",
]
