import hashlib
import logging
from pathlib import Path
import shutil
import subprocess
import time

from esphome.build_helpers.tools_cache import ZEPHYR_TOOLS_CACHE, tools_cache_path
import esphome.config_validation as cv
from esphome.const import CONF_PATH, CONF_REF, CONF_TYPE, CONF_URL, TYPE_LOCAL
from esphome.core import CORE, TimePeriodSeconds
from esphome.types import ConfigType

from .const import KEY_SDK_SOURCE_RESOLVED_REF

_LOGGER = logging.getLogger(__name__)


def _sdk_source_version_cache_root() -> Path:
    return tools_cache_path(*ZEPHYR_TOOLS_CACHE) / "sdk_source_version_cache"


def _parse_zephyr_version_file(version_file: Path) -> str:
    """Parse a Zephyr VERSION file (VERSION_MAJOR/VERSION_MINOR/PATCHLEVEL = N lines) into
    a 'major.minor.patch' string."""
    values: dict[str, str] = {}
    for line in version_file.read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    try:
        return f"{values['VERSION_MAJOR']}.{values['VERSION_MINOR']}.{values['PATCHLEVEL']}"
    except KeyError as exc:
        raise cv.Invalid(
            f"'{version_file}' is missing {exc} -- not a valid Zephyr VERSION file"
        ) from exc


def _sdk_source_cache_key(url: str, ref: str | None) -> str:
    return hashlib.sha1(f"{url}@{ref or 'HEAD'}".encode()).hexdigest()[:16]


def _resolve_git_head(dest: Path) -> str:
    """Return the exact commit SHA checked out at dest."""
    try:
        result = subprocess.run(
            ["git", "-C", str(dest), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise cv.Invalid(
            f"Can't resolve sdk_source commit: {exc.stderr.strip()}"
        ) from exc
    return result.stdout.strip()


def _fetch_sdk_source_version(url: str, ref: str | None, dest: Path) -> None:
    """Update the cached clone of url@ref in place, or clone it if not cached yet.

    A failed refresh raises rather than silently building from stale source, and
    leaves the cache intact so the next refresh stays incremental.
    """
    label = f"{url}@{ref or 'default branch'}"
    _LOGGER.info("[zephyr] Checking sdk_source version (%s) ...", label)
    cached = (dest / ".git").is_dir()
    if cached:
        cmds = [
            ["git", "-C", str(dest), "fetch", "--depth=1", "origin", ref or "HEAD"],
            ["git", "-C", str(dest), "reset", "--hard", "FETCH_HEAD"],
        ]
    else:
        shutil.rmtree(dest, ignore_errors=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        clone = ["git", "clone", "--depth=1", "--filter=blob:none", "--sparse"]
        if ref:
            clone += ["--branch", ref]
        cmds = [[*clone, url, str(dest)]]
    try:
        for cmd in cmds:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        if not cached:
            shutil.rmtree(dest, ignore_errors=True)
        # Last line is git's actual error; earlier ones are progress output.
        lines = (exc.stderr or "").strip().splitlines()
        reason = lines[-1] if lines else "git failed"
        raise cv.Invalid(f"Can't check sdk_source version ({label}): {reason}") from exc
    except FileNotFoundError as exc:
        raise cv.Invalid("sdk_source: requires git to be installed") from exc
    version_file = dest / "VERSION"
    if not version_file.is_file():
        raise cv.Invalid(
            f"'{label}' has no top-level VERSION file -- "
            "is this really a Zephyr repository?"
        )
    # Restart the refresh timer; reset leaves an unchanged file's mtime alone.
    version_file.touch()


def resolve_sdk_source_version(source: ConfigType, refresh: TimePeriodSeconds) -> str:
    """Return the 'major.minor.patch' version of a framework: source:, read directly from
    its VERSION file rather than guessed -- several places elsewhere in the codebase (build
    output layout, sysbuild mode, DTS lookups) branch on the exact semver, so this can't just
    default to the variant's own baseline when a custom source is in play.

    For a git source, also resolves the moving ref (branch, or an omitted ref: meaning
    "default branch") to one concrete commit SHA and stashes it onto source under
    KEY_SDK_SOURCE_RESOLVED_REF -- the single authoritative resolution framework.py's
    SDK install pins to, instead of re-resolving the same moving ref against its own
    timer.
    """
    if source[CONF_TYPE] == TYPE_LOCAL:
        # Points at the zephyr manifest repo itself, not a workspace root -- matches
        # framework.py's local_path/"west.yml" check.
        version_file = source[CONF_PATH] / "VERSION"
        if not version_file.is_file():
            raise cv.Invalid(
                f"'{version_file}' not found -- sdk_source: local: expects a path to a "
                "Zephyr checkout (e.g. a fork you're developing on)."
            )
        return _parse_zephyr_version_file(version_file)

    url = source[CONF_URL]
    ref = source.get(CONF_REF)
    dest = _sdk_source_version_cache_root() / _sdk_source_cache_key(url, ref)
    version_file = dest / "VERSION"

    needs_fetch = not version_file.is_file()
    if (
        not needs_fetch
        and refresh is not None
        and not CORE.skip_external_update
        and (time.time() - version_file.stat().st_mtime) > refresh.total_seconds
    ):
        needs_fetch = True

    if needs_fetch:
        _fetch_sdk_source_version(url, ref, dest)

    source[KEY_SDK_SOURCE_RESOLVED_REF] = _resolve_git_head(dest)
    return _parse_zephyr_version_file(version_file)
