"""Shared pytest fixtures."""
import os
import sys
import tempfile

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture
def project_root():
    return PROJECT_ROOT


@pytest.fixture
def temp_db_path(tmp_path):
    return str(tmp_path / "test_news.db")