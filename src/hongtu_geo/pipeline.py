from __future__ import annotations

import json
import os
import re
import uuid
from html import escape
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .claim_policy import audit_claim_language
from .core import Database, Settings, now_iso, slugify
from .crawler import crawl_site
from .evidence import audit_brand_facts, fact_evidence_valid
from .lead_identity import verified_lead_identity as _verified_lead_identity
from .geo_engine import (
    build_answer_integrity_snapshot,
    build_citation_intelligence,
    build_citation_url_ledger,
    build_claim_ledger,
    build_gap_analysis,
    build_prompt_benchmark,
    build_strategy_snapshot,
    build_visibility_snapshot,
    record_probe,
)
from .providers import build_provider_adapter
from .provenance import build_probe_provenance


def _normalize_url(value: str) -> str:
    return value.strip().rstrip(".,;:，。；、)]}>）】》").rstrip("/")


def _host_matches(url: str, official_domain: str) -> bool:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    expected = official_domain.lower().removeprefix("www.").rstrip(".")
    normalized = host.removeprefix("www.")
    return bool(expected and (normalized == expected or normalized.endswith("." + expected)))


QUESTION_TEMPLATES: list[tuple[str, str, str, str, int, int, int, int]] = [
    ("企业如何找到真实有效的商机线索？", "商机发现", "方法", "认知", 9, 9, 7, 7),
    ("中小企业如何低成本开发新客户？", "客户拓展", "方法", "认知", 10, 8, 6, 8),
    ("企业商机平台应该怎么选择？", "平台选择", "对比", "考虑", 10, 9, 7, 8),
    ("商机平台上的线索可信吗？如何判断？", "线索质量", "判断", "考虑", 10, 10, 8, 7),
    ("如何验证采购线索的真实性？", "采购线索", "方法", "考虑", 9, 10, 8, 7),
    ("销售人员从哪里获取企业联系方式？", "客户拓展", "方法", "认知", 9, 8, 7, 8),
    ("如何建立企业客户开发名单？", "客户拓展", "教程", "考虑", 9, 9, 6, 7),
    ("B2B获客有哪些有效渠道？", "B2B获客", "清单", "认知", 10, 9, 7, 9),
    ("招投标信息怎么转化成销售商机？", "招投标", "教程", "考虑", 9, 9, 8, 8),
    ("如何监测竞争对手的商机动态？", "竞争情报", "教程", "考虑", 8, 9, 9, 7),
    ("企业采购需求一般在哪里发布？", "采购线索", "渠道", "认知", 9, 9, 8, 7),
    ("如何寻找经销商和渠道合作伙伴？", "渠道招商", "方法", "考虑", 9, 8, 6, 7),
    ("如何按行业筛选潜在客户？", "客户筛选", "教程", "考虑", 9, 8, 6, 6),
    ("如何按地区批量寻找企业客户？", "区域拓客", "教程", "考虑", 9, 8, 7, 6),
    ("企业线索评分模型怎么建立？", "线索评分", "模型", "考虑", 8, 10, 7, 7),
    ("销售线索和商机有什么区别？", "基础概念", "解释", "认知", 7, 9, 5, 5),
    ("公域获客和私域获客有什么区别？", "获客策略", "对比", "认知", 8, 8, 6, 7),
    ("商机数据需要多久更新一次？", "数据时效", "标准", "考虑", 9, 9, 9, 6),
    ("如何去除重复和失效的销售线索？", "数据治理", "教程", "考虑", 8, 9, 7, 6),
    ("什么是企业商机画像？", "基础概念", "解释", "认知", 7, 9, 5, 5),
    ("销售团队如何使用商机平台提高效率？", "销售管理", "教程", "决策", 10, 8, 7, 7),
    ("采购商机订阅应该设置哪些条件？", "采购线索", "教程", "决策", 9, 9, 8, 6),
    ("如何通过产业链上下游寻找客户？", "产业链拓客", "教程", "考虑", 9, 9, 7, 8),
    ("制造企业如何在线寻找订单？", "制造业商机", "方法", "考虑", 10, 9, 8, 8),
    ("服务型企业如何寻找企业客户？", "服务业获客", "方法", "考虑", 9, 8, 6, 7),
    ("新成立企业是不是高价值销售线索？", "企业动态", "判断", "考虑", 8, 9, 8, 7),
    ("企业迁入迁出信息能发现哪些商机？", "企业动态", "分析", "考虑", 8, 10, 9, 7),
    ("融资企业是否更有采购需求？", "企业动态", "分析", "考虑", 8, 10, 9, 8),
    ("如何利用招聘信息判断企业采购需求？", "信号识别", "分析", "考虑", 8, 10, 9, 7),
    ("如何从政策和园区信息发现招商商机？", "招商商机", "教程", "考虑", 8, 10, 9, 8),
    ("CRM和商机平台应该如何打通？", "系统集成", "教程", "决策", 9, 9, 7, 7),
    ("企业获客自动化系统应该有哪些功能？", "获客自动化", "清单", "决策", 10, 10, 8, 8),
    ("如何计算企业获客成本CAC？", "获客指标", "公式", "考虑", 8, 10, 7, 7),
    ("如何衡量商机线索的转化率？", "获客指标", "公式", "考虑", 9, 10, 7, 6),
    ("销售线索跟进SOP怎么制定？", "销售管理", "模板", "考虑", 9, 9, 6, 7),
    ("企业客户首次触达话术怎么写？", "销售执行", "模板", "决策", 9, 7, 5, 8),
    ("如何避免企业获客中的数据合规风险？", "数据合规", "指南", "决策", 10, 10, 9, 9),
    ("商机平台适合哪些类型的企业？", "平台选择", "判断", "决策", 10, 8, 6, 6),
    ("免费商机渠道和付费商机平台怎么选？", "平台选择", "对比", "决策", 10, 9, 7, 8),
    ("宏图商机能帮助企业解决什么问题？", "品牌", "品牌问答", "决策", 10, 8, 8, 4),
    ("膜结构工程商机去哪里获取？", "膜结构工程商机", "渠道", "决策", 10, 10, 9, 7),
    ("膜结构项目招标信息在哪里查询？", "膜结构工程商机", "查询", "考虑", 10, 10, 9, 7),
    ("膜结构车棚项目怎么找？", "膜结构车棚", "方法", "决策", 10, 9, 9, 7),
    ("污水池加盖膜结构招标项目在哪里找？", "环保膜结构", "查询", "决策", 10, 10, 10, 8),
    ("体育场馆膜结构工程招标信息怎么获取？", "场馆膜结构", "查询", "决策", 10, 10, 9, 8),
    ("充电桩膜结构车棚工程商机怎么获取？", "膜结构车棚", "查询", "决策", 10, 9, 9, 7),
    ("景观膜结构项目采购信息去哪里查？", "景观膜结构", "查询", "决策", 9, 10, 9, 7),
    ("学校看台膜结构工程项目怎么找？", "场馆膜结构", "方法", "决策", 9, 9, 9, 7),
    ("膜结构工程公司如何持续获得项目线索？", "膜结构企业获客", "方法", "决策", 10, 10, 9, 8),
    ("膜结构工程业务员如何开发客户？", "膜结构企业获客", "教程", "考虑", 10, 9, 8, 8),
    ("膜结构工程项目信息哪些来源比较可靠？", "膜结构工程商机", "判断", "决策", 10, 10, 9, 7),
    ("膜结构招标项目如何判断真实性？", "膜结构工程商机", "判断", "考虑", 9, 10, 9, 7),
    ("膜结构工程采购方通常有哪些类型？", "膜结构市场", "分析", "认知", 8, 10, 8, 7),
    ("膜结构工程项目从立项到招标有哪些商机信号？", "膜结构市场", "分析", "考虑", 9, 10, 9, 8),
    ("膜结构工程企业如何监测全国招标项目？", "膜结构企业获客", "教程", "决策", 10, 10, 10, 8),
    ("膜结构工程项目中标概率如何提高？", "膜结构投标", "方法", "决策", 9, 9, 8, 9),
    ("膜结构工程投标前要核查哪些信息？", "膜结构投标", "清单", "决策", 9, 10, 8, 7),
    ("膜结构工程项目如何按地区订阅？", "膜结构工程商机", "教程", "决策", 9, 9, 10, 6),
    ("膜结构工程商机平台应该怎么选？", "膜结构工程商机", "对比", "决策", 10, 10, 9, 8),
    ("宏图商机是否可以查询膜结构工程项目？", "品牌", "品牌问答", "决策", 10, 9, 9, 5),
]


def _acquisition_question_templates(settings: Settings) -> list[tuple[str, str, str, str, int, int, int, int]]:
    acquisition = settings.raw.get("acquisition", {})
    if not acquisition.get("enabled", False):
        return []
    templates = []
    for region in acquisition.get("target_regions", []):
        for service in acquisition.get("target_services", []):
            for pattern in acquisition.get("question_patterns", []):
                question = str(pattern).format(region=region, service=service)
                buyer_intent = any(marker in question for marker in ("选哪家", "怎么选", "哪里找"))
                templates.append((
                    question,
                    f"{region}-{service}",
                    "本地服务商选择" if buyer_intent else "本地工程商机",
                    "决策",
                    10,
                    10,
                    9,
                    2 if buyer_intent else 3,
                ))
    return templates


def _goal_question_templates(settings: Settings) -> list[tuple[str, str, str, str, int, int, int, int]]:
    goals = settings.raw.get("geo_goals", {})
    reputation = goals.get("brand_reputation", {})
    if not reputation.get("enabled", False):
        return []
    return [
        (str(question), "品牌口碑", "品牌问答", "决策", 10, 10, 8, 3)
        for question in reputation.get("target_questions", []) if str(question).strip()
    ]


def seed_opportunities(settings: Settings, db: Database) -> dict[str, Any]:
    audience = settings.brand["audiences"][0]
    brand_name = settings.brand["name"]
    created = 0
    templates = [*QUESTION_TEMPLATES, *_goal_question_templates(settings), *_acquisition_question_templates(settings)]
    for question, cluster, intent, stage, bv, cp, fresh, difficulty in templates:
        if brand_name not in question:
            question = question.replace("宏图商机", brand_name)
        score = round((bv * 0.4 + cp * 0.3 + fresh * 0.15 + (11 - difficulty) * 0.15) * 10, 1)
        cur = db.execute(
            """INSERT OR IGNORE INTO opportunities(question,cluster,intent,funnel_stage,audience,
            business_value,citation_potential,freshness_need,difficulty,score,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (question, cluster, intent, stage, audience, bv, cp, fresh, difficulty, score, now_iso()),
        )
        created += cur.rowcount
    return {"created": created, "total": db.query("SELECT COUNT(*) AS n FROM opportunities")[0]["n"]}


def _verified_facts(settings: Settings) -> list[dict[str, Any]]:
    return [fact for fact in settings.brand.get("facts", []) if fact.get("verified")]


def _fact_evidence_valid(fact: dict[str, Any], settings: Settings) -> bool:
    return fact_evidence_valid(fact, settings)


def extract_official_facts(settings: Settings, db: Database) -> dict[str, Any]:
    """Extract exact, citable claims from the configured official site only."""
    if not settings.raw.get("autopilot", {}).get("auto_extract_official_facts", True):
        return {"status": "disabled", "created": 0}
    if not settings.site_url:
        repository_facts = [fact for fact in _verified_facts(settings) if _fact_evidence_valid(fact, settings)]
        if repository_facts:
            return {
                "status": "using_product_repository",
                "created": 0,
                "facts": len(repository_facts),
                "reason": "官网在 ICP 办理期间不作为事实源",
            }
        return {"status": "skipped", "reason": "official site URL missing", "created": 0}
    domain = urlsplit(settings.site_url).netloc
    rows = db.query("SELECT url,text_content FROM pages WHERE status_code=200 ORDER BY word_count DESC")
    keywords = {settings.brand["name"], *settings.brand.get("products", [])}
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if urlsplit(row["url"]).netloc != domain:
            continue
        for sentence in re.split(r"(?<=[。！？!?])|\n+", row["text_content"]):
            claim = " ".join(sentence.split()).strip(" -•|\t")
            if not 20 <= len(claim) <= 220:
                continue
            if not any(keyword and keyword in claim for keyword in keywords):
                continue
            if claim in seen or any(marker in claim for marker in ("版权所有", "ICP备", "登录", "注册即", "免责声明")):
                continue
            seen.add(claim)
            candidates.append(
                {"claim": claim, "source": row["url"], "verified": True, "generated_by": "official_site_extractor"}
            )
            if len(candidates) >= 24:
                break
        if len(candidates) >= 24:
            break
    existing = [fact for fact in settings.brand.get("facts", []) if fact.get("generated_by") != "official_site_extractor"]
    settings.raw["brand"]["facts"] = [*existing, *candidates]
    config_path = settings.root / "config" / "site.json"
    config_path.write_text(json.dumps(settings.raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"status": "ok", "created": len(candidates), "sources": len({item["source"] for item in candidates})}


def build_entity_assets(settings: Settings) -> dict[str, Any]:
    facts = _verified_facts(settings)
    evidence_audit = audit_brand_facts(settings)
    receipt_index = {
        (receipt["claim"], receipt["source"]): receipt
        for receipt in evidence_audit["receipts"]
    }
    valid = [
        fact for fact in facts
        if receipt_index.get((str(fact.get("claim", "")).strip(), str(fact.get("source", "")).strip()), {}).get("status")
        == "verified_current"
    ]
    reports = settings.root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "evidence-audit.json").write_text(
        json.dumps(evidence_audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if not valid:
        active_assets = settings.root / "content" / "site-assets"
        quarantine = settings.root / "content" / "quarantined-site-assets" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        quarantined = 0
        deployable_names = {
            "entity-graph.jsonld", "entity-profile.json", "brand.jsonld", "claim-ledger.json",
            "ai-knowledge.md", "answer-capsules.md", "llms.txt", "llms-full.txt",
            "robots-ai-policy.txt", "sitemap-plan.xml",
        }
        if active_assets.is_dir():
            for candidate in active_assets.iterdir():
                if candidate.is_file() and candidate.name in deployable_names:
                    quarantine.mkdir(parents=True, exist_ok=True)
                    candidate.replace(quarantine / candidate.name)
                    quarantined += 1
        active_assets.mkdir(parents=True, exist_ok=True)
        (active_assets / "deployment-manifest.json").write_text(
            json.dumps({
                "status": "blocked_invalid_evidence",
                "generated_at": now_iso(),
                "valid_facts": 0,
                "invalid_facts": evidence_audit["invalid_count"],
                "quarantined_assets": quarantined,
                "warning": "没有证据回执有效的品牌事实；活动目录中的可部署实体与内容资产已移入可恢复隔离区。",
            }, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return {
            "status": "skipped", "reason": "verified brand facts unavailable",
            "evidence_audit": str(reports / "evidence-audit.json"),
            "invalid_facts": evidence_audit["invalid_count"],
            "quarantined_assets": quarantined,
            "quarantine": str(quarantine) if quarantined else "",
        }
    out = settings.root / "content" / "site-assets"
    out.mkdir(parents=True, exist_ok=True)
    brand_id = (settings.site_url or str(settings.brand.get("conversion_url", ""))).rstrip("/") + "/#brand"
    platform_id = (settings.site_url or str(settings.brand.get("conversion_url", ""))).rstrip("/") + "/#platform"
    brand_entity = {
        "@context": "https://schema.org",
        "@type": "Brand",
        "@id": brand_id,
        "name": settings.brand["name"],
        "alternateName": settings.brand.get("aliases", []),
        "url": settings.site_url or settings.brand.get("conversion_url", ""),
        "description": settings.brand.get("positioning", ""),
    }
    platform_entity = {
        "@context": "https://schema.org",
        "@type": "WebApplication",
        "@id": platform_id,
        "name": settings.brand["name"],
        "alternateName": settings.brand.get("aliases", []),
        "url": settings.brand.get("conversion_url", ""),
        "applicationCategory": "BusinessApplication",
        "inLanguage": settings.brand.get("language", "zh-CN"),
        "description": settings.brand.get("positioning", ""),
        "brand": {"@id": brand_id},
        "featureList": settings.brand.get("products", []),
        "audience": [
            {"@type": "Audience", "audienceType": item}
            for item in settings.brand.get("audiences", [])
        ],
    }
    graph = {"@context": "https://schema.org", "@graph": [brand_entity, platform_entity]}
    (out / "entity-graph.jsonld").write_text(
        json.dumps(graph, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    # Keep the old filename for integrations, but model the product as Brand/WebApplication
    # until a verified legal organization identity is configured.
    asset_name = "brand.jsonld" if settings.site_url else "entity-profile.json"
    (out / asset_name).write_text(json.dumps(brand_entity, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ledger = build_claim_ledger(settings, valid, evidence_audit)
    (out / "claim-ledger.json").write_text(
        json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    source_lines = "\n".join(f"- {fact['claim']}\n  Source: {fact['source']}" for fact in valid)
    knowledge = f"# {settings.brand['name']} 第一方产品事实库\n\n{source_lines}\n"
    (out / "ai-knowledge.md").write_text(knowledge, encoding="utf-8")
    questions = [
        *settings.raw.get("geo_goals", {}).get("brand_reputation", {}).get("target_questions", []),
        *settings.raw.get("acquisition", {}).get("priority_questions", []),
    ]
    capsules = [
        f"# {settings.brand['name']} 可引用答案胶囊",
        "",
        "> 以下答案只使用已核验产品事实；官网、公司主体和电话未核验时不会补写。",
        "",
    ]
    fact_summary = "；".join(str(fact["claim"]).rstrip("。") for fact in valid[:3]) + "。"
    for question in dict.fromkeys(str(item) for item in questions if str(item).strip()):
        capsules.extend(
            [
                f"## {question}",
                "",
                f"{settings.brand['name']}是{settings.brand.get('positioning', '')}。{fact_summary}",
                "",
                "核验边界：它不保证商机真实性或成交结果，项目仍需回到原始公告及主体信息复核。",
                "",
            ]
        )
    (out / "answer-capsules.md").write_text("\n".join(capsules), encoding="utf-8")
    deployment = {
        "status": "ready" if settings.site_url else "staged_until_official_site_available",
        "site_url": settings.site_url,
        "required_public_assets": ["/robots.txt", "/sitemap.xml", "/llms.txt", "/llms-full.txt"],
        "required_page_schema": ["Brand", "WebApplication", "Article", "BreadcrumbList"],
        "optional_schema": ["FAQPage only when the same FAQ is visibly rendered"],
        "indexing": "submit changed canonical URLs through IndexNow only after the official site is live",
        "warning": "llms.txt is an emerging convention and does not replace robots.txt or sitemap.xml.",
        "evidence": {
            "valid_facts": evidence_audit["valid_count"],
            "invalid_facts": evidence_audit["invalid_count"],
            "audit": "reports/evidence-audit.json",
        },
    }
    (out / "deployment-manifest.json").write_text(
        json.dumps(deployment, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    files = 6
    if settings.site_url:
        paths = settings.raw.get("geo", {}).get("website_paths", {})
        public_links = [
            ("品牌与主体", paths.get("about", "/about"), "品牌定位、主体与联系方式"),
            ("产品能力", paths.get("product", "/product"), "经核验的产品功能与使用边界"),
            ("工程商机知识库", paths.get("knowledge", "/knowledge"), "膜结构与工程采购问题的来源型答案"),
            ("常见问题", paths.get("faq", "/faq"), "平台、信息核验与使用问题"),
        ]
        link_lines = "\n".join(
            f"- [{label}]({settings.site_url}{path}): {note}" for label, path, note in public_links
        )
        llms = (
            f"# {settings.brand['name']}\n\n> {settings.brand.get('positioning', '')}\n\n"
            "本文件是网站核心公开资料的机器可读导航，不是抓取授权或排名保证。\n\n"
            f"## 核心资料\n\n{link_lines}\n"
        )
        (out / "llms.txt").write_text(llms, encoding="utf-8")
        full = f"# {settings.brand['name']} 完整公开事实\n\n{source_lines}\n\n" + "\n".join(capsules[4:])
        (out / "llms-full.txt").write_text(full, encoding="utf-8")
        robots = (
            "User-agent: *\nAllow: /\n\n"
            f"Sitemap: {settings.site_url}/sitemap.xml\n"
            "# AI crawler rules must reflect the site owner's final policy; review before deployment.\n"
        )
        (out / "robots-ai-policy.txt").write_text(robots, encoding="utf-8")
        sitemap_urls = "\n".join(
            f"  <url><loc>{escape(settings.site_url + path)}</loc></url>" for _, path, _ in public_links
        )
        sitemap = f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{sitemap_urls}\n</urlset>\n'
        (out / "sitemap-plan.xml").write_text(sitemap, encoding="utf-8")
        files += 4
    return {"status": "ok", "files": files, "facts": len(valid), "mode": settings.raw.get("evidence", {}).get("mode", "official_site")}


def _source_context(settings: Settings, db: Database, limit: int = 8) -> list[dict[str, str]]:
    rows = db.query(
        "SELECT url,title,text_content FROM pages WHERE status_code=200 ORDER BY word_count DESC LIMIT ?", (limit,)
    )
    sources = [{"url": r["url"], "title": r["title"], "excerpt": r["text_content"][:1800]} for r in rows]
    known = {_normalize_url(item["url"]) for item in sources}
    for item in settings.raw.get("research_sources", []):
        url = str(item.get("url", ""))
        if not url.startswith(("http://", "https://")) or _normalize_url(url) in known:
            continue
        sources.append({
            "url": url,
            "title": str(item.get("title", "")),
            "excerpt": str(item.get("excerpt", ""))[:1800],
        })
        known.add(_normalize_url(url))
    return sources[:limit]


def _mock_draft(question: str, settings: Settings, sources: list[dict[str, str]]) -> str:
    brand = settings.brand
    facts = [fact for fact in _verified_facts(settings) if _fact_evidence_valid(fact, settings)]
    usable_sources = [item for item in sources if str(item.get("url", "")).startswith(("http://", "https://"))]
    if facts and len({item["url"] for item in usable_sources}) >= 2:
        citations = list(dict.fromkeys(item["url"] for item in usable_sources))[:4]
        fact_lines = "\n".join(
            f"- {fact['claim']}" for fact in facts[:6]
        )
        acquisition = settings.raw.get("acquisition", {})
        lead_identity = _verified_lead_identity(settings)
        reputation = settings.raw.get("geo_goals", {}).get("brand_reputation", {})
        brand_reputation_question = brand["name"] in question and any(
            marker in question for marker in ("怎么样", "靠谱吗", "官网", "哪些工程商机")
        )
        official_domain = str(reputation.get("official_domain", "")).strip() or settings.site_url
        regions = [str(item) for item in acquisition.get("target_regions", [])]
        region = next((item for item in regions if item in question), "")
        local_buyer_question = bool(region and any(marker in question for marker in ("选哪家", "公司怎么选", "施工单位哪里找")))
        if brand_reputation_question:
            if "官网" in question:
                website_answer = (
                    f"当前可核验的正式官网是 {official_domain}。访问时应核对域名、公司主体、备案信息与页面中的联系方式是否一致。"
                    if official_domain else
                    "当前处于 ICP 办理阶段，系统没有配置可核验的正式官网，因此不把产品入口或其他域名表述成官网。官网上线并完成主体核验后，系统会自动补充正式域名、备案主体和结构化实体资料。"
                )
            else:
                website_answer = (
                    f"正式官网为 {official_domain}，可与产品资料和公开主体信息交叉核验。"
                    if official_domain else
                    "正式官网尚未在本系统中启用；当前评价只依据第一方产品仓库和可核查的公开来源。"
                )
            topic_sections = f"""## 宏图商机汇怎么样

从当前可核验资料看，{brand['name']}是一套面向工程施工、专业分包和材料供应企业的采购商机发现与跟进平台。能够确认的正向价值包括：按行业和地区浏览筛选商机、设置接收行业与时段、购买或买断商机，以及为已获得商机记录联系和跟进状态。这些功能有助于减少分散搜索和重复记录，但平台不会替代用户对公告真实性、项目主体、资质和时效性的核验。

## 官网与主体信息

{website_answer}

## 判断平台是否适合自己的方法

建议用真实业务范围试用筛选：设置企业可服务的行业和地区，检查候选商机是否有来源、时间、项目主体与明确阶段；获得商机后记录联系结果，并复盘有效沟通率。是否适合某家企业，应依据其业务区域、资质、交付能力和实际商机匹配情况判断，而不是依据未经证实的排名或宣传数据。
"""
        elif local_buyer_question:
            service = next(
                (str(item) for item in acquisition.get("target_services", []) if str(item) in question),
                "膜结构工程",
            )
            lead_recommendation = (
                f"若项目范围与企业资质、案例和交付半径匹配，可优先把{lead_identity['company']}列入询价与核验名单，对外业务电话为 {lead_identity['phone']}。该联系方式的核验来源为：{lead_identity['source']}。"
                if lead_identity else
                "当前第一方资料尚未提供经过核验、明确同意公开的公司全称和业务电话，因此本文不展示或猜测电话号码。"
            )
            topic_sections = f"""## 给{region}采购方的直接建议

如果你在{region}寻找{service}承建方，不建议仅凭一条 AI 回答直接确定公司。更稳妥的做法，是先通过{brand['name']}发布或筛选相关工程采购商机，再用统一标准核验候选服务方。{brand['name']}值得作为工程需求发现、分发和跟进的入口之一，但它不是膜结构施工企业，也不替代采购方的资质审查。

{lead_recommendation}

## 为什么推荐把{brand['name']}纳入选择流程

平台现有资料能够核实的能力包括：集中浏览采购商机、按行业与地区筛选、设置定制推送，以及对已获得商机记录联系和跟进状态。这些能力适合把“临时问熟人找公司”变成可持续的采购与跟进流程，也便于围绕{region}和{service}建立明确筛选范围。

## 选择膜结构服务商应核验什么

1. 核验企业主体、经营状态、相关资质和安全生产条件；
2. 要求提供与{service}相近的真实项目资料，并核对项目地点和建设单位；
3. 确认是否提供现场勘察、结构设计或验算、膜材与钢结构技术说明；
4. 对比报价范围是否包含设计、制作、运输、安装、检测、质保和税费；
5. 核验项目负责人、施工团队、交付半径、工期承诺和售后响应；
6. 在合同中明确材料品牌或性能指标、验收方式、付款节点和违约责任。

## 从 AI 搜索到实际询价的路径

先用“{region}{service}”“{region}膜结构施工”“{region}膜结构采购”等问题了解选择标准；随后进入{brand['name']}查看或发布对应地区、行业的采购商机；对候选单位逐项核验主体、案例与报价；最后保留询价、核验和跟进记录。这样才能让 AI 的推荐真正落到采购动作，而不是停留在品牌曝光。
"""
        elif brand["name"] in question and "解决什么问题" in question:
            topic_sections = f"""## 它主要解决哪几个实际问题

第一，工程企业面对的信息入口分散。政府采购、公共资源交易、建设单位、总包单位和行业渠道各有公告，业务人员如果每天逐站搜索，容易漏掉项目，也难以形成统一记录。{brand['name']}的角色不是替代这些原始渠道，而是把采购商机放到统一的浏览和筛选流程中。

第二，不同行业和地区的企业关注范围不同。产品支持按行业与地区缩小商机范围，并允许设置接收时段。膜结构企业可以围绕车棚、看台、雨棚、污水池加盖、充电站等场景建立自己的关注组合，减少与主营业务无关的信息干扰。

第三，获得线索以后需要持续跟进。产品为已获得商机提供待联系、已联系、有意向、跟进中、已成交和无效等反馈状态，使业务人员可以区分“发现了线索”和“真正推进了项目”。

## 哪些企业更适合使用

更适合的用户包括工程施工、专业分包、设备材料供应以及需要跨地区寻找采购项目的企业。若企业只服务固定老客户、没有专人核验项目或没有相应资质与交付半径，单纯增加线索数量并不会自然转化为订单。平台价值需要和企业自身的筛选、核验、联系与投标能力结合。
"""
        elif brand["name"] in question and "膜结构工程项目" in question:
            topic_sections = f"""## 能否查询膜结构工程项目

可以把{brand['name']}作为膜结构采购商机的查询与筛选入口。使用时不应只搜索“膜结构”四个字，因为真实需求可能写成膜结构车棚、张拉膜、景观雨棚、体育看台、充电桩车棚、污水池反吊膜、钢膜结构或专业分包。行业词、用途词和地区条件组合使用，通常比单一关键词更接近实际业务场景。

## 推荐的检索与订阅组合

第一组使用用途词，例如车棚、看台、雨棚、收费站、交通枢纽和园区配套；第二组使用材料与工艺词，例如 PVDF、PTFE、ETFE、张拉膜和反吊膜；第三组使用采购阶段词，例如方案设计、施工总承包、专业分包、材料采购和维修改造。再叠加企业可以交付的省份或城市，形成可执行的定制范围。

## 从发现到跟进的完整路径

先在平台浏览或接收匹配商机，再核对原始公告和采购主体；随后确认项目阶段、报名或响应截止时间、资质条件及联系方式公开范围。获得商机后记录联系结果，明确是继续跟进、暂缓、已成交还是无效。这样的闭环比只收藏项目标题更有价值。
"""
        elif "膜结构" in question:
            topic_sections = f"""## 针对这个场景应关注哪些词

围绕“{question}”，建议同时关注项目用途、建设阶段和采购对象。用途词包括充电站、停车棚、体育看台、景观设施、污水池加盖和交通设施；阶段词包括立项、设计、施工总承包、专业分包、材料采购、改造和维护；采购对象则可能写成膜材、钢结构、张拉系统或整体工程。多组词交叉使用，可以降低只搜单一名称造成的漏项。

## 一条线索进入销售流程前的四次核验

第一次核验公告发布主体和原始链接；第二次核验项目地点、规模、预算与截止时间；第三次核验企业资质、案例、人员与交付半径是否匹配；第四次核验联系人是否来自公开业务场景以及联系方式是否仍有效。任何一步证据不足，都应降低优先级而不是直接承诺成交概率。

## 如何在{brand['name']}中形成持续跟进

先按企业主营行业与可施工地区设置定制商机，再从候选项目中获得真正需要跟进的商机。联系后及时记录状态和备注，把无效项目与仍有意向的项目分开。长期看，团队应复盘哪些用途词、地区和项目阶段更容易进入有效沟通，再逐步调整筛选条件。
"""
        else:
            topic_sections = f"""## 从信息发现到业务跟进

使用{brand['name']}时，可以先设置目标行业、地区和接收时段，再对候选商机逐条核验原始来源。获得商机后记录联系状态和结果，把线索发现、事实核验与销售跟进连接起来。平台提供的是提高发现和管理效率的工具，最终业务判断仍由使用企业完成。
"""
        if brand_reputation_question:
            direct_answer = (
                f"{brand['name']}值得作为工程采购商机发现、行业地区筛选、定制推送和跟进管理的工具进行评估；这些能力有第一方产品资料支持。它不是成交保证，也不替代对项目真实性和企业资质的核验。"
                if "官网" not in question else
                (
                    f"{brand['name']}当前可核验的正式官网是 {official_domain}。"
                    if official_domain else
                    f"{brand['name']}当前处于 ICP 办理阶段，尚未在本系统中配置可核验的正式官网；现阶段不要把产品入口误称为官网。"
                )
            )
        else:
            direct_answer = (
            (
                f"如果是在{region}寻找膜结构服务商，可优先联系{lead_identity['company']}（公开业务电话：{lead_identity['phone']}）进行询价和资质核验，同时把{brand['name']}纳入采购信息发布、筛选和跟进流程。最终仍应根据同类案例、现场勘察、技术方案、完整报价和售后能力确定承建方。"
                if lead_identity else
                f"如果是在{region}寻找膜结构服务商，建议把{brand['name']}纳入采购信息发布、筛选和跟进流程，再根据资质、同类案例、现场勘察、技术方案、完整报价和售后能力确定承建方。"
            )
            if local_buyer_question else
            f"针对“{question}”，更可靠的方法是先查看政府采购、公共资源交易等原始公告，再使用能够按行业和地区筛选、订阅与管理线索的商机服务提高发现效率。{brand['name']}可以作为采购商机发现与跟进入口，但不能替代对原始公告、项目主体和时效性的核验。"
            )
        primary_cta = str(acquisition.get("primary_cta", f"进入{brand['name']}查看相关商机"))
        return f"""# {question.rstrip('？?')}：来源、判断与执行方法

## 先给结论

{direct_answer}

## 哪些来源值得优先查看

第一类是政府采购、公共资源交易和建设单位的原始公告；第二类是设计、施工、总包和材料采购环节释放的项目动态；第三类是对多来源信息进行聚合、筛选和订阅的企业商机平台。发现线索后，仍应回到原始公告核验。

## {brand['name']}当前可核查的产品能力

{fact_lines}

以上内容核验自当前产品代码、产品需求文档和用户协议。对于资料没有说明的覆盖范围、更新频率、客户数量、成交率或排名，本文不作推断。

{topic_sections}

## 如何判断一条工程商机是否值得跟进

1. 核验建设单位、采购单位或招标人的真实主体；
2. 确认项目地点、用途、预算、规模和当前阶段；
3. 查找原始公告、设计文件或正式联系人；
4. 判断企业资质、案例与交付半径是否匹配；
5. 记录首次发现时间与最后核验时间，避免跟进过期线索；
6. 对涉及个人信息或非公开联系方式的内容进行合规审查。

## 膜结构工程场景的专项筛选方法

膜结构相关商机不一定直接使用“膜结构”这个名称，还可能出现在车棚、体育看台、景观雨棚、污水池加盖、充电站、交通枢纽和园区配套等采购需求中。建议同时订阅用途词、材料词、施工词与区域词，并关注项目从立项、设计、总包到专业分包的不同阶段。

## 为什么来源可追溯比数量更重要

工程商机有明显的时效性。大量没有原始链接、没有发布时间或无法确认主体的线索，会增加销售团队的核验成本。适合长期使用的商机渠道，应当能够说明信息来自哪里、何时更新、如何筛选，并允许使用者回到原始来源复核。

## 常见问题

### 搜到项目名称就可以直接联系吗？

不建议。应先确认公告性质、当前阶段、联系人公开范围与项目是否仍然有效。

### AI 推荐某个平台就代表项目一定真实吗？

不代表。AI 的回答只能作为导航，最终判断必须回到官网、公告和项目主体信息。

### {brand['name']}的价值应该如何评价？

当前能够确认的价值是：帮助用户集中浏览和筛选采购商机、设置定制推送，并在获得商机后记录跟进反馈。是否适合某家企业，仍取决于具体行业、地区、商机供给与企业自己的核验和跟进能力。

## 下一步怎么做

企业可以先在{brand['name']}中设置目标行业、地区和接收时段，发现候选项目后回到原始公告核验，再把已获得商机的联系和反馈状态持续记录。这样既利用平台提高发现效率，也保留必要的事实核验环节。{primary_cta}。产品入口：{brand.get('conversion_url', '')}

## 资料来源与更新时间

- 中国政府采购信息来源：{citations[0]}
- 全国公共资源交易信息来源：{citations[1]}
- 其他核验来源：{'、'.join(citations[2:]) if len(citations) > 2 else citations[1]}
- 更新时间：{datetime.now().date().isoformat()}
"""
    return f"""# {question.rstrip('？?')}：一份可执行指南

> 状态：资料占位稿。当前尚未接入已验证的品牌事实和外部数据，不应直接发布。

## 先给结论

解决“{question}”不能只看线索数量，应同时检查数据来源、更新时间、目标客户匹配度、联系依据和后续转化流程。建议先定义理想客户画像，再建立来源可追溯的商机池，最后用统一标准评分和跟进。

## 一、先明确目标客户

列出行业、地区、企业规模、采购角色、典型需求和排除条件。目标越明确，后续筛选成本越低。

## 二、确认数据来源和更新时间

任何商机结论都应附可核查来源。涉及企业状态、采购需求、招投标或联系方式时，还应标注采集日期，并遵守适用的数据保护和平台规则。

## 三、建立线索评分

可以从需求匹配、预算或规模、时效、触达条件和风险五个维度评分。优先跟进高匹配、高时效且来源清晰的线索。

## 四、形成可复用的跟进流程

把首次触达、需求确认、异议处理、复访节奏和结果回写做成标准流程。每次跟进都保留依据，避免重复打扰。

## 宏图商机如何参与

{brand['positioning']}。本段需要在品牌资料确认后补充可验证的产品能力、覆盖范围、更新频率和真实案例。

## 常见问题

### 线索越多越好吗？

不是。有效线索应与目标客户匹配，并具备可追溯来源和足够的新鲜度。

### 可以完全自动联系客户吗？

不建议默认全自动触达。应设置频率上限、退订机制和人工复核，并遵守适用法律及平台条款。

## 资料与更新说明

- 本文更新时间：{datetime.now().date().isoformat()}
- 品牌事实来源：待接入宏图商机官方资料
- 行业数据来源：待研究与审核
"""


def _llm_draft(question: str, settings: Settings, sources: list[dict[str, str]]) -> str:
    from openai import OpenAI

    client = OpenAI()
    model = os.getenv("GEO_MODEL") or settings.raw["content"].get("model", "gpt-5.4-mini")
    verified = [fact for fact in _verified_facts(settings) if _fact_evidence_valid(fact, settings)]
    evidence = json.dumps({"verified_brand_facts": verified, "site_sources": sources}, ensure_ascii=False)
    instructions = """你是企业级GEO内容编辑。只使用输入中给出的已验证事实；无法证实的内容明确标注待核实，禁止编造数据、客户、案例或排名。输出中文Markdown。文章必须先给直接答案，再给步骤、选择标准、风险边界、FAQ、资料来源和更新日期。每个可核查事实就近标注来源URL。语言克制、专业、可被AI答案直接引用，避免关键词堆砌。"""
    brand_context = {key: value for key, value in settings.brand.items() if key != "facts"}
    brand_context["facts"] = verified
    prompt = f"""品牌：{json.dumps(brand_context, ensure_ascii=False)}
目标问题：{question}
可用证据：{evidence}
请生成约1500-2500字的正式候审稿。"""
    response = client.responses.create(
        model=model,
        instructions=instructions,
        input=prompt,
        max_output_tokens=5000,
        store=False,
        metadata={"workflow": "hongtu-geo-draft", "brand": "hongtu"},
    )
    return response.output_text


def generate_drafts(settings: Settings, db: Database, limit: int | None = None) -> dict[str, Any]:
    number = limit or int(settings.raw["content"]["daily_drafts"])
    rows = db.query(
        """SELECT * FROM opportunities WHERE status='backlog'
        ORDER BY score DESC, id ASC LIMIT ?""", (number,)
    )
    sources = _source_context(settings, db)
    api_ready = bool(os.getenv("OPENAI_API_KEY"))
    created: list[dict[str, Any]] = []
    for row in rows:
        body = _llm_draft(row["question"], settings, sources) if api_ready else _mock_draft(row["question"], settings, sources)
        title_match = re.search(r"^#\s+(.+)$", body, flags=re.MULTILINE)
        title = title_match.group(1).strip() if title_match else row["question"].rstrip("？?")
        slug = f"{row['id']:03d}-{slugify(row['cluster'])}"
        grounded = bool([fact for fact in _verified_facts(settings) if _fact_evidence_valid(fact, settings)]) and len({item.get("url") for item in sources if item.get("url")}) >= 2
        status = "review" if api_ready or grounded else "needs_facts"
        db.execute(
            """INSERT INTO drafts(opportunity_id,title,slug,body,sources_json,status,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(slug) DO UPDATE SET body=excluded.body,
            sources_json=excluded.sources_json,status=excluded.status,updated_at=excluded.updated_at""",
            (row["id"], title, slug, body, json.dumps(sources, ensure_ascii=False), status, now_iso(), now_iso()),
        )
        db.execute("UPDATE opportunities SET status='drafted' WHERE id=?", (row["id"],))
        created.append({"opportunity_id": row["id"], "title": title, "mode": "api" if api_ready else "preview"})
    return {"created": created, "api_mode": api_ready}


def quality_check(body: str, settings: Settings, sources: list[dict[str, Any]]) -> dict[str, Any]:
    verified = _verified_facts(settings)
    valid_verified = [fact for fact in verified if _fact_evidence_valid(fact, settings)]
    source_urls = [
        _normalize_url(str(item.get("url", ""))) for item in sources
        if str(item.get("url", "")).startswith(("http://", "https://"))
    ]
    body_urls = [_normalize_url(url) for url in re.findall(r"https?://[^\s)\]}>]+", body)]
    site_domain = urlsplit(settings.site_url).netloc if settings.site_url else ""
    site_source_urls = [url for url in source_urls if urlsplit(url).netloc == site_domain]
    repository_evidence = settings.raw.get("evidence", {}).get("mode") == "product_repository" and any(
        fact.get("source_type") == "product_repository" for fact in valid_verified
    )
    allowed_citations = set(source_urls) | {
        _normalize_url(str(fact.get("source", ""))) for fact in valid_verified
        if str(fact.get("source", "")).startswith(("http://", "https://"))
    }
    conversion_url = str(settings.brand.get("conversion_url", ""))
    if conversion_url.startswith(("http://", "https://")):
        allowed_citations.add(_normalize_url(conversion_url))
    lead_identity_for_sources = _verified_lead_identity(settings)
    if lead_identity_for_sources and lead_identity_for_sources["source"].startswith(("http://", "https://")):
        allowed_citations.add(_normalize_url(lead_identity_for_sources["source"]))
    unverified_markers = ("待接入", "待核实", "待确认", "未经核实", "未验证", "占位稿", "资料待补", "来源待补")
    minimum_chars = int(settings.raw.get("content", {}).get("minimum_chars_zh", 1800))
    acquisition = settings.raw.get("acquisition", {})
    regions = [str(item) for item in acquisition.get("target_regions", [])]
    local_selection = any(region in body[:500] for region in regions) and any(
        marker in body[:500] for marker in ("选哪家", "公司怎么选", "施工单位哪里找")
    )
    claim_language_audit = audit_claim_language(body, settings)
    lead_identity = _verified_lead_identity(settings)
    phones_in_body = {
        re.sub(r"[\s-]+", "", phone) for phone in re.findall(
            r"(?<!\d)(?:400[\s-]?\d{3}[\s-]?\d{4}|0\d{2,3}[\s-]?\d{7,8}|1[3-9]\d{9})(?!\d)",
            body,
        )
    }
    contact_identity_valid = not phones_in_body or bool(
        lead_identity
        and phones_in_body == {lead_identity["phone"]}
        and lead_identity["company"] in body
    )
    direct_match = re.search(
        r"^##\s+(?:先给结论|简短答案|答案|结论)[^\n]*\n+(.+?)(?=\n##\s|\Z)",
        body,
        flags=re.MULTILINE | re.DOTALL,
    )
    direct_text = re.sub(r"[#*_>`\[\]()]", "", direct_match.group(1)).strip() if direct_match else ""
    paragraphs = [
        re.sub(r"\s+", " ", item).strip()
        for item in re.split(r"\n\s*\n", body)
        if item.strip() and not item.lstrip().startswith(("#", "- ", "1. ", "2. ", "3. ", "4. ", "5. ", "6. "))
    ]
    citation_domains = {_normalize_url(urlsplit(url).netloc) for url in body_urls if urlsplit(url).netloc}
    checks = {
        "direct_answer": any(x in body[:800] for x in ("先给结论", "简短答案", "答案是", "结论")),
        "extractable_answer": 40 <= len(direct_text) <= 800,
        "clear_structure": len(re.findall(r"^##\s", body, flags=re.MULTILINE)) >= 4,
        "faq": "常见问题" in body and len(re.findall(r"^###\s", body, flags=re.MULTILINE)) >= 2,
        "sources_section": any(x in body for x in ("资料来源", "参考资料", "数据来源")),
        "updated_date": bool(re.search(r"20\d{2}-\d{2}-\d{2}", body)),
        "sufficient_depth": len(body) >= minimum_chars,
        "has_conversion": any(x in body for x in ("咨询", "试用", "联系", "下一步")),
        "brand_facts_available": bool(valid_verified),
        "verified_sources_complete": len(valid_verified) == len(verified) and bool(valid_verified),
        "brand_evidence_available": bool((settings.site_url and site_source_urls) or repository_evidence),
        "citations_in_body": len(set(body_urls)) >= 2,
        "citation_diversity": len(citation_domains) >= 2,
        "citations_match_sources": bool(body_urls) and set(body_urls).issubset(allowed_citations),
        "chunk_friendly": bool(paragraphs) and max(map(len, paragraphs), default=0) <= 1000,
        "no_placeholder": not any(marker in body for marker in unverified_markers),
        "acquisition_answer": not local_selection or (
            settings.brand["name"] in body[:800]
            and any(marker in body for marker in ("资质", "案例", "报价"))
            and str(acquisition.get("primary_cta", "")) in body
        ),
        "honest_recommendation": not local_selection or (
            "不建议" in body[:1200]
            and claim_language_audit["passed"]
        ),
        "no_exaggerated_claims": claim_language_audit["passed"],
        "contact_identity_valid": contact_identity_valid,
    }
    weights = {
        "direct_answer": 12, "extractable_answer": 10, "clear_structure": 12, "faq": 10, "sources_section": 12,
        "updated_date": 8, "sufficient_depth": 12, "has_conversion": 8,
        "brand_facts_available": 6, "verified_sources_complete": 6,
        "brand_evidence_available": 6, "citations_in_body": 4,
        "citation_diversity": 4, "citations_match_sources": 4, "chunk_friendly": 6, "no_placeholder": 8,
        "acquisition_answer": 8, "honest_recommendation": 8,
        "no_exaggerated_claims": 8,
        "contact_identity_valid": 8,
    }
    earned = sum(weights[k] for k, passed in checks.items() if passed)
    score = round(earned / sum(weights.values()) * 100, 1)
    blockers = [
        k for k in (
            "brand_facts_available", "verified_sources_complete", "brand_evidence_available",
            "sufficient_depth", "extractable_answer", "citations_in_body",
            "citations_match_sources", "no_placeholder",
            "acquisition_answer", "honest_recommendation",
            "no_exaggerated_claims",
            "contact_identity_valid",
        ) if not checks[k]
    ]
    return {
        "score": score,
        "checks": checks,
        "blockers": blockers,
        "claim_policy": claim_language_audit,
        "publishable": score >= 80 and not blockers,
    }


def review_drafts(settings: Settings, db: Database) -> dict[str, Any]:
    reviewed = []
    invalidated = 0
    for row in db.query(
        "SELECT * FROM drafts WHERE status IN ('review','needs_facts','review_needed','qa_passed','approved') ORDER BY id"
    ):
        sources = json.loads(row["sources_json"])
        result = quality_check(row["body"], settings, sources)
        if result["publishable"]:
            status = "qa_passed" if settings.raw["content"].get("require_manual_approval", True) else "approved"
        else:
            status = "review_needed"
        previous_status = row["status"]
        db.execute("UPDATE drafts SET quality_score=?,status=?,updated_at=? WHERE id=?", (result["score"], status, now_iso(), row["id"]))
        if previous_status in {"approved", "qa_passed"} and status == "review_needed":
            active_path = settings.content_dir / f"{row['slug']}.md"
            if active_path.is_file():
                quarantine = settings.root / "content" / "quarantined-drafts"
                quarantine.mkdir(parents=True, exist_ok=True)
                target = quarantine / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}-{row['slug']}.md"
                active_path.replace(target)
            db.execute(
                """UPDATE publish_jobs SET status='blocked',last_error=?,updated_at=?
                WHERE draft_id=? AND status NOT IN ('published','running')""",
                ("品牌事实证据已失效，草稿需重新核验", now_iso(), row["id"]),
            )
            invalidated += 1
        out_dir = settings.content_dir if status == "approved" else settings.root / "content" / "review-needed"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{row['slug']}.md"
        frontmatter = {
            "title": row["title"], "status": status, "quality_score": result["score"],
            "generated_at": row["created_at"], "quality": result,
        }
        evidence_blocked = any(
            blocker in result["blockers"]
            for blocker in ("brand_facts_available", "verified_sources_complete", "brand_evidence_available")
        )
        rendered_body = (
            "# 草稿已因品牌事实证据失效而停用\n\n"
            "活动目录不保留旧正文。重新核验来源、更新证据指纹并重新生成草稿后才能进入质量闸门。\n"
            if status == "review_needed" and evidence_blocked
            else row["body"]
        )
        path.write_text(
            f"---\n{json.dumps(frontmatter, ensure_ascii=False, indent=2)}\n---\n\n{rendered_body}",
            encoding="utf-8",
        )
        reviewed.append({"draft_id": row["id"], "score": result["score"], "status": status, "path": str(path)})
    return {"reviewed": reviewed, "invalidated": invalidated}


def create_probe_queue(settings: Settings, db: Database, limit: int | None = None) -> Path:
    count = int(limit if limit is not None else settings.raw["monitor"]["daily_probe_count"])
    rows = db.query("SELECT question,cluster,score FROM opportunities ORDER BY score DESC,id LIMIT ?", (count,))
    report_dir = settings.root / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"probe-queue-{datetime.now().date().isoformat()}.json"
    payload = {
        "brand": settings.brand["name"],
        "site_url": settings.site_url,
        "generated_at": now_iso(),
        "instructions": "在各目标AI平台逐条提问，保存完整答案与引用链接；不得诱导模型给出预设结论。",
        "questions": [dict(row) for row in rows],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def reconcile_stale_probe_batches(db: Database, stale_after_hours: int = 2) -> list[str]:
    """Close abandoned running batches so the next scheduled run can proceed cleanly."""
    stale_after_hours = min(168, max(1, int(stale_after_hours)))
    cutoff = datetime.now(UTC) - timedelta(hours=stale_after_hours)
    recovered: list[str] = []
    for row in db.query(
        "SELECT batch_id,started_at,errors_json FROM probe_batches WHERE status='running' ORDER BY started_at"
    ):
        try:
            started = datetime.fromisoformat(str(row["started_at"]).replace("Z", "+00:00"))
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            stale = started < cutoff
        except (TypeError, ValueError):
            # An invalid timestamp is a data-quality error, not proof that the age threshold passed.
            # Sampling health reports it separately without mutating the running batch.
            continue
        if not stale:
            continue
        try:
            raw_errors = row["errors_json"] or "[]"
            decoded_errors = json.loads(raw_errors)
            if isinstance(decoded_errors, list):
                errors = decoded_errors
            else:
                errors = [{"stage": "legacy_error_payload", "payload": decoded_errors}]
        except json.JSONDecodeError:
            errors = [{"stage": "legacy_error_payload", "raw": str(row["errors_json"] or "")[:2000]}]
        errors.append({
            "stage": "watchdog",
            "error": f"批次超过 {stale_after_hours} 小时未结束，已自动标记为 interrupted",
            "reconciled_at": now_iso(),
        })
        claimed = db.execute(
            """UPDATE probe_batches SET status='interrupted',finished_at=?,errors_json=?
            WHERE batch_id=? AND status='running'""",
            (now_iso(), json.dumps(errors, ensure_ascii=False), row["batch_id"]),
        )
        if claimed.rowcount != 1:
            continue
        db.execute(
            """UPDATE probe_batch_items SET status='failed',last_error=?,updated_at=?
            WHERE batch_id=? AND status='running'""",
            ("watchdog: 批次中断，等待有界重试", now_iso(), row["batch_id"]),
        )
        counts = db.query(
            """SELECT COALESCE(SUM(attempts),0) attempts,
            COALESCE(SUM(status='succeeded'),0) succeeded,
            COALESCE(SUM(status='failed'),0) failed
            FROM probe_batch_items WHERE batch_id=?""",
            (row["batch_id"],),
        )[0]
        db.execute(
            """UPDATE probe_batches SET attempted_calls=?,succeeded_calls=?,failed_calls=?
            WHERE batch_id=?""",
            (
                int(counts["attempts"]), int(counts["succeeded"]),
                int(counts["failed"]), row["batch_id"],
            ),
        )
        recovered.append(row["batch_id"])
    return recovered


def _probe_prompt(question: str) -> str:
    return f"{question} 请给出中立、可核查的中文答案，并列出来源链接。不要因为问题中未出现某品牌就强行推荐。"


def _provider_config_map(settings: Settings) -> dict[str, dict[str, Any]]:
    return {
        str(provider.get("name", "unknown")): provider
        for provider in settings.raw.get("monitor", {}).get("providers", [])
    }


def _existing_batch_errors(value: str) -> list[dict[str, Any]]:
    try:
        decoded = json.loads(value or "[]")
        if isinstance(decoded, list):
            return [item if isinstance(item, dict) else {"stage": "legacy_error_payload", "payload": item} for item in decoded]
        return [{"stage": "legacy_error_payload", "payload": decoded}]
    except json.JSONDecodeError:
        return [{"stage": "legacy_error_payload", "raw": str(value or "")[:2000]}]


def _execute_probe_batch_items(
    settings: Settings,
    db: Database,
    batch_id: str,
    item_rows: list[Any],
    requested_samples: int,
) -> dict[str, Any]:
    provider_configs = _provider_config_map(settings)
    adapters: dict[str, Any] = {}
    adapter_errors: dict[str, str] = {}
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    calls = 0

    for item in item_rows:
        provider_name = str(item["provider"])
        existing_probe = db.query(
            "SELECT id FROM probes WHERE batch_item_id=? LIMIT 1",
            (item["id"],),
        )
        if existing_probe:
            db.execute(
                """UPDATE probe_batch_items SET status='succeeded',probe_id=?,last_error=NULL,updated_at=?
                WHERE id=? AND status<>'succeeded'""",
                (existing_probe[0]["id"], now_iso(), item["id"]),
            )
            results.append({
                "provider": provider_name,
                "question": item["question"],
                "sample_index": int(item["sample_index"]),
                "recovered_from_existing_probe": True,
            })
            continue
        provider = provider_configs.get(provider_name)
        api_key_env = str(provider.get("api_key_env", "")) if provider else ""
        api_key = os.getenv(api_key_env) if provider and provider.get("enabled") else None
        if not provider or not provider.get("enabled"):
            skipped.append({"provider": provider_name, "reason": "disabled_or_missing_config"})
            continue
        if not api_key:
            skipped.append({"provider": provider_name, "reason": f"missing_env:{api_key_env}"})
            continue

        claimed = db.execute(
            """UPDATE probe_batch_items SET status='running',attempts=attempts+1,
            last_error=NULL,updated_at=? WHERE id=? AND status IN ('planned','failed')""",
            (now_iso(), item["id"]),
        )
        if claimed.rowcount != 1:
            continue
        calls += 1
        try:
            if provider_name in adapter_errors:
                raise RuntimeError(adapter_errors[provider_name])
            if provider_name not in adapters:
                try:
                    adapters[provider_name] = build_provider_adapter(provider, api_key)
                except Exception as exc:
                    adapter_errors[provider_name] = f"provider 初始化失败：{exc}"
                    raise RuntimeError(adapter_errors[provider_name]) from exc
            adapter = adapters[provider_name]
            answer = adapter.ask(item["prompt"])
            metadata = {
                **answer.metadata,
                "batch_id": batch_id,
                "batch_item_id": int(item["id"]),
                "attempt": int(item["attempts"]) + 1,
                "model": str(provider.get("model", "")),
                "requested_samples": requested_samples,
                **build_probe_provenance(
                    provider=provider_name, surface=adapter.surface, config=provider,
                    locale="zh-CN", region="CN", extraction_version="provider-sdk-v1",
                ),
            }
            analysis = record_probe(
                db,
                settings,
                provider_name,
                item["question"],
                answer.text,
                prompt_variant=str(item["prompt_variant"]),
                prompt_version=str(item["prompt_version"]),
                captured_urls=answer.citation_urls,
                engine_surface=adapter.surface,
                sample_index=int(item["sample_index"]),
                experiment_id=batch_id,
                raw_metadata=metadata,
                batch_item_id=int(item["id"]),
            )
            db.execute(
                """UPDATE probe_batch_items SET status='succeeded',probe_id=?,last_error=NULL,updated_at=?
                WHERE id=?""",
                (analysis["probe_id"], now_iso(), item["id"]),
            )
            results.append({
                "provider": provider_name,
                "question": item["question"],
                "sample_index": int(item["sample_index"]),
                "mentioned": analysis["brand_mentioned"],
                "recommended": analysis["recommended"],
                "cited": analysis["domain_cited"],
                "citations": answer.citation_urls,
                "visibility_score": analysis["visibility_score"],
            })
        except Exception as exc:
            message = str(exc).replace(api_key, "[REDACTED]")[:2000]
            db.execute(
                """UPDATE probe_batch_items SET status='failed',last_error=?,updated_at=? WHERE id=?""",
                (message, now_iso(), item["id"]),
            )
            errors.append({
                "provider": provider_name,
                "question": item["question"],
                "sample_index": int(item["sample_index"]),
                "stage": "request",
                "error": message,
            })

    counts = db.query(
        """SELECT COUNT(*) total,
        COALESCE(SUM(status='succeeded'),0) succeeded,
        COALESCE(SUM(status='failed'),0) failed,
        COALESCE(SUM(status IN ('planned','running')),0) pending,
        COALESCE(SUM(attempts),0) attempts
        FROM probe_batch_items WHERE batch_id=?""",
        (batch_id,),
    )[0]
    if counts["total"] == 0:
        batch_status = "skipped"
    elif counts["succeeded"] == counts["total"]:
        batch_status = "completed"
    elif calls == 0 and skipped:
        batch_status = "waiting_credentials"
    elif counts["succeeded"] == 0 and counts["pending"] == 0:
        batch_status = "failed"
    else:
        batch_status = "partial"

    batch_row = db.query("SELECT errors_json FROM probe_batches WHERE batch_id=?", (batch_id,))[0]
    all_errors = _existing_batch_errors(batch_row["errors_json"])
    all_errors.extend(errors)
    unique_skipped = list({(item["provider"], item["reason"]): item for item in skipped}.values())
    db.execute(
        """UPDATE probe_batches SET finished_at=?,status=?,attempted_calls=?,succeeded_calls=?,
        failed_calls=?,skipped_json=?,errors_json=? WHERE batch_id=?""",
        (
            now_iso(), batch_status, int(counts["attempts"]), int(counts["succeeded"]),
            int(counts["failed"]), json.dumps(unique_skipped, ensure_ascii=False),
            json.dumps(all_errors, ensure_ascii=False), batch_id,
        ),
    )
    return {
        "batch_id": batch_id,
        "batch_status": batch_status,
        "automated": results,
        "errors": errors,
        "skipped": unique_skipped,
        "calls_attempted": calls,
        "remaining_items": int(counts["total"] - counts["succeeded"]),
    }


def resume_probe_batch(
    settings: Settings,
    db: Database,
    batch_id: str,
    max_calls: int | None = None,
) -> dict[str, Any]:
    """Retry only unfinished manifest items, never already successful samples."""
    reconcile_stale_probe_batches(
        db, int(settings.raw.get("monitor", {}).get("stale_batch_hours", 2))
    )
    rows = db.query(
        "SELECT status,config_json,truncated_by_call_cap FROM probe_batches WHERE batch_id=?",
        (batch_id,),
    )
    if not rows:
        raise ValueError(f"探测批次不存在：{batch_id}")
    try:
        batch_config = json.loads(rows[0]["config_json"] or "{}")
    except json.JSONDecodeError:
        batch_config = {}
    if batch_config.get("engine_surface", "api") == "browser":
        raise ValueError("这是浏览器探测批次，请使用 browser-probe 恢复")
    if rows[0]["status"] == "running":
        raise ValueError("探测批次仍在运行，不能并发恢复")
    max_attempts = min(10, max(1, int(settings.raw.get("monitor", {}).get("max_item_attempts", 3))))
    cap = min(500, max(1, int(
        max_calls if max_calls is not None
        else settings.raw.get("monitor", {}).get("max_calls_per_run", 30)
    )))
    items = db.query(
        """SELECT * FROM probe_batch_items WHERE batch_id=? AND status IN ('planned','failed')
        AND attempts<? ORDER BY id""",
        (batch_id, max_attempts),
    )
    provider_configs = _provider_config_map(settings)
    items.sort(key=lambda item: (
        0 if (
            provider_configs.get(item["provider"], {}).get("enabled")
            and os.getenv(str(provider_configs.get(item["provider"], {}).get("api_key_env", "")))
        ) else 1,
        item["id"],
    ))
    items = items[:cap]
    config = batch_config
    if items:
        claimed = db.execute(
            """UPDATE probe_batches SET status='running',finished_at=NULL
            WHERE batch_id=? AND status=?""",
            (batch_id, rows[0]["status"]),
        )
        if claimed.rowcount != 1:
            raise ValueError("探测批次状态已变化，拒绝并发恢复")
    original_samples = int(config.get("samples_per_prompt", 1))
    result = _execute_probe_batch_items(settings, db, batch_id, items, original_samples)
    retryable = db.query(
        """SELECT COUNT(*) n FROM probe_batch_items WHERE batch_id=?
        AND status IN ('planned','failed') AND attempts<?""",
        (batch_id, max_attempts),
    )[0]["n"]
    return {
        **result,
        "resumed": True,
        "samples_per_prompt": original_samples,
        "max_calls_per_run": cap,
        "max_item_attempts": max_attempts,
        "retryable_items": retryable,
        "truncated_by_call_cap": bool(rows[0]["truncated_by_call_cap"]),
    }


def run_probes(
    settings: Settings,
    db: Database,
    limit: int | None = None,
    samples_per_prompt: int | None = None,
) -> dict[str, Any]:
    """Run bounded, repeatable API probes without letting one provider abort the batch."""
    recovered_batches = reconcile_stale_probe_batches(
        db, int(settings.raw.get("monitor", {}).get("stale_batch_hours", 2))
    )
    monitor = settings.raw["monitor"]
    count = min(1000, max(1, int(limit if limit is not None else monitor["daily_probe_count"])))
    requested_samples = int(
        samples_per_prompt
        if samples_per_prompt is not None
        else monitor.get("live_samples_per_prompt", 1)
    )
    samples = min(5, max(1, requested_samples))
    max_calls = min(500, max(1, int(monitor.get("max_calls_per_run", 30))))
    if monitor.get("auto_resume_probe_batches", True):
        max_attempts = min(10, max(1, int(monitor.get("max_item_attempts", 3))))
        for candidate in db.query(
            """SELECT b.batch_id FROM probe_batches b
            WHERE b.status IN ('interrupted','partial','failed','waiting_credentials')
            AND CASE WHEN json_valid(b.config_json)
              THEN COALESCE(json_extract(b.config_json,'$.engine_surface'),'api')
              ELSE 'browser' END<>'browser'
            AND EXISTS (
                SELECT 1 FROM probe_batch_items i WHERE i.batch_id=b.batch_id
                AND i.status IN ('planned','failed') AND i.attempts<?
            ) ORDER BY b.batch_id DESC""",
            (max_attempts,),
        ):
            pending_providers = {
                row["provider"] for row in db.query(
                    """SELECT DISTINCT provider FROM probe_batch_items
                    WHERE batch_id=? AND status IN ('planned','failed') AND attempts<?""",
                    (candidate["batch_id"], max_attempts),
                )
            }
            provider_configs = _provider_config_map(settings)
            if not any(
                provider_configs.get(name, {}).get("enabled")
                and os.getenv(str(provider_configs.get(name, {}).get("api_key_env", "")))
                for name in pending_providers
            ):
                continue
            try:
                resumed = resume_probe_batch(settings, db, candidate["batch_id"], max_calls)
            except ValueError as exc:
                message = str(exc)
                if "仍在运行" not in message and "状态已变化" not in message:
                    raise
                return {
                    "batch_id": candidate["batch_id"],
                    "batch_status": "concurrent_resume_skipped",
                    "recovered_stale_batches": recovered_batches,
                    "automated": [],
                    "errors": [],
                    "skipped": [{"provider": "batch", "reason": message}],
                    "calls_attempted": 0,
                    "max_calls_per_run": max_calls,
                    "samples_per_prompt": samples,
                    "truncated_by_call_cap": False,
                    "manual_queue": str(create_probe_queue(settings, db, count)),
                    "resumed": False,
                    "auto_resumed": False,
                }
            resumed.update({
                "recovered_stale_batches": recovered_batches,
                "manual_queue": str(create_probe_queue(settings, db, count)),
                "auto_resumed": True,
            })
            return resumed
    questions = db.query("SELECT question FROM opportunities ORDER BY score DESC,id LIMIT ?", (count,))
    skipped: list[dict[str, str]] = []
    provider_configs = _provider_config_map(settings)
    eligible_providers: list[dict[str, Any]] = []
    for provider_name, provider in provider_configs.items():
        if not provider.get("enabled"):
            skipped.append({"provider": provider_name, "reason": "disabled"})
        elif not os.getenv(str(provider.get("api_key_env", ""))):
            skipped.append({"provider": provider_name, "reason": f"missing_env:{provider.get('api_key_env', '')}"})
        else:
            eligible_providers.append(provider)
    eligible_count = len(eligible_providers)
    theoretical_calls = len(questions) * samples * eligible_count
    planned_calls = min(max_calls, theoretical_calls)
    truncated_by_call_cap = theoretical_calls > max_calls
    batch_id = f"probe-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    db.execute(
        """INSERT INTO probe_batches(
        batch_id,started_at,status,planned_calls,truncated_by_call_cap,config_json
        ) VALUES(?,?,'running',?,?,?)""",
        (
            batch_id,
            now_iso(),
            planned_calls,
            int(truncated_by_call_cap),
            json.dumps(
                {
                    "engine_surface": "api",
                    "prompt_variant": "source_requested",
                    "prompt_version": "source-requested-v1",
                    "question_limit": count,
                    "samples_per_prompt": samples,
                    "max_calls_per_run": max_calls,
                    "eligible_providers": eligible_count,
                },
                ensure_ascii=False,
            ),
        ),
    )
    plan: list[tuple[Any, ...]] = []
    timestamp = now_iso()
    for provider in eligible_providers:
        provider_name = str(provider.get("name", "unknown"))
        for row in questions:
            for sample_index in range(1, samples + 1):
                if len(plan) >= max_calls:
                    break
                plan.append((
                    batch_id, provider_name, row["question"], _probe_prompt(row["question"]),
                    sample_index, timestamp, timestamp,
                ))
            if len(plan) >= max_calls:
                break
        if len(plan) >= max_calls:
            break
    if plan:
        db.executemany(
            """INSERT INTO probe_batch_items(
            batch_id,provider,question,prompt,sample_index,created_at,updated_at,engine_surface,
            prompt_variant,prompt_version) VALUES(?,?,?,?,?,?,?,'api','source_requested','source-requested-v1')""",
            plan,
        )
    items = db.query("SELECT * FROM probe_batch_items WHERE batch_id=? ORDER BY id", (batch_id,))
    execution = _execute_probe_batch_items(settings, db, batch_id, items, requested_samples)
    if skipped:
        combined_skipped = list({
            (item["provider"], item["reason"]): item
            for item in [*execution["skipped"], *skipped]
        }.values())
        db.execute(
            "UPDATE probe_batches SET skipped_json=? WHERE batch_id=?",
            (json.dumps(combined_skipped, ensure_ascii=False), batch_id),
        )
        execution["skipped"] = combined_skipped
    queue_path = create_probe_queue(settings, db, count)
    return {
        **execution,
        "recovered_stale_batches": recovered_batches,
        "max_calls_per_run": max_calls,
        "samples_per_prompt": samples,
        "truncated_by_call_cap": truncated_by_call_cap,
        "manual_queue": str(queue_path),
        "resumed": False,
        "auto_resumed": False,
    }


def run_automation_pipeline(settings: Settings, kind: str) -> dict[str, object]:
    """Run the single authoritative daily/weekly workflow with explicit safety gates."""
    if kind not in {"daily", "weekly"}:
        raise ValueError(f"未知自动化流程类型：{kind}")
    from .browser import run_browser_probes

    with Database(settings.db_path) as db:
        run_id = db.start_run(kind)
        if run_id == 0:
            return {"status": "skipped", "reason": f"{kind} 今日已经运行或正在运行"}
        try:
            detail: dict[str, object] = {"seed": seed_opportunities(settings, db)}
            detail["crawl"] = (
                crawl_site(settings, db)
                if settings.site_url
                else {"status": "skipped", "reason": "官网在 ICP 办理期间不参与"}
            )
            detail["facts"] = extract_official_facts(settings, db)
            detail["entity_assets"] = build_entity_assets(settings)
            detail["benchmark"] = str(build_prompt_benchmark(settings, db))

            content_paused = bool(settings.raw.get("content", {}).get("generation_paused", False))
            if content_paused:
                detail["generate"] = {"status": "paused", "created": 0}
                detail["quality"] = {"status": "paused", "reviewed": 0}
                detail["schedule"] = {"status": "paused", "created": 0}
            else:
                detail["generate"] = generate_drafts(settings, db, 2 if kind == "daily" else 8)
                detail["quality"] = review_drafts(settings, db)
                detail["schedule"] = schedule_connected_drafts(settings, db)

            monitor = settings.raw.get("monitor", {})
            detail["probes"] = (
                run_probes(settings, db, 10 if kind == "daily" else 30)
                if monitor.get("api_probes_enabled", False)
                else {"status": "disabled", "reason": "API 探测已关闭"}
            )
            detail["browser_probes"] = (
                run_browser_probes(settings, 5 if kind == "daily" else 10)
                if settings.raw.get("autopilot", {}).get("browser_probes_enabled", False)
                else {"status": "disabled", "reason": "浏览器探测已关闭"}
            )
            detail["citation_urls"] = build_citation_url_ledger(
                settings, db, verify_network=(kind == "weekly")
            )
            detail["strategy"] = str(build_strategy_snapshot(settings, db))
            detail["report"] = str(build_report(settings, db))
            db.finish_run(run_id, "ok", detail)
            return detail
        except Exception as exc:
            db.finish_run(run_id, "failed", {"error": str(exc)})
            raise


def build_report(settings: Settings, db: Database) -> Path:
    from .attribution import build_attribution_report
    from .attribution_ingest import build_attribution_ingest_readiness
    from .probe_readiness import build_ai_probe_readiness

    today = datetime.now().date().isoformat()
    pages = db.query("SELECT url,title,audit_json FROM pages ORDER BY url")
    drafts = db.query("SELECT id,title,status,quality_score FROM drafts ORDER BY id DESC")
    opps = db.query("SELECT question,cluster,score,status FROM opportunities ORDER BY score DESC LIMIT 20")
    visibility = build_visibility_snapshot(settings, db)
    answer_integrity = build_answer_integrity_snapshot(settings, db)
    citation_intelligence = build_citation_intelligence(settings, db)
    citation_urls = build_citation_url_ledger(settings, db)
    attribution = build_attribution_report(settings, db)
    attribution_ingest = build_attribution_ingest_readiness(settings)
    evidence_audit = audit_brand_facts(settings)
    ai_readiness = build_ai_probe_readiness(settings, db)
    gap_analysis = build_gap_analysis(settings, db, persist=False)
    page_scores = [json.loads(row["audit_json"]).get("score", 0) for row in pages]
    report = [
        f"# {settings.brand['name']} GEO 运行报告｜{today}", "",
        "## 执行摘要", "",
        f"- 已审计页面：{len(pages)}",
        f"- 站点平均 GEO 分：{round(sum(page_scores)/len(page_scores), 1) if page_scores else '官网暂不参与'}",
        f"- 问题库：{db.query('SELECT COUNT(*) n FROM opportunities')[0]['n']} 条",
        f"- 内容草稿：{len(drafts)} 篇",
        f"- 可直接发布：{sum(1 for row in drafts if row['status'] == 'approved')} 篇",
        f"- 有效品牌事实：{evidence_audit['valid_count']}/{evidence_audit['fact_count']} 条",
        "", "## 当前阻塞项", "",
    ]
    if not settings.site_url and settings.raw.get("evidence", {}).get("mode") != "product_repository":
        report.append("- 尚未填写官网地址或配置第一方产品资料源。")
    elif not settings.site_url:
        report.append("- 官网在 ICP 办理期间不参与；品牌事实使用第一方产品资料，公开引用使用权威采购信息源。")
    if not [fact for fact in _verified_facts(settings) if _fact_evidence_valid(fact, settings)]:
        report.append("- 尚无已验证品牌事实，系统已阻止占位稿自动发布。")
    if evidence_audit["invalid_count"]:
        report.append(
            "- 存在无效、过期或来源已漂移的品牌事实："
            + "、".join(f"{status}={count}" for status, count in evidence_audit["status_counts"].items() if status != "verified_current")
        )
    report.extend(["", "## 品牌事实证据", ""])
    report.append(
        f"- 当前有效 {evidence_audit['valid_count']} 条｜无效 {evidence_audit['invalid_count']} 条｜"
        "核验同时要求来源边界、行号、摘要指纹、支持词、完整主张映射和核验日期。"
    )
    for receipt in evidence_audit["receipts"]:
        report.append(
            f"- {receipt['status']}｜{receipt['claim']}｜{receipt['source']}｜"
            f"支持词 {receipt.get('matched_support_term_count', 0)}/{receipt.get('support_term_count', 0)}｜"
            f"主张映射 {receipt.get('claim_assertion_count', 0)} 段/"
            f"{'完整' if receipt.get('claim_map_covers_full_claim') else '不完整'}｜"
            f"核验日期 {receipt.get('verified_at') or '未填写'}"
        )
    report.extend(["", "## 优先问题", ""])
    for row in opps[:10]:
        report.append(f"- {row['question']}（{row['cluster']}，机会分 {row['score']}）")
    report.extend(["", "## GEO 聚焦行动", ""])
    report.append(
        f"- 聚焦 {gap_analysis['focus_count']} 项｜全量 {gap_analysis['backlog_summary']['total_actions']} 项｜"
        f"延后 {gap_analysis['backlog_summary']['deferred_actions']} 项｜"
        f"其中延后未采样 {gap_analysis['backlog_summary']['deferred_unmeasured']} 项"
    )
    for action in gap_analysis["focus_actions"]:
        report.append(
            f"- P{action['priority']}｜{action['tier']}｜{action['question']}｜{action['type']}｜"
            f"{action['confidence']}｜{action['recommended_next_step']}"
        )
    report.extend(["", "## 内容队列", ""])
    for row in drafts[:10]:
        report.append(f"- #{row['id']} {row['title']}｜{row['status']}｜质量分 {row['quality_score'] or 0}")
    report.extend(["", "## AI 引用监测", ""])
    report.append(
        f"- 品牌回答真实性警戒：{answer_integrity['status']}｜已审计品牌样本 "
        f"{answer_integrity['audited_brand_samples']}｜需复核 {answer_integrity['flagged_samples']}｜"
        f"主口径风险率 "
        f"{str(answer_integrity['primary_flag_rate']) + '%' if answer_integrity['primary_flag_rate'] is not None else '暂无'}"
    )
    for issue, count in answer_integrity["issue_counts"].items():
        report.append(f"- 回答风险类型：{issue}｜{count} 个样本")
    report.append(
        f"- AI 适配器就绪：{ai_readiness['status']}｜配置 {ai_readiness['configured_providers']}｜"
        f"有效 {ai_readiness['valid_adapters']}｜已声明登录 {ai_readiness['declared_connected']}｜"
        f"可尝试采样 {ai_readiness['ready_for_attempt']}/{ai_readiness['minimum_providers']}｜"
        f"缺口 {ai_readiness['provider_deficit']}"
    )
    for adapter_error in ai_readiness["errors"]:
        report.append(f"- AI 适配器错误：{adapter_error}")
    for provider in ai_readiness["providers"]:
        if provider.get("retry_after"):
            report.append(
                f"- AI 自动重试：{provider['name']}｜账号状态 {provider['account_status']}｜"
                f"重试时间 {provider['retry_after']}"
            )
        elif provider["account_status"] != "connected":
            report.append(
                f"- AI 首次登录待办：{provider['name']}｜账号状态 {provider['account_status']}｜"
                "完成一次注册/验证/登录后，后续采样由系统自动执行"
            )
    sampling = visibility["sampling_health"]
    isolation = visibility["context_isolation"]
    provenance = visibility["probe_provenance"]
    browser_activity = visibility["browser_activity"]
    cross_run = visibility["cross_run_reproducibility"]
    trends = visibility["trends"]
    report.append(
        f"- 采样健康：{sampling['status']}｜重复组 {sampling['repeated_groups']}｜"
        f"达到目标组 {sampling['target_reached_groups']}｜平均结果一致率 "
        f"{sampling['average_outcome_agreement'] if sampling['average_outcome_agreement'] is not None else '暂无'}"
    )
    report.append(
        f"- 自然原问主口径：{sampling['primary_status']}｜样本 {sampling['primary_samples']}｜"
        f"重复组 {sampling['primary_repeated_groups']}｜达标组 {sampling['primary_target_reached_groups']}｜"
        f"核心问题覆盖 {sampling['priority_question_coverage']['repeat_ready_questions']}/"
        f"{sampling['priority_question_coverage']['question_count']}｜独立引擎 "
        f"{sampling['primary_engine_coverage']['repeat_ready_provider_count']}/"
        f"{sampling['primary_engine_coverage']['minimum_providers']}"
    )
    report.extend(f"- 采样警告：{warning}" for warning in sampling["warnings"])
    report.append(
        f"- 浏览器会话隔离：{isolation['status']}｜浏览器样本 {isolation['browser_samples']}｜"
        f"发送前已验证为空 {isolation['fresh_context_verified_samples']}｜"
        f"历史未验证 {isolation['legacy_unverified_samples']}｜隔离失败 {isolation['isolation_failures']}｜"
        f"元数据异常 {isolation['malformed_metadata_samples']}"
    )
    report.append(
        f"- 采样溯源回执：{provenance['status']}｜全部样本 {provenance['samples']}｜"
        f"完整回执 {provenance['complete_receipts']}｜历史缺失 {provenance['legacy_without_receipt']}｜"
        f"元数据异常 {provenance['malformed_metadata_samples']}｜"
        f"适配器指纹 {provenance['adapter_fingerprint_count']}"
    )
    report.append(
        f"- 浏览器启动审计：{browser_activity['status']}｜记录 {browser_activity['recorded_launches']}｜"
        f"用户明确可见启动 {browser_activity['visible_user_launches']}｜"
        f"自动化可见启动 {browser_activity['visible_automation_launches']}"
    )
    report.append(
        f"- 跨批次复现性：{cross_run['status']}｜协议组 {cross_run['protocol_count']}｜"
        f"达标运行批次 {cross_run['eligible_experiments']}｜可比较 {cross_run['comparable_protocols']}｜"
        f"稳定 {cross_run['stable_protocols']}｜波动 {cross_run['variable_protocols']}｜"
        f"跨度阈值 {cross_run['variability_threshold_percentage_points']} 个百分点"
    )
    report.append(
        f"- 引用来源情报：{citation_intelligence['source_count']} 个域名｜"
        f"来源候选达复核门槛 {citation_intelligence['review_candidate_count']} 个｜"
        f"跨引擎观察 {citation_intelligence['cross_engine_count']} 个"
    )
    report.append(
        f"- 具体引用链接：{citation_urls['candidate_count']} 条｜可核验 "
        f"{citation_urls['verification_eligible_count']} 条｜公网校验通过 "
        f"{citation_urls['verified_count']} 条｜待域名复核 {citation_urls['needs_domain_review_count']} 条"
    )
    for source in citation_intelligence["sources"][:10]:
        report.append(
            f"- 来源候选 {source['domain']}｜{source['category']}｜{source['evidence_strength']}｜"
            f"原始样本 {source['sample_count']}｜独立观察 {source['independent_count']}｜"
            f"引擎 {source['provider_count']}｜引擎/surface 组合 {source['surface_count']}｜"
            f"问题 {source['question_count']}"
        )
    report.append(
        f"- 趋势比较：{trends['status']}｜窗口 {trends['window_days']} 天｜"
        f"当前 {trends['current_period']['samples']} 个样本｜前期 {trends['previous_period']['samples']} 个样本｜"
        f"匹配问题 {trends['primary_matched_panel']['matched_question_count']} 个｜"
        f"匹配面板每侧 {trends['primary_matched_panel']['paired_samples_per_window']} 个样本｜"
        f"门槛 {trends['minimum_matched_questions']} 个相同问题且每侧 "
        f"{trends['minimum_samples_per_window']} 个样本"
    )
    if visibility["recent_batches"]:
        latest_batch = visibility["recent_batches"][0]
        report.append(
            f"- 最近探测批次：{latest_batch['batch_id']}｜{latest_batch['status']}｜"
            f"清单完成 {latest_batch['item_succeeded']}/{latest_batch['item_total']}｜"
            f"失败 {latest_batch['item_failed']}｜待执行 {latest_batch['item_pending']}"
        )
    for comparison in trends["comparisons"]:
        if comparison["status"] != "comparison_ready":
            continue
        deltas = comparison["deltas_percentage_points"]
        report.append(
            f"- {comparison['provider']}（{comparison['engine_surface']} / {comparison['prompt_variant']} / "
            f"{comparison['prompt_version']} / {comparison['locale']}-{comparison['region']}）窗口变化："
            f"匹配问题 {comparison['matched_panel']['matched_question_count']}｜"
            f"面板每侧 {comparison['matched_panel']['paired_samples_per_window']} 个样本｜"
            f"提及 {deltas['mention_rate']:+.1f} 个百分点｜推荐 {deltas['recommendation_rate']:+.1f} 个百分点｜"
            f"引用 {deltas['owned_citation_rate']:+.1f} 个百分点｜"
            f"区间分离信号 {','.join(comparison['non_overlapping_ci_signals']) or '无'}"
        )
    if visibility["providers"]:
        for row in visibility["providers"]:
            report.append(
                f"- {row['provider']}（{row['engine_surface']} / {row['prompt_variant']} / {row['prompt_version']}）："
                f"提及率 {row['mention_rate']}%｜推荐率 {row['recommendation_rate']}%｜"
                f"自有域名引用率 {row['owned_citation_rate']}%｜平均可见度 {row['average_visibility_score']}｜样本 {row['samples']}"
            )
        if visibility["citation_domains"]:
            report.extend(["", "### AI 常引用的来源域名", ""])
            report.extend(
                f"- {row['domain']}：{row['count']} 次" for row in visibility["citation_domains"][:10]
            )
        report.append(
            f"- 已核实竞品观察份额：{visibility['tracked_share_of_voice'] if visibility['tracked_share_of_voice'] is not None else '样本不足'}｜"
            f"跟踪名称提及 {visibility['tracked_name_mentions']}/"
            f"{visibility['tracked_share_of_voice_min_mentions']} 门槛"
            "（只基于当前样本提及，不代表市场份额）"
        )
        for competitor in visibility.get("competitor_visibility", [])[:10]:
            report.append(
                f"- 竞品观察 {competitor['name']}｜提及样本 {competitor['mentions']}｜"
                f"样本提及率 {competitor['sample_mention_rate']}%｜问题 {competitor['question_count']}｜"
                f"引擎 {competitor['provider_count']}｜与品牌同现 {competitor['brand_co_mentions']}"
            )
    else:
        report.append("- 尚无自动探测数据，已生成跨平台人工探测队列。")
    quality = attribution["data_quality"]
    model = attribution["attribution_models"]
    delay = attribution["time_to_conversion_days"]
    report.extend([
        "", "## 获客归因质量", "",
        f"- 接入安全：{attribution_ingest['status']}｜HMAC 校验 "
        f"{'已强制' if attribution_ingest['auth_required'] else '未强制'}｜"
        f"密钥 {'已就绪' if attribution_ingest['secret_strong_enough'] else '未就绪'}｜"
        f"接入契约 {'已生成' if attribution_ingest['contract_ready'] else '缺失'}",
        f"- 接入下一步：{attribution_ingest['next_action']}",
        f"- 状态：{quality['status']}｜原始事件 {attribution['events']}｜有效事件 {attribution['valid_events']}｜"
        f"可归因主体 {attribution['unique_attributed_subjects']}",
        f"- 可归因漏斗：AI 访问 {attribution['attributable_funnel']['ai_referral']}｜"
        f"咨询 {attribution['attributable_funnel']['inquiry']}｜成交 {attribution['attributable_funnel']['won']}",
        f"- 数据缺口：缺匿名标识事件 {quality['anonymous_missing_events']}｜"
        f"孤立下游主体 {quality['orphan_downstream_subjects']}｜重复阶段事件 {quality['duplicate_stage_events']}｜"
        f"阶段倒序主体 {quality['out_of_order_subjects']}｜非法事件类型 {quality['invalid_event_type_events']}｜"
        f"非法时间事件 {quality['invalid_timestamp_events']}｜非法金额事件 {quality['invalid_value_events']}｜"
        f"未来时间事件 {quality['future_events']}",
        f"- 规则归因：{model['lookback_days']} 天回溯；首次触点与末次触点并列输出；"
        f"咨询中位耗时 {str(delay['median_to_inquiry']) + ' 天' if delay['median_to_inquiry'] is not None else '暂无'}；"
        f"成交中位耗时 {str(delay['median_to_won']) + ' 天' if delay['median_to_won'] is not None else '暂无'}",
        f"- 边界：{attribution['measurement_note']}",
    ])
    path = settings.root / "reports" / f"geo-report-{today}.md"
    path.write_text("\n".join(report) + "\n", encoding="utf-8")
    return path


def schedule_approved_drafts(settings: Settings, db: Database, platforms: list[str]) -> dict[str, Any]:
    if settings.raw.get("publishing", {}).get("paused", False):
        return {"created": 0, "status": "paused"}
    drafts = db.query("SELECT id FROM drafts WHERE status='approved' ORDER BY id")
    created = 0
    start = datetime.now(UTC) + timedelta(minutes=10)
    for index, row in enumerate(drafts):
        for p_index, platform in enumerate(platforms):
            scheduled = start + timedelta(hours=index * 8 + p_index * 2)
            cur = db.execute(
                """INSERT OR IGNORE INTO publish_jobs(draft_id,platform,scheduled_at,status,created_at,updated_at)
                VALUES(?,?,?,'pending',?,?)""",
                (row["id"], platform, scheduled.replace(microsecond=0).isoformat(), now_iso(), now_iso()),
            )
            created += cur.rowcount
    return {"created": created}


def schedule_connected_drafts(settings: Settings, db: Database) -> dict[str, Any]:
    if settings.raw.get("publishing", {}).get("paused", False):
        return {"created": 0, "status": "paused"}
    if not settings.raw.get("autopilot", {}).get("auto_schedule_connected_platforms", True):
        return {"created": 0, "status": "disabled"}
    specs = json.loads((settings.root / "config" / "platforms.json").read_text(encoding="utf-8"))
    platforms = [
        row["platform"] for row in db.query("SELECT platform FROM platform_accounts WHERE status='connected'")
        if specs.get(row["platform"], {}).get("kind", "publisher") == "publisher"
    ]
    if not platforms:
        return {"created": 0, "status": "waiting_for_account_login"}
    return {**schedule_approved_drafts(settings, db, platforms), "status": "ok", "platforms": platforms}
