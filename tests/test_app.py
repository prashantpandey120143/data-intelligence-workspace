"""Smoke tests: the app must start cleanly in local mode and accept no-data state."""
import os
from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parent.parent / "app.py")


def test_app_starts_with_empty_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    monkeypatch.delenv("VOLUME_URI", raising=False)
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert at.title[0].value == "Data Intelligence Workspace"
    assert any("No datasets uploaded yet" in i.value for i in at.info)
    for sub in ("uploads", "metadata", "exports"):
        assert (tmp_path / sub).is_dir()
