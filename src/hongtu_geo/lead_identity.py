from __future__ import annotations

import re

from .core import Settings


def verified_lead_identity(settings: Settings) -> dict[str, str] | None:
    """Return a publishable company/phone identity only when every evidence gate passes."""
    lead = settings.raw.get("geo_goals", {}).get("industry_leads", {})
    company = str(lead.get("public_company_name", "")).strip()
    phone = re.sub(r"[\s-]+", "", str(lead.get("public_business_phone", "")))
    source = str(lead.get("evidence_source", "")).strip()
    consent = bool(lead.get("publication_consent", False))
    phone_valid = bool(re.fullmatch(r"(?:400\d{7}|0\d{9,11}|1[3-9]\d{9})", phone))
    source_valid = source.startswith(("https://", "http://", "repo://"))
    if lead.get("verified") is True and consent and company and phone_valid and source_valid:
        return {"company": company, "phone": phone, "source": source}
    return None
