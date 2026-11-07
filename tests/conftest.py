"""Shared fixtures for flicbutton tests."""

import pytest

from .fake_flicd import FakeFlicd


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading of custom integrations in all tests."""
    yield


@pytest.fixture
async def fake_flicd():
    """Start a fake flicd on 127.0.0.1 on a free port."""
    server = FakeFlicd()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()
