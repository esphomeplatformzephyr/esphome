"""Helpers shared by the Zephyr-based framework installers."""

from __future__ import annotations

import configparser
from dataclasses import dataclass
import hashlib
import logging
import os
from pathlib import Path
import re
import shutil
import sys

from esphome.core import EsphomeError
from esphome.framework_helpers import (
    create_venv,
    get_python_env_executable_path,
    rmdir,
    run_command_ok,
)

_LOGGER = logging.getLogger(__name__)


def needs_venv_rebuild(
    env_python_path: Path, sentinel: Path, requirements_hash: str
) -> bool:
    """True when a penv must be (re)built.

    Rebuild when the interpreter is not a regular file, which covers a
    dangling symlink (a cached venv outliving a host interpreter upgrade)
    and a corrupt restore, or when the sentinel is missing or stale.
    """
    return (
        not env_python_path.is_file()
        or not sentinel.exists()
        or sentinel.read_text(encoding="utf-8") != requirements_hash
    )


_SITECUSTOMIZE = """\
import os, stat, shutil
_orig = shutil.rmtree
def _handler(func, path, exc):
    os.chmod(path, stat.S_IWRITE); func(path)
def _rmtree(path, ignore_errors=False, onerror=None, *, onexc=None, dir_fd=None):
    if onerror is None and onexc is None:
        onexc = _handler
    return _orig(path, ignore_errors=ignore_errors, onerror=onerror, onexc=onexc, dir_fd=dir_fd)
shutil.rmtree = _rmtree
"""


def install_sitecustomize(python_env_path: Path) -> None:
    """Patch shutil.rmtree inside the penv to handle read-only files.

    west init's shutil.move falls back to copytree+rmtree on Windows, and
    rmtree dies on the read-only .idx/.pack files git just wrote into
    manifest-tmp. Dropping a sitecustomize.py into the venv applies the
    same fix esphome.helpers.rmtree uses, but inside the subprocess.
    """
    if os.name != "nt":
        return
    site_packages = python_env_path / "Lib" / "site-packages"
    site_packages.mkdir(parents=True, exist_ok=True)
    (site_packages / "sitecustomize.py").write_text(_SITECUSTOMIZE, encoding="utf-8")


def patch_framework_file(path: Path, old: str, new: str) -> bool:
    """Replace ``old`` with ``new`` in a framework script, atomically and
    keeping the file mode (helpers.write_file would flatten it to 0o644).
    Returns False when nothing matched."""
    import tempfile

    content = path.read_text(encoding="utf-8")
    patched = content.replace(old, new)
    if patched == content:
        return False
    # Unique sibling tmp: the install lock is best effort, so two builds
    # may patch at once and a shared tmp name could rename a half
    # written file into place.
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(patched)
        shutil.copymode(path, tmp)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return True


_WEST_BUILD_RELPATH = "        srcrel = os.path.relpath(self.source_dir)\n"
# Zephyr main's replacement, verbatim (zephyr commit dfbdca0e13)
_WEST_BUILD_RELPATH_SAFE = """        try:
            srcrel = pathlib.Path(self.source_dir).relative_to(pathlib.Path.cwd(), walk_up=True)
        except ValueError:
            srcrel = self.source_dir
"""


def patch_west_build_source_dir_relpath(framework_path: Path) -> None:
    # west build names the source dir relative to the working directory (the
    # SDK workspace) in two warnings; Python refuses that on Windows when the
    # project is on another drive, and west dies before building. Applies
    # Zephyr main's fix; SDKs that carry it have no line to patch.
    build = framework_path / "zephyr" / "scripts" / "west_commands" / "build.py"
    if build.exists():
        patch_framework_file(build, _WEST_BUILD_RELPATH, _WEST_BUILD_RELPATH_SAFE)


def patch_gen_defines_dts_path(framework_path: Path) -> None:
    # The absolute zephyr.dts.pre path in the header's top comment is
    # its only per device byte and blocks ccache sharing; emit the
    # basename. Upstream candidate.
    gen_defines = framework_path / "zephyr" / "scripts" / "dts" / "gen_defines.py"
    if not gen_defines.exists():
        return
    if patch_framework_file(
        gen_defines, "  {edt.dts_path}", "  {os.path.basename(edt.dts_path)}"
    ):
        return
    if "{os.path.basename(edt.dts_path)}" not in gen_defines.read_text(
        encoding="utf-8"
    ):
        # Upstream reformatted the comment; sharing silently degrading
        # would be invisible, so say it out loud.
        _LOGGER.warning(
            "gen_defines.py no longer matches; the devicetree header "
            "stays per device and ccache sharing between devices degrades"
        )


# pyOCD 0.44 to 0.46 write the nRF54LM20A RRAM without its write buffer, which faults on the first
# store (pyocd/pyOCD#2016), and the Seeed XIAO nRF54LM20A boards flash through pyOCD. This is the
# flash algorithm of pyocd/pyOCD#2033, which fixes it, put in place until a release carries it.
_PYOCD_NRF54LM20A_FLASH_ALGO = """\
FLASH_ALGO = {
    'load_address' : 0x20000000,

    # Flash algorithm as a hex string
    'instructions': [
        0xe7fdbe00,
        0xf8d24a02, 0x07db3400, 0x4770d5fb, 0x5004e000, 0xf7ffb508, 0x2000fff5, 0x2000bd08, 0x00004770,
        0x49072001, 0xf8c1b508, 0xf7ff0500, 0xf8c1ffe9, 0x20000540, 0xffe4f7ff, 0x0500f8c1, 0xbf00bd08,
        0x5004e000, 0xf242b508, 0x49080301, 0x3500f8c1, 0xffd6f7ff, 0x33fff04f, 0x23016003, 0x608b2000,
        0xffcef7ff, 0x0500f8c1, 0xbf00bd08, 0x5004e000, 0xf242b538, 0x46140301, 0xf0214d0c, 0xf8c50103,
        0xf7ff3500, 0x4622ffbd, 0x1b004421, 0xeb02428a, 0xd1070300, 0x20002301, 0xf7ff60ab, 0xf8c5ffb1,
        0xbd380500, 0x4b04f852, 0xe7ef601c, 0x5004e000,
    ],

    # Relative function addresses
    'pc_init': 0x20000015,
    'pc_unInit': 0x2000001f,
    'pc_program_page': 0x20000075,
    'pc_erase_sector': 0x20000049,
    'pc_eraseAll': 0x20000025,

    'static_base' : 0x200000B8,
    'begin_stack' : 0x20003000,
    'page_size' : 0x1000,
    'analyzer_supported' : False,
    'analyzer_address' : 0x00000000,
    'page_buffers' : [0x20000100,0x20001100],   # 4 KiB double buffers, 128-bit line aligned
    'min_program_length' : 0x4,

    # Relative region addresses and sizes
    'ro_start': 0xb4,
    'ro_size': 0x0,
    'rw_start': 0xb4,
    'rw_size': 0x0,
    'zi_start': 0xb4,
    'zi_size': 0x0,

    # Flash information
    'flash_start': 0x0,
    'flash_size': 0x1fd000,
    'sector_sizes': (
        (0x0, 0x1fd000),
        (0xffd000, 0x1000),
    )
}
"""
_PYOCD_FLASH_ALGO_RE = re.compile(r"^FLASH_ALGO = \{.*?^\}\n", re.MULTILINE | re.DOTALL)
# An instruction word of the faulting algorithm only; an algorithm without it is left alone
_PYOCD_NRF54LM20A_FAULTY_WORD = "0x68134a02"


def penv_site_packages(penv_path: Path) -> Path:
    if os.name == "nt":
        return penv_path / "Lib" / "site-packages"
    python_dir = f"python{sys.version_info.major}.{sys.version_info.minor}"
    return penv_path / "lib" / python_dir / "site-packages"


def pyocd_builtin_target(python_env_path: Path, target_file: str) -> Path:
    """PyOCD's built-in target file in the penv, e.g. target_nRF54L15.py."""
    site_packages = penv_site_packages(python_env_path)
    if not site_packages.is_dir():
        # An env kept across an ESPHome Python upgrade still has its own Python version
        site_packages = next(
            python_env_path.glob("lib/python3.*/site-packages"), site_packages
        )
    return site_packages / "pyocd" / "target" / "builtin" / target_file


def _pyocd_nrf54lm20a_target(python_env_path: Path) -> Path:
    return pyocd_builtin_target(python_env_path, "target_nRF54LM20A.py")


def unpatched_pyocd_nrf54lm20a_target(python_env_path: Path) -> Path | None:
    """PyOCD's nRF54LM20A target file when it has the faulting flash algorithm, else None."""
    target = _pyocd_nrf54lm20a_target(python_env_path)
    if target.is_file() and _PYOCD_NRF54LM20A_FAULTY_WORD in target.read_text(
        encoding="utf-8"
    ):
        return target
    return None


def patch_pyocd_nrf54lm20a_flash_algo(python_env_path: Path) -> None:
    target = _pyocd_nrf54lm20a_target(python_env_path)
    if not target.is_file():
        _LOGGER.debug(
            "pyOCD has no nRF54LM20A target at %s; not patching its flash algorithm",
            target,
        )
        return
    content = target.read_text(encoding="utf-8")
    if _PYOCD_NRF54LM20A_FAULTY_WORD not in content:
        return
    if (match := _PYOCD_FLASH_ALGO_RE.search(content)) is None:
        _LOGGER.warning(
            "Could not patch pyOCD's nRF54LM20A flash algorithm in %s", target
        )
        return
    patch_framework_file(target, match.group(0), _PYOCD_NRF54LM20A_FLASH_ALGO)
    _LOGGER.info("Patched pyOCD's nRF54LM20A flash algorithm in %s", target)


def ensure_venv(python_env_path: Path, requirements: Path, label: str) -> bool:
    """Build the penv from ``requirements`` unless it is current.

    Returns True when it was (re)built.
    """
    env_python_path = get_python_env_executable_path(python_env_path, "python")
    sentinel = python_env_path / ".ready"
    requirements_hash = hashlib.sha256(requirements.read_bytes()).hexdigest()
    if not needs_venv_rebuild(env_python_path, sentinel, requirements_hash):
        return False
    rmdir(python_env_path, msg=f"Clean up {label} Python environment")

    create_venv(python_env_path, msg=label)

    install_sitecustomize(python_env_path)

    _LOGGER.info("Installing requirements ...")
    cmd = [
        str(env_python_path),
        "-m",
        "pip",
        "install",
        "-r",
        str(requirements),
    ]
    if not run_command_ok(cmd):
        raise EsphomeError(
            f"Install requirements for {label} Python environment failure"
        )
    sentinel.write_text(requirements_hash, encoding="utf-8")
    return True


@dataclass(frozen=True)
class FrameworkSource:
    """Where a framework comes from, and which of its projects a workspace fetches."""

    # The framework's name in messages
    name: str
    # The repository it is cloned from
    url: str
    # Projects (patterns) fetched only when a build needs them
    optional: tuple[str, ...]
    # Projects every build fetches
    required: frozenset[str]


# The projects a finished install fetched
_WEST_PROJECTS_FILE: str = ".west_projects"


def _set_project_filter(
    env_python_path: Path,
    framework_path: Path,
    source: FrameworkSource,
    projects: set[str],
) -> bool:
    project_filter = ",".join(
        [*(f"-{p}" for p in source.optional), *(f"+{p}" for p in sorted(projects))]
    )
    # "--" keeps west from reading the leading "-" as an option
    cmd = [str(env_python_path), "-m", "west", "config", "manifest.project-filter"]
    return run_command_ok([*cmd, "--", project_filter], cwd=framework_path)


def _check_west_projects(
    env_python_path: Path,
    framework_path: Path,
    source: FrameworkSource,
    version: str,
    projects: set[str],
) -> None:
    """Raise when the manifest lacks one of ``projects`` beyond the required ones.

    west update quietly skips an unknown name in the filter, west list fails on it.
    Needs the workspace updated once, since a manifest may import others. A required
    project a later version dropped only leaves its filter entry unused.
    """
    if not (names := sorted(projects - source.required)):
        return
    cmd = [str(env_python_path), "-m", "west", "list", "-f", "{name}", *names]
    if not run_command_ok(cmd, cwd=framework_path):
        raise EsphomeError(
            f"west list failed for the requested {source.name} {version} projects "
            f"({', '.join(names)}); a project the manifest does not have is the "
            "usual cause, see west's output above"
        )


def _west_update(
    env_python_path: Path,
    framework_path: Path,
    source: FrameworkSource,
    version: str,
    projects: set[str],
    *,
    only: set[str] | None = None,
) -> bool:
    """Fetch ``projects``, or just ``only`` of them; False when the fetch fails.

    ``only`` is for a finished workspace, whose names are checked already.
    """
    if not _set_project_filter(env_python_path, framework_path, source, projects):
        return False
    cmd = [
        str(env_python_path),
        "-m",
        "west",
        "update",
        "--narrow",
        "--fetch-opt=--depth=1",
        *sorted(only or ()),
    ]
    # Streamed so the clone's progress reaches the log
    if not run_command_ok(cmd, cwd=framework_path, stream_output=True):
        return False
    if only is None:
        _check_west_projects(env_python_path, framework_path, source, version, projects)
    (framework_path / _WEST_PROJECTS_FILE).write_text(
        "\n".join(sorted(projects)), encoding="utf-8"
    )
    return True


def _installed_west_projects(framework_path: Path) -> set[str] | None:
    """The projects a finished install fetched; None when it has every project."""
    try:
        stamp = (framework_path / _WEST_PROJECTS_FILE).read_text(encoding="utf-8")
    except FileNotFoundError:
        pass
    else:
        return set(stamp.split())
    # No stamp: an install from before the filter has every project, a filtered
    # one that lost its stamp fetches again
    config = configparser.ConfigParser()
    if not config.read(framework_path / ".west" / "config", encoding="utf-8"):
        return set()
    if config.has_option("manifest", "project-filter"):
        return set()
    return None


def _restore_project_filter(
    env_python_path: Path,
    framework_path: Path,
    source: FrameworkSource,
    version: str,
    installed: set[str],
) -> None:
    """Put the filter back to the stamp's projects; the required ones always stay in."""
    projects = installed | source.required
    if not _set_project_filter(env_python_path, framework_path, source, projects):
        _LOGGER.warning(
            "Couldn't put the %s %s project filter back; "
            "the next build that fetches a project sets it again",
            source.name,
            version,
        )


def fetch_missing_west_projects(
    env_python_path: Path,
    framework_path: Path,
    source: FrameworkSource,
    version: str,
    projects: set[str],
) -> None:
    """Fetch the wanted projects a finished install lacks; it only ever gains.

    Only the missing projects are updated, so a build running from the others
    is left alone. Call under the workspace's install lock.
    """
    if projects <= source.required:
        return
    installed = _installed_west_projects(framework_path)
    # Before the fetch, so an unknown name costs nothing on any install
    _check_west_projects(env_python_path, framework_path, source, version, projects)
    if installed is None or not (missing := projects - installed):
        return
    _LOGGER.info(
        "Fetching %s %s projects: %s",
        source.name,
        version,
        ", ".join(sorted(missing)),
    )
    if not _west_update(
        env_python_path,
        framework_path,
        source,
        version,
        installed | projects,
        only=missing,
    ):
        _restore_project_filter(
            env_python_path, framework_path, source, version, installed
        )
        raise EsphomeError(f"Can't update {source.name} {version}")


def _pin_manifest_repo(framework_path: Path, pin: str) -> bool:
    """Point the manifest repository at commit ``pin``; False when git fails.

    ``west init --mr`` can't take a commit and ``west update`` never moves the
    manifest repository, so this is done with git before ``west update`` reads it.
    """
    config = configparser.ConfigParser()
    config.read(framework_path / ".west" / "config", encoding="utf-8")
    manifest_dir = framework_path / config.get("manifest", "path", fallback="zephyr")
    _LOGGER.info("Pinning the manifest repository to %s ...", pin[:12])
    return run_command_ok(
        ["git", "fetch", "--depth=1", "--", "origin", pin], cwd=manifest_dir
    ) and run_command_ok(["git", "reset", "--hard", "FETCH_HEAD"], cwd=manifest_dir)


def install_framework(
    env_python_path: Path,
    framework_path: Path,
    source: FrameworkSource,
    version: str,
    projects: set[str],
    *,
    pin: str | None = None,
) -> None:
    """Clone the SDK into ``framework_path`` with west, fetching ``projects``.

    A download cut short after ``west init`` leaves the workspace behind;
    rerunning ``west update`` there only fetches what is missing, so it resumes
    instead of cloning again. A resume that fails keeps what was fetched (a
    flaky network is the likely cause) and is retried on the next build; only
    a second failure in a row starts over clean. Call under the workspace's
    install lock.

    With ``pin`` (a commit), the default branch is cloned and the manifest
    repository moved to ``pin`` before every update; ``version`` is then only a label.
    """
    name = source.name
    resume_failed = framework_path / ".resume_failed"
    # Resume only a workspace whose ``west init`` finished (it writes the
    # config last). ``.ready`` with missing requirements is a damaged install,
    # not an interrupted one, so it goes the clean way.
    initialized = (framework_path / ".west" / "config").is_file()
    if initialized and not (framework_path / ".ready").exists():
        _LOGGER.info("Resuming the %s %s download ...", name, version)
        # Pinned again: the download may have stopped between init and the pin
        if (pin is None or _pin_manifest_repo(framework_path, pin)) and _west_update(
            env_python_path, framework_path, source, version, projects
        ):
            resume_failed.unlink(missing_ok=True)
            return
        if not resume_failed.exists():
            resume_failed.touch()
            raise EsphomeError(
                f"Can't resume the {name} {version} download; the next build retries it"
            )
        _LOGGER.warning("Resuming failed again; downloading %s %s anew", name, version)
    rmdir(framework_path, msg=f"Clean up {version} framework environment")
    _LOGGER.info("Initializing %s %s ...", name, version)
    cmd = [str(env_python_path), "-m", "west", "init", "-m", source.url, "-o=--depth=1"]
    if pin is None:
        cmd += ["--mr", version]
    cmd.append(str(framework_path))
    if not run_command_ok(cmd, stream_output=True):
        raise EsphomeError(f"Can't initialize {name} {version}")
    if pin is not None and not _pin_manifest_repo(framework_path, pin):
        raise EsphomeError(f"Can't pin {name} {version} to {pin}")
    _LOGGER.info("Updating %s %s (this may take a while) ...", name, version)
    if not _west_update(env_python_path, framework_path, source, version, projects):
        raise EsphomeError(f"Can't update {name} {version}")


_PATCHED_SCRIPTS: tuple[Path, ...] = (
    Path("scripts") / "dts" / "gen_defines.py",
    Path("scripts") / "west_commands" / "build.py",
)


def _restore_patched_scripts(framework_path: Path) -> None:
    """Undo the script patches; a modified tracked file can block west's checkout."""
    zephyr_dir = framework_path / "zephyr"
    for script in _PATCHED_SCRIPTS:
        if (zephyr_dir / script).is_file():
            run_command_ok(["git", "checkout", "--", script.as_posix()], cwd=zephyr_dir)


def refresh_framework(
    env_python_path: Path,
    framework_path: Path,
    source: FrameworkSource,
    version: str,
    projects: set[str],
    pin: str,
) -> None:
    """Move a finished workspace to commit ``pin`` and update every project.

    ``.ready`` goes first, so a failure leaves an unfinished install that the
    next build resumes (and pins again). The caller writes ``.ready`` after.
    Call under the workspace's install lock.
    """
    (framework_path / ".ready").unlink(missing_ok=True)
    _restore_patched_scripts(framework_path)
    _LOGGER.info("Updating %s %s to %s ...", source.name, version, pin[:12])
    if not _pin_manifest_repo(framework_path, pin) or not _west_update(
        env_python_path, framework_path, source, version, projects
    ):
        raise EsphomeError(
            f"Can't update {source.name} {version} to {pin}; the next build retries it"
        )


def install_local_framework(env_python_path: Path, checkout: Path, name: str) -> Path:
    """Use the user's own checkout; its folder's parent is the workspace, returned.

    ``west update`` runs every time, since the checkout may change between
    builds. Nothing is ever deleted or patched there and no project filter is
    set: it is the user's tree. A failure raises and the next build retries.
    """
    workspace = checkout.parent
    label = str(checkout)
    if not (workspace / ".west").is_dir():
        _LOGGER.info("Initializing the %s workspace for %s ...", name, label)
        cmd = [str(env_python_path), "-m", "west", "init", "-l", checkout.name]
        if not run_command_ok(cmd, cwd=workspace):
            raise EsphomeError(f"Can't initialize the {name} workspace for {label}")
    _LOGGER.info("Updating the %s workspace for %s ...", name, label)
    cmd = [
        str(env_python_path),
        "-m",
        "west",
        "update",
        "--narrow",
        "--fetch-opt=--depth=1",
    ]
    if not run_command_ok(cmd, cwd=workspace, stream_output=True):
        raise EsphomeError(
            f"Can't update the {name} workspace for {label}; the next build retries it"
        )
    return workspace


def ensure_zephyr_requirements(
    python_env_path: Path,
    framework_path: Path,
    venv_rebuilt: bool,
    label: str,
    *,
    zephyr_dir: Path | None = None,
) -> None:
    """Install the workspace's Zephyr Python requirements into the penv unless current,
    then patch pyOCD's nRF54LM20A flash algorithm.

    ``zephyr_dir`` is the Zephyr tree when it isn't ``framework_path/zephyr``
    (a local checkout).
    """
    zephyr_reqs = (
        (zephyr_dir or framework_path / "zephyr") / "scripts" / "requirements.txt"
    )
    # Inside the penv, so a rebuild removes it
    sentinel = python_env_path / ".zephyr_reqs_ready"
    if (
        venv_rebuilt
        or not sentinel.exists()
        or zephyr_reqs.stat().st_mtime > sentinel.stat().st_mtime
    ):
        _LOGGER.info("Installing Zephyr requirements ...")
        env_python_path = get_python_env_executable_path(python_env_path, "python")
        cmd = [str(env_python_path), "-m", "pip", "install", "-r", str(zephyr_reqs)]
        if not run_command_ok(cmd):
            raise EsphomeError(f"Install Zephyr requirements for {label} failure")
        sentinel.touch()
    # Every build, as a pip install can put the faulting file back
    patch_pyocd_nrf54lm20a_flash_algo(python_env_path)
