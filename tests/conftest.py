import asyncio
import sys

import pytest


@pytest.fixture(scope="session")
def event_loop_policy():
    # same loop the services use on Windows (see junctionlab.netutil)
    if sys.platform == "win32":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()
