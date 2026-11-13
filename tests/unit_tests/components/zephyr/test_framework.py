"""Unit tests for esphome.components.zephyr.framework."""

from __future__ import annotations

from collections.abc import Iterator
import os
from pathlib import Path
import subprocess
import time
from typing import Any
from unittest.mock import MagicMock, patch

import platformdirs
import pytest

from esphome.components.zephyr import framework, sdk_install
from esphome.components.zephyr.const import KEY_SDK_SOURCE_RESOLVED_REF
from esphome.components.zephyr.framework import (
    _git_output,
    _remote_commit,
    _source_cache_key,
    check_and_install,
    get_zephyr_tools_path,
    install_git_modules,
)
from esphome.components.zephyr.framework_helpers import FrameworkSource
from esphome.components.zephyr.variants import ZephyrModule, ZephyrSDK
from esphome.core import EsphomeError
from esphome.framework_helpers import get_python_env_executable_path

TOOLCHAIN: str = "riscv64-zephyr-elf"
_SHA: str = "a" * 40
_SOURCE: FrameworkSource = FrameworkSource(
    name="Zephyr",
    url="https://example.invalid/zephyr",
    optional=("hal_.*",),
    required=frozenset({"hal_common"}),
)
_SDK: ZephyrSDK = ZephyrSDK(source=_SOURCE, workspace_prefix="zephyr")


class _Calls(list):
    """run_command_ok() commands; cwds[i] is the cwd= that call i was given."""

    def __init__(self) -> None:
        super().__init__()
        self.cwds: list[str | None] = []


def _fake_create_venv(root: Path, msg: str | None = None) -> None:
    # Must match get_python_env_executable_path()'s real path (bin/python vs.
    # Scripts/python.exe), or the venv looks missing forever on Windows.
    python = get_python_env_executable_path(root, "python")
    python.parent.mkdir(parents=True, exist_ok=True)
    python.touch()


def _git_source(sha: str | None = _SHA) -> dict[str, Any]:
    source: dict[str, Any] = {
        "type": "git",
        "url": "https://example.invalid/fork.git",
        "ref": "my-branch",
    }
    if sha is not None:
        source[KEY_SDK_SOURCE_RESOLVED_REF] = sha
    return source


def _workspace(tmp_path: Path, source: dict[str, Any] | None = None) -> Path:
    return (
        tmp_path / "zephyr" / "frameworks" / _source_cache_key(_SDK, "v4.4.1", source)
    )


def _install(
    tmp_path: Path,
    source: dict[str, Any] | None = None,
    projects: frozenset[str] = frozenset({"hal_stm32"}),
    manifest_path: str = "zephyr",
    sdk: ZephyrSDK = _SDK,
) -> tuple[_Calls, tuple[Path, Path, dict[str, str]]]:
    """Run check_and_install() against a fake west; return its commands and result.

    A ``west init`` leaves .west/config (manifest.path is where west put the manifest
    repository) and Zephyr's requirements; ``west init -l`` leaves .west/.
    """
    calls = _Calls()

    def fake_run_command_ok(cmd: list[str], **kwargs: Any) -> bool:
        calls.append(cmd)
        calls.cwds.append(kwargs.get("cwd"))
        if cmd[2:4] == ["west", "init"]:
            if "-l" in cmd:
                (Path(kwargs["cwd"]) / ".west").mkdir()
            else:
                workspace = Path(cmd[-1])
                (workspace / ".west").mkdir(parents=True, exist_ok=True)
                (workspace / ".west" / "config").write_text(
                    f"[manifest]\npath = {manifest_path}\n"
                )
                reqs = workspace / "zephyr" / "scripts" / "requirements.txt"
                reqs.parent.mkdir(parents=True, exist_ok=True)
                reqs.write_text("pyelftools\n")
        return True

    with (
        patch.object(
            framework, "get_zephyr_tools_path", return_value=tmp_path / "zephyr"
        ),
        patch(
            "esphome.components.zephyr.framework_helpers.run_command_ok",
            side_effect=fake_run_command_ok,
        ),
        patch(
            "esphome.components.zephyr.framework_helpers.create_venv",
            side_effect=_fake_create_venv,
        ),
        patch("esphome.components.zephyr.framework_helpers.rmdir"),
    ):
        result = check_and_install(
            sdk=sdk, version="4.4.1", source=source, projects=projects
        )
    return calls, result


def _west(calls: _Calls, subcommand: str) -> list[list[str]]:
    return [c for c in calls if c[2:4] == ["west", subcommand]]


def _git(calls: _Calls) -> list[list[str]]:
    return [c for c in calls if c[0] == "git"]


def test_install_locks_are_per_workspace(tmp_path: Path) -> None:
    """Workspaces of one version install apart; the venv they share has its own
    lock, for building it and for installing Zephyr's requirements."""
    with patch("filelock.FileLock") as file_lock:
        _install(tmp_path)

    lock_files = [Path(c.args[0]).name for c in file_lock.call_args_list]
    workspace = _workspace(tmp_path).name
    assert lock_files == [f"{workspace}.lock", "penv-v4.4.1.lock", "penv-v4.4.1.lock"]


def test_official_source_fetches_the_build_projects(tmp_path: Path) -> None:
    calls, (python_bin, framework_path, west_env) = _install(tmp_path)

    workspace = _workspace(tmp_path)
    (init,) = _west(calls, "init")
    assert init[init.index("init") :] == [
        "init",
        "-m",
        _SOURCE.url,
        "-o=--depth=1",
        "--mr",
        "v4.4.1",
        str(workspace),
    ]
    assert _west(calls, "config")[0][-1] == "-hal_.*,+hal_common,+hal_stm32"
    assert (workspace / ".ready").exists()
    assert (workspace / ".west_projects").read_text().split() == [
        "hal_common",
        "hal_stm32",
    ]
    assert framework_path == workspace
    assert west_env["ZEPHYR_BASE"] == str(workspace / "zephyr")
    assert west_env["PATH"].startswith(str(python_bin.parent))


def test_finished_workspace_only_checks_the_names(tmp_path: Path) -> None:
    _install(tmp_path)
    calls, _ = _install(tmp_path)

    assert [c[2:4] for c in calls] == [["west", "list"]]


def test_rebuilt_venv_keeps_the_workspace(tmp_path: Path) -> None:
    """A rebuilt venv (its interpreter was removed) only gets Zephyr's requirements."""
    _install(tmp_path)
    get_python_env_executable_path(
        tmp_path / "zephyr" / "penvs" / "v4.4.1", "python"
    ).unlink()

    calls, _ = _install(tmp_path)

    assert not _west(calls, "init")
    assert not _west(calls, "update")
    assert calls[-1][-2:] == [
        "-r",
        str(_workspace(tmp_path) / "zephyr" / "scripts" / "requirements.txt"),
    ]


def test_git_source_clones_the_fork_and_pins_its_commit(tmp_path: Path) -> None:
    """`west init --mr` can't take a commit: the default branch is cloned, then the
    manifest repository is moved to the commit resolve_sdk_source_version() chose."""
    calls, _ = _install(tmp_path, _git_source())

    workspace = _workspace(tmp_path, _git_source())
    (init,) = _west(calls, "init")
    assert "https://example.invalid/fork.git" in init
    assert "--mr" not in init
    assert _git(calls) == [
        ["git", "fetch", "--depth=1", "--", "origin", _SHA],
        ["git", "reset", "--hard", "FETCH_HEAD"],
    ]
    assert (workspace / ".resolved_ref").read_text() == _SHA
    assert (workspace / ".ready").exists()


def test_git_source_pins_where_west_put_the_manifest(tmp_path: Path) -> None:
    """The manifest repository isn't always zephyr/: NCS's declares `self: path: nrf`."""
    calls, _ = _install(tmp_path, _git_source(), manifest_path="nrf")

    nrf = _workspace(tmp_path, _git_source()) / "nrf"
    assert calls.cwds[calls.index(_git(calls)[0])] == nrf


def test_git_source_moved_branch_is_updated_in_place(tmp_path: Path) -> None:
    _install(tmp_path, _git_source())
    new_sha = "b" * 40

    calls, _ = _install(tmp_path, _git_source(new_sha))

    workspace = _workspace(tmp_path, _git_source())
    assert not _west(calls, "init")
    assert _git(calls)[0] == ["git", "fetch", "--depth=1", "--", "origin", new_sha]
    assert _west(calls, "update")
    assert (workspace / ".resolved_ref").read_text() == new_sha
    assert (workspace / ".ready").exists()


def test_git_source_unchanged_branch_only_checks_the_names(tmp_path: Path) -> None:
    _install(tmp_path, _git_source())
    calls, _ = _install(tmp_path, _git_source())

    assert [c[2:4] for c in calls] == [["west", "list"]]


def test_git_source_without_a_resolved_commit_inits_at_the_ref(
    tmp_path: Path,
) -> None:
    """Defensive: a source dict that bypassed resolve_sdk_source_version()."""
    calls, _ = _install(tmp_path, _git_source(sha=None))

    (init,) = _west(calls, "init")
    assert init[init.index("--mr") + 1] == "my-branch"
    assert not _git(calls)


def test_local_source_uses_the_checkout_where_it_is(tmp_path: Path) -> None:
    """The checkout's parent is the workspace; nothing is filtered or patched there."""
    checkout = tmp_path / "ws" / "my-zephyr"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "west.yml").touch()
    (checkout / "scripts" / "requirements.txt").write_text("pyelftools\n")
    source = {"type": "local", "path": checkout}

    calls, (_, framework_path, west_env) = _install(tmp_path, source)

    assert framework_path == checkout.parent
    assert west_env["ZEPHYR_BASE"] == str(checkout)
    assert [c[2:4] for c in calls if "west" in c] == [
        ["west", "init"],
        ["west", "update"],
    ]
    assert calls[-1][-1] == str(checkout / "scripts" / "requirements.txt")
    assert not (checkout.parent / ".ready").exists()


def test_local_source_without_a_manifest_raises(tmp_path: Path) -> None:
    checkout = tmp_path / "ws" / "my-zephyr"
    checkout.mkdir(parents=True)

    with pytest.raises(EsphomeError, match="does not look like a Zephyr manifest"):
        _install(tmp_path, {"type": "local", "path": checkout})


def test_sdk_without_optional_projects_filters_nothing(tmp_path: Path) -> None:
    """Silicon Labs' manifest already limits its projects itself."""
    sdk = ZephyrSDK(
        source=FrameworkSource(
            name="Silicon Labs SDK",
            url="https://example.invalid/zephyr-silabs",
            optional=(),
            required=frozenset(),
        ),
        workspace_prefix="zephyr-silabs",
    )

    calls, _ = _install(tmp_path, projects=frozenset(), sdk=sdk)

    assert _west(calls, "config")[0][-1] == ""


# ---------------------------------------------------------------------------
# _source_cache_key
# ---------------------------------------------------------------------------


def test_source_cache_key_differs_across_sdk_variants() -> None:
    # ZEPHYR_MAINLINE/NCS/SILABS share one machine-global cache root; a matching version:
    # string must not resolve to the same directory for different SDKs.
    mainline = ZephyrSDK(
        source=FrameworkSource(
            name="Test",
            url="https://example.invalid/zephyr",
            optional=(),
            required=frozenset(),
        ),
        workspace_prefix="a",
    )
    ncs = ZephyrSDK(
        source=FrameworkSource(
            name="Test",
            url="https://example.invalid/nrf",
            optional=(),
            required=frozenset(),
        ),
        workspace_prefix="b",
    )

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
            "esphome.components.zephyr.framework.get_zephyr_tools_path",
            return_value=tmp_path,
        ),
        patch(
            "esphome.components.zephyr.framework.run_command_ok",
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
        "esphome.components.zephyr.framework._git_output",
        side_effect=fake_git_output,
    ):
        calls, _ = _run_install_git_modules(
            tmp_path, [_BRANCH_MODULE], fail_fetch=fail_fetch
        )
    return calls


def test_install_git_modules_skips_the_check_before_refresh(tmp_path: Path) -> None:
    _run_install_git_modules(tmp_path, [_BRANCH_MODULE])

    with patch("esphome.components.zephyr.framework._git_output") as git_output:
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
        "esphome.components.zephyr.framework._git_output",
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


def test_new_venv_gets_the_windows_rmtree_fix(tmp_path: Path) -> None:
    """As nrf52: west init's manifest move needs it on Windows; only a new venv gets it."""
    with patch(
        "esphome.components.zephyr.framework_helpers.install_sitecustomize"
    ) as install_sitecustomize:
        _install(tmp_path)
        _install(tmp_path)
    install_sitecustomize.assert_called_once_with(
        tmp_path / "zephyr" / "penvs" / "v4.4.1"
    )


@pytest.fixture
def zephyr_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep the test out of the real ~/.cache/esphome."""
    monkeypatch.setenv("ESPHOME_ZEPHYR_PREFIX", str(tmp_path / "cache"))
    return (tmp_path / "cache").resolve()


def _make_framework(tmp_path: Path, sdk_version: str = "1.0.1") -> Path:
    framework_path = tmp_path / "framework"
    (framework_path / "zephyr").mkdir(parents=True)
    (framework_path / "zephyr" / "SDK_VERSION").write_text(sdk_version)
    return framework_path


@pytest.fixture
def mock_download(zephyr_tools: Path) -> Iterator[MagicMock]:
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


def test_toolchain_path_is_per_sdk_version_and_toolchain(zephyr_tools: Path) -> None:
    assert (
        framework._get_toolchain_path("1.0.1", TOOLCHAIN)
        == zephyr_tools / "toolchains" / f"zephyr-sdk-1.0.1-{TOOLCHAIN}"
    )


def test_toolchain_path_default_is_global_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ESPHOME_ZEPHYR_PREFIX", raising=False)
    expected = (
        Path(platformdirs.user_cache_dir("esphome", appauthor=False))
        / "zephyr"
        / "toolchains"
        / f"zephyr-sdk-1.0.1-{TOOLCHAIN}"
    ).resolve()
    assert framework._get_toolchain_path("1.0.1", TOOLCHAIN) == expected


def test_install_toolchain_downloads_minimal_and_toolchain(
    tmp_path: Path, mock_download: MagicMock
) -> None:
    """The SDK version comes from the workspace's zephyr/SDK_VERSION."""
    framework_path = _make_framework(tmp_path)
    sdk_path = framework._get_toolchain_path("1.0.1", TOOLCHAIN)

    assert framework.install_toolchain(framework_path, TOOLCHAIN) == sdk_path

    (minimal, toolchain) = mock_download.call_args_list
    assert minimal.args[0] == sdk_install.SDK_NG_MINIMAL_MIRRORS
    assert minimal.args[3] == sdk_path
    assert toolchain.args[0] == sdk_install.SDK_NG_TOOLCHAIN_GNU_MIRRORS
    assert toolchain.args[1]["VERSION"] == "1.0.1"
    assert toolchain.args[1]["toolchain"] == TOOLCHAIN
    assert toolchain.args[3] == sdk_path / "gnu" / TOOLCHAIN
    assert (sdk_path / ".ready").exists()


def test_install_toolchain_locks_per_sdk_and_toolchain(
    tmp_path: Path, mock_download: MagicMock
) -> None:
    framework_path = _make_framework(tmp_path)

    with patch("filelock.FileLock") as file_lock:
        framework.install_toolchain(framework_path, TOOLCHAIN)

    lock_files = [Path(c.args[0]).name for c in file_lock.call_args_list]
    assert lock_files == [f"toolchain-zephyr-sdk-1.0.1-{TOOLCHAIN}.lock"]


def test_install_toolchain_raises_without_sdk_version(
    tmp_path: Path, zephyr_tools: Path
) -> None:
    """A workspace with no SDK_VERSION file names the file it could not read."""
    with pytest.raises(EsphomeError, match="SDK_VERSION"):
        framework.install_toolchain(tmp_path, TOOLCHAIN)


def test_install_toolchain_raises_on_an_empty_sdk_version(
    tmp_path: Path, zephyr_tools: Path
) -> None:
    framework_path = _make_framework(tmp_path, sdk_version="\n")

    with pytest.raises(EsphomeError, match="names no Zephyr SDK version"):
        framework.install_toolchain(framework_path, TOOLCHAIN)


def test_install_toolchain_removes_only_stale_partial_downloads(
    tmp_path: Path, mock_download: MagicMock
) -> None:
    """A fresh leftover may belong to a running build."""
    framework_path = _make_framework(tmp_path)
    toolchains = framework._get_toolchain_path("1.0.1", TOOLCHAIN).parent
    toolchains.mkdir(parents=True)
    stale = toolchains / "zephyr-sdk-0.17.4-arm-zephyr-eabi.toolchain.archive.part"
    fresh = toolchains / "zephyr-sdk-1.0.2-arm-zephyr-eabi.toolchain.archive.part"
    for leftover in (stale, fresh):
        leftover.write_text("")
    two_days_ago = time.time() - 2 * 24 * 60 * 60
    os.utime(stale, (two_days_ago, two_days_ago))

    framework.install_toolchain(framework_path, TOOLCHAIN)

    assert not stale.exists()
    assert fresh.exists()
