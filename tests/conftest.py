"""Pytest configuration and shared fixtures."""

from __future__ import annotations

import pytest


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line("markers", "asyncio: mark a test as an asyncio test")
