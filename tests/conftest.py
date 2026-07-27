import pytest

from esphome.core import CORE


@pytest.fixture(autouse=True)
def _diagnose_core_config_corruption(request):
    before = CORE.config
    yield
    after = CORE.config
    if before is not None and after is None:
        print(
            f"\n[CORE-DIAG] {request.node.nodeid} cleared CORE.config from a real dict to None\n",
            flush=True,
        )
