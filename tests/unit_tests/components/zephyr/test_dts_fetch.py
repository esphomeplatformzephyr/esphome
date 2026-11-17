"""Unit tests for esphome.components.zephyr.dts_fetch."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import nullcontext
import os
from pathlib import Path
import subprocess
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from esphome.components.zephyr.const import KEY_SDK_SOURCE_RESOLVED_REF
from esphome.components.zephyr.dts_fetch import (
    _parse_zephyr_version_file,
    _resolve_git_head,
    _sdk_source_cache_key,
    _sdk_source_version_cache_root,
    resolve_sdk_source_version,
)
import esphome.config_validation as cv
from esphome.core import TimePeriodSeconds


@pytest.fixture(autouse=True)
def install_lock() -> Iterator[MagicMock]:
    """Keep the cache's lock file out of the real tools cache."""
    with patch(
        "esphome.components.zephyr.dts_fetch.install_lock",
        side_effect=lambda root, name: nullcontext(),
    ) as lock:
        yield lock


_FAKE_RESOLVED_SHA: str = "a" * 40

# ---------------------------------------------------------------------------
# _resolve_git_head / resolve_sdk_source_version -- single ref resolution
# (resolve_sdk_source_version() resolves a moving git ref once and stashes it onto
# source[KEY_SDK_SOURCE_RESOLVED_REF] for the SDK install to reuse)
# ---------------------------------------------------------------------------


def test_resolve_git_head_returns_rev_parse_output(tmp_path: Path) -> None:
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            [], 0, stdout=f"{_FAKE_RESOLVED_SHA}\n", stderr=""
        )
        assert _resolve_git_head(tmp_path) == _FAKE_RESOLVED_SHA
    mock_run.assert_called_once_with(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )


def test_resolve_git_head_raises_cv_invalid_on_git_failure(tmp_path: Path) -> None:
    with (
        patch(
            "subprocess.run",
            side_effect=subprocess.CalledProcessError(
                1, ["git"], stderr="not a git repository"
            ),
        ),
        pytest.raises(cv.Invalid, match="Can't resolve sdk_source commit"),
    ):
        _resolve_git_head(tmp_path)


def test_resolve_sdk_source_version_sets_resolved_ref_on_cache_hit(
    tmp_path: Path,
) -> None:
    """Even when the cached VERSION file is fresh enough to skip re-cloning,
    the resolved commit must still be captured from the existing checkout --
    it's the single value the other two consumers pin to for this run."""
    source = {"type": "git", "url": "https://example.invalid/zephyr", "ref": "main"}
    dest = tmp_path / _sdk_source_cache_key(source["url"], source["ref"])
    dest.mkdir(parents=True)
    (dest / "VERSION").write_text(
        "VERSION_MAJOR = 4\nVERSION_MINOR = 4\nPATCHLEVEL = 1\n"
    )

    with (
        patch(
            "esphome.components.zephyr.dts_fetch._sdk_source_version_cache_root",
            return_value=tmp_path,
        ),
        patch("subprocess.run") as mock_run,
    ):
        mock_run.return_value = subprocess.CompletedProcess(
            [], 0, stdout=f"{_FAKE_RESOLVED_SHA}\n", stderr=""
        )
        version = resolve_sdk_source_version(source, refresh=None)

    assert version == "4.4.1"
    assert source[KEY_SDK_SOURCE_RESOLVED_REF] == _FAKE_RESOLVED_SHA
    # No clone attempted -- only the rev-parse for the resolved ref.
    mock_run.assert_called_once()
    assert mock_run.call_args.args[0][-2:] == ["rev-parse", "HEAD"]


_VERSION_441 = "VERSION_MAJOR = 4\nVERSION_MINOR = 4\nPATCHLEVEL = 1\n"
_VERSION_442 = "VERSION_MAJOR = 4\nVERSION_MINOR = 4\nPATCHLEVEL = 2\n"
_SOURCE = {"type": "git", "url": "https://example.invalid/zephyr", "ref": "main"}


def _stale_cache(tmp_path: Path) -> Path:
    """A cached sdk_source clone whose VERSION is older than any refresh interval."""
    dest = tmp_path / _sdk_source_cache_key(_SOURCE["url"], _SOURCE["ref"])
    (dest / ".git").mkdir(parents=True)
    (dest / "VERSION").write_text(_VERSION_441)
    os.utime(dest / "VERSION", (0, 0))
    return dest


def _resolve_with(
    tmp_path: Path, git: Callable[[list[str]], None]
) -> tuple[str, list[list[str]]]:
    """Run resolve_sdk_source_version with `git(cmd)` handling every git call except
    rev-parse; return the version and the commands run."""
    calls: list[list[str]] = []

    def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if "rev-parse" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=f"{_FAKE_RESOLVED_SHA}\n", stderr=""
            )
        git(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with (
        patch(
            "esphome.components.zephyr.dts_fetch._sdk_source_version_cache_root",
            return_value=tmp_path,
        ),
        patch("subprocess.run", side_effect=run),
    ):
        version = resolve_sdk_source_version(
            dict(_SOURCE), refresh=TimePeriodSeconds(seconds=1)
        )
    return version, calls


def _offline(cmd: list[str]) -> None:
    raise subprocess.CalledProcessError(
        128, cmd, stderr="Cloning...\nfatal: unable to access: Could not resolve host"
    )


def test_resolve_sdk_source_version_refresh_updates_cache_in_place(
    tmp_path: Path,
) -> None:
    dest = _stale_cache(tmp_path)

    def git(cmd: list[str]) -> None:
        if "reset" in cmd:
            (dest / "VERSION").write_text(_VERSION_442)

    version, calls = _resolve_with(tmp_path, git)

    assert version == "4.4.2"
    assert not [c for c in calls if "clone" in c]
    assert ["git", "-C", str(dest), "fetch", "--depth=1", "origin", "main"] in calls
    assert ["git", "-C", str(dest), "reset", "--hard", "FETCH_HEAD"] in calls
    # Refresh timer restarted even if VERSION's content didn't change.
    assert (dest / "VERSION").stat().st_mtime > 0


def test_resolve_sdk_source_version_failed_refresh_raises_and_keeps_cache(
    tmp_path: Path,
) -> None:
    dest = _stale_cache(tmp_path)

    with pytest.raises(cv.Invalid, match="Could not resolve host"):
        _resolve_with(tmp_path, _offline)
    assert (dest / ".git").is_dir()
    assert (dest / "VERSION").read_text() == _VERSION_441


def test_resolve_sdk_source_version_clones_when_not_cached(tmp_path: Path) -> None:
    def git(cmd: list[str]) -> None:
        dest = Path(cmd[-1])
        (dest / ".git").mkdir(parents=True)
        (dest / "VERSION").write_text(_VERSION_442)

    version, calls = _resolve_with(tmp_path, git)

    assert version == "4.4.2"
    assert calls[0][:2] == ["git", "clone"]


def test_resolve_sdk_source_version_fetches_under_the_cache_lock(
    tmp_path: Path, install_lock: MagicMock
) -> None:
    """Builds at once would otherwise clone into, or delete, the same folder."""

    def git(cmd: list[str]) -> None:
        dest = Path(cmd[-1])
        (dest / ".git").mkdir(parents=True)
        (dest / "VERSION").write_text(_VERSION_442)

    _resolve_with(tmp_path, git)

    key = _sdk_source_cache_key(_SOURCE["url"], _SOURCE["ref"])
    install_lock.assert_called_once_with(tmp_path, key)


def test_resolve_sdk_source_version_failed_clone_leaves_no_partial_dir(
    tmp_path: Path,
) -> None:
    dest = tmp_path / _sdk_source_cache_key(_SOURCE["url"], _SOURCE["ref"])

    def git(cmd: list[str]) -> None:
        Path(cmd[-1]).mkdir(parents=True)
        _offline(cmd)

    with pytest.raises(cv.Invalid, match="Can't check sdk_source version"):
        _resolve_with(tmp_path, git)
    assert not dest.exists()


# ---------------------------------------------------------------------------
# _native_dts_path
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _sdk_source_version_cache_root -- must nest under the machine-global zephyr root (ESPHOME_ZEPHYR_PREFIX),
# not a bare ~/.esphome/, so they're covered by the same override and by
# `esphome clean-all`.
# ---------------------------------------------------------------------------


def test_sdk_source_version_cache_root_nests_under_zephyr_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ESPHOME_ZEPHYR_PREFIX", str(tmp_path))
    assert (
        _sdk_source_version_cache_root()
        == (tmp_path / "sdk_source_version_cache").resolve()
    )


# ---------------------------------------------------------------------------
# _parse_zephyr_version_file
# ---------------------------------------------------------------------------


def test_parse_zephyr_version_file_reads_major_minor_patch(tmp_path: Path) -> None:
    version_file = tmp_path / "VERSION"
    version_file.write_text(
        "VERSION_MAJOR = 4\n"
        "VERSION_MINOR = 4\n"
        "PATCHLEVEL = 2\n"
        "VERSION_TWEAK = 0\n"
        "EXTRAVERSION =\n"
        "a line without an equals sign\n"
    )
    assert _parse_zephyr_version_file(version_file) == "4.4.2"


def test_parse_zephyr_version_file_rejects_missing_key(tmp_path: Path) -> None:
    version_file = tmp_path / "VERSION"
    version_file.write_text("VERSION_MAJOR = 4\nVERSION_MINOR = 4\n")
    with pytest.raises(cv.Invalid, match="PATCHLEVEL"):
        _parse_zephyr_version_file(version_file)


# ---------------------------------------------------------------------------
# fetch_board_dts -- one branch per way of finding the Zephyr tree
# ---------------------------------------------------------------------------

_FETCH = "esphome.components.zephyr.dts_fetch"


_FetchMocks = tuple[MagicMock, MagicMock, MagicMock]
