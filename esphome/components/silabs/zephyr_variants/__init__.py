"""Zephyr variants of the silabs platform; the zephyr component loads them."""

# Modules here that define a variant. Each exports VARIANT_NAME and a ZephyrVariant
# named VARIANT; a family's module is <family>_family.
VARIANT_MODULES = [
    "efr32mg24",
    "simg301",
    "efr32bg27",
    "siwx917",
]
