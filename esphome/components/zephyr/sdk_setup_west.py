"""Install the Zephyr SDK a west workspace needs, for Zephyr toolchain builds."""

from __future__ import annotations

import logging
from pathlib import Path
import time

from esphome.framework_helpers import install_lock

from .framework_west import get_zephyr_tools_path
from .sdk_install import install_sdk

_LOGGER = logging.getLogger(__name__)

# A partial download untouched this long belongs to no running build
_STALE_DOWNLOAD_AGE = 24 * 60 * 60


def _read_sdk_version(framework_path: Path) -> str:
    """Read the required SDK version from the framework's SDK_VERSION file."""
    sdk_version_file = framework_path / "zephyr" / "SDK_VERSION"
    try:
        return sdk_version_file.read_text().strip()
    except OSError as e:
        raise RuntimeError(
            f"Cannot read SDK_VERSION from {sdk_version_file}: {e}"
        ) from e


def _sdk_install_dir(sdk_version: str, toolchain: str) -> Path:
    return (
        get_zephyr_tools_path() / "toolchains" / f"zephyr-sdk-{sdk_version}-{toolchain}"
    )


def _remove_stale_downloads(toolchains_dir: Path) -> None:
    """Best-effort removal of partial downloads no build has written to for a day.

    These are left by interrupted downloads of SDK versions no longer built; the
    archives are hundreds of MB. A running download keeps its file fresh.
    """
    cutoff = time.time() - _STALE_DOWNLOAD_AGE
    for leftover in toolchains_dir.glob("*.archive.part*"):
        try:
            if leftover.stat().st_mtime < cutoff:
                leftover.unlink()
        except OSError as err:
            _LOGGER.debug("Could not remove %s: %s", leftover, err)


def check_and_install(framework_path: Path, toolchain: str) -> Path:
    """Ensure the Zephyr SDK with ``toolchain`` is installed.

    Args:
        framework_path: Path to the West workspace root.
        toolchain: Zephyr SDK toolchain name to install (e.g. ``"riscv64-zephyr-elf"``).

    Returns the SDK install dir, one per SDK version and toolchain.
    """
    sdk_version = _read_sdk_version(framework_path)
    sdk_path = _sdk_install_dir(sdk_version, toolchain)
    sentinel = sdk_path / ".ready"
    if sentinel.exists():
        return sdk_path
    with install_lock(get_zephyr_tools_path(), f"toolchain-{sdk_path.name}"):
        if not sentinel.exists():  # another build may have installed it meanwhile
            install_sdk(sdk_path, sdk_version, toolchain)
            _remove_stale_downloads(sdk_path.parent)
            sentinel.touch()
    return sdk_path
