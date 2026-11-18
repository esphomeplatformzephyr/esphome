"""Tests for the Zephyr toolchain build, run_compile()."""

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from esphome.components import stm32
from esphome.components.stm32.zephyr_variants import stm32c5
from esphome.components.zephyr import run_compile, zephyr_data
from esphome.components.zephyr.const import KEY_BOARD_ROOT, KEY_SHIELDS, KEY_SNIPPETS
from esphome.const import KEY_CORE, Toolchain
from esphome.core import CORE, EsphomeError


@pytest.fixture
def build(tmp_path: Path) -> Iterator[MagicMock]:
    """run_compile on an STM32C5 config, with the install, the toolchain and west mocked."""
    CORE.data[KEY_CORE] = {}
    CORE.config_path = tmp_path / "test.yaml"
    CORE.build_path = tmp_path / "build"
    CORE.name = "test"
    stm32.CONFIG_SCHEMA({"variant": "STM32C5", "board": "xiao_stm32c5"})
    CORE.toolchain = Toolchain.ZEPHYR
    module = MagicMock(local_path=tmp_path / "module")
    with (
        patch.multiple(
            "esphome.components.zephyr.framework",
            check_and_install=MagicMock(),
            get_build_paths=MagicMock(return_value=(Path("/py"), tmp_path / "ws")),
            get_zephyr_base=MagicMock(return_value=tmp_path / "ws" / "zephyr"),
            install_git_modules=MagicMock(return_value=[]),
            install_toolchain=MagicMock(return_value=Path("/sdk")),
        ),
        patch(
            "esphome.components.zephyr.resolve_zephyr_modules", return_value=[module]
        ),
        patch("esphome.components.zephyr.build_zephyr.log_available_runners"),
        patch("esphome.components.zephyr.mcuboot.check_mcuboot_swap_limit"),
        patch("esphome.build_helpers.pch.pch_enabled", return_value=False),
        patch("esphome.framework_helpers.run_command_ok", return_value=True) as run,
    ):
        yield run


def _cmd(run: MagicMock) -> list[str]:
    return run.call_args.args[0]


def test_builds_with_sysbuild_in_the_workspace(
    build: MagicMock, tmp_path: Path
) -> None:
    assert run_compile(None, {}) is True

    cmd = _cmd(build)
    assert "--sysbuild" in cmd
    assert cmd[cmd.index("-b") + 1] == "xiao_stm32c5"
    assert cmd[cmd.index("-d") + 1] == str(tmp_path / "build" / ".west_build")
    assert build.call_args.kwargs["cwd"] == str(tmp_path / "ws")
    env = build.call_args.kwargs["env"]
    assert env["ZEPHYR_BASE"] == str(tmp_path / "ws" / "zephyr")
    assert env["ZEPHYR_SDK_INSTALL_DIR"] == str(Path("/sdk"))


def test_modules_join_the_cmake_lists(build: MagicMock, tmp_path: Path) -> None:
    """In CMakeLists, not on the command line, where they would hide the libraries."""
    run_compile(None, {})

    cmake_lists = (tmp_path / "build" / "zephyr" / "CMakeLists.txt").read_text()
    assert (tmp_path / "module").as_posix() in cmake_lists
    assert not any("EXTRA_ZEPHYR_MODULES" in arg for arg in _cmd(build))


def test_board_root_shields_and_snippets(build: MagicMock, tmp_path: Path) -> None:
    zephyr_data()[KEY_BOARD_ROOT] = tmp_path / "boards"
    zephyr_data()[KEY_SHIELDS] = ["a", "b"]
    zephyr_data()[KEY_SNIPPETS] = ["cdc-acm-console"]

    run_compile(None, {})

    cmd = _cmd(build)
    split = cmd.index("--")
    assert cmd[cmd.index("-S") + 1] == "cdc-acm-console"
    assert cmd.index("-S") < split
    assert f"-DBOARD_ROOT={tmp_path / 'boards'}" in cmd[split:]
    assert "-DSHIELD=a;b" in cmd[split:]


@pytest.mark.parametrize("skip", [True, False])
def test_skip_bootloader_builds_only_the_app(build: MagicMock, skip: bool) -> None:
    """MCUboot is still configured, which the app's signing needs."""
    CORE.skip_bootloader = skip

    run_compile(None, {})

    cmd = _cmd(build)
    assert ("--domain" in cmd[: cmd.index("--")]) is skip


def test_fails_with_west(build: MagicMock) -> None:
    build.return_value = False
    with pytest.raises(EsphomeError, match="Zephyr native build failed"):
        run_compile(None, {})


def test_needs_the_variant_toolchain(build: MagicMock) -> None:
    with (
        patch.object(stm32c5.VARIANT, "toolchain", None),
        pytest.raises(EsphomeError, match="names no Zephyr SDK toolchain"),
    ):
        run_compile(None, {})
    build.assert_not_called()
