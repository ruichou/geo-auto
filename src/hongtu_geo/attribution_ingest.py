from __future__ import annotations

import hashlib
import hmac
import os
import re
import time
from pathlib import Path
from typing import Any, Mapping

from .core import Settings


TIMESTAMP_HEADER = "x-hongtu-timestamp"
SIGNATURE_HEADER = "x-hongtu-signature"
SECRET_ENV = "HONGTU_ATTRIBUTION_INGEST_SECRET"


class AttributionIngestConfigurationError(RuntimeError):
    """Raised when signed ingestion is required but cannot be verified safely."""


def _auth_config(settings: Settings) -> dict[str, Any]:
    value = settings.raw.get("attribution", {})
    return value if isinstance(value, dict) else {}


def attribution_ingest_auth_required(settings: Settings) -> bool:
    return bool(_auth_config(settings).get("ingest_auth_required", True))


def attribution_ingest_secret() -> str:
    return os.getenv(SECRET_ENV, "").strip()


def _max_clock_skew(settings: Settings) -> int:
    try:
        configured = int(_auth_config(settings).get("max_clock_skew_seconds", 300))
    except (TypeError, ValueError):
        configured = 300
    return max(30, min(configured, 900))


def sign_attribution_payload(body: bytes, timestamp: str, secret: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        timestamp.encode("ascii") + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    return f"sha256={digest}"


def verify_attribution_signature(
    settings: Settings,
    body: bytes,
    headers: Mapping[str, str],
    *,
    now: float | None = None,
) -> None:
    if not attribution_ingest_auth_required(settings):
        return
    if len(body) > 65_536:
        raise ValueError("归因请求签名无效")
    secret = attribution_ingest_secret()
    if len(secret.encode("utf-8")) < 32:
        raise AttributionIngestConfigurationError(
            f"{SECRET_ENV} 未配置或长度不足 32 字节"
        )
    normalized = {str(key).lower(): str(value).strip() for key, value in headers.items()}
    timestamp = normalized.get(TIMESTAMP_HEADER, "")
    signature = normalized.get(SIGNATURE_HEADER, "")
    if not re.fullmatch(r"[0-9]{1,12}", timestamp):
        raise ValueError("归因请求签名无效")
    try:
        timestamp_number = int(timestamp)
    except (TypeError, ValueError) as exc:
        raise ValueError("归因请求签名无效") from exc
    max_skew = _max_clock_skew(settings)
    if abs((time.time() if now is None else now) - timestamp_number) > max_skew:
        raise ValueError("归因请求签名无效")
    if not signature.startswith("sha256=") or len(signature) != 71:
        raise ValueError("归因请求签名无效")
    expected = sign_attribution_payload(body, timestamp, secret)
    if not hmac.compare_digest(signature.lower(), expected):
        raise ValueError("归因请求签名无效")


def build_attribution_ingest_readiness(settings: Settings) -> dict[str, Any]:
    config = _auth_config(settings)
    required = attribution_ingest_auth_required(settings)
    secret = attribution_ingest_secret()
    secret_ready = len(secret.encode("utf-8")) >= 32
    contract_relative = str(
        config.get("integration_contract", "docs/newhongtu-attribution-integration.md")
    ).strip()
    contract_path = (settings.root / contract_relative).resolve()
    try:
        contract_inside_root = contract_path.is_relative_to(settings.root.resolve())
    except ValueError:
        contract_inside_root = False
    contract_ready = contract_inside_root and contract_path.is_file()
    if required and not secret_ready:
        status = "needs_secret"
    elif not contract_ready:
        status = "needs_contract"
    else:
        status = "awaiting_product_integration"
    return {
        "status": status,
        "auth_required": required,
        "signature_algorithm": "HMAC-SHA256(timestamp.raw_body)",
        "max_clock_skew_seconds": _max_clock_skew(settings),
        "secret_env": SECRET_ENV,
        "secret_present": bool(secret),
        "secret_strong_enough": secret_ready,
        "contract_path": contract_relative,
        "contract_ready": contract_ready,
        "endpoint": "/api/attribution/events",
        "next_action": (
            f"设置至少 32 字节的 {SECRET_ENV}，并在 NewHongTU 安全保存同一密钥"
            if required and not secret_ready
            else "按契约在 NewHongTU 增加事务型 outbox 与异步投递；GEO 端尚未收到真实事件"
        ),
    }
