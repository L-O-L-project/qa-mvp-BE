from __future__ import annotations

from typing import Any, Dict

import httpx
from bs4 import BeautifulSoup

from app.analysis.geo_audit import (
    analyze_heading_structure,
    analyze_meta_tags,
    build_recommendations,
    check_file_presence,
    detect_faq,
    detect_structured_data,
    extract_entities,
    score_geo,
)
from app.crawler import CrawledPage, crawl_site


def _aggregate_page_results(pages: list[CrawledPage]) -> Dict[str, Any]:
    meta_aggregate = {
        "title": False,
        "meta_description": False,
        "og_title": False,
        "og_description": False,
        "og_image": False,
        "og_tags": False,
        "canonical": False,
    }
    headings_aggregate = {
        "h1_present": False,
        "h1_unique": True,
        "h2_h3_hierarchy": True,
    }
    structured_data: set[str] = set()
    faq_detected = False
    entity_candidate: Dict[str, Any] | None = None

    for page in pages:
        soup = BeautifulSoup(page.html, "html.parser")

        meta = analyze_meta_tags(soup)
        for key in meta_aggregate:
            meta_aggregate[key] = bool(meta_aggregate[key] or meta.get(key, False))

        headings = analyze_heading_structure(soup)
        headings_aggregate["h1_present"] = bool(headings_aggregate["h1_present"] or headings.get("h1_present", False))
        headings_aggregate["h1_unique"] = bool(headings_aggregate["h1_unique"] and headings.get("h1_unique", False))
        headings_aggregate["h2_h3_hierarchy"] = bool(
            headings_aggregate["h2_h3_hierarchy"] and headings.get("h2_h3_hierarchy", False)
        )

        structured_data.update(detect_structured_data(soup))
        faq_detected = bool(faq_detected or detect_faq(soup))

        entities = extract_entities(soup, page.url)
        if entities.get("entity_clarity"):
            entity_candidate = entities
            break
        if entity_candidate is None:
            entity_candidate = entities

    if entity_candidate is None:
        entity_candidate = {
            "company_name": None,
            "service_name": None,
            "contact_information": {"emails": [], "phones": []},
            "location": None,
            "entity_clarity": False,
            "page_url": pages[0].url if pages else "",
        }

    meta_aggregate["og_tags"] = bool(
        meta_aggregate["og_title"] and meta_aggregate["og_description"] and meta_aggregate["og_image"]
    )

    return {
        "meta": meta_aggregate,
        "headings": headings_aggregate,
        "structured_data": sorted(structured_data),
        "faq_detected": faq_detected,
        "entities": entity_candidate,
    }


async def run_geo_audit(url: str) -> Dict[str, Any]:
    crawl_result = await crawl_site(url)

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        file_presence = await check_file_presence(crawl_result["origin"], client)

    aggregated = _aggregate_page_results(crawl_result["pages"])
    results = {
        "file_presence": file_presence,
        **aggregated,
    }

    geo_score = score_geo(results)
    recommendations = build_recommendations(results)

    checks = {
        **results["file_presence"],
        "title": results["meta"]["title"],
        "meta_description": results["meta"]["meta_description"],
        "og_tags": results["meta"]["og_tags"],
        "faq_detected": results["faq_detected"],
        "structured_data": results["structured_data"],
    }

    return {
        "url": crawl_result["target"],
        "geo_score": geo_score,
        "checks": checks,
        "structured_data": results["structured_data"],
        "recommendations": recommendations,
    }
