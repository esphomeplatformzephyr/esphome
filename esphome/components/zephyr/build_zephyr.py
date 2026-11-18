"""The native west build that nrf52 and the Zephyr toolchain share."""

from collections.abc import Sequence
import logging
from pathlib import Path
import subprocess

import yaml

from esphome.build_helpers import pch
from esphome.build_helpers.ccache import ccache_env
from esphome.const import KEY_CORE, KEY_FRAMEWORK_VERSION
from esphome.core import CORE, EsphomeError
from esphome.framework_helpers import (
    cmake_escape,
    get_cmake_compile_flags,
    get_cmake_link_flags,
    lex_cmake_build_flags,
    quote_cmake_args,
    run_command_ok,
)
from esphome.helpers import rmtree, write_file_if_changed
from esphome.util import get_serial_number

_LOGGER = logging.getLogger(__name__)

# GCC only loads a precompiled header ahead of every other forced header, and
# Zephyr forces two with -imacros. They hold macros only, so the C++ sources
# of the app get them through the precompiled header.
_PCH_CMAKE_LINES = [
    "",
    "# ESPHome precompiled header",
    "get_property(esphome_options TARGET zephyr_interface",
    "    PROPERTY INTERFACE_COMPILE_OPTIONS)",
    "set(esphome_kept_options)",
    "set(esphome_pch_headers)",
    "foreach(option IN LISTS esphome_options)",
    '  if(option MATCHES "imacros> ([^>]+)")',
    '    list(APPEND esphome_pch_headers "${CMAKE_MATCH_1}")',
    "    list(APPEND esphome_kept_options",
    '        "$<$<NOT:$<AND:$<COMPILE_LANGUAGE:CXX>,$<STREQUAL:$<TARGET_PROPERTY:NAME>,app>>>:${option}>")',
    "  else()",
    '    list(APPEND esphome_kept_options "${option}")',
    "  endif()",
    "endforeach()",
    "if(NOT esphome_pch_headers)",
    '  message(FATAL_ERROR "ESPHome: the headers Zephyr forces were not found, so "',
    '      "the precompiled header would not load (set ESPHOME_PCH_ENABLE=0)")',
    "endif()",
    "set_property(TARGET zephyr_interface",
    '    PROPERTY INTERFACE_COMPILE_OPTIONS "${esphome_kept_options}")',
    *(
        f'list(APPEND esphome_pch_headers "${{CMAKE_CURRENT_LIST_DIR}}/../src/{header}")'
        for header in pch.PCH_DEFAULT_HEADERS
    ),
    'list(TRANSFORM esphome_pch_headers REPLACE "(.+)" "$<$<COMPILE_LANGUAGE:CXX>:\\\\1>")',
    "target_precompile_headers(app PRIVATE ${esphome_pch_headers})",
]
_PCH_SUM_PATH = "CMakeFiles/app.dir/cmake_pch.hxx.gch.sum"


def _sdk_revision(framework_path: Path) -> str:
    """The checked-out Zephyr commit; a moving ref keeps its version string."""
    result = subprocess.run(
        ["git", "-C", str(framework_path / "zephyr"), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _write_pch_checksum(
    build_dir: Path, source_dir: Path, board: str, framework_path: Path
) -> None:
    """Stand in for the path-laden .gch in ccache's hash; the app dir exists
    only after the first configure, nested by sysbuild."""
    app_dir = build_dir / "zephyr"
    if not (app_dir / "CMakeCache.txt").is_file():
        app_dir = build_dir
    if not (app_dir / "CMakeCache.txt").is_file():
        return
    checksum = pch.pch_checksum(
        CORE.relative_src_path(),
        pch.PCH_DEFAULT_HEADERS,
        (
            str(CORE.data[KEY_CORE][KEY_FRAMEWORK_VERSION]),
            _sdk_revision(framework_path),
            board,
            *(
                path.read_text(encoding="utf-8")
                for path in sorted(source_dir.iterdir())
                if path.suffix in (".conf", ".overlay")
            ),
        ),
    )
    write_file_if_changed(app_dir / _PCH_SUM_PATH, checksum + "\n")


def _find_runners_yaml(build_dir: Path) -> Path:
    """Locate the runners.yaml describing the board's default flash runner.

    run_west_build() always passes --sysbuild, which fans a single `west build`
    out into one build directory per image domain (mcuboot, app, ...), each with
    its own `zephyr/runners.yaml`, plus a top-level `domains.yaml` naming which
    domain is the default. Every domain targets the same physical board, so the
    default domain's runners.yaml is a faithful stand-in for the whole build.
    """
    domains_yaml_path = Path(build_dir) / "domains.yaml"
    try:
        with domains_yaml_path.open(encoding="utf-8") as f:
            domains_yaml = yaml.safe_load(f)
        default_domain = domains_yaml["default"]
        domain_build_dir = next(
            d["build_dir"]
            for d in domains_yaml["domains"]
            if d["name"] == default_domain
        )
        return Path(domain_build_dir) / "zephyr" / "runners.yaml"
    except (OSError, yaml.YAMLError, KeyError, TypeError, StopIteration):
        # Not a sysbuild (multi-domain) build -- runners.yaml lives directly
        # under build_dir.
        return Path(build_dir) / "zephyr" / "runners.yaml"


def log_available_runners(build_dir: Path, requested_runner: str | None = None) -> None:
    """Log the board's available west flash runners and which one is the
    default, read from runners.yaml (see resolve_dev_id() for another
    consumer of the same file).

    `requested_runner` (framework: advanced: runner:), if given, is checked against
    `runners` -- valid names are only knowable here, after CMake configure -- and
    logged if recognized, else warned about, never a hard config-time error.
    """
    runners_yaml_path = _find_runners_yaml(build_dir)
    try:
        with runners_yaml_path.open(encoding="utf-8") as f:
            runners_yaml = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        _LOGGER.debug("Could not read %s: %s", runners_yaml_path, e)
        return
    runners_yaml = runners_yaml or {}
    runners = runners_yaml.get("runners")
    flash_runner = runners_yaml.get("flash-runner")
    if not runners:
        return
    _LOGGER.info(
        "Available flash runners: %s (default: %s)",
        ", ".join(runners),
        flash_runner or "none",
    )
    if requested_runner:
        if requested_runner in runners:
            _LOGGER.info(
                "Overriding flash runner: '%s' (board default: %s)",
                requested_runner,
                flash_runner or "none",
            )
        else:
            _LOGGER.warning(
                "Configured runner '%s' is not in this board's available runners (%s)",
                requested_runner,
                ", ".join(runners),
            )


def _runner_supports_dev_id(
    python_executable: Path, framework_path: Path, runner: str
) -> bool:
    """Ask the pinned SDK's own west runner framework whether `runner` declares
    the `dev_id` capability (a `-i/--dev-id` option), instead of checking against
    a hand-maintained list -- a future SDK version adding/removing dev_id support
    on a runner is picked up automatically, with no list to keep in sync.

    Runs inside the west venv (python_executable) so this sees runners the exact
    same way `west flash` itself does -- importing `runners` standalone outside
    west's own venv can fail for modules with west-specific dependencies.
    Returns False (matching the pre-dynamic-lookup default) if the runner is
    unrecognized or the query fails for any reason -- callers should treat that
    as "don't forward -i", reproducing today's single-probe auto-detect behavior.
    """
    runners_dir = framework_path / "zephyr" / "scripts" / "west_commands"
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(runners_dir)!r})\n"
        "try:\n"
        "    import runners\n"
        f"    print(runners.get_runner_cls({runner!r}).capabilities().dev_id)\n"
        "except Exception:\n"
        "    print(False)\n"
    )
    try:
        result = subprocess.run(
            [str(python_executable), "-c", script],
            capture_output=True,
            timeout=10,
            check=False,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.stdout.strip() == "True"


def resolve_dev_id(
    python_executable: Path,
    framework_path: Path,
    build_dir: Path,
    port: str,
    runner_override: str | None = None,
) -> str | None:
    """Resolve a `-i/--dev-id` value to disambiguate which probe `west flash` uses.

    Reads the board's default flash runner from runners.yaml (written by the
    CMake configure step, see _find_runners_yaml() for the sysbuild wrinkle) and,
    only when the *effective* runner -- `runner_override` (framework: advanced:
    runner:) if given, else the board's default -- is known to support device IDs
    (see _runner_supports_dev_id()), looks up the USB serial number backing the
    selected serial `port`. Returns None whenever the runner is unknown/unsupported
    or the serial number can't be determined -- callers should treat None as
    "don't pass -i", which reproduces today's single-probe auto-detect behavior
    exactly.
    """
    runners_yaml_path = _find_runners_yaml(build_dir)
    try:
        with runners_yaml_path.open(encoding="utf-8") as f:
            runners_yaml = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        _LOGGER.debug("Could not read %s: %s", runners_yaml_path, e)
        return None

    flash_runner = runner_override or (runners_yaml or {}).get("flash-runner")
    if flash_runner is None or not _runner_supports_dev_id(
        python_executable, framework_path, flash_runner
    ):
        return None

    return get_serial_number(port)


def generate_cmake_lists(
    platform_name: str, pch_on: bool, extra_modules: Sequence[Path] = ()
) -> bool:
    """Write the project CMakeLists.txt, returning True if it changed.
    ``platform_name`` names the platform in build flag warnings; ``extra_modules``
    join the converted libraries in EXTRA_ZEPHYR_MODULES."""
    entries = lex_cmake_build_flags(platform_name)
    compile_flags = get_cmake_compile_flags(entries)
    link_flags = get_cmake_link_flags(entries)

    # Convert any PlatformIO libraries added via cg.add_library() into Zephyr
    # modules and discover them through EXTRA_ZEPHYR_MODULES (a CMake list, set
    # before find_package(Zephyr) so the modules are picked up). Only
    # framework-agnostic libraries actually compile under Zephyr.
    from .library import DEFINES_VAR, generate_zephyr_modules  # noqa: PLC0415

    module_dirs = [
        *generate_zephyr_modules(list(CORE.platformio_libraries.values())),
        *extra_modules,
    ]
    defines = [flag for flag in compile_flags if flag.startswith("-D")]
    options = [flag for flag in compile_flags if not flag.startswith("-D")]

    lines = [
        "cmake_minimum_required(VERSION 3.20.0)",
        "",
        'set(Zephyr_DIR "$ENV{ZEPHYR_BASE}/share/zephyr-package/cmake/")',
        "",
    ]

    if module_dirs:
        modules = ";".join(str(d).replace("\\", "/") for d in module_dirs)
        lines += [f'set(EXTRA_ZEPHYR_MODULES "{modules}")', ""]
        if defines:
            escaped = ";".join(cmake_escape(flag) for flag in defines)
            lines += [f'set({DEFINES_VAR} "{escaped}")', ""]

    lines += [
        "find_package(Zephyr REQUIRED)",
        "",
        f"project({CORE.name})",
        "",
        'file(GLOB_RECURSE APP_SOURCES CONFIGURE_DEPENDS "${CMAKE_CURRENT_LIST_DIR}/../src/*.cpp" "${CMAKE_CURRENT_LIST_DIR}/../src/*.c")',
        "",
        "target_sources(app PRIVATE ${APP_SOURCES})",
        'target_include_directories(app PRIVATE "${CMAKE_CURRENT_LIST_DIR}/../src")',
    ]

    if defines:
        lines += [
            "",
            "target_compile_options(app PRIVATE",
            *[f"  {arg}" for arg in quote_cmake_args(defines)],
            ")",
        ]
    if options:
        # A target's own options come before the ones it inherits from zephyr_interface, and the
        # compiler keeps the last of two opposing warning flags. Source options come after both.
        lines += [
            "",
            "set_source_files_properties(${APP_SOURCES} PROPERTIES",
            f'  COMPILE_OPTIONS "{";".join(cmake_escape(flag) for flag in options)}"',
            ")",
        ]

    if pch_on:
        lines += _PCH_CMAKE_LINES

    if link_flags:
        lines += [
            "",
            "zephyr_ld_options(",
            *[f"  {arg}" for arg in quote_cmake_args(link_flags)],
            ")",
        ]

    return write_file_if_changed(
        CORE.relative_build_path("zephyr", "CMakeLists.txt"),
        "\n".join(lines) + "\n",
    )


def add_ccache_env(
    env: dict[str, str], ccache: str | None, tools_cache: tuple[str, str]
) -> None:
    """Add the managed ccache settings to a west build's ``env``; ``ccache`` is the
    resolved binary (resolve_ccache_path), or None when ccache is disabled."""
    if ccache is None:
        # Zephyr wraps compiles with any ccache it finds; unmanaged it
        # must not cache (a sysbuild image never sees USE_CCACHE=0).
        env.setdefault("CCACHE_DISABLE", "1")
        return
    env.update(ccache_env(ccache, tools_cache))
    # Drop only the per build map entry (posix, CMake's spelling);
    # its from side covers no compiled sources. A spaced path cannot
    # survive ccache's space split list, so it stays hashed.
    source_dir = CORE.relative_build_path("zephyr").as_posix()
    if any(ch.isspace() for ch in source_dir):
        _LOGGER.debug("Whitespace in %s; the per build map stays hashed", source_dir)
    else:
        device_map = f"-fmacro-prefix-map={source_dir}=CMAKE_SOURCE_DIR"
        env["CCACHE_IGNOREOPTIONS"] = (
            f"{env.get('CCACHE_IGNOREOPTIONS', '')} {device_map}".strip()
        )


def clean_changed_build(build_dir: Path, cmake_lists_changed: bool) -> None:
    """A missing CMake cache (dropped by zephyr's copy_files() on config
    change) or a changed CMakeLists.txt requires a pristine build: Zephyr
    caches Kconfig/devicetree state that survives a plain cmake re-run.
    West can't do the wipe -- its pristine modes only recognize a build dir
    by reading ZEPHYR_BASE from the very cache that was dropped."""
    if (
        cmake_lists_changed or not (build_dir / "CMakeCache.txt").is_file()
    ) and build_dir.is_dir():
        _LOGGER.info("Build inputs changed, cleaning %s", build_dir)
        rmtree(build_dir)


def _module_revision(
    python_executable: Path, framework_path: Path, env: dict, module: str
) -> str | None:
    """The commit `module` is pinned to in this workspace, or None if west can't say."""
    result = subprocess.run(
        [str(python_executable), "-m", "west", "list", module, "-f", "{sha}"],
        env=env,
        cwd=str(framework_path),
        capture_output=True,
        text=True,
        check=False,
    )
    revision = result.stdout.strip()
    return revision if result.returncode == 0 and revision else None


def run_west_blobs_fetch(
    python_executable: Path,
    framework_path: Path,
    env: dict,
    module: str,
    allow_regex: str,
    sentinel_name: str,
) -> None:
    """Fetch west blobs for a HAL module once per module revision.

    The sentinel records the module revision the blobs were fetched for, so a
    `west update` that moves the module (e.g. a moving framework: source:) fetches
    again. Uses --allow-regex to limit the download to the relevant chip's blobs
    and --auto-accept to skip interactive license prompts.
    """
    sentinel = framework_path / sentinel_name
    revision = _module_revision(python_executable, framework_path, env, module)
    if (
        revision is not None
        and sentinel.is_file()
        and sentinel.read_text().strip() == revision
    ):
        return
    _LOGGER.info("Fetching %s binary blobs matching '%s' ...", module, allow_regex)
    cmd = [
        str(python_executable),
        "-m",
        "west",
        "blobs",
        "fetch",
        module,
        "--allow-regex",
        allow_regex,
        "--auto-accept",
    ]
    if not run_command_ok(cmd, env=env, cwd=str(framework_path)):
        raise EsphomeError(f"Failed to fetch {module} binary blobs")
    sentinel.write_text(revision or "")


def check_bootloader_built(build_dir: Path) -> None:
    """Refuse a serial flash of a --skip-bootloader build: west flash writes
    every sysbuild image, and the MCUboot one was never compiled."""
    domains = build_dir / "domains.yaml"
    if not domains.is_file():
        return
    names = {d.get("name") for d in yaml.safe_load(domains.read_text())["domains"]}
    if (
        "mcuboot" in names
        and not (build_dir / "mcuboot" / "zephyr" / "zephyr.elf").is_file()
    ):
        raise EsphomeError(
            "This build was compiled with --skip-bootloader; recompile without it"
        )


def run_west_flash(
    python_executable: Path,
    framework_path: Path,
    env: dict,
    build_dir: Path,
    device: str,
    baud_rate: str | int | None = None,
    runner: str | None = None,
) -> bool:
    """Flash a real Zephyr embedded target (e.g. esp32_h2) via `west flash`.

    Delegates bootloader/partition-table/app image selection and offsets to
    Zephyr's own runner (runners/esp32.py, wrapping esptool) and its sysbuild
    multi-domain flashing -- both already know every image from the CMake
    configure step, so there is no need to replicate that logic here.

    `runner`, when given (framework: advanced: runner:), overrides Zephyr's own
    esp32-family default runner.
    """
    west_cmd = [
        str(python_executable),
        "-m",
        "west",
        "flash",
        # '--skip-rebuild' still works in west v1.5, but is deprecated.
        "--no-rebuild",
        "-d",
        str(build_dir),
        "--esp-device",
        device,
    ]
    if baud_rate:
        west_cmd += ["--esp-baud-rate", str(baud_rate)]
    if runner:
        west_cmd += ["--runner", runner]

    return run_command_ok(
        west_cmd,
        env=env,
        stream_output=True,
        cwd=str(framework_path),
    )


def run_west_flash_generic(
    python_executable: Path,
    framework_path: Path,
    env: dict,
    build_dir: Path,
    dev_id: str | None = None,
    runner: str | None = None,
) -> bool:
    """Flash a Zephyr target using the board's default west runner.

    This is used by non-ESP32 Zephyr variants where flashing is typically
    handled by probe-based runners configured by Zephyr (jlink, pyocd, etc.).

    `dev_id`, when given (see `resolve_dev_id`), is forwarded as `-i/--dev-id`
    to tell the runner which attached probe to use -- without it, the runner
    picks whichever probe it finds, which is ambiguous when more than one
    debug-probe board is attached at once.

    `runner`, when given (framework: advanced: runner:), overrides the board's
    default flash-runner (runners.yaml's `flash-runner:`).
    """
    west_cmd = [
        str(python_executable),
        "-m",
        "west",
        "flash",
        # '--skip-rebuild' still works in west v1.5, but is deprecated.
        "--no-rebuild",
        "-d",
        str(build_dir),
    ]
    if dev_id:
        west_cmd += ["-i", dev_id]
    if runner:
        west_cmd += ["--runner", runner]
    return run_command_ok(
        west_cmd,
        env=env,
        stream_output=True,
        cwd=str(framework_path),
    )


def run_west_flash_pyocd(
    python_executable: Path,
    framework_path: Path,
    env: dict,
    build_dir: Path,
    dev_id: str | None = None,
) -> bool:
    """Flash a real Zephyr embedded target via a J-Link/CMSIS-DAP debug probe.

    Delegates to Zephyr's own pyocd runner -- same rationale as run_west_flash
    above, just a different runner for variants with no serial/esptool path
    (e.g. nrf52, which flashes over SWD instead).

    `dev_id`, when given, is forwarded as `-i/--dev-id` -- see
    `run_west_flash_generic` above.
    """
    west_cmd = [
        str(python_executable),
        "-m",
        "west",
        "flash",
        # '--skip-rebuild' still works in west v1.5, but is deprecated.
        "--no-rebuild",
        "-d",
        str(build_dir),
        "--runner",
        "pyocd",
    ]
    if dev_id:
        west_cmd += ["-i", dev_id]
    return run_command_ok(
        west_cmd,
        env=env,
        stream_output=True,
        cwd=str(framework_path),
    )


def west_build_command(
    python_executable: Path,
    board: str,
    build_dir: Path,
    source_dir: Path,
    *,
    sysbuild: bool = False,
    west_args: Sequence[str] = (),
    cmake_args: Sequence[str] = (),
) -> list[str]:
    """``sysbuild`` turns sysbuild on where the framework leaves it off (mainline
    Zephyr); the build then nests the app's output in ``build_dir/zephyr/``.
    ``west_args`` go to west build, ``cmake_args`` to CMake."""
    return [
        str(python_executable),
        "-m",
        "west",
        "build",
        "--pristine=auto",
        *(["--sysbuild"] if sysbuild else []),
        *west_args,
        "-b",
        board,
        "-d",
        str(build_dir),
        str(source_dir),
        "--",
        # Only adds -DNDEBUG (Kconfig sets the optimization level); picolibc used to force it
        "-DCMAKE_BUILD_TYPE=MinSizeRel",
        *cmake_args,
    ]
