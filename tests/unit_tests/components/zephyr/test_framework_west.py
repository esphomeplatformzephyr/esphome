"""Unit tests for esphome.components.zephyr.framework_west."""

from __future__ import annotations

from collections.abc import Callable
import configparser
import os
from pathlib import Path
import subprocess
import time
from typing import Any
from unittest.mock import MagicMock, patch

import platformdirs
import pytest

from esphome.components.zephyr.const import KEY_SDK_SOURCE_RESOLVED_REF
from esphome.components.zephyr.framework_west import (
    _NEUTRAL_HALS,
    _git_output,
    _remote_commit,
    _source_cache_key,
    check_and_install,
    get_zephyr_tools_path,
    install_git_modules,
)
from esphome.components.zephyr.variants import ZephyrModule, ZephyrSDK
from esphome.core import EsphomeError
from esphome.framework_helpers import get_python_env_executable_path

_FAKE_SDK = ZephyrSDK(manifest_url="https://example.invalid/zephyr")
_CACHE_KEY = "v4.4.1-my-branch-00000000"


class _Calls(list):
    """run_command_ok() commands; cwds[i] is the cwd= that call i was given."""

    def __init__(self) -> None:
        super().__init__()
        self.cwds: list[str | None] = []


def _make_fake_run_command_ok(
    tmp_path: Path,
    manifest_path: str = "zephyr",
    fail_list: bool = False,
    zephyr_reqs: bool = False,
) -> tuple[_Calls, Callable[..., bool]]:
    """Record every run_command_ok() call; a `west init` also creates the
    framework and .west/config it would leave on disk (manifest.path is where west put
    the manifest repository), since check_and_install() depends on them existing."""
    calls = _Calls()

    def fake_run_command_ok(cmd: list[str], **kwargs: Any) -> bool:
        calls.append(cmd)
        calls.cwds.append(kwargs.get("cwd"))
        if cmd[2:4] == ["west", "init"]:
            west_dir = tmp_path / "zephyr" / "frameworks" / _CACHE_KEY / ".west"
            west_dir.mkdir(parents=True, exist_ok=True)
            (west_dir / "config").write_text(f"[manifest]\npath = {manifest_path}\n")
        if zephyr_reqs and cmd[2:4] == ["west", "init"]:
            reqs = _framework(tmp_path) / "zephyr" / "scripts" / "requirements.txt"
            reqs.parent.mkdir(parents=True, exist_ok=True)
            reqs.write_text("pyelftools\n")
        if cmd[2:5] == list(_FILTER_CMD):
            config_path = (
                tmp_path / "zephyr" / "frameworks" / _CACHE_KEY / ".west" / "config"
            )
            config = configparser.ConfigParser()
            config.read(config_path)
            config["manifest"]["project-filter"] = cmd[-1]
            with config_path.open("w") as f:
                config.write(f)
        return not (fail_list and cmd[2:4] == ["west", "list"])

    return calls, fake_run_command_ok


def _fake_create_venv(root: Path, msg: str | None = None) -> None:
    # Must match get_python_env_executable_path()'s real path (bin/python vs.
    # Scripts/python.exe), or install_venv looks true forever on Windows.
    python = get_python_env_executable_path(root, "python")
    python.parent.mkdir(parents=True, exist_ok=True)
    python.touch()


def _run_check_and_install(
    tmp_path: Path,
    source: dict | None,
    manifest_path: str = "zephyr",
    update_returncode: int = 0,
    hals: frozenset[str] | None = None,
    fail_list: bool = False,
    zephyr_reqs: bool = False,
) -> tuple[_Calls, MagicMock]:
    """Return the run_command_ok() calls and the mocked subprocess.run."""
    calls, fake_run_command_ok = _make_fake_run_command_ok(
        tmp_path, manifest_path, fail_list, zephyr_reqs
    )
    with (
        patch(
            "esphome.components.zephyr.framework_west.get_zephyr_tools_path",
            return_value=tmp_path / "zephyr",
        ),
        patch(
            "esphome.components.zephyr.framework_west._source_cache_key",
            return_value=_CACHE_KEY,
        ),
        patch(
            "esphome.components.zephyr.framework_west.run_command_ok",
            side_effect=fake_run_command_ok,
        ),
        patch(
            "esphome.components.zephyr.framework_west.create_venv",
            side_effect=_fake_create_venv,
        ),
        patch("subprocess.run") as mock_subprocess_run,
    ):
        mock_subprocess_run.return_value.returncode = update_returncode
        check_and_install(sdk=_FAKE_SDK, version="4.4.1", source=source, hals=hals)
    return calls, mock_subprocess_run


def test_install_locks_are_per_workspace(tmp_path: Path) -> None:
    """Workspaces of one version install apart; the venv they share has its own lock."""
    with patch("filelock.FileLock") as file_lock:
        _run_check_and_install(tmp_path, source=None)

    lock_files = [Path(c.args[0]).name for c in file_lock.call_args_list]
    assert lock_files == [f"{_CACHE_KEY}.lock", "penv-v4.4.1.lock"]


def test_zephyr_requirements_install_holds_the_venv_lock(tmp_path: Path) -> None:
    """Workspaces of one version pip-install into the venv they share one at a time."""
    reqs = _framework(tmp_path) / "zephyr" / "scripts" / "requirements.txt"

    with patch("filelock.FileLock") as file_lock:
        calls, _ = _run_check_and_install(tmp_path, source=None, zephyr_reqs=True)

    lock_files = [Path(c.args[0]).name for c in file_lock.call_args_list]
    assert lock_files == [f"{_CACHE_KEY}.lock", "penv-v4.4.1.lock", "penv-v4.4.1.lock"]
    assert any(c[-2:] == ["-r", str(reqs)] for c in calls)


def _git_source(resolved_sha: str) -> dict:
    return {
        "type": "git",
        "url": "https://example.invalid/fork.git",
        "ref": "my-branch",
        KEY_SDK_SOURCE_RESOLVED_REF: resolved_sha,
    }


def _git_calls(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if c[0] == "git"]


def test_git_source_west_init_uses_ref_then_pins_manifest_repo_to_sha(
    tmp_path: Path,
) -> None:
    """A git sdk_source: is pinned to one resolved commit SHA before
    check_and_install() ever runs (dts_fetch.resolve_sdk_source_version()). `west init
    --mr` can't take a SHA (git clone --branch rejects it), so it inits at the source's
    own ref and the manifest repository is then pinned to the SHA with git."""
    resolved_sha = "a" * 40
    calls, _ = _run_check_and_install(tmp_path, _git_source(resolved_sha))

    init_calls = [c for c in calls if c[2:4] == ["west", "init"]]
    assert len(init_calls) == 1
    assert init_calls[0][init_calls[0].index("--mr") + 1] == "my-branch"
    assert resolved_sha not in init_calls[0]
    assert _git_calls(calls) == [
        ["git", "fetch", "--depth=1", "--", "origin", resolved_sha],
        ["git", "reset", "--hard", "FETCH_HEAD"],
    ]
    framework = tmp_path / "zephyr" / "frameworks" / _CACHE_KEY
    assert (framework / ".resolved_ref").read_text() == resolved_sha


def test_git_source_pins_manifest_repo_where_west_put_it(tmp_path: Path) -> None:
    """The manifest repository isn't always framework/zephyr: NCS's manifest declares
    `self: path: nrf`, and west records wherever it put it in .west/config."""
    resolved_sha = "a" * 40
    calls, _ = _run_check_and_install(
        tmp_path, _git_source(resolved_sha), manifest_path="nrf"
    )

    framework = tmp_path / "zephyr" / "frameworks" / _CACHE_KEY
    fetch = ["git", "fetch", "--depth=1", "--", "origin", resolved_sha]
    assert calls.cwds[calls.index(fetch)] == str(framework / "nrf")
    reset = ["git", "reset", "--hard", "FETCH_HEAD"]
    assert calls.cwds[calls.index(reset)] == str(framework / "nrf")


def test_git_source_moved_ref_repins_manifest_repo_without_reinit(
    tmp_path: Path,
) -> None:
    """A branch that moved since the workspace was initialized is updated in place."""
    _run_check_and_install(tmp_path, _git_source("a" * 40))
    new_sha = "b" * 40
    calls, mock_run = _run_check_and_install(tmp_path, _git_source(new_sha))

    assert not [c for c in calls if c[2:4] == ["west", "init"]]
    assert _git_calls(calls)[0] == [
        "git",
        "fetch",
        "--depth=1",
        "--",
        "origin",
        new_sha,
    ]
    assert mock_run.call_args.args[0][2:4] == ["west", "update"]
    framework = tmp_path / "zephyr" / "frameworks" / _CACHE_KEY
    assert (framework / ".resolved_ref").read_text() == new_sha


def test_git_source_unchanged_ref_skips_update(tmp_path: Path) -> None:
    _run_check_and_install(tmp_path, _git_source("a" * 40))
    calls, mock_run = _run_check_and_install(tmp_path, _git_source("a" * 40))

    assert not calls
    mock_run.assert_not_called()


def test_missing_ready_sentinel_reruns_update_without_reinit(tmp_path: Path) -> None:
    """A workspace whose last `west update` never finished has .west/ but no .ready."""
    _run_check_and_install(tmp_path, None)
    framework = tmp_path / "zephyr" / "frameworks" / _CACHE_KEY
    (framework / ".ready").unlink()

    calls, mock_run = _run_check_and_install(tmp_path, None)

    assert not [c for c in calls if c[2:4] == ["west", "init"]]
    assert mock_run.call_args.args[0][2:4] == ["west", "update"]
    assert (framework / ".ready").is_file()


def test_failed_update_leaves_no_ready_sentinel_and_is_retried(
    tmp_path: Path,
) -> None:
    with pytest.raises(EsphomeError, match="Can't update Zephyr SDK"):
        _run_check_and_install(tmp_path, None, update_returncode=1)
    framework = tmp_path / "zephyr" / "frameworks" / _CACHE_KEY
    assert (framework / ".west").is_dir()
    assert not (framework / ".ready").exists()

    calls, mock_run = _run_check_and_install(tmp_path, None)

    assert not [c for c in calls if c[2:4] == ["west", "init"]]
    assert mock_run.call_args.args[0][2:4] == ["west", "update"]
    assert (framework / ".ready").is_file()


def test_second_failed_update_in_a_row_starts_over(tmp_path: Path) -> None:
    """One failure resumes; a second in a row removes the workspace, so the
    next build initializes it anew."""
    framework = tmp_path / "zephyr" / "frameworks" / _CACHE_KEY
    with pytest.raises(EsphomeError, match="retries it"):
        _run_check_and_install(tmp_path, None, update_returncode=1)
    assert (framework / ".update_failed").is_file()

    with pytest.raises(EsphomeError, match="twice in a row"):
        _run_check_and_install(tmp_path, None, update_returncode=1)
    assert not framework.exists()

    calls, _ = _run_check_and_install(tmp_path, None)
    assert [c for c in calls if c[2:4] == ["west", "init"]]
    assert not (framework / ".update_failed").exists()


def test_successful_update_clears_the_failure_marker(tmp_path: Path) -> None:
    framework = tmp_path / "zephyr" / "frameworks" / _CACHE_KEY
    with pytest.raises(EsphomeError):
        _run_check_and_install(tmp_path, None, update_returncode=1)

    _run_check_and_install(tmp_path, None)

    assert not (framework / ".update_failed").exists()
    assert (framework / ".ready").is_file()


def test_rebuilt_venv_updates_workspace_without_reclone(tmp_path: Path) -> None:
    """A missing venv (e.g. its interpreter was removed) must not wipe the workspace."""
    _run_check_and_install(tmp_path, None)
    framework = tmp_path / "zephyr" / "frameworks" / _CACHE_KEY
    (framework / "keep").touch()
    python = get_python_env_executable_path(
        tmp_path / "zephyr" / "penvs" / "v4.4.1", "python"
    )
    python.unlink()

    calls, mock_run = _run_check_and_install(tmp_path, None)

    assert not [c for c in calls if c[2:4] == ["west", "init"]]
    assert mock_run.call_args.args[0][2:4] == ["west", "update"]
    assert (framework / "keep").exists()


def test_complete_official_install_skips_update(tmp_path: Path) -> None:
    _run_check_and_install(tmp_path, None)
    calls, mock_run = _run_check_and_install(tmp_path, None)

    assert not calls
    mock_run.assert_not_called()


_FILTER_CMD = ("west", "config", "manifest.project-filter")
_SILABS = frozenset({"hal_silabs"})
_NORDIC = frozenset({"hal_nordic"})
_SILABS_FILTER = "-hal_.*,-nrf_hw_models,+hal_afbr,+hal_ethos_u,+hal_infineon,+hal_silabs,+hal_st,+hal_tdk,+hal_wurthelektronik,+hal_xtensa"


def _stamp(*hals: str) -> str:
    return "\n".join(sorted(_NEUTRAL_HALS | set(hals)))


def _filters(calls: list[list[str]]) -> list[str]:
    return [c[-1] for c in calls if c[2:5] == list(_FILTER_CMD)]


def _framework(tmp_path: Path) -> Path:
    return tmp_path / "zephyr" / "frameworks" / _CACHE_KEY


def test_fresh_install_fetches_only_the_family_hals(tmp_path: Path) -> None:
    calls, _ = _run_check_and_install(tmp_path, None, hals=_SILABS)

    assert _filters(calls) == [_SILABS_FILTER]
    assert [c[2:] for c in calls if c[2:4] == ["west", "list"]] == [
        ["west", "list", "-f", "{name}", "hal_silabs"]
    ]
    assert (_framework(tmp_path) / ".west_hals").read_text() == _stamp("hal_silabs")


def test_installed_hals_skip_update(tmp_path: Path) -> None:
    _run_check_and_install(tmp_path, None, hals=_SILABS)
    calls, mock_run = _run_check_and_install(tmp_path, None, hals=_SILABS)

    assert not calls
    mock_run.assert_not_called()


def test_new_family_grows_the_filter_and_leaves_zephyr_alone(tmp_path: Path) -> None:
    """A build may be running from zephyr/, so its gen_defines.py is not restored."""
    _run_check_and_install(tmp_path, None, hals=_SILABS)
    framework = _framework(tmp_path)
    gen_defines = framework / "zephyr" / "scripts" / "dts" / "gen_defines.py"
    gen_defines.parent.mkdir(parents=True)
    gen_defines.touch()

    calls, mock_run = _run_check_and_install(tmp_path, None, hals=_NORDIC)

    assert _filters(calls) == [
        "-hal_.*,-nrf_hw_models,+hal_afbr,+hal_ethos_u,+hal_infineon,+hal_nordic,+hal_silabs,+hal_st,+hal_tdk,+hal_wurthelektronik,+hal_xtensa"
    ]
    assert [c.args[0][2:4] for c in mock_run.call_args_list] == [["west", "update"]]
    assert mock_run.call_args.args[0][-1:] == ["hal_nordic"]
    assert (framework / ".west_hals").read_text() == _stamp("hal_nordic", "hal_silabs")


def test_new_common_hal_grows_an_older_workspace(tmp_path: Path) -> None:
    _run_check_and_install(tmp_path, None, hals=_SILABS)
    framework = _framework(tmp_path)
    (framework / ".west_hals").write_text("hal_silabs")

    calls, mock_run = _run_check_and_install(tmp_path, None, hals=_SILABS)

    assert _filters(calls) == [_SILABS_FILTER]
    assert mock_run.call_args.args[0][-len(_NEUTRAL_HALS) :] == sorted(_NEUTRAL_HALS)
    assert (framework / ".west_hals").read_text() == _stamp("hal_silabs")


def test_failed_grow_puts_the_filter_back(tmp_path: Path) -> None:
    _run_check_and_install(tmp_path, None, hals=_SILABS)
    with pytest.raises(EsphomeError):
        _run_check_and_install(tmp_path, None, hals=_NORDIC, update_returncode=1)

    framework = _framework(tmp_path)
    config = configparser.ConfigParser()
    config.read(framework / ".west" / "config")
    assert config["manifest"]["project-filter"] == _SILABS_FILTER
    assert (framework / ".west_hals").read_text() == _stamp("hal_silabs")
    assert (framework / ".ready").exists()
    assert not (framework / ".update_failed").exists()


def test_two_failed_grows_keep_the_workspace(tmp_path: Path) -> None:
    """Other families build from the shared workspace; a missing HAL never removes it."""
    _run_check_and_install(tmp_path, None, hals=_SILABS)
    for _ in range(2):
        with pytest.raises(EsphomeError, match="retries it"):
            _run_check_and_install(tmp_path, None, hals=_NORDIC, update_returncode=1)

    framework = _framework(tmp_path)
    assert (framework / ".ready").exists()
    assert (framework / ".west_hals").read_text() == _stamp("hal_silabs")


def test_unknown_hal_fails_before_update(tmp_path: Path) -> None:
    """A failing update would raise its own error, so this one shows west list ran first."""
    with pytest.raises(EsphomeError, match="west list failed"):
        _run_check_and_install(
            tmp_path, None, hals=_SILABS, fail_list=True, update_returncode=1
        )

    framework = _framework(tmp_path)
    assert not (framework / ".update_failed").exists()
    assert not (framework / ".west_hals").exists()


def test_unfiltered_workspace_is_never_filtered(tmp_path: Path) -> None:
    """A workspace fetched whole before the filter existed downloads nothing new."""
    _run_check_and_install(tmp_path, None)
    calls, mock_run = _run_check_and_install(tmp_path, None, hals=_SILABS)

    assert not calls
    mock_run.assert_not_called()


def test_unfinished_unfiltered_workspace_gets_the_filter(tmp_path: Path) -> None:
    _run_check_and_install(tmp_path, None)
    (_framework(tmp_path) / ".ready").unlink()

    calls, _ = _run_check_and_install(tmp_path, None, hals=_SILABS)

    assert _filters(calls) == [_SILABS_FILTER]


def test_local_source_is_never_filtered(tmp_path: Path) -> None:
    checkout = tmp_path / "ws" / "zephyr"
    checkout.mkdir(parents=True)
    (checkout / "west.yml").touch()

    calls, _ = _run_check_and_install(
        tmp_path, {"type": "local", "path": checkout}, hals=_SILABS
    )

    assert not _filters(calls)


def test_git_source_west_init_falls_back_to_raw_ref_when_unresolved(
    tmp_path: Path,
) -> None:
    """Defensive fallback: a source dict without KEY_SDK_SOURCE_RESOLVED_REF (e.g.
    constructed directly, bypassing resolve_sdk_source_version()) still works,
    using the raw ref as before."""
    source = {
        "type": "git",
        "url": "https://example.invalid/fork.git",
        "ref": "my-branch",
    }
    calls, fake_run_command_ok = _make_fake_run_command_ok(tmp_path)

    with (
        patch(
            "esphome.components.zephyr.framework_west.get_zephyr_tools_path",
            return_value=tmp_path / "zephyr",
        ),
        patch(
            "esphome.components.zephyr.framework_west._source_cache_key",
            return_value=_CACHE_KEY,
        ),
        patch(
            "esphome.components.zephyr.framework_west.run_command_ok",
            side_effect=fake_run_command_ok,
        ),
        patch(
            "esphome.components.zephyr.framework_west.create_venv",
            side_effect=lambda root, msg=None: Path(root).mkdir(
                parents=True, exist_ok=True
            ),
        ),
        patch("subprocess.run") as mock_subprocess_run,
    ):
        mock_subprocess_run.return_value.returncode = 0
        check_and_install(sdk=_FAKE_SDK, version="4.4.1", source=source)

    init_calls = [c for c in calls if c[2:4] == ["west", "init"]]
    assert len(init_calls) == 1
    assert init_calls[0][init_calls[0].index("--mr") + 1] == "my-branch"


# ---------------------------------------------------------------------------
# _source_cache_key
# ---------------------------------------------------------------------------


def test_source_cache_key_differs_across_sdk_variants() -> None:
    # MAINLINE/NCS/SILABS share one machine-global cache root; a matching version:
    # string must not resolve to the same directory for different SDKs.
    mainline = ZephyrSDK(
        manifest_url="https://example.invalid/zephyr", tools_subdir="a"
    )
    ncs = ZephyrSDK(manifest_url="https://example.invalid/nrf", tools_subdir="b")

    assert _source_cache_key(mainline, "v1.0.0", None) != _source_cache_key(
        ncs, "v1.0.0", None
    )


# ---------------------------------------------------------------------------
# install_git_modules
# ---------------------------------------------------------------------------

_NCS_URL = "https://github.com/nrfconnect/sdk-nrf"
_MODULE = ZephyrModule(
    name="mod", manifest_url="https://example.invalid/mod.git", revision="v1.4.0"
)


def _run_install_git_modules(
    tmp_path: Path,
    modules: list[ZephyrModule],
    fail_fetch: bool = False,
    module_yml: str | None = None,
    west_yml: str | None = None,
) -> tuple[_Calls, list[Path]]:
    """Return the run_command_ok() calls and the module folders; module_yml and
    west_yml are the zephyr/module.yml and west.yml the checkout leaves."""
    calls = _Calls()

    def fake_run_command_ok(cmd: list[str], **kwargs: Any) -> bool:
        calls.append(cmd)
        calls.cwds.append(kwargs.get("cwd"))
        if cmd[:2] == ["git", "init"]:
            (Path(kwargs["cwd"]) / ".git").mkdir()
        if cmd[:2] == ["git", "checkout"] and module_yml is not None:
            (Path(kwargs["cwd"]) / "zephyr").mkdir()
            (Path(kwargs["cwd"]) / "zephyr" / "module.yml").write_text(module_yml)
        if cmd[:2] == ["git", "checkout"] and west_yml is not None:
            (Path(kwargs["cwd"]) / "west.yml").write_text(west_yml)
        return not (fail_fetch and cmd[:2] == ["git", "fetch"])

    with (
        patch(
            "esphome.components.zephyr.framework_west.get_zephyr_tools_path",
            return_value=tmp_path,
        ),
        patch(
            "esphome.components.zephyr.framework_west.run_command_ok",
            side_effect=fake_run_command_ok,
        ),
    ):
        return calls, install_git_modules(modules, _NCS_URL)


def test_install_git_modules_fetches_each_module_once(tmp_path: Path) -> None:
    local = ZephyrModule(name="local", local_path=tmp_path / "mine")

    calls, paths = _run_install_git_modules(tmp_path, [_MODULE, local])

    assert len(paths) == 1
    assert paths[0].parent == tmp_path / "modules"
    assert paths[0].name.startswith("mod-v1.4.0-")
    assert calls == [
        ["git", "init", "-q"],
        [
            "git",
            "fetch",
            "--depth=1",
            "--",
            "https://example.invalid/mod.git",
            "v1.4.0",
        ],
        ["git", "checkout", "-q", "FETCH_HEAD"],
    ]
    assert set(calls.cwds) == {str(paths[0])}

    with patch("filelock.FileLock") as file_lock:
        calls, again = _run_install_git_modules(tmp_path, [_MODULE])
    assert again == paths
    assert not calls
    file_lock.assert_not_called()


def test_install_git_modules_skips_a_fetch_finished_while_waiting(
    tmp_path: Path,
) -> None:
    def _other_build_fetches(lock_file: str, **kwargs: Any) -> MagicMock:
        sentinel = (
            tmp_path / "modules" / Path(lock_file).stem / ".git" / "esphome-ready"
        )
        sentinel.parent.mkdir(parents=True)
        sentinel.touch()
        return MagicMock()

    with patch("filelock.FileLock", side_effect=_other_build_fetches):
        calls, paths = _run_install_git_modules(tmp_path, [_MODULE])

    assert not calls
    assert len(paths) == 1


_BRANCH_MODULE = ZephyrModule(
    name="mod",
    manifest_url="https://example.invalid/mod.git",
    revision="main",
    refresh=3600,
)
_HEAD = "a" * 40


def _fetched_hour_ago(tmp_path: Path) -> Path:
    """Fetch _BRANCH_MODULE and make its last check two hours old."""
    _, paths = _run_install_git_modules(tmp_path, [_BRANCH_MODULE])
    sentinel = paths[0] / ".git" / "esphome-ready"
    old = time.time() - 7200
    os.utime(sentinel, (old, old))
    return sentinel


def _refresh(
    tmp_path: Path, remote: str | None, fail_fetch: bool = False
) -> list[list[str]]:
    """Run a due refresh with the remote branch at `remote` (None = offline)."""

    def fake_git_output(cmd: list[str], cwd: Path | None = None) -> str | None:
        if cmd[1] == "ls-remote":
            return None if remote is None else f"{remote}\trefs/heads/main\n"
        return f"{_HEAD}\n"

    with patch(
        "esphome.components.zephyr.framework_west._git_output",
        side_effect=fake_git_output,
    ):
        calls, _ = _run_install_git_modules(
            tmp_path, [_BRANCH_MODULE], fail_fetch=fail_fetch
        )
    return calls


def test_install_git_modules_skips_the_check_before_refresh(tmp_path: Path) -> None:
    _run_install_git_modules(tmp_path, [_BRANCH_MODULE])

    with patch("esphome.components.zephyr.framework_west._git_output") as git_output:
        calls, _ = _run_install_git_modules(tmp_path, [_BRANCH_MODULE])

    assert not calls
    git_output.assert_not_called()


def test_install_git_modules_unchanged_branch_fetches_nothing(tmp_path: Path) -> None:
    sentinel = _fetched_hour_ago(tmp_path)

    assert not _refresh(tmp_path, remote=_HEAD)
    assert time.time() - sentinel.stat().st_mtime < 60


def test_install_git_modules_moved_branch_updates_in_place(tmp_path: Path) -> None:
    sentinel = _fetched_hour_ago(tmp_path)

    calls = _refresh(tmp_path, remote="b" * 40)

    assert calls == [
        ["git", "fetch", "--depth=1", "--", "https://example.invalid/mod.git", "main"],
        ["git", "checkout", "-q", "FETCH_HEAD"],
    ]
    assert time.time() - sentinel.stat().st_mtime < 60


def test_install_git_modules_offline_keeps_the_module(tmp_path: Path) -> None:
    sentinel = _fetched_hour_ago(tmp_path)

    assert not _refresh(tmp_path, remote=None)
    assert sentinel.exists()


def test_install_git_modules_failed_update_keeps_the_module(tmp_path: Path) -> None:
    sentinel = _fetched_hour_ago(tmp_path)

    with pytest.raises(EsphomeError, match="Can't fetch Zephyr module mod main"):
        _refresh(tmp_path, remote="b" * 40, fail_fetch=True)

    # Still there and still due, so the next build tries the update again
    assert sentinel.exists()
    assert time.time() - sentinel.stat().st_mtime > 3600


def test_git_output_runs_git(tmp_path: Path) -> None:
    """Against a local repository, so no network is needed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        [*git, "commit", "-q", "--allow-empty", "-m", "c"], cwd=repo, check=True
    )
    head = _git_output(["git", "rev-parse", "HEAD"], cwd=repo).strip()

    assert _remote_commit(str(repo), "main") == head
    assert _git_output(["git", "rev-parse", "HEAD"], cwd=tmp_path) is None


@pytest.mark.parametrize(
    ("ls_remote", "expected"),
    [
        (f"{'b' * 40}\trefs/heads/main\n", "b" * 40),
        # An annotated tag: its own object, then the commit on the ^{} line
        (
            f"{'c' * 40}\trefs/tags/main\n{'d' * 40}\trefs/tags/main^{{}}\n",
            "d" * 40,
        ),
        (f"{'e' * 40}\trefs/tags/main\n", "e" * 40),
        ("", None),  # a commit SHA, which ls-remote doesn't list
        (None, None),  # offline
    ],
)
def test_remote_commit(ls_remote: str | None, expected: str | None) -> None:
    with patch(
        "esphome.components.zephyr.framework_west._git_output",
        return_value=ls_remote,
    ):
        assert _remote_commit("https://example.invalid/mod.git", "main") == expected


def test_install_git_modules_failed_fetch_is_retried(tmp_path: Path) -> None:
    with pytest.raises(EsphomeError, match="Can't fetch Zephyr module mod v1.4.0"):
        _run_install_git_modules(tmp_path, [_MODULE], fail_fetch=True)

    calls, _ = _run_install_git_modules(tmp_path, [_MODULE])
    assert ["git", "checkout", "-q", "FETCH_HEAD"] in calls


def test_install_git_modules_folder_follows_the_url(tmp_path: Path) -> None:
    fork = ZephyrModule(
        name="mod", manifest_url="https://example.invalid/fork.git", revision="v1.4.0"
    )

    _, paths = _run_install_git_modules(tmp_path, [_MODULE, fork])

    assert paths[0] != paths[1]


def test_install_git_modules_refuses_a_module_with_blobs(tmp_path: Path) -> None:
    module_yml = "name: mod\nblobs:\n  - path: lib.a\n    sha256: 00\n    url: x\n"

    with pytest.raises(EsphomeError, match="needs binary blobs"):
        _run_install_git_modules(tmp_path, [_MODULE], module_yml=module_yml)

    # The folder is not marked done, so it is checked again on the next build
    calls, _ = _run_install_git_modules(tmp_path, [_MODULE])
    assert calls


def test_install_git_modules_accepts_a_module_without_blobs(tmp_path: Path) -> None:
    _, paths = _run_install_git_modules(
        tmp_path, [_MODULE], module_yml="name: mod\nbuild:\n  cmake: .\n"
    )

    assert len(paths) == 1


def test_install_git_modules_accepts_a_west_yml_listing_only_the_sdk(
    tmp_path: Path,
) -> None:
    # As ncs-zigbee's own west.yml
    west_yml = (
        "manifest:\n"
        "  remotes:\n"
        "    - name: ncs\n"
        "      url-base: https://github.com/nrfconnect\n"
        "  projects:\n"
        "    - name: nrf\n"
        "      remote: ncs\n"
        "      repo-path: sdk-nrf\n"
        "      revision: v3.4.0\n"
        "      import: true\n"
    )

    _, paths = _run_install_git_modules(tmp_path, [_MODULE], west_yml=west_yml)

    assert len(paths) == 1


def test_install_git_modules_refuses_other_projects(tmp_path: Path) -> None:
    west_yml = (
        "manifest:\n"
        "  defaults:\n"
        "    remote: ncs\n"
        "  remotes:\n"
        "    - name: ncs\n"
        "      url-base: https://github.com/nrfconnect\n"
        "  projects:\n"
        "    - name: sdk-nrf\n"
        "    - name: extra\n"
        "      url: https://example.invalid/extra.git\n"
    )

    with pytest.raises(EsphomeError, match=r"needs other projects \(extra\)"):
        _run_install_git_modules(tmp_path, [_MODULE], west_yml=west_yml)


# ---------------------------------------------------------------------------
# get_zephyr_tools_path -- machine-global cache location
# ---------------------------------------------------------------------------


def test_tools_path_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_path / "custom" / "zephyr"
    monkeypatch.setenv("ESPHOME_ZEPHYR_PREFIX", str(override))
    assert get_zephyr_tools_path() == override.resolve()


def test_tools_path_default_is_global_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ESPHOME_ZEPHYR_PREFIX", raising=False)
    expected = (
        Path(platformdirs.user_cache_dir("esphome", appauthor=False)) / "zephyr"
    ).resolve()
    assert get_zephyr_tools_path() == expected


def _gen_defines(zephyr_dir: Path, line: str) -> Path:
    path = zephyr_dir / "scripts" / "dts" / "gen_defines.py"
    path.parent.mkdir(parents=True)
    path.write_text(f'header = f"""\n * Generated from:\n *{line}\n"""\n')
    path.chmod(0o755)
    return path


def test_patch_gen_defines_dts_path_strips_the_directory(tmp_path: Path) -> None:
    """The devicetree header comment keeps only the dts basename, once."""
    from esphome.components.zephyr.framework_west import _patch_gen_defines_dts_path

    path = _gen_defines(tmp_path, "  {edt.dts_path}")
    mode = path.stat().st_mode
    _patch_gen_defines_dts_path(tmp_path)
    _patch_gen_defines_dts_path(tmp_path)
    text = path.read_text()
    assert text.count("{os.path.basename(edt.dts_path)}") == 1
    assert "  {edt.dts_path}" not in text
    assert path.stat().st_mode == mode


def test_patch_gen_defines_dts_path_warns_on_drift(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A reformatted upstream line is left alone and reported."""
    from esphome.components.zephyr.framework_west import _patch_gen_defines_dts_path

    path = _gen_defines(tmp_path, " {edt.dts_source}")
    before = path.read_text()
    _patch_gen_defines_dts_path(tmp_path)
    assert path.read_text() == before
    assert "no longer matches" in caplog.text


def test_patch_gen_defines_dts_path_without_the_script(tmp_path: Path) -> None:
    """A tree without gen_defines.py is skipped quietly."""
    from esphome.components.zephyr.framework_west import _patch_gen_defines_dts_path

    _patch_gen_defines_dts_path(tmp_path)


def test_patch_west_build_tolerates_a_source_dir_on_another_drive(
    tmp_path: Path,
) -> None:
    """West build's relative source path raises across Windows drives; the
    patch applies Zephyr main's fallback to the absolute path."""
    from esphome.components.zephyr.framework_west import (
        _patch_west_build_source_dir_relpath,
    )

    build = tmp_path / "scripts" / "west_commands" / "build.py"
    build.parent.mkdir(parents=True)
    build.write_text(
        "    def _sanity_check_source_dir(self):\n"
        "        srcrel = os.path.relpath(self.source_dir)\n"
        "        self.check_force(True, srcrel)\n"
    )
    _patch_west_build_source_dir_relpath(tmp_path)
    patched = build.read_text()
    assert "relative_to(pathlib.Path.cwd(), walk_up=True)" in patched
    assert (
        "        except ValueError:\n            srcrel = self.source_dir\n" in patched
    )
    assert "os.path.relpath" not in patched
    assert "        self.check_force(True, srcrel)\n" in patched
    assert not list(build.parent.glob("*.tmp"))
    _patch_west_build_source_dir_relpath(tmp_path)  # idempotent
    assert build.read_text() == patched
    _patch_west_build_source_dir_relpath(tmp_path / "absent")  # tolerant


def test_new_venv_gets_the_windows_rmtree_fix(tmp_path: Path) -> None:
    """As nrf52: west init's manifest move needs it on Windows; only a new venv gets it."""
    with patch(
        "esphome.components.nrf52.framework._install_sitecustomize"
    ) as install_sitecustomize:
        _run_check_and_install(tmp_path, None)
        _run_check_and_install(tmp_path, None)
    install_sitecustomize.assert_called_once_with(
        tmp_path / "zephyr" / "penvs" / "v4.4.1"
    )
