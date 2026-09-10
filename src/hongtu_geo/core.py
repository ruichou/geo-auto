from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit


ROOT = Path(__file__).resolve().parents[2]


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def slugify(value: str, fallback: str = "item") -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().lower()
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "-", normalized, flags=re.UNICODE)
    normalized = normalized.strip("-_")
    return normalized[:80] or fallback


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    root: Path
    raw: dict[str, Any]

    @property
    def brand(self) -> dict[str, Any]:
        return self.raw["brand"]

    @property
    def db_path(self) -> Path:
        return self.root / "data" / "geo.db"

    @property
    def site_url(self) -> str:
        return str(self.brand.get("site_url", "")).rstrip("/")

    @property
    def content_dir(self) -> Path:
        return self.root / self.raw["content"]["output_dir"]

    @classmethod
    def load(cls, root: Path | None = None, config_path: Path | None = None) -> "Settings":
        project_root = (root or ROOT).resolve()
        load_dotenv(project_root / ".env")
        path = config_path or project_root / "config" / "site.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        settings = cls(project_root, raw)
        settings.ensure_dirs()
        return settings

    def ensure_dirs(self) -> None:
        for path in (
            self.root / "data",
            self.root / "data" / "raw",
            self.root / "reports",
            self.content_dir,
            self.root / "content" / "review-needed",
        ):
            path.mkdir(parents=True, exist_ok=True)


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS pages (
  url TEXT PRIMARY KEY,
  status_code INTEGER,
  fetched_at TEXT NOT NULL,
  title TEXT,
  description TEXT,
  canonical TEXT,
  h1 TEXT,
  headings_json TEXT NOT NULL,
  text_content TEXT NOT NULL,
  word_count INTEGER NOT NULL,
  internal_links_json TEXT NOT NULL,
  external_links_json TEXT NOT NULL,
  jsonld_json TEXT NOT NULL,
  audit_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS opportunities (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  question TEXT UNIQUE NOT NULL,
  cluster TEXT NOT NULL,
  intent TEXT NOT NULL,
  funnel_stage TEXT NOT NULL,
  audience TEXT NOT NULL,
  business_value INTEGER NOT NULL,
  citation_potential INTEGER NOT NULL,
  freshness_need INTEGER NOT NULL,
  difficulty INTEGER NOT NULL,
  score REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'backlog',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  opportunity_id INTEGER NOT NULL,
  title TEXT NOT NULL,
  slug TEXT UNIQUE NOT NULL,
  body TEXT NOT NULL,
  sources_json TEXT NOT NULL,
  status TEXT NOT NULL,
  quality_score REAL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(opportunity_id) REFERENCES opportunities(id)
);
CREATE TABLE IF NOT EXISTS probes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider TEXT NOT NULL,
  question TEXT NOT NULL,
  answer TEXT NOT NULL,
  brand_mentioned INTEGER NOT NULL,
  domain_cited INTEGER NOT NULL,
  competitors_json TEXT NOT NULL,
  probed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS platform_accounts (
  platform TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'not_connected',
  profile_dir TEXT NOT NULL,
  last_checked_at TEXT,
  last_error TEXT,
  retry_after TEXT
);
CREATE TABLE IF NOT EXISTS publish_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  draft_id INTEGER NOT NULL,
  platform TEXT NOT NULL,
  scheduled_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  published_url TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(draft_id, platform),
  FOREIGN KEY(draft_id) REFERENCES drafts(id)
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate_run_uniqueness()
        self._migrate_geo_observability()

    def _migrate_run_uniqueness(self) -> None:
        duplicates = self.conn.execute(
            """SELECT kind,substr(started_at,1,10) day,MAX(id) keep_id
            FROM runs WHERE status IN ('running','ok')
            GROUP BY kind,substr(started_at,1,10) HAVING COUNT(*)>1"""
        ).fetchall()
        for row in duplicates:
            self.conn.execute(
                """UPDATE runs SET status='superseded',finished_at=COALESCE(finished_at,?),
                detail='{"migration":"superseded duplicate daily batch"}'
                WHERE kind=? AND substr(started_at,1,10)=? AND id<>? AND status IN ('running','ok')""",
                (now_iso(), row["kind"], row["day"], row["keep_id"]),
            )
        self.conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_one_successful_batch_per_day
            ON runs(kind, substr(started_at, 1, 10)) WHERE status IN ('running', 'ok')"""
        )
        self.conn.commit()

    def _migrate_geo_observability(self) -> None:
        """Add richer AEO/GEO measurements without invalidating existing data."""
        platform_existing = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(platform_accounts)").fetchall()
        }
        if "retry_after" not in platform_existing:
            self.conn.execute("ALTER TABLE platform_accounts ADD COLUMN retry_after TEXT")
        existing = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(probes)").fetchall()
        }
        additions = {
            "brand_rank": "INTEGER",
            "sentiment": "TEXT NOT NULL DEFAULT 'neutral'",
            "sentiment_score": "REAL NOT NULL DEFAULT 0",
            "recommended": "INTEGER NOT NULL DEFAULT 0",
            "citation_urls_json": "TEXT NOT NULL DEFAULT '[]'",
            "citation_domains_json": "TEXT NOT NULL DEFAULT '[]'",
            "visibility_score": "REAL NOT NULL DEFAULT 0",
            "prompt_variant": "TEXT NOT NULL DEFAULT 'original'",
            "prompt_version": "TEXT NOT NULL DEFAULT 'legacy'",
            "answer_hash": "TEXT NOT NULL DEFAULT ''",
            "visibility_state": "TEXT NOT NULL DEFAULT 'invisible'",
            "engine_surface": "TEXT NOT NULL DEFAULT 'unknown'",
            "locale": "TEXT NOT NULL DEFAULT 'zh-CN'",
            "region": "TEXT NOT NULL DEFAULT 'CN'",
            "sample_index": "INTEGER NOT NULL DEFAULT 1",
            "experiment_id": "TEXT NOT NULL DEFAULT ''",
            "citation_position": "INTEGER",
            "raw_metadata_json": "TEXT NOT NULL DEFAULT '{}'",
            "batch_item_id": "INTEGER",
        }
        for name, definition in additions.items():
            if name not in existing:
                self.conn.execute(f"ALTER TABLE probes ADD COLUMN {name} {definition}")
        for row in self.conn.execute(
            """SELECT id,raw_metadata_json FROM probes WHERE engine_surface='browser'
            AND (raw_metadata_json LIKE '%entry_origin_path%'
            OR raw_metadata_json LIKE '%conversation_url%')"""
        ).fetchall():
            try:
                metadata = json.loads(row["raw_metadata_json"] or "{}")
            except (json.JSONDecodeError, TypeError, ValueError, UnicodeError):
                continue
            if not isinstance(metadata, dict):
                continue
            changed = False
            had_legacy_entry = "entry_origin_path" in metadata
            legacy_entry = metadata.pop("entry_origin_path", None)
            if had_legacy_entry:
                changed = True
            if legacy_entry:
                parsed = urlsplit(str(legacy_entry))
                if parsed.scheme and parsed.netloc:
                    metadata["entry_origin"] = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
            had_full_conversation_url = "conversation_url" in metadata
            full_conversation_url = metadata.pop("conversation_url", None)
            if had_full_conversation_url:
                changed = True
            if full_conversation_url:
                metadata.setdefault(
                    "conversation_url_sha256",
                    hashlib.sha256(str(full_conversation_url).encode("utf-8")).hexdigest(),
                )
            if changed:
                self.conn.execute(
                    "UPDATE probes SET raw_metadata_json=? WHERE id=?",
                    (json.dumps(metadata, ensure_ascii=False), row["id"]),
                )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_probes_provider_question_time "
            "ON probes(provider, question, probed_at)"
        )
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_probes_batch_item_id "
            "ON probes(batch_item_id) WHERE batch_item_id IS NOT NULL"
        )
        self.conn.execute(
            """UPDATE probes SET visibility_state=CASE
            WHEN brand_mentioned=1 AND domain_cited=1 THEN 'full_visibility'
            WHEN brand_mentioned=1 THEN 'mention_only'
            WHEN domain_cited=1 THEN 'citation_only'
            ELSE 'invisible' END"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS geo_actions (
            action_key TEXT PRIMARY KEY,
            question TEXT NOT NULL,
            action_type TEXT NOT NULL,
            priority INTEGER NOT NULL,
            reason TEXT NOT NULL,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS entity_candidates (
            name TEXT PRIMARY KEY,
            entity_type TEXT NOT NULL DEFAULT 'unknown',
            sample_count INTEGER NOT NULL DEFAULT 0,
            confidence REAL NOT NULL DEFAULT 0,
            contexts_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'candidate',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
            )"""
        )
        entity_existing = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(entity_candidates)").fetchall()
        }
        entity_additions = {
            "independent_count": "INTEGER NOT NULL DEFAULT 0",
            "provider_count": "INTEGER NOT NULL DEFAULT 0",
            "surface_count": "INTEGER NOT NULL DEFAULT 0",
            "question_count": "INTEGER NOT NULL DEFAULT 0",
            "evidence_strength": "TEXT NOT NULL DEFAULT 'observed_once'",
        }
        for name, definition in entity_additions.items():
            if name not in entity_existing:
                self.conn.execute(f"ALTER TABLE entity_candidates ADD COLUMN {name} {definition}")
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS citation_sources (
            domain TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            sample_count INTEGER NOT NULL DEFAULT 0,
            independent_count INTEGER NOT NULL DEFAULT 0,
            provider_count INTEGER NOT NULL DEFAULT 0,
            surface_count INTEGER NOT NULL DEFAULT 0,
            question_count INTEGER NOT NULL DEFAULT 0,
            experiment_count INTEGER NOT NULL DEFAULT 0,
            evidence_strength TEXT NOT NULL DEFAULT 'observed_once',
            review_status TEXT NOT NULL DEFAULT 'observed',
            contexts_json TEXT NOT NULL DEFAULT '[]',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS citation_url_candidates (
            url_hash TEXT PRIMARY KEY,
            canonical_url TEXT NOT NULL,
            domain TEXT NOT NULL,
            category TEXT NOT NULL,
            sample_count INTEGER NOT NULL DEFAULT 0,
            independent_count INTEGER NOT NULL DEFAULT 0,
            provider_count INTEGER NOT NULL DEFAULT 0,
            surface_count INTEGER NOT NULL DEFAULT 0,
            question_count INTEGER NOT NULL DEFAULT 0,
            experiment_count INTEGER NOT NULL DEFAULT 0,
            evidence_strength TEXT NOT NULL DEFAULT 'observed_once',
            review_status TEXT NOT NULL DEFAULT 'observed',
            network_status TEXT NOT NULL DEFAULT 'not_checked',
            http_status INTEGER,
            content_type TEXT NOT NULL DEFAULT '',
            checked_at TEXT,
            last_error TEXT,
            contexts_json TEXT NOT NULL DEFAULT '[]',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
            )"""
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_citation_url_candidates_domain "
            "ON citation_url_candidates(domain)"
        )
        citation_existing = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(citation_sources)").fetchall()
        }
        if "surface_count" not in citation_existing:
            self.conn.execute(
                "ALTER TABLE citation_sources ADD COLUMN surface_count INTEGER NOT NULL DEFAULT 0"
            )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS attribution_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT UNIQUE NOT NULL,
            occurred_at TEXT NOT NULL,
            event_type TEXT NOT NULL,
            source_engine TEXT NOT NULL DEFAULT 'unknown',
            landing_url TEXT NOT NULL DEFAULT '',
            utm_source TEXT NOT NULL DEFAULT '',
            utm_medium TEXT NOT NULL DEFAULT '',
            utm_campaign TEXT NOT NULL DEFAULT '',
            anonymous_id_hash TEXT NOT NULL DEFAULT '',
            value REAL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
            )"""
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_attribution_source_time "
            "ON attribution_events(source_engine, occurred_at)"
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS browser_launch_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            occurred_at TEXT NOT NULL,
            finished_at TEXT,
            platform TEXT NOT NULL,
            action TEXT NOT NULL,
            trigger TEXT NOT NULL,
            visible INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'started',
            error TEXT
            )"""
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_browser_launch_events_time "
            "ON browser_launch_events(occurred_at)"
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS probe_batches (
            batch_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            planned_calls INTEGER NOT NULL DEFAULT 0,
            attempted_calls INTEGER NOT NULL DEFAULT 0,
            succeeded_calls INTEGER NOT NULL DEFAULT 0,
            failed_calls INTEGER NOT NULL DEFAULT 0,
            truncated_by_call_cap INTEGER NOT NULL DEFAULT 0,
            skipped_json TEXT NOT NULL DEFAULT '[]',
            errors_json TEXT NOT NULL DEFAULT '[]',
            config_json TEXT NOT NULL DEFAULT '{}'
            )"""
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_probe_batches_started_at ON probe_batches(started_at)"
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS probe_batch_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            question TEXT NOT NULL,
            prompt TEXT NOT NULL,
            sample_index INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'planned',
            attempts INTEGER NOT NULL DEFAULT 0,
            probe_id INTEGER,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(batch_id,provider,question,sample_index),
            FOREIGN KEY(batch_id) REFERENCES probe_batches(batch_id),
            FOREIGN KEY(probe_id) REFERENCES probes(id)
            )"""
        )
        batch_item_existing = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(probe_batch_items)")
        }
        if "engine_surface" not in batch_item_existing:
            self.conn.execute(
                "ALTER TABLE probe_batch_items ADD COLUMN engine_surface TEXT NOT NULL DEFAULT 'api'"
            )
        if "prompt_variant" not in batch_item_existing:
            self.conn.execute(
                "ALTER TABLE probe_batch_items ADD COLUMN prompt_variant TEXT NOT NULL DEFAULT 'legacy'"
            )
        if "prompt_version" not in batch_item_existing:
            self.conn.execute(
                "ALTER TABLE probe_batch_items ADD COLUMN prompt_version TEXT NOT NULL DEFAULT 'legacy'"
            )
        self.conn.execute(
            """UPDATE probe_batch_items SET
            prompt_variant=CASE
              WHEN trim(prompt)=trim(question) THEN 'naturalistic'
              WHEN trim(prompt)=trim(question || ' 请给出中立、可核查的中文答案，并尽量列出公开来源链接。')
                OR trim(prompt)=trim(question || ' 请给出中立、可核查的中文答案，并列出来源链接。不要因为问题中未出现某品牌就强行推荐。')
                THEN 'source_requested'
              ELSE 'legacy' END,
            prompt_version=CASE
              WHEN trim(prompt)=trim(question) THEN 'naturalistic-v1'
              WHEN trim(prompt)=trim(question || ' 请给出中立、可核查的中文答案，并尽量列出公开来源链接。')
                OR trim(prompt)=trim(question || ' 请给出中立、可核查的中文答案，并列出来源链接。不要因为问题中未出现某品牌就强行推荐。')
                THEN 'source-requested-v1'
              ELSE 'legacy' END"""
        )
        self.conn.execute(
            """UPDATE probes SET
            prompt_variant=(SELECT i.prompt_variant FROM probe_batch_items i WHERE i.id=probes.batch_item_id),
            prompt_version=(SELECT i.prompt_version FROM probe_batch_items i WHERE i.id=probes.batch_item_id)
            WHERE batch_item_id IS NOT NULL
            AND EXISTS (SELECT 1 FROM probe_batch_items i WHERE i.id=probes.batch_item_id)"""
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_probe_batch_items_resume "
            "ON probe_batch_items(batch_id,status,attempts,id)"
        )
        # Privacy migration: legacy AI answers may contain signed/query URLs. Keep only
        # their canonical query-free form before any row is exposed to reports or APIs.
        from .citation_verifier import canonicalize_citation_url, sanitize_citation_text

        for row in self.conn.execute(
            """SELECT id,answer,citation_urls_json,brand_mentioned,domain_cited,
            visibility_score FROM probes"""
        ).fetchall():
            original_answer = str(row["answer"] or "")
            safe_answer = sanitize_citation_text(original_answer)
            try:
                raw_urls = json.loads(row["citation_urls_json"] or "[]")
            except (json.JSONDecodeError, TypeError, ValueError, UnicodeError):
                raw_urls = []
            if not isinstance(raw_urls, list):
                raw_urls = []
            canonicalized_urls = [canonicalize_citation_url(item) for item in raw_urls]
            unsafe_url_removed = any(canonical is None for canonical in canonicalized_urls)
            unsafe_url_removed = unsafe_url_removed or (
                safe_answer.count("[已移除不安全链接]")
                > original_answer.count("[已移除不安全链接]")
            )
            safe_urls = list(dict.fromkeys(
                canonical for canonical in canonicalized_urls if canonical
            ))
            safe_urls_json = json.dumps(safe_urls, ensure_ascii=False)
            if safe_answer != row["answer"] or safe_urls_json != row["citation_urls_json"]:
                if unsafe_url_removed:
                    brand_mentioned = bool(row["brand_mentioned"])
                    visibility_score = max(
                        0.0,
                        float(row["visibility_score"] or 0) - (25.0 if row["domain_cited"] else 0.0),
                    )
                    self.conn.execute(
                        """UPDATE probes SET answer=?,citation_urls_json=?,answer_hash=?,
                        domain_cited=0,citation_position=NULL,visibility_score=?,visibility_state=?
                        WHERE id=?""",
                        (
                            safe_answer, safe_urls_json,
                            hashlib.sha256(safe_answer.encode("utf-8")).hexdigest(),
                            visibility_score, "mention_only" if brand_mentioned else "invisible",
                            row["id"],
                        ),
                    )
                else:
                    self.conn.execute(
                        "UPDATE probes SET answer=?,citation_urls_json=?,answer_hash=? WHERE id=?",
                        (
                            safe_answer,
                            safe_urls_json,
                            hashlib.sha256(safe_answer.encode("utf-8")).hexdigest(),
                            row["id"],
                        ),
                    )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, tuple(params))
        self.conn.commit()
        return cur

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        self.conn.executemany(sql, [tuple(row) for row in rows])
        self.conn.commit()

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, tuple(params)).fetchall())

    def start_run(self, kind: str) -> int:
        try:
            cur = self.execute(
                "INSERT INTO runs(kind, started_at, status) VALUES(?, ?, 'running')",
                (kind, now_iso()),
            )
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            return 0

    def finish_run(self, run_id: int, status: str, detail: dict[str, Any]) -> None:
        self.execute(
            "UPDATE runs SET finished_at=?, status=?, detail=? WHERE id=?",
            (now_iso(), status, json.dumps(detail, ensure_ascii=False), run_id),
        )

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
