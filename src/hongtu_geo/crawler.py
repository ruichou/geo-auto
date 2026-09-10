from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from collections import deque
from html.parser import HTMLParser
from typing import Any

from .core import Database, Settings, now_iso


class PageParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title = ""
        self.description = ""
        self.canonical = ""
        self.language = ""
        self.meta_robots = ""
        self.open_graph: dict[str, str] = {}
        self.headings: list[dict[str, str]] = []
        self.links: list[str] = []
        self.jsonld: list[Any] = []
        self.paragraphs: list[str] = []
        self._text: list[str] = []
        self._capture: str | None = None
        self._capture_buf: list[str] = []
        self._ignore_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {k.lower(): (v or "") for k, v in attrs}
        tag = tag.lower()
        if tag == "html":
            self.language = values.get("lang", "").strip()
        if tag in {"script", "style", "noscript", "svg"}:
            if tag == "script" and values.get("type", "").lower() == "application/ld+json":
                self._capture = "jsonld"
                self._capture_buf = []
            else:
                self._ignore_depth += 1
            return
        if tag == "title" or tag == "p" or re.fullmatch(r"h[1-6]", tag):
            self._capture = tag
            self._capture_buf = []
        elif tag == "meta" and values.get("name", "").lower() == "description":
            self.description = values.get("content", "").strip()
        elif tag == "meta" and values.get("name", "").lower() == "robots":
            self.meta_robots = values.get("content", "").strip().lower()
        elif tag == "meta" and values.get("property", "").lower().startswith("og:"):
            self.open_graph[values["property"].lower()] = values.get("content", "").strip()
        elif tag == "link" and "canonical" in values.get("rel", "").lower():
            self.canonical = urllib.parse.urljoin(self.base_url, values.get("href", ""))
        elif tag == "a" and values.get("href"):
            absolute = urllib.parse.urljoin(self.base_url, values["href"])
            parts = urllib.parse.urlsplit(absolute)
            if parts.scheme in {"http", "https"}:
                self.links.append(urllib.parse.urlunsplit(parts._replace(fragment="")))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        capture_ends = self._capture == tag or (self._capture == "jsonld" and tag == "script")
        if capture_ends:
            value = " ".join(" ".join(self._capture_buf).split()).strip()
            if tag == "title":
                self.title = value
            elif re.fullmatch(r"h[1-6]", tag):
                self.headings.append({"level": tag, "text": value})
            elif tag == "p" and value:
                self.paragraphs.append(value)
            elif self._capture == "jsonld" and value:
                try:
                    self.jsonld.append(json.loads(value))
                except json.JSONDecodeError:
                    self.jsonld.append({"_invalid": value[:500]})
            self._capture = None
            self._capture_buf = []
        if tag in {"script", "style", "noscript", "svg"} and self._ignore_depth:
            self._ignore_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._capture_buf.append(data)
        if not self._ignore_depth and self._capture != "jsonld":
            value = " ".join(data.split()).strip()
            if value:
                self._text.append(value)

    @property
    def text(self) -> str:
        return "\n".join(self._text)


def _fetch(url: str, settings: Settings) -> tuple[int, str, str]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": settings.raw["crawl"]["user_agent"],
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.raw["crawl"]["timeout_seconds"]) as response:
            content_type = response.headers.get_content_type()
            charset = response.headers.get_content_charset() or "utf-8"
            body = response.read().decode(charset, errors="replace")
            return int(response.status), content_type, body
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.headers.get_content_type(), ""


def audit_page(parser: PageParser, status_code: int, domain: str) -> dict[str, Any]:
    text = parser.text
    lower = text.lower()
    jsonld_types = json.dumps(parser.jsonld, ensure_ascii=False)
    external = [u for u in parser.links if urllib.parse.urlsplit(u).netloc != domain]
    checks = {
        "http_ok": status_code == 200,
        "has_title": 8 <= len(parser.title) <= 70,
        "has_description": 40 <= len(parser.description) <= 180,
        "single_h1": sum(h["level"] == "h1" for h in parser.headings) == 1,
        "has_canonical": bool(parser.canonical),
        "indexable": "noindex" not in parser.meta_robots,
        "has_language": bool(parser.language),
        "substantial_content": len(text) >= 1200,
        "has_structured_data": bool(parser.jsonld),
        "has_org_or_article_schema": any(x in jsonld_types for x in ("Organization", "Article", "FAQPage")),
        "has_author_or_editor": any(x in lower for x in ("作者", "编辑", "审核", "专家")),
        "has_update_date": bool(re.search(r"20\d{2}[-年/.]\d{1,2}", text)),
        "has_evidence_language": any(x in text for x in ("数据来源", "参考资料", "依据", "调研", "报告")),
        "has_external_sources": bool(external),
        "has_faq": any(x in text for x in ("常见问题", "FAQ", "问答")),
        "has_conversion": any(x in text for x in ("立即咨询", "免费试用", "联系我们", "获取方案", "注册")),
        "answer_first": bool(parser.paragraphs and 30 <= len(parser.paragraphs[0]) <= 500),
        "question_headings": any(h["text"].endswith(("？", "?")) for h in parser.headings),
        "chunk_friendly": bool(parser.paragraphs) and max(map(len, parser.paragraphs), default=0) <= 900,
        "has_open_graph": "og:title" in parser.open_graph and "og:description" in parser.open_graph,
    }
    weights = {
        "http_ok": 5, "has_title": 5, "has_description": 4, "single_h1": 4,
        "has_canonical": 5, "indexable": 6, "has_language": 3,
        "substantial_content": 8, "has_structured_data": 7,
        "has_org_or_article_schema": 6, "has_author_or_editor": 5, "has_update_date": 5,
        "has_evidence_language": 7, "has_external_sources": 6, "has_faq": 6,
        "has_conversion": 5, "answer_first": 6, "question_headings": 4,
        "chunk_friendly": 4, "has_open_graph": 3,
    }
    score = sum(weights[key] for key, passed in checks.items() if passed)
    failed = [key for key, passed in checks.items() if not passed]
    return {"score": score, "checks": checks, "failed": failed}


def audit_site_resources(settings: Settings) -> dict[str, Any]:
    """Audit site-level discovery assets; llms.txt is treated as optional/emerging."""
    if not settings.site_url:
        return {"status": "skipped", "reason": "official site unavailable"}
    base = settings.site_url.rstrip("/")
    targets = {
        "robots": f"{base}/robots.txt",
        "sitemap": f"{base}/sitemap.xml",
        "llms": f"{base}/llms.txt",
    }
    fetched: dict[str, dict[str, Any]] = {}
    for name, url in targets.items():
        try:
            status, content_type, body = _fetch(url, settings)
            fetched[name] = {
                "url": url,
                "status": status,
                "content_type": content_type,
                "body": body[:200_000],
            }
        except Exception as exc:
            fetched[name] = {"url": url, "status": 0, "error": str(exc), "body": ""}
    robots = fetched["robots"]["body"]
    sitemap = fetched["sitemap"]["body"]
    llms = fetched["llms"]["body"]
    ai_agents = ["OAI-SearchBot", "ChatGPT-User", "PerplexityBot", "ClaudeBot", "Google-Extended"]
    checks = {
        "robots_available": fetched["robots"]["status"] == 200,
        "sitemap_available": fetched["sitemap"]["status"] == 200 and "<urlset" in sitemap,
        "sitemap_declared": "sitemap:" in robots.lower(),
        "llms_available": fetched["llms"]["status"] == 200,
        "llms_has_h1": bool(re.search(r"(?m)^#\s+\S", llms)),
        "llms_has_links": bool(re.search(r"(?m)^-\s+\[[^]]+\]\(https?://", llms)),
    }
    return {
        "status": "ok",
        "checks": checks,
        "score": round(sum(checks.values()) / len(checks) * 100, 1),
        "ai_agents_explicitly_named": [agent for agent in ai_agents if agent.lower() in robots.lower()],
        "note": "llms.txt is an emerging navigation convention, not a crawl-control or ranking guarantee.",
        "resources": {name: {k: v for k, v in item.items() if k != "body"} for name, item in fetched.items()},
    }


def crawl_site(settings: Settings, db: Database) -> dict[str, Any]:
    if not settings.site_url:
        return {"status": "skipped", "reason": "config/site.json 中尚未填写 brand.site_url", "pages": 0}
    base = urllib.parse.urlsplit(settings.site_url)
    domain = base.netloc
    robots_url = urllib.parse.urlunsplit((base.scheme, domain, "/robots.txt", "", ""))
    robot = urllib.robotparser.RobotFileParser(robots_url)
    try:
        robot.read()
    except Exception:
        robot = None
    queue: deque[str] = deque([settings.site_url])
    seen: set[str] = set()
    errors: list[dict[str, str]] = []
    max_pages = int(settings.raw["crawl"]["max_pages"])
    excludes = tuple(settings.raw["crawl"].get("exclude_paths", []))
    includes = tuple(settings.raw["crawl"].get("include_paths", []))
    while queue and len(seen) < max_pages:
        url = queue.popleft()
        if url in seen:
            continue
        parsed_url = urllib.parse.urlsplit(url)
        if parsed_url.netloc != domain or parsed_url.path.startswith(excludes):
            continue
        if includes and not parsed_url.path.startswith(includes):
            continue
        if robot and not robot.can_fetch(settings.raw["crawl"]["user_agent"], url):
            continue
        seen.add(url)
        try:
            status, content_type, body = _fetch(url, settings)
            if content_type not in {"text/html", "application/xhtml+xml"}:
                continue
            parser = PageParser(url)
            parser.feed(body)
            internal = sorted({u for u in parser.links if urllib.parse.urlsplit(u).netloc == domain})
            external = sorted({u for u in parser.links if urllib.parse.urlsplit(u).netloc != domain})
            for link in internal:
                if link not in seen:
                    queue.append(link)
            audit = audit_page(parser, status, domain)
            db.execute(
                """INSERT INTO pages(url,status_code,fetched_at,title,description,canonical,h1,
                headings_json,text_content,word_count,internal_links_json,external_links_json,jsonld_json,audit_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(url) DO UPDATE SET status_code=excluded.status_code,fetched_at=excluded.fetched_at,
                title=excluded.title,description=excluded.description,canonical=excluded.canonical,h1=excluded.h1,
                headings_json=excluded.headings_json,text_content=excluded.text_content,word_count=excluded.word_count,
                internal_links_json=excluded.internal_links_json,external_links_json=excluded.external_links_json,
                jsonld_json=excluded.jsonld_json,audit_json=excluded.audit_json""",
                (
                    url, status, now_iso(), parser.title, parser.description, parser.canonical,
                    next((h["text"] for h in parser.headings if h["level"] == "h1"), ""),
                    json.dumps(parser.headings, ensure_ascii=False), parser.text, len(parser.text),
                    json.dumps(internal, ensure_ascii=False), json.dumps(external, ensure_ascii=False),
                    json.dumps(parser.jsonld, ensure_ascii=False), json.dumps(audit, ensure_ascii=False),
                ),
            )
        except Exception as exc:
            errors.append({"url": url, "error": str(exc)})
    rows = db.query("SELECT audit_json FROM pages")
    scores = [json.loads(row["audit_json"]).get("score", 0) for row in rows]
    return {
        "status": "ok",
        "pages": len(rows),
        "average_geo_score": round(sum(scores) / len(scores), 1) if scores else 0,
        "site_resources": audit_site_resources(settings),
        "errors": errors[:20],
    }
