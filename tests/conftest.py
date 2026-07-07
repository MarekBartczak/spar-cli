"""Pytest configuration and fixtures for spar-cli tests."""

import os
import pytest


@pytest.fixture(autouse=True)
def isolate_home_and_xdg(tmp_path, monkeypatch):
    """
    Autouse fixture that isolates HOME and removes XDG_CONFIG_HOME for every test.

    This ensures tests are hermetic and don't read from the real user's ~/.config/spar/config.toml.

    Tests that need to explicitly set HOME or XDG_CONFIG_HOME can use the monkeypatch
    fixture directly, which will override this isolation.
    """
    # Create an isolated home directory with a unique name to avoid conflicts with tests
    # that create their own tmp_path / "home" subdirectory
    fake_home = tmp_path / "_autouse_home"
    fake_home.mkdir()

    # Set HOME to the isolated directory
    monkeypatch.setenv("HOME", str(fake_home))

    # Remove XDG_CONFIG_HOME from the environment if it exists
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
