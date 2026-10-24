import asyncio

import httpx
import pytest
import respx

from app import channels, db, max_poller

UPD = {"update_type": "message_created", "message": {"sender": {"user_id": 5, "name": "Ира"}, "recipient": {"chat_type": "dialog"}, "body": {"text": "привет"}}}


@pytest.fixture(autouse=True)
def tok(monkeypatch):
    monkeypatch.setenv("MAX_BOT_TOKEN", "tok")
    max_poller.recent.clear()


@respx.mock
async def test_poll_once_dispatches_and_saves_marker():
    got = []

    async def handler(p):
        got.append(p)
    route = respx.get("http://max/updates").mock(return_value=httpx.Response(200, json={"updates": [UPD, {"update_type": "dialog_muted"}], "marker": 123}))
    assert await max_poller.poll_once(handler) == 2
    await asyncio.sleep(0)
    assert got == [{"kind": "message", "user_id": "5", "name": "Ира", "text": "привет"}]
    assert db.get_setting("max_marker") == "123" and "marker" not in dict(route.calls[0].request.url.params)
    assert len(max_poller.recent) == 2 and max_poller.state["last_update"]
    await max_poller.poll_once(handler)  # второй запрос уже с маркером
    assert route.calls[1].request.url.params["marker"] == "123"


@respx.mock
async def test_poll_once_error_status_and_null_marker():
    respx.get("http://max/updates").mock(return_value=httpx.Response(401, text="bad"))
    with pytest.raises(RuntimeError, match="401"):
        await max_poller.poll_once(None)
    respx.get("http://max/updates").mock(return_value=httpx.Response(200, json={"updates": [], "marker": None}))
    assert await max_poller.poll_once(None) == 0 and db.get_setting("max_marker") is None


@respx.mock
async def test_run_loop_polls_only_when_enabled_and_reports_errors(monkeypatch):
    calls = {"n": 0}

    async def fake_poll(handler):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("tls fail")
        await asyncio.sleep(0.01)
    monkeypatch.setattr(max_poller, "poll_once", fake_poll)
    task = asyncio.create_task(max_poller.run(None, idle=0.01))
    await asyncio.sleep(0.05)
    assert calls["n"] == 0 and max_poller.state["running"]  # режим web — не опрашиваем
    channels.set_mode("both")
    await asyncio.sleep(0.1)
    assert calls["n"] >= 2 and max_poller.state["last_error"] == "tls fail"
    monkeypatch.setenv("MAX_TRANSPORT", "webhook")
    n = calls["n"]
    await asyncio.sleep(0.1)
    assert calls["n"] == n  # transport=webhook — polling выключен
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not max_poller.state["running"]


def test_config_helpers(monkeypatch):
    from app import config
    monkeypatch.setenv("MAX_CA_BUNDLE", "/etc/ca.pem")
    assert config.max_ca_bundle() == "/etc/ca.pem"
    monkeypatch.delenv("MAX_CA_BUNDLE")
    monkeypatch.setenv("MAX_VERIFY_TLS", "false")
    assert config.max_ca_bundle() is False
    assert config.internal_token() != config.max_token()


def test_app_lifespan_starts_and_stops_poller():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
