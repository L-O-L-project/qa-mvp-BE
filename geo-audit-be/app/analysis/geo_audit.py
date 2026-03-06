from __future__ import annotations

import json
import re
from typing import Any, Dict, List
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

SCHEMA_TARGETS = {"Organization", "WebSite", "Article", "FAQPage", "BreadcrumbList"}


def _safe_text(soup: BeautifulSoup) -> str:
    return " ".join(soup.stripped_strings)


async def check_file_presence(origin: str, client: httpx.AsyncClient) -> Dict[str, bool]:
    checks = {
        "llms_txt": "/llms.txt",
        "ai_txt": "/ai.txt",
        "robots_txt": "/robots.txt",
        "sitemap": "/sitemap.xml",
    }
    out: Dict[str, bool] = {}
    for key, path in checks.items():
        exists = False
        try:
            response = await client.get(urljoin(origin + "/", path))
            exists = response.status_code < 400
        except Exception:
            exists = False
        out[key] = exists
    return out


def analyze_meta_tags(soup: BeautifulSoup) -> Dict[str, bool]:
    def has_meta(name: str | None = None, prop: str | None = None) -> bool:
        query: Dict[str, str] = {}
        if name:
            query["name"] = name
        if prop:
            query["property"] = prop
        el = soup.find("meta", attrs=query)
        return bool(el and str(el.get("content") or "").strip())

    canonical = soup.find("link", attrs={"rel": "canonical"})
    og_title = has_meta(prop="og:title")
    og_description = has_meta(prop="og:description")
    og_image = has_meta(prop="og:image")

    return {
        "title": bool(soup.title and (soup.title.text or "").strip()),
        "meta_description": has_meta(name="description"),
        "og_title": og_title,
        "og_description": og_description,
        "og_image": og_image,
        "og_tags": og_title and og_description and og_image,
        "canonical": bool(canonical and canonical.get("href")),
    }


def analyze_heading_structure(soup: BeautifulSoup) -> Dict[str, Any]:
    h1_tags = soup.find_all("h1")
    h2_count = len(soup.find_all("h2"))
    h3_count = len(soup.find_all("h3"))
    hierarchy_valid = h2_count > 0 or h3_count == 0

    return {
        "h1_present": len(h1_tags) > 0,
        "h1_unique": len(h1_tags) == 1,
        "h2_count": h2_count,
        "h3_count": h3_count,
        "h2_h3_hierarchy": hierarchy_valid,
    }


def detect_structured_data(soup: BeautifulSoup) -> List[str]:
    detected: set[str] = set()

    for block in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = (block.string or block.get_text() or "").strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue

        stack = [payload]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                t = node.get("@type")
                if isinstance(t, list):
                    for item in t:
                        if isinstance(item, str) and item in SCHEMA_TARGETS:
                            detected.add(item)
                elif isinstance(t, str) and t in SCHEMA_TARGETS:
                    detected.add(t)
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)

    return sorted(detected)


def detect_faq(soup: BeautifulSoup) -> bool:
    faq_blocks = soup.select("[class*='faq' i], [id*='faq' i], details")
    if faq_blocks:
        return True

    text = _safe_text(soup).lower()
    patterns = [r"\bwhat\b", r"\bhow\b", r"\bwhy\b", r"\bcan\b", r"\bshould\b"]
    hits = sum(1 for p in patterns if re.search(p, text))
    return hits >= 3


def extract_entities(soup: BeautifulSoup, page_url: str) -> Dict[str, Any]:
    text = _safe_text(soup)
    lowered = text.lower()

    company_name = None
    og_site = soup.find("meta", attrs={"property": "og:site_name"})
    if og_site and og_site.get("content"):
        company_name = str(og_site.get("content")).strip()
    elif soup.title and soup.title.text:
        company_name = soup.title.text.strip().split("|")[0].strip()

    service_name = None
    for tag in soup.find_all(["h1", "h2"], limit=5):
        value = (tag.get_text() or "").strip()
        if len(value) >= 4:
            service_name = value
            break

    emails = sorted(set(re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)))
    phones = sorted(set(re.findall(r"(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{2,4}\)?[\s.-]?)\d{3,4}[\s.-]?\d{4}", text)))

    location = None
    location_patterns = [r"\b[a-zA-Z\s]+,\s?[A-Z]{2}\b", r"\b[a-zA-Z\s]+,\s?(?:UK|USA|Korea|Japan|Germany|France)\b"]
    for pattern in location_patterns:
        match = re.search(pattern, text)
        if match:
            location = match.group(0).strip()
            break

    return {
        "company_name": company_name,
        "service_name": service_name,
        "contact_information": {"emails": emails, "phones": phones},
        "location": location,
        "entity_clarity": bool(company_name) and bool(service_name) and (bool(emails) or bool(phones) or "contact" in lowered),
        "page_url": page_url,
    }


def score_geo(results: Dict[str, Any]) -> int:
    file_presence = results["file_presence"]
    meta = results["meta"]
    headings = results["headings"]
    structured_data = results["structured_data"]
    faq_detected = results["faq_detected"]
    entities = results["entities"]

    score = 0.0

    score += 20 * (sum(1 for ok in file_presence.values() if ok) / max(len(file_presence), 1))

    meta_keys = ["title", "meta_description", "og_title", "og_description", "og_image", "canonical"]
    score += 20 * (sum(1 for key in meta_keys if meta.get(key)) / len(meta_keys))

    heading_checks = [headings.get("h1_present"), headings.get("h1_unique"), headings.get("h2_h3_hierarchy")]
    score += 15 * (sum(1 for x in heading_checks if x) / len(heading_checks))

    schema_points = min(len(structured_data), 3) / 3
    score += 20 * schema_points

    score += 10 if faq_detected else 0

    score += 15 if entities.get("entity_clarity") else 0

    return max(0, min(100, round(score)))


def build_recommendations(results: Dict[str, Any]) -> List[str]:
    recs: List[str] = []

    if not results["file_presence"].get("llms_txt"):
        recs.append("Add llms.txt to guide AI crawlers")
    if not results["faq_detected"]:
        recs.append("Add FAQ section with 3–5 questions")
    if not results["structured_data"]:
        recs.append("Add Organization schema")
    elif "FAQPage" not in results["structured_data"]:
        recs.append("Add FAQPage schema")
    if not results["entities"].get("entity_clarity"):
        recs.append("Improve service description clarity")
    if not results["meta"].get("meta_description"):
        recs.append("Add a descriptive meta description for better AI snippet quality")

    return recs
