"""Unit tests for esphome.components.zephyr.variants.simg301: config_schema().

SIMG301 is the one variant that needs `board_source:`/`framework: source:` just to
validate at all -- its board and SoC support aren't upstream/tagged yet (see the
module comment in variants/simg301.py). These tests use `type: local` sources
(a real VERSION file / board.yml on disk) instead of git, so they stay hermetic --
no network access, unlike the real variant's own git defaults.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from esphome.components.zephyr import _variant_config_schema
from esphome.components.zephyr.const import (
    CONF_BOARD_SOURCE,
    KEY_ZEPHYR,
    ZEPHYR_VARIANT_SIMG301,
)
import esphome.config_validation as cv
from esphome.const import (
    CONF_BOARD,
    CONF_FRAMEWORK,
    CONF_PATH,
    CONF_SOURCE,
    CONF_TYPE,
    CONF_VARIANT,
    KEY_CORE,
    TYPE_LOCAL,
)
from esphome.core import CORE

from .zephyr_state import empty_zephyr_data

_BOARD_NAME = "simg301_ek2719a"


def _init_core(tmp_path: Path) -> None:
    CORE.data[KEY_CORE] = {}
    CORE.data[KEY_ZEPHYR] = empty_zephyr_data()
    CORE.config_path = tmp_path / "test.yaml"


def _write_board_yml(root: Path) -> None:
    board_dir = root / "boards" / "silabs" / _BOARD_NAME
    board_dir.mkdir(parents=True)
    (board_dir / "board.yml").write_text(
        f"board:\n  name: {_BOARD_NAME}\n  vendor: silabs\n"
    )


def _write_version_file(root: Path, major: int, minor: int, patch: int) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "VERSION").write_text(
        f"VERSION_MAJOR = {major}\nVERSION_MINOR = {minor}\nPATCHLEVEL = {patch}\n"
    )


def test_simg301_requires_board_source(tmp_path: Path) -> None:
    _init_core(tmp_path)
    with pytest.raises(cv.Invalid, match="isn't in mainline Zephyr yet"):
        _variant_config_schema({CONF_VARIANT: ZEPHYR_VARIANT_SIMG301})


def test_simg301_requires_framework_source(tmp_path: Path) -> None:
    _init_core(tmp_path)
    board_root = tmp_path / "boards_repo"
    _write_board_yml(board_root)
    config = {
        CONF_VARIANT: ZEPHYR_VARIANT_SIMG301,
        CONF_BOARD: _BOARD_NAME,
        CONF_BOARD_SOURCE: {CONF_TYPE: TYPE_LOCAL, CONF_PATH: str(board_root)},
    }
    with pytest.raises(cv.Invalid, match=r"requires zephyr >= 4\.4\.3"):
        _variant_config_schema(config)


def test_simg301_validates_with_board_and_framework_source(tmp_path: Path) -> None:
    _init_core(tmp_path)
    board_root = tmp_path / "boards_repo"
    _write_board_yml(board_root)
    sdk_root = tmp_path / "sdk_source"
    _write_version_file(sdk_root, 4, 5, 0)

    config = {
        CONF_VARIANT: ZEPHYR_VARIANT_SIMG301,
        CONF_BOARD: _BOARD_NAME,
        CONF_BOARD_SOURCE: {CONF_TYPE: TYPE_LOCAL, CONF_PATH: str(board_root)},
        CONF_FRAMEWORK: {
            CONF_SOURCE: {CONF_TYPE: TYPE_LOCAL, CONF_PATH: str(sdk_root)}
        },
    }

    validated = _variant_config_schema(config)

    assert validated[CONF_VARIANT] == ZEPHYR_VARIANT_SIMG301
    assert validated[CONF_BOARD] == _BOARD_NAME


def test_simg301_rejects_framework_source_below_min_version(tmp_path: Path) -> None:
    """A real but too-old source: (e.g. a local checkout at v4.4.2) must still be
    rejected -- min_version_override isn't just a `source:`-vs-not switch."""
    _init_core(tmp_path)
    board_root = tmp_path / "boards_repo"
    _write_board_yml(board_root)
    sdk_root = tmp_path / "sdk_source"
    _write_version_file(sdk_root, 4, 4, 2)

    config = {
        CONF_VARIANT: ZEPHYR_VARIANT_SIMG301,
        CONF_BOARD: _BOARD_NAME,
        CONF_BOARD_SOURCE: {CONF_TYPE: TYPE_LOCAL, CONF_PATH: str(board_root)},
        CONF_FRAMEWORK: {
            CONF_SOURCE: {CONF_TYPE: TYPE_LOCAL, CONF_PATH: str(sdk_root)}
        },
    }

    with pytest.raises(cv.Invalid, match=r"requires zephyr >= 4\.4\.3"):
        _variant_config_schema(config)
