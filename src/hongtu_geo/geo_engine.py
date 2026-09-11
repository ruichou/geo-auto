from __future__ import annotations

import hashlib
import json
import re
import math
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .core import Database, Settings, now_iso
from .claim_policy import audit_claim_language
from .citation_verifier import (
    canonicalize_citation_url,
    citation_allowed_domains,
    domain_is_allowlisted,
    sanitize_citation_text,
    verify_citation_url,
)
from .evidence import audit_brand_facts
from .lead_identity import verified_lead_identity
from .provenance import PROBE_PROVENANCE_SCHEMA_VERSION, STRATEGY_SCHEMA_VERSION
from .browser_activity import build_browser_activity_health


URL_PATTERN = re.compile(r"https?://[^\s)\]}>，。；、]+")
POSITIVE_TERMS = ("推荐", "值得", "适合", "可以", "优势", "可靠", "便捷", "有助于", "优先")
NEGATIVE_TERMS = ("不推荐", "不可靠", "风险", "投诉", "谨慎", "缺点", "无法确认", "不建议")
PHONE_PATTERN = re.compile(
    r"(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d(?:[-\s]?\d){8}(?!\d)"
    r"|(?<!\d)0\d{2,3}[-\s]?\d{7,8}(?!\d)"
    r"|(?<!\d)400[-\s]?\d{3}[-\s]?\d{4}(?!\d)"
)
QUANTIFIED_BRAND_CLAIM_PATTERN = re.compile(
    r"(?:累计|拥有|覆盖|服务|收录|汇聚|提供).{0,12}\d+(?:\.\d+)?\s*(?:万|千|百)?\s*(?:家|条|个|项|%|％)"
)
WEB_TARGET_PATTERN = re.compile(
    r"(?P<target>https?://[^\s，。！？；;]+|(?:www\.)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}(?:/[^\s，。！？；;]*)?)",
    re.IGNORECASE,
)
OFFICIAL_URL_ASSERTION_PATTERN = re.compile(
    r"(?:官网|官方网站|官方网址|网址)(?:地址)?\s*(?:是|为|：|:|在)?\s*" + WEB_TARGET_PATTERN.pattern,
    re.IGNORECASE,
)


def _repeat_target(settings: Settings) -> int:
    try:
        configured = int(settings.raw.get("monitor", {}).get("samples_per_prompt", 3))
    except (TypeError, ValueError):
        configured = 3
    return max(3, configured)


def _parse_timestamp_utc(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (TypeError, ValueError):
        return None


def _normalized_host(url: str) -> str:
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        # Accessing port validates syntax and the 1..65535 range.
        parts.port
    except ValueError:
        return ""
    return _canonical_domain(host)


def _canonical_domain(value: Any) -> str:
    """Return a strict ASCII hostname, or an empty string for malformed input."""
    raw = str(value or "").strip().lower()
    if not raw or any(character in raw for character in "/\\@,?#") or re.search(r"\s", raw):
        return ""
    host = raw
    if ":" in raw:
        if raw.count(":") != 1:
            return ""
        host, port = raw.rsplit(":", 1)
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            return ""
    if host.endswith("."):
        host = host[:-1]
        if host.endswith("."):
            return ""
    try:
        host = host.encode("idna").decode("ascii").removeprefix("www.")
    except UnicodeError:
        return ""
    labels = host.split(".")
    label_pattern = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    if len(labels) < 2 or len(host) > 253 or any(not label_pattern.fullmatch(label) for label in labels):
        return ""
    return host


def _host_is(url: str, domain: str) -> bool:
    host = _normalized_host(url)
    expected = domain.lower().removeprefix("www.").rstrip(".")
    return bool(expected and (host == expected or host.endswith("." + expected)))


def _detect_competitors(answer: str, settings: Settings) -> list[str]:
    canonical_order: list[str] = []
    candidates: set[tuple[str, str]] = set()
    for item in settings.raw.get("monitor", {}).get("competitors", []):
        name = str(item.get("name", item) if isinstance(item, dict) else item).strip()
        if not name:
            continue
        if name not in canonical_order:
            canonical_order.append(name)
        aliases = item.get("aliases", []) if isinstance(item, dict) else []
        terms = [name, *(str(alias).strip() for alias in aliases if isinstance(aliases, list))]
        candidates.update((term, name) for term in terms if term)
    occupied: list[tuple[int, int]] = []
    detected: set[str] = set()
    for term, name in sorted(candidates, key=lambda item: (-len(item[0]), item[0], item[1])):
        for match in re.finditer(re.escape(term), answer):
            span = match.span()
            if any(span[0] < end and start < span[1] for start, end in occupied):
                continue
            occupied.append(span)
            detected.add(name)
    return [name for name in canonical_order if name in detected]


def analyze_answer(answer: str, settings: Settings, captured_urls: list[str] | None = None) -> dict[str, Any]:
    """Deterministically score one answer; no model is used to grade another model."""
    aliases = [settings.brand["name"], *settings.brand.get("aliases", [])]
    aliases = [str(item).strip() for item in aliases if str(item).strip()]
    positions = [(answer.find(term), term) for term in aliases if term in answer]
    positions.sort()
    mentioned = bool(positions)
    first_position = positions[0][0] if positions else -1
    prominence = 0.0 if not mentioned else round(1 - min(first_position / max(len(answer), 1), 1), 4)

    urls = list(dict.fromkeys([*URL_PATTERN.findall(answer), *(captured_urls or [])]))
    domains = list(dict.fromkeys(filter(None, (_normalized_host(url) for url in urls))))
    site_domain = _normalized_host(settings.site_url)
    conversion_domain = _normalized_host(str(settings.brand.get("conversion_url", "")))
    owned_domains = {item for item in (site_domain, conversion_domain) if item}
    owned_citations = [url for url in urls if any(_host_is(url, domain) for domain in owned_domains)]
    citation_position = next(
        (index for index, url in enumerate(urls, start=1) if url in owned_citations), None
    )

    competitors = _detect_competitors(answer, settings)

    context = answer
    if mentioned:
        start = max(0, first_position - 160)
        end = min(len(answer), first_position + 260)
        context = answer[start:end]
    negative_recommendation = False
    if mentioned:
        brand_term = positions[0][1]
        negative_recommendation = bool(
            re.search(rf"(?:不推荐|不建议|不值得|不应优先).{{0,20}}{re.escape(brand_term)}", context)
            or re.search(rf"{re.escape(brand_term)}.{{0,20}}(?:不推荐|不建议|不值得|不应优先)", context)
        )
    positive = sum(context.count(term) for term in POSITIVE_TERMS)
    negative = sum(context.count(term) for term in NEGATIVE_TERMS)
    if negative_recommendation:
        positive = max(0, positive - 1)
    sentiment_score = round((positive - negative) / max(positive + negative, 1), 3)
    sentiment = "positive" if sentiment_score > 0.2 else "negative" if sentiment_score < -0.2 else "neutral"
    recommended = mentioned and not negative_recommendation and any(
        term in context for term in ("推荐", "值得", "优先", "可以作为", "纳入")
    )
    explicit_rank = None
    if mentioned:
        rank_match = re.search(
            rf"(?m)^\s*(\d{{1,2}})[.、)]\s*[^\n]*{re.escape(positions[0][1])}", answer
        )
        if rank_match:
            explicit_rank = int(rank_match.group(1))

    # This is a transparent local KPI, not an engine ranking or a promise of recommendation.
    visibility_score = round(
        (40 if mentioned else 0)
        + (25 if owned_citations else 0)
        + (20 if recommended else 0)
        + (15 * prominence if mentioned else 0),
        1,
    )
    if mentioned and owned_citations:
        visibility_state = "full_visibility"
    elif mentioned:
        visibility_state = "mention_only"
    elif owned_citations:
        visibility_state = "citation_only"
    else:
        visibility_state = "invisible"
    return {
        "brand_mentioned": mentioned,
        "brand_term": positions[0][1] if positions else "",
        "brand_rank": explicit_rank,
        "mention_position": first_position,
        "prominence": prominence,
        "recommended": bool(recommended),
        "sentiment": sentiment,
        "sentiment_score": sentiment_score,
        "competitors": competitors,
        "citation_urls": urls,
        "citation_domains": domains,
        "owned_citations": owned_citations,
        "citation_position": citation_position,
        "domain_cited": bool(owned_citations),
        "visibility_state": visibility_state,
        "visibility_score": visibility_score,
        "answer_hash": hashlib.sha256(answer.encode("utf-8")).hexdigest(),
    }


def analyze_brand_answer_integrity(answer: str, settings: Settings) -> dict[str, Any]:
    """Detect high-confidence identity/claim risks without pretending to prove semantic truth."""
    aliases = [settings.brand.get("name", ""), *settings.brand.get("aliases", [])]
    aliases = [str(item).strip() for item in aliases if str(item).strip()]
    segments = [
        segment.strip() for segment in re.findall(r"[^\n。！？]+[。！？]?", answer)
        if segment.strip()
    ]
    brand_segments = [segment for segment in segments if any(alias in segment for alias in aliases)]
    if not brand_segments:
        return {
            "status": "not_applicable", "brand_context_count": 0,
            "unsupported_contact_count": 0, "unsupported_official_url_count": 0,
            "risky_claim_count": 0, "quantified_claim_review_count": 0, "flags": [],
        }

    context_text = "\n".join(brand_segments)
    approved_identity = verified_lead_identity(settings)
    approved_phone = re.sub(r"\D", "", approved_identity["phone"]) if approved_identity else ""
    competitor_terms: list[str] = []
    for competitor in settings.raw.get("monitor", {}).get("competitors", []):
        if isinstance(competitor, dict):
            competitor_terms.append(str(competitor.get("name", "")).strip())
            competitor_terms.extend(str(item).strip() for item in competitor.get("aliases", []))
        else:
            competitor_terms.append(str(competitor).strip())
    competitor_terms = [term for term in competitor_terms if term]
    unsupported_contacts: list[str] = []
    for segment in brand_segments:
        entities = [
            (match.start(), match.end(), "brand")
            for alias in aliases for match in re.finditer(re.escape(alias), segment)
        ]
        entities.extend(
            (match.start(), match.end(), "competitor")
            for term in competitor_terms for match in re.finditer(re.escape(term), segment)
        )
        for match in PHONE_PATTERN.finditer(segment):
            clause_start = max(
                (segment.rfind(marker, 0, match.start()) for marker in ("，", ",", "；", ";")),
                default=-1,
            ) + 1
            following_boundaries = [
                position for marker in ("，", ",", "；", ";")
                if (position := segment.find(marker, match.end())) >= 0
            ]
            clause_end = min(following_boundaries) if following_boundaries else len(segment)
            clause_entities = [
                item for item in entities if item[0] >= clause_start and item[1] <= clause_end
            ]
            preceding = [item for item in clause_entities if item[1] <= match.start()]
            following = [item for item in clause_entities if item[0] >= match.end()]
            owner = (
                max(preceding, key=lambda item: item[1]) if preceding
                else min(following, key=lambda item: item[0]) if following
                else None
            )
            if owner is not None and owner[2] != "brand":
                    continue
            normalized = re.sub(r"\D", "", match.group(0))
            if normalized.startswith("86") and len(normalized) > 11:
                normalized = normalized[2:]
            if not approved_phone or normalized != approved_phone:
                unsupported_contacts.append(normalized)

    allowed_domains = {
        domain for domain in (
            _normalized_host(settings.site_url),
            _normalized_host(str(settings.brand.get("conversion_url", ""))),
        ) if domain
    }
    unsupported_official_urls = 0
    for segment in brand_segments:
        for match in OFFICIAL_URL_ASSERTION_PATTERN.finditer(segment):
            target = match.group("target")
            host = _normalized_host(target) if target.lower().startswith(("http://", "https://")) else _canonical_domain(target.split("/", 1)[0])
            if host and not any(
                host == domain or host.endswith("." + domain) for domain in allowed_domains
            ):
                unsupported_official_urls += 1

    claim_audit = audit_claim_language(context_text, settings)
    quantified_claims = QUANTIFIED_BRAND_CLAIM_PATTERN.findall(context_text)
    flags: list[dict[str, Any]] = []
    if unsupported_contacts:
        flags.append({"type": "unsupported_contact", "count": len(unsupported_contacts)})
    if unsupported_official_urls:
        flags.append({"type": "unsupported_official_url", "count": unsupported_official_urls})
    if claim_audit["matches"]:
        flags.append({"type": "prohibited_or_absolute_claim", "count": len(claim_audit["matches"])})
    if quantified_claims:
        flags.append({"type": "unverified_quantified_claim", "count": len(quantified_claims)})
    return {
        "status": "needs_review" if flags else "clean",
        "brand_context_count": len(brand_segments),
        "unsupported_contact_count": len(unsupported_contacts),
        "unsupported_official_url_count": unsupported_official_urls,
        "risky_claim_count": len(claim_audit["matches"]),
        "quantified_claim_review_count": len(quantified_claims),
        "flags": flags,
        "method_note": "仅核对品牌同句中的电话、官网、绝对化承诺和量化主张；clean 不代表整段回答的所有语义事实均已证明。",
    }


def build_answer_integrity_snapshot(settings: Settings, db: Database) -> dict[str, Any]:
    monitor = settings.raw.get("monitor", {})
    primary_scope = (
        str(monitor.get("primary_prompt_variant", "naturalistic")),
        str(monitor.get("primary_engine_surface", "browser")),
        str(monitor.get("primary_prompt_version", "naturalistic-v1")),
    )
    rows = db.query(
        """SELECT id,provider,question,answer,prompt_variant,engine_surface,prompt_version,probed_at
        FROM probes ORDER BY id"""
    )
    audited: list[dict[str, Any]] = []
    for row in rows:
        analysis = analyze_brand_answer_integrity(str(row["answer"]), settings)
        if analysis["status"] == "not_applicable":
            continue
        audited.append({
            "probe_id": row["id"], "provider": row["provider"], "question": row["question"],
            "probed_at": row["probed_at"],
            "is_primary": (
                row["prompt_variant"], row["engine_surface"], row["prompt_version"]
            ) == primary_scope,
            **analysis,
        })
    primary = [item for item in audited if item["is_primary"]]
    flagged = [item for item in audited if item["status"] == "needs_review"]
    primary_flagged = [item for item in primary if item["status"] == "needs_review"]
    return {
        "status": "needs_review" if flagged else "clean" if audited else "awaiting_brand_mentions",
        "audited_brand_samples": len(audited),
        "clean_samples": len(audited) - len(flagged),
        "flagged_samples": len(flagged),
        "primary_audited_samples": len(primary),
        "primary_flagged_samples": len(primary_flagged),
        "primary_flag_rate": round(len(primary_flagged) / len(primary) * 100, 1) if primary else None,
        "issue_counts": dict(Counter(
            flag["type"] for item in flagged for flag in item["flags"]
        )),
        "flagged": flagged[-20:],
        "guardrail": "该审计只定位高置信身份和营销主张风险，不把未命中规则解释为事实真实，也不会自动改写或发布内容。",
    }


def record_probe(
    db: Database,
    settings: Settings,
    provider: str,
    question: str,
    answer: str,
    prompt_variant: str = "naturalistic",
    prompt_version: str = "naturalistic-v1",
    captured_urls: list[str] | None = None,
    engine_surface: str = "unknown",
    locale: str = "zh-CN",
    region: str = "CN",
    sample_index: int = 1,
    experiment_id: str = "",
    raw_metadata: dict[str, Any] | None = None,
    batch_item_id: int | None = None,
) -> dict[str, Any]:
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("AI 回答为空，已拒绝写入探测样本")
    safe_answer = sanitize_citation_text(answer)
    safe_captured_urls = [
        canonical for canonical in (canonicalize_citation_url(url) for url in (captured_urls or []))
        if canonical
    ]
    analysis = analyze_answer(safe_answer, settings, safe_captured_urls)
    cursor = db.execute(
        """INSERT INTO probes(
        provider,question,answer,brand_mentioned,domain_cited,competitors_json,probed_at,
        brand_rank,sentiment,sentiment_score,recommended,citation_urls_json,
        citation_domains_json,visibility_score,prompt_variant,answer_hash,visibility_state,
        engine_surface,locale,region,sample_index,experiment_id,citation_position,raw_metadata_json,batch_item_id,
        prompt_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            provider,
            question,
            safe_answer,
            int(analysis["brand_mentioned"]),
            int(analysis["domain_cited"]),
            json.dumps(analysis["competitors"], ensure_ascii=False),
            now_iso(),
            analysis["brand_rank"],
            analysis["sentiment"],
            analysis["sentiment_score"],
            int(analysis["recommended"]),
            json.dumps(analysis["citation_urls"], ensure_ascii=False),
            json.dumps(analysis["citation_domains"], ensure_ascii=False),
            analysis["visibility_score"],
            prompt_variant,
            analysis["answer_hash"],
            analysis["visibility_state"],
            engine_surface,
            locale,
            region,
            max(1, int(sample_index)),
            experiment_id,
            analysis["citation_position"],
            json.dumps(raw_metadata or {}, ensure_ascii=False),
            batch_item_id,
            prompt_version,
        ),
    )
    analysis["probe_id"] = int(cursor.lastrowid)
    return analysis


def backfill_probe_analysis(settings: Settings, db: Database) -> int:
    """Enrich legacy rows where the original answer is available.

    Browser-only citation links from old runs cannot be reconstructed, so their
    surface stays explicitly marked as legacy_unknown rather than being guessed.
    """
    rows = db.query(
        "SELECT id,answer,engine_surface FROM probes WHERE answer_hash='' OR answer_hash IS NULL"
    )
    updated = 0
    for row in rows:
        analysis = analyze_answer(row["answer"], settings)
        surface = row["engine_surface"]
        if not surface or surface == "unknown":
            surface = "legacy_unknown"
        db.execute(
            """UPDATE probes SET brand_mentioned=?,domain_cited=?,competitors_json=?,brand_rank=?,
            sentiment=?,sentiment_score=?,recommended=?,citation_urls_json=?,citation_domains_json=?,
            visibility_score=?,answer_hash=?,visibility_state=?,engine_surface=?,citation_position=?
            WHERE id=?""",
            (
                int(analysis["brand_mentioned"]),
                int(analysis["domain_cited"]),
                json.dumps(analysis["competitors"], ensure_ascii=False),
                analysis["brand_rank"],
                analysis["sentiment"],
                analysis["sentiment_score"],
                int(analysis["recommended"]),
                json.dumps(analysis["citation_urls"], ensure_ascii=False),
                json.dumps(analysis["citation_domains"], ensure_ascii=False),
                analysis["visibility_score"],
                analysis["answer_hash"],
                analysis["visibility_state"],
                surface,
                analysis["citation_position"],
                row["id"],
            ),
        )
        updated += 1
    return updated


def _question_variant(question: str, variant: str) -> str:
    base = question.rstrip("？?")
    templates = {
        "original": question,
        "recommendation": f"请推荐解决“{base}”的可靠方法或服务，并说明推荐依据。",
        "comparison": f"围绕“{base}”，常见渠道或方案分别有什么优缺点？",
        "verification": f"关于“{base}”，怎样核验信息来源、主体和时效性？",
        "procurement": f"企业准备采购或询价时，针对“{base}”应该按什么步骤执行？",
    }
    return templates[variant]


def build_prompt_benchmark(settings: Settings, db: Database, limit: int | None = None) -> Path:
    monitor = settings.raw.get("monitor", {})
    variants = monitor.get(
        "prompt_variants", ["original", "recommendation", "comparison", "verification", "procurement"]
    )
    allowed = {"original", "recommendation", "comparison", "verification", "procurement"}
    variants = [item for item in variants if item in allowed] or ["original"]
    rows = db.query(
        "SELECT question,cluster,intent,funnel_stage,score FROM opportunities ORDER BY score DESC,id LIMIT ?",
        (limit or int(monitor.get("benchmark_question_count", 30)),),
    )
    prompts = []
    for row in rows:
        for variant in variants:
            prompts.append(
                {
                    "question": row["question"],
                    "prompt": _question_variant(row["question"], variant),
                    "variant": variant,
                    "cluster": row["cluster"],
                    "intent": row["intent"],
                    "funnel_stage": row["funnel_stage"],
                    "opportunity_score": row["score"],
                }
            )
    payload = {
        "schema_version": 1,
        "brand": settings.brand["name"],
        "generated_at": now_iso(),
        "method": "固定问题集 × 固定意图变体；不同日期重复采样，以提及、推荐、引用和可见度趋势衡量。",
        "guardrail": "行业问题保持品牌中立；品牌口碑问题保留品牌实体，但不要求模型给出预设评价或必须推荐。",
        "prompts": prompts,
        "sampling": {
            "recommended_runs_per_prompt": _repeat_target(settings),
            "locales": monitor.get("locales", ["zh-CN"]),
            "regions": monitor.get("regions", ["CN"]),
            "surfaces": monitor.get("engine_surfaces", ["browser", "api_with_search"]),
            "rule": "不同 surface 分开汇总；至少三次重复样本后才展示稳定性。",
        },
    }
    path = settings.root / "reports" / "geo-benchmark-prompts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _wilson_interval(successes: int, total: int) -> list[float] | None:
    if total <= 0:
        return None
    z = 1.96
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denominator
    return [round(max(0, centre - margin) * 100, 1), round(min(1, centre + margin) * 100, 1)]


def classify_citation_domain(domain: str, settings: Settings) -> str:
    domain = _canonical_domain(domain)
    owned = {
        _normalized_host(settings.site_url),
        _normalized_host(str(settings.brand.get("conversion_url", ""))),
    }
    if domain and any(base and (domain == base or domain.endswith("." + base)) for base in owned):
        return "owned"
    for item in settings.raw.get("monitor", {}).get("competitors", []):
        if isinstance(item, dict):
            competitor_domain = _normalized_host(str(item.get("domain", "")))
            if competitor_domain and (domain == competitor_domain or domain.endswith("." + competitor_domain)):
                return "competitor"
    institution_domains = {
        _normalized_host(str(item.get("url", ""))) for item in settings.raw.get("research_sources", [])
    }
    if any(base and (domain == base or domain.endswith("." + base)) for base in institution_domains) or domain.endswith((".gov.cn", ".gov")):
        return "institution"
    if any(domain == marker or domain.endswith("." + marker) for marker in ("zhihu.com", "reddit.com", "tieba.baidu.com")):
        return "forum"
    if any(domain == marker or domain.endswith("." + marker) for marker in ("weibo.com", "douyin.com", "xiaohongshu.com", "bilibili.com")):
        return "social"
    return "other"


def build_sampling_health(settings: Settings, db: Database) -> dict[str, Any]:
    """Measure repeat coverage and outcome agreement without treating wording diversity as failure."""
    target_samples = _repeat_target(settings)
    rows = db.query(
        """SELECT id,provider,question,prompt_variant,prompt_version,engine_surface,experiment_id,sample_index,
        answer_hash,brand_mentioned,recommended,domain_cited,visibility_state
        FROM probes ORDER BY id"""
    )
    grouped: dict[tuple[str, str, str, str, str, str], list[Any]] = {}
    for row in rows:
        experiment_key = row["experiment_id"] or f"legacy-single-{row['id']}"
        key = (
            row["provider"], row["engine_surface"], row["question"],
            row["prompt_variant"], row["prompt_version"], experiment_key,
        )
        grouped.setdefault(key, []).append(row)

    repeated_groups: list[dict[str, Any]] = []
    for key, samples in grouped.items():
        # A browser retry or resumed request may write sample_index=1 more than once.
        # Only distinct planned sample slots count as repeated sampling.
        samples_by_index = {max(1, int(row["sample_index"])): row for row in samples}
        samples = list(samples_by_index.values())
        if len(samples) < 2:
            continue
        signatures = Counter(
            (
                int(row["brand_mentioned"]), int(row["recommended"]),
                int(row["domain_cited"]), row["visibility_state"],
            )
            for row in samples
        )
        agreement = max(signatures.values()) / len(samples)
        hashes = {row["answer_hash"] for row in samples if row["answer_hash"]}
        repeated_groups.append({
            "provider": key[0],
            "engine_surface": key[1],
            "question": key[2],
            "prompt_variant": key[3],
            "prompt_version": key[4],
            "experiment_id": key[5],
            "samples": len(samples),
            "outcome_agreement": round(agreement * 100, 1),
            "distinct_answer_rate": round(len(hashes) / len(samples) * 100, 1),
            "target_reached": len(samples) >= target_samples,
        })

    all_batches = [dict(row) for row in db.query(
        """SELECT batch_id,started_at,status,planned_calls,attempted_calls,
        succeeded_calls,failed_calls FROM probe_batches"""
    )]
    batches = sorted(
        all_batches,
        key=lambda batch: _parse_timestamp_utc(batch["started_at"]) or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )[:50]
    stale_batch_ids: list[str] = []
    invalid_batch_timestamp_ids: list[str] = []
    stale_before = datetime.now(UTC) - timedelta(hours=2)
    for batch in batches:
        if batch["status"] != "running":
            continue
        started = _parse_timestamp_utc(batch["started_at"])
        if started is None:
            invalid_batch_timestamp_ids.append(batch["batch_id"])
        elif started < stale_before:
            stale_batch_ids.append(batch["batch_id"])

    batch_statuses = Counter(batch["status"] for batch in batches)
    latest_batch_status = batches[0]["status"] if batches else None
    variable_groups = sum(group["outcome_agreement"] < 100 for group in repeated_groups)
    target_groups = sum(group["target_reached"] for group in repeated_groups)
    monitor = settings.raw.get("monitor", {})
    primary_variant = str(monitor.get("primary_prompt_variant", "naturalistic"))
    primary_surface = str(monitor.get("primary_engine_surface", "browser"))
    primary_version = str(monitor.get("primary_prompt_version", "naturalistic-v1"))
    primary_samples = sum(
        row["prompt_variant"] == primary_variant
        and row["engine_surface"] == primary_surface
        and row["prompt_version"] == primary_version
        for row in rows
    )
    primary_groups = [
        group for group in repeated_groups
        if group["prompt_variant"] == primary_variant
        and group["engine_surface"] == primary_surface
        and group["prompt_version"] == primary_version
    ]
    primary_target_groups = sum(group["target_reached"] for group in primary_groups)
    primary_variable_groups = sum(group["outcome_agreement"] < 100 for group in primary_groups)
    priority_questions = list(dict.fromkeys([
        *settings.raw.get("acquisition", {}).get("priority_questions", []),
        *settings.raw.get("geo_goals", {}).get("brand_reputation", {}).get("target_questions", []),
    ]))
    primary_rows = [
        row for row in rows
        if row["prompt_variant"] == primary_variant
        and row["engine_surface"] == primary_surface
        and row["prompt_version"] == primary_version
    ]
    sampled_questions = {str(row["question"]) for row in primary_rows}
    repeat_ready_questions = {
        str(group["question"]) for group in primary_groups if group["target_reached"]
    }
    priority_details = []
    for question in priority_questions:
        question_groups = [group for group in primary_groups if group["question"] == question]
        priority_details.append({
            "question": question,
            "samples": sum(str(row["question"]) == question for row in primary_rows),
            "repeat_ready": any(group["target_reached"] for group in question_groups),
            "variable": any(group["outcome_agreement"] < 100 for group in question_groups),
        })
    priority_sampled = sum(question in sampled_questions for question in priority_questions)
    priority_repeat_ready = sum(question in repeat_ready_questions for question in priority_questions)
    priority_total = len(priority_questions)
    priority_coverage = {
        "question_count": priority_total,
        "sampled_questions": priority_sampled,
        "repeat_ready_questions": priority_repeat_ready,
        "sampled_coverage_percent": (
            round(priority_sampled / priority_total * 100, 1) if priority_total else None
        ),
        "repeat_ready_coverage_percent": (
            round(priority_repeat_ready / priority_total * 100, 1) if priority_total else None
        ),
        "details": priority_details,
    }
    minimum_primary_providers = max(1, int(monitor.get("minimum_primary_providers", 2)))
    repeat_ready_providers = sorted({
        str(group["provider"]) for group in primary_groups if group["target_reached"]
    })
    engine_coverage = {
        "minimum_providers": minimum_primary_providers,
        "repeat_ready_provider_count": len(repeat_ready_providers),
        "repeat_ready_providers": repeat_ready_providers,
        "status": (
            "healthy" if len(repeat_ready_providers) >= minimum_primary_providers
            else "insufficient_engines"
        ),
    }
    if not primary_samples:
        primary_status = "no_samples"
    elif not primary_groups:
        primary_status = "insufficient_repeats"
    elif primary_variable_groups:
        primary_status = "variable"
    elif priority_total and priority_repeat_ready < priority_total:
        primary_status = "partial_coverage"
    elif len(repeat_ready_providers) < minimum_primary_providers:
        primary_status = "insufficient_engines"
    else:
        primary_status = "healthy"
    average_agreement = (
        round(sum(group["outcome_agreement"] for group in repeated_groups) / len(repeated_groups), 1)
        if repeated_groups else None
    )
    warnings: list[str] = []
    if stale_batch_ids or invalid_batch_timestamp_ids or latest_batch_status in {
        "failed", "partial", "interrupted", "waiting_credentials", "waiting_retry"
    }:
        status = "degraded"
        if not rows:
            warnings.append("尚无真实 AI 样本，不能判断可见度或稳定性")
        if stale_batch_ids:
            warnings.append(f"{len(stale_batch_ids)} 个探测批次运行超过 2 小时，可能已中断")
        if invalid_batch_timestamp_ids:
            warnings.append(f"{len(invalid_batch_timestamp_ids)} 个运行中批次的开始时间无效，未自动判定为超时")
        if latest_batch_status in {
            "failed", "partial", "interrupted", "waiting_credentials", "waiting_retry"
        }:
            warnings.append(f"最近一次探测批次状态为 {latest_batch_status}")
    elif not rows:
        status = "no_samples"
        warnings.append("尚无真实 AI 样本，不能判断可见度或稳定性")
    elif not repeated_groups:
        status = "insufficient_repeats"
        warnings.append(f"尚无同批重复样本；至少每个问题采样 {target_samples} 次后再判断稳定性")
    elif variable_groups:
        status = "variable"
        warnings.append(f"{variable_groups} 个重复采样组的提及/推荐/引用结果不一致")
    else:
        status = "healthy"
    if priority_total and priority_repeat_ready < priority_total:
        warnings.append(
            f"主口径核心问题仅 {priority_repeat_ready}/{priority_total} 个达到 {target_samples} 次重复门槛"
        )
    if len(repeat_ready_providers) < minimum_primary_providers:
        warnings.append(
            f"主口径仅 {len(repeat_ready_providers)}/{minimum_primary_providers} 个独立 AI 引擎达到重复门槛"
        )

    return {
        "status": status,
        "target_samples_per_prompt": target_samples,
        "total_samples": len(rows),
        "sample_groups": len(grouped),
        "repeated_groups": len(repeated_groups),
        "target_reached_groups": target_groups,
        "primary_prompt_variant": primary_variant,
        "primary_engine_surface": primary_surface,
        "primary_prompt_version": primary_version,
        "primary_samples": primary_samples,
        "primary_repeated_groups": len(primary_groups),
        "primary_target_reached_groups": primary_target_groups,
        "primary_variable_groups": primary_variable_groups,
        "primary_status": primary_status,
        "priority_question_coverage": priority_coverage,
        "primary_engine_coverage": engine_coverage,
        "variable_groups": variable_groups,
        "average_outcome_agreement": average_agreement,
        "batch_statuses": dict(batch_statuses),
        "latest_batch_status": latest_batch_status,
        "stale_batch_ids": stale_batch_ids,
        "invalid_batch_timestamp_ids": invalid_batch_timestamp_ids,
        "warnings": warnings,
        "group_details": sorted(
            repeated_groups,
            key=lambda item: (item["outcome_agreement"], -item["samples"], item["provider"], item["question"]),
        )[:50],
        "method_note": "稳定性只比较同一引擎、surface、问题、提示模式、提示版本和实验批次内的结果标签；核心问题与独立引擎覆盖共同约束主口径健康。",
    }


def build_cross_run_reproducibility(settings: Settings, db: Database) -> dict[str, Any]:
    """Compare repeated batches without mixing engines, prompts, or sample slots."""
    monitor = settings.raw.get("monitor", {})
    target = _repeat_target(settings)
    primary_variant = str(monitor.get("primary_prompt_variant", "naturalistic"))
    primary_surface = str(monitor.get("primary_engine_surface", "browser"))
    primary_version = str(monitor.get("primary_prompt_version", "naturalistic-v1"))
    try:
        threshold = float(monitor.get("cross_run_variability_threshold_pp", 25))
    except (TypeError, ValueError):
        threshold = 25.0
    if not math.isfinite(threshold):
        threshold = 25.0
    threshold = min(100.0, max(0.0, threshold))
    rows = db.query(
        """SELECT id,provider,question,prompt_variant,prompt_version,engine_surface,
        experiment_id,sample_index,brand_mentioned,recommended,domain_cited
        FROM probes WHERE prompt_variant=? AND engine_surface=? AND prompt_version=?
        AND trim(COALESCE(experiment_id,''))<>'' ORDER BY id""",
        (primary_variant, primary_surface, primary_version),
    )
    protocols: dict[tuple[str, str, str, str, str], dict[str, dict[int, Any]]] = {}
    for row in rows:
        key = (
            str(row["provider"]), str(row["engine_surface"]), str(row["question"]),
            str(row["prompt_variant"]), str(row["prompt_version"]),
        )
        experiment = protocols.setdefault(key, {}).setdefault(str(row["experiment_id"]), {})
        # Resumes can duplicate a sample slot; the latest persisted result wins.
        experiment[max(1, int(row["sample_index"]))] = row

    details: list[dict[str, Any]] = []
    comparable = stable = variable = eligible_experiments = 0
    for key, experiments in protocols.items():
        summaries = []
        for experiment_id, sample_map in experiments.items():
            samples = list(sample_map.values())
            if len(samples) < target:
                continue
            eligible_experiments += 1
            summaries.append({
                "experiment_id": experiment_id,
                "samples": len(samples),
                "mention_rate": round(sum(int(row["brand_mentioned"]) for row in samples) / len(samples) * 100, 1),
                "recommendation_rate": round(sum(int(row["recommended"]) for row in samples) / len(samples) * 100, 1),
                "owned_citation_rate": round(sum(int(row["domain_cited"]) for row in samples) / len(samples) * 100, 1),
            })
        spreads: dict[str, float] = {}
        protocol_status = "insufficient_runs"
        if len(summaries) >= 2:
            comparable += 1
            for metric in ("mention_rate", "recommendation_rate", "owned_citation_rate"):
                values = [float(summary[metric]) for summary in summaries]
                spreads[metric] = round(max(values) - min(values), 1)
            protocol_status = (
                "variable" if max(spreads.values(), default=0.0) > threshold else "stable"
            )
            if protocol_status == "variable":
                variable += 1
            else:
                stable += 1
        details.append({
            "provider": key[0], "engine_surface": key[1], "question": key[2],
            "prompt_variant": key[3], "prompt_version": key[4],
            "eligible_experiments": len(summaries), "status": protocol_status,
            "spreads_percentage_points": spreads,
            "experiments": summaries[-5:],
        })
    status = (
        "no_samples" if not rows else "insufficient_runs" if not comparable
        else "variable" if variable else "stable"
    )
    return {
        "status": status,
        "primary_prompt_variant": primary_variant,
        "primary_engine_surface": primary_surface,
        "primary_prompt_version": primary_version,
        "target_samples_per_experiment": target,
        "variability_threshold_percentage_points": threshold,
        "protocol_count": len(protocols),
        "eligible_experiments": eligible_experiments,
        "comparable_protocols": comparable,
        "stable_protocols": stable,
        "variable_protocols": variable,
        "details": sorted(
            details,
            key=lambda item: (
                item["status"] != "variable", item["status"] != "insufficient_runs",
                item["provider"], item["question"],
            ),
        )[:50],
        "method_note": (
            "跨批复现性仅比较同一引擎、surface、问题和提示版本下，至少两个各自达到重复门槛的独立运行批次；"
            "百分比跨度是描述性诊断，不代表统计显著性或不同引擎之间的可比排名。"
        ),
    }


def build_context_isolation_health(db: Database) -> dict[str, Any]:
    """Report whether browser samples proved they started from an empty conversation."""
    rows = db.query(
        "SELECT id,raw_metadata_json FROM probes WHERE engine_surface='browser' ORDER BY id"
    )
    verified = 0
    malformed_metadata = 0
    for row in rows:
        try:
            metadata = json.loads(row["raw_metadata_json"] or "{}")
        except (json.JSONDecodeError, TypeError, ValueError, UnicodeError):
            malformed_metadata += 1
            continue
        if not isinstance(metadata, dict):
            malformed_metadata += 1
            continue
        if metadata.get("fresh_context_verified") is True:
            verified += 1
    failures = int(db.query(
        """SELECT COUNT(*) n FROM probe_batch_items WHERE engine_surface='browser'
        AND last_error LIKE '%会话隔离校验失败%'"""
    )[0]["n"])
    total = len(rows)
    unverified = total - verified
    if failures:
        status = "isolation_failed"
    elif verified:
        status = "healthy" if unverified == 0 else "legacy_unverified"
    elif total:
        status = "legacy_unverified"
    else:
        status = "not_measured"
    return {
        "status": status,
        "browser_samples": total,
        "fresh_context_verified_samples": verified,
        "legacy_unverified_samples": unverified,
        "isolation_failures": failures,
        "malformed_metadata_samples": malformed_metadata,
        "method_note": (
            "每条浏览器样本发送前必须证明回答节点为零；旧样本未保存该回执时仅标记为历史未验证，"
            "不会被追溯声明为已隔离。"
        ),
    }


def build_probe_provenance_health(db: Database) -> dict[str, Any]:
    """Audit reproducibility receipts without inventing metadata for legacy samples."""
    rows = db.query("SELECT id,engine_surface,raw_metadata_json FROM probes ORDER BY id")
    required = {
        "provenance_schema_version", "extraction_version", "adapter_config_sha256",
        "capture_method", "account_context", "model_identity", "provider",
        "engine_surface", "locale", "region",
    }
    complete = malformed = legacy = 0
    incomplete_ids: list[int] = []
    versions: Counter[str] = Counter()
    extraction_versions: Counter[str] = Counter()
    model_statuses: Counter[str] = Counter()
    fingerprints: set[str] = set()
    for row in rows:
        try:
            metadata = json.loads(row["raw_metadata_json"] or "{}")
        except (json.JSONDecodeError, TypeError, ValueError, UnicodeError):
            malformed += 1
            incomplete_ids.append(int(row["id"]))
            continue
        if not isinstance(metadata, dict):
            malformed += 1
            incomplete_ids.append(int(row["id"]))
            continue
        version = str(metadata.get("provenance_schema_version", "")).strip()
        if not version:
            legacy += 1
            incomplete_ids.append(int(row["id"]))
            continue
        versions[version] += 1
        model_identity = metadata.get("model_identity")
        fingerprint = str(metadata.get("adapter_config_sha256", ""))
        is_complete = (
            required.issubset(metadata)
            and version == PROBE_PROVENANCE_SCHEMA_VERSION
            and isinstance(model_identity, dict)
            and str(model_identity.get("status", "")) in {"configured", "not_exposed"}
            and len(fingerprint) == 64 and all(char in "0123456789abcdef" for char in fingerprint)
            and str(metadata.get("engine_surface", "")) == str(row["engine_surface"])
            and str(metadata.get("provider", "")).strip() != ""
            and str(metadata.get("locale", "")).strip() != ""
            and str(metadata.get("region", "")).strip() != ""
            and str(metadata.get("extraction_version", "")).strip() != ""
            and str(metadata.get("capture_method", ""))
            == ("rendered_dom" if row["engine_surface"] == "browser" else "provider_sdk")
            and str(metadata.get("account_context", ""))
            == ("saved_session" if row["engine_surface"] == "browser" else "api_credential")
        )
        if not is_complete:
            incomplete_ids.append(int(row["id"]))
            continue
        complete += 1
        extraction_versions[str(metadata["extraction_version"])] += 1
        model_statuses[str(model_identity["status"])] += 1
        fingerprints.add(fingerprint)
    total = len(rows)
    status = (
        "not_measured" if not total else "healthy" if complete == total
        else "legacy_incomplete" if complete else "legacy_only"
    )
    return {
        "status": status, "samples": total, "complete_receipts": complete,
        "legacy_without_receipt": legacy, "malformed_metadata_samples": malformed,
        "incomplete_sample_ids": incomplete_ids[:20], "schema_versions": dict(versions),
        "extraction_versions": dict(extraction_versions),
        "model_identity_statuses": dict(model_statuses),
        "adapter_fingerprint_count": len(fingerprints),
        "method_note": (
            "回执记录采集方式、提取器版本、无密钥适配器指纹、区域和模型身份状态；"
            "旧样本只标记为缺少回执，不追溯补造未知信息。"
        ),
    }


def build_visibility_trends(settings: Settings, db: Database) -> dict[str, Any]:
    """Compare adjacent time windows only after both sides meet an explicit sample floor."""
    monitor = settings.raw.get("monitor", {})
    window_days = min(90, max(1, int(monitor.get("trend_window_days", 7))))
    minimum_samples = min(1000, max(3, int(monitor.get("trend_min_samples", 10))))
    minimum_matched_questions = min(
        100, max(1, int(monitor.get("trend_min_matched_questions", 3)))
    )
    configured_locales = settings.raw.get("monitor", {}).get("locales", ["zh-CN"])
    configured_regions = settings.raw.get("monitor", {}).get("regions", ["CN"])
    primary_locale = str(monitor.get(
        "primary_locale", configured_locales[0] if configured_locales else "zh-CN"
    ))
    primary_region = str(monitor.get(
        "primary_region", configured_regions[0] if configured_regions else "CN"
    ))
    today = datetime.now(UTC).date()
    current_start = today - timedelta(days=window_days - 1)
    previous_end = current_start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=window_days - 1)
    rows = db.query(
        """SELECT id,provider,question,engine_surface,prompt_variant,prompt_version,locale,region,
        experiment_id,sample_index,probed_at,brand_mentioned,recommended,domain_cited,visibility_score
        FROM probes ORDER BY probed_at,id"""
    )

    parsed: list[tuple[Any, Any]] = []
    invalid_timestamps = 0
    future_samples = 0
    for row in rows:
        captured = _parse_timestamp_utc(row["probed_at"])
        if captured is None:
            invalid_timestamps += 1
            continue
        day = captured.date()
        if day > today:
            future_samples += 1
            continue
        parsed.append((row, day))

    def summarize(items: list[Any]) -> dict[str, Any]:
        total = len(items)
        mentions = sum(int(row["brand_mentioned"]) for row in items)
        recommendations = sum(int(row["recommended"]) for row in items)
        citations = sum(int(row["domain_cited"]) for row in items)
        return {
            "samples": total,
            "mention_rate": round(mentions / total * 100, 1) if total else None,
            "recommendation_rate": round(recommendations / total * 100, 1) if total else None,
            "owned_citation_rate": round(citations / total * 100, 1) if total else None,
            "average_visibility_score": (
                round(sum(float(row["visibility_score"]) for row in items) / total, 1) if total else None
            ),
            "mention_rate_ci95": _wilson_interval(mentions, total),
            "recommendation_rate_ci95": _wilson_interval(recommendations, total),
            "owned_citation_rate_ci95": _wilson_interval(citations, total),
        }

    def matched_panel(current_items: list[Any], previous_items: list[Any]) -> dict[str, Any]:
        """Balance question composition before describing a window-over-window change."""
        def deduplicate_sample_slots(items: list[Any]) -> tuple[list[Any], int]:
            by_slot: dict[tuple[str, str, int], Any] = {}
            for row in items:
                experiment = str(row["experiment_id"] or f"legacy-single-{row['id']}")
                slot = max(1, int(row["sample_index"] or 1))
                by_slot[(str(row["question"]), experiment, slot)] = row
            return list(by_slot.values()), len(items) - len(by_slot)

        current_items, current_duplicate_slots = deduplicate_sample_slots(current_items)
        previous_items, previous_duplicate_slots = deduplicate_sample_slots(previous_items)
        current_by_question: dict[str, list[Any]] = {}
        previous_by_question: dict[str, list[Any]] = {}
        for row in current_items:
            current_by_question.setdefault(str(row["question"]), []).append(row)
        for row in previous_items:
            previous_by_question.setdefault(str(row["question"]), []).append(row)
        questions = sorted(set(current_by_question) & set(previous_by_question))
        balanced_current: list[Any] = []
        balanced_previous: list[Any] = []
        per_question_samples: dict[str, int] = {}
        for question in questions:
            pair_count = min(
                len(current_by_question[question]), len(previous_by_question[question])
            )
            if pair_count <= 0:
                continue
            # Use the most recent observations on each side and the same count per
            # question, so a changed question mix cannot manufacture a trend.
            balanced_current.extend(current_by_question[question][-pair_count:])
            balanced_previous.extend(previous_by_question[question][-pair_count:])
            per_question_samples[question] = pair_count
        current_summary = summarize(balanced_current)
        previous_summary = summarize(balanced_previous)
        ready = (
            len(per_question_samples) >= minimum_matched_questions
            and len(balanced_current) >= minimum_samples
            and len(balanced_previous) >= minimum_samples
        )
        deltas: dict[str, float | None] = {}
        interval_signals: list[str] = []
        for metric in ("mention_rate", "recommendation_rate", "owned_citation_rate"):
            current_value, previous_value = current_summary[metric], previous_summary[metric]
            deltas[metric] = (
                round(float(current_value) - float(previous_value), 1)
                if ready and current_value is not None and previous_value is not None else None
            )
            current_ci = current_summary[f"{metric}_ci95"]
            previous_ci = previous_summary[f"{metric}_ci95"]
            if ready and current_ci and previous_ci and (
                current_ci[1] < previous_ci[0] or previous_ci[1] < current_ci[0]
            ):
                interval_signals.append(metric)
        return {
            "status": "comparison_ready" if ready else "insufficient_data",
            "matched_question_count": len(per_question_samples),
            "minimum_matched_questions": minimum_matched_questions,
            "paired_samples_per_window": len(balanced_current),
            "per_question_samples": per_question_samples,
            "current": current_summary,
            "previous": previous_summary,
            "deltas_percentage_points": deltas,
            "non_overlapping_ci_signals": interval_signals,
            "current_samples_excluded": len(current_items) - len(balanced_current),
            "previous_samples_excluded": len(previous_items) - len(balanced_previous),
            "duplicate_sample_slots_excluded": {
                "current": current_duplicate_slots,
                "previous": previous_duplicate_slots,
            },
        }

    daily_groups: dict[tuple[Any, str, str, str, str, str, str], list[Any]] = {}
    for row, day in parsed:
        daily_groups.setdefault((
            day, row["provider"], row["engine_surface"], row["prompt_variant"], row["prompt_version"],
            row["locale"], row["region"],
        ), []).append(row)
    daily = []
    for (day, provider, surface, variant, version, locale, region), items in sorted(
        daily_groups.items(), reverse=True
    ):
        daily.append({
            "date": day.isoformat(), "provider": provider, "engine_surface": surface,
            "prompt_variant": variant, "prompt_version": version,
            "locale": locale, "region": region, **summarize(items),
        })

    surfaces = sorted({
        (
            row["provider"], row["engine_surface"], row["prompt_variant"], row["prompt_version"],
            row["locale"], row["region"],
        )
        for row, _ in parsed
    })
    comparisons: list[dict[str, Any]] = []
    for provider, surface, variant, version, locale, region in surfaces:
        current_items = [
            row for row, day in parsed
            if row["provider"] == provider and row["engine_surface"] == surface
            and row["prompt_variant"] == variant and row["prompt_version"] == version
            and row["locale"] == locale and row["region"] == region
            and current_start <= day <= today
        ]
        previous_items = [
            row for row, day in parsed
            if row["provider"] == provider and row["engine_surface"] == surface
            and row["prompt_variant"] == variant and row["prompt_version"] == version
            and row["locale"] == locale and row["region"] == region
            and previous_start <= day <= previous_end
        ]
        current = summarize(current_items)
        previous = summarize(previous_items)
        panel = matched_panel(current_items, previous_items)
        comparisons.append({
            "provider": provider,
            "engine_surface": surface,
            "prompt_variant": variant,
            "prompt_version": version,
            "locale": locale,
            "region": region,
            "status": panel["status"],
            "current": current,
            "previous": previous,
            "matched_panel": panel,
            "deltas_percentage_points": panel["deltas_percentage_points"],
            "non_overlapping_ci_signals": panel["non_overlapping_ci_signals"],
        })

    primary_variant = str(monitor.get("primary_prompt_variant", "naturalistic"))
    primary_surface = str(monitor.get("primary_engine_surface", "browser"))
    primary_version = str(monitor.get("primary_prompt_version", "naturalistic-v1"))
    current_all_modes = [row for row, day in parsed if current_start <= day <= today]
    previous_all_modes = [row for row, day in parsed if previous_start <= day <= previous_end]
    current_all = [
        row for row in current_all_modes
        if row["prompt_variant"] == primary_variant
        and row["engine_surface"] == primary_surface
        and row["prompt_version"] == primary_version
        and row["locale"] == primary_locale
        and row["region"] == primary_region
    ]
    previous_all = [
        row for row in previous_all_modes
        if row["prompt_variant"] == primary_variant
        and row["engine_surface"] == primary_surface
        and row["prompt_version"] == primary_version
        and row["locale"] == primary_locale
        and row["region"] == primary_region
    ]
    primary_panel = matched_panel(current_all, previous_all)
    return {
        "status": (
            "no_samples" if not current_all and not previous_all
            else "comparison_ready" if primary_panel["status"] == "comparison_ready"
            else "insufficient_data"
        ),
        "window_days": window_days,
        "minimum_samples_per_window": minimum_samples,
        "minimum_matched_questions": minimum_matched_questions,
        "primary_prompt_variant": primary_variant,
        "primary_engine_surface": primary_surface,
        "primary_prompt_version": primary_version,
        "primary_locale": primary_locale,
        "primary_region": primary_region,
        "all_modes_current_samples": len(current_all_modes),
        "all_modes_previous_samples": len(previous_all_modes),
        "current_period": {"start": current_start.isoformat(), "end": today.isoformat(), **summarize(current_all)},
        "previous_period": {"start": previous_start.isoformat(), "end": previous_end.isoformat(), **summarize(previous_all)},
        "primary_matched_panel": primary_panel,
        "comparisons": comparisons,
        "daily": daily[: window_days * 8],
        "invalid_timestamps": invalid_timestamps,
        "future_samples_ignored": future_samples,
        "method_note": (
            "仅在引擎、surface、提示模式、提示版本、语言和地区完全一致，并把相邻窗口限制为相同问题、"
            "每题相同样本数的匹配面板后计算变化；匹配问题数和双方样本量均须达标。"
            "同一实验批次的重复样本槽位会去重；区间不重叠只作筛查信号，不证明因果。"
        ),
    }


def build_visibility_snapshot(settings: Settings, db: Database) -> dict[str, Any]:
    backfill_probe_analysis(settings, db)
    rows = db.query(
        """SELECT provider,question,brand_mentioned,domain_cited,recommended,sentiment,
        visibility_score,citation_domains_json,competitors_json,visibility_state,prompt_variant,prompt_version,engine_surface
        FROM probes ORDER BY id"""
    )
    batch_rows = [dict(row) for row in db.query(
        """SELECT b.batch_id,b.started_at,b.finished_at,b.status,b.planned_calls,b.attempted_calls,
        b.succeeded_calls,b.failed_calls,b.truncated_by_call_cap,
        (SELECT COUNT(*) FROM probe_batch_items i WHERE i.batch_id=b.batch_id) item_total,
        (SELECT COUNT(*) FROM probe_batch_items i WHERE i.batch_id=b.batch_id AND i.status='succeeded') item_succeeded,
        (SELECT COUNT(*) FROM probe_batch_items i WHERE i.batch_id=b.batch_id AND i.status='failed') item_failed,
        (SELECT COUNT(*) FROM probe_batch_items i WHERE i.batch_id=b.batch_id AND i.status IN ('planned','running')) item_pending
        FROM probe_batches b"""
    )]
    recent_batches = sorted(
        batch_rows,
        key=lambda batch: _parse_timestamp_utc(batch["started_at"]) or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )[:10]
    for batch in recent_batches:
        batch["truncated_by_call_cap"] = bool(batch["truncated_by_call_cap"])
    try:
        minimum_sov_mentions = max(1, int(
            settings.raw.get("monitor", {}).get("share_of_voice_min_mentions", 10)
        ))
    except (TypeError, ValueError):
        minimum_sov_mentions = 10
    if not rows:
        return {
            "sample_size": 0, "providers": [], "citation_domains": [],
            "competitor_visibility": [], "tracked_share_of_voice": None,
            "tracked_share_of_voice_status": "insufficient_mentions",
            "tracked_name_mentions": 0,
            "tracked_share_of_voice_min_mentions": minimum_sov_mentions,
            "recent_batches": recent_batches, "sampling_health": build_sampling_health(settings, db),
            "context_isolation": build_context_isolation_health(db),
            "probe_provenance": build_probe_provenance_health(db),
            "browser_activity": build_browser_activity_health(db),
            "cross_run_reproducibility": build_cross_run_reproducibility(settings, db),
            "trends": build_visibility_trends(settings, db),
        }
    providers: list[dict[str, Any]] = []
    provider_surfaces = sorted({
        (row["provider"], row["engine_surface"], row["prompt_variant"], row["prompt_version"])
        for row in rows
    })
    for provider, surface, variant, version in provider_surfaces:
        group = [
            row for row in rows
            if row["provider"] == provider and row["engine_surface"] == surface
            and row["prompt_variant"] == variant and row["prompt_version"] == version
        ]
        total = len(group)
        brand_mentions = sum(row["brand_mentioned"] for row in group)
        positive_brand_mentions = sum(
            row["brand_mentioned"] and row["sentiment"] == "positive" for row in group
        )
        providers.append(
            {
                "provider": provider,
                "engine_surface": surface,
                "prompt_variant": variant,
                "prompt_version": version,
                "samples": total,
                "mention_rate": round(sum(row["brand_mentioned"] for row in group) / total * 100, 1),
                "recommendation_rate": round(sum(row["recommended"] for row in group) / total * 100, 1),
                "owned_citation_rate": round(sum(row["domain_cited"] for row in group) / total * 100, 1),
                "positive_rate": (
                    round(positive_brand_mentions / brand_mentions * 100, 1)
                    if brand_mentions else None
                ),
                "positive_rate_basis": "brand_mentions_only",
                "average_visibility_score": round(sum(row["visibility_score"] for row in group) / total, 1),
                "mention_rate_ci95": _wilson_interval(sum(row["brand_mentioned"] for row in group), total),
                "owned_citation_rate_ci95": _wilson_interval(sum(row["domain_cited"] for row in group), total),
                "visibility_states": dict(Counter(row["visibility_state"] for row in group)),
                "surface_mix": dict(Counter(row["engine_surface"] for row in group)),
            }
        )
    domains: Counter[str] = Counter()
    for row in rows:
        try:
            domains.update(json.loads(row["citation_domains_json"] or "[]"))
        except json.JSONDecodeError:
            continue
    classified = Counter()
    for name, count in domains.items():
        classified[classify_citation_domain(name, settings)] += count
    brand_mentions = sum(row["brand_mentioned"] for row in rows)
    competitor_stats: dict[str, dict[str, Any]] = {}
    competitor_mentions = 0
    for row in rows:
        try:
            names = {
                str(name).strip() for name in json.loads(row["competitors_json"] or "[]")
                if str(name).strip()
            }
        except (json.JSONDecodeError, TypeError):
            continue
        competitor_mentions += len(names)
        for name in names:
            stats = competitor_stats.setdefault(name, {
                "mentions": 0, "questions": set(), "providers": set(), "co_mentions": 0,
            })
            stats["mentions"] += 1
            stats["questions"].add(str(row["question"]))
            stats["providers"].add(str(row["provider"]))
            stats["co_mentions"] += int(row["brand_mentioned"])
    competitor_visibility = [
        {
            "name": name,
            "mentions": stats["mentions"],
            "sample_mention_rate": round(stats["mentions"] / len(rows) * 100, 1),
            "mention_rate_ci95": _wilson_interval(stats["mentions"], len(rows)),
            "question_count": len(stats["questions"]),
            "provider_count": len(stats["providers"]),
            "brand_co_mentions": stats["co_mentions"],
        }
        for name, stats in competitor_stats.items()
    ]
    competitor_visibility.sort(key=lambda item: (-item["mentions"], item["name"]))
    tracked_mentions = brand_mentions + competitor_mentions
    share_of_voice = (
        round(brand_mentions / tracked_mentions * 100, 1)
        if tracked_mentions >= minimum_sov_mentions else None
    )
    return {
        "sample_size": len(rows),
        "providers": providers,
        "citation_domains": [{"domain": name, "count": count} for name, count in domains.most_common(20)],
        "citation_media_mix": dict(classified),
        "competitor_visibility": competitor_visibility,
        "tracked_share_of_voice": share_of_voice,
        "tracked_share_of_voice_status": (
            "ready" if share_of_voice is not None else "insufficient_mentions"
        ),
        "tracked_name_mentions": tracked_mentions,
        "tracked_share_of_voice_min_mentions": minimum_sov_mentions,
        "recent_batches": recent_batches,
        "sampling_health": build_sampling_health(settings, db),
        "context_isolation": build_context_isolation_health(db),
        "probe_provenance": build_probe_provenance_health(db),
        "browser_activity": build_browser_activity_health(db),
        "cross_run_reproducibility": build_cross_run_reproducibility(settings, db),
        "trends": build_visibility_trends(settings, db),
        "method_note": "置信区间仅描述当前固定样本；API、浏览器、自然原问、引用请求及不同提示版本必须分开解释。",
    }


def build_gap_analysis(settings: Settings, db: Database, *, persist: bool = True) -> dict[str, Any]:
    questions = db.query(
        "SELECT question,cluster,score FROM opportunities ORDER BY score DESC,id"
    )
    gaps: list[dict[str, Any]] = []
    now = now_iso()
    monitor = settings.raw.get("monitor", {})
    repeat_target = _repeat_target(settings)
    primary_variant = str(monitor.get("primary_prompt_variant", "naturalistic"))
    primary_surface = str(monitor.get("primary_engine_surface", "browser"))
    primary_version = str(monitor.get("primary_prompt_version", "naturalistic-v1"))
    try:
        minimum_providers = max(2, int(monitor.get("minimum_primary_providers", 2)))
    except (TypeError, ValueError):
        minimum_providers = 2
    core_questions = list(dict.fromkeys([
        *settings.raw.get("acquisition", {}).get("priority_questions", []),
        *settings.raw.get("geo_goals", {}).get("brand_reputation", {}).get("target_questions", []),
    ]))
    core_set = {str(question).strip() for question in core_questions if str(question).strip()}
    all_probe_rows = db.query(
        """SELECT id,provider,question,engine_surface,prompt_variant,prompt_version,experiment_id,sample_index,
        brand_mentioned,domain_cited,recommended,competitors_json,citation_domains_json FROM probes"""
    )
    all_counts = Counter(str(row["question"]) for row in all_probe_rows)
    primary_samples: dict[str, list[Any]] = {}
    for row in all_probe_rows:
        if (
            row["prompt_variant"] == primary_variant
            and row["engine_surface"] == primary_surface
            and row["prompt_version"] == primary_version
        ):
            primary_samples.setdefault(str(row["question"]), []).append(row)
    next_steps = {
        "measurement_gap": f"按主口径完成同批至少 {repeat_target} 次浏览器采样，不用其他提示实验代替基线",
        "sampling_gap": f"在原实验批次补齐至少 {repeat_target} 个唯一样本位，再判断可见度",
        "competitive_blind_spot": "先核验竞品候选实体与原回答，并在第二独立引擎复测，不自动写入竞品事实",
        "visibility_gap": "先在第二独立 AI 引擎复测；官网与发布解锁后再围绕该问题部署可引用资产",
        "citation_gap": "核对品牌提及上下文；官网上线后补齐规范地址、实体标记与可引用答案页",
        "narrative_gap": "核查现有提及是否中性或负面，只用已核验产品事实改善答案素材",
    }
    for question in questions:
        question_text = str(question["question"])
        all_sample_count = int(all_counts.get(question_text, 0))
        samples = primary_samples.get(question_text, [])
        if not samples:
            action_type, priority, reason = (
                "measurement_gap", 2,
                (
                    f"尚无自然用户原问样本；已有 {all_sample_count} 个其他提示实验样本，不能替代自然基线"
                    if all_sample_count else "该问题尚无自然用户原问样本"
                ),
            )
        else:
            repeat_groups: dict[tuple[str, str, str, str, str], set[int]] = {}
            for sample in samples:
                if not sample["experiment_id"]:
                    continue
                group_key = (
                    sample["provider"], sample["engine_surface"],
                    sample["prompt_variant"], sample["prompt_version"], sample["experiment_id"],
                )
                repeat_groups.setdefault(group_key, set()).add(max(1, int(sample["sample_index"])))
            repeat_ready = any(len(indexes) >= repeat_target for indexes in repeat_groups.values())
            repeat_ready_providers = {
                group_key[0] for group_key, indexes in repeat_groups.items()
                if len(indexes) >= repeat_target
            }
            mentions = sum(row["brand_mentioned"] for row in samples)
            citations = sum(row["domain_cited"] for row in samples)
            recommendations = sum(row["recommended"] for row in samples)
            competitor_set: set[str] = set()
            for row in samples:
                try:
                    competitor_set.update(json.loads(row["competitors_json"] or "[]"))
                except json.JSONDecodeError:
                    pass
            if not repeat_ready:
                action_type, priority, reason = (
                    "sampling_gap", 3,
                    f"已有 {len(samples)} 个样本，但尚无同批 {repeat_target} 次有效重复；先补采样再判断可见度",
                )
            elif mentions == 0 and competitor_set:
                action_type, priority, reason = (
                    "competitive_blind_spot", 5, "竞品被提及但宏图商机汇不可见"
                )
            elif mentions == 0:
                action_type, priority, reason = (
                    "visibility_gap", 4, "已有真实样本，但品牌尚未被提及"
                )
            elif citations == 0:
                action_type, priority, reason = (
                    "citation_gap", 4, "品牌已被提及，但自有域名没有被引用"
                )
            elif recommendations == 0:
                action_type, priority, reason = (
                    "narrative_gap", 3, "品牌被看见且被引用，但尚未形成正向推荐语境"
                )
            else:
                continue
        evidence = {
            "cluster": question["cluster"],
            "opportunity_score": question["score"],
            "sample_size": len(samples),
            "all_prompt_modes_sample_size": all_sample_count,
            "decision_prompt_variant": primary_variant,
            "decision_engine_surface": primary_surface,
            "decision_prompt_version": primary_version,
            "repeat_target": repeat_target,
            "minimum_providers": minimum_providers,
        }
        provider_count = len({str(row["provider"]) for row in samples})
        repeat_ready_provider_count = len(repeat_ready_providers) if samples else 0
        if not samples:
            confidence = "unmeasured"
        elif not repeat_ready:
            confidence = "insufficient_repeats"
        elif repeat_ready_provider_count < minimum_providers:
            confidence = "single_engine_preliminary"
        else:
            confidence = "multi_engine_observed"
        tier = "core" if question_text in core_set else "backlog"
        evidence.update({
            "tier": tier,
            "provider_count": provider_count,
            "repeat_ready_provider_count": repeat_ready_provider_count,
            "confidence": confidence,
            "recommended_next_step": next_steps[action_type],
        })
        action_key = hashlib.sha256(
            f"{question['question']}|{action_type}".encode("utf-8")
        ).hexdigest()[:24]
        if persist:
            db.execute(
                """INSERT INTO geo_actions(action_key,question,action_type,priority,reason,evidence_json,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,'open',?,?) ON CONFLICT(action_key) DO UPDATE SET priority=excluded.priority,
                reason=excluded.reason,evidence_json=excluded.evidence_json,updated_at=excluded.updated_at""",
                (action_key, question["question"], action_type, priority, reason,
                 json.dumps(evidence, ensure_ascii=False), now, now),
            )
        gaps.append({
            "action_key": action_key,
            "question": question["question"],
            "cluster": question["cluster"],
            "type": action_type,
            "priority": priority,
            "reason": reason,
            "samples": len(samples),
            "tier": tier,
            "confidence": confidence,
            "provider_count": provider_count,
            "repeat_ready_provider_count": repeat_ready_provider_count,
            "recommended_next_step": next_steps[action_type],
        })
    score_by_question = {str(row["question"]): int(row["score"]) for row in questions}
    gaps.sort(key=lambda item: (
        item["tier"] != "core",
        -item["priority"],
        -score_by_question.get(item["question"], 0),
        item["question"],
    ))
    active_keys = [item["action_key"] for item in gaps]
    if persist and active_keys:
        placeholders = ",".join("?" for _ in active_keys)
        db.execute(
            f"UPDATE geo_actions SET status='resolved',updated_at=? "
            f"WHERE status='open' AND action_key NOT IN ({placeholders})",
            (now, *active_keys),
        )
    elif persist:
        db.execute(
            "UPDATE geo_actions SET status='resolved',updated_at=? WHERE status='open'",
            (now,),
        )
    try:
        focus_limit = max(1, min(100, int(monitor.get("focus_action_limit", 20))))
    except (TypeError, ValueError):
        focus_limit = 20
    core_actions = [item for item in gaps if item["tier"] == "core"]
    evidence_backlog_actions = [
        item for item in gaps
        if item["tier"] == "backlog" and item["type"] not in {"measurement_gap", "sampling_gap"}
    ]
    focus_actions = (core_actions + evidence_backlog_actions)[:focus_limit]
    if not core_set:
        focus_actions = gaps[:focus_limit]
    deferred = [item for item in gaps if item not in focus_actions]
    return {
        "generated_at": now,
        "counts": dict(Counter(item["type"] for item in gaps)),
        "priority_counts": dict(Counter(item["type"] for item in core_actions)),
        "backlog_counts": dict(Counter(item["type"] for item in gaps if item["tier"] == "backlog")),
        "focus_actions": focus_actions,
        "focus_count": len(focus_actions),
        "backlog_summary": {
            "total_actions": len(gaps),
            "deferred_actions": len(deferred),
            "deferred_unmeasured": sum(item["type"] == "measurement_gap" for item in deferred),
            "reason": "核心问题未完成时，长尾未采样问题保留在完整待办中，不占用控制台聚焦位。",
        },
        "actions": gaps,
        "method_note": "全量待办保留可追溯；聚焦队列优先核心问题和已有证据的长尾缺口，单引擎结果只标记为初步。",
    }


def _candidate_evidence_strength(independent: int, providers: int, questions: int) -> str:
    if independent >= 2 and providers >= 2:
        return "cross_engine"
    if independent >= 3 and questions >= 2:
        return "multi_context"
    if independent >= 2:
        return "repeated_contexts"
    return "observed_once"


def _observation_key(row: Any) -> tuple[Any, ...]:
    if row["experiment_id"]:
        return (row["provider"], row["engine_surface"], row["question"], row["experiment_id"])
    return ("legacy_single", row["id"])


def discover_entity_candidates(settings: Settings, db: Database) -> dict[str, Any]:
    """Discover candidates using independent contexts, never raw repeat count as corroboration."""
    aliases = {settings.brand["name"], *settings.brand.get("aliases", [])}
    generic = {"中国政府采购网", "全国公共资源交易平台", "国家企业信用信息公示系统"}
    observations: dict[tuple[str, str], list[dict[str, Any]]] = {}
    probe_rows = db.query(
        """SELECT id,provider,engine_surface,question,experiment_id,answer,citation_domains_json
        FROM probes"""
    )
    for row in probe_rows:
        candidates: set[tuple[str, str]] = set()
        for match in re.finditer(r"(?m)^\s*(?:\d{1,2}[.、)]|[-*])\s*([^：:\n（(]{2,30})", row["answer"]):
            name = re.sub(r"^(?:推荐|选择|考虑|可以选择|可选)", "", match.group(1)).strip(" ‘’“”\"《》")
            if name and name not in aliases and name not in generic and not name.endswith(("方法", "步骤", "标准", "建议")):
                candidates.add((name, "brand_candidate"))
        try:
            domains = json.loads(row["citation_domains_json"] or "[]")
        except json.JSONDecodeError:
            domains = []
        for raw_domain in domains:
            domain = _canonical_domain(raw_domain)
            if not domain:
                continue
            category = classify_citation_domain(domain, settings)
            if category not in {"owned", "institution"}:
                candidates.add((str(domain), "citation_source" if category == "other" else category))
        for name, entity_type in candidates:
            observations.setdefault((name, entity_type), []).append({
                "probe_id": row["id"], "provider": row["provider"],
                "engine_surface": row["engine_surface"], "question": row["question"],
                "experiment_id": row["experiment_id"], "observation_key": list(_observation_key(row)),
            })
    timestamp = now_iso()
    db.execute("UPDATE entity_candidates SET status='not_observed_current_analysis'")
    confidence_by_strength = {
        "observed_once": 0.25,
        "repeated_contexts": 0.5,
        "multi_context": 0.7,
        "cross_engine": 0.85,
    }
    for (name, entity_type), contexts in observations.items():
        independent_contexts = {
            tuple(context["observation_key"]): context for context in contexts
        }
        providers = {context["provider"] for context in independent_contexts.values()}
        surfaces = {
            (context["provider"], context["engine_surface"])
            for context in independent_contexts.values()
        }
        questions = {context["question"] for context in independent_contexts.values()}
        strength = _candidate_evidence_strength(len(independent_contexts), len(providers), len(questions))
        status = (
            "review_candidate"
            if len(independent_contexts) >= 2 and (len(providers) >= 2 or len(questions) >= 2)
            else "candidate"
        )
        db.execute(
            """INSERT INTO entity_candidates(
            name,entity_type,sample_count,confidence,contexts_json,status,first_seen_at,last_seen_at,
            independent_count,provider_count,surface_count,question_count,evidence_strength
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET
            entity_type=excluded.entity_type,sample_count=excluded.sample_count,confidence=excluded.confidence,
            contexts_json=excluded.contexts_json,status=excluded.status,last_seen_at=excluded.last_seen_at,
            independent_count=excluded.independent_count,provider_count=excluded.provider_count,
            surface_count=excluded.surface_count,question_count=excluded.question_count,
            evidence_strength=excluded.evidence_strength""",
            (
                name, entity_type, len(contexts), confidence_by_strength[strength],
                json.dumps(list(independent_contexts.values())[-20:], ensure_ascii=False), status,
                timestamp, timestamp, len(independent_contexts), len(providers), len(surfaces),
                len(questions), strength,
            ),
        )
    rows = [dict(row) for row in db.query(
        """SELECT name,entity_type,sample_count,independent_count,provider_count,surface_count,question_count,
        evidence_strength,confidence,status FROM entity_candidates
        WHERE status<>'not_observed_current_analysis'
        ORDER BY independent_count DESC,provider_count DESC,question_count DESC,name"""
    )]
    return {
        "count": len(rows),
        "candidates": rows,
        "guardrail": "候选实体不会自动成为事实、竞品配置或发布内容；同批重复回答只算一个独立观察，仍需公开来源核验。",
    }


def build_citation_intelligence(
    settings: Settings, db: Database, persist: bool = True
) -> dict[str, Any]:
    """Rank cited domains by independent contexts while preserving their candidate-only status."""
    observations: dict[str, list[dict[str, Any]]] = {}
    invalid_domains = 0
    rows = db.query(
        """SELECT id,provider,engine_surface,question,experiment_id,citation_domains_json,probed_at
        FROM probes ORDER BY id"""
    )
    for row in rows:
        try:
            domains = json.loads(row["citation_domains_json"] or "[]")
        except json.JSONDecodeError:
            invalid_domains += 1
            continue
        if not isinstance(domains, list):
            invalid_domains += 1
            continue
        canonical_domains: set[str] = set()
        for item in domains:
            canonical_domain = _canonical_domain(item)
            if not canonical_domain:
                invalid_domains += 1
                continue
            canonical_domains.add(canonical_domain)
        for raw_domain in canonical_domains:
            observations.setdefault(raw_domain, []).append({
                "probe_id": row["id"],
                "provider": row["provider"],
                "engine_surface": row["engine_surface"],
                "question": row["question"],
                "experiment_id": row["experiment_id"],
                "observed_at": row["probed_at"],
                "observation_key": list(_observation_key(row)),
            })

    timestamp = now_iso()
    if persist:
        db.execute("UPDATE citation_sources SET review_status='not_observed_current_analysis'")
    sources: list[dict[str, Any]] = []
    for domain, contexts in observations.items():
        independent_contexts = {tuple(item["observation_key"]): item for item in contexts}
        independent_values = list(independent_contexts.values())
        providers = {item["provider"] for item in independent_values}
        surfaces = {(item["provider"], item["engine_surface"]) for item in independent_values}
        questions = {item["question"] for item in independent_values}
        experiments = {item["experiment_id"] for item in independent_values if item["experiment_id"]}
        strength = _candidate_evidence_strength(len(independent_values), len(providers), len(questions))
        category = classify_citation_domain(domain, settings)
        if category == "owned":
            review_status = "owned_observed"
        elif len(independent_values) >= 2 and (len(providers) >= 2 or len(questions) >= 2):
            review_status = "review_candidate"
        else:
            review_status = "observed"
        parsed_times = [
            parsed for parsed in (_parse_timestamp_utc(item["observed_at"]) for item in contexts)
            if parsed is not None
        ]
        first_seen = min(parsed_times).isoformat() if parsed_times else timestamp
        last_seen = max(parsed_times).isoformat() if parsed_times else timestamp
        source_record = {
            "domain": domain,
            "category": category,
            "sample_count": len(contexts),
            "independent_count": len(independent_values),
            "provider_count": len(providers),
            "surface_count": len(surfaces),
            "question_count": len(questions),
            "experiment_count": len(experiments),
            "evidence_strength": strength,
            "review_status": review_status,
            "first_seen_at": first_seen,
            "last_seen_at": last_seen,
        }
        sources.append(source_record)
        if persist:
            db.execute(
                """INSERT INTO citation_sources(
                domain,category,sample_count,independent_count,provider_count,surface_count,question_count,
                experiment_count,evidence_strength,review_status,contexts_json,first_seen_at,last_seen_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(domain) DO UPDATE SET
                category=excluded.category,sample_count=excluded.sample_count,
                independent_count=excluded.independent_count,provider_count=excluded.provider_count,
                surface_count=excluded.surface_count,question_count=excluded.question_count,
                experiment_count=excluded.experiment_count,
                evidence_strength=excluded.evidence_strength,review_status=excluded.review_status,
                contexts_json=excluded.contexts_json,first_seen_at=excluded.first_seen_at,
                last_seen_at=excluded.last_seen_at""",
                (
                    domain, category, len(contexts), len(independent_values), len(providers),
                    len(surfaces), len(questions), len(experiments), strength, review_status,
                    json.dumps(independent_values[-20:], ensure_ascii=False), first_seen, last_seen,
                ),
            )
    sources.sort(
        key=lambda item: (
            -item["independent_count"], -item["provider_count"],
            -item["question_count"], item["domain"],
        )
    )
    return {
        "source_count": len(sources),
        "review_candidate_count": sum(item["review_status"] == "review_candidate" for item in sources),
        "category_counts": dict(Counter(item["category"] for item in sources)),
        "cross_engine_count": sum(item["evidence_strength"] == "cross_engine" for item in sources),
        "invalid_domain_entries": invalid_domains,
        "sources": sources[:50],
        "guardrail": "AI 引用域名仅是来源候选与差距线索，不代表来源真实、权威、合作关系或品牌背书；采用前必须人工或规则核验原始页面。",
    }


def build_citation_url_ledger(
    settings: Settings,
    db: Database,
    persist: bool = True,
    verify_network: bool = False,
) -> dict[str, Any]:
    """Build a privacy-safe page-level citation ledger and optionally verify eligible URLs."""
    observations: dict[str, list[dict[str, Any]]] = {}
    invalid_urls = 0
    for row in db.query(
        """SELECT id,provider,engine_surface,question,experiment_id,citation_urls_json,probed_at
        FROM probes ORDER BY id"""
    ):
        try:
            urls = json.loads(row["citation_urls_json"] or "[]")
        except json.JSONDecodeError:
            invalid_urls += 1
            continue
        if not isinstance(urls, list):
            invalid_urls += 1
            continue
        canonical_urls: set[str] = set()
        for raw_url in urls:
            canonical = canonicalize_citation_url(raw_url)
            if canonical:
                canonical_urls.add(canonical)
            else:
                invalid_urls += 1
        for canonical in canonical_urls:
            observations.setdefault(canonical, []).append({
                "probe_id": row["id"], "provider": row["provider"],
                "engine_surface": row["engine_surface"], "question": row["question"],
                "experiment_id": row["experiment_id"], "observed_at": row["probed_at"],
                "observation_key": list(_observation_key(row)),
            })

    config = settings.raw.get("citation_verification", {})
    verification_enabled = bool(config.get("enabled", False))
    max_checks = max(0, min(int(config.get("max_candidates_per_run", 10)), 50))
    allowed_domains = citation_allowed_domains(settings)
    checks_used = 0
    timestamp = now_iso()
    existing = {
        row["url_hash"]: dict(row)
        for row in db.query("SELECT * FROM citation_url_candidates")
    }
    if persist:
        db.execute("UPDATE citation_url_candidates SET review_status='not_observed_current_analysis'")
    candidates: list[dict[str, Any]] = []
    for canonical, contexts in observations.items():
        independent = list({tuple(item["observation_key"]): item for item in contexts}.values())
        providers = {item["provider"] for item in independent}
        surfaces = {(item["provider"], item["engine_surface"]) for item in independent}
        questions = {item["question"] for item in independent}
        experiments = {item["experiment_id"] for item in independent if item["experiment_id"]}
        domain = (urlsplit(canonical).hostname or "").lower().removeprefix("www.")
        category = classify_citation_domain(domain, settings)
        strength = _candidate_evidence_strength(len(independent), len(providers), len(questions))
        repeated = len(independent) >= 2 and (len(providers) >= 2 or len(questions) >= 2)
        allowlisted = domain_is_allowlisted(domain, allowed_domains)
        if category == "owned":
            review_status = "owned_observed"
        elif not allowlisted:
            review_status = "needs_domain_review"
        elif repeated:
            review_status = "verification_eligible"
        else:
            review_status = "needs_more_observations"
        parsed_times = [
            parsed for parsed in (_parse_timestamp_utc(item["observed_at"]) for item in contexts)
            if parsed is not None
        ]
        first_seen = min(parsed_times).isoformat() if parsed_times else timestamp
        last_seen = max(parsed_times).isoformat() if parsed_times else timestamp
        url_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        previous = existing.get(url_hash, {})
        network = {
            "network_status": previous.get("network_status", "not_checked"),
            "http_status": previous.get("http_status"),
            "content_type": previous.get("content_type", ""),
            "checked_at": previous.get("checked_at"),
            "last_error": previous.get("last_error"),
        }
        if verify_network and verification_enabled and review_status == "verification_eligible" and checks_used < max_checks:
            network = {
                "network_status": "not_checked", "http_status": None, "content_type": "",
                "checked_at": None, "last_error": None,
                **verify_citation_url(canonical, settings),
            }
            checks_used += 1
        record = {
            "url_hash": url_hash, "canonical_url": canonical, "domain": domain,
            "category": category, "sample_count": len(contexts),
            "independent_count": len(independent), "provider_count": len(providers),
            "surface_count": len(surfaces), "question_count": len(questions),
            "experiment_count": len(experiments), "evidence_strength": strength,
            "review_status": review_status, **network,
            "first_seen_at": first_seen, "last_seen_at": last_seen,
        }
        candidates.append(record)
        if persist:
            db.execute(
                """INSERT INTO citation_url_candidates(
                url_hash,canonical_url,domain,category,sample_count,independent_count,provider_count,
                surface_count,question_count,experiment_count,evidence_strength,review_status,
                network_status,http_status,content_type,checked_at,last_error,contexts_json,
                first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(url_hash) DO UPDATE SET canonical_url=excluded.canonical_url,
                domain=excluded.domain,category=excluded.category,sample_count=excluded.sample_count,
                independent_count=excluded.independent_count,provider_count=excluded.provider_count,
                surface_count=excluded.surface_count,question_count=excluded.question_count,
                experiment_count=excluded.experiment_count,evidence_strength=excluded.evidence_strength,
                review_status=excluded.review_status,network_status=excluded.network_status,
                http_status=excluded.http_status,content_type=excluded.content_type,
                checked_at=excluded.checked_at,last_error=excluded.last_error,
                contexts_json=excluded.contexts_json,first_seen_at=excluded.first_seen_at,
                last_seen_at=excluded.last_seen_at""",
                (
                    url_hash, canonical, domain, category, len(contexts), len(independent), len(providers),
                    len(surfaces), len(questions), len(experiments), strength, review_status,
                    network["network_status"], network["http_status"], network["content_type"],
                    network["checked_at"], network["last_error"],
                    json.dumps(independent[-20:], ensure_ascii=False), first_seen, last_seen,
                ),
            )
    candidates.sort(key=lambda item: (-item["independent_count"], item["canonical_url"]))
    return {
        "candidate_count": len(candidates),
        "verification_eligible_count": sum(item["review_status"] == "verification_eligible" for item in candidates),
        "verified_count": sum(item["network_status"] == "verified" for item in candidates),
        "needs_domain_review_count": sum(item["review_status"] == "needs_domain_review" for item in candidates),
        "invalid_url_entries": invalid_urls,
        "network_checks_performed": checks_used,
        "verification_enabled": verification_enabled,
        "allowed_domains": sorted(allowed_domains),
        "candidates": candidates[:50],
        "guardrail": "具体链接先去除查询参数与片段，并按独立观察计数；仅重复出现且域名在显式白名单内的候选可做公网校验，校验通过仍不等于权威或品牌背书。",
    }


def build_maturity_audit(settings: Settings, db: Database) -> dict[str, Any]:
    from .attribution import build_attribution_report

    evidence_audit = audit_brand_facts(settings)
    valid_fact_count = evidence_audit["valid_count"]
    probe_count = db.query("SELECT COUNT(*) n FROM probes")[0]["n"]
    sampling_health = build_sampling_health(settings, db)
    prompt_count = db.query("SELECT COUNT(*) n FROM opportunities")[0]["n"]
    attribution = build_attribution_report(settings, db)
    attribution_quality = attribution["data_quality"]["status"]
    attribution_score = (
        80 if attribution["funnel"]["won"]
        else 65 if attribution["funnel"]["inquiry"]
        else 50 if attribution_quality == "collecting"
        else 30 if attribution_quality == "needs_attention"
        else 20
    )
    priority_coverage = sampling_health["priority_question_coverage"]
    measurement_score = (
        50 + round(
            40 * priority_coverage["repeat_ready_questions"]
            / priority_coverage["question_count"]
        )
        if priority_coverage["question_count"] and sampling_health["primary_samples"]
        else 50 if probe_count else 20
    )
    if sampling_health["primary_engine_coverage"]["status"] != "healthy":
        measurement_score = min(measurement_score, 70)
    assets = settings.root / "content" / "site-assets"
    layers = {
        "evidence": {
            "score": 100 if valid_fact_count >= 5 else min(100, valid_fact_count * 20),
            "status": (
                f"{valid_fact_count} 条证据指纹与核验日期有效；"
                f"{evidence_audit['invalid_count']} 条无效或已漂移"
            ),
        },
        "entity": {
            "score": 100 if valid_fact_count and (assets / "entity-graph.jsonld").exists() and (assets / "claim-ledger.json").exists() else 0,
            "status": (
                "实体图与带证据回执的事实账本已生成"
                if valid_fact_count and (assets / "entity-graph.jsonld").exists()
                else "实体资产缺失或证据已失效"
            ),
        },
        "prompt_strategy": {
            "score": 100 if prompt_count >= 100 else round(min(prompt_count / 100, 1) * 100),
            "status": f"{prompt_count} 个问题",
        },
        "measurement": {
            "score": measurement_score,
            "status": (
                f"{probe_count} 个全部实验样本；自然原问 {sampling_health['primary_samples']} 个；"
                f"自然原问达标组 {sampling_health['primary_target_reached_groups']}；"
                f"核心问题覆盖 {sampling_health['priority_question_coverage']['repeat_ready_questions']}/"
                f"{sampling_health['priority_question_coverage']['question_count']}；"
                f"独立引擎 {sampling_health['primary_engine_coverage']['repeat_ready_provider_count']}/"
                f"{sampling_health['primary_engine_coverage']['minimum_providers']}；"
                f"主口径状态 {sampling_health['primary_status']}"
            ),
        },
        "website": {
            "score": 0 if not settings.site_url else 50,
            "status": "ICP 阶段，官网未参与" if not settings.site_url else "官网已配置，等待完整技术审计",
        },
        "attribution": {
            "score": attribution_score,
            "status": (
                "已有 UTM 与离线质量审计；等待接入真实落地页、咨询和 CRM 事件"
                if attribution_quality == "awaiting_integration"
                else f"已记录 {attribution['events']} 个事件；数据质量 {attribution_quality}"
            ),
        },
    }
    weights = {"evidence": 20, "entity": 15, "prompt_strategy": 15, "measurement": 20, "website": 15, "attribution": 15}
    score = round(sum(layers[name]["score"] * weight for name, weight in weights.items()) / 100, 1)
    return {"score": score, "layers": layers, "note": "成熟度衡量系统能力，不代表外部 GEO 效果。"}


def build_strategy_snapshot(settings: Settings, db: Database) -> Path:
    from .attribution import build_attribution_report
    from .attribution_ingest import build_attribution_ingest_readiness
    from .probe_readiness import build_ai_probe_readiness

    payload = {
        "schema_version": STRATEGY_SCHEMA_VERSION,
        "generated_at": now_iso(),
        "maturity": build_maturity_audit(settings, db),
        "evidence_audit": audit_brand_facts(settings),
        "visibility": build_visibility_snapshot(settings, db),
        "probe_provenance": build_probe_provenance_health(db),
        "browser_activity": build_browser_activity_health(db),
        "cross_run_reproducibility": build_cross_run_reproducibility(settings, db),
        "answer_integrity": build_answer_integrity_snapshot(settings, db),
        "gaps": build_gap_analysis(settings, db),
        "entity_candidates": discover_entity_candidates(settings, db),
        "citation_intelligence": build_citation_intelligence(settings, db),
        "citation_url_ledger": build_citation_url_ledger(settings, db),
        "ai_probe_readiness": build_ai_probe_readiness(settings, db),
        "attribution": build_attribution_report(settings, db),
        "attribution_ingest": build_attribution_ingest_readiness(settings),
        "attribution_plan": {
            "stage_1": "记录 AI referral、utm_source、utm_campaign、landing_page 和首次访问时间",
            "stage_2": "咨询或注册表单增加‘从哪里知道宏图商机汇’自报来源",
            "stage_3": "CRM 回传有效咨询、报价、成交和收入，仅用聚合数据评估 GEO",
            "privacy": "不在 GEO 报告中保存私人手机号、聊天内容或未授权联系人信息",
        },
    }
    path = settings.root / "reports" / "geo-strategy-snapshot.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def build_claim_ledger(
    settings: Settings,
    valid_facts: list[dict[str, Any]],
    evidence_audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    audit = evidence_audit or audit_brand_facts(settings)
    receipt_index = {
        (receipt["claim"], receipt["source"]): receipt for receipt in audit["receipts"]
    }
    claims = []
    for index, fact in enumerate(valid_facts, start=1):
        claim = str(fact["claim"]).strip()
        receipt = receipt_index.get((claim, str(fact["source"]).strip()), {})
        claims.append(
            {
                "id": f"HT-{index:03d}",
                "claim": claim,
                "source": fact["source"],
                "source_type": fact.get("source_type", "official_site"),
                "verified": True,
                "claim_hash": hashlib.sha256(claim.encode("utf-8")).hexdigest(),
                "approved_claim_hash": receipt.get("expected_claim_sha256", ""),
                "verified_at": fact.get("verified_at", ""),
                "evidence_status": receipt.get("status", "unknown"),
                "evidence_excerpt_sha256": receipt.get("excerpt_sha256", ""),
                "source_file_sha256": receipt.get("source_file_sha256", ""),
                "repository_revision_context": receipt.get("repository_revision_context", ""),
                "repository_revision_scope": receipt.get("repository_revision_scope", ""),
                "support_terms": receipt.get("support_terms", []),
                "matched_support_terms": receipt.get("matched_support_terms", []),
                "claim_evidence_map": receipt.get("claim_evidence_map", []),
                "claim_map_covers_full_claim": receipt.get("claim_map_covers_full_claim", False),
                "checked_at": receipt.get("checked_at", ""),
            }
        )
    return {
        "schema_version": 3,
        "brand": settings.brand["name"],
        "evidence_guardrail": audit["guardrail"],
        "claims": claims,
    }
