import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def pytest_configure(config):
    # One fixed temp folder (pytest empties it each run) instead of the shared pytest-of-<user>, which can be locked.
    if not config.option.basetemp:
        config.option.basetemp = str(Path(tempfile.gettempdir()) / "creations-browser-pytest")


@pytest.fixture(autouse=True)
def net(monkeypatch):
    """No test reaches the real network: urlopen itself is replaced, and core._open is the fake transport."""
    import fakes
    return fakes.install(monkeypatch)


@pytest.fixture(autouse=True)
def no_real_local_app_data(monkeypatch, tmp_path_factory):
    """No test reads the real ``%LOCALAPPDATA%`` (where the games keep their own lists of downloaded Creations): the variable
    points at an empty temporary folder. Tests that need a list build one there, or pass their own ``base``."""
    folder = tmp_path_factory.mktemp("local-app-data")
    monkeypatch.setenv("LOCALAPPDATA", str(folder))
    return folder
