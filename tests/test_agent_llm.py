"""OpenAI-compatible chat client: request shape, reasoning-tag stripping, JSON extraction, explicit failures."""
from __future__ import annotations

import json

import pytest

from quantcomm.agent.llm import LLMClient, LLMError, extract_json, strip_reasoning

SECRET = "sk-test-SECRET-123"


class FakeResponse:
    def __init__(self, status: int, body: dict | str):
        self.status_code = status
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("body is text, not JSON")
        return self._body


def _completion(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def test_strip_reasoning_removes_think_blocks_and_fences():
    raw = "<think>weigh the storms...</think>\n```json\n{\"view\": \"neutral\"}\n```"
    assert extract_json(strip_reasoning(raw)) == {"view": "neutral"}


def test_extract_json_finds_the_object_inside_prose():
    assert extract_json('Here you go: {"a": {"b": 2}} thanks') == {"a": {"b": 2}}


def test_extract_json_rejects_non_object_and_garbage():
    with pytest.raises(LLMError):
        extract_json("no json here")
    with pytest.raises(LLMError):
        extract_json("[1, 2, 3]")


def test_chat_json_posts_to_chat_completions_and_parses(monkeypatch):
    calls = []

    def transport(url, headers, body, timeout):
        calls.append((url, headers, body, timeout))
        return FakeResponse(200, _completion('<think>x</think>{"ok": true}'))

    client = LLMClient("https://llm.example/v1/", SECRET, "qwen3", timeout_s=7, transport=transport)
    assert client.chat_json("sys", "usr") == {"ok": True}
    url, headers, body, timeout = calls[0]
    assert url == "https://llm.example/v1/chat/completions"
    assert headers["Authorization"] == f"Bearer {SECRET}"
    assert body["model"] == "qwen3" and body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["messages"][1] == {"role": "user", "content": "usr"} and timeout == 7


def test_http_error_is_explicit_and_never_echoes_the_key():
    client = LLMClient("https://llm.example/v1", SECRET, "qwen3",
                       transport=lambda *a: FakeResponse(401, f"bad key {SECRET}"))
    with pytest.raises(LLMError) as exc:
        client.chat_json("s", "u")
    assert "401" in str(exc.value) and SECRET not in str(exc.value)


def test_transport_failure_is_wrapped(monkeypatch):
    def boom(*a):
        raise ConnectionError("no route")

    with pytest.raises(LLMError, match="no route"):
        LLMClient("https://llm.example/v1", SECRET, "qwen3", transport=boom).chat_json("s", "u")


def test_non_json_reply_is_an_error_not_a_guess():
    client = LLMClient("https://llm.example/v1", SECRET, "qwen3",
                       transport=lambda *a: FakeResponse(200, _completion("I think it will go up.")))
    with pytest.raises(LLMError, match="JSON"):
        client.chat_json("s", "u")


def test_from_env_requires_all_three_settings(monkeypatch):
    for name in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("quantcomm.config.load_dotenv", lambda *a, **k: None)
    with pytest.raises(LLMError, match="LLM_BASE_URL"):
        LLMClient.from_env()
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("LLM_API_KEY", SECRET)
    monkeypatch.setenv("LLM_MODEL", "qwen3")
    client = LLMClient.from_env()
    assert client.model == "qwen3" and client.base_url == "https://llm.example/v1"
