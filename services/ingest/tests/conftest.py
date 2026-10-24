import pytest


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GATEWAY_URL", "http://gw")
    monkeypatch.setenv("QDRANT_URL", "http://qd:6333")
    monkeypatch.setenv("QDRANT_COLLECTION", "kb")
