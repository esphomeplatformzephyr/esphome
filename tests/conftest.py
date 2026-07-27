import sys

import pytest

from esphome.core import CORE


@pytest.fixture(autouse=True)
def _diagnose_core_config_corruption(request):
    before = CORE.config
    yield
    after = CORE.config
    if before is not None and after is None:
        # Write to the real stderr fd, bypassing pytest's capture -- otherwise
        # this is silently dropped whenever the offending test itself passes
        # (pytest only surfaces captured output for failing tests).
        sys.__stderr__.write(
            f"\n[CORE-DIAG] {request.node.nodeid} cleared CORE.config from a real dict to None\n"
        )
        sys.__stderr__.flush()
