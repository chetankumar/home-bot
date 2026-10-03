from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from hub.services.ai import AIError, AIPolicyError, ExtractionError, ProviderUnavailable
from hub.services.ai.anthropic import AnthropicProvider
from hub.services.ai.base import Completion, StructuredResult, Usage
from hub.services.ai.openai_compat import OpenAICompatProvider
from hub.services.ai.service import AIService
from hub.services.db import hub_database
from tests.conftest import make_config


class Item(BaseModel):
    name: str
    qty: int


class FakeProvider:
    """Records calls; extract() returns queued outputs in order."""

    def __init__(self, name, outputs=None, available=True):
        self.name = name
        self.available = available
        self.calls = []
        self.outputs = list(outputs or [])

    def complete(self, model, messages, max_tokens, temperature):
        self.calls.append(("complete", model, messages))
        return Completion("hi", self.name, model, Usage(3, 1))

    def stream(self, model, messages, max_tokens, temperature):
        self.calls.append(("stream", model, messages))
        yield from ["a", "b"]

    def extract(self, model, messages, schema, schema_name, max_tokens):
        self.calls.append(("extract", model, messages))
        return StructuredResult(self.outputs.pop(0), Usage(5, 5))


TOML = {
    "ai": {
        "providers": {"anthropic": {"type": "anthropic"}, "ollama": {"type": "openai_compat"}},
        "aliases": {"default": "anthropic:claude-opus-5-5", "local": "ollama:llama3.1:8b", "cheap": "local"},
    },
    "apps": {"finance": {"ai": {"allowed_providers": ["ollama"], "default_alias": "local"}}},
}


@pytest.fixture
def svc(tmp_path):
    cfg = make_config(tmp_path, toml=TOML)
    service = AIService(cfg, hub_database(cfg.data_dir))
    service.providers = {"anthropic": FakeProvider("anthropic"), "ollama": FakeProvider("ollama")}
    return service


def test_alias_resolution(svc):
    assert svc.resolve("default") == ("anthropic", "claude-opus-5-5")
    assert svc.resolve("cheap") == ("ollama", "llama3.1:8b")  # alias of alias; model keeps its colon
    assert svc.resolve("ollama:qwen2.5:7b") == ("ollama", "qwen2.5:7b")
    with pytest.raises(AIError):
        svc.resolve("nonsense")


def test_policy_blocks_cloud_provider_before_any_request(svc):
    fin = svc.for_app("finance")
    with pytest.raises(AIPolicyError):
        fin.complete("hello", model="default")
    with pytest.raises(AIPolicyError):
        fin.extract("hello", schema=Item, model="anthropic:claude-haiku-4-5-20251001")
    with pytest.raises(AIPolicyError):
        fin.stream("hello", model="default")
    assert svc.providers["anthropic"].calls == []
    # its default alias is the local model, which is allowed
    assert fin.complete("hello").provider == "ollama"
    assert not fin.available("default") and fin.available()


def test_unrestricted_app_uses_default_alias(svc):
    result = svc.for_app("hello").complete("hi", system="be brief")
    assert (result.provider, result.model) == ("anthropic", "claude-opus-5-5")
    _, _, messages = svc.providers["anthropic"].calls[0]
    assert [m.role for m in messages] == ["system", "user"]


def test_usage_is_logged_per_app(svc):
    svc.for_app("finance").complete("x")
    svc.for_app("hello").complete("x")
    rows = {(r["app_id"], r["provider"]) for r in svc.usage_summary()}
    assert rows == {("finance", "ollama"), ("hello", "anthropic")}


def test_unavailable_provider(svc):
    svc.providers["anthropic"].available = False
    with pytest.raises(ProviderUnavailable):
        svc.for_app("hello").complete("x")


def test_extract_validates_and_retries_once(svc):
    p = svc.providers["ollama"]
    p.outputs = ['{"name": "milk"}', '```json\n{"name": "milk", "qty": 2}\n```']
    item = svc.for_app("finance").extract("2 milk", schema=Item)
    assert item == Item(name="milk", qty=2)
    retry_messages = p.calls[1][2]
    assert retry_messages[-1].role == "user" and "invalid" in retry_messages[-1].content


def test_extract_gives_up_after_second_failure(svc):
    svc.providers["ollama"].outputs = ["not json", '{"qty": "many"}']
    with pytest.raises(ExtractionError):
        svc.for_app("finance").extract("x", schema=Item)


def test_stream(svc):
    assert "".join(svc.for_app("hello").stream("x")) == "ab"


# -- adapters against mocked SDK clients -------------------------------------------------
def test_openai_compat_extract_uses_json_schema():
    seen = {}

    def create(**params):
        seen.update(params)
        msg = SimpleNamespace(content='{"name": "eggs", "qty": 12}')
        return SimpleNamespace(
            choices=[SimpleNamespace(message=msg)],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=4),
        )

    p = OpenAICompatProvider("ollama", "", key_required=False)
    p._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    from hub.services.ai.base import Message

    out = p.extract("m", [Message("user", "12 eggs")], Item.model_json_schema(), "Item", 100)
    assert out.data == '{"name": "eggs", "qty": 12}' and out.usage.input_tokens == 10
    assert seen["response_format"]["type"] == "json_schema"
    assert seen["response_format"]["json_schema"]["schema"]["required"] == ["name", "qty"]
    assert p.available  # no key needed for local servers


def test_anthropic_extract_uses_forced_tool():
    seen = {}

    def create(**params):
        seen.update(params)
        block = SimpleNamespace(type="tool_use", input={"name": "tea", "qty": 1})
        return SimpleNamespace(content=[block], usage=SimpleNamespace(input_tokens=7, output_tokens=3))

    p = AnthropicProvider("anthropic", "key")
    p._client = SimpleNamespace(messages=SimpleNamespace(create=create))
    from hub.services.ai.base import Message

    out = p.extract(
        "claude-haiku-4-5-20251001",
        [Message("system", "sys"), Message("user", "one tea")],
        Item.model_json_schema(),
        "Item",
        100,
    )
    assert out.data == {"name": "tea", "qty": 1}
    assert seen["tool_choice"] == {"type": "tool", "name": "Item"}
    assert seen["system"] == "sys" and [m["role"] for m in seen["messages"]] == ["user"]
