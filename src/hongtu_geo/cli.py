from __future__ import annotations

import argparse
import json
import subprocess
import sys
import webbrowser

from .attribution import build_attribution_report
from .app import run_dashboard
from .browser import connect_platform, open_platform, publish_job, run_browser_probes, sync_platform_accounts
from .browser_activity import begin_browser_launch, finish_browser_launch
from .core import Database, Settings
from .crawler import crawl_site
from .geo_engine import build_prompt_benchmark, build_strategy_snapshot
from .pipeline import (
    build_entity_assets,
    build_report,
    extract_official_facts,
    generate_drafts,
    review_drafts,
    resume_probe_batch,
    run_automation_pipeline,
    run_probes,
    schedule_connected_drafts,
    seed_opportunities,
)


def run_pipeline(settings: Settings, kind: str) -> dict[str, object]:
    return run_automation_pipeline(settings, kind)


def main() -> None:
    parser = argparse.ArgumentParser(prog="hongtu-geo", description="宏图商机汇 GEO 自动化与多平台发布中台")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="初始化数据库、平台和问题库")
    sub.add_parser("daily", help="运行每日流程")
    sub.add_parser("weekly", help="运行全面周流程")
    sub.add_parser("crawl", help="审计官网")
    sub.add_parser("assets", help="生成实体、事实账本与官网待部署 GEO 资产")
    benchmark = sub.add_parser("benchmark", help="生成固定问题与意图变体基准集")
    benchmark.add_argument("--limit", type=int, default=30)
    sub.add_parser("strategy", help="生成成熟度、可见度、引用与获客差距诊断")
    sub.add_parser("attribution", help="查看匿名 GEO 获客归因漏斗")
    generate = sub.add_parser("generate", help="生成内容草稿")
    generate.add_argument("--limit", type=int, default=2)
    sub.add_parser("review", help="运行事实与质量闸门")
    probe = sub.add_parser("probe", help="运行引用监测并生成跨平台探测队列")
    probe.add_argument("--limit", type=int, default=None, help="本次最多选择的问题数")
    probe.add_argument("--samples", type=int, default=None, help="每个问题重复采样次数（最多 5）")
    probe_resume = sub.add_parser("probe-resume", help="只重试指定探测批次中的未完成样本")
    probe_resume.add_argument("batch_id")
    probe_resume.add_argument("--max-calls", type=int, default=None)
    browser_probe = sub.add_parser("browser-probe", help="使用已保存登录态运行有界浏览器监测")
    browser_probe.add_argument("--limit", type=int, default=1, help="本次最多选择的问题数")
    browser_probe.add_argument("--samples", type=int, default=None, help="每个问题重复采样次数（最多 5）")
    browser_probe.add_argument("--provider", action="append", dest="providers", help="仅运行指定 AI 平台，可重复")
    browser_probe.add_argument("--question", action="append", dest="questions", help="精确指定监测问题，可重复")
    browser_probe.add_argument(
        "--mode", choices=["naturalistic", "source_requested"], default=None,
        help="提示实验模式；默认 naturalistic，原样发送用户问题",
    )
    sub.add_parser("report", help="生成 GEO 报告")
    dash = sub.add_parser("dashboard", help="启动本地控制台")
    dash.add_argument("--host", default="127.0.0.1")
    dash.add_argument("--port", type=int, default=8765)
    dash.add_argument("--no-open", action="store_true")
    sub.add_parser("install-browser", help="安装系统内置 Chromium")
    connect = sub.add_parser("connect", help="打开平台注册/登录浏览器")
    connect.add_argument("platform")
    launch = sub.add_parser("open", help="使用保存的登录态打开平台工作台")
    launch.add_argument("platform")
    publish = sub.add_parser("publish", help="执行一个已批准发布任务")
    publish.add_argument("job_id", type=int)
    publish.add_argument("--visible", action="store_true")
    args = parser.parse_args()
    settings = Settings.load()

    if args.command == "install-browser":
        raise SystemExit(subprocess.call([sys.executable, "-m", "playwright", "install", "chromium"], cwd=settings.root))
    if args.command == "dashboard":
        if not args.no_open:
            import threading
            event_id = begin_browser_launch(
                settings, "local_dashboard", "open_dashboard",
                visible=True, trigger="user_explicit",
            )

            def open_dashboard() -> None:
                try:
                    opened = webbrowser.open(f"http://{args.host}:{args.port}")
                    finish_browser_launch(
                        settings, event_id, "completed" if opened else "failed",
                        None if opened else "系统未接受浏览器打开请求",
                    )
                except Exception as exc:
                    finish_browser_launch(settings, event_id, "failed", str(exc))

            threading.Timer(1.2, open_dashboard).start()
        run_dashboard(args.host, args.port)
        return
    if args.command == "connect":
        raise SystemExit(connect_platform(settings, args.platform))
    if args.command == "open":
        raise SystemExit(open_platform(settings, args.platform))
    if args.command == "publish":
        print(json.dumps(publish_job(
            settings, args.job_id, args.visible,
            trigger="user_explicit" if args.visible else "automation",
        ), ensure_ascii=False, indent=2))
        return

    with Database(settings.db_path) as db:
        if args.command == "init":
            sync_platform_accounts(settings, db)
            result = seed_opportunities(settings, db)
        elif args.command == "crawl":
            result = crawl_site(settings, db)
        elif args.command == "assets":
            result = build_entity_assets(settings)
        elif args.command == "benchmark":
            result = {"benchmark": str(build_prompt_benchmark(settings, db, args.limit))}
        elif args.command == "strategy":
            result = {"strategy": str(build_strategy_snapshot(settings, db))}
        elif args.command == "attribution":
            result = build_attribution_report(settings, db)
        elif args.command == "generate":
            result = generate_drafts(settings, db, args.limit)
        elif args.command == "review":
            result = review_drafts(settings, db)
        elif args.command == "probe":
            result = run_probes(settings, db, args.limit, args.samples)
        elif args.command == "probe-resume":
            result = resume_probe_batch(settings, db, args.batch_id, args.max_calls)
        elif args.command == "browser-probe":
            result = run_browser_probes(
                settings, args.limit, args.samples, args.providers, args.questions, args.mode
            )
        elif args.command == "report":
            result = {"report": str(build_report(settings, db))}
        else:
            result = None
    if args.command in {"daily", "weekly"}:
        result = run_pipeline(settings, args.command)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
