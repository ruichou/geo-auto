from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import urlsplit


PROBE_PROVENANCE_SCHEMA_VERSION = "hongtu-geo-probe-v1"
STRATEGY_SCHEMA_VERSION = "hongtu-geo-strategy-v1"

_SECRET_MARKERS = ("api_key", "token", "secret", "password", "cookie", "credential")
_BROWSER_KEYS = (
    "kind", "login_url", "studio_url", "new_chat_url", "input_locators",
    "answer_locators", "new_chat_locators", "logged_out_locators",
)
_API_KEYS = ("kind", "base_url", "model")


def _safe_adapter_config(config: dict[str, Any], surface: str) -> dict[str, Any]:
    keys = _BROWSER_KEYS if surface == "browser" else _API_KEYS
    safe: dict[str, Any] = {}
    for key in keys:
        if any(marker in key.lower() for marker in _SECRET_MARKERS):
            continue
        value = config.get(key)
        if key.endswith("_url") or key == "base_url":
            parsed = urlsplit(str(value or ""))
            host = parsed.hostname or ""
            host = f"[{host}]" if ":" in host else host
            try:
                port = f":{parsed.port}" if parsed.port is not None else ""
            except ValueError:
                port = ""
            value = f"{parsed.scheme}://{host}{port}{parsed.path}" if parsed.scheme and host else ""
        if isinstance(value, (str, int, float, bool, list, tuple)) or value is None:
            safe[key] = value
    return safe


def adapter_config_fingerprint(config: dict[str, Any], surface: str) -> str:
    """Hash an allowlisted, credential-free adapter description."""
    canonical = json.dumps(
        _safe_adapter_config(config, surface), ensure_ascii=False,
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_probe_provenance(
    *, provider: str, surface: str, config: dict[str, Any], locale: str,
    region: str, extraction_version: str,
) -> dict[str, Any]:
    model = str(config.get("model", "")).strip() if surface != "browser" else ""
    return {
        "provenance_schema_version": PROBE_PROVENANCE_SCHEMA_VERSION,
        "extraction_version": extraction_version,
        "adapter_config_sha256": adapter_config_fingerprint(config, surface),
        "capture_method": "rendered_dom" if surface == "browser" else "provider_sdk",
        "account_context": "saved_session" if surface == "browser" else "api_credential",
        "model_identity": {
            "status": "configured" if model else "not_exposed",
            "value": model,
        },
        "provider": provider,
        "engine_surface": surface,
        "locale": locale,
        "region": region,
    }
