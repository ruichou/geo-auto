from types import SimpleNamespace

import pytest

from hongtu_geo.providers import (
    ChatCompletionsAdapter,
    ResponsesAdapter,
    build_provider_adapter,
    extract_urls,
)


class DumpableResponse:
    def __init__(self, payload, **attributes):
        self.payload = payload
        for key, value in attributes.items():
            setattr(self, key, value)

    def model_dump(self, mode="json"):
        return self.payload


def test_extract_urls_reads_nested_sdk_payload_and_deduplicates() -> None:
    payload = {
        "output": [{"annotations": [{"type": "url_citation", "url": "https://one.example/a"}]}],
        "text": "参考 https://two.example/b，也可看 https://one.example/a",
    }
    assert extract_urls(payload) == ["https://one.example/a", "https://two.example/b"]


def test_responses_adapter_collects_answer_and_citations() -> None:
    response = DumpableResponse(
        {"output": [{"annotations": [{"url": "https://source.example/report"}]}]},
        output_text="可核查答案",
        id="resp-1",
    )
    create = lambda **kwargs: response
    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    answer = ResponsesAdapter(client, "model-a").ask("问题")
    assert answer.text == "可核查答案"
    assert answer.citation_urls == ["https://source.example/report"]
    assert answer.metadata == {"response_id": "resp-1"}


def test_chat_adapter_collects_top_level_citations() -> None:
    message = SimpleNamespace(content="答案")
    response = DumpableResponse(
        {"choices": []},
        choices=[SimpleNamespace(message=message)],
        citations=["https://source.example/a"],
        id="chat-1",
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: response)))
    answer = ChatCompletionsAdapter(client, "model-b").ask("问题")
    assert answer.text == "答案"
    assert answer.citation_urls == ["https://source.example/a"]


def test_build_provider_adapter_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="不支持"):
        build_provider_adapter(
            {"kind": "unknown", "model": "x"},
            "key",
            client_factory=lambda **kwargs: object(),
        )
