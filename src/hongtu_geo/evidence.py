from __future__ import annotations

import hashlib
import re
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .claim_policy import audit_claim_language
from .core import Settings, now_iso


SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
GIT_REVISION_PATTERN = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
LINE_ANCHOR_PATTERN = re.compile(r"L(\d+)(?:-L?(\d+))?")


def _host_matches(url: str, expected_domain: str) -> bool:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    expected = expected_domain.lower().rstrip(".")
    return bool(expected and (host == expected or host.endswith("." + expected)))


def _normalize_support_text(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def _normalize_claim_contract_text(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _clean_support_terms(values: Any) -> list[str]:
    terms: list[str] = []
    normalized_terms: set[str] = set()
    if not isinstance(values, list):
        return terms
    for value in values:
        term = str(value).strip() if isinstance(value, str) else ""
        normalized_term = _normalize_support_text(term)
        if (
            len(normalized_term) >= 2
            and any(character.isalnum() for character in normalized_term)
            and normalized_term not in normalized_terms
        ):
            terms.append(term)
            normalized_terms.add(normalized_term)
    return terms


def _repository_revision(repository: Path) -> str:
    """Best-effort Git revision context; excerpt hashes remain the proof boundary."""
    git_entry = repository / ".git"
    try:
        if git_entry.is_file():
            marker = git_entry.read_text(encoding="utf-8").strip()
            if not marker.lower().startswith("gitdir:"):
                return ""
            git_dir = Path(marker.split(":", 1)[1].strip())
            if not git_dir.is_absolute():
                git_dir = (repository / git_dir).resolve()
        elif git_entry.is_dir():
            git_dir = git_entry.resolve()
        else:
            return ""
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if GIT_REVISION_PATTERN.fullmatch(head):
            return head
        if not head.startswith("ref:"):
            return ""
        ref_name = head.split(":", 1)[1].strip()
        loose_ref = git_dir / Path(ref_name)
        if loose_ref.is_file():
            revision = loose_ref.read_text(encoding="utf-8").strip()
            return revision if GIT_REVISION_PATTERN.fullmatch(revision) else ""
        packed_refs = git_dir / "packed-refs"
        if packed_refs.is_file():
            for line in packed_refs.read_text(encoding="utf-8").splitlines():
                if line.startswith(("#", "^")):
                    continue
                revision, _, name = line.partition(" ")
                if name == ref_name and GIT_REVISION_PATTERN.fullmatch(revision):
                    return revision
    except (OSError, UnicodeError, ValueError):
        return ""
    return ""


def inspect_fact_evidence(fact: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Return a reproducible receipt without treating a boolean flag as sufficient proof."""
    checked_at = now_iso()
    claim = str(fact.get("claim", "")).strip()
    source = str(fact.get("source", "")).strip()
    receipt: dict[str, Any] = {
        "claim": claim,
        "source": source,
        "source_type": str(fact.get("source_type", "official_site")),
        "declared_verified": bool(fact.get("verified")),
        "verified_at": str(fact.get("verified_at", "")),
        "checked_at": checked_at,
        "status": "invalid",
    }
    if not receipt["declared_verified"]:
        receipt["status"] = "not_declared_verified"
        return receipt
    if not claim or not source or any(marker in source for marker in ("待", "未接入", "unknown")):
        receipt["status"] = "missing_claim_or_source"
        return receipt
    if source.startswith(("http://", "https://")):
        official_domain = urlsplit(settings.site_url).hostname or ""
        language_audit = audit_claim_language(claim, settings)
        receipt["claim_policy"] = language_audit
        if not _host_matches(source, official_domain):
            receipt["status"] = "untrusted_web_domain"
        elif not language_audit["passed"]:
            receipt["status"] = "prohibited_claim_language"
        elif bool(settings.raw.get("evidence", {}).get("require_http_evidence_receipts", False)):
            receipt["status"] = "missing_http_evidence_receipt"
        else:
            receipt["status"] = "verified_current"
        return receipt

    evidence = settings.raw.get("evidence", {})
    if receipt["source_type"] != "product_repository" or evidence.get("mode") != "product_repository":
        receipt["status"] = "unsupported_source_type"
        return receipt
    prefix = str(evidence.get("repository_uri", "repo://newhongtu/")).rstrip("/") + "/"
    if not source.startswith(prefix):
        receipt["status"] = "repository_uri_mismatch"
        return receipt
    repository_text = str(evidence.get("repository_path", "")).strip()
    if not repository_text:
        receipt["status"] = "repository_missing"
        return receipt
    repository = Path(repository_text).resolve()
    relative = source.removeprefix(prefix).split("#", 1)[0]
    try:
        target = (repository / relative).resolve()
        target.relative_to(repository)
    except (OSError, ValueError):
        receipt["status"] = "path_outside_repository"
        return receipt
    if not repository.is_dir() or not target.is_file():
        receipt["status"] = "source_file_missing"
        return receipt
    anchor = source.split("#", 1)[1] if "#" in source else ""
    match = LINE_ANCHOR_PATTERN.fullmatch(anchor)
    if not match:
        receipt["status"] = "invalid_line_anchor"
        return receipt
    start = int(match.group(1))
    end = int(match.group(2) or start)
    if start <= 0 or end < start:
        receipt["status"] = "invalid_line_anchor"
        return receipt
    try:
        source_bytes = target.read_bytes()
        content = source_bytes.decode("utf-8")
    except (OSError, UnicodeError):
        receipt["status"] = "source_unreadable"
        return receipt
    lines = content.splitlines()
    if end > len(lines):
        receipt["status"] = "line_range_out_of_bounds"
        return receipt
    excerpt = "\n".join(lines[start - 1:end])
    excerpt_sha256 = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
    expected_sha256 = str(fact.get("evidence_excerpt_sha256", "")).strip().lower()
    receipt.update({
        "repository_relative_path": target.relative_to(repository).as_posix(),
        "line_start": start,
        "line_end": end,
        "line_count": len(lines),
        "source_file_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "excerpt_sha256": excerpt_sha256,
        "expected_excerpt_sha256": expected_sha256,
    })
    revision = _repository_revision(repository)
    if revision:
        receipt["repository_revision_context"] = revision
        receipt["repository_revision_scope"] = "head_context_only_file_hash_is_authoritative"
    if not SHA256_PATTERN.fullmatch(expected_sha256):
        receipt["status"] = "missing_or_invalid_fingerprint"
        return receipt
    if excerpt_sha256 != expected_sha256:
        receipt["status"] = "source_changed"
        return receipt
    claim_sha256 = hashlib.sha256(claim.encode("utf-8")).hexdigest()
    expected_claim_sha256 = str(fact.get("evidence_claim_sha256", "")).strip().lower()
    require_claim_fingerprint = bool(evidence.get("require_claim_fingerprint", False))
    receipt.update({
        "claim_sha256": claim_sha256,
        "expected_claim_sha256": expected_claim_sha256,
        "claim_fingerprint_required": require_claim_fingerprint,
    })
    if require_claim_fingerprint and not SHA256_PATTERN.fullmatch(expected_claim_sha256):
        receipt["status"] = "missing_or_invalid_claim_fingerprint"
        return receipt
    if require_claim_fingerprint and claim_sha256 != expected_claim_sha256:
        receipt["status"] = "claim_changed"
        return receipt
    language_audit = audit_claim_language(claim, settings)
    receipt["claim_policy"] = language_audit
    if not language_audit["passed"]:
        receipt["status"] = "prohibited_claim_language"
        return receipt
    require_support_terms = bool(evidence.get("require_support_terms", False))
    try:
        minimum_support_terms = int(evidence.get("minimum_support_terms", 2))
    except (TypeError, ValueError):
        minimum_support_terms = 2
    minimum_support_terms = max(1, min(minimum_support_terms, 10))
    support_terms = _clean_support_terms(fact.get("evidence_terms", []))
    normalized_excerpt = _normalize_support_text(excerpt)
    matched_terms = [term for term in support_terms if _normalize_support_text(term) in normalized_excerpt]
    receipt.update({
        "support_terms_required": require_support_terms,
        "minimum_support_terms": minimum_support_terms,
        "support_term_count": len(support_terms),
        "matched_support_term_count": len(matched_terms),
        "support_terms": support_terms,
        "matched_support_terms": matched_terms,
    })
    if require_support_terms and len(support_terms) < minimum_support_terms:
        receipt["status"] = "missing_support_terms"
        return receipt
    if require_support_terms and len(matched_terms) != len(support_terms):
        receipt["status"] = "claim_not_supported_by_excerpt"
        return receipt
    require_claim_map = bool(evidence.get("require_claim_evidence_map", False))
    declared_map = fact.get("claim_evidence_map", [])
    mapped_assertions: list[dict[str, Any]] = []
    map_malformed = not isinstance(declared_map, list)
    if isinstance(declared_map, list):
        for assertion in declared_map:
            if not isinstance(assertion, dict):
                map_malformed = True
                continue
            fragment = str(assertion.get("claim_fragment", "")).strip()
            terms = _clean_support_terms(assertion.get("evidence_terms", []))
            source_markers = _clean_support_terms(assertion.get("source_markers", []))
            normalized_fragment = _normalize_support_text(fragment)
            matched = [
                term for term in terms
                if _normalize_support_text(term) in normalized_excerpt
                and _normalize_support_text(term) in normalized_fragment
            ]
            matched_source_markers = [
                marker for marker in source_markers
                if _normalize_support_text(marker) in normalized_excerpt
            ]
            if not _normalize_claim_contract_text(fragment) or not terms:
                map_malformed = True
            mapped_assertions.append({
                "claim_fragment": fragment,
                "evidence_terms": terms,
                "matched_evidence_terms": matched,
                "source_markers": source_markers,
                "matched_source_markers": matched_source_markers,
                "fully_matched": (
                    bool(terms)
                    and len(matched) == len(terms)
                    and len(matched_source_markers) == len(source_markers)
                ),
            })
    mapped_claim = "".join(
        _normalize_claim_contract_text(assertion["claim_fragment"])
        for assertion in mapped_assertions
    )
    normalized_claim = _normalize_claim_contract_text(claim)
    receipt.update({
        "claim_evidence_map_required": require_claim_map,
        "claim_assertion_count": len(mapped_assertions),
        "claim_map_covers_full_claim": bool(normalized_claim) and mapped_claim == normalized_claim,
        "claim_evidence_map": mapped_assertions,
    })
    if require_claim_map and (map_malformed or not mapped_assertions):
        receipt["status"] = "missing_or_invalid_claim_evidence_map"
        return receipt
    if require_claim_map and mapped_claim != normalized_claim:
        receipt["status"] = "claim_map_does_not_cover_claim"
        return receipt
    if require_claim_map and not all(assertion["fully_matched"] for assertion in mapped_assertions):
        receipt["status"] = "claim_map_evidence_missing"
        return receipt
    try:
        verified_date = date.fromisoformat(receipt["verified_at"])
    except ValueError:
        receipt["status"] = "missing_or_invalid_verification_date"
        return receipt
    today = datetime.now(UTC).date()
    age_days = (today - verified_date).days
    receipt["verification_age_days"] = age_days
    try:
        max_age_days = int(evidence.get("max_fact_age_days", 180))
    except (TypeError, ValueError):
        max_age_days = 180
    max_age_days = max(1, min(max_age_days, 3650))
    receipt["max_fact_age_days"] = max_age_days
    if age_days < 0:
        receipt["status"] = "verification_date_in_future"
    elif age_days > max_age_days:
        receipt["status"] = "verification_expired"
    else:
        receipt["status"] = "verified_current"
    return receipt


def audit_brand_facts(settings: Settings) -> dict[str, Any]:
    receipts = [inspect_fact_evidence(fact, settings) for fact in settings.brand.get("facts", [])]
    counts = Counter(receipt["status"] for receipt in receipts)
    return {
        "generated_at": now_iso(),
        "fact_count": len(receipts),
        "valid_count": counts.get("verified_current", 0),
        "invalid_count": len(receipts) - counts.get("verified_current", 0),
        "status_counts": dict(counts),
        "receipts": receipts,
        "guardrail": "verified=true 只是声明；只有来源边界、行号、摘要指纹、支持词、完整主张映射和核验日期同时有效的事实才能进入内容资产；这些是可追溯性闸门，不等同于自动语义真值证明。",
    }


def fact_evidence_valid(fact: dict[str, Any], settings: Settings) -> bool:
    return inspect_fact_evidence(fact, settings)["status"] == "verified_current"
