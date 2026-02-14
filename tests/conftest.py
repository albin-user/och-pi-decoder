"""Shared pytest fixtures."""

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
