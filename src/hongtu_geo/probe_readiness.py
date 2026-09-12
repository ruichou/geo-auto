from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from .core import Database, Settings


def _valid_web_url(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = urlsplit(value.strip())
        parsed.port
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def _minimum_provider_target(settings: Settings) -> tuple[int, str | None]:
    raw = settings.raw.get("monitor", {}).get("minimum_primary_providers", 2)
    try:
        if isinstance(raw, bool):
            raise ValueError
        value = int(raw)
        if value < 1:
            raise ValueError
        return value, None
    except (TypeError, ValueError):
        return 2, "monitor.minimum_primary_providers 必须是大于等于 1 的整数"


def _retry_state(status: str, retry_after: Any, now: datetime) -> str | None:
    if status != "temporarily_unavailable":
        return None
    if not isinstance(retry_after, str) or not retry_after.strip():
        return "unknown"
    try:
        value = retry_after.strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        parsed = parsed.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        return "invalid"
    return "retry_due" if parsed <= now else "restriction_active"


def validate_ai_probe_spec(platform: str, spec: dict[str, Any]) -> list[str]:
    """Validate the declarative contract needed by the generic browser probe runner."""
    errors: list[str] = []
    if spec.get("kind") != "ai_probe":
        errors.append("kind 必须为 ai_probe")
    if not isinstance(spec.get("name"), str) or not spec["name"].strip():
        errors.append("缺少平台名称")
    for field in ("login_url", "studio_url"):
        if not _valid_web_url(spec.get(field)):
            errors.append(f"{field} 必须是完整 http(s) 地址")
    for field in ("logged_in_url_contains", "input_locators", "answer_locators"):
        values = spec.get(field)
        if not isinstance(values, list) or not any(isinstance(item, str) and item.strip() for item in values):
            errors.append(f"{field} 必须包含至少一个非空字符串")
    if not _valid_web_url(spec.get("new_chat_url")) and not (
        isinstance(spec.get("new_chat_locators"), list)
        and any(isinstance(item, str) and item.strip() for item in spec["new_chat_locators"])
    ):
        errors.append("必须配置 new_chat_url 或 new_chat_locators 以隔离会话")
    return [f"{platform}: {error}" for error in errors]


def build_ai_probe_readiness(settings: Settings, db: Database) -> dict[str, Any]:
    """Report configuration and declared login readiness without opening a browser."""
    config_path = settings.root / "config" / "platforms.json"
    minimum, target_error = _minimum_provider_target(settings)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        return {
            "status": "invalid_configuration",
            "minimum_providers": minimum,
            "configured_providers": 0,
            "valid_adapters": 0,
            "declared_connected": 0,
            "ready_for_attempt": 0,
            "provider_deficit": minimum,
            "retry_due": 0,
            "active_restrictions": 0,
            "invalid_retry_timestamps": 0,
            "providers": [],
            "errors": [
                *([target_error] if target_error else []),
                f"无法读取平台配置：{type(exc).__name__}",
            ],
            "method_note": "只读就绪诊断不会启动浏览器；connected 是本地保存状态，不等于本次已实时验证。",
        }
    if not isinstance(raw, dict):
        return {
            "status": "invalid_configuration",
            "minimum_providers": minimum,
            "configured_providers": 0,
            "valid_adapters": 0,
            "declared_connected": 0,
            "ready_for_attempt": 0,
            "provider_deficit": minimum,
            "retry_due": 0,
            "active_restrictions": 0,
            "invalid_retry_timestamps": 0,
            "providers": [],
            "errors": [
                *([target_error] if target_error else []),
                "platforms.json 顶层必须是 JSON 对象",
            ],
            "method_note": "只读就绪诊断不会启动浏览器；connected 是本地保存状态，不等于本次已实时验证。",
        }
    account_rows = {
        str(row["platform"]): row
        for row in db.query(
            "SELECT platform,status,last_checked_at,last_error,retry_after FROM platform_accounts"
        )
    }
    providers: list[dict[str, Any]] = []
    errors: list[str] = [target_error] if target_error else []
    now = datetime.now(UTC)
    for platform, value in sorted(raw.items()):
        if isinstance(value, dict) and "kind" in value and value.get("kind") not in {"ai_probe", "publisher"}:
            errors.append(f"{platform}: 未知 kind={value.get('kind')!r}")
        if not isinstance(value, dict) or value.get("kind") != "ai_probe":
            continue
        spec_errors = validate_ai_probe_spec(platform, value)
        errors.extend(spec_errors)
        account = account_rows.get(platform)
        account_status = str(account["status"]) if account else "not_initialized"
        valid = not spec_errors
        declared_connected = account_status == "connected"
        retry_state = _retry_state(
            account_status, account["retry_after"] if account else None, now
        )
        retry_due = retry_state == "retry_due"
        ready_for_attempt = valid and (declared_connected or retry_due)
        providers.append({
            "provider": platform,
            "name": str(value.get("name", platform)),
            "adapter_valid": valid,
            "account_status": account_status,
            "effective_account_status": "retry_due" if retry_due else account_status,
            "declared_connected": declared_connected,
            "ready_for_attempt": ready_for_attempt,
            "retry_state": retry_state,
            "last_checked_at": account["last_checked_at"] if account else None,
            "last_error_present": bool(account and account["last_error"]),
            "retry_after": account["retry_after"] if account else None,
            "configuration_errors": spec_errors,
        })
    valid_count = sum(item["adapter_valid"] for item in providers)
    connected_count = sum(item["declared_connected"] for item in providers)
    ready_count = sum(item["ready_for_attempt"] for item in providers)
    retry_due_count = sum(item["retry_state"] == "retry_due" for item in providers)
    active_restrictions = sum(
        item["retry_state"] == "restriction_active" for item in providers
    )
    invalid_retry_timestamps = sum(
        item["retry_state"] in {"invalid", "unknown"} for item in providers
    )
    if errors:
        status = "invalid_configuration"
    elif ready_count >= minimum:
        status = "ready"
    elif ready_count:
        status = "insufficient_connected_engines"
    else:
        status = "no_connected_engines"
    return {
        "status": status,
        "minimum_providers": minimum,
        "configured_providers": len(providers),
        "valid_adapters": valid_count,
        "declared_connected": connected_count,
        "ready_for_attempt": ready_count,
        "provider_deficit": max(0, minimum - ready_count),
        "retry_due": retry_due_count,
        "active_restrictions": active_restrictions,
        "invalid_retry_timestamps": invalid_retry_timestamps,
        "providers": providers,
        "errors": errors,
        "method_note": (
            "只读就绪诊断不会启动浏览器或修改账号状态；connected 是本地保存状态。"
            "临时限制到期后仅标记为 retry_due，表示后台流程可以尝试恢复；"
            "只有成功采样才构成测量证据。"
        ),
    }
