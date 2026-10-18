"""Minimal OpenAI-compatible chat client for the NRP LLM endpoint.

Reads LLM_BASE_URL, LLM_API_KEY and LLM_MODEL from .env. Every failure raises LLMError with a readable message;
the API key is never included in an error.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

import requests

from floodalpha import config

DEFAULT_TIMEOUT_S = 420   # qwen3 reasons before answering; the portfolio call reads all 14 analyst views
TEMPERATURE = 0.2
REQUIRED_ENV = ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")
REASONING_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
ERROR_BODY_CHARS = 200


class LLMError(RuntimeError):
    pass


def strip_reasoning(text: str) -> str:
    """Drop Qwen-style <think> blocks. An unterminated block (cut off by the token limit) is dropped to its end."""
    out = REASONING_RE.sub("", text)
    lower = out.lower()
    if "<think>" in lower:
        out = out[:lower.index("<think>")]
    return out.strip()


def extract_json(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise LLMError("LLM reply has no JSON object")
    try:
        obj = json.loads(text[start:end + 1])
    except ValueError as exc:
        raise LLMError("LLM reply is not valid JSON") from exc
    if not isinstance(obj, dict):
        raise LLMError("LLM reply JSON is not an object")
    return obj


def _default_transport(url: str, headers: dict, body: dict, timeout: float) -> Any:
    return requests.post(url, headers=headers, json=body, timeout=timeout)


class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str, timeout_s: float = DEFAULT_TIMEOUT_S,
                 transport: Callable[..., Any] | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s
        self._post = transport or _default_transport

    @classmethod
    def from_env(cls) -> LLMClient:
        vals = {name: config.env(name) for name in REQUIRED_ENV}
        missing = [name for name, v in vals.items() if not v]
        if missing:
            raise LLMError(f"{', '.join(missing)} not set; add to .env (see .env.example)")
        return cls(vals["LLM_BASE_URL"], vals["LLM_API_KEY"], vals["LLM_MODEL"])

    def _redact(self, text: str) -> str:
        return (text or "").replace(self.api_key, "***")[:ERROR_BODY_CHARS]

    def chat_json(self, system: str, user: str) -> dict:
        body = {"model": self.model, "temperature": TEMPERATURE,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            resp = self._post(f"{self.base_url}/chat/completions", headers, body, self.timeout_s)
        except Exception as exc:
            raise LLMError(f"LLM request failed: {self._redact(str(exc))}") from exc
        if resp.status_code != 200:
            raise LLMError(f"LLM returned HTTP {resp.status_code}: {self._redact(resp.text)}")
        try:
            content = resp.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError("LLM reply had no message content") from exc
        return extract_json(strip_reasoning(content or ""))
