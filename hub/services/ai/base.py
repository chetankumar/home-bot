"""Provider-neutral types shared by the AI service and its adapters."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal, Protocol

Role = Literal["system", "user", "assistant"]


class AIError(Exception):
    """Base class for AI service errors."""


class AIPolicyError(AIError):
    """The calling app is not allowed to use this provider (hub.toml policy)."""


class ProviderUnavailable(AIError):
    """Provider is unknown, or has no API key configured."""


class ExtractionError(AIError):
    """The model's structured output failed validation, even after a retry."""


@dataclass
class Message:
    role: Role
    content: str


@dataclass
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass
class Completion:
    text: str
    provider: str
    model: str
    usage: Usage


@dataclass
class StructuredResult:
    """Raw structured output from a provider, before Pydantic validation.

    `data` is a dict when the provider returned parsed JSON (tool use), or a
    string when it returned JSON text that still needs decoding.
    """

    data: dict[str, Any] | str
    usage: Usage


class Provider(Protocol):
    name: str

    @property
    def available(self) -> bool: ...

    def complete(
        self, model: str, messages: list[Message], max_tokens: int, temperature: float | None
    ) -> Completion: ...

    def stream(
        self, model: str, messages: list[Message], max_tokens: int, temperature: float | None
    ) -> Iterator[str]: ...

    def extract(
        self,
        model: str,
        messages: list[Message],
        schema: dict[str, Any],
        schema_name: str,
        max_tokens: int,
    ) -> StructuredResult: ...


def split_system(messages: list[Message]) -> tuple[str | None, list[Message]]:
    """Pull system messages out (Anthropic and Gemini take them separately)."""
    system = "\n\n".join(m.content for m in messages if m.role == "system") or None
    return system, [m for m in messages if m.role != "system"]
