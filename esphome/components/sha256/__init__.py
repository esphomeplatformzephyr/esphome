import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.core import CORE
from esphome.helpers import IS_MACOS
from esphome.types import ConfigType

CODEOWNERS = ["@esphome/core"]

sha256_ns = cg.esphome_ns.namespace("sha256")

CONFIG_SCHEMA = cv.Schema({})


async def to_code(config: ConfigType) -> None:
    cg.add_define("USE_SHA256")

    if CORE.is_zephyr:
        from esphome.components.zephyr import zephyr_add_prj_conf

        # All platform:zephyr variants (native_sim/esp32_h2 >= 4.4.0) are above
        # the PSA crypto threshold.
        zephyr_add_prj_conf("PSA_CRYPTO", True)
        zephyr_add_prj_conf("PSA_WANT_ALG_SHA_256", True)
        if not CORE.is_nrf52:
            # NCS's nrf_security (used on nrf52) implements the PSA Crypto
            # API directly -- see sha256.h's USE_ZEPHYR_VARIANT_NRF52 branch.
            # This Kconfig only matters for mainline Zephyr's own mbedtls
            # module (the legacy compat header used on other variants).
            zephyr_add_prj_conf("MBEDTLS_MD_C", True)
        return

    # Add OpenSSL library for host platform (Zephyr uses PSA crypto, not OpenSSL)
    if not CORE.is_host:
        return
    if IS_MACOS:
        # macOS needs special handling for Homebrew OpenSSL
        cg.add_build_flag("-I/opt/homebrew/opt/openssl/include")
        cg.add_build_flag("-L/opt/homebrew/opt/openssl/lib")
    cg.add_build_flag("-lcrypto")
