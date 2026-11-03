"""The shared Grok client talks only to xAI and only with XAI_API_KEY."""

import json

import pytest

import grok_client


@pytest.fixture(autouse=True)
def _clear_key(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("GROK_MODEL", raising=False)


def test_missing_key_does_not_call_the_network(monkeypatch):
    def forbid(*args, **kwargs):
        raise AssertionError("HTTP should not run without a key")

    monkeypatch.setattr(grok_client, "post_chat", forbid)
    with pytest.raises(grok_client.GrokNotConfigured):
        grok_client.chat_json("system", "user")
    assert grok_client.is_configured() is False
    assert grok_client.model_name() == "grok-4.5"


def test_chat_json_posts_to_xai_with_the_env_model(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "test-key")
    monkeypatch.setenv("GROK_MODEL", "grok-4.20-non-reasoning")
    seen = {}

    def fake_post(payload, api_key_value, timeout=90):
        seen["payload"] = payload
        seen["key"] = api_key_value
        seen["timeout"] = timeout
        body = {"choices": [{"message": {"content": "{\"queries\": [\"Tesla earnings\"]}"}}]}
        return 200, json.dumps(body)

    monkeypatch.setattr(grok_client, "post_chat", fake_post)
    assert grok_client.chat_json("plan the search", "Tesla", timeout=30) == {"queries": ["Tesla earnings"]}
    assert seen["key"] == "test-key"
    assert seen["payload"]["model"] == "grok-4.20-non-reasoning"
    assert seen["payload"]["messages"][0] == {"role": "system", "content": "plan the search"}
    assert seen["payload"]["response_format"] == {"type": "json_object"}


def test_grok_http_error_raises(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "test-key")
    monkeypatch.setattr(grok_client, "post_chat", lambda *args, **kwargs: (401, "bad key"))
    with pytest.raises(grok_client.GrokError):
        grok_client.chat_json("system", "user")
