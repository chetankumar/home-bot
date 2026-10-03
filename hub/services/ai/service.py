"""The AI service: aliases, per-app policy, usage logging, structured extraction.

Models are addressed as "provider:model" (split on the first colon, so
"ollama:llama3.1:8b" works) or by an alias from hub.toml [ai.aliases].
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from hub.config import Config
from hub.services.ai.anthropic import AnthropicProvider
from hub.services.ai.base import (
    AIError,
    AIPolicyError,
    Completion,
    ExtractionError,
    Message,
    Provider,
    ProviderUnavailable,
    Usage,
)
from hub.services.ai.gemini import GeminiProvider
from hub.services.ai.openai_compat import OpenAICompatProvider
from hub.services.db import Database

log = logging.getLogger("hub.ai")

T = TypeVar("T", bound=BaseModel)
MessagesIn = str | list[Message] | list[dict[str, str]]


def build_provider(name: str, spec: dict[str, Any], config: Config) -> Provider:
    kind = spec.get("type", "openai_compat")
    key = config.secret(spec.get("api_key_env", f"{name.upper()}_API_KEY"))
    timeout = float(spec.get("timeout", 120))
    if kind == "anthropic":
        return AnthropicProvider(name, key, spec.get("base_url"), timeout)
    if kind == "openai_compat":
        return OpenAICompatProvider(
            name,
            key,
            spec.get("base_url"),
            key_required=spec.get("key_required", True),
            timeout=timeout,
            max_tokens_param=spec.get("max_tokens_param", "max_tokens"),
        )
    if kind == "gemini":
        return GeminiProvider(name, key, timeout)
    raise ValueError(f"unknown provider type {kind!r} for provider {name!r}")


def normalize_messages(messages: MessagesIn, system: str | None = None) -> list[Message]:
    if isinstance(messages, str):
        out = [Message("user", messages)]
    else:
        out = [m if isinstance(m, Message) else Message(m["role"], m["content"]) for m in messages]
    if system:
        out.insert(0, Message("system", system))
    return out


_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)


def _decode_json(data: dict[str, Any] | str) -> Any:
    if isinstance(data, dict):
        return data
    m = _FENCE.match(data)  # small local models like to fence their JSON
    return json.loads(m.group(1) if m else data)


class AIService:
    def __init__(self, config: Config, hub_db: Database):
        self._config = config
        self._db = hub_db
        ai = config.ai
        self.aliases: dict[str, str] = dict(ai.get("aliases", {}))
        self.providers: dict[str, Provider] = {}
        for name, spec in ai.get("providers", {}).items():
            try:
                self.providers[name] = build_provider(name, spec, config)
            except Exception:
                log.exception("could not configure AI provider %s", name)

    # -- resolution ----------------------------------------------------------
    def resolve(self, ref: str) -> tuple[str, str]:
        """Alias or "provider:model" -> (provider, model)."""
        seen = set()
        while ref in self.aliases:
            if ref in seen:
                raise AIError(f"alias loop at {ref!r}")
            seen.add(ref)
            ref = self.aliases[ref]
        provider, sep, model = ref.partition(":")
        if not sep or not model:
            raise AIError(f"{ref!r} is neither an alias nor 'provider:model'")
        return provider, model

    def provider(self, name: str) -> Provider:
        p = self.providers.get(name)
        if p is None:
            raise ProviderUnavailable(f"provider {name!r} is not configured in hub.toml")
        if not p.available:
            raise ProviderUnavailable(f"provider {name!r} has no API key configured")
        return p

    def status(self) -> list[dict[str, Any]]:
        return [{"name": n, "available": p.available} for n, p in self.providers.items()]

    def for_app(self, app_id: str) -> AppAI:
        policy = self._config.app_config(app_id).get("ai", {})
        return AppAI(self, app_id, policy.get("allowed_providers"), policy.get("default_alias"))

    # -- usage -----------------------------------------------------------------
    def record(
        self, app_id: str, provider: str, model: str, op: str, usage: Usage | None, error=None
    ) -> None:
        try:
            with self._db() as conn:
                conn.execute(
                    "INSERT INTO ai_usage(app_id, provider, model, op, input_tokens,"
                    " output_tokens, ok, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        app_id,
                        provider,
                        model,
                        op,
                        usage.input_tokens if usage else None,
                        usage.output_tokens if usage else None,
                        0 if error else 1,
                        str(error)[:500] if error else None,
                    ),
                )
        except Exception:
            log.exception("failed to record AI usage")

    def usage_summary(self) -> list[dict[str, Any]]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT app_id, provider, model, COUNT(*) AS calls,"
                " SUM(ok = 0) AS errors, SUM(input_tokens) AS input_tokens,"
                " SUM(output_tokens) AS output_tokens, MAX(ts) AS last_used"
                " FROM ai_usage GROUP BY app_id, provider, model"
                " ORDER BY app_id, calls DESC"
            ).fetchall()
        return [dict(r) for r in rows]


class AppAI:
    """The AI service as seen by one app: policy enforced, usage tagged."""

    def __init__(
        self,
        service: AIService,
        app_id: str,
        allowed_providers: list[str] | None,
        default_alias: str | None,
    ):
        self._svc = service
        self.app_id = app_id
        self.allowed_providers = allowed_providers
        self.default_model = default_alias or "default"

    def _target(self, model: str | None) -> tuple[Provider, str]:
        provider_name, model_name = self._svc.resolve(model or self.default_model)
        if self.allowed_providers is not None and provider_name not in self.allowed_providers:
            log.warning("app %s denied provider %s by policy", self.app_id, provider_name)
            raise AIPolicyError(
                f"app '{self.app_id}' may only use {self.allowed_providers}, not '{provider_name}'"
            )
        return self._svc.provider(provider_name), model_name

    def resolve(self, model: str | None = None) -> tuple[str, str]:
        """Alias (default: this app's) -> (provider, model). Doesn't check policy or keys."""
        return self._svc.resolve(model or self.default_model)

    def available(self, model: str | None = None) -> bool:
        try:
            self._target(model)
            return True
        except AIError:
            return False

    def complete(
        self,
        messages: MessagesIn,
        *,
        model: str | None = None,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float | None = None,
    ) -> Completion:
        provider, model_name = self._target(model)
        msgs = normalize_messages(messages, system)
        try:
            result = provider.complete(model_name, msgs, max_tokens, temperature)
        except Exception as e:
            self._svc.record(self.app_id, provider.name, model_name, "complete", None, e)
            raise AIError(f"{provider.name}: {e}") from e
        self._svc.record(self.app_id, provider.name, model_name, "complete", result.usage)
        return result

    def stream(
        self,
        messages: MessagesIn,
        *,
        model: str | None = None,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float | None = None,
    ) -> Iterator[str]:
        provider, model_name = self._target(model)  # policy checked before iteration starts
        msgs = normalize_messages(messages, system)

        def gen() -> Iterator[str]:
            try:
                yield from provider.stream(model_name, msgs, max_tokens, temperature)
            except Exception as e:
                self._svc.record(self.app_id, provider.name, model_name, "stream", None, e)
                raise AIError(f"{provider.name}: {e}") from e
            self._svc.record(self.app_id, provider.name, model_name, "stream", None)

        return gen()

    def extract(
        self,
        messages: MessagesIn,
        *,
        schema: type[T],
        model: str | None = None,
        system: str | None = None,
        max_tokens: int = 1024,
    ) -> T:
        """Return a validated instance of `schema`, retrying once on bad output."""
        provider, model_name = self._target(model)
        msgs = normalize_messages(messages, system)
        json_schema = schema.model_json_schema()
        last_error: Exception | None = None
        for _attempt in range(2):
            try:
                result = provider.extract(
                    model_name, msgs, json_schema, schema.__name__, max_tokens
                )
            except Exception as e:
                self._svc.record(self.app_id, provider.name, model_name, "extract", None, e)
                raise AIError(f"{provider.name}: {e}") from e
            try:
                value = schema.model_validate(_decode_json(result.data))
            except (ValueError, ValidationError) as e:  # JSONDecodeError is a ValueError
                last_error = e
                self._svc.record(
                    self.app_id, provider.name, model_name, "extract", result.usage, e
                )
                raw = result.data if isinstance(result.data, str) else json.dumps(result.data)
                msgs = msgs + [
                    Message("assistant", raw),
                    Message(
                        "user",
                        f"That output was invalid: {e}\n"
                        "Reply again with only JSON that matches the schema exactly.",
                    ),
                ]
                continue
            self._svc.record(self.app_id, provider.name, model_name, "extract", result.usage)
            return value
        raise ExtractionError(f"structured output failed validation twice: {last_error}")
