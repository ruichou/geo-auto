from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from .core import Database, Settings, now_iso
from .browser_activity import begin_browser_launch, finish_browser_launch
from .geo_engine import _repeat_target, record_probe
from .provenance import build_probe_provenance


class TemporaryAIRestriction(RuntimeError):
    def __init__(self, message: str, retry_after: str):
        super().__init__(message)
        self.retry_after = retry_after


def _parse_ai_temporary_restriction(
    text: str, now_utc: datetime | None = None,
) -> tuple[str, str] | None:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    markers = ("账号已被禁言", "已被禁言至", "账号暂时受限", "账号已被限制")
    if not any(marker in normalized for marker in markers):
        return None
    match = re.search(
        r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(\d{1,2})\s*[:：]\s*(\d{2})",
        normalized,
    )
    if match:
        year, month, day, hour, minute = (int(value) for value in match.groups())
        local = datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("Asia/Shanghai"))
        retry_after = local.astimezone(UTC).replace(microsecond=0).isoformat()
    else:
        retry_after = ((now_utc or datetime.now(UTC)) + timedelta(hours=24)).replace(
            microsecond=0
        ).isoformat()
    return (
        f"AI 监测账号暂时受限；将在 {retry_after} 后自动重试",
        retry_after,
    )


def _raise_if_ai_temporarily_restricted(page: Any) -> None:
    try:
        body_text = page.locator("body").inner_text(timeout=5000)
    except Exception:
        return
    restriction = _parse_ai_temporary_restriction(body_text)
    if restriction:
        raise TemporaryAIRestriction(*restriction)


def load_platforms(settings: Settings) -> dict[str, dict[str, Any]]:
    return json.loads((settings.root / "config" / "platforms.json").read_text(encoding="utf-8"))


def profile_path(settings: Settings, platform: str) -> Path:
    path = settings.root / "data" / "browser-profiles" / platform
    path.mkdir(parents=True, exist_ok=True)
    marker = path / ".permissions-hardened"
    if not marker.exists():
        if os.name == "nt":
            user = os.getenv("USERNAME") or getpass.getuser()
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(OI)(CI)F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            path.chmod(0o700)
        marker.write_text("Local browser session data; do not back up or share.\n", encoding="utf-8")
    return path


def sync_platform_accounts(settings: Settings, db: Database) -> None:
    for key, item in load_platforms(settings).items():
        db.execute(
            """INSERT INTO platform_accounts(platform,display_name,profile_dir)
            VALUES(?,?,?) ON CONFLICT(platform) DO UPDATE SET display_name=excluded.display_name,
            profile_dir=excluded.profile_dir""",
            (key, item["name"], str(profile_path(settings, key))),
        )


def _looks_logged_in(page: Any, spec: dict[str, Any]) -> bool:
    current = page.url
    url_matches = any(piece in current for piece in spec.get("logged_in_url_contains", []))
    if url_matches and not re.search(r"login|sign[_-]?in|passport", current, re.I):
        # Some AI products expose the chat composer before authentication. A
        # visible login control is therefore stronger evidence than URL shape.
        if _first_visible(page, spec.get("logged_out_locators", [])) is not None:
            return False
        required = spec.get("logged_in_locators", [])
        if not required:
            return True
        return _first_visible(page, required) is not None
    return False


def connect_platform(settings: Settings, platform: str, timeout_minutes: int = 30) -> int:
    specs = load_platforms(settings)
    if platform not in specs:
        raise SystemExit(f"未知平台：{platform}")
    spec = specs[platform]
    with Database(settings.db_path) as db:
        sync_platform_accounts(settings, db)
        db.execute("UPDATE platform_accounts SET status='connecting',last_error=NULL WHERE platform=?", (platform,))
    launch_event = begin_browser_launch(
        settings, platform, "connect", visible=True, trigger="user_explicit"
    )
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_path(settings, platform)),
                headless=False,
                viewport={"width": 1440, "height": 900},
                locale="zh-CN",
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(spec["login_url"], wait_until="domcontentloaded", timeout=60_000)
            deadline = time.time() + timeout_minutes * 60
            connected = False
            while time.time() < deadline:
                with Database(settings.db_path) as db:
                    account = db.query("SELECT status FROM platform_accounts WHERE platform=?", (platform,))
                if account and account[0]["status"] == "connected":
                    connected = True
                    break
                if _looks_logged_in(page, spec):
                    connected = True
                    break
                if not context.pages:
                    break
                page = context.pages[-1]
                time.sleep(2)
            if connected:
                with Database(settings.db_path) as db:
                    db.execute(
                        "UPDATE platform_accounts SET status='connected',last_checked_at=?,last_error=NULL WHERE platform=?",
                        (now_iso(), platform),
                    )
                    db.execute(
                        """UPDATE publish_jobs SET status='pending',last_error=NULL,updated_at=?
                        WHERE platform=? AND status='blocked' AND last_error LIKE '平台尚未连接%'""",
                        (now_iso(), platform),
                    )
                    from .pipeline import schedule_connected_drafts
                    schedule_connected_drafts(settings, db)
                time.sleep(3)
            else:
                with Database(settings.db_path) as db:
                    db.execute(
                        "UPDATE platform_accounts SET status='not_connected',last_checked_at=?,last_error=? WHERE platform=?",
                        (now_iso(), "登录未确认；可能是窗口被关闭或超时", platform),
                    )
            context.close()
            finish_browser_launch(
                settings, launch_event, "completed" if connected else "login_required",
                None if connected else "登录未确认；可能是窗口被关闭或超时",
            )
            return 0 if connected else 2
    except Exception as exc:
        finish_browser_launch(settings, launch_event, "failed", str(exc))
        with Database(settings.db_path) as db:
            db.execute(
                "UPDATE platform_accounts SET status='error',last_checked_at=?,last_error=? WHERE platform=?",
                (now_iso(), str(exc)[:1000], platform),
            )
        raise


def open_platform(settings: Settings, platform: str) -> int:
    specs = load_platforms(settings)
    if platform not in specs:
        raise SystemExit(f"未知平台：{platform}")
    from playwright.sync_api import sync_playwright

    launch_event = begin_browser_launch(
        settings, platform, "open_studio", visible=True, trigger="user_explicit"
    )
    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_path(settings, platform)),
                headless=False,
                viewport={"width": 1440, "height": 900},
                locale="zh-CN",
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(specs[platform]["studio_url"], wait_until="domcontentloaded", timeout=60_000)
            while context.pages:
                time.sleep(2)
        finish_browser_launch(settings, launch_event, "closed")
        return 0
    except Exception as exc:
        finish_browser_launch(settings, launch_event, "failed", str(exc))
        raise


def _first_visible(page: Any, selectors: list[str]) -> Any | None:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.is_visible(timeout=1500):
                return locator
        except Exception:
            continue
    return None


def _first_attached(page: Any, selectors: list[str]) -> Any | None:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count():
                return locator
        except Exception:
            continue
    return None


def _fill_editor(locator: Any, value: str, rich_html: str | None = None) -> None:
    if rich_html and locator.get_attribute("contenteditable") == "true":
        # Framework editors such as Zhihu's may display directly assigned HTML
        # without updating their internal document state, leaving Publish disabled.
        # Playwright's fill follows the browser editing path and emits the events
        # the editor expects. Plain text is preferable to a visually rich but
        # unsaved DOM mutation.
        locator.fill(value)
        return
    try:
        locator.fill(value)
    except Exception:
        locator.click()
        locator.evaluate("(el, text) => { el.innerHTML = ''; el.textContent = text; el.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:text})); }", value)


def _markdown_to_safe_html(value: str) -> str:
    import bleach
    import markdown

    rendered = markdown.markdown(value, extensions=["tables", "fenced_code"])
    tags = set(bleach.sanitizer.ALLOWED_TAGS) | {
        "p", "h1", "h2", "h3", "h4", "br", "hr", "pre", "code",
        "ul", "ol", "li", "blockquote", "table", "thead", "tbody", "tr", "th", "td",
    }
    return bleach.clean(rendered, tags=tags, attributes={"a": ["href", "title"]}, strip=True)


def _tracked_conversion_url(url: str, platform: str, draft_id: int, campaign: str) -> str:
    if not url.startswith(("http://", "https://")):
        return url
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({
        "utm_source": platform,
        "utm_medium": "geo_content",
        "utm_campaign": campaign,
        "utm_content": f"draft-{draft_id}",
    })
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _platform_body(settings: Settings, platform: str, job: Any) -> str:
    body = re.sub(r"^---.*?---\s*", "", job["body"], flags=re.S)
    conversion_url = str(settings.brand.get("conversion_url", ""))
    campaign = str(settings.raw.get("acquisition", {}).get("tracking_campaign", "geo"))
    tracked = _tracked_conversion_url(conversion_url, platform, int(job["draft_id"]), campaign)
    if conversion_url and tracked != conversion_url:
        body = body.replace(conversion_url, tracked)
    if platform == "douyin":
        from .social_assets import douyin_caption

        return douyin_caption(
            job["title"],
            body,
            settings.brand["name"],
            str(settings.raw.get("acquisition", {}).get("primary_cta", "")),
        )
    return body


def _confirm_publish(page: Any, spec: dict[str, Any]) -> bool:
    success_words = ("发布成功", "提交成功", "审核中", "已发布", "发布完成")
    for _ in range(12):
        for selector in spec.get(
            "success_locators",
            ["[role='alert']", "[aria-live='assertive']", ".el-message", ".ant-message-notice", ".toast", ".Toast"],
        ):
            try:
                locator = page.locator(selector).last
                if locator.is_visible(timeout=300):
                    message = locator.inner_text(timeout=500)
                    if any(word in message for word in success_words):
                        return True
            except Exception:
                continue
        if any(piece in page.url for piece in spec.get("success_url_contains", [])):
            return True
        page.wait_for_timeout(1000)
    return False


def publish_job(
    settings: Settings, job_id: int, force_visible: bool = False,
    trigger: str = "automation",
) -> dict[str, Any]:
    if settings.raw["publishing"].get("paused", False):
        return {"status": "skipped", "reason": "publishing.paused 已锁定发布"}
    if settings.raw["publishing"].get("mode") != "live":
        return {"status": "skipped", "reason": "publishing.mode 不是 live"}
    with Database(settings.db_path) as db:
        rows = db.query(
            """SELECT j.*,d.title,d.body,d.sources_json,d.status draft_status,a.status account_status
            FROM publish_jobs j JOIN drafts d ON d.id=j.draft_id
            LEFT JOIN platform_accounts a ON a.platform=j.platform WHERE j.id=?""", (job_id,)
        )
        if not rows:
            return {"status": "error", "reason": "任务不存在"}
        job = rows[0]
        from .pipeline import quality_check
        current_quality = quality_check(job["body"], settings, json.loads(job["sources_json"]))
        if job["draft_status"] != "approved" or not current_quality["publishable"]:
            db.execute(
                "UPDATE publish_jobs SET status='blocked',last_error=?,updated_at=? WHERE id=?",
                ("草稿未通过当前事实与质量闸门", now_iso(), job_id),
            )
            return {"status": "blocked", "reason": "草稿未通过当前事实与质量闸门"}
        if job["account_status"] != "connected":
            db.execute(
                "UPDATE publish_jobs SET status='blocked',last_error=?,updated_at=? WHERE id=?",
                ("平台尚未连接或登录态未确认", now_iso(), job_id),
            )
            return {"status": "blocked", "reason": "平台尚未连接"}
        claimed = db.execute(
            """UPDATE publish_jobs SET status='running',attempts=attempts+1,updated_at=?
            WHERE id=? AND status IN ('pending','failed') AND attempts<3""",
            (now_iso(), job_id),
        )
        if claimed.rowcount != 1:
            return {"status": "skipped", "reason": "任务已被其他进程领取或已达最大重试次数"}
    spec = load_platforms(settings)[job["platform"]]
    if spec.get("kind", "publisher") != "publisher":
        return {"status": "blocked", "reason": "该账号用于 AI 监测，不是内容发布平台"}
    launch_event = begin_browser_launch(
        settings, str(job["platform"]), "publish", visible=force_visible, trigger=trigger,
    )
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_path(settings, job["platform"])),
                headless=not force_visible,
                viewport={"width": 1440, "height": 900},
                locale="zh-CN",
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(spec["studio_url"], wait_until="domcontentloaded", timeout=90_000)
            page.wait_for_timeout(3000)
            if not _looks_logged_in(page, spec):
                raise RuntimeError("登录态已失效，请重新连接平台")
            if spec.get("media_mode") == "image_text":
                from .social_assets import generate_social_cards

                upload = _first_attached(page, spec.get("upload_locators", []))
                if not upload:
                    raise RuntimeError("未找到图文上传控件；平台页面可能已更新，需要校准连接器")
                cards = generate_social_cards(settings, int(job["draft_id"]), job["title"], job["body"])
                upload.set_input_files([str(path) for path in cards])
                page.wait_for_timeout(8000)
            title = _first_visible(page, spec.get("title_locators", []))
            body = _first_visible(page, spec.get("body_locators", []))
            if (not title and not spec.get("title_optional")) or not body:
                raise RuntimeError("未找到标题或正文编辑器；平台页面可能已更新，需要校准连接器")
            if title:
                _fill_editor(title, job["title"])
            clean_body = _platform_body(settings, job["platform"], job)
            _fill_editor(body, clean_body, _markdown_to_safe_html(clean_body))
            publish = _first_visible(page, spec["publish_locators"])
            if not publish:
                raise RuntimeError("未找到发布按钮；内容已填入但没有提交")
            publish.click()
            page.wait_for_timeout(1500)
            try:
                response_text = page.locator("body").inner_text(timeout=2000)
            except Exception:
                response_text = ""
            if "当前请求存在异常" in response_text and any(
                phrase in response_text for phrase in ("一键登录", "微信登录", "重新登录")
            ):
                raise RuntimeError("平台要求重新登录：知乎风控错误 40362")
            if not _confirm_publish(page, spec):
                trace_dir = settings.root / "data" / "traces"
                trace_dir.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(trace_dir / f"publish-{job_id}-unconfirmed.png"), full_page=True)
                raise RuntimeError("已点击发布，但页面没有出现成功或审核中状态；任务未标记为已发布")
            published_url = page.url
            context.close()
        finish_browser_launch(settings, launch_event, "completed")
        with Database(settings.db_path) as db:
            db.execute(
                "UPDATE publish_jobs SET status='published',published_url=?,last_error=NULL,updated_at=? WHERE id=?",
                (published_url, now_iso(), job_id),
            )
        return {"status": "published", "url": published_url}
    except Exception as exc:
        finish_browser_launch(settings, launch_event, "failed", str(exc))
        login_required = "平台要求重新登录" in str(exc)
        with Database(settings.db_path) as db:
            db.execute(
                """UPDATE publish_jobs SET status=CASE WHEN ? THEN 'blocked'
                WHEN attempts>=3 THEN 'manual_review' ELSE 'failed' END,
                last_error=?,updated_at=? WHERE id=?""",
                (login_required, str(exc)[:1000], now_iso(), job_id),
            )
            if login_required:
                db.execute(
                    "UPDATE platform_accounts SET status='not_connected',last_checked_at=?,last_error=? WHERE platform=?",
                    (now_iso(), str(exc)[:1000], job["platform"]),
                )
        return {"status": "failed", "reason": str(exc)}


def run_due_jobs(settings: Settings) -> list[dict[str, Any]]:
    if settings.raw.get("publishing", {}).get("paused", False):
        return []
    if settings.raw.get("publishing", {}).get("mode") != "live":
        return []
    with Database(settings.db_path) as db:
        lease_cutoff = (datetime.now(UTC) - timedelta(minutes=30)).replace(microsecond=0).isoformat()
        db.execute(
            """UPDATE publish_jobs SET status='pending',last_error='发布进程超时，任务已回收',updated_at=?
            WHERE status='running' AND updated_at<? AND attempts<3""",
            (now_iso(), lease_cutoff),
        )
        rows = db.query(
            """SELECT id FROM publish_jobs WHERE attempts<3 AND
            ((status='pending' AND scheduled_at<=?) OR (status='failed' AND updated_at<=?))
            ORDER BY scheduled_at LIMIT 3""",
            (now_iso(), (datetime.now(UTC) - timedelta(minutes=5)).replace(microsecond=0).isoformat()),
        )
    return [{"job_id": row["id"], **publish_job(settings, row["id"])} for row in rows]


def _browser_probe_prompt(question: str, prompt_mode: str = "naturalistic") -> tuple[str, str, str]:
    if prompt_mode == "naturalistic":
        return question, "naturalistic", "naturalistic-v1"
    if prompt_mode == "source_requested":
        return (
            f"{question} 请给出中立、可核查的中文答案，并尽量列出公开来源链接。",
            "source_requested",
            "source-requested-v1",
        )
    raise ValueError(f"未知浏览器提示模式：{prompt_mode}")


def _capture_browser_answer(page: Any, spec: dict[str, Any], prompt: str) -> tuple[str, list[str]]:
    """Capture one answer in a fresh conversation page supplied by the caller."""
    editor = _first_visible(page, spec["input_locators"])
    if not editor:
        raise RuntimeError("未找到 AI 对话输入框，连接器需要校准")
    answer_selector = ",".join(spec["answer_locators"])
    before = page.locator(answer_selector).count()
    if before != 0:
        raise RuntimeError(
            f"会话隔离校验失败：发送前重新出现 {before} 条历史回答，已拒绝提问"
        )
    _fill_editor(editor, prompt)
    editor.press("Enter")
    deadline = time.time() + 150
    answer = ""
    stable = 0
    while time.time() < deadline:
        items = page.locator(answer_selector)
        if items.count() > before:
            try:
                current = items.last.inner_text(timeout=3000).strip()
            except Exception:
                current = ""
            if current and current == answer and len(current) >= 40:
                stable += 1
            else:
                stable = 0
                answer = current
            if stable >= 4:
                break
        page.wait_for_timeout(1500)
    if len(answer) < 40:
        raise RuntimeError("AI 回答未完成或无法读取")
    try:
        links = page.locator(answer_selector).last.locator("a").evaluate_all(
            "els => els.map(a => a.href).filter(Boolean)"
        )
    except Exception:
        links = []
    return answer, list(dict.fromkeys(str(link) for link in links if link))


def _prepare_fresh_ai_conversation(page: Any, spec: dict[str, Any]) -> dict[str, Any]:
    """Open and prove an empty conversation before a measurement sample is sent."""
    entry_url = str(spec.get("new_chat_url") or spec["studio_url"])
    page.goto(entry_url, wait_until="domcontentloaded", timeout=90_000)
    page.wait_for_timeout(2500)
    if not _looks_logged_in(page, spec):
        raise RuntimeError("AI 监测账号登录态已失效")
    _raise_if_ai_temporarily_restricted(page)
    answer_selector = ",".join(spec["answer_locators"])
    initial_count = page.locator(answer_selector).count()
    method = "new_chat_url"
    if initial_count:
        new_chat = _first_visible(page, spec.get("new_chat_locators", []))
        if not new_chat:
            raise RuntimeError(
                f"会话隔离校验失败：首页已有 {initial_count} 条回答且未找到新建对话控件"
            )
        new_chat.click()
        page.wait_for_timeout(1500)
        method = "new_chat_control"
    empty_count = page.locator(answer_selector).count()
    if empty_count:
        raise RuntimeError(f"会话隔离校验失败：新对话仍有 {empty_count} 条历史回答")
    parsed = urlsplit(page.url)
    return {
        "fresh_context_verified": True,
        "fresh_context_method": method,
        "initial_answer_count": int(initial_count),
        "empty_answer_count": int(empty_count),
        "entry_origin": urlunsplit((parsed.scheme, parsed.netloc, "", "", "")),
    }


def _browser_batch_config(row: Any) -> dict[str, Any]:
    try:
        value = json.loads(row["config_json"] or "{}")
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _browser_batch_matches_request(
    config: dict[str, Any],
    requested_questions: list[str] | None,
    requested_variant: str,
) -> bool:
    if config.get("engine_surface") != "browser":
        return False
    batch_variant = str(config.get("prompt_variant") or "naturalistic")
    if batch_variant != requested_variant:
        return False
    if requested_questions:
        requested = {
            question.strip() for question in requested_questions if question.strip()
        }
        batch_questions = {
            str(question).strip() for question in config.get("questions", [])
            if str(question).strip()
        }
        if batch_questions != requested:
            return False
    return True


def _sync_playwright_context() -> Any:
    from playwright.sync_api import sync_playwright

    return sync_playwright()


def _finish_browser_batch(db: Database, batch_id: str, errors: list[dict[str, Any]]) -> str:
    counts = db.query(
        """SELECT COUNT(*) total,COALESCE(SUM(status='succeeded'),0) succeeded,
        COALESCE(SUM(status='failed'),0) failed,
        COALESCE(SUM(status IN ('planned','running')),0) pending,
        COALESCE(SUM(attempts),0) attempts FROM probe_batch_items WHERE batch_id=?""",
        (batch_id,),
    )[0]
    if counts["total"] == 0:
        status = "skipped"
    elif counts["succeeded"] == counts["total"]:
        status = "completed"
    elif counts["succeeded"] == 0 and counts["pending"] == 0:
        status = "failed"
    else:
        status = "partial"
    batch_row = db.query("SELECT errors_json FROM probe_batches WHERE batch_id=?", (batch_id,))[0]
    try:
        existing_errors = json.loads(batch_row["errors_json"] or "[]")
        if not isinstance(existing_errors, list):
            existing_errors = [{"stage": "legacy_error_payload", "payload": existing_errors}]
    except json.JSONDecodeError:
        existing_errors = [{"stage": "legacy_error_payload", "raw": str(batch_row["errors_json"])[:2000]}]
    all_errors = [*existing_errors, *errors]
    db.execute(
        """UPDATE probe_batches SET finished_at=?,status=?,attempted_calls=?,succeeded_calls=?,
        failed_calls=?,errors_json=? WHERE batch_id=?""",
        (
            now_iso(), status, int(counts["attempts"]), int(counts["succeeded"]),
            int(counts["failed"]), json.dumps(all_errors, ensure_ascii=False), batch_id,
        ),
    )
    return status


def _select_browser_probe_questions(
    settings: Settings,
    db: Database,
    limit: int,
    explicit_questions: list[str] | None = None,
    providers: list[str] | None = None,
    samples_per_question: int | None = None,
    call_cap: int | None = None,
) -> list[str]:
    if explicit_questions:
        return list(dict.fromkeys(
            question.strip() for question in explicit_questions if question.strip()
        ))[:limit]
    priority = list(dict.fromkeys([
        *settings.raw.get("acquisition", {}).get("priority_questions", []),
        *settings.raw.get("geo_goals", {}).get("brand_reputation", {}).get("target_questions", []),
    ]))
    ranked = [row["question"] for row in db.query(
        "SELECT question FROM opportunities ORDER BY score DESC,id LIMIT ?", (max(limit * 3, limit),)
    )]
    pool = list(dict.fromkeys([*priority, *ranked]))
    monitor = settings.raw.get("monitor", {})
    primary_variant = str(monitor.get("primary_prompt_variant", "naturalistic"))
    primary_version = str(monitor.get("primary_prompt_version", "naturalistic-v1"))
    core_questions = set(priority)
    expected_providers = list(dict.fromkeys(str(item) for item in (providers or []) if str(item)))
    scored: list[dict[str, Any]] = []
    for order, question in enumerate(pool):
        rows = db.query(
            """SELECT id,provider,experiment_id,sample_index,probed_at FROM probes
            WHERE engine_surface='browser' AND prompt_variant=? AND prompt_version=? AND question=?""",
            (primary_variant, primary_version, question),
        )
        experiment_slots: dict[tuple[str, str], set[int]] = {}
        for row in rows:
            experiment_key = row["experiment_id"] or f"legacy-single-{row['id']}"
            experiment_slots.setdefault((str(row["provider"]), str(experiment_key)), set()).add(
                max(1, int(row["sample_index"]))
            )
        observed_providers = sorted({str(row["provider"]) for row in rows})
        comparison_providers = expected_providers or observed_providers or [""]
        depth_by_provider = {
            provider: max(
                (len(slots) for (name, _), slots in experiment_slots.items() if name == provider),
                default=0,
            )
            for provider in comparison_providers
        }
        repeat_runs_by_provider = {
            provider: sum(
                len(slots) >= _repeat_target(settings)
                for (name, _), slots in experiment_slots.items() if name == provider
            )
            for provider in comparison_providers
        }
        # The weakest connected engine controls baseline readiness. A complete
        # DeepSeek run must not hide a missing Kimi baseline after Kimi connects.
        repeat_depth = min(depth_by_provider.values(), default=0)
        repeat_ready_runs = min(repeat_runs_by_provider.values(), default=0)
        last_at = max((str(row["probed_at"] or "") for row in rows), default="")
        scored.append({
            "repeat_depth": repeat_depth, "repeat_ready_runs": repeat_ready_runs,
            "last_at": last_at, "order": order, "question": question,
        })
    incomplete_core = [
        item for item in scored
        if item["question"] in core_questions
        and item["repeat_depth"] < _repeat_target(settings)
    ]
    if incomplete_core:
        incomplete_core.sort(key=lambda item: (
            item["repeat_depth"], item["last_at"], item["order"],
        ))
        return [item["question"] for item in incomplete_core[:limit]]

    try:
        configured_longitudinal = int(monitor.get("longitudinal_core_questions_per_run", 1))
    except (TypeError, ValueError):
        configured_longitudinal = 1
    provider_count = max(1, len(expected_providers))
    samples_cost = max(1, int(
        samples_per_question
        if samples_per_question is not None
        else monitor.get("browser_samples_per_prompt", 3)
    ))
    available_calls = max(1, int(
        call_cap if call_cap is not None else monitor.get("browser_max_calls_per_run", 6)
    ))
    complete_question_capacity = max(1, available_calls // (provider_count * samples_cost))
    if complete_question_capacity >= 2:
        longitudinal_slots = min(
            max(0, configured_longitudinal), complete_question_capacity - 1, max(0, limit - 1)
        )
    else:
        completed_browser_batches = int(db.query(
            """SELECT COUNT(*) n FROM probe_batches
            WHERE CASE WHEN json_valid(config_json)
              THEN COALESCE(json_extract(config_json,'$.engine_surface'),'') ELSE '' END='browser'
            AND status IN ('completed','partial','failed','waiting_retry')"""
        )[0]["n"])
        # With capacity for only one complete question, alternate discovery and
        # longitudinal runs instead of letting either objective starve forever.
        longitudinal_slots = min(
            max(0, configured_longitudinal), 1 if limit > 0 else 0
        ) if completed_browser_batches % 2 == 1 else 0
    longitudinal = sorted(
        (item for item in scored if item["question"] in core_questions),
        key=lambda item: (item["repeat_ready_runs"], item["last_at"], item["order"]),
    )[:longitudinal_slots]
    selected = [item["question"] for item in longitudinal]
    discovery = sorted(
        (item for item in scored if item["question"] not in set(selected)),
        key=lambda item: (item["repeat_depth"], item["last_at"], item["order"]),
    )
    selected.extend(item["question"] for item in discovery[: max(0, limit - len(selected))])
    return selected


def _build_browser_probe_plan(
    batch_id: str,
    providers: list[str],
    questions: list[str],
    samples: int,
    max_calls: int,
    timestamp: str,
    prompt_mode: str,
) -> list[tuple[Any, ...]]:
    """Allocate a question across engines before taking its next repeat."""
    plan: list[tuple[Any, ...]] = []
    for question in questions:
        for sample_index in range(1, samples + 1):
            for platform in providers:
                if len(plan) >= max_calls:
                    return plan
                prompt, variant, version = _browser_probe_prompt(question, prompt_mode)
                plan.append((
                    batch_id, platform, question, prompt, sample_index, timestamp, timestamp,
                    variant, version,
                ))
    return plan


def _next_browser_batch_item_ids(
    db: Database,
    batch_id: str,
    providers: list[str],
    call_limit: int,
    max_attempts: int,
) -> set[int]:
    if not providers or call_limit < 1:
        return set()
    placeholders = ",".join("?" for _ in providers)
    rows = db.query(
        f"""SELECT id FROM probe_batch_items WHERE batch_id=?
        AND provider IN ({placeholders}) AND status IN ('planned','failed') AND attempts<?
        ORDER BY id LIMIT ?""",
        (batch_id, *providers, max_attempts, call_limit),
    )
    return {int(row["id"]) for row in rows}


def run_browser_probes(
    settings: Settings,
    limit: int = 5,
    samples_per_prompt: int | None = None,
    providers: list[str] | None = None,
    questions: list[str] | None = None,
    prompt_mode: str | None = None,
) -> dict[str, Any]:
    specs = load_platforms(settings)
    from .pipeline import reconcile_stale_probe_batches

    monitor = settings.raw.get("monitor", {})
    samples = min(5, max(1, int(
        samples_per_prompt
        if samples_per_prompt is not None
        else monitor.get("browser_samples_per_prompt", monitor.get("samples_per_prompt", 3))
    )))
    mode = str(prompt_mode or monitor.get("browser_prompt_mode", "naturalistic"))
    # Validate before a batch manifest is created.
    requested_variant = _browser_probe_prompt("validation", mode)[1]
    max_calls = min(100, max(1, int(monitor.get("browser_max_calls_per_run", 6))))
    manifest_cap = min(500, max(max_calls, int(monitor.get("browser_max_manifest_items", 100))))
    max_attempts = min(10, max(1, int(monitor.get("max_item_attempts", 3))))
    with Database(settings.db_path) as db:
        recovered_batches = reconcile_stale_probe_batches(
            db, int(monitor.get("stale_batch_hours", 2))
        )
        db.execute(
            """UPDATE platform_accounts SET status='connected',retry_after=NULL
            WHERE status='temporarily_unavailable' AND retry_after IS NOT NULL AND retry_after<=?""",
            (now_iso(),),
        )
        connected = [
            row["platform"] for row in db.query("SELECT platform FROM platform_accounts WHERE status='connected'")
            if specs.get(row["platform"], {}).get("kind") == "ai_probe"
            and (not providers or row["platform"] in providers)
        ]
        waiting_retry = [dict(row) for row in db.query(
            """SELECT platform,retry_after,last_error FROM platform_accounts
            WHERE status='temporarily_unavailable' ORDER BY retry_after"""
        ) if not providers or row["platform"] in providers]
        login_required = [dict(row) for row in db.query(
            """SELECT platform,status,last_error FROM platform_accounts
            WHERE status NOT IN ('connected','temporarily_unavailable') ORDER BY platform"""
        ) if specs.get(row["platform"], {}).get("kind") == "ai_probe"
        and (not providers or row["platform"] in providers)]
        eligible_providers = {
            *connected,
            *(str(row["platform"]) for row in waiting_retry),
        }
        selected_questions = _select_browser_probe_questions(
            settings, db, limit, questions, providers=connected,
            samples_per_question=samples, call_cap=max_calls,
        )
        resumable = []
        for row in db.query(
            """SELECT * FROM probe_batches WHERE status IN ('interrupted','partial','failed','waiting_retry')
            ORDER BY started_at DESC"""
        ):
            config = _browser_batch_config(row)
            if not _browser_batch_matches_request(config, questions, requested_variant):
                continue
            batch_providers = set(config.get("providers", []))
            if batch_providers and not batch_providers.intersection(eligible_providers):
                continue
            pending = db.query(
                """SELECT COUNT(*) n FROM probe_batch_items WHERE batch_id=?
                AND status IN ('planned','failed') AND attempts<?""",
                (row["batch_id"], max_attempts),
            )[0]["n"]
            if pending:
                resumable.append(row["batch_id"])
                break
    if not connected:
        if waiting_retry:
            if resumable:
                with Database(settings.db_path) as db:
                    db.execute(
                        "UPDATE probe_batches SET status='waiting_retry' WHERE batch_id=?",
                        (resumable[0],),
                    )
            return {
                "status": (
                    "waiting_for_ai_login_and_retry" if login_required
                    else "waiting_for_ai_retry"
                ),
                "results": [],
                "retry": waiting_retry,
                "login_required": login_required,
                "recovered_stale_batches": recovered_batches,
            }
        return {
            "status": "waiting_for_ai_login", "results": [],
            "login_required": login_required,
            "recovered_stale_batches": recovered_batches,
        }
    if resumable:
        batch_id = resumable[0]
        with Database(settings.db_path) as db:
            batch = db.query(
                "SELECT status,config_json FROM probe_batches WHERE batch_id=?", (batch_id,)
            )[0]
            resumed_config = _browser_batch_config(batch)
            mode = (
                "source_requested"
                if resumed_config.get("prompt_variant") == "source_requested"
                else "naturalistic"
            )
            claimed = db.execute(
                "UPDATE probe_batches SET status='running',finished_at=NULL WHERE batch_id=? AND status=?",
                (batch_id, batch["status"]),
            )
            if claimed.rowcount != 1:
                return {
                    "status": "concurrent_resume_skipped", "batch_id": batch_id, "results": [],
                    "recovered_stale_batches": recovered_batches,
                }
    else:
        batch_id = f"browser-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        timestamp = now_iso()
        desired_calls = len(connected) * len(selected_questions) * samples
        plan = _build_browser_probe_plan(
            batch_id, connected, selected_questions, samples, manifest_cap, timestamp, mode
        )
        with Database(settings.db_path) as db:
            db.execute(
                """INSERT INTO probe_batches(batch_id,started_at,status,planned_calls,
                truncated_by_call_cap,config_json) VALUES(?,?,'running',?,?,?)""",
                (
                    batch_id, timestamp, len(plan),
                    int(desired_calls > len(plan)),
                    json.dumps({
                        "engine_surface": "browser", "question_limit": limit,
                        "samples_per_prompt": samples, "max_calls_per_run": max_calls,
                        "providers": connected, "questions": selected_questions,
                        "prompt_variant": _browser_probe_prompt("validation", mode)[1],
                        "prompt_version": _browser_probe_prompt("validation", mode)[2],
                    }, ensure_ascii=False),
                ),
            )
            if plan:
                db.executemany(
                    """INSERT INTO probe_batch_items(batch_id,provider,question,prompt,sample_index,
                    created_at,updated_at,prompt_variant,prompt_version,engine_surface)
                    VALUES(?,?,?,?,?,?,?,?,?,'browser')""",
                    plan,
                )
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    retry_windows: list[dict[str, str]] = []
    with Database(settings.db_path) as db:
        runnable_item_ids = _next_browser_batch_item_ids(
            db, batch_id, connected, max_calls, max_attempts
        )
    with _sync_playwright_context() as playwright:
        for platform in connected:
            spec = specs[platform]
            context = None
            handled_item_ids: set[int] = set()
            with Database(settings.db_path) as db:
                candidate_items = db.query(
                    """SELECT * FROM probe_batch_items WHERE batch_id=? AND provider=?
                    AND status IN ('planned','failed') AND attempts<? ORDER BY id""",
                    (batch_id, platform, max_attempts),
                )
                items = [row for row in candidate_items if int(row["id"]) in runnable_item_ids]
            if not items:
                continue
            launch_event = begin_browser_launch(
                settings, platform, "ai_probe", visible=False, trigger="automation"
            )
            try:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(profile_path(settings, platform)), headless=True,
                    viewport={"width": 1440, "height": 900}, locale="zh-CN",
                )
                page = context.pages[0] if context.pages else context.new_page()
                for item in items:
                    with Database(settings.db_path) as db:
                        existing = db.query("SELECT id FROM probes WHERE batch_item_id=?", (item["id"],))
                        if existing:
                            db.execute(
                                """UPDATE probe_batch_items SET status='succeeded',probe_id=?,
                                last_error=NULL,updated_at=? WHERE id=?""",
                                (existing[0]["id"], now_iso(), item["id"]),
                            )
                            handled_item_ids.add(int(item["id"]))
                            continue
                        claimed = db.execute(
                            """UPDATE probe_batch_items SET status='running',attempts=attempts+1,
                            last_error=NULL,updated_at=? WHERE id=? AND status IN ('planned','failed')""",
                            (now_iso(), item["id"]),
                        )
                    if claimed.rowcount != 1:
                        continue
                    handled_item_ids.add(int(item["id"]))
                    try:
                        isolation = _prepare_fresh_ai_conversation(page, spec)
                        answer, links = _capture_browser_answer(page, spec, item["prompt"])
                        conversation_url_hash = hashlib.sha256(page.url.encode("utf-8")).hexdigest()
                        with Database(settings.db_path) as db:
                            analysis = record_probe(
                                db, settings, platform, item["question"], answer,
                                prompt_variant=str(item["prompt_variant"]),
                                prompt_version=str(item["prompt_version"]),
                                captured_urls=links, engine_surface="browser",
                                sample_index=int(item["sample_index"]), experiment_id=batch_id,
                                raw_metadata={
                                    "batch_id": batch_id, "batch_item_id": int(item["id"]),
                                    **isolation, "conversation_url_sha256": conversation_url_hash,
                                    **build_probe_provenance(
                                        provider=platform, surface="browser", config=spec,
                                        locale="zh-CN", region="CN",
                                        extraction_version="browser-dom-v1",
                                    ),
                                },
                                batch_item_id=int(item["id"]),
                            )
                            db.execute(
                                """UPDATE probe_batch_items SET status='succeeded',probe_id=?,
                                last_error=NULL,updated_at=? WHERE id=?""",
                                (analysis["probe_id"], now_iso(), item["id"]),
                            )
                    except Exception as exc:
                        message = str(exc)[:2000]
                        with Database(settings.db_path) as db:
                            if isinstance(exc, TemporaryAIRestriction):
                                db.execute(
                                    """UPDATE probe_batch_items SET status=?,attempts=MAX(0,attempts-1),
                                    last_error=?,updated_at=? WHERE id=?""",
                                    (item["status"], message, now_iso(), item["id"]),
                                )
                            else:
                                db.execute(
                                    """UPDATE probe_batch_items SET status='failed',last_error=?,updated_at=?
                                    WHERE id=?""", (message, now_iso(), item["id"]),
                                )
                        errors.append({
                            "provider": platform, "question": item["question"],
                            "sample_index": int(item["sample_index"]), "error": message,
                        })
                        if isinstance(exc, TemporaryAIRestriction):
                            retry_windows.append({
                                "provider": platform, "retry_after": exc.retry_after,
                                "reason": message,
                            })
                            raise
                        if "登录态" in message or "验证码" in message:
                            raise
                        continue
                    results.append({
                        "provider": platform,
                        "question": item["question"],
                        "sample_index": int(item["sample_index"]),
                        "mentioned": analysis["brand_mentioned"],
                        "recommended": analysis["recommended"],
                        "cited": analysis["domain_cited"],
                        "visibility_score": analysis["visibility_score"],
                    })
                with Database(settings.db_path) as db:
                    db.execute(
                        """UPDATE platform_accounts SET status='connected',last_error=NULL,
                        retry_after=NULL,last_checked_at=? WHERE platform=?""",
                        (now_iso(), platform),
                    )
                finish_browser_launch(settings, launch_event, "completed")
            except Exception as exc:
                finish_browser_launch(settings, launch_event, "failed", str(exc))
                with Database(settings.db_path) as db:
                    if not isinstance(exc, TemporaryAIRestriction):
                        for item in items:
                            if int(item["id"]) in handled_item_ids:
                                continue
                            db.execute(
                                """UPDATE probe_batch_items SET status='failed',attempts=attempts+1,
                                last_error=?,updated_at=? WHERE id=? AND status IN ('planned','failed')
                                AND attempts<?""",
                                (str(exc)[:2000], now_iso(), item["id"], max_attempts),
                            )
                    account_status = (
                        "temporarily_unavailable" if isinstance(exc, TemporaryAIRestriction)
                        else "not_connected" if ("登录态" in str(exc) or "验证码" in str(exc))
                        else "connected"
                    )
                    retry_after = exc.retry_after if isinstance(exc, TemporaryAIRestriction) else None
                    db.execute(
                        """UPDATE platform_accounts SET status=?,last_error=?,retry_after=?,last_checked_at=?
                        WHERE platform=?""",
                        (account_status, str(exc)[:1000], retry_after, now_iso(), platform),
                    )
                results.append({"provider": platform, "status": "failed", "error": str(exc)})
            finally:
                if context is not None:
                    try:
                        context.close()
                    except Exception:
                        pass
    with Database(settings.db_path) as db:
        status = _finish_browser_batch(db, batch_id, errors)
        if retry_windows:
            status = "waiting_retry"
            db.execute(
                "UPDATE probe_batches SET status=? WHERE batch_id=?",
                (status, batch_id),
            )
    return {
        "status": "waiting_for_ai_retry" if retry_windows else status,
        "batch_status": status, "batch_id": batch_id, "results": results, "errors": errors,
        "retry": retry_windows,
        "samples_per_prompt": samples, "max_calls_per_run": max_calls,
        "prompt_mode": mode,
        "recovered_stale_batches": recovered_batches,
    }


def browser_main() -> None:
    parser = argparse.ArgumentParser(description="宏图 GEO 独立浏览器工作进程")
    sub = parser.add_subparsers(dest="command", required=True)
    connect = sub.add_parser("connect")
    connect.add_argument("platform")
    connect.add_argument("--timeout-minutes", type=int, default=30)
    launch = sub.add_parser("open")
    launch.add_argument("platform")
    publish = sub.add_parser("publish")
    publish.add_argument("job_id", type=int)
    publish.add_argument("--visible", action="store_true")
    args = parser.parse_args()
    settings = Settings.load()
    if args.command == "connect":
        raise SystemExit(connect_platform(settings, args.platform, args.timeout_minutes))
    if args.command == "open":
        raise SystemExit(open_platform(settings, args.platform))
    result = publish_job(
        settings, args.job_id, args.visible,
        trigger="user_explicit" if args.visible else "automation",
    )
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result["status"] in {"published", "skipped"} else 2)


if __name__ == "__main__":
    browser_main()
