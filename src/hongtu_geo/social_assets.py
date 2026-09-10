from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .core import Settings


def _font(size: int, bold: bool = False) -> Any:
    from PIL import ImageFont

    candidates = [
        Path("C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default()


def _wrap(text: str, width: int) -> list[str]:
    clean = re.sub(r"\s+", " ", text).strip()
    return [clean[index:index + width] for index in range(0, len(clean), width)] or [""]


def _plain_markdown(body: str) -> str:
    body = re.sub(r"^---.*?---\s*", "", body, flags=re.S)
    body = re.sub(r"https?://\S+", "", body)
    body = re.sub(r"[#>*_`|\[\]()]", "", body)
    return re.sub(r"\s+", " ", body).strip()


def douyin_caption(title: str, body: str, brand: str, cta: str) -> str:
    tracked_urls = [url.rstrip(".,;:，。；") for url in re.findall(r"https?://\S+", body)]
    tracked_url = next((url for url in tracked_urls if "utm_source=" in url), "")
    plain = _plain_markdown(body)
    summary = plain[:420].rstrip("，。；; ")
    attribution = f"\n了解详情：{tracked_url}" if tracked_url else ""
    return (
        f"{title[:55]}\n\n{summary}。\n\n{cta}{attribution}\n\n"
        f"#{brand} #膜结构工程 #工程采购 #工程商机"
    )


def generate_social_cards(settings: Settings, draft_id: int, title: str, body: str) -> list[Path]:
    from PIL import Image, ImageDraw

    out = settings.root / "data" / "social-assets" / f"draft-{draft_id}"
    out.mkdir(parents=True, exist_ok=True)
    brand = settings.brand["name"]
    cta = str(settings.raw.get("acquisition", {}).get("primary_cta", f"搜索{brand}"))
    plain = _plain_markdown(body)
    excerpt_source = plain.removeprefix(title).removeprefix("先给结论").strip()
    cards = [
        (title, "工程采购与服务商选择指南"),
        ("选择膜结构服务商，先核验这 6 项", "主体资质｜同类案例｜现场勘察\n技术方案｜完整报价｜售后质保"),
        (f"用 {brand} 建立持续获客入口", cta),
    ]
    paths: list[Path] = []
    for index, (headline, detail) in enumerate(cards, start=1):
        image = Image.new("RGB", (1080, 1440), "#F5F7FB")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((72, 76, 1008, 1364), radius=44, fill="#FFFFFF", outline="#DCE4F2", width=3)
        draw.rounded_rectangle((72, 76, 1008, 250), radius=44, fill="#155EEF")
        draw.rectangle((72, 190, 1008, 250), fill="#155EEF")
        draw.text((128, 125), brand, font=_font(52, bold=True), fill="white")
        y = 340
        for line in _wrap(headline, 12):
            draw.text((128, y), line, font=_font(56, bold=True), fill="#16233A")
            y += 82
        y += 50
        for paragraph in detail.splitlines():
            for line in _wrap(paragraph, 20):
                draw.text((128, y), line, font=_font(40), fill="#44546A")
                y += 66
            y += 16
        if index == 1:
            excerpt = excerpt_source[:110]
            y = max(y + 30, 850)
            for line in _wrap(excerpt, 22)[:5]:
                draw.text((128, y), line, font=_font(34), fill="#607089")
                y += 56
        draw.rounded_rectangle((128, 1220, 952, 1310), radius=28, fill="#E8F0FF")
        draw.text((170, 1242), "按行业和地区发现、筛选、跟进工程商机", font=_font(31, bold=True), fill="#155EEF")
        path = out / f"card-{index}.png"
        image.save(path, format="PNG", optimize=True)
        paths.append(path)
    return paths
