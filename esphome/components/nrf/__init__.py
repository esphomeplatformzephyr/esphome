"""Nordic nRF chips."""

from esphome.const import PLATFORM_NRF
from esphome.core import CORE, CoroPriority, coroutine_with_priority
from esphome.schema_extractors import SCHEMA_EXTRACT, schema_extractor
from esphome.storage_json import StorageJSON
from esphome.types import ConfigType

# force import gpio to register pin schema
from .gpio import nrf_pin_to_code  # noqa: F401

CODEOWNERS = ["@rwrozelle"]
DOMAIN = "nrf"
IS_TARGET_PLATFORM = True
# The variant families zephyr builds for this platform
ZEPHYR_FAMILIES = frozenset({"nordic"})

# Zephyr is the only framework so far. Another one is chosen in CONFIG_SCHEMA by
# framework: type:, and the functions below branch on CORE.toolchain, as rp2 does.


def AUTO_LOAD() -> list[str]:
    if CORE.using_toolchain_zephyr:
        return ["zephyr", "preferences"]
    return ["preferences"]


@schema_extractor("schema")
def CONFIG_SCHEMA(config: ConfigType) -> ConfigType:
    from esphome.components.zephyr import zephyr_config_schema, zephyr_toolchain_schema

    if config is SCHEMA_EXTRACT:
        return zephyr_toolchain_schema(ZEPHYR_FAMILIES)
    return zephyr_config_schema(config, ZEPHYR_FAMILIES, PLATFORM_NRF)


@coroutine_with_priority(CoroPriority.PLATFORM)
async def to_code(config: ConfigType) -> None:
    if CORE.using_toolchain_zephyr:
        from esphome.components import zephyr

        await zephyr.zephyr_toolchain_to_code(config)


# Called by writer.py
def copy_files() -> None:
    if CORE.using_toolchain_zephyr:
        from esphome.components import zephyr

        zephyr.zephyr_toolchain_copy_files()


def get_download_types(storage_json: StorageJSON) -> list[dict[str, str]]:
    # Zephyr is the only framework so far, so a sidecar that records none is Zephyr
    if storage_json.framework in (None, "zephyr"):
        from esphome.components import zephyr

        return zephyr.get_download_types(storage_json)
    return []


def run_compile(args, config: ConfigType) -> bool:
    if not CORE.using_toolchain_zephyr:
        return False
    from esphome.components import zephyr

    return zephyr.run_compile(args, config)


def upload_program(config: ConfigType, args, host: str) -> bool:
    if not CORE.using_toolchain_zephyr:
        return False
    from esphome.components import zephyr

    return zephyr.upload_program(config, args, host)


def process_stacktrace(config: ConfigType, line: str, backtrace_state: bool) -> bool:
    if not CORE.using_toolchain_zephyr:
        return False
    from esphome.components import zephyr

    return zephyr.process_stacktrace(config, line, backtrace_state)


def show_logs(config: ConfigType, args, devices: list[str]) -> bool:
    if not CORE.using_toolchain_zephyr:
        return False
    from esphome.components import zephyr

    return zephyr.show_logs(config, args, devices)


def has_network_ota(config: ConfigType) -> bool:
    if not CORE.using_toolchain_zephyr:
        return False
    from esphome.components import zephyr

    return zephyr.has_network_ota(config)
