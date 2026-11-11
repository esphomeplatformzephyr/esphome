"""Unit tests for esphome.components.zephyr.sdk_setup_west."""

from __future__ import annotations

from collections.abc import Iterator
import os
from pathlib import Path
import time
from typing import Any
from unittest.mock import MagicMock, patch

import platformdirs
import pytest

from esphome.components.zephyr import sdk_install
from esphome.components.zephyr.sdk_setup_west import _sdk_install_dir, check_and_install

TOOLCHAIN = "riscv64-zephyr-elf"


@pytest.fixture(autouse=True)
def _isolate_zephyr_tools_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test out of the real ~/.cache/esphome."""
    monkeypatch.setenv("ESPHOME_ZEPHYR_PREFIX", str(tmp_path / "cache"))


# ---------------------------------------------------------------------------
# _sdk_install_dir -- machine-global cache location
# ---------------------------------------------------------------------------


def test_sdk_install_dir_env_override(tmp_path: Path) -> None:
    assert (
        _sdk_install_dir("1.0.1", TOOLCHAIN)
        == (tmp_path / "cache").resolve()
        / "toolchains"
        / f"zephyr-sdk-1.0.1-{TOOLCHAIN}"
    )


def test_sdk_install_dir_default_is_global_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ESPHOME_ZEPHYR_PREFIX", raising=False)
    expected = (
        Path(platformdirs.user_cache_dir("esphome", appauthor=False))
        / "zephyr"
        / "toolchains"
        / f"zephyr-sdk-1.0.1-{TOOLCHAIN}"
    ).resolve()
    assert _sdk_install_dir("1.0.1", TOOLCHAIN) == expected


# ---------------------------------------------------------------------------
# check_and_install
# ---------------------------------------------------------------------------


def _make_framework(tmp_path: Path, sdk_version: str = "1.0.1") -> Path:
    framework = tmp_path / "framework"
    (framework / "zephyr").mkdir(parents=True)
    (framework / "zephyr" / "SDK_VERSION").write_text(sdk_version)
    return framework


@pytest.fixture
def mock_download() -> Iterator[MagicMock]:
    """download_and_extract() as seen by the installer; it creates the extract dir."""

    def _extract(*args: Any, **kwargs: Any) -> str:
        Path(args[3]).mkdir(parents=True, exist_ok=True)
        return "https://example.com/archive"

    with (
        patch.object(sdk_install, "download_and_extract", side_effect=_extract) as m,
        patch.object(
            sdk_install,
            "get_sdk_host_info",
            return_value=("linux", "x86_64", "tar.xz"),
        ),
    ):
        yield m


def test_check_and_install_downloads_minimal_and_toolchain(
    tmp_path: Path, mock_download: MagicMock
) -> None:
    framework = _make_framework(tmp_path)
    sdk_path = _sdk_install_dir("1.0.1", TOOLCHAIN)

    assert check_and_install(framework, toolchain=TOOLCHAIN) == sdk_path

    (minimal, toolchain) = mock_download.call_args_list
    assert minimal.args[0] == sdk_install.SDK_NG_MINIMAL_MIRRORS
    assert minimal.args[2] == sdk_path.with_name(f"{sdk_path.name}.minimal.archive")
    assert minimal.args[3] == sdk_path
    assert toolchain.args[0] == sdk_install.SDK_NG_TOOLCHAIN_GNU_MIRRORS
    assert toolchain.args[1] == {
        "VERSION": "1.0.1",
        "sysname": "linux",
        "machine": "x86_64",
        "extension": "tar.xz",
        "toolchain": TOOLCHAIN,
    }
    assert toolchain.args[3] == sdk_path / "gnu" / TOOLCHAIN
    assert (sdk_path / ".ready").exists()


def test_check_and_install_skips_when_ready(
    tmp_path: Path, mock_download: MagicMock
) -> None:
    framework = _make_framework(tmp_path)
    sdk_path = _sdk_install_dir("1.0.1", TOOLCHAIN)
    sdk_path.mkdir(parents=True)
    (sdk_path / ".ready").touch()

    with patch("filelock.FileLock") as file_lock:
        check_and_install(framework, toolchain=TOOLCHAIN)

    file_lock.assert_not_called()
    mock_download.assert_not_called()


def test_check_and_install_locks_per_sdk_and_toolchain(
    tmp_path: Path, mock_download: MagicMock
) -> None:
    framework = _make_framework(tmp_path)

    with patch("filelock.FileLock") as file_lock:
        check_and_install(framework, toolchain=TOOLCHAIN)

    lock_files = [Path(c.args[0]).name for c in file_lock.call_args_list]
    assert lock_files == [f"toolchain-zephyr-sdk-1.0.1-{TOOLCHAIN}.lock"]


def test_check_and_install_skips_install_finished_while_waiting(
    tmp_path: Path, mock_download: MagicMock
) -> None:
    framework = _make_framework(tmp_path)
    sdk_path = _sdk_install_dir("1.0.1", TOOLCHAIN)

    def _other_build_installs(*args: Any, **kwargs: Any) -> MagicMock:
        sdk_path.mkdir(parents=True, exist_ok=True)
        (sdk_path / ".ready").touch()
        return MagicMock()

    with patch("filelock.FileLock", side_effect=_other_build_installs):
        assert check_and_install(framework, toolchain=TOOLCHAIN) == sdk_path

    mock_download.assert_not_called()


def test_check_and_install_removes_an_unfinished_install(
    tmp_path: Path, mock_download: MagicMock
) -> None:
    """A half-extracted toolchain without .ready is removed and installed again."""
    framework = _make_framework(tmp_path)
    sdk_path = _sdk_install_dir("1.0.1", TOOLCHAIN)
    leftover = sdk_path / "gnu" / TOOLCHAIN / "half-extracted"
    leftover.parent.mkdir(parents=True)
    leftover.touch()

    check_and_install(framework, toolchain=TOOLCHAIN)

    assert not leftover.exists()
    assert mock_download.call_count == 2
    assert (sdk_path / ".ready").exists()


def test_check_and_install_no_marker_on_extract_failure(
    tmp_path: Path, mock_download: MagicMock
) -> None:
    framework = _make_framework(tmp_path)
    sdk_path = _sdk_install_dir("1.0.1", TOOLCHAIN)
    mock_download.side_effect = ValueError("Unsupported archive format")

    with pytest.raises(ValueError, match="Unsupported archive format"):
        check_and_install(framework, toolchain=TOOLCHAIN)

    assert not (sdk_path / ".ready").exists()


def test_check_and_install_raises_without_sdk_version(tmp_path: Path) -> None:
    """A workspace with no SDK_VERSION file names the file it could not read."""
    with pytest.raises(RuntimeError, match="Cannot read SDK_VERSION"):
        check_and_install(tmp_path, toolchain=TOOLCHAIN)


def test_check_and_install_removes_only_stale_partial_downloads(
    tmp_path: Path, mock_download: MagicMock
) -> None:
    """Partial downloads untouched for a day go; a fresh one may belong to a running build."""
    framework = _make_framework(tmp_path)
    toolchains = _sdk_install_dir("1.0.1", TOOLCHAIN).parent
    toolchains.mkdir(parents=True)
    stale = toolchains / "zephyr-sdk-0.17.4-arm-zephyr-eabi.toolchain.archive.part"
    fresh = toolchains / "zephyr-sdk-1.0.2-arm-zephyr-eabi.toolchain.archive.part"
    for leftover in (stale, fresh):
        leftover.write_text("")
    two_days_ago = time.time() - 2 * 24 * 60 * 60
    os.utime(stale, (two_days_ago, two_days_ago))

    check_and_install(framework, toolchain=TOOLCHAIN)

    assert not stale.exists()
    assert fresh.exists()


def test_check_and_install_keeps_going_when_a_stale_download_is_locked(
    tmp_path: Path, mock_download: MagicMock
) -> None:
    framework = _make_framework(tmp_path)
    toolchains = _sdk_install_dir("1.0.1", TOOLCHAIN).parent
    toolchains.mkdir(parents=True)
    stale = toolchains / "zephyr-sdk-0.17.4-arm-zephyr-eabi.toolchain.archive.part"
    stale.write_text("")
    os.utime(stale, (0, 0))

    with patch.object(Path, "unlink", side_effect=OSError("in use")):
        check_and_install(framework, toolchain=TOOLCHAIN)

    assert (_sdk_install_dir("1.0.1", TOOLCHAIN) / ".ready").exists()
