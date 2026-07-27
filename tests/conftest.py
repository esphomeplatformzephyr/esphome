import os
from pathlib import Path
import tempfile

import pytest

from esphome.core import CORE

_TRACE_PATH = Path(tempfile.gettempdir()) / f"core_trace_{os.getpid()}.log"


def _state(config) -> str:
    if config is None:
        return "None"
    if isinstance(config, dict):
        return f"dict(id={id(config)}, keys={len(config)})"
    return f"{type(config).__name__}(id={id(config)})"


@pytest.fixture(autouse=True)
def _diagnose_core_config_corruption(request):
    before = _state(CORE.config)
    yield
    after = _state(CORE.config)
    # Written to a real file, not stdout/stderr -- pytest's default capture
    # mode redirects the underlying OS file descriptors, so even
    # sys.__stderr__.write() gets swallowed. One file per worker process
    # (keyed by pid) avoids concurrent-write corruption under pytest-xdist.
    with _TRACE_PATH.open("a") as f:
        f.write(f"{request.node.nodeid} before={before} after={after}\n")
