from __future__ import annotations

import re
from typing import Any

from .core import Database, Settings, now_iso


def begin_browser_launch(
    settings: Settings, platform: str, action: str, *, visible: bool, trigger: str,
) -> int:
    """Record a browser launch and reject visible automation by construction."""
    if trigger not in {"user_explicit", "automation"}:
        raise ValueError(f"未知浏览器触发类型：{trigger}")
    if visible and trigger != "user_explicit":
        raise RuntimeError("安全策略禁止后台自动化拉起可见浏览器")
    with Database(settings.db_path) as db:
        cursor = db.execute(
            """INSERT INTO browser_launch_events(
            occurred_at,platform,action,trigger,visible,status,finished_at,error
            ) VALUES(?,?,?,?,?,'started',NULL,NULL)""",
            (now_iso(), platform, action, trigger, int(visible)),
        )
        return int(cursor.lastrowid)


def finish_browser_launch(
    settings: Settings, event_id: int, status: str, error: str | None = None,
) -> None:
    if status not in {"completed", "failed", "closed", "login_required"}:
        raise ValueError(f"未知浏览器活动状态：{status}")
    safe_error = _sanitize_browser_error(error)
    with Database(settings.db_path) as db:
        db.execute(
            """UPDATE browser_launch_events SET status=?,finished_at=?,error=?
            WHERE id=? AND status='started'""",
            (status, now_iso(), safe_error, event_id),
        )


def _sanitize_browser_error(error: str | None) -> str | None:
    text = str(error or "").replace("\r", " ").replace("\n", " ").strip()
    if not text:
        return None
    text = re.sub(r"https?://\S+", "[URL已脱敏]", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(?i)\b[a-z]:[\\/][^\s\"'<>]+", "[本地路径已脱敏]", text,
    )
    text = re.sub(
        r"(?i)\b(api[_-]?key|token|secret|password|cookie)\s*[=:]\s*[^\s,;]+",
        r"\1=[已脱敏]", text,
    )
    return text[:1000] or None


def build_browser_activity_health(db: Database) -> dict[str, Any]:
    counts = dict(db.query(
        """SELECT COUNT(*) recorded_launches,
        COALESCE(SUM(CASE WHEN visible=1 THEN 1 ELSE 0 END),0) visible_launches,
        COALESCE(SUM(CASE WHEN visible=1 AND trigger='user_explicit' THEN 1 ELSE 0 END),0)
            visible_user_launches,
        COALESCE(SUM(CASE WHEN visible=1 AND trigger<>'user_explicit' THEN 1 ELSE 0 END),0)
            visible_automation_launches
        FROM browser_launch_events"""
    )[0])
    rows = [dict(row) for row in db.query(
        """SELECT id,occurred_at,finished_at,platform,action,trigger,visible,status,error
        FROM browser_launch_events ORDER BY id DESC LIMIT 50"""
    )]
    last_visible_rows = db.query(
        """SELECT id,occurred_at,finished_at,platform,action,trigger,visible,status,error
        FROM browser_launch_events WHERE visible=1 ORDER BY id DESC LIMIT 1"""
    )
    total = int(counts["recorded_launches"])
    visible_automation = int(counts["visible_automation_launches"])
    return {
        "status": "policy_violation" if visible_automation else "healthy" if total else "not_recorded",
        "recorded_launches": total,
        "visible_user_launches": int(counts["visible_user_launches"]),
        "visible_automation_launches": visible_automation,
        "last_visible_launch": dict(last_visible_rows[0]) if last_visible_rows else None,
        "recent_events": rows[:10],
        "policy": "可见浏览器只允许由用户明确点击注册/登录、打开工作台或显式可见发布命令触发；定时探测和发布检查只能无头运行。",
    }
