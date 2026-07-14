import pytest


@pytest.fixture(autouse=True)
def isolated_call_log(tmp_path, monkeypatch):
    """Keep router call logs out of ~/.vetter during tests."""
    monkeypatch.setenv("VETTER_LOG_DIR", str(tmp_path / "vetter-logs"))
    return tmp_path / "vetter-logs" / "calls.jsonl"
