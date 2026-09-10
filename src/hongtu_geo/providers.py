from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


URL_RE = re.compile(r"https?://[^\s)\]}>，。；、\"']+")


@dataclass
class ProviderAnswer:
    text: str
    citation_urls: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class ProviderAdapter(Protocol):
    surface: str

    def ask(self, prompt: str) -> ProviderAnswer: ...


def _plain_payload(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json")
        except TypeError:
            return value.model_dump()
    if isinstance(value, (dict, list, tuple, str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "__dict__"):
        return vars(value)
    return str(value)


def extract_urls(value: Any) -> list[str]:
    """Collect citation URLs from nested SDK payloads without depending on one SDK version."""
    found: list[str] = []

    def walk(item: Any) -> None:
        item = _plain_payload(item)
        if isinstance(item, str):
            found.extend(URL_RE.findall(item))
        elif isinstance(item, dict):
            for key, child in item.items():
                if key.lower() in {"url", "uri", "source_url", "citation_url"} and isinstance(child, str):
                    if child.startswith(("http://", "https://")):
                        found.append(child)
                walk(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                walk(child)

    walk(value)
    return list(dict.fromkeys(found))


class ResponsesAdapter:
    surface = "api"

    def __init__(self, client: Any, model: str, request_options: dict[str, Any] | None = None):
        self.client = client
        self.model = model
        self.request_options = request_options or {}

    def ask(self, prompt: str) -> ProviderAnswer:
        response = self.client.responses.create(
            model=self.model,
            input=prompt,
            store=False,
            **self.request_options,
        )
        text = str(getattr(response, "output_text", "") or "")
        return ProviderAnswer(
            text=text,
            citation_urls=extract_urls(response),
            metadata={"response_id": str(getattr(response, "id", "") or "")},
        )


class ChatCompletionsAdapter:
    surface = "api"

    def __init__(self, client: Any, model: str, request_options: dict[str, Any] | None = None):
        self.client = client
        self.model = model
        self.request_options = request_options or {}

    def ask(self, prompt: str) -> ProviderAnswer:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            **self.request_options,
        )
        text = str(response.choices[0].message.content or "")
        citations = extract_urls(response)
        # Several OpenAI-compatible providers expose citations as a top-level array.
        citations.extend(str(url) for url in getattr(response, "citations", []) if str(url).startswith("http"))
        return ProviderAnswer(
            text=text,
            citation_urls=list(dict.fromkeys(citations)),
            metadata={"response_id": str(getattr(response, "id", "") or "")},
        )


def build_provider_adapter(
    provider: dict[str, Any],
    api_key: str,
    client_factory: Callable[..., Any] | None = None,
) -> ProviderAdapter:
    if client_factory is None:
        from openai import OpenAI
        client_factory = OpenAI
    client = client_factory(api_key=api_key, base_url=provider.get("base_url"))
    request_options = provider.get("request_options", {})
    if not isinstance(request_options, dict):
        raise ValueError("provider.request_options 必须是对象")
    kind = provider.get("kind")
    if kind == "responses":
        return ResponsesAdapter(client, str(provider["model"]), request_options)
    if kind == "openai-compatible":
        return ChatCompletionsAdapter(client, str(provider["model"]), request_options)
    raise ValueError(f"不支持的 provider.kind：{kind}")
