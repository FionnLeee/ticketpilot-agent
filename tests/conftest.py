import os
from unittest.mock import patch

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-docker", action="store_true", default=False, help="run docker integration tests"
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "docker: mark test as requiring docker containers")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-docker"):
        skip_docker = pytest.mark.skip(reason="need --run-docker option to run")
        for item in items:
            if "docker" in item.keywords:
                item.add_marker(skip_docker)


@pytest.fixture
def mock_env():
    """Fixture to ensure environment is clean while preserving Windows home paths."""
    required_windows_env = {
        key: os.environ[key]
        for key in ("USERPROFILE", "HOMEDRIVE", "HOMEPATH")
        if key in os.environ
    }
    with patch.dict(os.environ, required_windows_env, clear=True):
        yield
