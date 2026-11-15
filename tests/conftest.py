"""Shared test setup: a fresh temporary database per test, and no real AI calls."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("OPENROUTER_API_KEY", "test-key")  # never used: every AI call is faked

from app.storage import init_db  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    init_db(tmp_path / "test.db")
    yield
