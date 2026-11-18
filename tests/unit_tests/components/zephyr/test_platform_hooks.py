"""The Zephyr toolchain hooks of the platforms built only through zephyr."""

import asyncio
import importlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from esphome.const import Toolchain
from esphome.core import CORE, CoroPriority

# Platforms whose to_code only hands a Zephyr toolchain build to zephyr
THIN_PLATFORMS: list[str] = ["nrf", "nxp", "renesas", "silabs", "stm32"]
# Also esp32 and rp2, whose other toolchains build without the hook
RUN_COMPILE_PLATFORMS: list[str] = [*THIN_PLATFORMS, "esp32", "rp2"]


@pytest.mark.parametrize("platform", THIN_PLATFORMS)
def test_to_code_runs_the_zephyr_code_generation_first(platform: str) -> None:
    """At PLATFORM priority, so the workspace is installed before any devicetree read."""
    module = importlib.import_module(f"esphome.components.{platform}")
    assert module.to_code.priority == float(CoroPriority.PLATFORM)
    CORE.toolchain = Toolchain.ZEPHYR
    with patch(
        "esphome.components.zephyr.zephyr_toolchain_to_code", new=AsyncMock()
    ) as to_code:
        asyncio.run(module.to_code({"variant": "X"}))
    to_code.assert_awaited_once_with({"variant": "X"})


@pytest.mark.parametrize("platform", RUN_COMPILE_PLATFORMS)
@pytest.mark.parametrize(
    ("toolchain", "claimed"),
    [(Toolchain.ZEPHYR, True), (Toolchain.PLATFORMIO, False)],
)
def test_run_compile_claims_only_zephyr_builds(
    platform: str, toolchain: Toolchain, claimed: bool
) -> None:
    """True tells the compile command the platform built it."""
    module = importlib.import_module(f"esphome.components.{platform}")
    CORE.toolchain = toolchain
    with patch(
        "esphome.components.zephyr.run_compile", new=MagicMock(return_value=True)
    ) as run_compile:
        assert module.run_compile("args", {}) is claimed
    assert run_compile.called is claimed
