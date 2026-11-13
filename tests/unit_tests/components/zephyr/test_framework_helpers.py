"""Tests for esphome.components.zephyr.framework_helpers."""

from collections.abc import Iterator
import os
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY, MagicMock, patch

import pytest

from esphome.components.zephyr import framework_helpers
from esphome.components.zephyr.framework_helpers import (
    FrameworkSource,
    ensure_venv,
    ensure_zephyr_requirements,
    fetch_missing_west_projects,
    install_framework,
    install_local_framework,
    install_sitecustomize,
    needs_venv_rebuild,
    patch_gen_defines_dts_path,
    patch_west_build_source_dir_relpath,
    penv_site_packages,
    pyocd_builtin_target,
    refresh_framework,
    unpatched_pyocd_nrf54lm20a_target,
)
from esphome.core import EsphomeError
from esphome.framework_helpers import get_python_env_executable_path


def test_needs_venv_rebuild_gates(tmp_path: Path) -> None:
    """The shared penv gate rebuilds on any missing or stale piece."""
    penv = tmp_path / "penv"
    penv.mkdir()
    python = penv / "python"
    sentinel = penv / ".ready"
    good_hash = "abc123"

    # Nothing in place yet
    assert needs_venv_rebuild(python, sentinel, good_hash)

    python.write_text("")
    # Interpreter present but no sentinel
    assert needs_venv_rebuild(python, sentinel, good_hash)

    sentinel.write_text(good_hash, encoding="utf-8")
    # Everything in place
    assert not needs_venv_rebuild(python, sentinel, good_hash)

    # Stale requirements hash
    assert needs_venv_rebuild(python, sentinel, "otherhash")


@pytest.mark.skipif(
    sys.platform == "win32", reason="symlink creation needs privileges on Windows"
)
def test_needs_venv_rebuild_on_dangling_interpreter_symlink(tmp_path: Path) -> None:
    """A cached venv restored after a host interpreter upgrade has a
    bin/python symlink whose target is gone; the valid sentinel must not
    mask it."""
    penv = tmp_path / "penv"
    penv.mkdir()
    python = penv / "python"
    sentinel = penv / ".ready"
    sentinel.write_text("abc123", encoding="utf-8")
    python.symlink_to(tmp_path / "hostedtoolcache" / "3.12.14" / "python3")
    assert python.is_symlink()
    assert not python.exists()

    assert needs_venv_rebuild(python, sentinel, "abc123")


def test_install_sitecustomize_writes_patch_on_windows(tmp_path: Path) -> None:
    with patch.object(framework_helpers.os, "name", "nt"):
        install_sitecustomize(tmp_path)

    written = tmp_path / "Lib" / "site-packages" / "sitecustomize.py"
    assert written.read_text(encoding="utf-8") == framework_helpers._SITECUSTOMIZE


def test_install_sitecustomize_does_nothing_elsewhere(tmp_path: Path) -> None:
    with patch.object(framework_helpers.os, "name", "posix"):
        install_sitecustomize(tmp_path)

    assert not any(tmp_path.iterdir())


def test_patch_gen_defines_relativizes_the_dts_path(tmp_path: Path) -> None:
    """The absolute dts.pre path is the only per device byte in the
    devicetree header; the patch makes gen_defines emit the basename."""
    gen = tmp_path / "zephyr" / "scripts" / "dts" / "gen_defines.py"
    gen.parent.mkdir(parents=True)
    gen.write_text("s = f'DTS input file:\\n  {edt.dts_path}\\n'\n")
    patch_gen_defines_dts_path(tmp_path)
    assert "{os.path.basename(edt.dts_path)}" in gen.read_text()
    assert not list(gen.parent.glob("*.tmp"))  # no leftovers
    before = gen.read_text()
    patch_gen_defines_dts_path(tmp_path)  # idempotent
    assert gen.read_text() == before
    patch_gen_defines_dts_path(tmp_path / "absent")  # tolerant


def test_patch_west_build_tolerates_a_source_dir_on_another_drive(
    tmp_path: Path,
) -> None:
    """West build's relative source path raises across Windows drives; the
    patch applies Zephyr main's fallback to the absolute path."""
    build = tmp_path / "zephyr" / "scripts" / "west_commands" / "build.py"
    build.parent.mkdir(parents=True)
    build.write_text(
        "    def _sanity_check_source_dir(self):\n"
        "        srcrel = os.path.relpath(self.source_dir)\n"
        "        self.check_force(True, srcrel)\n"
    )
    patch_west_build_source_dir_relpath(tmp_path)
    patched = build.read_text()
    assert "relative_to(pathlib.Path.cwd(), walk_up=True)" in patched
    assert (
        "        except ValueError:\n            srcrel = self.source_dir\n" in patched
    )
    assert "os.path.relpath" not in patched
    assert "        self.check_force(True, srcrel)\n" in patched
    assert not list(build.parent.glob("*.tmp"))
    patch_west_build_source_dir_relpath(tmp_path)  # idempotent
    assert build.read_text() == patched
    patch_west_build_source_dir_relpath(tmp_path / "absent")  # tolerant


def test_patch_gen_defines_warns_when_the_anchor_is_gone(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A reformatted upstream must not silently cost the sharing."""
    gen = tmp_path / "zephyr" / "scripts" / "dts" / "gen_defines.py"
    gen.parent.mkdir(parents=True)
    gen.write_text("s = 'something else entirely'\n")
    with caplog.at_level("WARNING"):
        patch_gen_defines_dts_path(tmp_path)
    assert "gen_defines.py no longer matches" in caplog.text


def _fake_create_venv(root: Path, msg: str | None = None) -> None:
    python = get_python_env_executable_path(root, "python")
    python.parent.mkdir(parents=True)
    python.touch()


def test_ensure_venv_builds_once_per_requirements(tmp_path: Path) -> None:
    """Built when missing, skipped while current, rebuilt when requirements change."""
    penv = tmp_path / "penv"
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("west==1.5.0\n")
    with (
        patch.object(
            framework_helpers, "create_venv", side_effect=_fake_create_venv
        ) as create,
        patch.object(framework_helpers, "run_command_ok", return_value=True) as pip,
    ):
        assert ensure_venv(penv, requirements, "v1") is True
        assert ensure_venv(penv, requirements, "v1") is False
        requirements.write_text("west==1.6.0\n")
        assert ensure_venv(penv, requirements, "v1") is True

    assert create.call_count == 2
    assert pip.call_args.args[0][-2:] == ["-r", str(requirements)]


def test_ensure_venv_raises_when_pip_fails(tmp_path: Path) -> None:
    penv = tmp_path / "penv"
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("west==1.5.0\n")
    with (
        patch.object(framework_helpers, "create_venv", side_effect=_fake_create_venv),
        patch.object(framework_helpers, "run_command_ok", return_value=False),
        pytest.raises(EsphomeError, match="Install requirements for v1"),
    ):
        ensure_venv(penv, requirements, "v1")

    assert not (penv / ".ready").exists()


SOURCE: FrameworkSource = FrameworkSource(
    name="Test SDK",
    url="https://example.com/sdk",
    optional=("hal_.*",),
    required=frozenset({"zephyr"}),
)
VERSION: str = "v1.0.0"
ENV_PYTHON: Path = Path("/penv/bin/python")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path / "frameworks" / "sdk"


@pytest.fixture
def west(workspace: Path) -> Iterator[MagicMock]:
    """run_command_ok for west and pip; west init leaves an initialized workspace."""

    def _run(cmd: list[str], **kwargs: Any) -> bool:
        if "init" in cmd:
            _mark_initialized(workspace)
        return True

    with (
        patch.object(framework_helpers, "rmdir") as rmdir,
        patch.object(framework_helpers, "run_command_ok", side_effect=_run) as run,
    ):
        run.rmdir = rmdir
        yield run


def _mark_initialized(workspace: Path, config: str = "") -> None:
    (workspace / ".west").mkdir(parents=True, exist_ok=True)
    (workspace / ".west" / "config").write_text(config, encoding="utf-8")


def _mark_finished(workspace: Path, stamp: str | None) -> None:
    """A finished install with a filter; ``stamp`` None leaves the stamp out."""
    _mark_initialized(workspace, "[manifest]\nproject-filter = -hal_.*,+zephyr\n")
    (workspace / ".ready").touch()
    if stamp is not None:
        (workspace / ".west_projects").write_text(stamp, encoding="utf-8")


def _subcommands(run: MagicMock) -> list[str]:
    def _sub(cmd: list[str]) -> str:
        if cmd[0] == "git":
            return f"git {cmd[1]}"
        return cmd[cmd.index("west" if "west" in cmd else "pip") + 1]

    return [_sub(c.args[0]) for c in run.call_args_list]


def _west_call(run: MagicMock, subcommand: str, nth: int = -1) -> MagicMock:
    return [c for c in run.call_args_list if subcommand in c.args[0]][nth]


def _project_filter(run: MagicMock) -> str:
    return _west_call(run, "config").args[0][-1]


def _install(workspace: Path, projects: set[str]) -> None:
    install_framework(ENV_PYTHON, workspace, SOURCE, VERSION, projects)


def _fetch(workspace: Path, projects: set[str]) -> None:
    fetch_missing_west_projects(ENV_PYTHON, workspace, SOURCE, VERSION, projects)


def test_install_clones_the_manifest_with_the_filter(
    workspace: Path, west: MagicMock
) -> None:
    _install(workspace, {"zephyr", "hal_a"})

    west.rmdir.assert_called_once_with(workspace, msg=ANY)
    assert _subcommands(west) == ["init", "config", "update", "list"]
    init = _west_call(west, "init")
    assert init.args[0][init.args[0].index("init") :] == [
        "init",
        "-m",
        SOURCE.url,
        "-o=--depth=1",
        "--mr",
        VERSION,
        str(workspace),
    ]
    assert _project_filter(west) == "-hal_.*,+hal_a,+zephyr"
    update = _west_call(west, "update")
    assert update.args[0][-2:] == ["--narrow", "--fetch-opt=--depth=1"]
    assert update.kwargs == {"cwd": workspace, "stream_output": True}
    # Streamed, so the long clone's progress reaches the log
    assert init.kwargs["stream_output"] is True
    # Required projects are never checked by name
    assert _west_call(west, "list").args[0][-1:] == ["hal_a"]
    assert (workspace / ".west_projects").read_text(encoding="utf-8").split() == [
        "hal_a",
        "zephyr",
    ]


def test_install_of_only_required_projects_checks_no_names(
    workspace: Path, west: MagicMock
) -> None:
    _install(workspace, {"zephyr"})

    assert _subcommands(west) == ["init", "config", "update"]


def test_interrupted_download_resumes(workspace: Path, west: MagicMock) -> None:
    """A workspace left by a cut-short download is updated in place."""
    _mark_initialized(workspace)
    (workspace / ".resume_failed").touch()

    _install(workspace, {"zephyr"})

    west.rmdir.assert_not_called()
    assert _subcommands(west) == ["config", "update"]
    assert not (workspace / ".resume_failed").exists()


def test_failed_resume_keeps_the_download_once(
    workspace: Path, west: MagicMock
) -> None:
    """A first failed resume keeps what was fetched and is retried next build."""
    _mark_initialized(workspace)
    west.side_effect = None
    west.return_value = False

    with pytest.raises(EsphomeError, match="Can't resume the Test SDK v1.0.0"):
        _install(workspace, {"zephyr"})

    west.rmdir.assert_not_called()
    assert (workspace / ".resume_failed").exists()


def test_second_failed_resume_starts_over(workspace: Path, west: MagicMock) -> None:
    _mark_initialized(workspace)
    (workspace / ".resume_failed").touch()
    run = west.side_effect
    # The resumed update (second command) fails, the clean clone succeeds
    west.side_effect = lambda cmd, **kw: west.call_count != 2 and run(cmd, **kw)

    _install(workspace, {"zephyr"})

    west.rmdir.assert_called_once_with(workspace, msg=ANY)
    assert _subcommands(west) == ["config", "update", "init", "config", "update"]


def test_cut_short_init_starts_over(workspace: Path, west: MagicMock) -> None:
    """A .west without its config (init cut short) clones clean."""
    (workspace / ".west").mkdir(parents=True)

    _install(workspace, {"zephyr"})

    west.rmdir.assert_called_once_with(workspace, msg=ANY)
    assert _subcommands(west)[0] == "init"


def test_install_init_failure_raises(workspace: Path, west: MagicMock) -> None:
    west.side_effect = None
    west.return_value = False

    with pytest.raises(EsphomeError, match="Can't initialize Test SDK v1.0.0"):
        _install(workspace, {"zephyr"})


def test_install_update_failure_raises(workspace: Path, west: MagicMock) -> None:
    run = west.side_effect
    west.side_effect = lambda cmd, **kw: "update" not in cmd and run(cmd, **kw)

    with pytest.raises(EsphomeError, match="Can't update Test SDK v1.0.0"):
        _install(workspace, {"zephyr"})

    assert not (workspace / ".west_projects").exists()


def test_unknown_project_is_never_recorded(workspace: Path, west: MagicMock) -> None:
    """A filter naming a project the manifest lacks fetches nothing, so the
    names are checked once the update resolved the manifest."""
    run = west.side_effect
    west.side_effect = lambda cmd, **kw: "list" not in cmd and run(cmd, **kw)

    with pytest.raises(EsphomeError, match="west list failed .*no_such_project"):
        _install(workspace, {"zephyr", "no_such_project"})

    assert not (workspace / ".west_projects").exists()


def test_fetch_of_only_required_projects_reads_nothing(
    workspace: Path, west: MagicMock
) -> None:
    """Any install has the required projects; an unreadable stamp shows nothing looked."""
    _mark_finished(workspace, None)
    (workspace / ".west_projects").mkdir()

    _fetch(workspace, {"zephyr"})

    west.assert_not_called()


def test_fetch_gains_only_the_missing_project(workspace: Path, west: MagicMock) -> None:
    """The names are checked first; only the new project is updated, so a
    build running from the others is left alone."""
    _mark_finished(workspace, "hal_a\nzephyr")

    _fetch(workspace, {"zephyr", "hal_b"})

    assert _subcommands(west) == ["list", "config", "update"]
    assert _west_call(west, "list").args[0][-1:] == ["hal_b"]
    assert _project_filter(west) == "-hal_.*,+hal_a,+hal_b,+zephyr"
    assert _west_call(west, "update").args[0][-1] == "hal_b"
    assert (workspace / ".west_projects").read_text(encoding="utf-8").split() == [
        "hal_a",
        "hal_b",
        "zephyr",
    ]
    west.rmdir.assert_not_called()


@pytest.mark.parametrize(
    ("config", "stamp"),
    [
        pytest.param("", None, id="install_from_before_the_filter"),
        pytest.param(None, "hal_a\nzephyr", id="project_already_fetched"),
    ],
)
def test_fetch_with_the_project_only_checks_the_names(
    workspace: Path, west: MagicMock, config: str | None, stamp: str | None
) -> None:
    """An install without the stamp or a filter has every project; the names
    are still checked, so a mistake shows on every install alike."""
    _mark_finished(workspace, stamp)
    if config is not None:
        _mark_initialized(workspace, config)

    _fetch(workspace, {"zephyr", "hal_a"})

    assert _subcommands(west) == ["list"]


@pytest.mark.parametrize(
    "lose_config",
    [
        pytest.param(False, id="filtered_install_lost_its_stamp"),
        pytest.param(True, id="stamp_and_west_config_lost"),
    ],
)
def test_fetch_without_a_record_fetches_again(
    workspace: Path, west: MagicMock, lose_config: bool
) -> None:
    """Nothing says what the install has, so the wanted projects are fetched with a
    full update, which skips a required project the manifest lacks."""
    _mark_finished(workspace, None)
    if lose_config:
        (workspace / ".west" / "config").unlink()

    _fetch(workspace, {"zephyr", "hal_a"})

    assert _subcommands(west) == ["list", "config", "update", "list"]
    assert _west_call(west, "update").args[0][-1] == "--fetch-opt=--depth=1"


def test_fetch_of_a_new_required_project_updates_in_full(
    workspace: Path, west: MagicMock
) -> None:
    """A stamp from before a project became required: west update by name would
    fail on a version whose manifest lacks it."""
    _mark_finished(workspace, "hal_a")

    _fetch(workspace, {"zephyr", "hal_b"})

    assert _west_call(west, "update").args[0][-1] == "--fetch-opt=--depth=1"
    assert _project_filter(west) == "-hal_.*,+hal_a,+hal_b,+zephyr"


@pytest.mark.parametrize(
    ("stamp", "restored"),
    [
        pytest.param("hal_a\nzephyr", "-hal_.*,+hal_a,+zephyr", id="stamp"),
        # An unknown installed set must not leave every project off
        pytest.param(None, "-hal_.*,+zephyr", id="lost_stamp"),
    ],
)
def test_failed_fetch_restores_the_filter(
    workspace: Path, west: MagicMock, stamp: str | None, restored: str
) -> None:
    """A failed fetch raises, keeps the stamp and puts the filter back to the
    stamp's projects and the required ones."""
    _mark_finished(workspace, stamp)
    run = west.side_effect
    west.side_effect = lambda cmd, **kw: "update" not in cmd and run(cmd, **kw)

    with pytest.raises(EsphomeError, match="Can't update Test SDK v1.0.0"):
        _fetch(workspace, {"zephyr", "hal_b"})

    assert _subcommands(west) == ["list", "config", "update", "config"]
    assert _project_filter(west) == restored
    if stamp is not None:
        assert (workspace / ".west_projects").read_text(encoding="utf-8") == stamp


def test_failed_filter_restore_is_logged(
    workspace: Path, west: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    _mark_finished(workspace, "zephyr")
    # list and config succeed, update and the restoring config fail
    west.side_effect = [True, True, False, False]

    with pytest.raises(EsphomeError, match="Can't update"):
        _fetch(workspace, {"zephyr", "hal_b"})

    assert "Couldn't put the Test SDK v1.0.0 project filter back" in caplog.text


def test_fetch_of_an_unknown_project_changes_nothing(
    workspace: Path, west: MagicMock
) -> None:
    """On a finished install the names are checked first, so an unknown one
    costs no fetch and leaves the workspace as it was."""
    _mark_finished(workspace, "zephyr")
    west.side_effect = [False]

    with pytest.raises(EsphomeError, match="west list failed .*Test SDK v1.0.0"):
        _fetch(workspace, {"zephyr", "no_such_project"})

    assert _subcommands(west) == ["list"]
    assert (workspace / ".west_projects").read_text(encoding="utf-8") == "zephyr"


@pytest.fixture
def zephyr_reqs(tmp_path: Path) -> Path:
    reqs = tmp_path / "sdk" / "zephyr" / "scripts" / "requirements.txt"
    reqs.parent.mkdir(parents=True)
    reqs.write_text("pyelftools\n")
    (tmp_path / "penv").mkdir()
    return reqs


def _ensure_zephyr_requirements(tmp_path: Path, venv_rebuilt: bool = False) -> None:
    ensure_zephyr_requirements(tmp_path / "penv", tmp_path / "sdk", venv_rebuilt, "v1")


@pytest.mark.parametrize(
    ("marker_age", "venv_rebuilt", "installs"),
    [
        pytest.param(None, False, True, id="no_marker"),
        pytest.param(10, False, False, id="current"),
        pytest.param(-10, False, True, id="requirements_newer"),
        pytest.param(10, True, True, id="venv_rebuilt"),
    ],
)
def test_ensure_zephyr_requirements(
    tmp_path: Path,
    zephyr_reqs: Path,
    marker_age: int | None,
    venv_rebuilt: bool,
    installs: bool,
) -> None:
    """Installed when the venv was rebuilt, the marker is missing, or the
    workspace's requirements are newer than the marker."""
    marker = tmp_path / "penv" / ".zephyr_reqs_ready-sdk"
    if marker_age is not None:
        marker.touch()
        reqs_time = marker.stat().st_mtime - marker_age
        os.utime(zephyr_reqs, (reqs_time, reqs_time))

    with patch.object(framework_helpers, "run_command_ok", return_value=True) as pip:
        _ensure_zephyr_requirements(tmp_path, venv_rebuilt)

    assert pip.called is installs
    if installs:
        assert pip.call_args.args[0][-2:] == ["-r", str(zephyr_reqs)]
        assert marker.exists()


def test_ensure_zephyr_requirements_raises_when_pip_fails(
    tmp_path: Path, zephyr_reqs: Path
) -> None:
    with (
        patch.object(framework_helpers, "run_command_ok", return_value=False),
        pytest.raises(EsphomeError, match="Install Zephyr requirements for v1"),
    ):
        _ensure_zephyr_requirements(tmp_path)

    assert not (tmp_path / "penv" / ".zephyr_reqs_ready-sdk").exists()


def test_each_workspace_sharing_the_penv_installs_its_requirements(
    tmp_path: Path, zephyr_reqs: Path
) -> None:
    """Another workspace's newer marker does not stand for this one's install."""
    marker = tmp_path / "penv" / ".zephyr_reqs_ready-other-sdk"
    marker.touch()
    reqs_time = marker.stat().st_mtime - 10
    os.utime(zephyr_reqs, (reqs_time, reqs_time))

    with patch.object(framework_helpers, "run_command_ok", return_value=True) as pip:
        _ensure_zephyr_requirements(tmp_path)

    assert pip.called


PIN: str = "0123456789abcdef0123456789abcdef01234567"


def test_pinned_install_clones_the_default_branch_then_pins(
    workspace: Path, west: MagicMock
) -> None:
    """--mr can't take a commit: the default branch is cloned, then the manifest
    repository is moved to the pin before west update reads it."""
    install_framework(ENV_PYTHON, workspace, SOURCE, "main", {"zephyr"}, pin=PIN)

    assert _subcommands(west) == ["init", "git fetch", "git reset", "config", "update"]
    assert "--mr" not in _west_call(west, "init").args[0]
    fetch = _west_call(west, "fetch")
    assert fetch.args[0] == ["git", "fetch", "--depth=1", "--", "origin", PIN]
    assert fetch.kwargs["cwd"] == workspace / "zephyr"


def test_pinned_install_raises_when_the_pin_fails(
    workspace: Path, west: MagicMock
) -> None:
    run = west.side_effect
    west.side_effect = lambda cmd, **kw: cmd[0] != "git" and run(cmd, **kw)

    with pytest.raises(EsphomeError, match=f"Can't pin Test SDK main to {PIN}"):
        install_framework(ENV_PYTHON, workspace, SOURCE, "main", {"zephyr"}, pin=PIN)

    assert "update" not in _subcommands(west)


def test_pinned_resume_pins_again_first(workspace: Path, west: MagicMock) -> None:
    """The download may have stopped between init and the pin."""
    _mark_initialized(workspace)

    install_framework(ENV_PYTHON, workspace, SOURCE, "main", {"zephyr"}, pin=PIN)

    assert _subcommands(west) == ["git fetch", "git reset", "config", "update"]


def test_pinned_resume_failing_to_pin_keeps_the_download_once(
    workspace: Path, west: MagicMock
) -> None:
    _mark_initialized(workspace)
    west.side_effect = None
    west.return_value = False

    with pytest.raises(EsphomeError, match="Can't resume"):
        install_framework(ENV_PYTHON, workspace, SOURCE, "main", {"zephyr"}, pin=PIN)

    assert (workspace / ".resume_failed").exists()


def test_refresh_moves_a_finished_workspace_to_the_pin(
    workspace: Path, west: MagicMock
) -> None:
    """The patched scripts are put back so west can check out the new commit;
    every project is updated and checked, and the stamp written."""
    _mark_finished(workspace, "zephyr")
    scripts = workspace / "zephyr" / "scripts"
    for script in ("dts/gen_defines.py", "west_commands/build.py"):
        (scripts / script).parent.mkdir(parents=True, exist_ok=True)
        (scripts / script).touch()

    refresh_framework(ENV_PYTHON, workspace, SOURCE, "main", {"zephyr", "hal_a"}, PIN)

    assert _subcommands(west) == [
        "git checkout",
        "git checkout",
        "git fetch",
        "git reset",
        "config",
        "update",
        "list",
    ]
    assert _west_call(west, "checkout").kwargs["cwd"] == workspace / "zephyr"
    assert not (workspace / ".ready").exists()
    assert (workspace / ".west_projects").read_text(encoding="utf-8").split() == [
        "hal_a",
        "zephyr",
    ]


def test_failed_refresh_leaves_an_unfinished_install(
    workspace: Path, west: MagicMock
) -> None:
    """Without .ready the next build resumes, which pins again."""
    _mark_finished(workspace, "zephyr")
    run = west.side_effect
    west.side_effect = lambda cmd, **kw: "update" not in cmd and run(cmd, **kw)

    with pytest.raises(EsphomeError, match="the next build retries it"):
        refresh_framework(ENV_PYTHON, workspace, SOURCE, "main", {"zephyr"}, PIN)

    assert not (workspace / ".ready").exists()
    assert (workspace / ".west" / "config").is_file()


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    path = tmp_path / "src" / "my-zephyr"
    path.mkdir(parents=True)
    return path


def test_local_install_initializes_once_and_updates_every_time(
    checkout: Path, west: MagicMock
) -> None:
    run = west.side_effect

    def _run(cmd: list[str], **kwargs: Any) -> bool:
        if "init" in cmd:
            (checkout.parent / ".west").mkdir()
        return run(cmd, **kwargs)

    west.side_effect = _run

    assert install_local_framework(ENV_PYTHON, checkout, "Zephyr") == checkout.parent
    assert install_local_framework(ENV_PYTHON, checkout, "Zephyr") == checkout.parent

    assert _subcommands(west) == ["init", "update", "update"]
    init = _west_call(west, "init", 0)
    assert init.args[0][-2:] == ["-l", "my-zephyr"]
    assert init.kwargs["cwd"] == checkout.parent
    # The user's tree: no filter, nothing deleted
    assert "config" not in _subcommands(west)
    west.rmdir.assert_not_called()


@pytest.mark.parametrize("failing", ["init", "update"])
def test_local_install_failure_raises(
    checkout: Path, west: MagicMock, failing: str
) -> None:
    run = west.side_effect
    west.side_effect = lambda cmd, **kw: failing not in cmd and run(cmd, **kw)

    with pytest.raises(EsphomeError, match=re.escape(str(checkout))):
        install_local_framework(ENV_PYTHON, checkout, "Zephyr")

    west.rmdir.assert_not_called()


def test_ensure_zephyr_requirements_of_a_local_checkout(tmp_path: Path) -> None:
    """A local checkout's folder needn't be named zephyr."""
    checkout = tmp_path / "my-zephyr"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "scripts" / "requirements.txt").write_text("pyelftools\n")
    (tmp_path / "penv").mkdir()

    with patch.object(framework_helpers, "run_command_ok", return_value=True) as pip:
        ensure_zephyr_requirements(
            tmp_path / "penv", tmp_path, False, "v1", zephyr_dir=checkout
        )

    assert pip.call_args.args[0][-1] == str(checkout / "scripts" / "requirements.txt")


@pytest.mark.parametrize(
    ("os_name", "expected_parts"),
    [
        (
            "posix",
            (
                "lib",
                f"python{sys.version_info.major}.{sys.version_info.minor}",
                "site-packages",
            ),
        ),
        ("nt", ("Lib", "site-packages")),
    ],
)
def test_penv_site_packages(
    tmp_path: Path, os_name: str, expected_parts: tuple[str, ...]
) -> None:
    penv_path = tmp_path / "penv"
    with patch("os.name", os_name):
        assert penv_site_packages(penv_path) == penv_path.joinpath(*expected_parts)


PYOCD_NRF54LM20A_BEFORE = """\
from ..family.target_nRF54L import NRF54L

FLASH_ALGO = {
    'load_address' : 0x20000000,
    'instructions': [
    0xe7fdbe00,
    0x68134a02, 0xd1fc2b01, 0x46c04770, 0x5004e400, 0x47702000, 0x47702000, 0x2501b570, 0x60254c05,
    ],
    'page_size' : 0x4,
    'sector_sizes': (
        (0x0, 0x1fd000),
        (0xffd000, 0x1000),
    )
}

class NRF54LM20A(NRF54L):
    pass
"""


def test_patch_pyocd_nrf54lm20a_flash_algo(tmp_path: Path) -> None:
    """PyOCD's faulting nRF54LM20A flash algorithm is replaced by the fixed one."""
    target = (
        penv_site_packages(tmp_path)
        / "pyocd"
        / "target"
        / "builtin"
        / "target_nRF54LM20A.py"
    )
    target.parent.mkdir(parents=True)
    target.write_text(PYOCD_NRF54LM20A_BEFORE)
    framework_helpers.patch_pyocd_nrf54lm20a_flash_algo(tmp_path)
    patched = target.read_text()
    assert framework_helpers._PYOCD_NRF54LM20A_FLASH_ALGO in patched
    assert "0x68134a02" not in patched
    assert patched.startswith("from ..family.target_nRF54L import NRF54L\n\n")
    assert patched.endswith("\nclass NRF54LM20A(NRF54L):\n    pass\n")
    framework_helpers.patch_pyocd_nrf54lm20a_flash_algo(tmp_path)  # idempotent
    assert target.read_text() == patched
    framework_helpers.patch_pyocd_nrf54lm20a_flash_algo(tmp_path / "absent")  # tolerant


def test_unpatched_pyocd_nrf54lm20a_target(tmp_path: Path) -> None:
    """The upload error names pyOCD's target file until the fix is in it."""
    target = framework_helpers._pyocd_nrf54lm20a_target(tmp_path)
    assert unpatched_pyocd_nrf54lm20a_target(tmp_path) is None  # absent
    target.parent.mkdir(parents=True)
    target.write_text(PYOCD_NRF54LM20A_BEFORE)
    assert unpatched_pyocd_nrf54lm20a_target(tmp_path) == target
    framework_helpers.patch_pyocd_nrf54lm20a_flash_algo(tmp_path)
    assert unpatched_pyocd_nrf54lm20a_target(tmp_path) is None


def test_patch_pyocd_nrf54lm20a_leaves_another_algorithm_alone(tmp_path: Path) -> None:
    """A newer pyOCD's algorithm, without the faulting instructions, is not replaced."""
    target = framework_helpers._pyocd_nrf54lm20a_target(tmp_path)
    target.parent.mkdir(parents=True)
    newer = PYOCD_NRF54LM20A_BEFORE.replace("0x68134a02", "0x12345678")
    target.write_text(newer)
    framework_helpers.patch_pyocd_nrf54lm20a_flash_algo(tmp_path)
    assert target.read_text() == newer
    assert unpatched_pyocd_nrf54lm20a_target(tmp_path) is None


def test_patch_pyocd_nrf54lm20a_warns_on_an_unknown_layout(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The faulting algorithm in a layout the patch cannot find is left as it is."""
    target = framework_helpers._pyocd_nrf54lm20a_target(tmp_path)
    target.parent.mkdir(parents=True)
    indented = PYOCD_NRF54LM20A_BEFORE.replace("FLASH_ALGO = {", "  FLASH_ALGO = {")
    target.write_text(indented)
    framework_helpers.patch_pyocd_nrf54lm20a_flash_algo(tmp_path)
    assert target.read_text() == indented
    assert "Could not patch pyOCD's nRF54LM20A flash algorithm" in caplog.text


def test_pyocd_target_found_in_an_env_of_another_python_version(
    tmp_path: Path,
) -> None:
    """An SDK env kept across a Python upgrade still has its packages under its own version."""
    builtin = tmp_path / "lib" / "python3.9" / "site-packages" / "pyocd" / "target"
    (builtin / "builtin").mkdir(parents=True)
    (builtin / "builtin" / "target_nRF54L15.py").write_text("")
    with patch.object(
        framework_helpers.sys, "version_info", SimpleNamespace(major=3, minor=99)
    ):
        assert pyocd_builtin_target(tmp_path, "target_nRF54L15.py").is_file()
        assert not pyocd_builtin_target(tmp_path, "target_nRF54LM20A.py").is_file()


def test_ensure_zephyr_requirements_patches_pyocd_every_build(
    tmp_path: Path, zephyr_reqs: Path
) -> None:
    """A current requirements marker skips pip, not the pyOCD patch."""
    (tmp_path / "penv" / ".zephyr_reqs_ready-sdk").touch()
    with (
        patch.object(framework_helpers, "run_command_ok") as pip,
        patch.object(framework_helpers, "patch_pyocd_nrf54lm20a_flash_algo") as fix,
    ):
        _ensure_zephyr_requirements(tmp_path)
    pip.assert_not_called()
    fix.assert_called_once_with(tmp_path / "penv")
