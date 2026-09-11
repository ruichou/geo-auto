from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from .attribution import build_attribution_report, record_attribution_event
from .attribution_ingest import (
    AttributionIngestConfigurationError,
    build_attribution_ingest_readiness,
    verify_attribution_signature,
)
from .browser import load_platforms, run_browser_probes, run_due_jobs, sync_platform_accounts
from .core import Database, Settings, now_iso
from .evidence import audit_brand_facts
from .crawler import crawl_site
from .geo_engine import (
    build_answer_integrity_snapshot,
    build_citation_intelligence,
    build_citation_url_ledger,
    build_gap_analysis,
    build_maturity_audit,
    build_prompt_benchmark,
    build_strategy_snapshot,
    build_visibility_snapshot,
)
from .pipeline import (
    build_entity_assets,
    build_report,
    extract_official_facts,
    generate_drafts,
    quality_check,
    review_drafts,
    run_automation_pipeline,
    run_probes,
    schedule_connected_drafts,
    schedule_approved_drafts,
    seed_opportunities,
)
from .probe_readiness import build_ai_probe_readiness


settings = Settings.load()
scheduler = BackgroundScheduler(timezone="Asia/Shanghai")


def _run_pipeline(kind: str = "daily") -> None:
    run_automation_pipeline(settings, kind)


def _safe_spawn(command: str, argument: str) -> None:
    creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    subprocess.Popen(
        [sys.executable, "-m", "hongtu_geo.browser", command, argument],
        cwd=settings.root,
        creationflags=creation_flags,
        close_fds=True,
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    with Database(settings.db_path) as db:
        sync_platform_accounts(settings, db)
        seed_opportunities(settings, db)
    interval = int(settings.raw.get("autopilot", {}).get("publish_check_minutes", 2))
    scheduler.add_job(lambda: run_due_jobs(settings), "interval", minutes=interval, id="publisher", replace_existing=True, max_instances=1)
    if settings.raw.get("autopilot", {}).get("enabled", True):
        hour = int(settings.raw.get("autopilot", {}).get("daily_hour", 9))
        weekly_day = str(settings.raw.get("autopilot", {}).get("weekly_day", "mon"))
        scheduler.add_job(lambda: _run_pipeline("weekly"), "cron", day_of_week=weekly_day, hour=hour, minute=0, id="weekly", replace_existing=True, max_instances=1)
        week_days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        daily_days = ",".join(day for day in week_days if day != weekly_day)
        scheduler.add_job(lambda: _run_pipeline("daily"), "cron", day_of_week=daily_days, hour=hour, minute=0, id="daily", replace_existing=True, max_instances=1)
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="宏图商机 GEO 发布中台", lifespan=lifespan)


class ScheduleRequest(BaseModel):
    platforms: list[str]


class ModeRequest(BaseModel):
    mode: str


class AttributionEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    occurred_at: str | None = None
    event_type: str
    source_engine: str = "unknown"
    landing_url: str = ""
    utm_source: str = ""
    utm_medium: str = ""
    utm_campaign: str = ""
    anonymous_id: str = ""
    value: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


async def _verify_attribution_ingest(request: Request) -> None:
    body = await request.body()
    try:
        verify_attribution_signature(settings, body, request.headers)
    except AttributionIngestConfigurationError as exc:
        raise HTTPException(503, "归因接收端尚未完成安全配置") from exc
    except ValueError as exc:
        raise HTTPException(401, "归因请求签名无效") from exc


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return DASHBOARD_HTML


@app.get("/api/status")
def api_status() -> dict[str, Any]:
    with Database(settings.db_path) as db:
        accounts = [dict(row) for row in db.query("SELECT * FROM platform_accounts ORDER BY display_name")]
        drafts = [dict(row) for row in db.query("SELECT id,title,status,quality_score,created_at FROM drafts ORDER BY id DESC LIMIT 30")]
        jobs = [dict(row) for row in db.query(
            """SELECT j.*,d.title,a.display_name FROM publish_jobs j JOIN drafts d ON d.id=j.draft_id
            LEFT JOIN platform_accounts a ON a.platform=j.platform ORDER BY j.scheduled_at DESC LIMIT 50"""
        )]
        runs = [dict(row) for row in db.query("SELECT * FROM runs ORDER BY id DESC LIMIT 10")]
        gap_analysis = build_gap_analysis(settings, db, persist=False)
        actions = [
            {**item, "action_type": item["type"], "status": "open"}
            for item in gap_analysis["focus_actions"]
        ]
        visibility = build_visibility_snapshot(settings, db)
        answer_integrity = build_answer_integrity_snapshot(settings, db)
        maturity = build_maturity_audit(settings, db)
        attribution = build_attribution_report(settings, db)
        attribution_ingest = build_attribution_ingest_readiness(settings)
        citation_intelligence = build_citation_intelligence(settings, db, persist=False)
        citation_url_ledger = build_citation_url_ledger(settings, db, persist=False)
        evidence_audit = audit_brand_facts(settings)
        ai_probe_readiness = build_ai_probe_readiness(settings, db)
        probe_total = visibility["sample_size"]
        monitor = settings.raw.get("monitor", {})
        primary_variant = str(monitor.get("primary_prompt_variant", "naturalistic"))
        primary_surface = str(monitor.get("primary_engine_surface", "browser"))
        primary_version = str(monitor.get("primary_prompt_version", "naturalistic-v1"))
        probe_totals = db.query(
            "SELECT COALESCE(SUM(brand_mentioned),0) mentions,COALESCE(SUM(recommended),0) recommendations,"
            "COALESCE(SUM(domain_cited),0) citations,COUNT(*) naturalistic_samples FROM probes "
            "WHERE prompt_variant=? AND engine_surface=? AND prompt_version=?",
            (primary_variant, primary_surface, primary_version),
        )[0]
        metrics = {
            "questions": db.query("SELECT COUNT(*) n FROM opportunities")[0]["n"],
            "drafts": db.query("SELECT COUNT(*) n FROM drafts")[0]["n"],
            "approved": db.query("SELECT COUNT(*) n FROM drafts WHERE status='approved'")[0]["n"],
            "publish_jobs": db.query("SELECT COUNT(*) n FROM publish_jobs")[0]["n"],
            "published": db.query("SELECT COUNT(*) n FROM publish_jobs WHERE status='published'")[0]["n"],
            "probes": probe_total,
            "naturalistic_probes": int(probe_totals["naturalistic_samples"]),
            "mention_rate": round(probe_totals["mentions"] / probe_totals["naturalistic_samples"] * 100, 1) if probe_totals["naturalistic_samples"] else 0,
            "recommendation_rate": round(probe_totals["recommendations"] / probe_totals["naturalistic_samples"] * 100, 1) if probe_totals["naturalistic_samples"] else 0,
            "citation_rate": round(probe_totals["citations"] / probe_totals["naturalistic_samples"] * 100, 1) if probe_totals["naturalistic_samples"] else 0,
            "primary_measurement_scope": {
                "prompt_variant": primary_variant,
                "engine_surface": primary_surface,
                "prompt_version": primary_version,
            },
        }
    specs = load_platforms(settings)
    for account in accounts:
        account["notes"] = specs.get(account["platform"], {}).get("notes", "")
        account["kind"] = specs.get(account["platform"], {}).get("kind", "publisher")
    return {
        "brand": settings.brand,
        "publishing_mode": settings.raw["publishing"]["mode"],
        "publishing_paused": bool(settings.raw["publishing"].get("paused", False)),
        "automation_safety": {
            "content_generation_paused": bool(settings.raw.get("content", {}).get("generation_paused", False)),
            "api_probes_enabled": bool(settings.raw.get("monitor", {}).get("api_probes_enabled", False)),
            "browser_probes_enabled": bool(settings.raw.get("autopilot", {}).get("browser_probes_enabled", False)),
            "auto_schedule_connected_platforms": bool(
                settings.raw.get("autopilot", {}).get("auto_schedule_connected_platforms", False)
            ),
        },
        "metrics": metrics,
        "accounts": accounts,
        "drafts": drafts,
        "jobs": jobs,
        "runs": runs,
        "visibility": visibility,
        "answer_integrity": answer_integrity,
        "maturity": maturity,
        "actions": actions,
        "action_backlog": gap_analysis["backlog_summary"],
        "attribution": attribution,
        "attribution_ingest": attribution_ingest,
        "sampling_health": visibility["sampling_health"],
        "context_isolation": visibility["context_isolation"],
        "probe_provenance": visibility["probe_provenance"],
        "browser_activity": visibility["browser_activity"],
        "cross_run_reproducibility": visibility["cross_run_reproducibility"],
        "trends": visibility["trends"],
        "probe_batches": visibility["recent_batches"],
        "citation_intelligence": citation_intelligence,
        "citation_url_ledger": citation_url_ledger,
        "ai_probe_readiness": ai_probe_readiness,
        "evidence_audit": evidence_audit,
    }


@app.post("/api/platforms/{platform}/connect")
def connect(platform: str) -> dict[str, str]:
    if platform not in load_platforms(settings):
        raise HTTPException(404, "未知平台")
    _safe_spawn("connect", platform)
    return {"status": "started", "message": "已打开独立登录窗口，请完成注册/扫码/验证"}


@app.post("/api/platforms/{platform}/confirm")
def confirm_platform_login(platform: str) -> dict[str, Any]:
    if platform not in load_platforms(settings):
        raise HTTPException(404, "未知平台")
    with Database(settings.db_path) as db:
        db.execute(
            "UPDATE platform_accounts SET status=?,last_checked_at=?,last_error=NULL WHERE platform=?",
            ("connected", now_iso(), platform),
        )
        db.execute(
            """UPDATE publish_jobs SET status='pending',last_error=NULL,updated_at=?
            WHERE platform=? AND status='blocked' AND last_error LIKE '平台尚未连接%'""",
            (now_iso(), platform),
        )
        schedule = schedule_connected_drafts(settings, db)
    return {"status": "connected", "platform": platform, "schedule": schedule}


@app.post("/api/platforms/{platform}/open")
def open_studio(platform: str) -> dict[str, str]:
    if platform not in load_platforms(settings):
        raise HTTPException(404, "未知平台")
    _safe_spawn("open", platform)
    return {"status": "started"}


@app.post("/api/pipeline/{kind}")
def run_pipeline(kind: str, background: BackgroundTasks) -> dict[str, str]:
    if kind not in {"daily", "weekly"}:
        raise HTTPException(400, "kind 必须是 daily 或 weekly")
    background.add_task(_run_pipeline, kind)
    return {"status": "started", "message": f"{kind} 流程已在后台启动"}


@app.post("/api/geo/refresh")
def refresh_geo_system() -> dict[str, Any]:
    """Refresh GEO intelligence and staged assets without generating or publishing content."""
    assets = build_entity_assets(settings)
    with Database(settings.db_path) as db:
        benchmark = build_prompt_benchmark(settings, db)
        strategy = build_strategy_snapshot(settings, db)
        report = build_report(settings, db)
    return {
        "status": "ok",
        "assets": assets,
        "benchmark": str(benchmark),
        "strategy": str(strategy),
        "report": str(report),
    }


@app.post("/api/attribution/events")
def create_attribution_event(
    request: AttributionEventRequest,
    _verified: None = Depends(_verify_attribution_ingest),
) -> dict[str, Any]:
    try:
        with Database(settings.db_path) as db:
            return record_attribution_event(settings, db, request.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/jobs/schedule")
def schedule(request: ScheduleRequest) -> dict[str, Any]:
    if settings.raw.get("publishing", {}).get("paused", False):
        raise HTTPException(409, "发布已锁定暂停；需先在配置中明确解除 publishing.paused")
    valid = set(load_platforms(settings))
    selected = [item for item in request.platforms if item in valid]
    if not selected:
        raise HTTPException(400, "至少选择一个有效平台")
    with Database(settings.db_path) as db:
        return schedule_approved_drafts(settings, db, selected)


@app.post("/api/drafts/{draft_id}/approve")
def approve_draft(draft_id: int) -> dict[str, str]:
    with Database(settings.db_path) as db:
        rows = db.query("SELECT status,body,sources_json FROM drafts WHERE id=?", (draft_id,))
        if not rows:
            raise HTTPException(404, "草稿不存在")
        if rows[0]["status"] != "qa_passed":
            raise HTTPException(409, "草稿尚未通过事实与质量闸门")
        current_quality = quality_check(rows[0]["body"], settings, json.loads(rows[0]["sources_json"]))
        if not current_quality["publishable"]:
            db.execute(
                "UPDATE drafts SET status='review_needed',quality_score=?,updated_at=datetime('now') WHERE id=?",
                (current_quality["score"], draft_id),
            )
            raise HTTPException(409, f"事实或来源已经变化，需要重新审核：{','.join(current_quality['blockers'])}")
        db.execute("UPDATE drafts SET status='approved',updated_at=datetime('now') WHERE id=?", (draft_id,))
    return {"status": "approved"}


@app.post("/api/mode")
def set_mode(request: ModeRequest) -> dict[str, str]:
    if request.mode not in {"queue", "live"}:
        raise HTTPException(400, "mode 必须是 queue 或 live")
    if request.mode == "live" and settings.raw.get("publishing", {}).get("paused", False):
        raise HTTPException(409, "发布已锁定暂停；需先在配置中明确解除 publishing.paused")
    config_path = settings.root / "config" / "site.json"
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["publishing"]["mode"] = request.mode
    config_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    settings.raw["publishing"]["mode"] = request.mode
    return {"status": "ok", "mode": request.mode}


DASHBOARD_HTML = r'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
  <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><rect width='64' height='64' rx='14' fill='%23d4472f'/><text x='32' y='44' text-anchor='middle' font-size='38' fill='white'>宏</text></svg>">
  <title>宏图商机 GEO 发布中台</title>
  <style>
    :root{--ink:#151711;--paper:#f4f1e8;--card:#fffdf8;--red:#d4472f;--green:#1f6b4f;--muted:#746f64;--line:#d9d2c4;--nav:#18231d}
    *{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:14px/1.55 Inter,"PingFang SC","Microsoft YaHei",sans-serif}
    header{background:var(--nav);color:#fff;padding:22px 32px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:5}
    header h1{font-size:22px;margin:0;letter-spacing:.04em}header p{margin:2px 0 0;color:#aebdb4}.brand{display:flex;gap:14px;align-items:center}.mark{width:42px;height:42px;border-radius:12px;background:var(--red);display:grid;place-items:center;font-weight:800;font-size:19px}
    main{max-width:1440px;margin:auto;padding:28px 32px 64px}.toolbar{display:flex;gap:10px;flex-wrap:wrap}.btn{border:0;border-radius:9px;padding:10px 15px;background:var(--ink);color:#fff;cursor:pointer;font-weight:650}.btn.alt{background:#fff;color:var(--ink);border:1px solid var(--line)}.btn.live{background:var(--red)}
    .grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:22px 0}.metric,.card{background:var(--card);border:1px solid var(--line);border-radius:14px;box-shadow:0 4px 18px rgba(50,45,35,.04)}.metric{padding:20px}.metric b{font:700 30px/1.1 Georgia,serif;display:block}.metric span{color:var(--muted)}
    section{margin-top:24px}.section-title{display:flex;justify-content:space-between;align-items:end;margin-bottom:12px}.section-title h2{margin:0;font:700 23px Georgia,"Songti SC",serif}.section-title small{color:var(--muted)}
    .platforms{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.platform{padding:17px}.platform h3{margin:0 0 5px}.platform p{color:var(--muted);min-height:44px}.status{display:inline-flex;align-items:center;gap:6px;font-size:12px;padding:3px 8px;border-radius:99px;background:#eee}.status.connected{background:#dceee5;color:var(--green)}.status.error{background:#f7dfda;color:#962f20}.dot{width:7px;height:7px;border-radius:50%;background:currentColor}.actions{display:flex;gap:7px}.actions .btn{padding:7px 10px;font-size:12px}
    .table-card{overflow:auto}.table-card table{width:100%;border-collapse:collapse;min-width:800px}th,td{text-align:left;padding:12px 14px;border-bottom:1px solid #ebe5d9}th{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);background:#faf7f0}td.title{max-width:440px}.pill{padding:3px 7px;border-radius:6px;background:#ece8de;font-size:12px}.empty{padding:26px;color:var(--muted);text-align:center}
    .notice{background:#fff4d9;border:1px solid #e9d49c;padding:12px 15px;border-radius:10px;margin:14px 0;color:#675421}.toast{position:fixed;right:24px;bottom:24px;background:var(--nav);color:#fff;padding:12px 16px;border-radius:9px;display:none;box-shadow:0 8px 30px #0003}
    @media(max-width:900px){.grid{grid-template-columns:repeat(2,1fr)}.platforms{grid-template-columns:1fr}main{padding:20px}header{padding:18px 20px}}
  </style>
</head>
<body>
<header><div class="brand"><div class="mark">宏</div><div><h1>宏图商机汇 GEO 中台</h1><p>证据 · 实体 · 内容 · AI 可见度 · 获客归因</p></div></div><div id="mode"></div></header>
<main>
  <div class="toolbar"><button class="btn" onclick="refreshGeo()">刷新 GEO 诊断</button><button class="btn alt" onclick="runPipe('daily')">立即跑每日流程</button><button class="btn alt" onclick="runPipe('weekly')">运行全面周审计</button><button class="btn alt" onclick="connectNext()">连接下一个平台</button><button class="btn alt" onclick="scheduleSelected()">审核稿一键排期</button><button class="btn alt" onclick="reload()">刷新状态</button><button class="btn live" id="modeBtn" onclick="toggleMode()">切换发布模式</button></div>
  <div class="notice">GEO 成效分开计算品牌提及、正向推荐、自有域名引用和最终转化；任何单项都不等于第三方 AI 的保证推荐。当前发布模式为 <b id="noticeMode"></b>；<span id="automationSafety"></span>。</div>
  <div class="grid" id="metrics"></div>
  <section><div class="section-title"><h2>品牌事实证据</h2><small>布尔核验标记不等于证据有效；来源漂移或主张映射不完整会自动停用事实</small></div><div class="card table-card"><table><thead><tr><th>状态</th><th>事实</th><th>来源</th><th>支持词</th><th>主张映射</th><th>核验日期</th><th>证据年龄</th><th>摘要指纹</th></tr></thead><tbody id="evidenceRows"></tbody></table></div></section>
  <section><div class="section-title"><h2>采样健康</h2><small>自然原问是主口径；核心问题或独立引擎覆盖不足时不能判定健康</small></div><div class="card table-card"><table><thead><tr><th>全部模式</th><th>全部样本</th><th>自然原问状态</th><th>自然原问样本</th><th>自然达标组</th><th>核心覆盖</th><th>引擎覆盖</th><th>结果一致率</th><th>诊断</th></tr></thead><tbody id="samplingHealth"></tbody></table></div></section>
  <section><div class="section-title"><h2>跨批次复现性</h2><small>同批稳定不等于跨日期稳定；不同引擎绝不混算</small></div><div class="card table-card"><table><thead><tr><th>状态</th><th>协议组</th><th>达标批次</th><th>可比较组</th><th>稳定组</th><th>波动组</th><th>阈值</th><th>诊断</th></tr></thead><tbody id="crossRun"></tbody></table></div></section>
  <section><div class="section-title"><h2>AI 监测适配器</h2><small>只读检查配置和本地登录声明；只有成功采样才构成测量证据</small></div><div class="card table-card"><table><thead><tr><th>状态</th><th>平台</th><th>适配器</th><th>账号状态</th><th>可尝试采样</th><th>上次检查</th><th>诊断</th></tr></thead><tbody id="aiProbeReadiness"></tbody></table></div></section>
  <section><div class="section-title"><h2>浏览器会话隔离</h2><small>每条样本发送前验证对话为空，旧样本不追溯背书</small></div><div class="card table-card"><table><thead><tr><th>状态</th><th>浏览器样本</th><th>隔离已验证</th><th>历史未验证</th><th>隔离失败</th><th>元数据异常</th><th>诊断</th></tr></thead><tbody id="contextIsolation"></tbody></table></div></section>
  <section><div class="section-title"><h2>采样溯源回执</h2><small>记录采集方式、提取器版本和无密钥适配器指纹；旧样本不补造</small></div><div class="card table-card"><table><thead><tr><th>状态</th><th>样本</th><th>完整回执</th><th>历史缺失</th><th>元数据异常</th><th>适配器版本数</th><th>模型身份</th><th>诊断</th></tr></thead><tbody id="probeProvenance"></tbody></table></div></section>
  <section><div class="section-title"><h2>浏览器启动审计</h2><small>可见窗口必须来自用户明确操作；自动探测和发布只能后台无头运行</small></div><div class="card table-card"><table><thead><tr><th>状态</th><th>已记录启动</th><th>用户可见</th><th>自动化可见</th><th>最近可见平台</th><th>动作</th><th>时间</th><th>策略</th></tr></thead><tbody id="browserActivity"></tbody></table></div></section>
  <section><div class="section-title"><h2>多周期趋势</h2><small>只用两侧相同问题、每题等量样本的匹配面板计算变化</small></div><div class="card table-card"><table><thead><tr><th>引擎 / surface</th><th>状态</th><th>原始样本</th><th>匹配问题</th><th>面板样本/侧</th><th>提及变化</th><th>推荐变化</th><th>引用变化</th><th>区间信号</th></tr></thead><tbody id="trendRows"></tbody></table></div></section>
  <section><div class="section-title"><h2>AI 引用来源候选</h2><small>重复采样不等于独立证据，候选不代表背书</small></div><div class="card table-card"><table><thead><tr><th>域名</th><th>分类</th><th>证据强度</th><th>原始样本</th><th>独立观察</th><th>引擎数</th><th>surface 组合</th><th>问题数</th><th>状态</th></tr></thead><tbody id="citationSources"></tbody></table></div></section>
  <section><div class="section-title"><h2>具体引用链接台账</h2><small>查询参数已移除；仅白名单、重复观察的链接可在周流程受限核验</small></div><div class="card table-card"><table><thead><tr><th>规范链接</th><th>域名</th><th>独立观察</th><th>证据强度</th><th>复核状态</th><th>网络状态</th><th>HTTP</th></tr></thead><tbody id="citationUrls"></tbody></table></div></section>
  <section><div class="section-title"><h2>品牌回答真实性警戒</h2><small>识别疑似编造电话、官网、绝对化排名和无证据量化主张；未命中不等于全部事实真实</small></div><div class="card table-card"><table><thead><tr><th>状态</th><th>品牌样本</th><th>需复核</th><th>主口径样本</th><th>主口径需复核</th><th>主口径风险率</th><th>风险类型</th></tr></thead><tbody id="answerIntegrity"></tbody></table></div></section>
  <section><div class="section-title"><h2>竞品可见度观察</h2><small>仅统计真实回答中的已核实名称；观察份额不等于市场份额</small></div><div class="card table-card"><table><thead><tr><th>平台</th><th>提及样本</th><th>样本提及率</th><th>95% 区间</th><th>问题数</th><th>引擎数</th><th>与品牌同现</th></tr></thead><tbody id="competitorVisibility"></tbody></table></div></section>
  <section><div class="section-title"><h2>获客归因质量</h2><small>首触与末触并列观察，不把规则归因当作因果增量</small></div><div class="card table-card"><table><thead><tr><th>状态</th><th>原始事件</th><th>有效事件</th><th>AI 访问</th><th>咨询</th><th>成交</th><th>咨询中位天数</th><th>成交中位天数</th><th>完整诊断</th></tr></thead><tbody id="attributionQuality"></tbody></table></div></section>
  <section><div class="section-title"><h2>最近探测批次</h2><small>逐项清单支持中断后精确续采</small></div><div class="card table-card"><table><thead><tr><th>批次</th><th>状态</th><th>计划</th><th>成功</th><th>失败</th><th>待执行</th><th>累计尝试</th><th>开始时间</th></tr></thead><tbody id="probeBatches"></tbody></table></div></section>
  <section><div class="section-title"><h2>GEO 聚焦行动队列</h2><small>优先核心问题与已有证据的长尾缺口；单引擎结论仅作初步诊断</small></div><div class="card table-card"><table><thead><tr><th>优先级</th><th>层级</th><th>问题</th><th>差距类型</th><th>证据等级</th><th>下一步</th></tr></thead><tbody id="geoActions"></tbody></table></div></section>
  <section><div class="section-title"><h2>平台矩阵</h2><small>每个平台使用独立登录态</small></div><div class="platforms" id="platforms"></div></section>
  <section><div class="section-title"><h2>内容队列</h2><small>来源、官网事实与正文引用全部通过后自动批准</small></div><div class="card table-card"><table><thead><tr><th>ID</th><th>标题</th><th>状态</th><th>质量分</th><th>创建时间</th></tr></thead><tbody id="drafts"></tbody></table></div></section>
  <section><div class="section-title"><h2>发布任务</h2><small>系统每 2 分钟检查一次到期任务</small></div><div class="card table-card"><table><thead><tr><th>平台</th><th>内容</th><th>计划时间</th><th>状态</th><th>结果</th></tr></thead><tbody id="jobs"></tbody></table></div></section>
</main><div class="toast" id="toast"></div>
<script>
let state={};const esc=s=>String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
function toast(msg){const t=document.getElementById('toast');t.textContent=msg;t.style.display='block';setTimeout(()=>t.style.display='none',3500)}
async function call(url,opts={}){const r=await fetch(url,{headers:{'Content-Type':'application/json'},...opts});const x=await r.json();if(!r.ok)throw Error(x.detail||'操作失败');return x}
async function reload(){state=await call('/api/status');render()}
function render(){document.getElementById('mode').innerHTML=`发布模式：<b>${state.publishing_paused?'已锁定暂停':esc(state.publishing_mode)}</b>`;document.getElementById('noticeMode').textContent=state.publishing_paused?'已锁定暂停':state.publishing_mode;document.getElementById('modeBtn').textContent=state.publishing_paused?'发布已锁定':(state.publishing_mode==='live'?'关闭正式发布':'开启正式发布');document.getElementById('modeBtn').disabled=state.publishing_paused;
const safe=state.automation_safety;document.getElementById('automationSafety').textContent=`内容生成${safe.content_generation_paused?'暂停':'开启'}、API 探测${safe.api_probes_enabled?'开启':'关闭'}、浏览器探测${safe.browser_probes_enabled?'开启':'关闭'}、自动排期${safe.auto_schedule_connected_platforms?'开启':'关闭'}`;
const m=state.metrics;document.getElementById('metrics').innerHTML=[['系统成熟度',state.maturity.score+'%'],['问题库',m.questions],['审核通过',m.approved],['自然原问样本',m.naturalistic_probes],['自然原问提及率',m.mention_rate+'%'],['自然原问推荐率',m.recommendation_rate+'%'],['自然原问引用率',m.citation_rate+'%'],['已发布',m.published]].map(x=>`<div class="metric"><b>${x[1]}</b><span>${x[0]}</span></div>`).join('');
const ea=state.evidence_audit;document.getElementById('evidenceRows').innerHTML=ea.receipts.length?ea.receipts.map(r=>`<tr><td><span class="pill">${esc(r.status)}</span></td><td class="title">${esc(r.claim)}</td><td class="title">${esc(r.source)}</td><td>${r.matched_support_term_count||0}/${r.support_term_count||0}</td><td>${r.claim_assertion_count||0}/${r.claim_map_covers_full_claim?'完整':'不完整'}</td><td>${esc(r.verified_at||'未填写')}</td><td>${r.verification_age_days==null?'—':r.verification_age_days+' 天'}</td><td>${esc((r.excerpt_sha256||'—').slice(0,12))}</td></tr>`).join(''):`<tr><td colspan="8" class="empty">尚未配置品牌事实</td></tr>`;
const h=state.sampling_health,pc=h.priority_question_coverage,ec=h.primary_engine_coverage;document.getElementById('samplingHealth').innerHTML=`<tr><td><span class="pill">${esc(h.status)}</span></td><td>${h.total_samples}</td><td><span class="pill">${esc(h.primary_status)}</span></td><td>${h.primary_samples}</td><td>${h.primary_target_reached_groups}</td><td>${pc.repeat_ready_questions}/${pc.question_count}</td><td>${ec.repeat_ready_provider_count}/${ec.minimum_providers}</td><td>${h.average_outcome_agreement==null?'暂无':h.average_outcome_agreement+'%'}</td><td>${esc(h.warnings.join('；')||h.method_note)}</td></tr>`;
const cr=state.cross_run_reproducibility;document.getElementById('crossRun').innerHTML=`<tr><td><span class="pill">${esc(cr.status)}</span></td><td>${cr.protocol_count}</td><td>${cr.eligible_experiments}</td><td>${cr.comparable_protocols}</td><td>${cr.stable_protocols}</td><td>${cr.variable_protocols}</td><td>${cr.variability_threshold_percentage_points}pp</td><td>${esc(cr.method_note)}</td></tr>`;
const ar=state.ai_probe_readiness;document.getElementById('aiProbeReadiness').innerHTML=ar.providers.length?ar.providers.map(p=>`<tr><td><span class="pill">${esc(ar.status)}</span></td><td>${esc(p.name)} (${esc(p.provider)})</td><td>${p.adapter_valid?'有效':'无效'}</td><td>${esc(p.account_status)}</td><td>${p.ready_for_attempt?'是':'否'}</td><td>${esc(p.last_checked_at||'未检查')}</td><td>${esc(p.configuration_errors.join('；')||(p.retry_after?'临时受限；自动重试 '+p.retry_after:(p.last_error_present?'存在最近错误；请查看账号详情':ar.method_note)))}</td></tr>`).join(''):`<tr><td colspan="7" class="empty">尚未配置 AI 监测适配器</td></tr>`;
const ix=state.context_isolation;document.getElementById('contextIsolation').innerHTML=`<tr><td><span class="pill">${esc(ix.status)}</span></td><td>${ix.browser_samples}</td><td>${ix.fresh_context_verified_samples}</td><td>${ix.legacy_unverified_samples}</td><td>${ix.isolation_failures}</td><td>${ix.malformed_metadata_samples}</td><td>${esc(ix.method_note)}</td></tr>`;
const pv=state.probe_provenance;document.getElementById('probeProvenance').innerHTML=`<tr><td><span class="pill">${esc(pv.status)}</span></td><td>${pv.samples}</td><td>${pv.complete_receipts}</td><td>${pv.legacy_without_receipt}</td><td>${pv.malformed_metadata_samples}</td><td>${pv.adapter_fingerprint_count}</td><td>${esc(Object.entries(pv.model_identity_statuses).map(x=>x[0]+':'+x[1]).join('；')||'历史样本未记录')}</td><td>${esc(pv.method_note)}</td></tr>`;
const ba=state.browser_activity,bv=ba.last_visible_launch||{};document.getElementById('browserActivity').innerHTML=`<tr><td><span class="pill">${esc(ba.status)}</span></td><td>${ba.recorded_launches}</td><td>${ba.visible_user_launches}</td><td>${ba.visible_automation_launches}</td><td>${esc(bv.platform||'暂无')}</td><td>${esc(bv.action||'—')}</td><td>${esc(bv.occurred_at||'—')}</td><td>${esc(ba.policy)}</td></tr>`;
const t=state.trends;document.getElementById('trendRows').innerHTML=t.comparisons.length?t.comparisons.map(r=>{const d=r.deltas_percentage_points,p=r.matched_panel;const fmt=v=>v==null?'样本不足':`${v>0?'+':''}${v}pp`;return `<tr><td>${esc(r.provider)} / ${esc(r.engine_surface)} / ${esc(r.prompt_variant)} / ${esc(r.prompt_version)} / ${esc(r.locale)}-${esc(r.region)}</td><td><span class="pill">${esc(r.status)}</span></td><td>${r.current.samples} / ${r.previous.samples}</td><td>${p.matched_question_count}/${p.minimum_matched_questions}</td><td>${p.paired_samples_per_window}</td><td>${fmt(d.mention_rate)}</td><td>${fmt(d.recommendation_rate)}</td><td>${fmt(d.owned_citation_rate)}</td><td>${esc(r.non_overlapping_ci_signals.join('、')||'无')}</td></tr>`}).join(''):`<tr><td colspan="9" class="empty">尚无可比较的真实样本</td></tr>`;
const ci=state.citation_intelligence;document.getElementById('citationSources').innerHTML=ci.sources.length?ci.sources.map(s=>`<tr><td class="title">${esc(s.domain)}</td><td>${esc(s.category)}</td><td><span class="pill">${esc(s.evidence_strength)}</span></td><td>${s.sample_count}</td><td>${s.independent_count}</td><td>${s.provider_count}</td><td>${s.surface_count}</td><td>${s.question_count}</td><td>${esc(s.review_status)}</td></tr>`).join(''):`<tr><td colspan="9" class="empty">尚无带引用链接的真实 AI 样本</td></tr>`;
const cul=state.citation_url_ledger;document.getElementById('citationUrls').innerHTML=cul.candidates.length?cul.candidates.map(s=>`<tr><td class="title">${esc(s.canonical_url)}</td><td>${esc(s.domain)}</td><td>${s.independent_count}</td><td><span class="pill">${esc(s.evidence_strength)}</span></td><td>${esc(s.review_status)}</td><td>${esc(s.network_status)}</td><td>${s.http_status==null?'—':s.http_status}</td></tr>`).join(''):`<tr><td colspan="7" class="empty">尚无具体引用链接</td></tr>`;
const ai=state.answer_integrity;document.getElementById('answerIntegrity').innerHTML=`<tr><td><span class="pill">${esc(ai.status)}</span></td><td>${ai.audited_brand_samples}</td><td>${ai.flagged_samples}</td><td>${ai.primary_audited_samples}</td><td>${ai.primary_flagged_samples}</td><td>${ai.primary_flag_rate==null?'暂无':ai.primary_flag_rate+'%'}</td><td>${esc(Object.entries(ai.issue_counts).map(x=>x[0]+':'+x[1]).join('；')||'未发现高置信风险')}</td></tr>`;
const cv=state.visibility.competitor_visibility||[];document.getElementById('competitorVisibility').innerHTML=cv.length?cv.map(c=>`<tr><td>${esc(c.name)}</td><td>${c.mentions}</td><td>${c.sample_mention_rate}%</td><td>${c.mention_rate_ci95?c.mention_rate_ci95.join('–')+'%':'暂无'}</td><td>${c.question_count}</td><td>${c.provider_count}</td><td>${c.brand_co_mentions}</td></tr>`).join(''):`<tr><td colspan="7" class="empty">当前真实样本尚未提及已核实商业平台</td></tr>`;
const aq=state.attribution,ig=state.attribution_ingest,qd=aq.data_quality,af=aq.attributable_funnel,td=aq.time_to_conversion_days;const qdiag=`接入 ${ig.status}｜密钥 ${ig.secret_strong_enough?'就绪':'未就绪'}｜契约 ${ig.contract_ready?'就绪':'缺失'}｜缺匿名 ${qd.anonymous_missing_events}｜孤立 ${qd.orphan_downstream_subjects}｜重复 ${qd.duplicate_stage_events}｜倒序 ${qd.out_of_order_subjects}｜非法类型 ${qd.invalid_event_type_events}｜非法时间 ${qd.invalid_timestamp_events}｜非法金额 ${qd.invalid_value_events}｜未来 ${qd.future_events}`;document.getElementById('attributionQuality').innerHTML=`<tr><td><span class="pill">${esc(qd.status)}</span></td><td>${aq.events}</td><td>${aq.valid_events}</td><td>${af.ai_referral}</td><td>${af.inquiry}</td><td>${af.won}</td><td>${td.median_to_inquiry??'暂无'}</td><td>${td.median_to_won??'暂无'}</td><td class="title">${esc(qdiag)}</td></tr>`;
document.getElementById('probeBatches').innerHTML=state.probe_batches.length?state.probe_batches.map(b=>`<tr><td class="title">${esc(b.batch_id)}</td><td><span class="pill">${esc(b.status)}</span></td><td>${b.item_total||b.planned_calls}</td><td>${b.item_succeeded}</td><td>${b.item_failed}</td><td>${b.item_pending}</td><td>${b.attempted_calls}</td><td>${esc(b.started_at)}</td></tr>`).join(''):`<tr><td colspan="8" class="empty">尚无 API 探测批次</td></tr>`;
document.getElementById('geoActions').innerHTML=state.actions.length?state.actions.map(a=>`<tr><td><span class="pill">P${esc(a.priority)}</span></td><td>${esc(a.tier)}</td><td class="title">${esc(a.question)}</td><td>${esc(a.action_type)}</td><td>${esc(a.confidence)}</td><td class="title">${esc(a.recommended_next_step)}</td></tr>`).join(''):`<tr><td colspan="6" class="empty">暂无聚焦行动；全量长尾待办仍保留在策略快照</td></tr>`;
document.getElementById('platforms').innerHTML=state.accounts.map(a=>`<div class="card platform"><span class="status ${esc(a.status)}"><i class="dot"></i>${esc(a.status)}</span>${a.kind==='publisher'?`<label style="float:right;color:var(--muted)"><input class="platform-check" type="checkbox" value="${esc(a.platform)}" ${a.status==='connected'?'checked':''}> 排期</label>`:'<span style="float:right;color:var(--green);font-size:12px">AI 监测</span>'}<h3>${esc(a.display_name)}</h3><p>${esc(a.notes)}</p><div class="actions"><button class="btn" onclick="connect('${a.platform}')">注册 / 登录</button><button class="btn alt" onclick="openStudio('${a.platform}')">打开工作台</button></div>${a.last_error?`<small style="color:#a33">${esc(a.last_error)}</small>`:''}</div>`).join('');
document.getElementById('drafts').innerHTML=state.drafts.length?state.drafts.map(d=>`<tr><td>#${d.id}</td><td class="title">${esc(d.title)}</td><td><span class="pill">${esc(d.status)}</span></td><td>${d.quality_score??0}</td><td>${esc(d.created_at)}</td></tr>`).join(''):'<tr><td colspan="5" class="empty">运行每日流程后，这里会出现内容</td></tr>';
document.getElementById('jobs').innerHTML=state.jobs.length?state.jobs.map(j=>`<tr><td>${esc(j.display_name||j.platform)}</td><td class="title">${esc(j.title)}</td><td>${esc(j.scheduled_at)}</td><td><span class="pill">${esc(j.status)}</span></td><td>${j.published_url?`<a href="${esc(j.published_url)}" target="_blank">查看</a>`:esc(j.last_error||'—')}</td></tr>`).join(''):'<tr><td colspan="5" class="empty">审核通过的内容可加入发布计划</td></tr>'}
async function connect(p){try{const x=await call(`/api/platforms/${p}/connect`,{method:'POST'});toast(x.message);setTimeout(reload,2500)}catch(e){toast(e.message)}}
function connectNext(){const next=state.accounts.find(a=>a.status!=='connected'&&a.status!=='connecting');if(!next){toast('所有平台均已连接或正在连接');return}connect(next.platform)}
async function openStudio(p){try{await call(`/api/platforms/${p}/open`,{method:'POST'});toast('浏览器已启动')}catch(e){toast(e.message)}}
async function runPipe(k){try{const x=await call(`/api/pipeline/${k}`,{method:'POST'});toast(x.message);setTimeout(reload,3000)}catch(e){toast(e.message)}}
async function refreshGeo(){try{await call('/api/geo/refresh',{method:'POST'});toast('GEO 资产、基准、差距和报告已刷新');reload()}catch(e){toast(e.message)}}
async function scheduleSelected(){const platforms=[...document.querySelectorAll('.platform-check:checked')].map(x=>x.value);if(!platforms.length){toast('请先勾选平台');return}try{const x=await call('/api/jobs/schedule',{method:'POST',body:JSON.stringify({platforms})});toast(`已新增 ${x.created} 个发布任务`);reload()}catch(e){toast(e.message)}}
async function toggleMode(){const mode=state.publishing_mode==='live'?'queue':'live';if(mode==='live'&&!confirm('正式发布会在任务到期时自动点击平台发布按钮。确认开启？'))return;try{await call('/api/mode',{method:'POST',body:JSON.stringify({mode})});toast(`已切换为 ${mode}`);reload()}catch(e){toast(e.message)}}
reload();setInterval(reload,10000);
</script></body></html>'''


def run_dashboard(host: str = "127.0.0.1", port: int = 8765) -> None:
    import uvicorn
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("安全限制：控制台只能监听本机回环地址")
    uvicorn.run("hongtu_geo.app:app", host=host, port=port, reload=False)
