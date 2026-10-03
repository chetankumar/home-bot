"""Anthropic adapter (Messages API). Structured output via forced tool use."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import anthropic

from hub.services.ai.base import (
    AIError,
    Completion,
    StructuredResult,
    Usage,
    split_system,
)


class AnthropicProvider:
    def __init__(self, name: str, api_key: str, base_url: str | None = None, timeout: float = 120):
        self.name = name
        self._api_key = api_key
        self._base_url = base_url
        self._timeout = timeout
        self._client: Any = None

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = anthropic.Anthropic(
                api_key=self._api_key, base_url=self._base_url, timeout=self._timeout
            )
        return self._client

    def _params(self, model, messages, max_tokens, temperature) -> dict[str, Any]:
        system, rest = split_system(messages)
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": m.role, "content": m.content} for m in rest],
        }
        if system:
            params["system"] = system
        if temperature is not None:
            params["temperature"] = temperature
        return params

    def complete(self, model, messages, max_tokens, temperature) -> Completion:
        resp = self.client.messages.create(**self._params(model, messages, max_tokens, temperature))
        text = "".join(b.text for b in resp.content if b.type == "text")
        return Completion(text, self.name, model, _usage(resp))

    def stream(self, model, messages, max_tokens, temperature) -> Iterator[str]:
        params = self._params(model, messages, max_tokens, temperature)
        with self.client.messages.stream(**params) as s:
            yield from s.text_stream

    def extract(self, model, messages, schema, schema_name, max_tokens) -> StructuredResult:
        params = self._params(model, messages, max_tokens, None)
        params["tools"] = [
            {
                "name": schema_name,
                "description": "Record the extracted data.",
                "input_schema": schema,
            }
        ]
        params["tool_choice"] = {"type": "tool", "name": schema_name}
        resp = self.client.messages.create(**params)
        for block in resp.content:
            if block.type == "tool_use":
                return StructuredResult(dict(block.input), _usage(resp))
        raise AIError("model did not return the requested tool call")


def _usage(resp: Any) -> Usage:
    u = getattr(resp, "usage", None)
    return Usage(getattr(u, "input_tokens", None), getattr(u, "output_tokens", None))
