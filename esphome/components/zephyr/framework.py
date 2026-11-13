from dataclasses import replace
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
    ensure_zephyr_requirements,
    fetch_missing_west_projects,
    install_framework,
    install_local_framework,
    patch_gen_defines_dts_path,
    patch_west_build_source_dir_relpath,
    refresh_framework,
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

    sdk.workspace_prefix prefixes the key so ZEPHYR_MAINLINE/NCS/SILABS -- which now share one
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
    projects: frozenset[str] = frozenset(),
) -> tuple[Path, Path, dict[str, str]]:
    """Install west and the Zephyr SDK, serialized against other builds installing
    the same workspace at once (see install_lock)."""
    ver_tag = f"v{version}" if not version.startswith("v") else version
    # Named after the workspace folder, so other workspaces of this version don't wait
    with install_lock(get_zephyr_tools_path(), _source_cache_key(sdk, ver_tag, source)):
        return _check_and_install(
            sdk, version, west_version, ninja_version, source, projects
        )


def _check_and_install(
    sdk: ZephyrSDK,
    version: str,
    west_version: str | None,
    ninja_version: str | None,
    source: ConfigType | None,
    projects: frozenset[str],
) -> tuple[Path, Path, dict[str, str]]:
    """Install west and the Zephyr SDK.

    west_version/ninja_version override zephyr/requirements.txt's pinned versions
    (CONF_WEST_VERSION/CONF_NINJA_VERSION). source, if given (framework: source:),
    overrides which Zephyr gets fetched: a git fork/branch/ref, cloned into ESPHome's
    cache like the official source and pinned to the commit
    dts_fetch.resolve_sdk_source_version() resolved (KEY_SDK_SOURCE_RESOLVED_REF), or a
    `local:` checkout used where it is (its parent folder becomes the workspace).

    projects: the projects this build needs beyond the SDK's required ones (a
    family's vendor HALs, the projects NCS components ask for). A workspace only
    gains projects, since the builds of one version share it.

    Returns (python_bin, framework_path, west_env) where:
      - python_bin      -- Python executable inside the managed venv
      - framework_path  -- west workspace root (contains zephyr/, modules/, ...)
      - west_env        -- environment dict with ZEPHYR_BASE and PATH set
    """
    ver_tag = f"v{version}" if not version.startswith("v") else version
    python_env = _get_python_env_path(ver_tag)
    python_bin = get_python_env_executable_path(python_env, "python")

    # Every workspace of this version shares the venv. It only changes when the west
    # requirements or overrides do, so builds that use it are not locked against that.
    with install_lock(get_zephyr_tools_path(), f"penv-{ver_tag}"):
        overrides = {"west": west_version, "ninja": ninja_version}
        # Beside the venv, not in it: a rebuild removes the venv folder
        requirements = python_env.with_name(f"{python_env.name}.requirements.txt")
        requirements.parent.mkdir(parents=True, exist_ok=True)
        requirements.write_text(_effective_requirements(overrides))
        install_venv = ensure_venv(python_env, requirements, ver_tag)

    if source is not None and source[CONF_TYPE] == TYPE_LOCAL:
        zephyr_dir = source[CONF_PATH]  # already a resolved Path
        if not (zephyr_dir / "west.yml").is_file():
            raise EsphomeError(
                f"'{zephyr_dir}' does not look like a Zephyr manifest repo (no west.yml) -- "
                "sdk_source: local: expects a path to a Zephyr checkout, e.g. a fork you're "
                "developing on."
            )
        framework = install_local_framework(python_bin, zephyr_dir, sdk.source.name)
    else:
        framework = _framework_path(_source_cache_key(sdk, ver_tag, source))
        zephyr_dir = framework / "zephyr"
        _install_workspace(python_bin, framework, sdk, ver_tag, source, projects)

    with install_lock(get_zephyr_tools_path(), f"penv-{ver_tag}"):
        ensure_zephyr_requirements(
            python_env, framework, install_venv, ver_tag, zephyr_dir=zephyr_dir
        )

    west_env = {
        **os.environ,
        "ZEPHYR_BASE": str(zephyr_dir),
        "PATH": f"{python_bin.parent}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    return python_bin, framework, west_env


def _install_workspace(
    python_bin: Path,
    framework: Path,
    sdk: ZephyrSDK,
    ver_tag: str,
    source: ConfigType | None,
    projects: frozenset[str],
) -> None:
    """The official source, or a git source pinned to its resolved commit."""
    framework_source = sdk.source
    label = ver_tag
    pin: str | None = None
    if source is not None:
        framework_source = replace(sdk.source, url=source[CONF_URL])
        rev = source.get(KEY_SDK_SOURCE_RESOLVED_REF, source.get(CONF_REF))
        label = source.get(CONF_REF) or "default branch"
        if _is_commit_sha(rev):
            pin = rev
        elif rev is not None:
            label = rev
    wanted = set(projects | framework_source.required)
    sentinel = framework / ".ready"
    resolved_marker = framework / ".resolved_ref"
    zephyr_reqs = framework / "zephyr" / "scripts" / "requirements.txt"
    if not sentinel.exists() or not zephyr_reqs.exists():
        install_framework(
            python_bin, framework, framework_source, label, wanted, pin=pin
        )
    elif pin is not None and (
        not resolved_marker.is_file() or resolved_marker.read_text() != pin
    ):
        refresh_framework(python_bin, framework, framework_source, label, wanted, pin)
    else:
        fetch_missing_west_projects(
            python_bin, framework, framework_source, label, wanted
        )
    if not sentinel.exists():
        if pin is not None:
            resolved_marker.write_text(pin)
        sentinel.touch()
    # Every run: existing installs need these too, and they are no-ops once applied
    patch_gen_defines_dts_path(framework)
    patch_west_build_source_dir_relpath(framework)


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
