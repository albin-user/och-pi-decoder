"""Shared pytest fixtures."""

from unittest.mock import patch

import pytest
from pathlib import Path

from pi_decoder.config import Config


@pytest.fixture
def project_root() -> Path:
    """Return the project root directory."""
    return Path(__file__).parent.parent


@pytest.fixture
def deploy_dir(project_root: Path) -> Path:
    """Return the deploy directory."""
    return project_root / "deploy"


@pytest.fixture
def pigen_dir(deploy_dir: Path) -> Path:
    """Return the pi-gen directory."""
    return deploy_dir / "pi-gen"


@pytest.fixture
def tmp_config(tmp_path: Path) -> Path:
    """Return a temporary config file path."""
    return tmp_path / "config.toml"


@pytest.fixture
def pco_config():
    """Config with PCO credentials set."""
    cfg = Config()
    cfg.pco.app_id = "test_app"
    cfg.pco.secret = "test_secret"
    cfg.pco.service_type_id = "12345"
    return cfg


@pytest.fixture(autouse=True)
def mock_fsutil_writable():
    """Make writable() a no-op in all tests.

    Patches the writable context manager itself rather than platform.system,
    so tests that mock platform.system (e.g. display tests) don't interfere.
    """
    from contextlib import contextmanager

    @contextmanager
    def _noop_writable(mount_point="/"):
        yield

    with patch("pi_decoder.fsutil.writable", _noop_writable):
        yield
