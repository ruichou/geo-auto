from __future__ import annotations

import ipaddress
import http.client
import re
import socket
import ssl
from typing import Any, Callable
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from .core import Settings, now_iso


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".home", ".lan")


def canonicalize_citation_url(raw: Any) -> str | None:
    """Return a query-free public-web candidate URL without performing network I/O."""
    value = str(raw or "").strip()
    if not value or _CONTROL.search(value) or "\\" in value:
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            return None
        if parts.username is not None or parts.password is not None:
            return None
        port = parts.port
        if port is not None and port != (443 if parts.scheme.lower() == "https" else 80):
            return None
        host = parts.hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return None
    if not host or "." not in host or host == "localhost" or host.endswith(_BLOCKED_SUFFIXES):
        return None
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        return None
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc += f":{port}"
    try:
        path = quote(unquote(parts.path or "/"), safe="/%:@!$&'()*+,;=-._~")
    except (UnicodeDecodeError, ValueError):
        return None
    return urlunsplit((parts.scheme.lower(), netloc, path, "", ""))


_TEXT_URL = re.compile(r"https?://[^\s)\]}>，。；、]+", re.IGNORECASE)


def sanitize_citation_text(value: str) -> str:
    """Remove URL secrets from stored answer text and discard unsafe URL targets."""
    def replace(match: re.Match[str]) -> str:
        canonical = canonicalize_citation_url(match.group(0))
        return canonical or "[已移除不安全链接]"

    return _TEXT_URL.sub(replace, value)


def citation_allowed_domains(settings: Settings) -> set[str]:
    configured = settings.raw.get("citation_verification", {}).get("allowed_domains", [])
    domains = {str(item).lower().removeprefix("www.").rstrip(".") for item in configured}
    for source in settings.raw.get("research_sources", []):
        host = urlsplit(str(source.get("url", ""))).hostname
        if host:
            domains.add(host.lower().removeprefix("www.").rstrip("."))
    for competitor in settings.raw.get("monitor", {}).get("competitors", []):
        if isinstance(competitor, dict) and competitor.get("domain"):
            domains.add(str(competitor["domain"]).lower().removeprefix("www.").rstrip("."))
    return {domain for domain in domains if domain and "." in domain}


def domain_is_allowlisted(domain: str, allowed_domains: set[str]) -> bool:
    normalized = domain.lower().removeprefix("www.").rstrip(".")
    return any(normalized == allowed or normalized.endswith("." + allowed) for allowed in allowed_domains)


def validate_public_destination(
    url: str,
    resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
) -> dict[str, Any]:
    canonical = canonicalize_citation_url(url)
    if not canonical:
        return {"safe": False, "reason": "invalid_url", "addresses": []}
    host = urlsplit(canonical).hostname or ""
    try:
        records = resolver(host, None, type=socket.SOCK_STREAM)
        addresses = sorted({str(record[4][0]).split("%")[0] for record in records})
    except (OSError, socket.gaierror) as exc:
        return {"safe": False, "reason": f"dns_error:{type(exc).__name__}", "addresses": []}
    if not addresses:
        return {"safe": False, "reason": "dns_no_addresses", "addresses": []}
    try:
        non_public = [address for address in addresses if not ipaddress.ip_address(address).is_global]
    except ValueError:
        return {"safe": False, "reason": "dns_invalid_address", "addresses": addresses}
    if non_public:
        return {"safe": False, "reason": "dns_non_public_address", "addresses": addresses}
    return {"safe": True, "reason": "public_destination", "addresses": addresses}


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, hostname: str, address: str, port: int, timeout: int) -> None:
        super().__init__(hostname, port=port, timeout=timeout)
        self._pinned_address = address

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._pinned_address, self.port), self.timeout, self.source_address
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, address: str, port: int, timeout: int) -> None:
        super().__init__(hostname, port=port, timeout=timeout, context=ssl.create_default_context())
        self._pinned_address = address

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._pinned_address, self.port), self.timeout, self.source_address
        )
        self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)


def verify_citation_url(
    url: str,
    settings: Settings,
    resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
) -> dict[str, Any]:
    """Verify one allowlisted URL over a DNS-validated, IP-pinned connection."""
    canonical = canonicalize_citation_url(url)
    checked_at = now_iso()
    if not canonical:
        return {"network_status": "rejected", "checked_at": checked_at, "last_error": "invalid_url"}
    host = (urlsplit(canonical).hostname or "").lower().removeprefix("www.")
    if not domain_is_allowlisted(host, citation_allowed_domains(settings)):
        return {"network_status": "not_allowlisted", "checked_at": checked_at, "last_error": "domain_not_allowlisted"}
    destination = validate_public_destination(canonical, resolver=resolver)
    if not destination["safe"]:
        return {"network_status": "rejected", "checked_at": checked_at, "last_error": destination["reason"]}
    config = settings.raw.get("citation_verification", {})
    timeout = max(1, min(int(config.get("timeout_seconds", 8)), 20))
    max_bytes = max(1024, min(int(config.get("max_bytes", 65536)), 262144))
    parts = urlsplit(canonical)
    path = parts.path or "/"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    last_error = "no_connection_attempt"
    for address in destination["addresses"]:
        connection: http.client.HTTPConnection | None = None
        try:
            connection_class = _PinnedHTTPSConnection if parts.scheme == "https" else _PinnedHTTPConnection
            connection = connection_class(host, address, port, timeout)
            connection.request(
                "GET", path,
                headers={
                    "User-Agent": "HongtuGEOCitationVerifier/1.0",
                    "Range": f"bytes=0-{max_bytes - 1}",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            status = int(response.status)
            content_type = str(response.getheader("Content-Type", ""))[:200]
            response.read(max_bytes + 1)
            return {
                "network_status": (
                    "verified" if 200 <= status < 300
                    else "redirect_blocked" if 300 <= status < 400
                    else "http_error"
                ),
                "http_status": status,
                "content_type": content_type,
                "checked_at": checked_at,
                "last_error": None if 200 <= status < 300 else f"http_status_{status}",
            }
        except (OSError, TimeoutError, ssl.SSLError, http.client.HTTPException) as exc:
            last_error = type(exc).__name__
        finally:
            if connection is not None:
                connection.close()
    return {"network_status": "network_error", "checked_at": checked_at, "last_error": last_error}
