"""OpenAI-compatible adapter: OpenAI, OpenRouter, Ollama, or any base_url.

Structured output uses the JSON-schema response_format, which OpenAI,
OpenRouter (for models that support it) and Ollama (>= 0.5) all accept.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import openai

from hub.services.ai.base import Completion, Message, StructuredResult, Usage


class OpenAICompatProvider:
    def __init__(
        self,
        name: str,
        api_key: str,
        base_url: str | None = None,
        key_required: bool = True,
        timeout: float = 120,
        max_tokens_param: str = "max_tokens",
    ):
        self.name = name
        self._api_key = api_key
        self._base_url = base_url
        self._key_required = key_required
        self._timeout = timeout
        self._max_tokens_param = max_tokens_param
        self._client: Any = None

    @property
    def available(self) -> bool:
        return bool(self._api_key) or not self._key_required

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = openai.OpenAI(
                # Local servers ignore the key, but the SDK insists on one.
                api_key=self._api_key or "not-needed",
                base_url=self._base_url,
                timeout=self._timeout,
                max_retries=1,
            )
        return self._client

    def _params(self, model, messages: list[Message], max_tokens, temperature) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            self._max_tokens_param: max_tokens,
        }
        if temperature is not None:
            params["temperature"] = temperature
        return params

    def complete(self, model, messages, max_tokens, temperature) -> Completion:
        resp = self.client.chat.completions.create(
            **self._params(model, messages, max_tokens, temperature)
        )
        return Completion(resp.choices[0].message.content or "", self.name, model, _usage(resp))

    def stream(self, model, messages, max_tokens, temperature) -> Iterator[str]:
        params = self._params(model, messages, max_tokens, temperature)
        for chunk in self.client.chat.completions.create(**params, stream=True):
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    def extract(self, model, messages, schema, schema_name, max_tokens) -> StructuredResult:
        params = self._params(model, messages, max_tokens, 0)
        params["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "schema": schema},
        }
        resp = self.client.chat.completions.create(**params)
        return StructuredResult(resp.choices[0].message.content or "", _usage(resp))


def _usage(resp: Any) -> Usage:
    u = getattr(resp, "usage", None)
    return Usage(getattr(u, "prompt_tokens", None), getattr(u, "completion_tokens", None))
