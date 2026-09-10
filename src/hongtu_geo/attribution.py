from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import statistics
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

from .core import Database, Settings, now_iso


EVENT_TYPES = ("ai_referral", "signup", "inquiry", "qualified_lead", "quote", "won")
AI_ENGINES = ("chatgpt", "deepseek", "perplexity", "gemini", "copilot", "claude", "doubao", "kimi", "unknown")
STAGE_ORDER = {name: index for index, name in enumerate(EVENT_TYPES)}
DEFAULT_METADATA_ALLOWLIST = {
    "page", "cta", "content_id", "question_cluster", "campaign_variant",
    "device_type", "region", "industry", "lead_source_detail",
}
FORBIDDEN_METADATA_KEYS = {
    "address", "chat", "chat_content", "company_name", "contact", "email",
    "message", "name", "openid", "phone", "real_name", "remark", "unionid",
    "user_id", "wechat", "wechat_id", "wx", "wx_account", "wxid",
}


def _safe_text(value: Any, maximum: int = 300) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", "", str(value or "")).strip()[:maximum]


def _safe_url(value: Any) -> str:
    text = _safe_text(value, 1000)
    if not text:
        return ""
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        return ""
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
    ):
        return ""
    try:
        hostname = parts.hostname.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return ""
    netloc = hostname if port is None else f"{hostname}:{port}"
    path = "" if _looks_sensitive(unquote(parts.path)) else parts.path
    return urlunsplit((parts.scheme, netloc, path, "", ""))


def _looks_sensitive(value: str) -> bool:
    return bool(
        re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", value)
        or re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", value)
        or re.search(r"(?i)(?:wxid_[a-z0-9_-]{5,}|(?:微信|联系人|姓名|聊天内容)\s*[:：])", value)
    )


def _safe_non_pii(value: Any, maximum: int) -> str:
    text = _safe_text(value, maximum)
    return "" if _looks_sensitive(text) else text


def _safe_category_token(value: Any, maximum: int) -> str:
    text = _safe_non_pii(value, maximum)
    return text if text and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", text) else ""


def _attribution_salt(settings: Settings) -> str:
    configured = os.getenv("HONGTU_ATTRIBUTION_SALT", "").strip()
    if configured:
        return configured
    path = settings.root / "data" / "attribution-salt.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(secrets.token_urlsafe(48), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return path.read_text(encoding="utf-8").strip()


def _anonymous_hash(value: Any, settings: Settings) -> str:
    raw = _safe_text(value, 200)
    if not raw:
        return ""
    if _looks_sensitive(raw):
        raise ValueError("anonymous_id 必须是第一方随机 UUID，不能使用个人联系方式")
    try:
        parsed_id = uuid.UUID(raw)
    except ValueError as exc:
        raise ValueError("anonymous_id 必须是第一方随机 UUID") from exc
    if parsed_id.int == 0:
        raise ValueError("anonymous_id 不能使用全零 UUID")
    raw = str(parsed_id)
    salt = _attribution_salt(settings)
    return hashlib.sha256(f"{salt}|{raw}".encode("utf-8")).hexdigest()


def _event_time(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("occurred_at 必须是 ISO-8601 时间") from exc
    if parsed.tzinfo is None:
        raise ValueError("occurred_at 必须包含时区")
    return parsed.astimezone(UTC)


def record_attribution_event(settings: Settings, db: Database, payload: dict[str, Any]) -> dict[str, Any]:
    event_type = _safe_text(payload.get("event_type"), 40)
    if event_type not in EVENT_TYPES:
        raise ValueError(f"event_type 必须是：{', '.join(EVENT_TYPES)}")
    engine = _safe_text(payload.get("source_engine"), 40).lower() or "unknown"
    if engine not in AI_ENGINES:
        engine = "unknown"
    supplied_occurred_at = _safe_text(payload.get("occurred_at"), 40)
    occurred_at = _event_time(supplied_occurred_at or now_iso()).isoformat()
    value = payload.get("value")
    if value is not None:
        try:
            value = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("value 超出允许范围") from exc
        if not math.isfinite(value) or value < 0 or value > 1_000_000_000:
            raise ValueError("value 超出允许范围")
    metadata = payload.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError("metadata 必须是对象")
    configured_allowlist = settings.raw.get("attribution", {}).get("metadata_allowlist", [])
    allowed_metadata = DEFAULT_METADATA_ALLOWLIST | {
        _safe_text(item, 60).lower() for item in configured_allowlist
    }
    sanitized_metadata: dict[str, str] = {}
    for key, item in metadata.items():
        safe_key = _safe_text(key, 60).lower()
        if safe_key in FORBIDDEN_METADATA_KEYS:
            continue
        if safe_key not in allowed_metadata or isinstance(item, (dict, list, tuple, set)):
            continue
        safe_value = _safe_category_token(item, 100)
        if safe_value:
            sanitized_metadata[safe_key] = safe_value
    metadata = sanitized_metadata
    supplied_event_id = _safe_text(payload.get("event_id"), 100)
    if not supplied_event_id:
        raise ValueError("event_id 为必填的稳定幂等键")
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", supplied_event_id):
        raise ValueError("event_id 只能包含字母、数字、点、下划线、冒号和连字符")
    event_id = supplied_event_id
    landing_url = _safe_url(payload.get("landing_url"))
    utm_source = _safe_category_token(payload.get("utm_source"), 100)
    utm_medium = _safe_category_token(payload.get("utm_medium"), 100)
    utm_campaign = _safe_category_token(payload.get("utm_campaign"), 150)
    anonymous_id_hash = _anonymous_hash(payload.get("anonymous_id"), settings)
    metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
    inserted = db.execute(
        """INSERT OR IGNORE INTO attribution_events(
        event_id,occurred_at,event_type,source_engine,landing_url,utm_source,utm_medium,
        utm_campaign,anonymous_id_hash,value,metadata_json,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            event_id,
            occurred_at,
            event_type,
            engine,
            landing_url,
            utm_source,
            utm_medium,
            utm_campaign,
            anonymous_id_hash,
            value,
            metadata_json,
            now_iso(),
        ),
    )
    stored = db.query(
        """SELECT id,event_id,event_type,source_engine,occurred_at,landing_url,utm_source,utm_medium,
        utm_campaign,anonymous_id_hash,value,metadata_json FROM attribution_events WHERE event_id=?""",
        (event_id,),
    )[0]
    if inserted.rowcount == 0:
        expected = {
            "event_type": event_type, "source_engine": engine,
            "landing_url": landing_url, "utm_source": utm_source, "utm_medium": utm_medium,
            "utm_campaign": utm_campaign, "anonymous_id_hash": anonymous_id_hash,
            "value": value, "metadata_json": metadata_json,
        }
        if supplied_occurred_at:
            expected["occurred_at"] = occurred_at
        conflicts = []
        for key, expected_value in expected.items():
            stored_value = stored[key]
            if key == "metadata_json":
                try:
                    stored_value = json.loads(stored_value)
                    expected_value = json.loads(str(expected_value))
                except (TypeError, json.JSONDecodeError):
                    pass
            if stored_value != expected_value:
                conflicts.append(key)
        if conflicts:
            raise ValueError("event_id 已存在但载荷不一致：" + ", ".join(conflicts))
    row = {key: stored[key] for key in ("id", "event_id", "event_type", "source_engine", "occurred_at")}
    return dict(row)


def build_attribution_report(settings: Settings, db: Database) -> dict[str, Any]:
    rows = db.query(
        "SELECT event_id,occurred_at,event_type,source_engine,anonymous_id_hash,value FROM attribution_events "
        "ORDER BY occurred_at,id"
    )
    now_utc = datetime.now(UTC)
    valid_rows: list[dict[str, Any]] = []
    invalid_event_type_events = 0
    invalid_timestamp_events = 0
    invalid_value_events = 0
    future_events = 0
    for row in rows:
        if row["event_type"] not in EVENT_TYPES:
            invalid_event_type_events += 1
            continue
        try:
            occurred = _event_time(row["occurred_at"])
        except ValueError:
            invalid_timestamp_events += 1
            continue
        if occurred > now_utc + timedelta(minutes=5):
            future_events += 1
            continue
        item = dict(row)
        item["occurred"] = occurred
        if item["value"] is not None:
            try:
                numeric_value = float(item["value"])
            except (TypeError, ValueError):
                numeric_value = math.nan
            if not math.isfinite(numeric_value) or numeric_value < 0 or numeric_value > 1_000_000_000:
                invalid_value_events += 1
                item["value"] = None
            else:
                item["value"] = numeric_value
        valid_rows.append(item)
    valid_rows.sort(key=lambda item: (item["occurred"], item["event_id"]))

    stage_subjects: dict[str, set[str]] = {event_type: set() for event_type in EVENT_TYPES}
    engines: dict[str, dict[str, Any]] = {}
    for row in valid_rows:
        subject = row["anonymous_id_hash"] or f"event:{row['event_id']}"
        stage_subjects[row["event_type"]].add(subject)
        engine = engines.setdefault(row["source_engine"], {"events": 0, "won": 0, "value": 0.0})
        engine["events"] += 1
        engine["value"] += float(row["value"] or 0)
        if row["event_type"] == "won":
            engine["won"] += 1
    stages = {event_type: len(subjects) for event_type, subjects in stage_subjects.items()}
    referrals = {
        row["anonymous_id_hash"] for row in valid_rows
        if row["event_type"] == "ai_referral" and row["anonymous_id_hash"]
    }

    journeys: dict[str, list[dict[str, Any]]] = {}
    for row in valid_rows:
        if not row["anonymous_id_hash"]:
            continue
        journeys.setdefault(row["anonymous_id_hash"], []).append(row)

    anonymous_missing_events = sum(not row["anonymous_id_hash"] for row in rows)
    downstream_subjects = set().union(*(stage_subjects[name] for name in EVENT_TYPES[1:]))
    orphan_subjects = {
        subject for subject in downstream_subjects - referrals if not subject.startswith("event:")
    }
    duplicate_stage_events = sum(
        sum(
            max(0, count - 1)
            for stage, count in Counter(item["event_type"] for item in events).items()
            if stage != "ai_referral"
        )
        for events in journeys.values()
    )
    out_of_order_subjects = 0
    for events in journeys.values():
        highest_stage = -1
        regressed = False
        timestamps = sorted({item["occurred"] for item in events})
        for timestamp in timestamps:
            timestamp_stages = [
                STAGE_ORDER[item["event_type"]]
                for item in events
                if item["occurred"] == timestamp and item["event_type"] != "ai_referral"
            ]
            if timestamp_stages and min(timestamp_stages) < highest_stage:
                regressed = True
            if timestamp_stages:
                highest_stage = max(highest_stage, max(timestamp_stages))
        touches = [item for item in events if item["event_type"] == "ai_referral"]
        downstream = [item for item in events if item["event_type"] != "ai_referral"]
        if touches and downstream and any(item["occurred"] < touches[0]["occurred"] for item in downstream):
            regressed = True
        out_of_order_subjects += int(regressed)

    try:
        configured_lookback = int(settings.raw.get("attribution", {}).get("lookback_days", 90))
    except (TypeError, ValueError):
        configured_lookback = 90
    lookback_days = max(1, min(configured_lookback, 365))
    model_credit: dict[str, dict[str, dict[str, float | int]]] = {"first_touch": {}, "last_touch": {}}
    attributed_stage_subjects: dict[str, set[str]] = {event_type: set() for event_type in EVENT_TYPES}
    inquiry_delays: list[float] = []
    won_delays: list[float] = []
    for subject, events in journeys.items():
        touches = [item for item in events if item["event_type"] == "ai_referral"]
        if not touches:
            continue
        attributed_stage_subjects["ai_referral"].add(subject)
        first_touch = touches[0]
        for stage_name in EVENT_TYPES[1:]:
            if any(
                item["event_type"] == stage_name
                and any(
                    timedelta(0) <= item["occurred"] - touch["occurred"] <= timedelta(days=lookback_days)
                    for touch in touches
                )
                for item in events
            ):
                attributed_stage_subjects[stage_name].add(subject)
        for stage_name, delays in (("inquiry", inquiry_delays), ("won", won_delays)):
            stage_events = [
                item for item in events
                if item["event_type"] == stage_name
                and timedelta(0) <= item["occurred"] - first_touch["occurred"] <= timedelta(days=lookback_days)
            ]
            if stage_events:
                delays.append((stage_events[0]["occurred"] - first_touch["occurred"]).total_seconds() / 86400)
        wins = [item for item in events if item["event_type"] == "won"]
        for win in wins:
            eligible = [
                item for item in touches
                if timedelta(0) <= win["occurred"] - item["occurred"] <= timedelta(days=lookback_days)
            ]
            if not eligible:
                continue
            for model, touch in (("first_touch", eligible[0]), ("last_touch", eligible[-1])):
                engine = touch["source_engine"]
                credit = model_credit[model].setdefault(engine, {"won": 0, "value": 0.0})
                credit["won"] += 1
                credit["value"] += float(win["value"] or 0)

    def conversion(stage: str) -> float | None:
        return (
            round(len(attributed_stage_subjects[stage]) / len(referrals) * 100, 1)
            if referrals else None
        )

    if not rows:
        quality_status = "awaiting_integration"
    elif (
        anonymous_missing_events or orphan_subjects or duplicate_stage_events
        or invalid_event_type_events or invalid_timestamp_events or invalid_value_events
        or future_events or out_of_order_subjects
    ):
        quality_status = "needs_attention"
    elif not referrals:
        quality_status = "needs_attention"
    else:
        quality_status = "collecting"
    return {
        "events": len(rows),
        "valid_events": len(valid_rows),
        "unique_observed_subjects": len(set().union(*stage_subjects.values())),
        "unique_attributed_subjects": len(referrals),
        "funnel": stages,
        "engines": engines,
        "referral_to_signup_rate": conversion("signup"),
        "referral_to_inquiry_rate": conversion("inquiry"),
        "referral_to_won_rate": conversion("won"),
        "attributable_funnel": {
            stage: len(attributed_stage_subjects[stage]) for stage in EVENT_TYPES
        },
        "attribution_models": {
            "lookback_days": lookback_days,
            "first_touch": model_credit["first_touch"],
            "last_touch": model_credit["last_touch"],
            "note": "规则模型只分配可观测路径信用，不表示因果增量。",
        },
        "time_to_conversion_days": {
            "median_to_inquiry": round(statistics.median(inquiry_delays), 2) if inquiry_delays else None,
            "median_to_won": round(statistics.median(won_delays), 2) if won_delays else None,
        },
        "data_quality": {
            "status": quality_status,
            "anonymous_missing_events": anonymous_missing_events,
            "orphan_downstream_subjects": len(orphan_subjects),
            "duplicate_stage_events": duplicate_stage_events,
            "out_of_order_subjects": out_of_order_subjects,
            "invalid_event_type_events": invalid_event_type_events,
            "invalid_timestamp_events": invalid_timestamp_events,
            "invalid_value_events": invalid_value_events,
            "future_events": future_events,
        },
        "privacy": "仅设计匿名 UUID 哈希和白名单聚合字段；GEO 库不提供姓名、手机号、邮箱、微信号或聊天内容字段。",
        "measurement_note": "该归因反映可观测事件；首触与末触是规则模型，不等同于单一渠道的因果增量。",
    }
