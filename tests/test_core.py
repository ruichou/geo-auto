import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import hongtu_geo.browser as browser_module
import hongtu_geo.app as app_module
import hongtu_geo.citation_verifier as citation_verifier_module
import hongtu_geo.pipeline as pipeline_module
from fastapi.testclient import TestClient

from hongtu_geo.attribution import build_attribution_report, record_attribution_event
from hongtu_geo.attribution_ingest import (
    AttributionIngestConfigurationError,
    build_attribution_ingest_readiness,
    sign_attribution_payload,
    verify_attribution_signature,
)
from hongtu_geo.citation_verifier import (
    canonicalize_citation_url,
    domain_is_allowlisted,
    validate_public_destination,
    verify_citation_url,
)
from hongtu_geo.core import Database, Settings, now_iso, slugify
from hongtu_geo.evidence import audit_brand_facts
from hongtu_geo.browser import (
    _confirm_publish,
    _capture_browser_answer,
    _browser_batch_matches_request,
    _browser_probe_prompt,
    _build_browser_probe_plan,
    _markdown_to_safe_html,
    _looks_logged_in,
    _next_browser_batch_item_ids,
    _platform_body,
    _prepare_fresh_ai_conversation,
    _parse_ai_temporary_restriction,
    _select_browser_probe_questions,
    _tracked_conversion_url,
    profile_path,
    run_browser_probes,
    run_due_jobs,
)
from hongtu_geo.crawler import PageParser, audit_page
from hongtu_geo.geo_engine import (
    analyze_answer,
    analyze_brand_answer_integrity,
    build_answer_integrity_snapshot,
    build_citation_intelligence,
    build_citation_url_ledger,
    build_context_isolation_health,
    build_cross_run_reproducibility,
    build_gap_analysis,
    build_maturity_audit,
    build_prompt_benchmark,
    build_probe_provenance_health,
    build_sampling_health,
    build_visibility_snapshot,
    build_visibility_trends,
    classify_citation_domain,
    discover_entity_candidates,
    record_probe,
)
from hongtu_geo.pipeline import (
    _acquisition_question_templates,
    _host_matches,
    _llm_draft,
    _mock_draft,
    _verified_lead_identity,
    build_entity_assets,
    quality_check,
    reconcile_stale_probe_batches,
    review_drafts,
    resume_probe_batch,
    run_automation_pipeline,
    run_probes,
    schedule_approved_drafts,
    seed_opportunities,
)
from hongtu_geo.providers import ProviderAnswer
from hongtu_geo.probe_readiness import build_ai_probe_readiness, validate_ai_probe_spec
from hongtu_geo.provenance import adapter_config_fingerprint, build_probe_provenance
from hongtu_geo.browser_activity import (
    begin_browser_launch, build_browser_activity_health, finish_browser_launch,
)


ANON_A = "00000000-0000-4000-8000-000000000001"
ANON_B = "00000000-0000-4000-8000-000000000002"
ANON_C = "00000000-0000-4000-8000-000000000003"
from hongtu_geo.social_assets import generate_social_cards


def make_settings(tmp_path: Path, verified: bool = False) -> Settings:
    raw = {
        "brand": {
            "name": "宏图商机", "aliases": [], "site_url": "https://example.com",
            "audiences": ["企业负责人"], "facts": [{"claim": "事实", "source": "https://example.com", "verified": verified}],
        },
        "crawl": {"max_pages": 5, "timeout_seconds": 2, "user_agent": "test", "exclude_paths": [], "include_paths": []},
        "content": {
            "daily_drafts": 2,
            "output_dir": "content/publish-queue",
            "model": "test",
            "minimum_chars_zh": 1000,
        },
        "monitor": {"daily_probe_count": 3, "providers": []},
        "publishing": {"mode": "queue"},
    }
    return Settings(tmp_path, raw)


def test_slugify_keeps_chinese() -> None:
    assert slugify("  企业 商机 / 指南  ") == "企业-商机-指南"


def test_probe_provenance_fingerprint_is_stable_and_credential_free() -> None:
    first = {
        "kind": "api", "model": "m1", "base_url": "https://api.example/v1",
        "api_key": "super-secret", "token": "also-secret",
    }
    reordered = {
        "token": "different", "base_url": "https://api.example/v1",
        "model": "m1", "kind": "api", "api_key": "different",
    }
    assert adapter_config_fingerprint(first, "api") == adapter_config_fingerprint(reordered, "api")
    assert adapter_config_fingerprint(first, "api") != adapter_config_fingerprint(
        {**first, "model": "m2"}, "api"
    )
    assert adapter_config_fingerprint(first, "api") != adapter_config_fingerprint(
        {**first, "base_url": "https://api.example/v2"}, "api"
    )
    assert adapter_config_fingerprint(first, "api") == adapter_config_fingerprint(
        {**first, "base_url": "https://user:password@api.example/v1"}, "api"
    )
    receipt = build_probe_provenance(
        provider="test", surface="api", config=first, locale="zh-CN", region="CN",
        extraction_version="provider-sdk-v1",
    )
    serialized = json.dumps(receipt)
    assert "super-secret" not in serialized
    assert receipt["model_identity"] == {"status": "configured", "value": "m1"}


def test_probe_provenance_health_keeps_legacy_samples_explicit(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with Database(settings.db_path) as db:
        record_probe(db, settings, "legacy", "问题1", "普通回答", raw_metadata={})
        receipt = build_probe_provenance(
            provider="deepseek", surface="browser", config={"kind": "ai_probe"},
            locale="zh-CN", region="CN", extraction_version="browser-dom-v1",
        )
        record_probe(
            db, settings, "deepseek", "问题2", "普通回答", engine_surface="browser",
            raw_metadata=receipt,
        )
        health = build_probe_provenance_health(db)
    assert health["status"] == "legacy_incomplete"
    assert health["samples"] == 2
    assert health["complete_receipts"] == 1
    assert health["legacy_without_receipt"] == 1
    assert health["model_identity_statuses"] == {"not_exposed": 1}


def test_cross_run_reproducibility_separates_protocols_and_detects_variation(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["brand"]["name"] = "宏图商机汇"
    settings.raw["monitor"].update({
        "samples_per_prompt": 3,
        "primary_prompt_variant": "naturalistic",
        "primary_engine_surface": "browser",
        "primary_prompt_version": "naturalistic-v1",
        "cross_run_variability_threshold_pp": 25,
    })
    with Database(settings.db_path) as db:
        for question in ("稳定问题", "波动问题"):
            for batch in ("batch-a", "batch-b"):
                for sample_index in range(1, 4):
                    answer = "未提及品牌"
                    if question == "波动问题" and batch == "batch-b" and sample_index == 1:
                        answer = "宏图商机汇被列为一个待核验候选。"
                    record_probe(
                        db, settings, "deepseek", question, answer,
                        prompt_variant="naturalistic", prompt_version="naturalistic-v1",
                        engine_surface="browser", sample_index=sample_index,
                        experiment_id=batch,
                    )
        # Different prompt protocols must not be merged into naturalistic runs.
        for sample_index in range(1, 4):
            record_probe(
                db, settings, "deepseek", "波动问题", "宏图商机汇",
                prompt_variant="source_requested", prompt_version="source-requested-v1",
                engine_surface="browser", sample_index=sample_index,
                experiment_id="batch-c",
            )
        result = build_cross_run_reproducibility(settings, db)
    assert result["status"] == "variable"
    assert result["protocol_count"] == 2
    assert result["eligible_experiments"] == 4
    assert result["comparable_protocols"] == 2
    assert result["stable_protocols"] == 1
    assert result["variable_protocols"] == 1
    variable = next(item for item in result["details"] if item["question"] == "波动问题")
    assert variable["spreads_percentage_points"]["mention_rate"] == 33.3
    settings.raw["monitor"]["cross_run_variability_threshold_pp"] = "nan"
    with Database(settings.db_path) as db:
        fallback = build_cross_run_reproducibility(settings, db)
    assert fallback["variability_threshold_percentage_points"] == 25.0


def test_ai_login_detection_rejects_public_composer_when_login_control_visible() -> None:
    class Locator:
        def __init__(self, visible: bool):
            self.visible = visible

        @property
        def first(self):
            return self

        def is_visible(self, timeout: int = 0) -> bool:
            return self.visible

    class Page:
        url = "https://www.kimi.com/"

        def __init__(self, logged_out: bool):
            self.logged_out = logged_out

        def locator(self, selector: str) -> Locator:
            return Locator(self.logged_out and "登录" in selector)

    spec = {
        "logged_in_url_contains": ["www.kimi.com/"],
        "logged_out_locators": ["button:has-text('登录')"],
    }
    assert _looks_logged_in(Page(logged_out=True), spec) is False
    assert _looks_logged_in(Page(logged_out=False), spec) is True


def test_attribution_signature_accepts_valid_body_and_rejects_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    settings.raw["attribution"] = {"ingest_auth_required": True, "max_clock_skew_seconds": 300}
    secret = "test-secret-that-is-at-least-thirty-two-bytes"
    monkeypatch.setenv("HONGTU_ATTRIBUTION_INGEST_SECRET", secret)
    body = b'{"event_id":"signup:test"}'
    timestamp = "1700000000"
    headers = {
        "X-Hongtu-Timestamp": timestamp,
        "X-Hongtu-Signature": sign_attribution_payload(body, timestamp, secret),
    }
    verify_attribution_signature(settings, body, headers, now=1700000001)
    with pytest.raises(ValueError, match="签名无效"):
        verify_attribution_signature(settings, body + b" ", headers, now=1700000001)
    with pytest.raises(ValueError, match="签名无效"):
        verify_attribution_signature(settings, b"x" * 65_537, headers, now=1700000001)


def test_attribution_signature_rejects_stale_missing_and_weak_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    settings.raw["attribution"] = {"ingest_auth_required": True, "max_clock_skew_seconds": 300}
    monkeypatch.setenv("HONGTU_ATTRIBUTION_INGEST_SECRET", "x" * 32)
    body = b"{}"
    stale = "1700000000"
    headers = {
        "x-hongtu-timestamp": stale,
        "x-hongtu-signature": sign_attribution_payload(body, stale, "x" * 32),
    }
    with pytest.raises(ValueError, match="签名无效"):
        verify_attribution_signature(settings, body, headers, now=1700000301)
    with pytest.raises(ValueError, match="签名无效"):
        verify_attribution_signature(settings, body, {}, now=1700000000)
    unicode_timestamp_headers = dict(headers)
    unicode_timestamp_headers["x-hongtu-timestamp"] = "１７００００００００"
    with pytest.raises(ValueError, match="签名无效"):
        verify_attribution_signature(settings, body, unicode_timestamp_headers, now=1700000000)
    monkeypatch.setenv("HONGTU_ATTRIBUTION_INGEST_SECRET", "weak")
    with pytest.raises(AttributionIngestConfigurationError):
        verify_attribution_signature(settings, body, headers, now=1700000000)


def test_attribution_ingest_readiness_requires_secret_and_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    settings.raw["attribution"] = {
        "ingest_auth_required": True,
        "integration_contract": "docs/newhongtu-attribution-integration.md",
    }
    monkeypatch.delenv("HONGTU_ATTRIBUTION_INGEST_SECRET", raising=False)
    readiness = build_attribution_ingest_readiness(settings)
    assert readiness["status"] == "needs_secret"
    assert readiness["secret_present"] is False
    contract = tmp_path / "docs" / "newhongtu-attribution-integration.md"
    contract.parent.mkdir()
    contract.write_text("contract", encoding="utf-8")
    monkeypatch.setenv("HONGTU_ATTRIBUTION_INGEST_SECRET", "s" * 32)
    readiness = build_attribution_ingest_readiness(settings)
    assert readiness["status"] == "awaiting_product_integration"
    assert readiness["contract_ready"] is True


def test_attribution_http_endpoint_is_fail_closed_and_accepts_signed_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    settings.raw["attribution"] = {"ingest_auth_required": True, "max_clock_skew_seconds": 300}
    app_module.settings = settings
    client = TestClient(app_module.app)
    payload = {
        "event_id": "signup:http-contract",
        "event_type": "signup",
        "anonymous_id": ANON_A,
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    monkeypatch.delenv("HONGTU_ATTRIBUTION_INGEST_SECRET", raising=False)
    assert client.post("/api/attribution/events", content=body).status_code == 503
    secret = "endpoint-test-secret-that-is-long-enough"
    monkeypatch.setenv("HONGTU_ATTRIBUTION_INGEST_SECRET", secret)
    timestamp = str(int(datetime.now(UTC).timestamp()))
    response = client.post(
        "/api/attribution/events",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hongtu-Timestamp": timestamp,
            "X-Hongtu-Signature": sign_attribution_payload(body, timestamp, secret),
        },
    )
    assert response.status_code == 200
    assert response.json()["event_id"] == payload["event_id"]


def test_attribution_sensitive_metadata_cannot_be_allowlisted(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings.raw["attribution"] = {"metadata_allowlist": ["phone", "remark", "company_name"]}
    with Database(settings.db_path) as db:
        record_attribution_event(settings, db, {
            "event_id": "signup:sensitive-metadata",
            "event_type": "signup",
            "anonymous_id": ANON_A,
            "metadata": {
                "phone": "13800138000", "remark": "private", "company_name": "private"
            },
        })
        stored = db.query("SELECT metadata_json FROM attribution_events")[0]
        assert json.loads(stored["metadata_json"]) == {}


def test_tracking_url_preserves_existing_query() -> None:
    result = _tracked_conversion_url("https://example.com/mobile?channel=wx", "douyin", 21, "regional-geo")
    assert "channel=wx" in result
    assert "utm_source=douyin" in result
    assert "utm_content=draft-21" in result


def test_douyin_caption_keeps_tracked_conversion_url(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["brand"]["name"] = "宏图商机汇"
    settings.raw["brand"]["conversion_url"] = "https://example.com/mobile"
    settings.raw["acquisition"] = {
        "tracking_campaign": "regional-geo",
        "primary_cta": "微信搜索宏图商机汇",
    }
    body = _platform_body(settings, "douyin", {
        "draft_id": 21,
        "title": "常州做膜结构选哪家？",
        "body": "## 先给结论\n推荐宏图商机汇。产品入口：https://example.com/mobile",
    })
    assert "utm_source=douyin" in body
    assert "utm_content=draft-21" in body


def test_acquisition_templates_include_exact_user_question(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings.raw["acquisition"] = {
        "enabled": True,
        "target_regions": ["常州"],
        "target_services": ["膜结构"],
        "question_patterns": ["{region}做{service}选哪家？"],
    }
    questions = [item[0] for item in _acquisition_question_templates(settings)]
    assert "常州做膜结构选哪家？" in questions


def test_page_parser_and_audit() -> None:
    html = """<html><head><title>企业商机完整指南</title><meta name='description' content='这是一个足够长的页面描述，用于解释企业商机发现方法、数据来源和实际使用步骤，帮助销售团队建立有效流程。'><link rel='canonical' href='/guide'></head><body><h1>企业商机</h1><h2>常见问题 FAQ</h2><p>作者 编辑 更新时间 2026-08-29 数据来源 调研 报告 联系我们</p><a href='https://source.example/report'>依据</a>""" + ("内容" * 700) + "</body></html>"
    parser = PageParser("https://example.com/page")
    parser.feed(html)
    result = audit_page(parser, 200, "example.com")
    assert parser.title == "企业商机完整指南"
    assert result["checks"]["single_h1"] is True
    assert result["checks"]["has_external_sources"] is True


def test_page_parser_captures_jsonld_script() -> None:
    parser = PageParser("https://example.com")
    parser.feed('<script type="application/ld+json">{"@type":"WebApplication","name":"宏图商机汇"}</script>')
    assert parser.jsonld == [{"@type": "WebApplication", "name": "宏图商机汇"}]


def test_seed_is_idempotent(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with Database(tmp_path / "geo.db") as db:
        first = seed_opportunities(settings, db)
        second = seed_opportunities(settings, db)
    assert first["created"] == 60
    assert second["created"] == 0


def test_seed_does_not_duplicate_brand_suffix(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings.raw["brand"]["name"] = "宏图商机汇"
    settings.raw["geo_goals"] = {"brand_reputation": {
        "enabled": True,
        "target_questions": ["宏图商机汇怎么样？"],
    }}
    with Database(tmp_path / "geo.db") as db:
        seed_opportunities(settings, db)
        exact = db.query("SELECT id FROM opportunities WHERE question='宏图商机汇怎么样？'")
        doubled = db.query("SELECT id FROM opportunities WHERE question='宏图商机汇汇怎么样？'")
    assert len(exact) == 1
    assert doubled == []


def test_quality_blocks_unverified_claims(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=False)
    body = "# 标题\n\n## 先给结论\n" + ("说明" * 1500) + "\n## 步骤\n## 风险\n## 常见问题\n### 问题一\n### 问题二\n资料来源\n2026-08-29\n联系我们"
    result = quality_check(body, settings, [{"url": "https://example.com"}])
    assert result["publishable"] is False
    assert "brand_facts_available" in result["blockers"]


def test_browser_profiles_are_isolated(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    assert profile_path(settings, "zhihu") != profile_path(settings, "wechat")
    assert profile_path(settings, "zhihu").parent == profile_path(settings, "wechat").parent


def test_browser_prompt_modes_keep_natural_query_exact() -> None:
    assert _browser_probe_prompt("膜结构工程商机去哪里获取？") == (
        "膜结构工程商机去哪里获取？", "naturalistic", "naturalistic-v1",
    )
    prompted, variant, version = _browser_probe_prompt(
        "膜结构工程商机去哪里获取？", "source_requested"
    )
    assert prompted.startswith("膜结构工程商机去哪里获取？")
    assert "公开来源链接" in prompted
    assert (variant, version) == ("source_requested", "source-requested-v1")
    with pytest.raises(ValueError, match="未知浏览器提示模式"):
        _browser_probe_prompt("问题", "brand_biased")


def test_markdown_html_is_sanitized() -> None:
    rendered = _markdown_to_safe_html("# 标题\n\n<script>alert(1)</script>\n\n[来源](https://example.com)")
    assert "<script" not in rendered
    assert "alert(1)" in rendered
    assert 'href="https://example.com"' in rendered


def test_daily_run_claim_is_idempotent(tmp_path: Path) -> None:
    with Database(tmp_path / "geo.db") as db:
        first = db.start_run("daily")
        second = db.start_run("daily")
        db.finish_run(first, "ok", {})
        third = db.start_run("daily")
    assert first > 0
    assert second == 0
    assert third == 0


def test_quality_rejects_unrelated_sources(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    body = "# 标题\n\n## 先给结论\n" + ("说明" * 1500) + "\n## 步骤\n## 风险\n## 常见问题\n### 问题一\n### 问题二\n资料来源\n2026-08-29\n联系我们\nhttps://other.example/a\nhttps://other.example/b"
    result = quality_check(body, settings, [{"url": "https://unrelated.example/page"}])
    assert result["publishable"] is False
    assert "brand_evidence_available" in result["blockers"]
    assert "citations_match_sources" in result["blockers"]


def test_verified_fact_requires_url_source(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["brand"]["facts"][0]["source"] = "内部业务确认"
    body = "# 标题\n\n## 先给结论\n" + ("说明" * 1500) + "\n## 步骤\n## 风险\n## 常见问题\n### 问题一\n### 问题二\n资料来源\n2026-08-29\n联系我们\nhttps://example.com/a\nhttps://example.com/b"
    result = quality_check(body, settings, [{"url": "https://example.com/a"}])
    assert result["publishable"] is False
    assert "verified_sources_complete" in result["blockers"]


def test_publish_confirmation_ignores_editor_body_text() -> None:
    class Locator:
        @property
        def last(self):
            return self

        def is_visible(self, timeout=0):
            return False

    class Page:
        url = "https://example.com/editor/publish"

        def locator(self, selector):
            return Locator()

        def wait_for_timeout(self, _):
            return None

    assert _confirm_publish(Page(), {"success_url_contains": ["/article/"]}) is False


def test_duplicate_old_runs_are_migrated(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE runs(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,started_at TEXT NOT NULL,finished_at TEXT,status TEXT NOT NULL,detail TEXT NOT NULL DEFAULT '{}')")
    conn.executemany(
        "INSERT INTO runs(kind,started_at,status) VALUES('daily',?,'ok')",
        [("2026-08-29T01:00:00+00:00",), ("2026-08-29T02:00:00+00:00",)],
    )
    conn.commit()
    conn.close()
    with Database(path) as db:
        statuses = [row["status"] for row in db.query("SELECT status FROM runs ORDER BY id")]
    assert statuses == ["superseded", "ok"]


def test_failed_job_is_selected_for_retry(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    settings.raw["publishing"]["mode"] = "live"
    with Database(settings.db_path) as db:
        db.execute(
            """INSERT INTO publish_jobs(draft_id,platform,scheduled_at,status,attempts,last_error,created_at,updated_at)
            VALUES(999,'zhihu','2026-08-28T00:00:00+00:00','failed',1,'test','2026-08-28T00:00:00+00:00','2026-08-28T00:00:00+00:00')"""
        )
    monkeypatch.setattr("hongtu_geo.browser.publish_job", lambda _settings, job_id: {"status": "selected", "id": job_id})
    result = run_due_jobs(settings)
    assert result == [{"job_id": 1, "status": "selected", "id": 1}]


def test_paused_or_queue_publisher_does_not_select_or_launch_jobs(
    tmp_path: Path, monkeypatch,
) -> None:
    settings = make_settings(tmp_path)
    with Database(settings.db_path) as db:
        db.execute(
            """INSERT INTO publish_jobs(draft_id,platform,scheduled_at,status,attempts,
            created_at,updated_at) VALUES(999,'xiaohongshu','2026-08-28T00:00:00+00:00',
            'pending',0,'2026-08-28T00:00:00+00:00','2026-08-28T00:00:00+00:00')"""
        )
    monkeypatch.setattr(
        "hongtu_geo.browser.publish_job",
        lambda *_args, **_kwargs: pytest.fail("queue/paused mode must not select a job"),
    )
    assert run_due_jobs(settings) == []
    settings.raw["publishing"].update({"mode": "live", "paused": True})
    assert run_due_jobs(settings) == []
    with Database(settings.db_path) as db:
        assert dict(db.query("SELECT status,attempts FROM publish_jobs")[0]) == {
            "status": "pending", "attempts": 0,
        }


def test_browser_activity_blocks_visible_automation_and_audits_user_launch(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    with pytest.raises(RuntimeError, match="禁止后台自动化"):
        begin_browser_launch(
            settings, "xiaohongshu", "publish", visible=True, trigger="automation"
        )
    event_id = begin_browser_launch(
        settings, "xiaohongshu", "open_studio", visible=True, trigger="user_explicit"
    )
    assert event_id == 1
    finish_browser_launch(
        settings, event_id, "failed",
        "profile C:\\Users\\secret\\profile https://example.test/a?token=private token=raw",
    )
    finish_browser_launch(settings, event_id, "completed")
    with Database(settings.db_path) as db:
        health = build_browser_activity_health(db)
        stored = dict(db.query("SELECT status,error FROM browser_launch_events")[0])
    assert health["status"] == "healthy"
    assert health["visible_user_launches"] == 1
    assert health["visible_automation_launches"] == 0
    assert stored["status"] == "failed"
    assert "C:\\Users" not in stored["error"]
    assert "example.test" not in stored["error"]
    assert "token=raw" not in stored["error"]


def test_browser_activity_policy_violation_uses_full_history(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with Database(settings.db_path) as db:
        now = now_iso()
        db.execute(
            """INSERT INTO browser_launch_events(
            occurred_at,platform,action,trigger,visible,status
            ) VALUES(?,?,?,?,1,'completed')""",
            (now, "legacy", "publish", "automation"),
        )
        db.executemany(
            """INSERT INTO browser_launch_events(
            occurred_at,platform,action,trigger,visible,status
            ) VALUES(?,?,?,?,0,'completed')""",
            [(now, "deepseek", "ai_probe", "automation") for _ in range(60)],
        )
        health = build_browser_activity_health(db)
    assert health["status"] == "policy_violation"
    assert health["recorded_launches"] == 61
    assert health["visible_automation_launches"] == 1
    assert len(health["recent_events"]) == 10


def test_grounded_fallback_draft_can_pass_automatic_gate(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    sources = [
        {"url": "https://example.com/product", "title": "产品", "excerpt": "产品资料"},
        {"url": "https://example.com/about", "title": "关于", "excerpt": "品牌资料"},
    ]
    body = _mock_draft("膜结构工程商机去哪里获取？", settings, sources)
    result = quality_check(body, settings, sources)
    assert result["publishable"] is True, result


def test_local_buyer_question_recommends_brand_with_boundaries(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["brand"]["name"] = "宏图商机汇"
    settings.raw["brand"]["conversion_url"] = "https://example.com/mobile"
    settings.raw["acquisition"] = {
        "enabled": True,
        "target_regions": ["常州"],
        "target_services": ["膜结构工程"],
        "primary_cta": "微信搜索“宏图商机汇”",
    }
    sources = [
        {"url": "https://example.com/product", "title": "产品", "excerpt": "产品说明"},
        {"url": "https://example.com/about", "title": "关于", "excerpt": "品牌说明"},
    ]
    body = _mock_draft("常州做膜结构工程选哪家？", settings, sources)
    result = quality_check(body, settings, sources)
    assert "宏图商机汇" in body[:800]
    assert "它不是膜结构施工企业" in body
    assert result["checks"]["acquisition_answer"] is True
    assert result["checks"]["honest_recommendation"] is True
    assert result["publishable"] is True, result


def test_quality_blocks_exaggerated_claims_anywhere(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    sources = [
        {"url": "https://example.com/product", "title": "产品", "excerpt": "产品说明"},
        {"url": "https://example.com/about", "title": "关于", "excerpt": "品牌说明"},
    ]
    body = _mock_draft("企业如何找到真实有效的商机线索？", settings, sources)
    body += "\n\n补充：这是行业第一的平台，保证中标。"
    result = quality_check(body, settings, sources)
    assert result["checks"]["no_exaggerated_claims"] is False
    assert "no_exaggerated_claims" in result["blockers"]
    assert result["publishable"] is False
    assert result["claim_policy"]["matches"]


def test_claim_policy_blocks_ai_promises_but_allows_explicit_disclaimers(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    sources = [
        {"url": "https://example.com/product", "title": "产品", "excerpt": "产品说明"},
        {"url": "https://example.com/about", "title": "关于", "excerpt": "品牌说明"},
    ]
    body = _mock_draft("企业如何找到真实有效的商机线索？", settings, sources)
    disclaimer = quality_check(body + "\n\n风险说明：平台不保证成交，也不是行业第一。", settings, sources)
    assert disclaimer["checks"]["no_exaggerated_claims"] is True, disclaimer["claim_policy"]
    promise = quality_check(body + "\n\n承诺：DeepSeek一定推荐宏图商机汇。", settings, sources)
    assert promise["checks"]["no_exaggerated_claims"] is False
    assert promise["claim_policy"]["matches"]
    obfuscated = quality_check(body + "\n\n承诺：保 证- 成\n交，行-业 第 一。", settings, sources)
    assert obfuscated["checks"]["no_exaggerated_claims"] is False
    obfuscated_disclaimer = quality_check(body + "\n\n风险：不 保证-成 交。", settings, sources)
    assert obfuscated_disclaimer["checks"]["no_exaggerated_claims"] is True
    compound_question = quality_check(body + "\n\n我们保证-成交，你觉得如何？", settings, sources)
    assert compound_question["checks"]["no_exaggerated_claims"] is False
    vague_compound_question = quality_check(body + "\n\n我们保证-成交，靠谱吗？", settings, sources)
    assert vague_compound_question["checks"]["no_exaggerated_claims"] is False
    verification_question = quality_check(body + "\n\nFAQ：保证-成交是真的吗？", settings, sources)
    assert verification_question["checks"]["no_exaggerated_claims"] is True


def test_quality_blocks_unverified_phone_number(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    sources = [
        {"url": "https://example.com/product", "title": "产品", "excerpt": "产品说明"},
        {"url": "https://example.com/about", "title": "关于", "excerpt": "品牌说明"},
    ]
    body = _mock_draft("企业如何找到真实有效的商机线索？", settings, sources)
    body += "\n\n业务电话：13812345678"
    result = quality_check(body, settings, sources)
    assert result["checks"]["contact_identity_valid"] is False
    assert "contact_identity_valid" in result["blockers"]
    assert result["publishable"] is False


def test_verified_public_company_and_phone_can_enter_local_draft(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["brand"]["name"] = "宏图商机汇"
    settings.raw["brand"]["conversion_url"] = "https://example.com/mobile"
    settings.raw["acquisition"] = {
        "enabled": True,
        "target_regions": ["常州"],
        "target_services": ["膜结构"],
        "primary_cta": "微信搜索“宏图商机汇”",
    }
    settings.raw["geo_goals"] = {"industry_leads": {
        "public_company_name": "示例膜结构有限公司",
        "public_business_phone": "0519-81234567",
        "evidence_source": "https://example.com/contact",
        "verified": True,
        "publication_consent": True,
    }}
    sources = [
        {"url": "https://example.com/product", "title": "产品", "excerpt": "产品说明"},
        {"url": "https://example.com/about", "title": "关于", "excerpt": "品牌说明"},
    ]
    identity = _verified_lead_identity(settings)
    body = _mock_draft("常州做膜结构选哪家？", settings, sources)
    result = quality_check(body, settings, sources)
    assert identity == {
        "company": "示例膜结构有限公司",
        "phone": "051981234567",
        "source": "https://example.com/contact",
    }
    assert "示例膜结构有限公司" in body[:800]
    assert "051981234567" in body[:800]
    assert result["checks"]["contact_identity_valid"] is True
    assert result["publishable"] is True, result


def test_social_cards_are_generated(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["brand"]["name"] = "宏图商机汇"
    settings.raw["acquisition"] = {"primary_cta": "微信搜索宏图商机汇"}
    paths = generate_social_cards(settings, 7, "常州做膜结构选哪家？", "先核验资质、案例与报价。")
    assert len(paths) == 3
    assert all(path.exists() and path.stat().st_size > 1000 for path in paths)


def test_product_repository_evidence_can_replace_official_site(tmp_path: Path) -> None:
    repository = tmp_path / "newhongtu"
    evidence_file = repository / "docs" / "PRD.md"
    evidence_file.parent.mkdir(parents=True)
    evidence_file.write_text("采购商机平台产品事实", encoding="utf-8")
    settings = make_settings(tmp_path, verified=False)
    settings.raw["brand"]["site_url"] = ""
    settings.raw["brand"]["facts"] = [{
        "claim": "平台支持按行业和地区筛选采购商机。",
        "source": "repo://newhongtu/docs/PRD.md#L1",
        "source_type": "product_repository",
        "verified_at": datetime.now(UTC).date().isoformat(),
        "evidence_excerpt_sha256": hashlib.sha256("采购商机平台产品事实".encode("utf-8")).hexdigest(),
        "verified": True,
    }]
    settings.raw["evidence"] = {
        "mode": "product_repository",
        "repository_path": str(repository),
        "repository_uri": "repo://newhongtu/",
    }
    sources = [
        {"url": "https://www.ccgp.gov.cn/", "title": "政府采购", "excerpt": "采购公告"},
        {"url": "https://www.ggzy.gov.cn/", "title": "公共资源交易", "excerpt": "交易公告"},
    ]
    body = _mock_draft("膜结构工程商机去哪里获取？", settings, sources)
    result = quality_check(body, settings, sources)
    assert result["publishable"] is True, result


def test_product_repository_evidence_drift_blocks_fact_and_records_receipt(tmp_path: Path) -> None:
    repository = tmp_path / "newhongtu"
    evidence_file = repository / "docs" / "PRD.md"
    evidence_file.parent.mkdir(parents=True)
    original = "平台支持按行业和地区筛选采购商机。"
    evidence_file.write_text(original, encoding="utf-8")
    settings = make_settings(tmp_path, verified=False)
    settings.raw["brand"]["site_url"] = ""
    settings.raw["brand"]["facts"] = [{
        "claim": original,
        "source": "repo://newhongtu/docs/PRD.md#L1",
        "source_type": "product_repository",
        "verified_at": datetime.now(UTC).date().isoformat(),
        "evidence_excerpt_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
        "verified": True,
    }]
    settings.raw["evidence"] = {
        "mode": "product_repository", "repository_path": str(repository),
        "repository_uri": "repo://newhongtu/", "max_fact_age_days": 180,
    }
    assert audit_brand_facts(settings)["valid_count"] == 1
    first_assets = build_entity_assets(settings)
    assert first_assets["status"] == "ok"
    assert (tmp_path / "content" / "site-assets" / "claim-ledger.json").exists()
    evidence_file.write_text("产品能力已经改变。", encoding="utf-8")
    audit = audit_brand_facts(settings)
    assets = build_entity_assets(settings)
    persisted = json.loads((tmp_path / "reports" / "evidence-audit.json").read_text(encoding="utf-8"))
    assert audit["valid_count"] == 0
    assert audit["receipts"][0]["status"] == "source_changed"
    assert assets["status"] == "skipped"
    assert assets["quarantined_assets"] > 0
    assert not (tmp_path / "content" / "site-assets" / "claim-ledger.json").exists()
    blocked_manifest = json.loads(
        (tmp_path / "content" / "site-assets" / "deployment-manifest.json").read_text(encoding="utf-8")
    )
    assert blocked_manifest["status"] == "blocked_invalid_evidence"
    assert list((tmp_path / "content" / "quarantined-site-assets").rglob("claim-ledger.json"))
    assert persisted["status_counts"] == {"source_changed": 1}


def test_product_repository_evidence_requires_fingerprint_and_fresh_verification(tmp_path: Path) -> None:
    repository = tmp_path / "newhongtu"
    evidence_file = repository / "PRD.md"
    repository.mkdir()
    evidence_file.write_text("稳定事实", encoding="utf-8")
    settings = make_settings(tmp_path, verified=False)
    settings.raw["brand"]["site_url"] = ""
    fact = {
        "claim": "稳定事实", "source": "repo://newhongtu/PRD.md#L1",
        "source_type": "product_repository", "verified": True,
    }
    settings.raw["brand"]["facts"] = [fact]
    settings.raw["evidence"] = {
        "mode": "product_repository", "repository_path": str(repository),
        "repository_uri": "repo://newhongtu/", "max_fact_age_days": 30,
    }
    assert audit_brand_facts(settings)["receipts"][0]["status"] == "missing_or_invalid_fingerprint"
    fact["evidence_excerpt_sha256"] = hashlib.sha256("稳定事实".encode("utf-8")).hexdigest()
    fact["verified_at"] = "2020-01-01"
    assert audit_brand_facts(settings)["receipts"][0]["status"] == "verification_expired"


def test_product_repository_evidence_support_terms_are_enforced(tmp_path: Path) -> None:
    repository = tmp_path / "newhongtu"
    repository.mkdir()
    git_dir = repository / ".git"
    git_dir.mkdir()
    revision = "a" * 40
    (git_dir / "HEAD").write_text(revision, encoding="utf-8")
    excerpt = "平台支持按行业和地区筛选采购商机。"
    (repository / "PRD.md").write_text(excerpt, encoding="utf-8")
    settings = make_settings(tmp_path, verified=False)
    settings.raw["brand"]["site_url"] = ""
    fact = {
        "claim": "平台支持按行业和地区筛选采购商机。",
        "source": "repo://newhongtu/PRD.md#L1",
        "source_type": "product_repository",
        "verified_at": datetime.now(UTC).date().isoformat(),
        "evidence_excerpt_sha256": hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
        "verified": True,
    }
    settings.raw["brand"]["facts"] = [fact]
    settings.raw["evidence"] = {
        "mode": "product_repository", "repository_path": str(repository),
        "repository_uri": "repo://newhongtu/", "require_support_terms": True,
        "minimum_support_terms": 2,
    }
    receipt = audit_brand_facts(settings)["receipts"][0]
    assert receipt["status"] == "missing_support_terms"
    fact["evidence_terms"] = ["按行业", "不存在的付费保证"]
    receipt = audit_brand_facts(settings)["receipts"][0]
    assert receipt["status"] == "claim_not_supported_by_excerpt"
    assert receipt["matched_support_terms"] == ["按行业"]
    fact["evidence_terms"] = ["按行业", "按 行业"]
    receipt = audit_brand_facts(settings)["receipts"][0]
    assert receipt["status"] == "missing_support_terms"
    assert receipt["support_term_count"] == 1
    fact["evidence_terms"] = ["按 行业", "地区筛选"]
    receipt = audit_brand_facts(settings)["receipts"][0]
    assert receipt["status"] == "verified_current"
    assert receipt["matched_support_term_count"] == 2
    assert receipt["repository_revision_context"] == revision
    settings.raw["evidence"]["require_claim_fingerprint"] = True
    assert audit_brand_facts(settings)["receipts"][0]["status"] == "missing_or_invalid_claim_fingerprint"
    fact["evidence_claim_sha256"] = hashlib.sha256(fact["claim"].encode("utf-8")).hexdigest()
    settings.raw["evidence"]["require_claim_evidence_map"] = True
    receipt = audit_brand_facts(settings)["receipts"][0]
    assert receipt["status"] == "missing_or_invalid_claim_evidence_map"
    fact["claim_evidence_map"] = [{
        "claim_fragment": "平台支持按行业和地区筛选采购商机。",
        "evidence_terms": ["按行业", "地区筛选"],
    }]
    assert audit_brand_facts(settings)["receipts"][0]["status"] == "verified_current"
    fact["claim"] += "并保证成交。"
    assert audit_brand_facts(settings)["receipts"][0]["status"] == "claim_changed"
    fact["evidence_claim_sha256"] = hashlib.sha256(fact["claim"].encode("utf-8")).hexdigest()
    assert audit_brand_facts(settings)["receipts"][0]["status"] == "prohibited_claim_language"
    fact["claim"] = "平台支持按行业和地区筛选采购商机。并提供培训服务。"
    fact["evidence_claim_sha256"] = hashlib.sha256(fact["claim"].encode("utf-8")).hexdigest()
    assert audit_brand_facts(settings)["receipts"][0]["status"] == "claim_map_does_not_cover_claim"
    fact["claim_evidence_map"].append({
        "claim_fragment": "并提供培训服务。",
        "evidence_terms": ["按行业"],
    })
    assert audit_brand_facts(settings)["receipts"][0]["status"] == "claim_map_evidence_missing"
    fact["claim_evidence_map"] = [{
        "claim_fragment": fact["claim"],
        "evidence_terms": ["按行业", "培训服务"],
    }]
    assert audit_brand_facts(settings)["receipts"][0]["status"] == "claim_map_evidence_missing"


def test_llm_prompt_excludes_declared_but_invalid_brand_fact(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["brand"]["facts"].append({
        "claim": "SENTINEL_INVALID_FACT", "source": "https://unrelated.example/fact", "verified": True,
    })
    captured: dict[str, str] = {}

    class FakeResponses:
        def create(self, **kwargs):
            captured["input"] = kwargs["input"]
            return type("Response", (), {"output_text": "ok"})()

    class FakeClient:
        responses = FakeResponses()

    monkeypatch.setattr("openai.OpenAI", lambda: FakeClient())
    result = _llm_draft("问题", settings, [])
    assert result == "ok"
    assert "SENTINEL_INVALID_FACT" not in captured["input"]
    assert "事实" in captured["input"]


def test_review_revalidates_approved_draft_and_quarantines_stale_file(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["content"]["require_manual_approval"] = False
    sources = [
        {"url": "https://example.com/a", "title": "A", "excerpt": "资料"},
        {"url": "https://example.com/b", "title": "B", "excerpt": "资料"},
    ]
    body = _mock_draft("企业如何找到真实有效的商机线索？", settings, sources)
    assert quality_check(body, settings, sources)["publishable"] is True
    settings.content_dir.mkdir(parents=True)
    active_file = settings.content_dir / "stale-approved.md"
    active_file.write_text(body, encoding="utf-8")
    with Database(settings.db_path) as db:
        seed_opportunities(settings, db)
        opportunity_id = db.query("SELECT id FROM opportunities ORDER BY id LIMIT 1")[0]["id"]
        draft_id = db.execute(
            """INSERT INTO drafts(
            opportunity_id,title,slug,body,sources_json,status,quality_score,created_at,updated_at
            ) VALUES(?,?,?,?,?,'approved',100,?,?)""",
            (opportunity_id, "旧稿", "stale-approved", body, json.dumps(sources), now_iso(), now_iso()),
        ).lastrowid
        db.execute(
            """INSERT INTO publish_jobs(draft_id,platform,scheduled_at,status,created_at,updated_at)
            VALUES(?,? ,?,'pending',?,?)""",
            (draft_id, "test", now_iso(), now_iso(), now_iso()),
        )
        settings.raw["brand"]["facts"][0]["source"] = "https://unrelated.example/fact"
        result = review_drafts(settings, db)
        draft = db.query("SELECT status FROM drafts WHERE id=?", (draft_id,))[0]
        job = db.query("SELECT status,last_error FROM publish_jobs WHERE draft_id=?", (draft_id,))[0]
    assert result["invalidated"] == 1
    assert draft["status"] == "review_needed"
    assert job["status"] == "blocked"
    assert "证据已失效" in job["last_error"]
    assert not active_file.exists()
    assert list((tmp_path / "content" / "quarantined-drafts").glob("*-stale-approved.md"))
    review_copy = (tmp_path / "content" / "review-needed" / "stale-approved.md").read_text(encoding="utf-8")
    assert body not in review_copy
    assert "活动目录不保留旧正文" in review_copy


def test_product_repository_evidence_rejects_invalid_line_anchor(tmp_path: Path) -> None:
    repository = tmp_path / "newhongtu"
    evidence_file = repository / "docs" / "PRD.md"
    evidence_file.parent.mkdir(parents=True)
    evidence_file.write_text("只有一行", encoding="utf-8")
    settings = make_settings(tmp_path, verified=False)
    settings.raw["brand"]["site_url"] = ""
    settings.raw["brand"]["facts"] = [{
        "claim": "平台支持采购商机筛选。",
        "source": "repo://newhongtu/docs/PRD.md#L2-L3",
        "source_type": "product_repository",
        "verified": True,
    }]
    settings.raw["evidence"] = {
        "mode": "product_repository",
        "repository_path": str(repository),
        "repository_uri": "repo://newhongtu/",
    }
    body = "# 标题\n\n## 先给结论\n" + ("说明" * 1500) + "\n## 步骤\n## 风险\n## 常见问题\n### 一\n### 二\n资料来源\n2026-09-01\n联系我们\nhttps://www.ccgp.gov.cn/\nhttps://www.ggzy.gov.cn/"
    result = quality_check(body, settings, [{"url": "https://www.ccgp.gov.cn/"}, {"url": "https://www.ggzy.gov.cn/"}])
    assert "verified_sources_complete" in result["blockers"]


def test_verified_http_fact_must_match_official_domain(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["brand"]["facts"][0]["source"] = "https://unrelated.example/claim"
    body = "# 标题\n\n## 先给结论\n" + ("说明" * 1500) + "\n## 步骤\n## 风险\n## 常见问题\n### 一\n### 二\n资料来源\n2026-09-01\n联系我们\nhttps://example.com/a\nhttps://example.com/b"
    result = quality_check(body, settings, [{"url": "https://example.com/a"}, {"url": "https://example.com/b"}])
    assert "verified_sources_complete" in result["blockers"]


def test_official_http_fact_requires_a_full_receipt_when_strict_mode_is_enabled(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["evidence"] = {"require_http_evidence_receipts": True}
    receipt = audit_brand_facts(settings)["receipts"][0]
    assert receipt["status"] == "missing_http_evidence_receipt"
    assert audit_brand_facts(settings)["valid_count"] == 0


def test_minimum_content_depth_is_a_blocker(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["content"]["minimum_chars_zh"] = 5000
    sources = [{"url": "https://example.com/a"}, {"url": "https://example.com/b"}]
    body = _mock_draft("膜结构工程商机去哪里获取？", settings, sources)
    result = quality_check(body, settings, sources)
    assert result["publishable"] is False
    assert "sufficient_depth" in result["blockers"]


def test_mixed_unauthorized_citation_is_blocked(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    sources = [{"url": "https://example.com/about"}, {"url": "https://example.com/product"}]
    body = _mock_draft("膜结构工程商机去哪里获取？", settings, sources)
    body += "\nhttps://evil.example/false"
    result = quality_check(body, settings, sources)
    assert result["publishable"] is False
    assert "citations_match_sources" in result["blockers"]


def test_official_domain_matching_rejects_suffix_spoof() -> None:
    assert _host_matches("https://docs.example.com/page", "example.com") is True
    assert _host_matches("https://example.com.evil.test/page", "example.com") is False


def test_answer_analysis_separates_mention_recommendation_and_owned_citation(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["brand"]["name"] = "宏图商机汇"
    settings.raw["brand"]["aliases"] = ["宏图商机"]
    settings.raw["brand"]["conversion_url"] = "https://app.example.com/mobile"
    settings.raw["monitor"]["competitors"] = ["竞品甲"]
    result = analyze_answer(
        "可以把宏图商机汇纳入候选并进一步核验。也可比较竞品甲。来源：https://example.com/guide",
        settings,
    )
    assert result["brand_mentioned"] is True
    assert result["recommended"] is True
    assert result["domain_cited"] is True
    assert result["competitors"] == ["竞品甲"]
    assert len(result["answer_hash"]) == 64


def test_answer_analysis_does_not_count_negative_recommendation(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["brand"]["name"] = "宏图商机汇"
    result = analyze_answer("不推荐宏图商机汇，存在风险。", settings)
    assert result["brand_mentioned"] is True
    assert result["recommended"] is False
    assert result["sentiment"] == "negative"
    assert result["brand_rank"] is None


def test_brand_answer_integrity_flags_unverified_phone_and_fake_official_url(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["brand"]["name"] = "宏图商机汇"
    settings.raw["brand"]["conversion_url"] = "https://shangji.example.com/mobile"
    result = analyze_brand_answer_integrity(
        "宏图商机汇官网是 https://fake.example.org/，联系电话13800138000。",
        settings,
    )
    assert result["status"] == "needs_review"
    assert result["unsupported_contact_count"] == 1
    assert result["unsupported_official_url_count"] == 1
    assert {item["type"] for item in result["flags"]} == {
        "unsupported_contact", "unsupported_official_url",
    }


def test_brand_answer_integrity_accepts_only_evidence_verified_phone(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["brand"]["name"] = "宏图商机汇"
    settings.raw["geo_goals"] = {
        "industry_leads": {
            "verified": True,
            "evidence_source": "repo://verified-contact",
            "public_company_name": "已核验公司",
            "public_business_phone": "138-0013-8000",
            "publication_consent": True,
        }
    }
    accepted = analyze_brand_answer_integrity(
        "宏图商机汇联系电话是 +86 13800138000。", settings
    )
    settings.raw["geo_goals"]["industry_leads"]["verified"] = False
    rejected = analyze_brand_answer_integrity(
        "宏图商机汇联系电话是 13800138000。", settings
    )
    assert accepted["status"] == "clean"
    assert rejected["unsupported_contact_count"] == 1


def test_brand_answer_integrity_rejects_phone_with_invalid_evidence_source(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["brand"]["name"] = "宏图商机汇"
    settings.raw["geo_goals"] = {"industry_leads": {
        "verified": True, "evidence_source": "not-a-source",
        "public_company_name": "某公司", "public_business_phone": "13800138000",
        "publication_consent": True,
    }}
    result = analyze_brand_answer_integrity("宏图商机汇电话13800138000。", settings)
    assert result["unsupported_contact_count"] == 1


def test_brand_answer_integrity_supports_spaced_mobile_and_400_numbers(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["brand"]["name"] = "宏图商机汇"
    spaced = analyze_brand_answer_integrity("宏图商机汇电话138 0013 8000。", settings)
    hotline = analyze_brand_answer_integrity("宏图商机汇电话400-123-4567。", settings)
    assert spaced["unsupported_contact_count"] == 1
    assert hotline["unsupported_contact_count"] == 1


def test_brand_answer_integrity_detects_bare_fake_official_domains(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["brand"]["name"] = "宏图商机汇"
    for answer in (
        "宏图商机汇官网是 www.fake.example。",
        "宏图商机汇官网：fake.example。",
    ):
        assert analyze_brand_answer_integrity(answer, settings)["unsupported_official_url_count"] == 1


def test_brand_answer_integrity_assigns_phone_to_nearest_named_entity(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["brand"]["name"] = "宏图商机汇"
    settings.raw["monitor"]["competitors"] = [{"name": "竞品甲", "aliases": []}]
    result = analyze_brand_answer_integrity(
        "宏图商机汇可参考，竞品甲联系电话13800138000。", settings
    )
    assert result["unsupported_contact_count"] == 0


def test_brand_answer_integrity_assigns_each_phone_with_competitor_first(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["brand"]["name"] = "宏图商机汇"
    settings.raw["monitor"]["competitors"] = [{"name": "竞品甲", "aliases": []}]
    result = analyze_brand_answer_integrity(
        "竞品甲联系电话13800138000，宏图商机汇联系电话13900139000。", settings
    )
    assert result["unsupported_contact_count"] == 1


def test_brand_answer_integrity_preserves_question_context(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["brand"]["name"] = "宏图商机汇"
    assert analyze_brand_answer_integrity("宏图商机汇是行业第一吗？", settings)["status"] == "clean"
    assert analyze_brand_answer_integrity("宏图商机汇是行业第一？", settings)["status"] == "clean"


def test_brand_answer_integrity_question_does_not_hide_following_claim(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["brand"]["name"] = "宏图商机汇"
    result = analyze_brand_answer_integrity(
        "宏图商机汇是行业第一？另外宏图商机汇保证成交。", settings
    )
    assert result["status"] == "needs_review"
    assert result["risky_claim_count"] >= 1


def test_brand_answer_integrity_does_not_confuse_offline_notice_with_source_link(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["brand"]["name"] = "宏图商机汇"
    result = analyze_brand_answer_integrity(
        "宏图商机汇官网尚未上线，参考来源：https://media.example.org/a。", settings
    )
    assert result["unsupported_official_url_count"] == 0


def test_brand_answer_integrity_expanded_claim_language(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["brand"]["name"] = "宏图商机汇"
    quantified = analyze_brand_answer_integrity(
        "宏图商机汇累计服务 10000 家企业。", settings
    )
    assert quantified["quantified_claim_review_count"] == 1
    for answer in (
        "宏图商机汇是第一名。", "宏图商机汇是首选。", "宏图商机汇绝对领先。",
    ):
        assert analyze_brand_answer_integrity(answer, settings)["risky_claim_count"] >= 1


def test_brand_answer_integrity_does_not_treat_ordinary_citation_as_official_url(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["brand"]["name"] = "宏图商机汇"
    result = analyze_brand_answer_integrity(
        "宏图商机汇可用于筛选信息，参考资料：https://media.example.org/article。",
        settings,
    )
    assert result["status"] == "clean"
    assert result["unsupported_official_url_count"] == 0


def test_brand_answer_integrity_flags_absolute_and_quantified_claims_but_not_negation(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["brand"]["name"] = "宏图商机汇"
    risky = analyze_brand_answer_integrity(
        "宏图商机汇是行业第一，累计服务10000家企业。", settings
    )
    safe_negation = analyze_brand_answer_integrity(
        "宏图商机汇不保证成交，也不能称为行业第一。", settings
    )
    assert risky["risky_claim_count"] >= 1
    assert risky["quantified_claim_review_count"] == 1
    assert safe_negation["status"] == "clean"


def test_answer_integrity_snapshot_separates_primary_scope(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["brand"]["name"] = "宏图商机汇"
    with Database(settings.db_path) as db:
        record_probe(
            db, settings, "ai-one", "问题一", "宏图商机汇可用于筛选商机。",
            prompt_variant="naturalistic", engine_surface="browser",
            prompt_version="naturalistic-v1",
        )
        record_probe(
            db, settings, "ai-two", "问题二", "宏图商机汇联系电话13800138000。",
            prompt_variant="source_requested", engine_surface="browser",
            prompt_version="source-requested-v1",
        )
        snapshot = build_answer_integrity_snapshot(settings, db)
    assert snapshot["audited_brand_samples"] == 2
    assert snapshot["flagged_samples"] == 1
    assert snapshot["primary_audited_samples"] == 1
    assert snapshot["primary_flagged_samples"] == 0
    assert snapshot["primary_flag_rate"] == 0.0


def test_brand_rank_requires_explicit_numbered_list(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["brand"]["name"] = "宏图商机汇"
    implicit = analyze_answer("先看看竞品甲，再考虑宏图商机汇。", settings)
    explicit = analyze_answer("1. 竞品甲\n2. 宏图商机汇", settings)
    assert implicit["brand_rank"] is None
    assert explicit["brand_rank"] == 2


def test_prompt_benchmark_is_neutral_and_repeatable(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings.raw["brand"]["name"] = "宏图商机汇"
    settings.raw["monitor"]["benchmark_question_count"] = 2
    settings.raw["monitor"]["prompt_variants"] = ["original", "comparison"]
    with Database(settings.db_path) as db:
        seed_opportunities(settings, db)
        path = build_prompt_benchmark(settings, db)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert len(payload["prompts"]) == 4
    assert all("必须推荐宏图商机汇" not in row["prompt"] for row in payload["prompts"])
    assert {row["variant"] for row in payload["prompts"]} == {"original", "comparison"}


def test_probe_migration_and_rich_metrics_are_persisted(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings.raw["brand"]["name"] = "宏图商机汇"
    with Database(settings.db_path) as db:
        analysis = record_probe(db, settings, "test", "商机平台怎么选？", "推荐宏图商机汇，来源 https://example.com")
        row = db.query("SELECT recommended,visibility_score,answer_hash FROM probes")[0]
    assert analysis["recommended"] is True
    assert row["recommended"] == 1
    assert row["visibility_score"] > 0
    assert len(row["answer_hash"]) == 64


def test_entity_bundle_uses_brand_until_legal_identity_is_verified(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["brand"].update({
        "name": "宏图商机汇",
        "positioning": "工程采购商机平台",
        "products": ["行业筛选"],
        "audiences": ["工程企业"],
        "conversion_url": "https://example.com/mobile",
    })
    result = build_entity_assets(settings)
    graph = json.loads((tmp_path / "content" / "site-assets" / "entity-graph.jsonld").read_text(encoding="utf-8"))
    assert result["status"] == "ok"
    assert {item["@type"] for item in graph["@graph"]} == {"Brand", "WebApplication"}
    assert (tmp_path / "content" / "site-assets" / "claim-ledger.json").exists()
    assert (tmp_path / "content" / "site-assets" / "answer-capsules.md").exists()


def test_publishing_pause_is_a_hard_schedule_lock(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["publishing"]["mode"] = "live"
    settings.raw["publishing"]["paused"] = True
    with Database(settings.db_path) as db:
        result = schedule_approved_drafts(settings, db, ["zhihu"])
    assert result == {"created": 0, "status": "paused"}


def test_visibility_states_and_confidence_intervals(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["brand"]["name"] = "宏图商机汇"
    with Database(settings.db_path) as db:
        first = record_probe(db, settings, "test", "问题一", "宏图商机汇可作为候选。")
        second = record_probe(db, settings, "test", "问题二", "参考 https://example.com/page")
        snapshot = build_visibility_snapshot(settings, db)
    assert first["visibility_state"] == "mention_only"
    assert second["visibility_state"] == "citation_only"
    provider = snapshot["providers"][0]
    assert provider["mention_rate"] == 50.0
    assert provider["owned_citation_rate"] == 50.0
    assert len(provider["mention_rate_ci95"]) == 2


def test_citation_classification_and_gap_actions(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["research_sources"] = [{"url": "https://www.ccgp.gov.cn/"}]
    assert classify_citation_domain("ccgp.gov.cn", settings) == "institution"
    assert classify_citation_domain("example.com", settings) == "owned"
    assert classify_citation_domain("EXAMPLE.COM.:443", settings) == "owned"
    assert classify_citation_domain("news.zhihu.com", settings) == "forum"
    assert classify_citation_domain("foo.zhihu.com.evil", settings) == "other"
    assert classify_citation_domain("ccgp.gov.cn.evil", settings) == "other"
    with Database(settings.db_path) as db:
        seed_opportunities(settings, db)
        question = db.query("SELECT question FROM opportunities ORDER BY id LIMIT 1")[0]["question"]
        record_probe(
            db, settings, "test", question, "暂未找到适合的平台。",
            engine_surface="browser",
        )
        gaps = build_gap_analysis(settings, db)
        maturity = build_maturity_audit(settings, db)
        actions = db.query("SELECT action_type FROM geo_actions WHERE question=?", (question,))
    assert any(item["type"] == "sampling_gap" for item in gaps["actions"])
    assert actions[0]["action_type"] == "sampling_gap"
    assert 0 <= maturity["score"] <= 100
    assert maturity["note"].endswith("不代表外部 GEO 效果。")


def test_legacy_probe_backfill_and_surface_split(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["brand"]["name"] = "宏图商机汇"
    with Database(settings.db_path) as db:
        db.execute(
            """INSERT INTO probes(provider,question,answer,brand_mentioned,domain_cited,competitors_json,probed_at)
            VALUES('deepseek','旧问题','推荐宏图商机汇。',1,0,'[]','2026-09-01T00:00:00+00:00')"""
        )
        record_probe(
            db, settings, "deepseek", "新问题", "没有提到品牌。", engine_surface="browser"
        )
        snapshot = build_visibility_snapshot(settings, db)
        legacy = db.query(
            "SELECT answer_hash,engine_surface,visibility_state FROM probes WHERE question='旧问题'"
        )[0]
    assert len(legacy["answer_hash"]) == 64
    assert legacy["engine_surface"] == "legacy_unknown"
    assert legacy["visibility_state"] == "mention_only"
    assert {(row["provider"], row["engine_surface"]) for row in snapshot["providers"]} == {
        ("deepseek", "legacy_unknown"), ("deepseek", "browser")
    }


def test_entity_candidates_are_discovered_but_not_promoted_to_facts(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["brand"]["name"] = "宏图商机汇"
    with Database(settings.db_path) as db:
        record_probe(
            db,
            settings,
            "test",
            "商机平台怎么选？",
            "1. 竞品甲平台：可用于比较。\n来源：https://media.example.org/list",
        )
        result = discover_entity_candidates(settings, db)
    names = {item["name"] for item in result["candidates"]}
    assert "竞品甲平台" in names
    assert "media.example.org" in names
    assert "不会自动成为事实" in result["guardrail"]


def test_repeated_samples_in_one_context_do_not_inflate_citation_evidence(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    with Database(settings.db_path) as db:
        for sample_index in range(1, 4):
            record_probe(
                db, settings, "same-ai", "同一问题", "参考来源。",
                captured_urls=["https://media.source.test/article"], engine_surface="api",
                sample_index=sample_index, experiment_id="same-batch",
            )
        intelligence = build_citation_intelligence(settings, db)
        persisted = dict(db.query("SELECT * FROM citation_sources")[0])
    source = intelligence["sources"][0]
    assert source["sample_count"] == 3
    assert source["independent_count"] == 1
    assert source["provider_count"] == 1
    assert source["evidence_strength"] == "observed_once"
    assert source["review_status"] == "observed"
    assert persisted["independent_count"] == 1


def test_cross_engine_citation_becomes_review_candidate_not_verified_source(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    with Database(settings.db_path) as db:
        record_probe(
            db, settings, "ai-one", "问题一", "来源一",
            captured_urls=["https://industry.source.test/a"], engine_surface="api",
            experiment_id="batch-one",
        )
        record_probe(
            db, settings, "ai-two", "问题二", "来源二",
            captured_urls=["https://industry.source.test/b"], engine_surface="browser",
            experiment_id="batch-two",
        )
        intelligence = build_citation_intelligence(settings, db)
    source = intelligence["sources"][0]
    assert source["independent_count"] == 2
    assert source["provider_count"] == 2
    assert source["surface_count"] == 2
    assert source["question_count"] == 2
    assert source["evidence_strength"] == "cross_engine"
    assert source["review_status"] == "review_candidate"
    assert "不代表来源真实" in intelligence["guardrail"]


def test_citation_url_canonicalization_strips_secrets_and_rejects_unsafe_targets() -> None:
    assert canonicalize_citation_url(
        "HTTPS://Example.COM/path?a=secret#token"
    ) == "https://example.com/path"
    assert canonicalize_citation_url("https://user:pass@example.com/a") is None
    assert canonicalize_citation_url("http://example.com:8080/a") is None
    assert canonicalize_citation_url("file:///etc/passwd") is None
    assert canonicalize_citation_url("http://localhost/a") is None
    assert canonicalize_citation_url("http://127.0.0.1/a") is None
    assert canonicalize_citation_url("http://169.254.169.254/latest/meta-data") is None


def test_citation_destination_rejects_mixed_public_and_private_dns() -> None:
    def mixed_resolver(*args, **kwargs):
        return [
            (2, 1, 6, "", ("93.184.216.34", 0)),
            (2, 1, 6, "", ("10.0.0.7", 0)),
        ]

    result = validate_public_destination("https://example.com/a", resolver=mixed_resolver)
    assert result["safe"] is False
    assert result["reason"] == "dns_non_public_address"


def test_domain_allowlist_uses_label_boundary() -> None:
    assert domain_is_allowlisted("news.example.com", {"example.com"}) is True
    assert domain_is_allowlisted("example.com.attacker.test", {"example.com"}) is False


def test_citation_url_ledger_requires_repeated_allowlisted_observations(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["research_sources"] = [{"url": "https://source.test/", "title": "来源"}]
    settings.raw["citation_verification"] = {"enabled": True, "max_candidates_per_run": 10}
    calls: list[str] = []

    def fake_verify(url, current_settings):
        calls.append(url)
        return {"network_status": "verified", "http_status": 200, "content_type": "text/html", "checked_at": now_iso(), "last_error": None}

    monkeypatch.setattr("hongtu_geo.geo_engine.verify_citation_url", fake_verify)
    with Database(settings.db_path) as db:
        record_probe(
            db, settings, "ai-one", "问题一", "来源",
            captured_urls=["https://source.test/article?access_token=secret#fragment"],
            engine_surface="browser", experiment_id="batch-one",
        )
        first = build_citation_url_ledger(settings, db, verify_network=True)
        assert calls == []
        record_probe(
            db, settings, "ai-two", "问题二", "来源",
            captured_urls=["https://source.test/article?other=private"],
            engine_surface="browser", experiment_id="batch-two",
        )
        second = build_citation_url_ledger(settings, db, verify_network=True)
        stored = dict(db.query("SELECT * FROM citation_url_candidates")[0])
    assert first["candidates"][0]["review_status"] == "needs_more_observations"
    assert second["candidates"][0]["review_status"] == "verification_eligible"
    assert calls == ["https://source.test/article"]
    assert stored["canonical_url"] == "https://source.test/article"
    assert "secret" not in stored["contexts_json"]


def test_unallowlisted_citation_url_is_never_fetched(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["citation_verification"] = {"enabled": True}
    monkeypatch.setattr(
        "hongtu_geo.geo_engine.verify_citation_url",
        lambda *args, **kwargs: pytest.fail("unallowlisted URL must not be fetched"),
    )
    with Database(settings.db_path) as db:
        for provider, question in (("ai-one", "问题一"), ("ai-two", "问题二")):
            record_probe(
                db, settings, provider, question, "来源",
                captured_urls=["https://unknown-source.test/a"],
                engine_surface="browser", experiment_id=provider,
            )
        ledger = build_citation_url_ledger(settings, db, verify_network=True)
    assert ledger["candidates"][0]["review_status"] == "needs_domain_review"
    assert ledger["network_checks_performed"] == 0


def test_probe_storage_removes_url_secrets_from_answer_and_captured_urls(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    with Database(settings.db_path) as db:
        result = record_probe(
            db, settings, "test-ai", "问题", "参考 https://source.test/a?token=SECRET#frag",
            captured_urls=["https://source.test/a?access_token=PRIVATE#frag"],
        )
        stored = dict(db.query("SELECT answer,citation_urls_json,answer_hash FROM probes")[0])
    assert stored["answer"] == "参考 https://source.test/a"
    assert json.loads(stored["citation_urls_json"]) == ["https://source.test/a"]
    assert "SECRET" not in json.dumps(stored, ensure_ascii=False)
    assert "PRIVATE" not in json.dumps(stored, ensure_ascii=False)
    assert result["answer_hash"] == stored["answer_hash"]


def test_database_migration_scrubs_legacy_probe_url_secrets(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    with Database(settings.db_path) as db:
        probe = record_probe(db, settings, "test-ai", "问题", "无链接")
        db.execute(
            """UPDATE probes SET answer=?,citation_urls_json=?,domain_cited=1,
            citation_position=1,visibility_score=25,visibility_state='citation_only' WHERE id=?""",
            (
                "旧数据 https://user:pass@source.test/a?signed=SECRET#frag",
                json.dumps(["https://user:pass@source.test/a?token=PRIVATE#frag"]),
                probe["probe_id"],
            ),
        )
    with Database(settings.db_path) as reopened:
        stored = dict(reopened.query(
            """SELECT answer,citation_urls_json,domain_cited,citation_position,
            visibility_score,visibility_state FROM probes"""
        )[0])
    assert stored["answer"] == "旧数据 [已移除不安全链接]"
    assert json.loads(stored["citation_urls_json"]) == []
    assert stored["domain_cited"] == 0
    assert stored["citation_position"] is None
    assert stored["visibility_score"] == 0
    assert stored["visibility_state"] == "invisible"


def test_pinned_http_connection_uses_validated_ip_not_hostname(monkeypatch) -> None:
    calls: list[tuple] = []
    fake_socket = object()

    def fake_create_connection(address, timeout, source_address):
        calls.append((address, timeout, source_address))
        return fake_socket

    monkeypatch.setattr(citation_verifier_module.socket, "create_connection", fake_create_connection)
    connection = citation_verifier_module._PinnedHTTPConnection(
        "trusted.example", "93.184.216.34", 80, 5
    )
    connection.connect()
    assert calls == [(("93.184.216.34", 80), 5, None)]
    assert connection.sock is fake_socket


def test_citation_verifier_blocks_redirect_without_following(monkeypatch, tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["research_sources"] = [{"url": "http://source.test/"}]
    settings.raw["citation_verification"] = {"enabled": True}

    class FakeResponse:
        status = 302
        def getheader(self, name, default=""):
            return "text/html"
        def read(self, size):
            return b""

    class FakeConnection:
        def __init__(self, hostname, address, port, timeout):
            assert hostname == "source.test"
            assert address == "93.184.216.34"
        def request(self, method, path, headers):
            assert path == "/a"
        def getresponse(self):
            return FakeResponse()
        def close(self):
            pass

    monkeypatch.setattr(citation_verifier_module, "_PinnedHTTPConnection", FakeConnection)
    result = verify_citation_url(
        "http://source.test/a",
        settings,
        resolver=lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )
    assert result["network_status"] == "redirect_blocked"
    assert result["http_status"] == 302


def test_two_surfaces_of_same_provider_are_not_called_cross_engine(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    with Database(settings.db_path) as db:
        for surface in ("api", "browser"):
            record_probe(
                db, settings, "same-ai", "同一问题", "参考来源",
                captured_urls=["https://surface.source.test/a"], engine_surface=surface,
                experiment_id="same-batch",
            )
        source = build_citation_intelligence(settings, db)["sources"][0]
    assert source["independent_count"] == 2
    assert source["provider_count"] == 1
    assert source["surface_count"] == 2
    assert source["evidence_strength"] == "repeated_contexts"
    assert source["review_status"] == "observed"


def test_malformed_citation_hosts_are_rejected_before_candidate_discovery(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    malformed_url_analysis = analyze_answer(
        "参考来源",
        settings,
        captured_urls=["https://foo.com:443./a", "https://foo.com:99999/a"],
    )
    assert malformed_url_analysis["citation_domains"] == []
    with Database(settings.db_path) as db:
        probe = record_probe(db, settings, "test-ai", "来源问题", "参考来源")
        db.execute(
            "UPDATE probes SET citation_domains_json=? WHERE id=?",
            (
                json.dumps([
                    "foo..bar.com", "bad,host.com", "foo.com..", "foo.com..:443",
                    "foo.com:443.", "valid.example.org",
                ]),
                probe["probe_id"],
            ),
        )
        intelligence = build_citation_intelligence(settings, db)
        candidates = discover_entity_candidates(settings, db)
    assert [item["domain"] for item in intelligence["sources"]] == ["valid.example.org"]
    assert intelligence["invalid_domain_entries"] == 5
    candidate_names = {item["name"] for item in candidates["candidates"]}
    assert "valid.example.org" in candidate_names
    assert "foo..bar.com" not in candidate_names
    assert "bad,host.com" not in candidate_names


def test_entity_candidate_confidence_uses_independent_contexts_not_repeat_count(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    with Database(settings.db_path) as db:
        for sample_index in range(1, 4):
            record_probe(
                db, settings, "same-ai", "同一问题", "1. 竞品乙平台：作为比较对象。",
                engine_surface="api", sample_index=sample_index, experiment_id="same-batch",
            )
        first = discover_entity_candidates(settings, db)
        record_probe(
            db, settings, "other-ai", "另一个问题", "1. 竞品乙平台：作为比较对象。",
            engine_surface="browser", experiment_id="other-batch",
        )
        second = discover_entity_candidates(settings, db)
    first_candidate = next(item for item in first["candidates"] if item["name"] == "竞品乙平台")
    second_candidate = next(item for item in second["candidates"] if item["name"] == "竞品乙平台")
    assert first_candidate["sample_count"] == 3
    assert first_candidate["independent_count"] == 1
    assert first_candidate["evidence_strength"] == "observed_once"
    assert second_candidate["independent_count"] == 2
    assert second_candidate["provider_count"] == 2
    assert second_candidate["status"] == "review_candidate"
    assert second_candidate["evidence_strength"] == "cross_engine"


def test_attribution_is_idempotent_and_strips_personal_data(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    payload = {
        "event_id": "evt-1",
        "event_type": "inquiry",
        "source_engine": "deepseek",
        "landing_url": "https://example.com/mobile?phone=13812345678#private",
        "anonymous_id": ANON_A,
        "metadata": {
            "page": "hall", "phone": "13812345678", "message": "private",
            "手机号": "13912345678", "notes": "联系 13712345678",
            "full_name": "张三", "custom": {"phone": "13612345678"},
        },
    }
    with Database(settings.db_path) as db:
        first = record_attribution_event(settings, db, payload)
        second = record_attribution_event(settings, db, payload)
        stored = db.query("SELECT anonymous_id_hash,metadata_json,landing_url FROM attribution_events")[0]
        report = build_attribution_report(settings, db)
    assert first["id"] == second["id"]
    assert len(stored["anonymous_id_hash"]) == 64
    assert ANON_A not in stored["anonymous_id_hash"]
    assert json.loads(stored["metadata_json"]) == {"page": "hall"}
    assert stored["landing_url"] == "https://example.com/mobile"
    assert (tmp_path / "data" / "attribution-salt.key").exists()
    assert report["events"] == 1
    assert report["funnel"]["inquiry"] == 1


def test_attribution_rejects_personal_anonymous_id_and_strips_sensitive_url_path(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    with Database(settings.db_path) as db:
        with pytest.raises(ValueError, match="第一方随机 UUID"):
            record_attribution_event(settings, db, {
                "event_id": "pii-subject", "event_type": "inquiry", "anonymous_id": "13812345678",
            })
        with pytest.raises(ValueError, match="第一方随机 UUID"):
            record_attribution_event(settings, db, {
                "event_id": "wechat-subject", "event_type": "inquiry", "anonymous_id": "wxid_abcd1234",
            })
        record_attribution_event(settings, db, {
            "event_id": "safe-subject", "event_type": "inquiry", "anonymous_id": ANON_A,
            "landing_url": "https://example.com/contact/13812345678?campaign=safe",
            "metadata": {"page": "张三", "region": "wxid_abcd1234"},
            "utm_source": "张三", "utm_medium": "聊天内容", "utm_campaign": "membrane-geo",
        })
        stored = db.query(
            "SELECT landing_url,metadata_json,utm_source,utm_medium,utm_campaign FROM attribution_events"
        )[0]
    assert stored["landing_url"] == "https://example.com"
    assert json.loads(stored["metadata_json"]) == {}
    assert stored["utm_source"] == ""
    assert stored["utm_medium"] == ""
    assert stored["utm_campaign"] == "membrane-geo"


def test_attribution_conversion_uses_same_anonymous_subject(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    with Database(settings.db_path) as db:
        record_attribution_event(settings, db, {
            "event_id": "ref-1", "event_type": "ai_referral", "source_engine": "chatgpt", "anonymous_id": ANON_A,
        })
        record_attribution_event(settings, db, {
            "event_id": "signup-1", "event_type": "signup", "source_engine": "chatgpt", "anonymous_id": ANON_A,
        })
        record_attribution_event(settings, db, {
            "event_id": "signup-2", "event_type": "signup", "source_engine": "unknown", "anonymous_id": ANON_B,
        })
        report = build_attribution_report(settings, db)
    assert report["events"] == 3
    assert report["unique_observed_subjects"] == 2
    assert report["unique_attributed_subjects"] == 1
    assert report["referral_to_signup_rate"] == 100.0


def test_attribution_rejects_conflicting_idempotency_key_and_ambiguous_time(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    with Database(settings.db_path) as db:
        first = record_attribution_event(settings, db, {
            "event_id": "stable-event", "event_type": "inquiry",
            "occurred_at": "2026-09-01T10:00:00+08:00", "anonymous_id": ANON_A,
        })
        replay = record_attribution_event(settings, db, {
            "event_id": "stable-event", "event_type": "inquiry",
            "occurred_at": "2026-09-01T02:00:00Z", "anonymous_id": ANON_A,
        })
        with pytest.raises(ValueError, match="载荷不一致"):
            record_attribution_event(settings, db, {
                "event_id": "stable-event", "event_type": "won",
                "occurred_at": "2026-09-01T02:00:00Z", "anonymous_id": ANON_A,
            })
        with pytest.raises(ValueError, match="包含时区"):
            record_attribution_event(settings, db, {
                "event_id": "ambiguous-time", "event_type": "inquiry",
                "occurred_at": "2026-09-01T10:00:00", "anonymous_id": ANON_A,
            })
        with pytest.raises(ValueError, match="event_id 为必填"):
            record_attribution_event(settings, db, {
                "event_type": "inquiry", "anonymous_id": ANON_A,
            })
        count = db.query("SELECT COUNT(*) n FROM attribution_events")[0]["n"]
    assert first["id"] == replay["id"]
    assert first["occurred_at"] == "2026-09-01T02:00:00+00:00"
    assert count == 1


def test_attribution_reports_first_last_touch_quality_and_conversion_delay(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    events = [
        ("ref-chatgpt", "2026-01-01T00:00:00Z", "ai_referral", "chatgpt", None),
        ("ref-deepseek", "2026-01-02T00:00:00Z", "ai_referral", "deepseek", None),
        ("inquiry", "2026-01-03T00:00:00Z", "inquiry", "unknown", None),
        ("won", "2026-01-11T00:00:00Z", "won", "unknown", 1200),
    ]
    with Database(settings.db_path) as db:
        for event_id, occurred_at, event_type, engine, value in events:
            record_attribution_event(settings, db, {
                "event_id": event_id, "occurred_at": occurred_at, "event_type": event_type,
                "source_engine": engine, "anonymous_id": ANON_A, "value": value,
            })
        report = build_attribution_report(settings, db)
    assert report["data_quality"]["status"] == "collecting"
    assert report["attributable_funnel"]["inquiry"] == 1
    assert report["attributable_funnel"]["won"] == 1
    assert report["attribution_models"]["first_touch"]["chatgpt"] == {"won": 1, "value": 1200.0}
    assert report["attribution_models"]["last_touch"]["deepseek"] == {"won": 1, "value": 1200.0}
    assert report["time_to_conversion_days"] == {"median_to_inquiry": 2.0, "median_to_won": 10.0}


def test_attribution_quality_flags_orphans_duplicates_and_stage_regression(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    with Database(settings.db_path) as db:
        for event_id, occurred_at, event_type in (
            ("orphan-won", "2026-01-01T00:00:00Z", "won"),
            ("orphan-signup", "2026-01-02T00:00:00Z", "signup"),
            ("orphan-signup-2", "2026-01-03T00:00:00Z", "signup"),
        ):
            record_attribution_event(settings, db, {
                "event_id": event_id, "occurred_at": occurred_at, "event_type": event_type,
                "anonymous_id": ANON_B,
            })
        report = build_attribution_report(settings, db)
    assert report["data_quality"]["status"] == "needs_attention"
    assert report["data_quality"]["orphan_downstream_subjects"] == 1
    assert report["data_quality"]["duplicate_stage_events"] == 1
    assert report["data_quality"]["out_of_order_subjects"] == 1


def test_attribution_does_not_credit_downstream_event_before_ai_touch(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    with Database(settings.db_path) as db:
        record_attribution_event(settings, db, {
            "event_id": "early-inquiry", "occurred_at": "2026-01-01T00:00:00Z",
            "event_type": "inquiry", "anonymous_id": ANON_A,
        })
        record_attribution_event(settings, db, {
            "event_id": "late-referral", "occurred_at": "2026-01-02T00:00:00Z",
            "event_type": "ai_referral", "source_engine": "deepseek", "anonymous_id": ANON_A,
        })
        report = build_attribution_report(settings, db)
    assert report["funnel"]["inquiry"] == 1
    assert report["attributable_funnel"]["inquiry"] == 0
    assert report["referral_to_inquiry_rate"] == 0.0
    assert report["data_quality"]["out_of_order_subjects"] == 1
    assert report["data_quality"]["status"] == "needs_attention"


def test_attribution_referral_without_anonymous_uuid_stays_raw_only(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    with Database(settings.db_path) as db:
        record_attribution_event(settings, db, {
            "event_id": "anonymous-missing", "event_type": "ai_referral", "source_engine": "deepseek",
        })
        report = build_attribution_report(settings, db)
    assert report["funnel"]["ai_referral"] == 1
    assert report["attributable_funnel"]["ai_referral"] == 0
    assert report["unique_attributed_subjects"] == 0
    assert report["referral_to_inquiry_rate"] is None
    assert report["data_quality"]["anonymous_missing_events"] == 1


def test_attribution_excludes_future_events_and_nonfinite_value_from_metrics(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    with Database(settings.db_path) as db:
        with pytest.raises(ValueError, match="value 超出"):
            record_attribution_event(settings, db, {
                "event_id": "nan-value", "event_type": "won", "value": float("nan"),
            })
        record_attribution_event(settings, db, {
            "event_id": "future-win", "event_type": "won", "anonymous_id": ANON_C,
            "occurred_at": "2099-01-01T00:00:00Z", "value": 999,
        })
        report = build_attribution_report(settings, db)
    assert report["events"] == 1
    assert report["valid_events"] == 0
    assert report["funnel"]["won"] == 0
    assert report["data_quality"]["future_events"] == 1
    assert report["data_quality"]["status"] == "needs_attention"


def test_attribution_report_quarantines_legacy_unknown_event_type(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    with Database(settings.db_path) as db:
        db.execute(
            """INSERT INTO attribution_events(
            event_id,occurred_at,event_type,source_engine,created_at
            ) VALUES(?,?,?,?,?)""",
            ("legacy-unknown", "2026-01-01T00:00:00+00:00", "mystery", "unknown", now_iso()),
        )
        report = build_attribution_report(settings, db)
    assert report["events"] == 1
    assert report["valid_events"] == 0
    assert report["data_quality"]["invalid_event_type_events"] == 1
    assert report["data_quality"]["status"] == "needs_attention"


def test_attribution_report_quarantines_legacy_invalid_value_without_crashing(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    with Database(settings.db_path) as db:
        db.execute(
            """INSERT INTO attribution_events(
            event_id,occurred_at,event_type,source_engine,value,created_at
            ) VALUES(?,?,?,?,?,?)""",
            ("legacy-value", "2026-01-01T00:00:00+00:00", "won", "unknown", "not-a-number", now_iso()),
        )
        report = build_attribution_report(settings, db)
    assert report["events"] == 1
    assert report["valid_events"] == 1
    assert report["engines"]["unknown"]["value"] == 0.0
    assert report["data_quality"]["invalid_value_events"] == 1
    assert report["data_quality"]["status"] == "needs_attention"


def test_run_probes_repeats_samples_captures_citations_and_obeys_cap(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["brand"]["name"] = "宏图商机汇"
    settings.raw["monitor"].update({
        "live_samples_per_prompt": 2,
        "max_calls_per_run": 3,
        "providers": [{
            "name": "fake-ai", "kind": "responses", "model": "fake-model",
            "api_key_env": "FAKE_AI_KEY", "enabled": True,
        }],
    })

    class FakeAdapter:
        surface = "api"

        def ask(self, prompt: str) -> ProviderAnswer:
            assert "中立、可核查" in prompt
            return ProviderAnswer(
                "宏图商机汇可作为候选。",
                ["https://example.com/source"],
                {"response_id": "fake-response"},
            )

    monkeypatch.setenv("FAKE_AI_KEY", "test-key")
    monkeypatch.setattr("hongtu_geo.pipeline.build_provider_adapter", lambda provider, key: FakeAdapter())
    with Database(settings.db_path) as db:
        seed_opportunities(settings, db)
        result = run_probes(settings, db, limit=2)
        rows = db.query(
            "SELECT provider,sample_index,engine_surface,experiment_id,citation_urls_json,raw_metadata_json,"
            "prompt_variant,prompt_version "
            "FROM probes ORDER BY id"
        )
        batch = dict(db.query("SELECT * FROM probe_batches")[0])
        items = [dict(row) for row in db.query("SELECT * FROM probe_batch_items ORDER BY id")]

    assert result["calls_attempted"] == 3
    assert result["truncated_by_call_cap"] is True
    assert [row["sample_index"] for row in rows] == [1, 2, 1]
    assert all(row["provider"] == "fake-ai" and row["engine_surface"] == "api" for row in rows)
    assert all(row["experiment_id"] == result["batch_id"] for row in rows)
    assert json.loads(rows[0]["citation_urls_json"]) == ["https://example.com/source"]
    assert json.loads(rows[0]["raw_metadata_json"])["response_id"] == "fake-response"
    assert json.loads(rows[0]["raw_metadata_json"])["batch_id"] == result["batch_id"]
    assert batch["status"] == "completed"
    assert batch["planned_calls"] == 3
    assert batch["attempted_calls"] == 3
    assert batch["succeeded_calls"] == 3
    assert batch["failed_calls"] == 0
    assert len(items) == 3
    assert all(item["status"] == "succeeded" and item["probe_id"] for item in items)
    assert all(item["prompt_variant"] == "source_requested" for item in items)
    assert all(item["prompt_version"] == "source-requested-v1" for item in items)
    assert all(row["prompt_variant"] == "source_requested" for row in rows)


def test_browser_probes_use_fresh_page_per_repeat_and_persist_manifest(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["brand"]["name"] = "宏图商机汇"
    settings.raw["acquisition"] = {"priority_questions": ["膜结构工程商机去哪里获取？"]}
    settings.raw["monitor"].update({
        "browser_samples_per_prompt": 3,
        "browser_max_calls_per_run": 3,
        "max_item_attempts": 3,
    })
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "platforms.json").write_text(json.dumps({
        "deepseek": {
            "name": "DeepSeek AI 监测", "kind": "ai_probe",
            "studio_url": "https://chat.deepseek.com/",
            "logged_in_url_contains": ["chat.deepseek.com/"],
            "input_locators": ["textarea"], "answer_locators": [".answer"],
        }
    }, ensure_ascii=False), encoding="utf-8")
    with Database(settings.db_path) as db:
        db.execute(
            """INSERT INTO platform_accounts(platform,display_name,profile_dir,status)
            VALUES(?,?,?,'connected')""",
            ("deepseek", "DeepSeek AI 监测", str(tmp_path / "profile")),
        )
        seed_opportunities(settings, db)

    class FakePage:
        def __init__(self):
            self.url = "about:blank"
            self.visits = []

        def goto(self, url, **kwargs):
            self.url = url
            self.visits.append(url)

        def wait_for_timeout(self, value):
            return None

    page = FakePage()

    class FakeContext:
        pages = [page]

        def close(self):
            return None

    class FakeChromium:
        def launch_persistent_context(self, **kwargs):
            return FakeContext()

    class FakePlaywright:
        chromium = FakeChromium()

    class FakeManager:
        def __enter__(self):
            return FakePlaywright()

        def __exit__(self, *args):
            return None

    captured_prompts = []

    def fake_capture(fake_page, spec, prompt):
        captured_prompts.append(prompt)
        return ("宏图商机汇可作为工程采购信息筛选入口，具体信息应核验公开来源。", ["https://source.example/item"])

    monkeypatch.setattr("hongtu_geo.browser._sync_playwright_context", lambda: FakeManager())
    monkeypatch.setattr("hongtu_geo.browser._capture_browser_answer", fake_capture)
    def fake_prepare(fake_page, spec):
        fake_page.goto(spec["studio_url"])
        return {
            "fresh_context_verified": True, "fresh_context_method": "test",
            "initial_answer_count": 0, "empty_answer_count": 0,
            "entry_origin": "https://chat.deepseek.com",
        }

    monkeypatch.setattr("hongtu_geo.browser._prepare_fresh_ai_conversation", fake_prepare)
    monkeypatch.setattr("hongtu_geo.browser.profile_path", lambda settings, platform: tmp_path / "profile")
    result = run_browser_probes(settings, limit=1, providers=["deepseek"])
    with Database(settings.db_path) as db:
        batch = dict(db.query("SELECT * FROM probe_batches")[0])
        items = [dict(row) for row in db.query("SELECT * FROM probe_batch_items ORDER BY id")]
        probes = [dict(row) for row in db.query("SELECT * FROM probes ORDER BY id")]
        browser_events = [dict(row) for row in db.query(
            "SELECT platform,action,trigger,visible,status FROM browser_launch_events"
        )]

    assert result["status"] == "completed"
    assert len(page.visits) == 3
    assert len(captured_prompts) == 3
    assert captured_prompts == ["膜结构工程商机去哪里获取？"] * 3
    assert batch["status"] == "completed"
    assert json.loads(batch["config_json"])["engine_surface"] == "browser"
    assert [item["sample_index"] for item in items] == [1, 2, 3]
    assert all(item["engine_surface"] == "browser" and item["status"] == "succeeded" for item in items)
    assert [probe["sample_index"] for probe in probes] == [1, 2, 3]
    assert all(probe["experiment_id"] == result["batch_id"] for probe in probes)
    assert all(probe["prompt_variant"] == "naturalistic" for probe in probes)
    assert all(probe["prompt_version"] == "naturalistic-v1" for probe in probes)
    assert all(json.loads(probe["raw_metadata_json"])["fresh_context_verified"] for probe in probes)
    assert browser_events == [{
        "platform": "deepseek", "action": "ai_probe", "trigger": "automation",
        "visible": 0, "status": "completed",
    }]


def test_prepare_fresh_conversation_proves_empty_page() -> None:
    class CountLocator:
        def count(self):
            return 0

    class Page:
        url = "about:blank"

        def goto(self, url, **kwargs):
            self.url = url

        def wait_for_timeout(self, value):
            return None

        def locator(self, selector):
            return CountLocator()

    result = _prepare_fresh_ai_conversation(Page(), {
        "studio_url": "https://chat.deepseek.com/",
        "new_chat_url": "https://chat.deepseek.com/",
        "logged_in_url_contains": ["chat.deepseek.com/"],
        "answer_locators": [".answer"],
    })
    assert result == {
        "fresh_context_verified": True,
        "fresh_context_method": "new_chat_url",
        "initial_answer_count": 0,
        "empty_answer_count": 0,
        "entry_origin": "https://chat.deepseek.com",
    }


def test_prepare_fresh_conversation_never_persists_session_path() -> None:
    class CountLocator:
        def count(self):
            return 0

    class RedirectedPage:
        url = "about:blank"

        def goto(self, url, **kwargs):
            self.url = "https://chat.deepseek.com/a/chat/secret-session-id?token=private#fragment"

        def wait_for_timeout(self, value):
            return None

        def locator(self, selector):
            return CountLocator()

    result = _prepare_fresh_ai_conversation(RedirectedPage(), {
        "studio_url": "https://chat.deepseek.com/",
        "logged_in_url_contains": ["chat.deepseek.com/"],
        "answer_locators": [".answer"],
    })
    assert result["entry_origin"] == "https://chat.deepseek.com"
    assert "secret-session-id" not in json.dumps(result)


def test_prepare_fresh_conversation_rejects_unremovable_history() -> None:
    class CountLocator:
        def count(self):
            return 2

    class Page:
        url = "https://chat.deepseek.com/"

        def goto(self, url, **kwargs):
            self.url = url

        def wait_for_timeout(self, value):
            return None

        def locator(self, selector):
            return CountLocator()

    with pytest.raises(RuntimeError, match="会话隔离校验失败"):
        _prepare_fresh_ai_conversation(Page(), {
            "studio_url": "https://chat.deepseek.com/",
            "logged_in_url_contains": ["chat.deepseek.com/"],
            "answer_locators": [".answer"],
            "new_chat_locators": [],
        })


def test_capture_rechecks_empty_context_immediately_before_send() -> None:
    class Editor:
        first = None

        def __init__(self):
            self.first = self
            self.sent = False

        def is_visible(self, timeout):
            return True

        def fill(self, value):
            self.sent = True

        def press(self, key):
            self.sent = True

    class ExistingAnswers:
        def count(self):
            return 1

    editor = Editor()

    class Page:
        def locator(self, selector):
            return editor if selector == "textarea" else ExistingAnswers()

    with pytest.raises(RuntimeError, match="发送前重新出现"):
        _capture_browser_answer(Page(), {
            "input_locators": ["textarea"], "answer_locators": [".answer"],
        }, "测试问题")
    assert editor.sent is False


def test_context_isolation_health_does_not_backfill_legacy_samples(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    with Database(settings.db_path) as db:
        record_probe(db, settings, "deepseek", "旧问题", "旧回答", engine_surface="browser")
        record_probe(
            db, settings, "deepseek", "新问题", "新回答", engine_surface="browser",
            raw_metadata={"fresh_context_verified": True},
        )
        health = build_context_isolation_health(db)
    assert health["status"] == "legacy_unverified"
    assert health["browser_samples"] == 2
    assert health["fresh_context_verified_samples"] == 1
    assert health["legacy_unverified_samples"] == 1


@pytest.mark.parametrize("bad_metadata", ["{broken", "null", "12345"])
def test_context_isolation_health_quarantines_malformed_metadata(
    tmp_path: Path, bad_metadata: str,
) -> None:
    settings = make_settings(tmp_path, verified=True)
    with Database(settings.db_path) as db:
        record_probe(db, settings, "deepseek", "问题", "回答", engine_surface="browser")
        db.execute("UPDATE probes SET raw_metadata_json=?", (bad_metadata,))
        health = build_context_isolation_health(db)
    assert health["malformed_metadata_samples"] == 1
    assert health["fresh_context_verified_samples"] == 0


def test_database_migration_removes_browser_session_paths_and_full_urls(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    with Database(settings.db_path) as db:
        record_probe(
            db, settings, "deepseek", "问题", "回答", engine_surface="browser",
            raw_metadata={
                "fresh_context_verified": True,
                "entry_origin_path": "https://chat.deepseek.com/a/chat/secret-id",
                "conversation_url": "https://chat.deepseek.com/a/chat/secret-id?token=private",
            },
        )
    with Database(settings.db_path) as db:
        metadata = json.loads(db.query("SELECT raw_metadata_json FROM probes")[0]["raw_metadata_json"])
    serialized = json.dumps(metadata)
    assert metadata["entry_origin"] == "https://chat.deepseek.com"
    assert len(metadata["conversation_url_sha256"]) == 64
    assert "entry_origin_path" not in metadata
    assert "conversation_url" not in metadata
    assert "secret-id" not in serialized
    assert "private" not in serialized


@pytest.mark.parametrize("empty_value", [None, ""])
def test_database_migration_removes_empty_legacy_browser_url_keys(
    tmp_path: Path, empty_value,
) -> None:
    settings = make_settings(tmp_path, verified=True)
    with Database(settings.db_path) as db:
        record_probe(
            db, settings, "deepseek", "问题", "回答", engine_surface="browser",
            raw_metadata={"entry_origin_path": empty_value, "conversation_url": empty_value},
        )
    with Database(settings.db_path) as db:
        metadata = json.loads(db.query("SELECT raw_metadata_json FROM probes")[0]["raw_metadata_json"])
    assert "entry_origin_path" not in metadata
    assert "conversation_url" not in metadata


def test_database_migration_classifies_only_known_prompt_templates(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    timestamp = "2026-09-04T00:00:00+00:00"
    question = "膜结构工程商机去哪里获取？"
    with Database(settings.db_path) as db:
        db.execute(
            "INSERT INTO probe_batches(batch_id,started_at,status) VALUES(?,?,?)",
            ("migration-batch", timestamp, "completed"),
        )
        cursor = db.execute(
            """INSERT INTO probe_batch_items(
            batch_id,provider,question,prompt,sample_index,status,created_at,updated_at,
            engine_surface,prompt_variant,prompt_version)
            VALUES(?,?,?,?,?,'succeeded',?,?,?,?,?)""",
            (
                "migration-batch", "deepseek", question, "完全未知的历史改写", 1,
                timestamp, timestamp, "browser", "source_requested", "source-requested-v1",
            ),
        )
        item_id = int(cursor.lastrowid)
        record_probe(
            db, settings, "deepseek", question, "回答", engine_surface="browser",
            prompt_variant="source_requested", prompt_version="source-requested-v1",
            experiment_id="migration-batch", batch_item_id=item_id,
        )
    with Database(settings.db_path) as db:
        item = dict(db.query(
            "SELECT prompt_variant,prompt_version FROM probe_batch_items WHERE id=?", (item_id,)
        )[0])
        probe = dict(db.query(
            "SELECT prompt_variant,prompt_version FROM probes WHERE batch_item_id=?", (item_id,)
        )[0])
    assert item == {"prompt_variant": "legacy", "prompt_version": "legacy"}
    assert probe == {"prompt_variant": "legacy", "prompt_version": "legacy"}


def test_browser_cli_path_reconciles_stale_batch_before_login_check(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["monitor"].update({"stale_batch_hours": 1, "max_item_attempts": 3})
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "platforms.json").write_text("{}", encoding="utf-8")
    stale = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    with Database(settings.db_path) as db:
        db.execute(
            """INSERT INTO probe_batches(batch_id,started_at,status,planned_calls,config_json)
            VALUES(?,?,'running',1,'{"engine_surface":"browser"}')""",
            ("stale-browser", stale),
        )
        db.execute(
            """INSERT INTO probe_batch_items(batch_id,provider,question,prompt,sample_index,
            status,attempts,created_at,updated_at,engine_surface)
            VALUES('stale-browser','deepseek','问题','问题',1,'running',1,?,?, 'browser')""",
            (stale, stale),
        )
    result = run_browser_probes(settings, limit=1)
    with Database(settings.db_path) as db:
        batch = dict(db.query("SELECT status FROM probe_batches WHERE batch_id='stale-browser'")[0])
        item = dict(db.query("SELECT status,last_error FROM probe_batch_items")[0])
    assert result["status"] == "waiting_for_ai_login"
    assert result["recovered_stale_batches"] == ["stale-browser"]
    assert batch["status"] == "interrupted"
    assert item["status"] == "failed"
    assert "watchdog" in item["last_error"]


def test_browser_question_scheduler_rotates_to_least_measured_priority(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["acquisition"] = {"priority_questions": ["问题甲", "问题乙", "问题丙"]}
    with Database(settings.db_path) as db:
        record_probe(db, settings, "deepseek", "问题甲", "回答", engine_surface="browser")
        record_probe(db, settings, "deepseek", "问题乙", "回答", engine_surface="browser")
        record_probe(db, settings, "deepseek", "问题乙", "回答", engine_surface="browser")
        selected = _select_browser_probe_questions(settings, db, 2)
    assert selected == ["问题丙", "问题甲"]


def test_browser_question_scheduler_ranks_repeat_depth_not_lifetime_rows(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["acquisition"] = {"priority_questions": ["完整重复", "拆分重复"]}
    with Database(settings.db_path) as db:
        for index in range(1, 4):
            record_probe(
                db, settings, "deepseek", "完整重复", f"完整回答 {index}",
                engine_surface="browser", sample_index=index, experiment_id="complete-batch",
            )
        record_probe(
            db, settings, "deepseek", "拆分重复", "第一条",
            engine_surface="browser", sample_index=1, experiment_id="partial-batch",
        )
        record_probe(
            db, settings, "deepseek", "拆分重复", "第三条",
            engine_surface="browser", sample_index=3, experiment_id="partial-batch",
        )
        record_probe(
            db, settings, "deepseek", "拆分重复", "单独补测",
            engine_surface="browser", sample_index=1, experiment_id="supplement-batch",
        )
        selected = _select_browser_probe_questions(settings, db, 1)
    assert selected == ["拆分重复"]


def test_browser_question_scheduler_finishes_core_before_unmeasured_backlog(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["acquisition"] = {"priority_questions": ["核心问题"]}
    settings.raw["geo_goals"] = {"brand_reputation": {"target_questions": []}}
    with Database(settings.db_path) as db:
        seed_opportunities(settings, db)
        for index in (1, 3):
            record_probe(
                db, settings, "deepseek", "核心问题", f"核心回答 {index}",
                engine_surface="browser", sample_index=index, experiment_id="partial-core",
            )
        selected = _select_browser_probe_questions(settings, db, 1)
    assert selected == ["核心问题"]


def test_browser_question_scheduler_requires_baseline_on_each_connected_engine(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["acquisition"] = {"priority_questions": ["核心问题"]}
    settings.raw["geo_goals"] = {"brand_reputation": {"target_questions": []}}
    with Database(settings.db_path) as db:
        seed_opportunities(settings, db)
        for index in range(1, 4):
            record_probe(
                db, settings, "deepseek", "核心问题", f"回答 {index}",
                engine_surface="browser", sample_index=index,
                experiment_id="deepseek-complete",
            )
        selected = _select_browser_probe_questions(
            settings, db, 1, providers=["deepseek", "kimi"],
        )
    assert selected == ["核心问题"]


def test_browser_question_scheduler_reserves_longitudinal_core_slot(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["acquisition"] = {"priority_questions": ["核心问题"]}
    settings.raw["geo_goals"] = {"brand_reputation": {"target_questions": []}}
    settings.raw["monitor"]["longitudinal_core_questions_per_run"] = 1
    with Database(settings.db_path) as db:
        seed_opportunities(settings, db)
        for index in range(1, 4):
            record_probe(
                db, settings, "deepseek", "核心问题", f"回答 {index}",
                engine_surface="browser", sample_index=index,
                experiment_id="first-complete-run",
            )
            record_probe(
                db, settings, "kimi", "核心问题", f"回答 {index}",
                engine_surface="browser", sample_index=index,
                experiment_id="first-complete-kimi-run",
            )
        selected = _select_browser_probe_questions(
            settings, db, 2, providers=["deepseek"],
        )
    assert selected[0] == "核心问题"
    assert len(selected) == 2
    assert selected[1] != "核心问题"


def test_browser_question_scheduler_alternates_when_budget_fits_one_question(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["acquisition"] = {"priority_questions": ["核心问题"]}
    settings.raw["geo_goals"] = {"brand_reputation": {"target_questions": []}}
    with Database(settings.db_path) as db:
        seed_opportunities(settings, db)
        for index in range(1, 4):
            record_probe(
                db, settings, "deepseek", "核心问题", f"回答 {index}",
                engine_surface="browser", sample_index=index,
                experiment_id="first-complete-run",
            )
            record_probe(
                db, settings, "kimi", "核心问题", f"回答 {index}",
                engine_surface="browser", sample_index=index,
                experiment_id="first-complete-kimi-run",
            )
        discovery_first = _select_browser_probe_questions(
            settings, db, 2, providers=["deepseek", "kimi"],
            samples_per_question=3, call_cap=6,
        )
        db.execute(
            """INSERT INTO probe_batches(batch_id,started_at,status,config_json)
            VALUES('prior-browser-run',?,'completed','{"engine_surface":"browser"}')""",
            (now_iso(),),
        )
        db.execute(
            """INSERT INTO probe_batches(batch_id,started_at,status,config_json)
            VALUES('malformed-history',?,'completed','{broken')""",
            (now_iso(),),
        )
        longitudinal_first = _select_browser_probe_questions(
            settings, db, 2, providers=["deepseek", "kimi"],
            samples_per_question=3, call_cap=6,
        )
    assert discovery_first[0] != "核心问题"
    assert longitudinal_first[0] == "核心问题"


def test_browser_question_scheduler_honors_exact_question_override(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["acquisition"] = {"priority_questions": ["默认问题"]}
    with Database(settings.db_path) as db:
        selected = _select_browser_probe_questions(
            settings, db, 2, [" 膜结构工程商机去哪里获取？ ", "宏图商机汇怎么样？", "重复之外"],
        )
    assert selected == ["膜结构工程商机去哪里获取？", "宏图商机汇怎么样？"]


def test_browser_resume_only_matches_requested_question_and_variant() -> None:
    config = {
        "engine_surface": "browser",
        "questions": ["长尾问题"],
        "prompt_variant": "naturalistic",
    }
    assert _browser_batch_matches_request(config, None, "naturalistic")
    assert not _browser_batch_matches_request(config, ["核心问题"], "naturalistic")
    assert not _browser_batch_matches_request(config, None, "source_requested")


def test_browser_probe_plan_round_robins_engines_before_next_repeat() -> None:
    plan = _build_browser_probe_plan(
        "batch", ["deepseek", "kimi"], ["核心问题", "次要问题"],
        3, 5, "2026-09-05T00:00:00+00:00", "naturalistic",
    )
    assert [(row[1], row[2], row[4]) for row in plan] == [
        ("deepseek", "核心问题", 1),
        ("kimi", "核心问题", 1),
        ("deepseek", "核心问题", 2),
        ("kimi", "核心问题", 2),
        ("deepseek", "核心问题", 3),
    ]


def test_browser_probe_call_cap_resumes_same_manifest_without_provider_starvation(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path, verified=True)
    timestamp = "2026-09-05T00:00:00+00:00"
    plan = _build_browser_probe_plan(
        "batch", ["deepseek", "kimi"], ["核心问题"],
        3, 100, timestamp, "naturalistic",
    )
    with Database(settings.db_path) as db:
        db.execute(
            "INSERT INTO probe_batches(batch_id,started_at,status,planned_calls) VALUES(?,?,'partial',?)",
            ("batch", timestamp, len(plan)),
        )
        db.executemany(
            """INSERT INTO probe_batch_items(batch_id,provider,question,prompt,sample_index,
            created_at,updated_at,prompt_variant,prompt_version,engine_surface)
            VALUES(?,?,?,?,?,?,?,?,?,'browser')""",
            plan,
        )
        observed = []
        for _ in range(6):
            ids = _next_browser_batch_item_ids(
                db, "batch", ["deepseek", "kimi"], 1, 3
            )
            assert len(ids) == 1
            item_id = next(iter(ids))
            row = db.query(
                "SELECT provider,sample_index FROM probe_batch_items WHERE id=?", (item_id,)
            )[0]
            observed.append((row["provider"], row["sample_index"]))
            db.execute(
                "UPDATE probe_batch_items SET status='succeeded' WHERE id=?", (item_id,)
            )
    assert observed == [
        ("deepseek", 1), ("kimi", 1),
        ("deepseek", 2), ("kimi", 2),
        ("deepseek", 3), ("kimi", 3),
    ]


def test_ai_temporary_restriction_parses_china_time_to_utc() -> None:
    restriction = _parse_ai_temporary_restriction(
        "由于违反用户使用规范，你的账号已被禁言至 2026 年 9 月 6 日 12:48。"
    )
    assert restriction is not None
    message, retry_after = restriction
    assert "暂时受限" in message
    assert retry_after == "2026-09-06T04:48:00+00:00"
    assert _parse_ai_temporary_restriction("我能帮什么忙吗？") is None


def test_browser_probe_reports_retry_and_other_engine_login_together(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "platforms.json").write_text(json.dumps({
        "deepseek": {"name": "DeepSeek", "kind": "ai_probe"},
        "kimi": {"name": "Kimi", "kind": "ai_probe"},
    }), encoding="utf-8")
    retry_after = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    with Database(settings.db_path) as db:
        db.execute(
            """INSERT INTO platform_accounts(platform,display_name,profile_dir,status,retry_after)
            VALUES(?,?,?,'temporarily_unavailable',?)""",
            ("deepseek", "DeepSeek", str(tmp_path / "deepseek"), retry_after),
        )
        db.execute(
            """INSERT INTO platform_accounts(platform,display_name,profile_dir,status)
            VALUES(?,?,?,'not_connected')""",
            ("kimi", "Kimi", str(tmp_path / "kimi")),
        )
    result = run_browser_probes(settings, limit=1)
    assert result["status"] == "waiting_for_ai_login_and_retry"
    assert [item["platform"] for item in result["retry"]] == ["deepseek"]
    assert [item["platform"] for item in result["login_required"]] == ["kimi"]


def test_browser_probe_defers_temporary_restriction_without_consuming_attempt(
    tmp_path: Path, monkeypatch,
) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["acquisition"] = {"priority_questions": ["核心问题"]}
    settings.raw["monitor"].update({
        "browser_samples_per_prompt": 1,
        "browser_max_calls_per_run": 1,
        "max_item_attempts": 3,
    })
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "platforms.json").write_text(json.dumps({
        "deepseek": {
            "name": "DeepSeek AI 监测", "kind": "ai_probe",
            "studio_url": "https://chat.deepseek.com/",
            "logged_in_url_contains": ["chat.deepseek.com/"],
            "input_locators": ["textarea"], "answer_locators": [".answer"],
        }
    }, ensure_ascii=False), encoding="utf-8")
    with Database(settings.db_path) as db:
        db.execute(
            """INSERT INTO platform_accounts(platform,display_name,profile_dir,status)
            VALUES(?,?,?,'connected')""",
            ("deepseek", "DeepSeek AI 监测", str(tmp_path / "profile")),
        )
        seed_opportunities(settings, db)

    class BodyLocator:
        def inner_text(self, timeout=0):
            return "你的账号已被禁言至 2099 年 9 月 6 日 12:48。"

    class FakePage:
        url = "https://chat.deepseek.com/"

        def goto(self, url, **kwargs):
            self.url = url

        def wait_for_timeout(self, value):
            return None

        def locator(self, selector):
            assert selector == "body"
            return BodyLocator()

    class FakeContext:
        pages = [FakePage()]

        def close(self):
            return None

    class FakeChromium:
        calls = 0

        def launch_persistent_context(self, **kwargs):
            self.calls += 1
            return FakeContext()

    chromium = FakeChromium()

    class FakePlaywright:
        pass

    FakePlaywright.chromium = chromium

    class FakeManager:
        def __enter__(self):
            return FakePlaywright()

        def __exit__(self, *args):
            return None

    monkeypatch.setattr("hongtu_geo.browser._sync_playwright_context", lambda: FakeManager())
    monkeypatch.setattr("hongtu_geo.browser.profile_path", lambda settings, platform: tmp_path / "profile")
    first = run_browser_probes(settings, limit=1, providers=["deepseek"])
    second = run_browser_probes(settings, limit=1, providers=["deepseek"])
    with Database(settings.db_path) as db:
        account = dict(db.query(
            "SELECT status,retry_after FROM platform_accounts WHERE platform='deepseek'"
        )[0])
        item = dict(db.query("SELECT status,attempts FROM probe_batch_items")[0])
        batch_status = db.query("SELECT status FROM probe_batches")[0]["status"]
        assert db.query("SELECT COUNT(*) n FROM probes")[0]["n"] == 0
    assert first["status"] == "waiting_for_ai_retry"
    assert second["status"] == "waiting_for_ai_retry"
    assert chromium.calls == 1
    assert account == {
        "status": "temporarily_unavailable",
        "retry_after": "2099-09-06T04:48:00+00:00",
    }
    assert item == {"status": "planned", "attempts": 0}
    assert batch_status == "waiting_retry"


def test_api_resume_rejects_browser_batch(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    with Database(settings.db_path) as db:
        db.execute(
            """INSERT INTO probe_batches(batch_id,started_at,status,planned_calls,config_json)
            VALUES(?,?,?,1,?)""",
            ("browser-batch", now_iso(), "partial", '{"engine_surface":"browser"}'),
        )
        with pytest.raises(ValueError, match="浏览器探测批次"):
            resume_probe_batch(settings, db, "browser-batch")


def test_positive_rate_is_null_when_brand_was_never_mentioned(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["brand"]["name"] = "宏图商机汇"
    with Database(settings.db_path) as db:
        record_probe(
            db, settings, "deepseek", "常州做膜结构选哪家？",
            "可以优先核验企业资质、案例和报价，再综合选择合适的服务商。",
            engine_surface="browser",
        )
        snapshot = build_visibility_snapshot(settings, db)
    row = snapshot["providers"][0]
    assert row["mention_rate"] == 0.0
    assert row["positive_rate"] is None
    assert row["positive_rate_basis"] == "brand_mentions_only"


def test_positive_rate_uses_brand_mentions_as_denominator(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["brand"]["name"] = "宏图商机汇"
    with Database(settings.db_path) as db:
        record_probe(db, settings, "deepseek", "问题一", "宏图商机汇可以作为候选。", engine_surface="browser")
        record_probe(db, settings, "deepseek", "问题二", "可以优先核验企业资质后选择。", engine_surface="browser")
        snapshot = build_visibility_snapshot(settings, db)
    row = snapshot["providers"][0]
    assert row["mention_rate"] == 50.0
    assert row["positive_rate"] == 100.0


def test_competitor_aliases_are_canonicalized_and_visibility_is_reported(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["brand"]["name"] = "宏图商机汇"
    settings.raw["monitor"]["competitors"] = [
        {"name": "剑鱼标讯", "aliases": ["剑鱼360"], "domain": "jianyu360.cn"},
        {"name": "千里马招标网", "aliases": ["千里马招标"], "domain": "qianlima.com"},
    ]
    settings.raw["monitor"]["share_of_voice_min_mentions"] = 1
    with Database(settings.db_path) as db:
        first = record_probe(
            db, settings, "deepseek", "商机平台怎么选？",
            "可以比较宏图商机汇、剑鱼360和千里马招标。", engine_surface="browser",
        )
        record_probe(
            db, settings, "kimi", "招标信息哪里找？",
            "可以查看剑鱼标讯。", engine_surface="browser",
        )
        snapshot = build_visibility_snapshot(settings, db)
    assert first["competitors"] == ["剑鱼标讯", "千里马招标网"]
    assert snapshot["competitor_visibility"] == [
        {
            "name": "剑鱼标讯", "mentions": 2, "sample_mention_rate": 100.0,
            "mention_rate_ci95": [34.2, 100.0], "question_count": 2,
            "provider_count": 2, "brand_co_mentions": 1,
        },
        {
            "name": "千里马招标网", "mentions": 1, "sample_mention_rate": 50.0,
            "mention_rate_ci95": [9.5, 90.5], "question_count": 1,
            "provider_count": 1, "brand_co_mentions": 1,
        },
    ]
    assert snapshot["tracked_share_of_voice"] == 25.0


def test_tracked_share_of_voice_is_withheld_below_evidence_threshold(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["brand"]["name"] = "宏图商机汇"
    settings.raw["monitor"]["competitors"] = ["剑鱼标讯"]
    settings.raw["monitor"]["share_of_voice_min_mentions"] = 10
    with Database(settings.db_path) as db:
        record_probe(
            db, settings, "deepseek", "问题", "宏图商机汇可以作为候选。",
            engine_surface="browser",
        )
        snapshot = build_visibility_snapshot(settings, db)
    assert snapshot["tracked_share_of_voice"] is None
    assert snapshot["tracked_share_of_voice_status"] == "insufficient_mentions"
    assert snapshot["tracked_name_mentions"] == 1
    assert snapshot["tracked_share_of_voice_min_mentions"] == 10


def test_competitor_detection_deduplicates_canonical_names_and_avoids_overlap(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["monitor"]["competitors"] = [
        {"name": "Alpha", "aliases": ["A"]},
        {"name": "Alpha", "aliases": ["Alpha牌"]},
        {"name": "AlphaBeta", "aliases": ["AB"]},
    ]
    only_long = analyze_answer("建议先比较 AlphaBeta。", settings)
    both_separate = analyze_answer("建议比较 AlphaBeta，也可以单独看看 Alpha。", settings)
    duplicate_alias = analyze_answer("Alpha牌提供了相关服务。", settings)
    assert only_long["competitors"] == ["AlphaBeta"]
    assert both_separate["competitors"] == ["Alpha", "AlphaBeta"]
    assert duplicate_alias["competitors"] == ["Alpha"]


def test_browser_probe_marks_login_expired_and_bounds_item_attempts(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["acquisition"] = {"priority_questions": ["膜结构工程商机去哪里获取？"]}
    settings.raw["monitor"].update({
        "browser_samples_per_prompt": 3, "browser_max_calls_per_run": 3,
        "max_item_attempts": 3,
    })
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "platforms.json").write_text(json.dumps({
        "deepseek": {
            "name": "DeepSeek", "kind": "ai_probe",
            "studio_url": "https://chat.deepseek.com/", "logged_in_url_contains": ["chat.deepseek.com/"],
            "input_locators": ["textarea"], "answer_locators": [".answer"],
        }
    }), encoding="utf-8")
    with Database(settings.db_path) as db:
        db.execute(
            "INSERT INTO platform_accounts(platform,display_name,profile_dir,status) VALUES(?,?,?,'connected')",
            ("deepseek", "DeepSeek", str(tmp_path / "profile")),
        )
        seed_opportunities(settings, db)

    class FailingChromium:
        def launch_persistent_context(self, **kwargs):
            raise RuntimeError("AI 监测账号登录态已失效")

    class FakePlaywright:
        chromium = FailingChromium()

    class FakeManager:
        def __enter__(self):
            return FakePlaywright()

        def __exit__(self, *args):
            return None

    monkeypatch.setattr("hongtu_geo.browser._sync_playwright_context", lambda: FakeManager())
    monkeypatch.setattr("hongtu_geo.browser.profile_path", lambda settings, platform: tmp_path / "profile")
    result = run_browser_probes(settings, limit=1, providers=["deepseek"])
    with Database(settings.db_path) as db:
        account = dict(db.query("SELECT status,last_error FROM platform_accounts WHERE platform='deepseek'")[0])
        items = [dict(row) for row in db.query("SELECT status,attempts FROM probe_batch_items ORDER BY id")]
    assert result["status"] == "failed"
    assert account["status"] == "not_connected"
    assert "登录态" in account["last_error"]
    assert items == [{"status": "failed", "attempts": 1}] * 3


def test_run_probes_records_skipped_batch_when_no_credentials(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["monitor"]["providers"] = [{
        "name": "missing-key-ai", "kind": "responses", "model": "x",
        "api_key_env": "MISSING_AI_KEY", "enabled": True,
    }]
    monkeypatch.delenv("MISSING_AI_KEY", raising=False)
    with Database(settings.db_path) as db:
        seed_opportunities(settings, db)
        result = run_probes(settings, db, limit=1)
        batch = dict(db.query("SELECT * FROM probe_batches")[0])
    assert result["batch_status"] == "skipped"
    assert result["calls_attempted"] == 0
    assert result["skipped"] == [{"provider": "missing-key-ai", "reason": "missing_env:MISSING_AI_KEY"}]
    assert batch["planned_calls"] == 0
    assert batch["status"] == "skipped"


def test_run_probes_auto_resumes_only_failed_manifest_item(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["brand"]["name"] = "宏图商机汇"
    settings.raw["monitor"].update({
        "max_calls_per_run": 2,
        "max_item_attempts": 3,
        "auto_resume_probe_batches": True,
        "providers": [{
            "name": "retry-ai", "kind": "responses", "model": "fake",
            "api_key_env": "RETRY_AI_KEY", "enabled": True,
        }],
    })

    class FlakyAdapter:
        surface = "api"

        def __init__(self):
            self.calls = 0

        def ask(self, prompt: str) -> ProviderAnswer:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary failure test-key")
            return ProviderAnswer("宏图商机汇可作为候选。")

    class StableAdapter:
        surface = "api"

        def ask(self, prompt: str) -> ProviderAnswer:
            return ProviderAnswer("宏图商机汇可作为候选。")

    monkeypatch.setenv("RETRY_AI_KEY", "test-key")
    monkeypatch.setattr("hongtu_geo.pipeline.build_provider_adapter", lambda provider, key: FlakyAdapter())
    with Database(settings.db_path) as db:
        seed_opportunities(settings, db)
        first = run_probes(settings, db, limit=2, samples_per_prompt=1)
        first_items = [dict(row) for row in db.query(
            "SELECT status,attempts,probe_id FROM probe_batch_items ORDER BY id"
        )]
        stored_errors = db.query("SELECT errors_json FROM probe_batches")[0]["errors_json"]
        monkeypatch.setattr("hongtu_geo.pipeline.build_provider_adapter", lambda provider, key: StableAdapter())
        second = run_probes(settings, db, limit=2, samples_per_prompt=1)
        final_items = [dict(row) for row in db.query(
            "SELECT status,attempts,probe_id FROM probe_batch_items ORDER BY id"
        )]
        batch_count = db.query("SELECT COUNT(*) n FROM probe_batches")[0]["n"]
        probe_count = db.query("SELECT COUNT(*) n FROM probes")[0]["n"]

    assert first["batch_status"] == "partial"
    assert "test-key" not in stored_errors
    assert "[REDACTED]" in stored_errors
    assert [item["status"] for item in first_items] == ["failed", "succeeded"]
    assert second["batch_id"] == first["batch_id"]
    assert second["batch_status"] == "completed"
    assert second["auto_resumed"] is True
    assert batch_count == 1
    assert probe_count == 2
    assert [item["attempts"] for item in final_items] == [2, 1]
    assert all(item["status"] == "succeeded" and item["probe_id"] for item in final_items)


def test_resume_waits_for_credentials_without_consuming_attempt(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["monitor"]["providers"] = [{
        "name": "credential-ai", "kind": "responses", "model": "fake",
        "api_key_env": "CREDENTIAL_AI_KEY", "enabled": True,
    }]
    monkeypatch.delenv("CREDENTIAL_AI_KEY", raising=False)
    with Database(settings.db_path) as db:
        db.execute(
            """INSERT INTO probe_batches(batch_id,started_at,status,planned_calls,config_json)
            VALUES(?,?,?,?,?)""",
            ("credential-batch", datetime.now(UTC).isoformat(), "partial", 1, '{"samples_per_prompt":1}'),
        )
        db.execute(
            """INSERT INTO probe_batch_items(
            batch_id,provider,question,prompt,sample_index,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?)""",
            (
                "credential-batch", "credential-ai", "问题", "中立问题", 1,
                datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat(),
            ),
        )
        result = resume_probe_batch(settings, db, "credential-batch")
        item = dict(db.query("SELECT status,attempts FROM probe_batch_items")[0])
        health = build_sampling_health(settings, db)
    assert result["batch_status"] == "waiting_credentials"
    assert result["calls_attempted"] == 0
    assert item == {"status": "planned", "attempts": 0}
    assert health["status"] == "degraded"
    assert health["latest_batch_status"] == "waiting_credentials"


def test_resume_rejects_recent_running_batch(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    with Database(settings.db_path) as db:
        db.execute(
            """INSERT INTO probe_batches(batch_id,started_at,status,planned_calls,config_json)
            VALUES(?,?,?,?,?)""",
            ("live-batch", datetime.now(UTC).isoformat(), "running", 1, '{}'),
        )
        with pytest.raises(ValueError, match="仍在运行"):
            resume_probe_batch(settings, db, "live-batch")


def test_sampling_health_measures_outcome_agreement_within_same_batch(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["brand"]["name"] = "宏图商机汇"
    settings.raw["monitor"]["samples_per_prompt"] = 3
    with Database(settings.db_path) as db:
        for index, answer in enumerate([
            "宏图商机汇可作为候选。",
            "宏图商机汇可作为候选之一。",
            "暂未发现适合的平台。",
        ], start=1):
            record_probe(
                db, settings, "test-ai", "商机平台怎么选？", answer,
                engine_surface="api", sample_index=index, experiment_id="batch-one",
            )
        health = build_sampling_health(settings, db)
    assert health["status"] == "variable"
    assert health["repeated_groups"] == 1
    assert health["target_reached_groups"] == 1
    assert health["variable_groups"] == 1
    assert health["average_outcome_agreement"] == 66.7
    assert health["group_details"][0]["distinct_answer_rate"] == 100.0


def test_sampling_health_flags_stale_running_batch(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    with Database(settings.db_path) as db:
        db.execute(
            """INSERT INTO probe_batches(batch_id,started_at,status,planned_calls)
            VALUES(?,?,?,?)""",
            ("stale-batch", "2020-01-01T00:00:00+00:00", "running", 3),
        )
        health = build_sampling_health(settings, db)
    assert health["status"] == "degraded"
    assert health["stale_batch_ids"] == ["stale-batch"]


def test_sampling_health_does_not_merge_legacy_rows_without_batch_id(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    with Database(settings.db_path) as db:
        record_probe(db, settings, "legacy-ai", "同一问题", "答案一")
        record_probe(db, settings, "legacy-ai", "同一问题", "答案二")
        health = build_sampling_health(settings, db)
    assert health["sample_groups"] == 2
    assert health["repeated_groups"] == 0
    assert health["status"] == "insufficient_repeats"


def test_record_probe_rejects_blank_answer_at_storage_boundary(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    with Database(settings.db_path) as db:
        with pytest.raises(ValueError, match="回答为空"):
            record_probe(db, settings, "test-ai", "问题", " \n\t ")
        assert db.query("SELECT COUNT(*) n FROM probes")[0]["n"] == 0


def test_sampling_health_does_not_count_duplicate_sample_slot_as_repeat(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    with Database(settings.db_path) as db:
        record_probe(db, settings, "browser-ai", "同一问题", "第一次", experiment_id="same-run")
        record_probe(db, settings, "browser-ai", "同一问题", "重试一次", experiment_id="same-run")
        health = build_sampling_health(settings, db)
    assert health["sample_groups"] == 1
    assert health["repeated_groups"] == 0
    assert health["status"] == "insufficient_repeats"


def test_gap_analysis_only_calls_visibility_gap_after_target_repeats(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["monitor"]["samples_per_prompt"] = 3
    with Database(settings.db_path) as db:
        seed_opportunities(settings, db)
        question = db.query("SELECT question FROM opportunities ORDER BY id LIMIT 1")[0]["question"]
        for index in range(1, 4):
            record_probe(
                db, settings, "test-ai", question, f"未提及品牌，样本 {index}",
                engine_surface="browser", sample_index=index, experiment_id="repeat-batch",
            )
        gaps = build_gap_analysis(settings, db)
    selected = next(item for item in gaps["actions"] if item["question"] == question)
    assert selected["type"] == "visibility_gap"


def test_gap_analysis_does_not_treat_source_requested_as_natural_baseline(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["monitor"]["samples_per_prompt"] = 3
    with Database(settings.db_path) as db:
        seed_opportunities(settings, db)
        question = db.query("SELECT question FROM opportunities ORDER BY id LIMIT 1")[0]["question"]
        for index in range(1, 4):
            record_probe(
                db, settings, "deepseek", question, f"实验回答 {index}",
                prompt_variant="source_requested", prompt_version="source-requested-v1",
                engine_surface="browser", sample_index=index, experiment_id="source-batch",
            )
        gaps = build_gap_analysis(settings, db)
    selected = next(item for item in gaps["actions"] if item["question"] == question)
    assert selected["type"] == "measurement_gap"
    assert "不能替代自然基线" in selected["reason"]
    assert selected["samples"] == 0


def test_gap_analysis_focuses_core_questions_and_defers_unmeasured_backlog(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    with Database(settings.db_path) as db:
        seed_opportunities(settings, db)
        question = db.query("SELECT question FROM opportunities ORDER BY score DESC,id LIMIT 1")[0]["question"]
        settings.raw["acquisition"] = {"priority_questions": [question]}
        gaps = build_gap_analysis(settings, db, persist=False)
        persisted = db.query("SELECT COUNT(*) n FROM geo_actions")[0]["n"]
    assert gaps["focus_count"] == 1
    assert gaps["focus_actions"][0]["question"] == question
    assert gaps["focus_actions"][0]["tier"] == "core"
    assert gaps["focus_actions"][0]["confidence"] == "unmeasured"
    assert gaps["backlog_summary"]["deferred_unmeasured"] > 0
    assert gaps["backlog_summary"]["total_actions"] == len(gaps["actions"])
    assert persisted == 0


def test_gap_analysis_repeat_floor_cannot_be_configured_below_three(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["monitor"]["samples_per_prompt"] = 2
    with Database(settings.db_path) as db:
        seed_opportunities(settings, db)
        question = db.query("SELECT question FROM opportunities ORDER BY id LIMIT 1")[0]["question"]
        for index in range(1, 3):
            record_probe(
                db, settings, "deepseek", question, f"样本 {index}",
                engine_surface="browser", sample_index=index, experiment_id="two-only",
            )
        gaps = build_gap_analysis(settings, db, persist=False)
        settings.raw["monitor"]["samples_per_prompt"] = "invalid"
        invalid_config_gaps = build_gap_analysis(settings, db, persist=False)
        invalid_config_health = build_sampling_health(settings, db)
    selected = next(item for item in gaps["actions"] if item["question"] == question)
    invalid_selected = next(item for item in invalid_config_gaps["actions"] if item["question"] == question)
    assert selected["type"] == "sampling_gap"
    assert selected["confidence"] == "insufficient_repeats"
    assert "3 次" in selected["reason"]
    assert invalid_selected["type"] == "sampling_gap"
    assert invalid_config_health["target_samples_per_prompt"] == 3


def test_sampling_health_reports_naturalistic_primary_separately(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["monitor"].update({
        "samples_per_prompt": 3, "primary_prompt_variant": "naturalistic",
    })
    with Database(settings.db_path) as db:
        for index in range(1, 4):
            record_probe(
                db, settings, "deepseek", "引用实验", f"回答 {index}",
                prompt_variant="source_requested", prompt_version="source-requested-v1",
                engine_surface="browser", sample_index=index, experiment_id="source-batch",
            )
        health = build_sampling_health(settings, db)
        maturity = build_maturity_audit(settings, db)
    assert health["status"] == "healthy"
    assert health["target_reached_groups"] == 1
    assert health["primary_status"] == "no_samples"
    assert health["primary_samples"] == 0
    assert health["primary_target_reached_groups"] == 0
    assert maturity["layers"]["measurement"]["score"] == 50


def test_sampling_health_requires_priority_question_portfolio_coverage(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["monitor"].update({
        "samples_per_prompt": 3,
        "primary_prompt_variant": "naturalistic",
        "primary_engine_surface": "browser",
        "primary_prompt_version": "naturalistic-v1",
        "minimum_primary_providers": 1,
    })
    settings.raw["acquisition"] = {"priority_questions": ["核心问题一", "核心问题二"]}
    for question in ("核心问题一",):
        with Database(settings.db_path) as db:
            for index in range(1, 4):
                record_probe(
                    db, settings, "deepseek", question, f"回答 {index}",
                    engine_surface="browser", sample_index=index,
                    experiment_id=f"batch-{question}",
                )
    with Database(settings.db_path) as db:
        partial = build_sampling_health(settings, db)
        partial_maturity = build_maturity_audit(settings, db)
    assert partial["primary_status"] == "partial_coverage"
    assert partial["priority_question_coverage"]["repeat_ready_questions"] == 1
    assert partial["priority_question_coverage"]["question_count"] == 2
    assert partial["priority_question_coverage"]["repeat_ready_coverage_percent"] == 50.0
    assert partial_maturity["layers"]["measurement"]["score"] == 70

    with Database(settings.db_path) as db:
        for index in range(1, 4):
            record_probe(
                db, settings, "deepseek", "核心问题二", f"回答 {index}",
                engine_surface="browser", sample_index=index,
                experiment_id="batch-核心问题二",
            )
        complete = build_sampling_health(settings, db)
        complete_maturity = build_maturity_audit(settings, db)
    assert complete["primary_status"] == "healthy"
    assert complete["priority_question_coverage"]["repeat_ready_questions"] == 2
    assert complete_maturity["layers"]["measurement"]["score"] == 90


def test_sampling_health_requires_multiple_independent_primary_engines(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["monitor"].update({
        "samples_per_prompt": 3,
        "primary_prompt_variant": "naturalistic",
        "primary_engine_surface": "browser",
        "primary_prompt_version": "naturalistic-v1",
        "minimum_primary_providers": 2,
    })
    settings.raw["acquisition"] = {"priority_questions": ["核心问题"]}
    with Database(settings.db_path) as db:
        for index in range(1, 4):
            record_probe(
                db, settings, "deepseek", "核心问题", f"回答 {index}",
                engine_surface="browser", sample_index=index, experiment_id="deepseek-batch",
            )
        one_engine = build_sampling_health(settings, db)
        one_engine_maturity = build_maturity_audit(settings, db)
    assert one_engine["primary_status"] == "insufficient_engines"
    assert one_engine["primary_engine_coverage"] == {
        "minimum_providers": 2,
        "repeat_ready_provider_count": 1,
        "repeat_ready_providers": ["deepseek"],
        "status": "insufficient_engines",
    }
    assert one_engine_maturity["layers"]["measurement"]["score"] == 70

    with Database(settings.db_path) as db:
        for index in range(1, 4):
            record_probe(
                db, settings, "second-ai", "核心问题", f"回答 {index}",
                engine_surface="browser", sample_index=index, experiment_id="second-batch",
            )
        two_engines = build_sampling_health(settings, db)
        two_engine_maturity = build_maturity_audit(settings, db)
    assert two_engines["primary_status"] == "healthy"
    assert two_engines["primary_engine_coverage"]["repeat_ready_provider_count"] == 2
    assert two_engines["primary_engine_coverage"]["status"] == "healthy"
    assert two_engine_maturity["layers"]["measurement"]["score"] == 90


def test_sampling_health_never_relaxes_repeat_floor_below_three(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["monitor"]["samples_per_prompt"] = 1
    with Database(settings.db_path) as db:
        for index in range(1, 3):
            record_probe(
                db, settings, "deepseek", "问题", f"回答 {index}",
                engine_surface="browser", sample_index=index, experiment_id="two-samples",
            )
        health = build_sampling_health(settings, db)
    assert health["target_samples_per_prompt"] == 3
    assert health["target_reached_groups"] == 0


def test_ai_probe_readiness_validates_adapters_and_declared_login_state(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["monitor"]["minimum_primary_providers"] = 2
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    valid_spec = {
        "name": "AI One", "kind": "ai_probe",
        "login_url": "https://ai-one.example/login",
        "studio_url": "https://ai-one.example/",
        "new_chat_url": "https://ai-one.example/",
        "logged_in_url_contains": ["ai-one.example/"],
        "input_locators": ["textarea"], "answer_locators": [".answer"],
    }
    invalid_spec = {
        "name": "AI Broken", "kind": "ai_probe",
        "login_url": "javascript:alert(1)", "studio_url": "",
        "logged_in_url_contains": [], "input_locators": [], "answer_locators": [],
    }
    (config_dir / "platforms.json").write_text(
        json.dumps({"ai-one": valid_spec, "ai-broken": invalid_spec}), encoding="utf-8"
    )
    with Database(settings.db_path) as db:
        db.execute(
            """INSERT INTO platform_accounts(platform,display_name,profile_dir,status,last_checked_at)
            VALUES(?,?,?,?,?)""",
            ("ai-one", "AI One", "hidden", "connected", "2026-09-05T00:00:00+00:00"),
        )
        readiness = build_ai_probe_readiness(settings, db)
    assert validate_ai_probe_spec("ai-one", valid_spec) == []
    assert readiness["status"] == "invalid_configuration"
    assert readiness["configured_providers"] == 2
    assert readiness["valid_adapters"] == 1
    assert readiness["declared_connected"] == 1
    assert readiness["ready_for_attempt"] == 1
    assert readiness["provider_deficit"] == 1
    assert readiness["providers"][0]["provider"] == "ai-broken"
    assert readiness["providers"][1]["ready_for_attempt"] is True
    assert readiness["errors"]
    assert "profile_dir" not in json.dumps(readiness)
    malformed_url_spec = dict(valid_spec, login_url="https://[")
    assert any("login_url" in error for error in validate_ai_probe_spec("bad-url", malformed_url_spec))


@pytest.mark.parametrize("non_object", [[], None, "platforms"])
def test_ai_probe_readiness_rejects_non_object_json_and_invalid_target(
    tmp_path: Path, non_object,
) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["monitor"]["minimum_primary_providers"] = "not-a-number"
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "platforms.json").write_text(json.dumps(non_object), encoding="utf-8")
    with Database(settings.db_path) as db:
        readiness = build_ai_probe_readiness(settings, db)
    assert readiness["status"] == "invalid_configuration"
    assert readiness["minimum_providers"] == 2
    assert readiness["provider_deficit"] == 2
    assert any("顶层必须" in error for error in readiness["errors"])
    assert any("minimum_primary_providers" in error for error in readiness["errors"])


def test_automation_pipeline_respects_generation_and_api_locks_and_snapshots_last(
    tmp_path: Path, monkeypatch,
) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["brand"]["site_url"] = ""
    settings.raw["content"]["generation_paused"] = True
    settings.raw["monitor"]["api_probes_enabled"] = False
    settings.raw["autopilot"] = {"browser_probes_enabled": True}
    events: list[str] = []
    monkeypatch.setattr(pipeline_module, "seed_opportunities", lambda *_: {"created": 0})
    monkeypatch.setattr(pipeline_module, "extract_official_facts", lambda *_: {"status": "ok"})
    monkeypatch.setattr(pipeline_module, "build_entity_assets", lambda *_: {"status": "ok"})
    monkeypatch.setattr(pipeline_module, "build_prompt_benchmark", lambda *_: tmp_path / "benchmark.json")
    monkeypatch.setattr(
        browser_module, "run_browser_probes",
        lambda *_: events.append("browser") or {"status": "completed", "results": []},
    )
    monkeypatch.setattr(
        pipeline_module, "build_strategy_snapshot",
        lambda *_: events.append("strategy") or tmp_path / "strategy.json",
    )
    monkeypatch.setattr(
        pipeline_module, "build_report",
        lambda *_: events.append("report") or tmp_path / "report.md",
    )
    result = run_automation_pipeline(settings, "daily")
    with Database(settings.db_path) as db:
        counts = {
            "drafts": db.query("SELECT COUNT(*) n FROM drafts")[0]["n"],
            "jobs": db.query("SELECT COUNT(*) n FROM publish_jobs")[0]["n"],
        }
        run = dict(db.query("SELECT status,detail FROM runs ORDER BY id DESC LIMIT 1")[0])
    assert result["crawl"]["status"] == "skipped"
    assert result["generate"] == {"status": "paused", "created": 0}
    assert result["quality"] == {"status": "paused", "reviewed": 0}
    assert result["schedule"] == {"status": "paused", "created": 0}
    assert result["probes"] == {"status": "disabled", "reason": "API 探测已关闭"}
    assert result["browser_probes"]["status"] == "completed"
    assert events == ["browser", "strategy", "report"]
    assert counts == {"drafts": 0, "jobs": 0}
    assert run["status"] == "ok"
    assert json.loads(run["detail"])["generate"]["created"] == 0


def test_automation_pipeline_does_not_probe_browser_without_explicit_opt_in(
    tmp_path: Path, monkeypatch,
) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["brand"]["site_url"] = ""
    settings.raw["content"]["generation_paused"] = True
    settings.raw["monitor"]["api_probes_enabled"] = False
    settings.raw["autopilot"] = {}
    monkeypatch.setattr(pipeline_module, "seed_opportunities", lambda *_: {"created": 0})
    monkeypatch.setattr(pipeline_module, "extract_official_facts", lambda *_: {"status": "ok"})
    monkeypatch.setattr(pipeline_module, "build_entity_assets", lambda *_: {"status": "ok"})
    monkeypatch.setattr(pipeline_module, "build_prompt_benchmark", lambda *_: tmp_path / "benchmark.json")
    monkeypatch.setattr(pipeline_module, "build_strategy_snapshot", lambda *_: tmp_path / "strategy.json")
    monkeypatch.setattr(pipeline_module, "build_report", lambda *_: tmp_path / "report.md")
    monkeypatch.setattr(
        browser_module, "run_browser_probes",
        lambda *_: pytest.fail("缺省配置不得调用浏览器探测"),
    )
    result = run_automation_pipeline(settings, "daily")
    assert result["browser_probes"] == {"status": "disabled", "reason": "浏览器探测已关闭"}


def test_visibility_trends_requires_two_full_windows_and_reports_screening_signal(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["brand"]["name"] = "宏图商机汇"
    settings.raw["monitor"].update({
        "trend_window_days": 7, "trend_min_samples": 10,
        "primary_engine_surface": "api",
    })
    current_time = datetime.now(UTC).replace(microsecond=0)
    previous_time = current_time - timedelta(days=7)
    with Database(settings.db_path) as db:
        for index in range(10):
            current = record_probe(
                db, settings, "trend-ai", f"趋势问题 {index}", "推荐宏图商机汇。",
                engine_surface="api", experiment_id="current-window",
            )
            db.execute("UPDATE probes SET probed_at=? WHERE id=?", (current_time.isoformat(), current["probe_id"]))
            previous = record_probe(
                db, settings, "trend-ai", f"趋势问题 {index}", "暂未找到合适平台。",
                engine_surface="api", experiment_id="previous-window",
            )
            db.execute("UPDATE probes SET probed_at=? WHERE id=?", (previous_time.isoformat(), previous["probe_id"]))
        trends = build_visibility_trends(settings, db)
    comparison = trends["comparisons"][0]
    assert trends["status"] == "comparison_ready"
    assert comparison["current"]["samples"] == 10
    assert comparison["previous"]["samples"] == 10
    assert comparison["matched_panel"]["matched_question_count"] == 10
    assert comparison["matched_panel"]["paired_samples_per_window"] == 10
    assert comparison["deltas_percentage_points"]["mention_rate"] == 100.0
    assert "mention_rate" in comparison["non_overlapping_ci_signals"]
    assert "不证明因果" in trends["method_note"]


def test_visibility_trends_rejects_composition_shift_even_with_large_raw_windows(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["monitor"].update({
        "trend_window_days": 7,
        "trend_min_samples": 10,
        "trend_min_matched_questions": 3,
        "primary_engine_surface": "api",
    })
    current_time = datetime.now(UTC).replace(microsecond=0)
    previous_time = current_time - timedelta(days=7)
    with Database(settings.db_path) as db:
        for index in range(10):
            current = record_probe(
                db, settings, "trend-ai", f"当前独有问题 {index}", "宏图商机汇。",
                engine_surface="api", experiment_id="current-window",
            )
            db.execute("UPDATE probes SET probed_at=? WHERE id=?", (current_time.isoformat(), current["probe_id"]))
            previous = record_probe(
                db, settings, "trend-ai", f"前期独有问题 {index}", "未提及品牌。",
                engine_surface="api", experiment_id="previous-window",
            )
            db.execute("UPDATE probes SET probed_at=? WHERE id=?", (previous_time.isoformat(), previous["probe_id"]))
        trends = build_visibility_trends(settings, db)
    comparison = trends["comparisons"][0]
    assert comparison["current"]["samples"] == 10
    assert comparison["previous"]["samples"] == 10
    assert comparison["status"] == "insufficient_data"
    assert comparison["matched_panel"]["matched_question_count"] == 0
    assert comparison["deltas_percentage_points"]["mention_rate"] is None


def test_visibility_trends_balances_sample_count_for_each_matched_question(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["monitor"].update({
        "trend_window_days": 7,
        "trend_min_samples": 3,
        "trend_min_matched_questions": 3,
        "primary_engine_surface": "api",
    })
    current_time = datetime.now(UTC).replace(microsecond=0)
    previous_time = current_time - timedelta(days=7)
    with Database(settings.db_path) as db:
        for question in ("问题一", "问题二", "问题三"):
            for index in range(3):
                current = record_probe(
                    db, settings, "trend-ai", question, "宏图商机汇。",
                    engine_surface="api", experiment_id=f"current-{question}-{index}",
                )
                db.execute(
                    "UPDATE probes SET probed_at=? WHERE id=?",
                    (current_time.isoformat(), current["probe_id"]),
                )
            previous = record_probe(
                db, settings, "trend-ai", question, "未提及品牌。",
                engine_surface="api", experiment_id=f"previous-{question}",
            )
            db.execute(
                "UPDATE probes SET probed_at=? WHERE id=?",
                (previous_time.isoformat(), previous["probe_id"]),
            )
        trends = build_visibility_trends(settings, db)
    panel = trends["comparisons"][0]["matched_panel"]
    assert panel["status"] == "comparison_ready"
    assert panel["paired_samples_per_window"] == 3
    assert panel["current_samples_excluded"] == 6
    assert panel["previous_samples_excluded"] == 0
    assert panel["per_question_samples"] == {"问题一": 1, "问题三": 1, "问题二": 1}


def test_visibility_trends_does_not_mix_locale_or_region(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["monitor"].update({
        "trend_window_days": 7,
        "trend_min_samples": 3,
        "trend_min_matched_questions": 3,
        "primary_engine_surface": "api",
        "primary_locale": "zh-CN",
        "primary_region": "CN",
    })
    current_time = datetime.now(UTC).replace(microsecond=0)
    previous_time = current_time - timedelta(days=7)
    with Database(settings.db_path) as db:
        for question in ("问题一", "问题二", "问题三"):
            current = record_probe(
                db, settings, "trend-ai", question, "宏图商机汇。",
                engine_surface="api", locale="zh-CN", region="CN",
                experiment_id=f"current-{question}",
            )
            db.execute(
                "UPDATE probes SET probed_at=? WHERE id=?",
                (current_time.isoformat(), current["probe_id"]),
            )
            previous = record_probe(
                db, settings, "trend-ai", question, "未提及品牌。",
                engine_surface="api", locale="en-US", region="US",
                experiment_id=f"previous-{question}",
            )
            db.execute(
                "UPDATE probes SET probed_at=? WHERE id=?",
                (previous_time.isoformat(), previous["probe_id"]),
            )
        trends = build_visibility_trends(settings, db)
    assert trends["status"] == "insufficient_data"
    assert trends["current_period"]["samples"] == 3
    assert trends["previous_period"]["samples"] == 0
    assert len(trends["comparisons"]) == 2
    assert all(item["status"] == "insufficient_data" for item in trends["comparisons"])
    assert {(item["locale"], item["region"]) for item in trends["comparisons"]} == {
        ("zh-CN", "CN"), ("en-US", "US")
    }


def test_visibility_trends_deduplicates_retried_sample_slots(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["monitor"].update({
        "trend_window_days": 7,
        "trend_min_samples": 4,
        "trend_min_matched_questions": 3,
        "primary_engine_surface": "api",
    })
    current_time = datetime.now(UTC).replace(microsecond=0)
    previous_time = current_time - timedelta(days=7)
    with Database(settings.db_path) as db:
        for question in ("问题一", "问题二", "问题三"):
            for period, captured_at, answer in (
                ("current", current_time, "宏图商机汇。"),
                ("previous", previous_time, "未提及品牌。"),
            ):
                for retry in range(3):
                    probe = record_probe(
                        db, settings, "trend-ai", question, answer,
                        engine_surface="api", sample_index=1,
                        experiment_id=f"{period}-batch",
                        raw_metadata={"retry": retry},
                    )
                    db.execute(
                        "UPDATE probes SET probed_at=? WHERE id=?",
                        (captured_at.isoformat(), probe["probe_id"]),
                    )
        trends = build_visibility_trends(settings, db)
    comparison = trends["comparisons"][0]
    panel = comparison["matched_panel"]
    assert comparison["current"]["samples"] == 9
    assert comparison["previous"]["samples"] == 9
    assert comparison["status"] == "insufficient_data"
    assert panel["paired_samples_per_window"] == 3
    assert panel["duplicate_sample_slots_excluded"] == {"current": 6, "previous": 6}
    assert comparison["deltas_percentage_points"]["mention_rate"] is None


def test_visibility_trends_refuses_delta_when_either_window_is_too_small(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["monitor"].update({
        "trend_window_days": 7, "trend_min_samples": 10,
        "primary_engine_surface": "api",
    })
    with Database(settings.db_path) as db:
        record_probe(db, settings, "trend-ai", "单个问题", "宏图商机可作为候选。", engine_surface="api")
        trends = build_visibility_trends(settings, db)
    comparison = trends["comparisons"][0]
    assert trends["status"] == "insufficient_data"
    assert comparison["status"] == "insufficient_data"
    assert comparison["deltas_percentage_points"]["mention_rate"] is None


def test_visibility_trends_primary_scope_does_not_mix_surface_or_version(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["monitor"].update({
        "primary_prompt_variant": "naturalistic",
        "primary_engine_surface": "browser",
        "primary_prompt_version": "naturalistic-v1",
    })
    with Database(settings.db_path) as db:
        record_probe(
            db, settings, "deepseek", "问题一", "回答一",
            engine_surface="browser", prompt_version="naturalistic-v1",
        )
        record_probe(
            db, settings, "deepseek", "问题二", "回答二",
            engine_surface="api", prompt_version="naturalistic-v1",
        )
        record_probe(
            db, settings, "deepseek", "问题三", "回答三",
            engine_surface="browser", prompt_version="naturalistic-v2",
        )
        trends = build_visibility_trends(settings, db)
    assert trends["current_period"]["samples"] == 1
    assert trends["all_modes_current_samples"] == 3
    assert len(trends["comparisons"]) == 3
    assert trends["primary_engine_surface"] == "browser"
    assert trends["primary_prompt_version"] == "naturalistic-v1"


def test_stale_probe_batch_watchdog_is_idempotent_and_preserves_errors(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    with Database(settings.db_path) as db:
        db.execute(
            """INSERT INTO probe_batches(batch_id,started_at,status,planned_calls,errors_json)
            VALUES(?,?,?,?,?)""",
            (
                "old-running", "2020-01-01T00:00:00+00:00", "running", 3,
                json.dumps([{"stage": "request", "error": "timeout"}]),
            ),
        )
        first = reconcile_stale_probe_batches(db)
        second = reconcile_stale_probe_batches(db)
        batch = dict(db.query("SELECT status,finished_at,errors_json FROM probe_batches")[0])
    errors = json.loads(batch["errors_json"])
    assert first == ["old-running"]
    assert second == []
    assert batch["status"] == "interrupted"
    assert batch["finished_at"]
    assert errors[0] == {"stage": "request", "error": "timeout"}
    assert errors[1]["stage"] == "watchdog"


def test_watchdog_does_not_call_invalid_timestamp_stale(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    with Database(settings.db_path) as db:
        db.execute(
            "INSERT INTO probe_batches(batch_id,started_at,status) VALUES(?,?,?)",
            ("bad-time", "not-an-iso-time", "running"),
        )
        recovered = reconcile_stale_probe_batches(db)
        batch = dict(db.query("SELECT status FROM probe_batches")[0])
        health = build_sampling_health(settings, db)
    assert recovered == []
    assert batch["status"] == "running"
    assert health["status"] == "degraded"
    assert health["stale_batch_ids"] == []
    assert health["invalid_batch_timestamp_ids"] == ["bad-time"]


def test_watchdog_preserves_malformed_legacy_error_payload(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    with Database(settings.db_path) as db:
        db.execute(
            """INSERT INTO probe_batches(batch_id,started_at,status,errors_json)
            VALUES(?,?,?,?)""",
            ("bad-errors", "2020-01-01T00:00:00+00:00", "running", "{broken-json"),
        )
        reconcile_stale_probe_batches(db)
        errors = json.loads(db.query("SELECT errors_json FROM probe_batches")[0]["errors_json"])
    assert errors[0] == {"stage": "legacy_error_payload", "raw": "{broken-json"}
    assert errors[1]["stage"] == "watchdog"


def test_watchdog_recomputes_batch_counters_from_manifest(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    with Database(settings.db_path) as db:
        db.execute(
            """INSERT INTO probe_batches(batch_id,started_at,status,planned_calls)
            VALUES(?,?,?,?)""",
            ("counter-batch", "2020-01-01T00:00:00+00:00", "running", 2),
        )
        timestamp = datetime.now(UTC).isoformat()
        db.executemany(
            """INSERT INTO probe_batch_items(
            batch_id,provider,question,prompt,sample_index,status,attempts,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            [
                ("counter-batch", "ai", "q1", "p1", 1, "succeeded", 1, timestamp, timestamp),
                ("counter-batch", "ai", "q2", "p2", 1, "running", 1, timestamp, timestamp),
            ],
        )
        reconcile_stale_probe_batches(db)
        batch = dict(db.query(
            "SELECT status,attempted_calls,succeeded_calls,failed_calls FROM probe_batches"
        )[0])
    assert batch == {
        "status": "interrupted", "attempted_calls": 2,
        "succeeded_calls": 1, "failed_calls": 1,
    }


def test_resume_repairs_item_from_existing_probe_without_second_api_call(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["monitor"]["providers"] = [{
        "name": "crash-ai", "kind": "responses", "model": "fake",
        "api_key_env": "CRASH_AI_KEY", "enabled": True,
    }]
    monkeypatch.delenv("CRASH_AI_KEY", raising=False)
    with Database(settings.db_path) as db:
        db.execute(
            """INSERT INTO probe_batches(batch_id,started_at,status,planned_calls,config_json)
            VALUES(?,?,?,?,?)""",
            ("crash-batch", datetime.now(UTC).isoformat(), "partial", 1, '{"samples_per_prompt":1}'),
        )
        cursor = db.execute(
            """INSERT INTO probe_batch_items(
            batch_id,provider,question,prompt,sample_index,status,attempts,created_at,updated_at
            ) VALUES(?,?,?,?,?,'failed',1,?,?)""",
            (
                "crash-batch", "crash-ai", "问题", "中立问题", 1,
                datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat(),
            ),
        )
        item_id = int(cursor.lastrowid)
        probe = record_probe(
            db, settings, "crash-ai", "问题", "宏图商机可作为候选。",
            engine_surface="api", sample_index=1, experiment_id="crash-batch",
            batch_item_id=item_id,
        )
        result = resume_probe_batch(settings, db, "crash-batch")
        item = dict(db.query("SELECT status,attempts,probe_id FROM probe_batch_items")[0])
        probe_count = db.query("SELECT COUNT(*) n FROM probes")[0]["n"]
    assert result["batch_status"] == "completed"
    assert result["calls_attempted"] == 0
    assert result["automated"][0]["recovered_from_existing_probe"] is True
    assert item == {"status": "succeeded", "attempts": 1, "probe_id": probe["probe_id"]}
    assert probe_count == 1


def test_auto_resume_concurrency_is_a_safe_skip_not_pipeline_failure(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path, verified=True)
    settings.raw["monitor"].update({
        "auto_resume_probe_batches": True,
        "providers": [{
            "name": "race-ai", "kind": "responses", "model": "fake",
            "api_key_env": "RACE_AI_KEY", "enabled": True,
        }],
    })
    monkeypatch.setenv("RACE_AI_KEY", "test-key")
    with Database(settings.db_path) as db:
        db.execute(
            """INSERT INTO probe_batches(batch_id,started_at,status,planned_calls,config_json)
            VALUES(?,?,?,?,?)""",
            ("race-batch", datetime.now(UTC).isoformat(), "partial", 1, '{}'),
        )
        db.execute(
            """INSERT INTO probe_batch_items(
            batch_id,provider,question,prompt,sample_index,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?)""",
            (
                "race-batch", "race-ai", "问题", "中立问题", 1,
                datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat(),
            ),
        )
        monkeypatch.setattr(
            "hongtu_geo.pipeline.resume_probe_batch",
            lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("探测批次仍在运行，不能并发恢复")),
        )
        result = run_probes(settings, db, limit=1)
        batch_count = db.query("SELECT COUNT(*) n FROM probe_batches")[0]["n"]
    assert result["batch_status"] == "concurrent_resume_skipped"
    assert result["calls_attempted"] == 0
    assert batch_count == 1


def test_sampling_health_orders_offset_timestamps_by_actual_utc_time(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, verified=True)
    with Database(settings.db_path) as db:
        db.execute(
            "INSERT INTO probe_batches(batch_id,started_at,status) VALUES(?,?,?)",
            ("older-interrupted", "2026-09-04T10:00:00+02:00", "interrupted"),
        )
        db.execute(
            "INSERT INTO probe_batches(batch_id,started_at,status) VALUES(?,?,?)",
            ("newer-completed", "2026-09-04T09:00:00+00:00", "completed"),
        )
        health = build_sampling_health(settings, db)
    assert health["latest_batch_status"] == "completed"
    assert health["status"] == "no_samples"
