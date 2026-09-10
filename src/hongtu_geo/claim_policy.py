from __future__ import annotations

import re
from typing import Any

from .core import Settings


DEFAULT_PROHIBITED_MARKERS = (
    "行业第一", "排名第一", "全国第一", "全网第一",
    "最好的一家", "全网最好", "全国最好", "唯一推荐",
    "保证成交", "保证中标", "保证获客", "保证推荐", "保证收录",
    "百分百成交", "100%成交", "百分百中标", "100%中标",
    "百分百真实", "100%真实", "绝对真实", "永不过期",
    "AI一定推荐", "AI保证推荐", "大模型一定推荐",
    "DeepSeek一定推荐", "ChatGPT一定推荐", "豆包一定推荐",
    "绝对领先",
)

HIGH_RISK_PATTERNS = (
    re.compile(r"(?:保证|确保|承诺).{0,8}(?:成交|中标|获客|推荐|收录|排名)"),
    re.compile(r"(?:行业|全国|全网|本地).{0,4}(?:第一|最好|最优|唯一)"),
    re.compile(r"(?:100%|百分之百|百分百|绝对|一定|必然).{0,8}(?:成交|中标|推荐|收录|有效|真实)"),
    re.compile(r"(?:DeepSeek|ChatGPT|豆包|AI|大模型).{0,8}(?:一定|保证|必然).{0,8}(?:推荐|提及|引用|收录)"),
    re.compile(r"(?:是|为|堪称).{0,3}(?:第一名|首选|绝对领先)"),
)

NEGATION_SUFFIXES = ("不", "不作", "不做", "无法", "不能", "并非", "不是", "绝不", "不承诺", "不予")
IGNORABLE_SEPARATORS = re.compile(r"[\s\-_\u2010-\u2015·•・]+")


def _is_negated(text: str, start: int) -> bool:
    prefix = text[max(0, start - 8):start].rstrip()
    return any(prefix.endswith(marker) for marker in NEGATION_SUFFIXES) or bool(
        re.search(r"(?:不|无须|无需|无法|不能|并非|不是|禁止|避免|不得|勿).{0,6}$", prefix)
    )


def _is_question_context(text: str, start: int, end: int) -> bool:
    sentence_boundaries = ("\n", "。", "！", "？", "!", "?")
    left = max(text.rfind(marker, 0, start) for marker in sentence_boundaries) + 1
    candidates = [
        position for marker in sentence_boundaries
        if (position := text.find(marker, end)) >= 0
    ]
    right = min(candidates) if candidates else len(text)
    segment = text[left:min(right + 1, len(text))]
    if "?" not in segment and "？" not in segment:
        return False
    prefix = text[left:start]
    suffix = text[end:right]
    prefix_cues = ("是否", "能否", "会不会", "可否", "为什么", "有人说", "听说")
    strong_suffix_cues = ("是否", "真的", "是不是", "能否", "会不会", "是否属实", "代表")
    if any(cue in prefix for cue in prefix_cues) or any(cue in suffix for cue in strong_suffix_cues):
        return True
    has_clause_break = any(marker in suffix for marker in ("，", ",", "；", ";"))
    return not has_clause_break


def prohibited_markers(settings: Settings) -> tuple[str, ...]:
    values = list(DEFAULT_PROHIBITED_MARKERS)
    configured = settings.raw.get("claim_policy", {}).get("prohibited_markers", [])
    if isinstance(configured, list):
        values.extend(
            str(value).strip() for value in configured
            if isinstance(value, str) and len(value.strip()) >= 2
        )
    return tuple(dict.fromkeys(values))


def audit_claim_language(text: str, settings: Settings) -> dict[str, Any]:
    scan_text = IGNORABLE_SEPARATORS.sub("", text)
    matches: list[dict[str, str]] = []
    for marker in prohibited_markers(settings):
        scan_marker = IGNORABLE_SEPARATORS.sub("", marker)
        if not scan_marker:
            continue
        start = 0
        while True:
            index = scan_text.find(scan_marker, start)
            if index < 0:
                break
            if not _is_negated(scan_text, index) and not _is_question_context(
                scan_text, index, index + len(scan_marker)
            ):
                matches.append({"type": "marker", "value": marker})
                break
            start = index + len(scan_marker)
    for pattern in HIGH_RISK_PATTERNS:
        for match in pattern.finditer(scan_text):
            if not _is_negated(scan_text, match.start()) and not _is_question_context(
                scan_text, match.start(), match.end()
            ):
                matches.append({"type": "pattern", "value": match.group(0)})
                break
    unique = list({(item["type"], item["value"]): item for item in matches}.values())
    return {
        "passed": not unique,
        "matches": unique,
        "guardrail": "阻止无可核查依据的保证、绝对化排名和第三方 AI 推荐承诺；明确否定性风险提示不计为承诺。",
    }
