import configparser
import hashlib
import logging
import os
from pathlib import Path
import re
import subprocess
import time

import yaml

from esphome.build_helpers.tools_cache import ZEPHYR_TOOLS_CACHE, tools_cache_path
from esphome.const import CONF_PATH, CONF_REF, CONF_TYPE, CONF_URL, TYPE_LOCAL
from esphome.core import EsphomeError
from esphome.framework_helpers import (
    get_python_env_executable_path,
    install_lock,
    rmdir,
    run_command_ok,
)
from esphome.types import ConfigType

from .const import COMMIT_SHA_RE, KEY_SDK_SOURCE_RESOLVED_REF
from .framework_helpers import (
    ensure_venv,
    patch_gen_defines_dts_path,
    patch_west_build_source_dir_relpath,
)
from .sdk_install import ensure_sdk
from .variants import ZephyrModule, ZephyrSDK

_LOGGER = logging.getLogger(__name__)

_REQUIREMENTS: Path = Path(__file__).parent / "requirements.txt"
# The SDK versions in use are unknown; a partial download untouched this long has no owner,
# so a download interrupted more than a day ago starts over
_STALE_DOWNLOAD_AGE: int = 24 * 60 * 60


def get_zephyr_tools_path() -> Path:
    # Machine-global (OS user cache dir) so all Zephyr-based projects share one
    # SDK/toolchain/west install; see build_helpers.tools_cache.tools_cache_path
    # for the env-override and normalization rules.
    return tools_cache_path(*ZEPHYR_TOOLS_CACHE)


def _get_python_env_path(version: str) -> Path:
    return get_zephyr_tools_path() / "penvs" / version


def _get_toolchain_path(sdk_version: str, toolchain: str) -> Path:
    return (
        get_zephyr_tools_path() / "toolchains" / f"zephyr-sdk-{sdk_version}-{toolchain}"
    )


def _framework_path(cache_key: str) -> Path:
    return get_zephyr_tools_path() / "frameworks" / cache_key


def _source_cache_key(
    sdk: ZephyrSDK,
    ver_tag: str,
    source: ConfigType | None,
) -> str:
    """Distinguish a custom framework: source: from the official ver_tag-keyed cache dir,
    and from other framework: source: configs sharing the same version: value.

    sdk.workspace_prefix prefixes the key so MAINLINE/NCS/SILABS -- which now share one
    machine-global cache root -- can't collide on a matching version: string (e.g. NCS's
    bundled Zephyr fork tracks close to mainline's own numbering)."""
    if source is None:
        key = f"{sdk.workspace_prefix}-{ver_tag}"
    elif source[CONF_TYPE] == TYPE_LOCAL:
        path_hash = hashlib.sha1(str(source[CONF_PATH]).encode()).hexdigest()[:8]
        key = f"{sdk.workspace_prefix}-{ver_tag}-local-{path_hash}"
    else:
        # TYPE_GIT
        ref = source.get(CONF_REF) or "HEAD"
        safe_ref = re.sub(r"[^A-Za-z0-9_.-]", "_", ref)
        url_hash = hashlib.sha1(source[CONF_URL].encode()).hexdigest()[:8]
        key = f"{sdk.workspace_prefix}-{ver_tag}-{safe_ref}-{url_hash}"
    return key


def _effective_requirements(overrides: dict[str, str | None]) -> str:
    """Return zephyr/requirements.txt's content, substituting the version on any pinned
    `pkg==...` line whose package name has a non-None override (e.g. {"west": "1.4.0"},
    from CONF_WEST_VERSION/CONF_NINJA_VERSION)."""
    text = _REQUIREMENTS.read_text(encoding="utf-8")
    if not any(overrides.values()):
        return text
    lines = []
    for line in text.splitlines():
        pkg = line.split("==", 1)[0]
        override = overrides.get(pkg)
        lines.append(f"{pkg}=={override}" if override else line)
    return "\n".join(lines) + "\n"


def _is_commit_sha(rev: str | None) -> bool:
    return rev is not None and COMMIT_SHA_RE.fullmatch(rev) is not None


# Projects in Zephyr's hal group that match "hal_.*" but belong to no SoC family:
# Xtensa, the Arm Ethos-U NPU, and the external chip libraries any board can carry
_NEUTRAL_HALS = frozenset(
    {
        "hal_afbr",
        "hal_ethos_u",
        "hal_infineon",
        "hal_st",
        "hal_tdk",
        "hal_wurthelektronik",
        "hal_xtensa",
    }
)
# BabbleSim's nRF models; their module needs hal_nordic, and no variant builds for BabbleSim
_EXCLUDED_PROJECTS = ("nrf_hw_models",)
# The vendor HALs a finished `west update` fetched
_WEST_HALS_FILE = ".west_hals"


def _set_hal_filter(python_bin: Path, framework: Path, hals: set[str]) -> bool:
    """Make every vendor HAL inactive except `hals`; other projects keep their groups."""
    value = ",".join(
        [
            "-hal_.*",
            *(f"-{p}" for p in _EXCLUDED_PROJECTS),
            *(f"+{h}" for h in sorted(hals | _NEUTRAL_HALS)),
        ]
    )
    # "--" keeps west from reading the leading "-" as an option
    cmd = [str(python_bin), "-m", "west", "config", "manifest.project-filter"]
    return run_command_ok([*cmd, "--", value], cwd=str(framework))


def _check_hals(python_bin: Path, framework: Path, hals: set[str]) -> None:
    """Raise when the manifest lacks one of `hals`; west update skips an unknown
    name quietly."""
    names = sorted(hals)
    cmd = [str(python_bin), "-m", "west", "list", "-f", "{name}", *names]
    if not run_command_ok(cmd, cwd=str(framework)):
        raise EsphomeError(
            f"west list failed for the Zephyr HALs {', '.join(names)}; a HAL the "
            "manifest does not have is the usual cause, see west's output above"
        )


def _installed_hals(framework: Path) -> set[str] | None:
    """The vendor HALs a finished install fetched; None when it has every HAL."""
    try:
        stamp = (framework / _WEST_HALS_FILE).read_text(encoding="utf-8")
    except FileNotFoundError:
        pass
    else:
        return set(stamp.split())
    # No stamp: a finished install from before the filter has every HAL; a filtered
    # or unfinished one fetches again
    config = configparser.ConfigParser()
    config.read(framework / ".west" / "config", encoding="utf-8")
    if (
        config.has_option("manifest", "project-filter")
        or not (framework / ".ready").exists()
    ):
        return set()
    return None


def _manifest_repo_dir(framework: Path) -> Path:
    """The manifest repository's directory, as recorded by `west init` in .west/config.

    It is the manifest's own `self: path:` (Zephyr: "zephyr", NCS: "nrf"), else the URL's
    basename -- not always framework/zephyr.
    """
    config = configparser.ConfigParser()
    config.read(framework / ".west" / "config")
    return framework / config.get("manifest", "path")


def _pin_manifest_repo(manifest_dir: Path, sha: str, label: str) -> None:
    """Point the manifest repository at `sha`.

    `west init --mr` can't take a commit SHA (it uses `git clone --branch`), and `west
    update` never alters the manifest repository, so this is done with git before
    `west update` reads the manifest from it.
    """
    _LOGGER.info("Pinning Zephyr manifest repository to %s ...", label)
    if not run_command_ok(
        ["git", "fetch", "--depth=1", "--", "origin", sha], cwd=str(manifest_dir)
    ):
        raise EsphomeError(f"Can't fetch Zephyr manifest repository ({label})")
    if not run_command_ok(
        ["git", "reset", "--hard", "FETCH_HEAD"], cwd=str(manifest_dir)
    ):
        raise EsphomeError(f"Can't update Zephyr manifest repository ({label})")


def _extra_projects(path: Path, sdk_url: str) -> list[str]:
    """The projects a module's own west.yml lists, other than the SDK itself."""
    west_yml = path / "west.yml"
    if not west_yml.is_file():
        return []
    loaded = yaml.safe_load(west_yml.read_text(encoding="utf-8")) or {}
    manifest = loaded.get("manifest") or {}
    url_bases = {r.get("name"): r.get("url-base") for r in manifest.get("remotes", [])}
    default_remote = (manifest.get("defaults") or {}).get("remote")

    def normalized(url: str) -> str:
        return url.rstrip("/").removesuffix(".git")

    extra = []
    for project in manifest.get("projects", []):
        name = project.get("name")
        url = project.get("url") or (
            f"{url_bases.get(project.get('remote', default_remote))}/"
            f"{project.get('repo-path', name)}"
        )
        if normalized(url) != normalized(sdk_url):
            extra.append(name)
    return extra


def _git_output(cmd: list[str], cwd: Path | None = None) -> str | None:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    return result.stdout if result.returncode == 0 else None


def _remote_commit(url: str, rev: str) -> str | None:
    """The commit a branch or tag names on the remote; None for a commit SHA, or when
    the remote can't be reached."""
    if (out := _git_output(["git", "ls-remote", "--", url, rev])) is None:
        return None
    commits = dict(reversed(line.split("\t", 1)) for line in out.splitlines())
    # An annotated tag's commit is on its peeled ^{} line
    for ref in (f"refs/heads/{rev}", f"refs/tags/{rev}^{{}}", f"refs/tags/{rev}"):
        if ref in commits:
            return commits[ref]
    return None


def _module_is_current(sentinel: Path, refresh: float | None) -> bool:
    return sentinel.exists() and (
        refresh is None or time.time() - sentinel.stat().st_mtime < refresh
    )


def install_git_modules(modules: list[ZephyrModule], sdk_url: str) -> list[Path]:
    """Fetch each git module into its own folder and return the folders, for
    EXTRA_ZEPHYR_MODULES. A fetched module's revision is checked again once its
    refresh time has passed. sdk_url is the SDK the build uses, the one project a
    module's own west.yml may list."""
    paths = []
    for module in modules:
        if module.manifest_url is None:
            continue
        safe_rev = re.sub(r"[^A-Za-z0-9_.-]", "_", module.revision)
        url_hash = hashlib.sha1(module.manifest_url.encode()).hexdigest()[:8]
        key = f"{module.name}-{safe_rev}-{url_hash}"
        path = get_zephyr_tools_path() / "modules" / key
        paths.append(path)
        sentinel = path / ".git" / "esphome-ready"
        if _module_is_current(sentinel, module.refresh):
            continue
        with install_lock(get_zephyr_tools_path(), key):
            # another build fetched or checked it while this one waited
            if _module_is_current(sentinel, module.refresh):
                continue
            url, rev = str(module.manifest_url), str(module.revision)
            if sentinel.exists():
                commit = _remote_commit(url, rev)
                head = _git_output(["git", "rev-parse", "HEAD"], cwd=path)
                # Unchanged, a commit SHA, or offline: keep the folder until next time
                if commit is None or head is None or commit == head.strip():
                    sentinel.touch()
                    continue
                # In place, so a build using it at this moment can see files change,
                # as with board_source: (git.clone_or_update)
                _LOGGER.info("Updating Zephyr module %s %s ...", module.name, rev)
                cmds = []
            else:
                _LOGGER.info("Fetching Zephyr module %s %s ...", module.name, rev)
                rmdir(path, msg=f"Clean up Zephyr module {key}")
                path.mkdir(parents=True)
                cmds = [["git", "init", "-q"]]
            # fetch, not clone --branch: the revision can also be a commit SHA
            cmds += [
                ["git", "fetch", "--depth=1", "--", url, rev],
                ["git", "checkout", "-q", "FETCH_HEAD"],
            ]
            for cmd in cmds:
                if not run_command_ok(cmd, cwd=str(path)):
                    raise EsphomeError(f"Can't fetch Zephyr module {module.name} {rev}")
                if cmd[1] == "fetch":
                    # From here a failure leaves a checkout the checks below never saw
                    sentinel.unlink(missing_ok=True)
            # `west blobs fetch` only reaches modules inside the workspace
            for name in ("module.yml", "module.yaml"):
                module_yml = path / "zephyr" / name
                if module_yml.is_file() and (
                    yaml.safe_load(module_yml.read_text(encoding="utf-8")) or {}
                ).get("blobs"):
                    raise EsphomeError(
                        f"Zephyr module {module.name} needs binary blobs, which "
                        "add-on modules can't fetch yet"
                    )
            # The workspace is the SDK alone, so nothing else in west.yml is fetched
            if extra := _extra_projects(path, sdk_url):
                raise EsphomeError(
                    f"Zephyr module {module.name} needs other projects "
                    f"({', '.join(extra)}), which add-on modules can't fetch yet"
                )
            sentinel.touch()
    return paths


def check_and_install(
    sdk: ZephyrSDK,
    version: str,
    west_version: str | None = None,
    ninja_version: str | None = None,
    source: ConfigType | None = None,
    hals: frozenset[str] | None = None,
) -> tuple[Path, Path, dict[str, str]]:
    """Install west and Zephyr SDK, serialized against other builds installing
    the same workspace at once (see install_lock)."""
    ver_tag = f"v{version}" if not version.startswith("v") else version
    # Named after the workspace folder, so other workspaces of this version don't wait
    with install_lock(get_zephyr_tools_path(), _source_cache_key(sdk, ver_tag, source)):
        return _check_and_install(
            sdk, version, west_version, ninja_version, source, hals
        )


def _check_and_install(
    sdk: ZephyrSDK,
    version: str,
    west_version: str | None,
    ninja_version: str | None,
    source: ConfigType | None,
    hals: frozenset[str] | None,
) -> tuple[Path, Path, dict[str, str]]:
    """Install west and Zephyr SDK.

    west_version/ninja_version override zephyr/requirements.txt's pinned versions
    (CONF_WEST_VERSION/CONF_NINJA_VERSION). source, if given (framework: source:), overrides
    which Zephyr gets fetched -- a git fork/branch/ref (cloned into ESPHome's own managed
    cache, same as the official source), or a `local:` path to a manifest repo (e.g. a Zephyr
    fork checked out for local development). `local:` deliberately does NOT copy or symlink
    the checkout into ESPHome's cache -- west resolves symlinks when computing a workspace's
    topdir, so a symlink doesn't actually keep writes out of the real directory, and a copy
    would silently go stale against the user's live edits. Instead `west init -l` / `west
    update` run directly against the given path's own parent directory, which becomes the
    workspace topdir; `modules/`, `bootloader/`, etc. land there as siblings of the checkout,
    not inside ESPHome's own `.esphome/` tree. See project_zephyr_local_sdk_source_docs_todo
    for the write-up this needs once real docs exist (recommend a dedicated directory per
    checkout, not reusing an existing unrelated working tree).

    A git source is pinned to one resolved commit SHA before this function runs (see
    dts_fetch.resolve_sdk_source_version(), which stashes it onto source under
    KEY_SDK_SOURCE_RESOLVED_REF). The workspace is keyed by ref name, so when a moved ref
    resolves to a new SHA the manifest repository is re-pointed and `west update` re-run
    in place. `local:` always re-runs `west update` (cheap/no-op if nothing changed,
    since the user's checkout can move between builds); the official source is pinned
    to an immutable tag and never refreshes.

    hals: the vendor HAL projects this build needs; the other vendor HALs are not
    fetched. A workspace only gains HALs, since variants of one version share it.
    None (NCS, Silicon Labs' SDK) leaves the project filter alone.

    Returns (python_bin, framework_path, west_env) where:
      - python_bin      — Python executable inside the managed venv
      - framework_path  — west workspace root (contains zephyr/, modules/, …)
      - west_env        — environment dict with ZEPHYR_BASE and PATH set
    """
    ver_tag = f"v{version}" if not version.startswith("v") else version
    python_env = _get_python_env_path(ver_tag)
    python_bin = get_python_env_executable_path(python_env, "python")

    is_local = source is not None and source[CONF_TYPE] == TYPE_LOCAL
    if is_local:
        local_path = source[
            CONF_PATH
        ]  # already a resolved Path -- cv.directory's return type
        if not (local_path / "west.yml").is_file():
            raise EsphomeError(
                f"'{local_path}' does not look like a Zephyr manifest repo (no west.yml) -- "
                "sdk_source: local: expects a path to a Zephyr checkout, e.g. a fork you're "
                "developing on."
            )
        framework = local_path.parent
        zephyr_dir = local_path
    else:
        framework = _framework_path(_source_cache_key(sdk, ver_tag, source))
        zephyr_dir = framework / "zephyr"

    west_env = {
        **os.environ,
        "ZEPHYR_BASE": str(zephyr_dir),
        "PATH": f"{python_bin.parent}{os.pathsep}{os.environ.get('PATH', '')}",
    }

    # Every workspace of this version shares the venv. It only changes when the west
    # requirements or overrides do, so builds that use it are not locked against that.
    with install_lock(get_zephyr_tools_path(), f"penv-{ver_tag}"):
        overrides = {"west": west_version, "ninja": ninja_version}
        # Beside the venv, not in it: a rebuild removes the venv folder
        requirements = python_env.with_name(f"{python_env.name}.requirements.txt")
        requirements.parent.mkdir(parents=True, exist_ok=True)
        requirements.write_text(_effective_requirements(overrides))
        install_venv = ensure_venv(python_env, requirements, ver_tag)

    manifest_url = (
        source[CONF_URL] if (source is not None and not is_local) else sdk.manifest_url
    )
    manifest_rev = (
        source.get(KEY_SDK_SOURCE_RESOLVED_REF, source.get(CONF_REF))
        if (source is not None and not is_local)
        else ver_tag
    )
    label = str(source[CONF_PATH]) if is_local else (manifest_rev or "default branch")

    sentinel = framework / ".ready"
    needs_init = not (framework / ".west").is_dir()
    # .ready is only written after a complete `west update`; without it a previous
    # update was interrupted and the workspace may be half-fetched.
    incomplete = not needs_init and not sentinel.exists()
    # local: (a user-edited checkout) can change between builds, so always re-run
    # `west update` -- cheap/no-op if unchanged. The official source is pinned to an
    # immutable tag and never needs this.
    # A rebuilt venv only needs an update, not a fresh clone of the workspace.
    needs_refresh = is_local or incomplete or install_venv

    # A git source: is pinned to one commit per run (dts_fetch.resolve_sdk_source_version()),
    # but the workspace directory is keyed by ref name, so a moved branch must be
    # re-pointed in place. The commit last fetched is recorded in a marker file.
    pinned = source is not None and not is_local and _is_commit_sha(manifest_rev)
    resolved_marker = framework / ".resolved_ref"
    if (
        pinned
        and not needs_init
        and (
            not resolved_marker.is_file() or resolved_marker.read_text() != manifest_rev
        )
    ):
        needs_refresh = True

    hal_filter: set[str] | None = None
    installed_hals: set[str] | None = None
    if hals is not None and not is_local:
        wanted = hals | _NEUTRAL_HALS
        installed_hals = set() if needs_init else _installed_hals(framework)
        if installed_hals is not None and not wanted <= installed_hals:
            hal_filter = installed_hals | wanted
    # Only new HALs: they alone are updated, so a build running from zephyr is left alone
    grow_only = hal_filter is not None and not (needs_init or needs_refresh)

    if needs_init:
        if is_local:
            # install_venv alone (framework/.west already present) doesn't need a re-init --
            # `west init` refuses to run against an already-initialized workspace.
            if not (framework / ".west").is_dir():
                _LOGGER.info("Initializing Zephyr SDK workspace for %s ...", label)
                cmd = [str(python_bin), "-m", "west", "init", "-l", local_path.name]
                if not run_command_ok(cmd, cwd=str(framework)):
                    raise EsphomeError(
                        f"Can't initialize Zephyr SDK workspace ({label})"
                    )
        else:
            _LOGGER.info("Initializing Zephyr SDK %s (%s) ...", ver_tag, label)
            rmdir(framework, msg=f"Clean up {ver_tag} framework")
            cmd = [
                str(python_bin),
                "-m",
                "west",
                "init",
                "-o=--depth=1",
                "-m",
                manifest_url,
            ]
            # A pinned SHA can't be a --mr value; init at the source's ref, then pin.
            init_rev = source.get(CONF_REF) if pinned else manifest_rev
            if init_rev:
                cmd += ["--mr", init_rev]
            cmd.append(str(framework))
            if not run_command_ok(cmd):
                raise EsphomeError(f"Can't initialize Zephyr SDK {ver_tag} ({label})")

    if pinned and (needs_init or needs_refresh):
        _pin_manifest_repo(_manifest_repo_dir(framework), manifest_rev, label)

    if needs_init or needs_refresh or grow_only:
        if incomplete:
            _LOGGER.info("Previous Zephyr SDK update did not finish; retrying")
        _LOGGER.info(
            "Updating Zephyr SDK %s (%s) (this may take a while) ...", ver_tag, label
        )
        # Cleared first so an interrupted update is retried on the next build; a grow
        # keeps it, since its HALs are missing from the stamp until the grow succeeds
        if not grow_only:
            sentinel.unlink(missing_ok=True)
        if not is_local and not grow_only:
            # A patched tracked file can block west update's checkout.
            _restore_patched_scripts(zephyr_dir)
        if hal_filter is not None:
            _check_hals(python_bin, framework, hals)
            _LOGGER.info("Zephyr HALs: %s", ", ".join(sorted(hal_filter)))
            if not _set_hal_filter(python_bin, framework, hal_filter):
                raise EsphomeError(f"Can't set the Zephyr SDK {ver_tag} project filter")
        cmd = [
            str(python_bin),
            "-m",
            "west",
            "update",
            "--narrow",
            "--fetch-opt=--depth=1",
        ]
        if grow_only:
            cmd += sorted(hal_filter - installed_hals)
        env = os.environ.copy()
        env.update(west_env)
        result = subprocess.run(cmd, env=env, cwd=str(framework), check=False)
        update_failed = framework / ".update_failed"
        if result.returncode != 0:
            # Back to the HALs on disk, so a later build of those alone fetches nothing
            if (
                hal_filter is not None
                and installed_hals
                and not _set_hal_filter(python_bin, framework, installed_hals)
            ):
                _LOGGER.warning(
                    "Couldn't put the Zephyr SDK %s project filter back", ver_tag
                )
            if grow_only:
                # The workspace is still complete; never start over for a missing HAL
                raise EsphomeError(
                    f"Can't fetch the Zephyr SDK {ver_tag} HALs ({label}); "
                    "the next build retries it"
                )
            # As nrf52: one failure is likely a flaky network and resumes;
            # a second in a row means a broken workspace, so start over.
            if is_local or not update_failed.exists():
                if not is_local:
                    update_failed.touch()
                raise EsphomeError(
                    f"Can't update Zephyr SDK {ver_tag} ({label}); the next build retries it"
                )
            rmdir(framework, msg=f"Clean up {ver_tag} framework")
            raise EsphomeError(
                f"Can't update Zephyr SDK {ver_tag} ({label}) twice in a row; "
                "the workspace was removed and the next build downloads it anew"
            )
        update_failed.unlink(missing_ok=True)
        if hal_filter is not None:
            (framework / _WEST_HALS_FILE).write_text(
                "\n".join(sorted(hal_filter)), encoding="utf-8"
            )

        if needs_init or pinned or incomplete or install_venv:
            zephyr_reqs = zephyr_dir / "scripts" / "requirements.txt"
            if zephyr_reqs.exists():
                _LOGGER.info("Installing Zephyr Python requirements ...")
                req_cmd = [
                    str(python_bin),
                    "-m",
                    "pip",
                    "install",
                    "-r",
                    str(zephyr_reqs),
                ]
                with install_lock(get_zephyr_tools_path(), f"penv-{ver_tag}"):
                    if not run_command_ok(req_cmd):
                        raise EsphomeError("Can't install Zephyr Python requirements")

        if pinned:
            resolved_marker.write_text(manifest_rev)
        sentinel.touch()

    if not is_local:
        # Never edit a sdk_source: local checkout; it is the user's own tree.
        patch_gen_defines_dts_path(framework)
        patch_west_build_source_dir_relpath(framework)
    return python_bin, framework, west_env


def _read_sdk_version(framework_path: Path) -> str:
    sdk_version_file = framework_path / "zephyr" / "SDK_VERSION"
    try:
        sdk_version = sdk_version_file.read_text(encoding="utf-8").strip()
    except OSError as err:
        raise EsphomeError(
            f"Can't read the Zephyr SDK version from {sdk_version_file}: {err}"
        ) from err
    if not sdk_version:
        raise EsphomeError(f"{sdk_version_file} names no Zephyr SDK version")
    return sdk_version


def install_toolchain(framework_path: Path, toolchain: str) -> Path:
    """Install the SDK named by the workspace's ``zephyr/SDK_VERSION``; returns its folder."""
    sdk_version = _read_sdk_version(framework_path)
    sdk_path = _get_toolchain_path(sdk_version, toolchain)
    cutoff = time.time() - _STALE_DOWNLOAD_AGE
    ensure_sdk(
        sdk_path,
        sdk_version,
        toolchain,
        keep_leftover=lambda leftover: leftover.stat().st_mtime >= cutoff,
        lock_root=get_zephyr_tools_path(),
    )
    return sdk_path


_GEN_DEFINES = Path("scripts") / "dts" / "gen_defines.py"
_WEST_BUILD = Path("scripts") / "west_commands" / "build.py"


def _restore_patched_scripts(zephyr_dir: Path) -> None:
    for script in (_GEN_DEFINES, _WEST_BUILD):
        if (zephyr_dir / script).is_file():
            subprocess.run(
                ["git", "-C", str(zephyr_dir), "checkout", "--", script.as_posix()],
                check=False,
                capture_output=True,
            )
