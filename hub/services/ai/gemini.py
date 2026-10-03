"""Google Gemini adapter (google-genai SDK). Structured output via response schema."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from hub.services.ai.base import Completion, Message, StructuredResult, Usage, split_system


class GeminiProvider:
    def __init__(self, name: str, api_key: str, timeout: float = 120):
        self.name = name
        self._api_key = api_key
        self._timeout = timeout
        self._client: Any = None

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    @property
    def client(self) -> Any:
        if self._client is None:
            from google import genai
            from google.genai import types

            self._client = genai.Client(
                api_key=self._api_key,
                http_options=types.HttpOptions(timeout=int(self._timeout * 1000)),
            )
        return self._client

    def _request(self, messages: list[Message], **config: Any) -> tuple[list, Any]:
        from google.genai import types

        system, rest = split_system(messages)
        contents = [
            types.Content(
                role="model" if m.role == "assistant" else "user",
                parts=[types.Part(text=m.content)],
            )
            for m in rest
        ]
        cfg = types.GenerateContentConfig(
            system_instruction=system, **{k: v for k, v in config.items() if v is not None}
        )
        return contents, cfg

    def complete(self, model, messages, max_tokens, temperature) -> Completion:
        contents, cfg = self._request(
            messages, max_output_tokens=max_tokens, temperature=temperature
        )
        resp = self.client.models.generate_content(model=model, contents=contents, config=cfg)
        return Completion(resp.text or "", self.name, model, _usage(resp))

    def stream(self, model, messages, max_tokens, temperature) -> Iterator[str]:
        contents, cfg = self._request(
            messages, max_output_tokens=max_tokens, temperature=temperature
        )
        for chunk in self.client.models.generate_content_stream(
            model=model, contents=contents, config=cfg
        ):
            if chunk.text:
                yield chunk.text

    def extract(self, model, messages, schema, schema_name, max_tokens) -> StructuredResult:
        contents, cfg = self._request(
            messages,
            max_output_tokens=max_tokens,
            temperature=0,
            response_mime_type="application/json",
            response_json_schema=schema,
        )
        resp = self.client.models.generate_content(model=model, contents=contents, config=cfg)
        return StructuredResult(resp.text or "", _usage(resp))


def _usage(resp: Any) -> Usage:
    u = getattr(resp, "usage_metadata", None)
    return Usage(
        getattr(u, "prompt_token_count", None), getattr(u, "candidates_token_count", None)
    )
