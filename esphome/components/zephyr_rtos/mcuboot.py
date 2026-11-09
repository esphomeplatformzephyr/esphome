"""MCUboot swap-method configuration, shared by every ``ota:`` platform that can run on Zephyr.

The MCUboot swap mode is a sysbuild-level choice, not scoped to whichever ota:
platform happens to set it -- platform: esphome and platform: zephyr_mcumgr both end
up pushing the very same MCUBOOT_MODE_SWAP_* sysbuild Kconfig symbol, so the
per-variant validation and the code that applies it live here once instead of
being duplicated per platform. The swap_method: schema itself lives in
esphome.components.ota instead (see CONF_SWAP_METHOD/SWAP_METHOD_SCHEMA there):
it has no zephyr dependency, and platform: esphome needs it at module-load
time on every platform, not just Zephyr.
"""

import logging
from pathlib import Path

from esphome.components.ota import CONF_SWAP_METHOD, reject_swap_method_off_zephyr
import esphome.config_validation as cv
from esphome.const import CONF_OTA
from esphome.core import CORE, EsphomeError
from esphome.types import ConfigType

_LOGGER = logging.getLogger(__name__)

_SYSBUILD_MODE = {
    "scratch": "MCUBOOT_MODE_SWAP_SCRATCH",
    "move": "MCUBOOT_MODE_SWAP_USING_MOVE",
    "offset": "MCUBOOT_MODE_SWAP_USING_OFFSET",
}
# The Kconfig symbol each swap method sets in MCUboot's own .config
_MCUBOOT_KCONFIG_METHOD = {
    "CONFIG_BOOT_SWAP_USING_SCRATCH": "scratch",
    "CONFIG_BOOT_SWAP_USING_MOVE": "move",
    "CONFIG_BOOT_SWAP_USING_OFFSET": "offset",
}

# MCUboot's image trailer (bootutil_area.h/.c, bootutil_public.h, mcuboot_config.h)
_BOOT_MAGIC_SZ = 16
_BOOT_DEFAULT_MAX_ALIGN = 8
_BOOT_DEFAULT_MAX_IMG_SECTORS = 128
_BOOT_STATUS_STATE_COUNT = {"scratch": 3, "move": 3, "offset": 2}


def validate_swap_method(config: ConfigType) -> ConfigType:
    """Reject a swap_method the current variant's port doesn't support."""
    if not CORE.using_toolchain_zephyr:
        return reject_swap_method_off_zephyr(config)
    from . import zephyr_variant
    from .variants import VARIANTS

    variant = zephyr_variant()
    if variant is None:
        return config
    allowed = VARIANTS[variant].swap_methods
    method = config[CONF_SWAP_METHOD]
    if method not in allowed:
        raise cv.Invalid(
            f"'{CONF_SWAP_METHOD}: {method}' is not supported on this variant; "
            f"choose one of {sorted(allowed)}"
        )
    return config


def apply_swap_method(config: ConfigType) -> None:
    """Push the configured MCUboot swap method to sysbuild.conf."""
    if not CORE.using_toolchain_zephyr:
        return
    from . import zephyr_add_sysbuild_conf

    method = config[CONF_SWAP_METHOD]
    # A sysbuild-level choice, not per-image prj.conf: the default MCUBOOT_MODE
    # is OVERWRITE_ONLY for the whole family, which silently defeats
    # boot_request_upgrade(BOOT_UPGRADE_TEST) (no image survives to revert to).
    zephyr_add_sysbuild_conf(_SYSBUILD_MODE[method], True)


def zephyr_skip_bootloader_supported() -> bool:
    """True when MCUboot is in the build: only with ota:, and OTA never sends it."""
    return CORE.using_toolchain_zephyr and bool((CORE.config or {}).get(CONF_OTA))


def apply_single_slot() -> None:
    """Push framework: single_slot: true to sysbuild.conf.

    Called directly from zephyr_to_code(), independent of whether any ota:
    platform is configured -- unlike apply_swap_method(), single_slot: has no
    ota: dependency at all (see ota/__init__.py's _ota_final_validate, which
    rejects any ota: platform when single_slot: true is set).
    """
    from . import zephyr_add_sysbuild_conf

    zephyr_add_sysbuild_conf("BOOTLOADER_MCUBOOT", True)
    zephyr_add_sysbuild_conf("MCUBOOT_MODE_SINGLE_APP", True)


def _align_up(value: int, align: int) -> int:
    return -(-value // align) * align


def _mcuboot_swap_limit(
    method: str,
    slot0: int,
    slot1: int,
    sector: int,
    write_size: int,
    max_sectors: int,
    max_align: int,
    scratch: int | None = None,
) -> int:
    """The largest image MCUboot can swap in.

    Mirrors app_max_size() in MCUboot's swap_move.c, swap_offset.c and swap_scratch.c
    for slots on one flash with a single sector size.
    """
    trailer_info = (
        4 * max_align  # swap_type, copy_done, image_ok, swap_size
        + (max_align if method == "offset" else 0)  # TLV size for both slots
        + _align_up(_BOOT_MAGIC_SZ, max_align)
    )
    status_entry = _BOOT_STATUS_STATE_COUNT[method] * write_size
    trailer = max_sectors * status_entry + trailer_info
    sectors0 = slot0 // sector
    sectors1 = slot1 // sector
    if method in ("move", "offset"):
        trailer = _align_up(trailer, sector)
        # The padding sector the swap needs is in the primary slot for move, the secondary for offset
        primary = sectors0 * sector - trailer - (sector if method == "move" else 0)
        secondary = sectors1 * sector - trailer - (sector if method == "offset" else 0)
        return min(primary, secondary)
    if scratch is None or scratch < sector:
        return 0  # "Cannot upgrade: not all sectors fit inside scratch"
    # app_max_size_adjust_to_trailer(): the scratch trailer must fit in the first
    # sector holding the slot trailer when that sector goes through scratch
    trailer_off = min(sectors0, sectors1) * sector - trailer
    trailer_sectors = max(1, -(-trailer // sector))
    first_trailer_sector_end = (max(sectors0, sectors1) - trailer_sectors + 1) * sector
    trailer_in_first_sector = first_trailer_sector_end - trailer_off
    padding = max(0, status_entry + trailer_info - trailer_in_first_sector)
    return trailer_off - padding


def _read_kconfig(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("CONFIG_") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def _read_slot_layout(mcuboot_dir: Path) -> dict[str, int] | None:
    """Slot, scratch, sector and write sizes from MCUboot's own devicetree."""
    import pickle

    from .dts_lookup import _load_edtlib

    cache = mcuboot_dir / "CMakeCache.txt"
    edt_pickle = mcuboot_dir / "zephyr" / "edt.pickle"
    if not (cache.is_file() and edt_pickle.is_file()):
        _LOGGER.warning(
            "Skipping the MCUboot slot size check: %s or %s is missing",
            cache,
            edt_pickle,
        )
        return None
    zephyr_base = next(
        (
            Path(line.split("=", 1)[1])
            for line in cache.read_text(encoding="utf-8").splitlines()
            if line.startswith("ZEPHYR_BASE:")
        ),
        None,
    )
    # edt.pickle holds edtlib objects, so the build's own edtlib must be importable
    if _load_edtlib(zephyr_base) is None:
        _LOGGER.warning("Skipping the MCUboot slot size check: edtlib is not available")
        return None
    try:
        with edt_pickle.open("rb") as f:
            # Written by this build's own west build
            edt = pickle.load(f)
    except Exception as err:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        _LOGGER.warning("Skipping the MCUboot slot size check: %s: %s", edt_pickle, err)
        return None

    slots = [edt.label2node.get(f"slot{index}_partition") for index in (0, 1)]
    if None in slots:
        _LOGGER.warning(
            "Skipping the MCUboot slot size check: no slot0/slot1 partition"
        )
        return None
    flash = slots[0].parent.parent
    erase = flash.props.get("erase-block-size")
    write = flash.props.get("write-block-size")
    if erase is None or write is None:
        _LOGGER.warning(
            "Skipping the MCUboot slot size check: %s has no erase-block-size or "
            "write-block-size",
            flash.path,
        )
        return None
    scratch = edt.label2node.get("scratch_partition")
    return {
        "slot0": slots[0].regs[0].size,
        "slot1": slots[1].regs[0].size,
        "sector": erase.val,
        "write_size": write.val,
        "scratch": scratch.regs[0].size if scratch is not None else None,
    }


def check_mcuboot_swap_limit(build_dir: Path, image: Path) -> None:
    """Fail a build whose image MCUboot would refuse to swap in.

    Zephyr's signing only checks the image and a 128-sector trailer against slot 1.
    MCUboot then boots the old image and leaves the new one pending, which blocks
    every later OTA update.
    """
    mcuboot_dir = build_dir / "mcuboot"
    config_path = mcuboot_dir / "zephyr" / ".config"
    if not config_path.is_file():
        return  # no MCUboot in this build
    config = _read_kconfig(config_path)
    method = next(
        (
            m
            for symbol, m in _MCUBOOT_KCONFIG_METHOD.items()
            if config.get(symbol) == "y"
        ),
        None,
    )
    if method is None:
        return  # single slot or overwrite only: nothing is swapped
    if not image.is_file():
        # A stale image from an earlier build could otherwise go out unnoticed
        _LOGGER.warning("Skipping the MCUboot slot size check: %s is missing", image)
        return
    layout = _read_slot_layout(mcuboot_dir)
    if layout is None:
        return

    if config.get("CONFIG_BOOT_MAX_IMG_SECTORS_AUTO") == "y":
        # MCUboot's CMakeLists.txt: the larger slot's sector count
        max_sectors = max(layout["slot0"], layout["slot1"]) // layout["sector"]
    else:
        max_sectors = int(
            config.get("CONFIG_BOOT_MAX_IMG_SECTORS", _BOOT_DEFAULT_MAX_IMG_SECTORS)
        )
    max_align = max(
        int(config.get("CONFIG_MCUBOOT_BOOT_MAX_ALIGN", 0)), _BOOT_DEFAULT_MAX_ALIGN
    )
    limit = _mcuboot_swap_limit(
        method, max_sectors=max_sectors, max_align=max_align, **layout
    )
    size = image.stat().st_size
    if size > limit:
        # As nrf52, which never copies an oversized image: nothing to send by mistake
        image.unlink()
        raise EsphomeError(
            f"The application image is {size} bytes, but MCUboot can only swap in "
            f"{limit} bytes on this board. Remove at least {size - limit} bytes, for "
            "example by leaving out components or lowering the log level."
        )
    _LOGGER.info("Application image: %d of %d bytes MCUboot can swap in", size, limit)
