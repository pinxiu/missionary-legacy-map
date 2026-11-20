import httpx

from mlm import preflight


def fake_get(status: int, text: str = ""):
    def get(url, **kw):
        return httpx.Response(status, text=text, request=httpx.Request("GET", url))
    return get


def test_rejected_anthropic_key_is_reported(monkeypatch):
    monkeypatch.setattr(httpx, "get", fake_get(401, '{"error":{"message":"invalid x-api-key"}}'))
    p = preflight.check_anthropic("sk-ant-dead")
    assert p and "ANTHROPIC_API_KEY" in p and "401" in p


def test_working_or_missing_anthropic_key(monkeypatch):
    monkeypatch.setattr(httpx, "get", fake_get(200, "{}"))
    assert preflight.check_anthropic("sk-ant-ok") is None
    assert "not set" in preflight.check_anthropic(None)


def test_network_blip_does_not_block(monkeypatch):
    def boom(url, **kw):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(httpx, "get", boom)
    assert preflight.check_anthropic("sk-ant-ok") is None
    assert preflight.check_supabase("https://x.supabase.co", "sb_secret_ok") is None


def test_supabase_checks(monkeypatch):
    monkeypatch.setattr(httpx, "get", fake_get(401, "Invalid API key"))
    assert "SUPABASE_SECRET_KEY" in preflight.check_supabase("https://x.supabase.co", "sb_secret_dead")
    assert "publishable" in preflight.check_supabase("https://x.supabase.co", "sb_publishable_x")
    monkeypatch.setattr(httpx, "get", fake_get(200, "[]"))
    assert preflight.check_supabase("https://x.supabase.co", "sb_secret_ok") is None


def test_run_exit_code(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)  # no GitHub calls in tests
    monkeypatch.setattr(preflight, "problems", lambda: ["ANTHROPIC_API_KEY was rejected"])
    assert preflight.run() == 1
    monkeypatch.setattr(preflight, "problems", lambda: [])
    assert preflight.run() == 0
