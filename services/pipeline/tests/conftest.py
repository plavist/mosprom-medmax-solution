import pytest


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GATEWAY_URL", "http://gw")
    monkeypatch.setenv("INGEST_URL", "http://ing")
    monkeypatch.setenv("MAX_API_URL", "http://max")
    monkeypatch.delenv("MAX_BOT_TOKEN", raising=False)
    monkeypatch.delenv("MAX_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("CHANNEL_MODE", "web")
