from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

try:
    from playwright.async_api import async_playwright
except Exception:  # pragma: no cover
    async_playwright = None


SCHEMA_TARGETS = {"Organization", "WebSite", "Article", "FAQPage", "BreadcrumbList"}


@dataclass
class CrawledPage:
    url: str
    path: str
    depth: int
    html: str
    status_code: int


def _normalize_target(url: str) -> str:
    candidate = (url or "").strip()
    if not candidate:
        raise ValueError("url is required")
    if not candidate.startswith(("http://", "https://")):
        candidate = f"https://{candidate}"
    return candidate


def _normalize_path(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return path


def _extract_paths_from_source(html: str) -> list[str]:
    found: set[str] = set()
    for match in re.findall(r"(?:router\.push|navigate|href|to)\(\s*['\"](/[^'\"#?\s]{1,120})['\"]\s*\)", html):
        found.add(match)
    for match in re.findall(r"['\"](/(?:[a-zA-Z0-9_\-]+/){0,4}[a-zA-Z0-9_\-]+)['\"]", html):
        if len(match) > 1:
            found.add(match)
    return sorted(found)[:120]


async def _extract_paths_dynamic(url: str, origin: str) -> list[str]:
    if async_playwright is None:
        return []

    discovered: set[str] = set()
    try:
        async with async_playwright() as playwright:  # type: ignore[misc]
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)

            hrefs = await page.eval_on_selector_all(
                "a[href]",
                "els => els.map(e => e.getAttribute('href')).filter(Boolean)",
            )
            for href in hrefs or []:
                absolute = urljoin(url, str(href))
                if absolute.startswith(origin):
                    discovered.add(_normalize_path(absolute))

            html = await page.content()
            for path in _extract_paths_from_source(html):
                discovered.add(path)

            await context.close()
            await browser.close()
    except Exception:
        return []

    return sorted([path for path in discovered if path.startswith("/")])[:150]


async def _crawl_site(url: str, max_pages: int = 10, max_depth: int = 2) -> Dict[str, Any]:
    target = _normalize_target(url)
    parsed = urlparse(target)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    verify_tls = os.getenv("QA_HTTP_VERIFY_TLS", "false").lower() in {"1", "true", "yes", "on"}

    queue: list[tuple[str, int]] = [(target, 0)]
    visited: set[str] = set()
    pages: list[CrawledPage] = []

    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, verify=verify_tls) as client:
        while queue and len(pages) < max_pages:
            next_url, depth = queue.pop(0)
            if next_url in visited:
                continue
            visited.add(next_url)

            try:
                response = await client.get(next_url)
            except Exception:
                continue

            html = response.text or ""
            pages.append(
                CrawledPage(
                    url=str(response.url),
                    path=_normalize_path(str(response.url)),
                    depth=depth,
                    html=html,
                    status_code=response.status_code,
                )
            )

            soup = BeautifulSoup(html, "html.parser")
            for anchor in soup.select("a[href]"):
                href = str(anchor.get("href") or "").strip()
                if not href or href.startswith(("#", "javascript:")):
                    continue
                absolute = urljoin(str(response.url), href)
                if not absolute.startswith(origin):
                    continue
                if depth < max_depth and absolute not in visited and all(q[0] != absolute for q in queue):
                    queue.append((absolute, depth + 1))

            for source_path in _extract_paths_from_source(html):
                absolute = urljoin(origin + "/", source_path)
                if depth < max_depth and absolute not in visited and all(q[0] != absolute for q in queue):
                    queue.append((absolute, depth + 1))

            if depth == 0 and os.getenv("QA_GEO_DYNAMIC", "false").lower() in {"1", "true", "yes", "on"}:
                for dynamic_path in await _extract_paths_dynamic(str(response.url), origin):
                    absolute = urljoin(origin + "/", dynamic_path)
                    if depth < max_depth and absolute not in visited and all(q[0] != absolute for q in queue):
                        queue.append((absolute, depth + 1))

    if not pages:
        raise RuntimeError("no pages crawled")

    return {"origin": origin, "target": target, "pages": pages}


def _analyze_meta_tags(soup: BeautifulSoup) -> Dict[str, bool]:
    def has_meta(name: str | None = None, prop: str | None = None) -> bool:
        attrs: Dict[str, str] = {}
        if name:
            attrs["name"] = name
        if prop:
            attrs["property"] = prop
        tag = soup.find("meta", attrs=attrs)
        return bool(tag and str(tag.get("content") or "").strip())

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


def _analyze_heading_structure(soup: BeautifulSoup) -> Dict[str, Any]:
    def _primary_heading() -> str:
        for tag in soup.select("article h1, article h2, article h3"):
            value = (tag.get_text() or "").strip()
            if len(value) >= 8:
                return value

        for tag in soup.find_all(["h1", "h2", "h3"], limit=12):
            value = (tag.get_text() or "").strip()
            if len(value) >= 8:
                return value

        og_title = soup.find("meta", attrs={"property": "og:title"})
        if og_title and og_title.get("content"):
            value = str(og_title.get("content")).strip()
            if len(value) >= 8:
                return value
        return ""

    h1_tags = soup.find_all("h1")
    h2_count = len(soup.find_all("h2"))
    h3_count = len(soup.find_all("h3"))
    primary_heading = _primary_heading()
    hierarchy_valid = h2_count > 0 or h3_count == 0 or bool(primary_heading)
    if len(h1_tags) > 0:
        h1_present = True
        h1_unique = len(h1_tags) == 1
    else:
        h1_present = bool(primary_heading)
        h1_unique = bool(primary_heading)
    return {
        "h1_present": h1_present,
        "h1_unique": h1_unique,
        "h2_h3_hierarchy": hierarchy_valid,
        "primary_heading": primary_heading,
    }


def _extract_schema_types(payload: Any) -> list[str]:
    detected: set[str] = set()
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            node_type = node.get("@type")
            if isinstance(node_type, list):
                for item in node_type:
                    if isinstance(item, str) and item.strip():
                        detected.add(item.strip())
            elif isinstance(node_type, str) and node_type.strip():
                detected.add(node_type.strip())
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return sorted(detected)


def _json_ld_has_context(payload: Any) -> bool:
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            context = node.get("@context")
            if isinstance(context, str) and context.strip():
                return True
            if isinstance(context, list) and any(isinstance(item, str) and item.strip() for item in context):
                return True
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return False


def _analyze_json_ld_blocks(soup: BeautifulSoup) -> Dict[str, Any]:
    blocks: list[Dict[str, Any]] = []
    page_types: set[str] = set()
    page_target_types: set[str] = set()
    issue_set: set[str] = set()

    for index, block in enumerate(soup.find_all("script", attrs={"type": "application/ld+json"}), start=1):
        raw = (block.string or block.get_text() or "").strip()
        issues: list[str] = []
        detected_types: list[str] = []
        target_types: list[str] = []
        has_context = False
        parse_ok = False

        if not raw:
            issues.append("Empty JSON-LD block")
        else:
            try:
                payload = json.loads(raw)
                parse_ok = True
            except Exception:
                issues.append("Invalid JSON-LD: parse failed")
            else:
                has_context = _json_ld_has_context(payload)
                detected_types = _extract_schema_types(payload)
                target_types = [item for item in detected_types if item in SCHEMA_TARGETS]
                if not has_context:
                    issues.append("Missing @context")
                if not detected_types:
                    issues.append("Missing @type")

        passed = parse_ok and has_context and bool(detected_types)
        page_types.update(detected_types)
        page_target_types.update(target_types)
        for issue in issues:
            issue_set.add(issue)
        blocks.append(
            {
                "index": index,
                "passed": passed,
                "status": "PASS" if passed else "MISS",
                "parse_ok": parse_ok,
                "has_context": has_context,
                "types": detected_types,
                "target_types": target_types,
                "issues": issues,
            }
        )

    block_count = len(blocks)
    valid_block_count = sum(1 for item in blocks if item.get("passed"))
    invalid_block_count = block_count - valid_block_count
    present = block_count > 0
    page_issues = sorted(issue_set)
    if not present:
        page_issues = ["No JSON-LD blocks found"]

    return {
        "present": present,
        "applied_well": valid_block_count > 0,
        "block_count": block_count,
        "valid_block_count": valid_block_count,
        "invalid_block_count": invalid_block_count,
        "types": sorted(page_types),
        "target_types": sorted(page_target_types),
        "issues": page_issues,
        "blocks": blocks,
    }


def _detect_structured_data(soup: BeautifulSoup) -> list[str]:
    detected: set[str] = set()
    for block in _analyze_json_ld_blocks(soup).get("blocks") or []:
        for item in block.get("target_types") or []:
            if isinstance(item, str) and item in SCHEMA_TARGETS:
                detected.add(item)
    return sorted(detected)


def _safe_text(soup: BeautifulSoup) -> str:
    return " ".join(soup.stripped_strings)


def _detect_faq(soup: BeautifulSoup) -> bool:
    faq_blocks = soup.select("[class*='faq' i], [id*='faq' i], [class*='qna' i], [id*='qna' i], details")
    if faq_blocks:
        return True

    text = _safe_text(soup).lower()
    patterns_en = [r"\bwhat\b", r"\bhow\b", r"\bwhy\b", r"\bcan\b", r"\bshould\b", r"\bfaq\b"]
    patterns_ko = [r"자주\s*묻는\s*질문", r"\b질문\b", r"\b답변\b", r"어떻게", r"왜", r"무엇", r"가능"]
    en_hits = sum(1 for pattern in patterns_en if re.search(pattern, text))
    ko_hits = sum(1 for pattern in patterns_ko if re.search(pattern, text))
    question_marks = text.count("?") + text.count("？")
    return en_hits >= 3 or ko_hits >= 2 or question_marks >= 3


def _extract_entities(soup: BeautifulSoup, page_url: str) -> Dict[str, Any]:
    text = _safe_text(soup)
    lowered = text.lower()

    company_name = None
    og_site = soup.find("meta", attrs={"property": "og:site_name"})
    if og_site and og_site.get("content"):
        company_name = str(og_site.get("content")).strip()
    elif soup.title and soup.title.text:
        company_name = soup.title.text.strip().split("|")[0].strip()

    service_name = None
    for tag in soup.find_all(["h1", "h2", "h3"], limit=8):
        value = (tag.get_text() or "").strip()
        if len(value) >= 4:
            service_name = value
            break
    if not service_name:
        og_title = soup.find("meta", attrs={"property": "og:title"})
        if og_title and og_title.get("content"):
            service_name = str(og_title.get("content")).strip()

    emails = sorted(set(re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)))
    phones = sorted(set(re.findall(r"(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{2,4}\)?[\s.-]?)\d{3,4}[\s.-]?\d{4}", text)))

    location = None
    patterns = [
        r"\b[a-zA-Z\s]+,\s?[A-Z]{2}\b",
        r"\b[a-zA-Z\s]+,\s?(?:UK|USA|Korea|Japan|Germany|France)\b",
        r"\b[가-힣]{2,}(?:시|군|구)\b",
    ]
    for pattern in patterns:
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


def _score_geo(results: Dict[str, Any]) -> int:
    file_presence = results["file_presence"]
    meta = results["meta"]
    headings = results["headings"]
    structured_data = results["structured_data"]
    faq_detected = results["faq_detected"]
    entities = results["entities"]
    llms_quality = results.get("llms_txt_quality") if isinstance(results.get("llms_txt_quality"), dict) else {}
    machine = results.get("machine_readable") if isinstance(results.get("machine_readable"), dict) else {}

    score = 0.0
    score += 15 * (sum(1 for ok in file_presence.values() if ok) / max(len(file_presence), 1))

    meta_keys = ["title", "meta_description", "og_title", "og_description", "og_image", "canonical"]
    score += 18 * (sum(1 for key in meta_keys if meta.get(key)) / len(meta_keys))

    heading_checks = [headings.get("h1_present"), headings.get("h1_unique"), headings.get("h2_h3_hierarchy")]
    score += 12 * (sum(1 for item in heading_checks if item) / len(heading_checks))

    schema_points = min(len(structured_data), 3) / 3
    score += 18 * schema_points

    score += 10 if faq_detected else 0

    score += 12 if entities.get("entity_clarity") else 0

    llms_ratio = float(llms_quality.get("score", 0)) / max(int(llms_quality.get("maxScore", 12)), 1)
    score += 8 * llms_ratio

    machine_checks = [
        int(machine.get("next_data_pages", 0)) > 0,
        int(machine.get("article_meta_pages", 0)) > 0,
        int(machine.get("h_meta_pages", 0)) > 0,
    ]
    score += 7 * (sum(1 for item in machine_checks if item) / len(machine_checks))

    return max(0, min(100, round(score)))


def _build_recommendations(results: Dict[str, Any]) -> list[str]:
    recs: list[str] = []
    json_ld_summary = results.get("json_ld_summary") if isinstance(results.get("json_ld_summary"), dict) else {}
    llms_quality = results.get("llms_txt_quality") if isinstance(results.get("llms_txt_quality"), dict) else {}
    machine = results.get("machine_readable") if isinstance(results.get("machine_readable"), dict) else {}
    llms_score = int(llms_quality.get("score", 0) or 0)

    if not results["file_presence"].get("llms_txt"):
        recs.append("Add llms.txt to guide AI crawlers")
    elif llms_score < 7:
        recs.append("Improve llms.txt quality (structure, links, contact, key services)")
    if not results["faq_detected"]:
        recs.append("Add FAQ section with 3+ user questions")
    if not results["structured_data"]:
        recs.append("Add Organization schema")
    elif "FAQPage" not in results["structured_data"]:
        recs.append("Add FAQPage schema")
    if not results["entities"].get("entity_clarity"):
        recs.append("Improve service description clarity")
    if not results["meta"].get("meta_description"):
        recs.append("Add a descriptive meta description for better AI snippet quality")
    if int(json_ld_summary.get("invalid_pages", 0)) > 0:
        recs.append("Fix invalid JSON-LD blocks on affected pages")
    elif int(json_ld_summary.get("missing_pages", 0)) > 0:
        recs.append("Add JSON-LD to more crawled pages for better page-level schema coverage")
    machine_present = (
        int(machine.get("next_data_pages", 0)) > 0
        or int(machine.get("article_meta_pages", 0)) > 0
        or int(machine.get("h_meta_pages", 0)) > 0
    )
    if not machine_present:
        recs.append("Add machine-readable page payload signals for AI parsers")

    return recs


async def _check_file_presence(origin: str, client: httpx.AsyncClient) -> Dict[str, bool]:
    out: Dict[str, bool] = {}
    details: Dict[str, Any] = {"sitemapCandidates": [], "resolvedSitemapUrl": "", "llmsTxtContent": ""}

    # robots.txt
    robots_text = ""
    try:
        robots_resp = await client.get(urljoin(origin + "/", "/robots.txt"))
        out["robots_txt"] = robots_resp.status_code < 400
        if out["robots_txt"]:
            robots_text = robots_resp.text or ""
    except Exception:
        out["robots_txt"] = False

    # llms.txt
    try:
        llms_resp = await client.get(urljoin(origin + "/", "/llms.txt"))
        out["llms_txt"] = llms_resp.status_code < 400
        if out["llms_txt"]:
            details["llmsTxtContent"] = llms_resp.text or ""
    except Exception:
        out["llms_txt"] = False

    # ai.txt
    try:
        ai_resp = await client.get(urljoin(origin + "/", "/ai.txt"))
        out["ai_txt"] = ai_resp.status_code < 400
    except Exception:
        out["ai_txt"] = False

    # sitemap.xml + robots.txt Sitemap: fallback
    sitemap_candidates: list[str] = [urljoin(origin + "/", "/sitemap.xml")]
    for line in (robots_text or "").splitlines():
        if not line.lower().startswith("sitemap:"):
            continue
        raw = line.split(":", 1)[1].strip()
        if not raw:
            continue
        candidate = raw if raw.startswith(("http://", "https://")) else urljoin(origin + "/", raw)
        if candidate not in sitemap_candidates:
            sitemap_candidates.append(candidate)

    details["sitemapCandidates"] = sitemap_candidates
    out["sitemap"] = False
    for sitemap_url in sitemap_candidates[:8]:
        try:
            sm_resp = await client.get(sitemap_url)
            if sm_resp.status_code < 400:
                out["sitemap"] = True
                details["resolvedSitemapUrl"] = sitemap_url
                break
        except Exception:
            continue

    return out, details


def _analyze_llms_text(raw_text: str) -> Dict[str, Any]:
    text = (raw_text or "").strip()
    max_score = 12
    score = 0
    signals = {
        "has_sections": False,
        "has_urls": False,
        "has_contact": False,
        "has_service_keywords": False,
        "has_list": False,
        "sufficient_length": False,
    }

    if not text:
        return {
            "score": 0,
            "maxScore": max_score,
            "passed": False,
            "signals": signals,
            "notes": ["llms.txt is empty"],
        }

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    joined = "\n".join(lines).lower()

    if any(line.startswith("#") for line in lines):
        signals["has_sections"] = True
        score += 2
    if re.search(r"https?://", joined):
        signals["has_urls"] = True
        score += 3
    if re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", joined):
        signals["has_contact"] = True
        score += 2
    if any(k in joined for k in ["service", "product", "api", "docs", "pricing", "support", "서비스", "제품", "문의", "문서"]):
        signals["has_service_keywords"] = True
        score += 2
    if any(line.startswith(("-", "*", "1.", "2.", "3.")) for line in lines):
        signals["has_list"] = True
        score += 1
    if len(text) >= 300:
        signals["sufficient_length"] = True
        score += 2

    notes = []
    if not signals["has_sections"]:
        notes.append("Add section headings")
    if not signals["has_urls"]:
        notes.append("Add canonical URLs to key pages")
    if not signals["has_contact"]:
        notes.append("Add contact information")
    if not signals["has_service_keywords"]:
        notes.append("Describe key services/products")

    return {
        "score": min(max_score, score),
        "maxScore": max_score,
        "passed": score >= 7,
        "signals": signals,
        "notes": notes[:4],
    }


def _analyze_machine_readable_signals(soup: BeautifulSoup) -> Dict[str, Any]:
    next_data_present = False
    next_data_parse_ok = False
    next_data_has_article = False

    next_data_node = soup.find("script", attrs={"id": "__NEXT_DATA__"})
    if next_data_node:
        next_data_present = True
        raw = (next_data_node.string or next_data_node.get_text() or "").strip()
        if raw:
            try:
                payload = json.loads(raw)
                next_data_parse_ok = True

                stack = [payload]
                while stack:
                    node = stack.pop()
                    if isinstance(node, dict):
                        keys = set(node.keys())
                        if "article" in keys or {"headline", "datePublished"} <= keys:
                            next_data_has_article = True
                            break
                        stack.extend(node.values())
                    elif isinstance(node, list):
                        stack.extend(node)
            except Exception:
                next_data_parse_ok = False

    article_meta_props = [
        "article:published_time",
        "article:modified_time",
        "article:section",
        "og:type",
        "author",
    ]
    article_meta_hits = 0
    for prop in article_meta_props:
        meta = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        if meta and meta.get("content"):
            article_meta_hits += 1

    h_meta_hits = len(soup.select("meta[name^='h:'], meta[property^='h:']"))

    return {
        "next_data_present": next_data_present,
        "next_data_parse_ok": next_data_parse_ok,
        "next_data_has_article": next_data_has_article,
        "article_meta_hits": article_meta_hits,
        "h_meta_hits": h_meta_hits,
    }


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
    headings_aggregate = {"h1_present": False, "h1_unique": True, "h2_h3_hierarchy": True}
    structured_data: set[str] = set()
    faq_detected = False
    entity_candidate: Dict[str, Any] | None = None
    json_ld_pages: list[Dict[str, Any]] = []
    machine_summary = {
        "total_pages": len(pages),
        "next_data_pages": 0,
        "next_data_article_pages": 0,
        "article_meta_pages": 0,
        "h_meta_pages": 0,
    }

    for page in pages:
        soup = BeautifulSoup(page.html, "html.parser")

        meta = _analyze_meta_tags(soup)
        for key in meta_aggregate:
            meta_aggregate[key] = bool(meta_aggregate[key] or meta.get(key, False))

        headings = _analyze_heading_structure(soup)
        headings_aggregate["h1_present"] = bool(headings_aggregate["h1_present"] or headings.get("h1_present", False))
        headings_aggregate["h1_unique"] = bool(headings_aggregate["h1_unique"] and headings.get("h1_unique", False))
        headings_aggregate["h2_h3_hierarchy"] = bool(
            headings_aggregate["h2_h3_hierarchy"] and headings.get("h2_h3_hierarchy", False)
        )

        json_ld = _analyze_json_ld_blocks(soup)
        json_ld_pages.append(
            {
                "url": page.url,
                "path": page.path,
                "depth": page.depth,
                "status_code": page.status_code,
                **json_ld,
            }
        )
        structured_data.update(json_ld.get("target_types") or [])
        faq_detected = bool(faq_detected or _detect_faq(soup))

        entities = _extract_entities(soup, page.url)
        if entities.get("entity_clarity"):
            entity_candidate = entities
        elif entity_candidate is None:
            entity_candidate = entities

        machine = _analyze_machine_readable_signals(soup)
        if machine.get("next_data_present"):
            machine_summary["next_data_pages"] += 1
        if machine.get("next_data_has_article"):
            machine_summary["next_data_article_pages"] += 1
        if int(machine.get("article_meta_hits", 0)) > 0:
            machine_summary["article_meta_pages"] += 1
        if int(machine.get("h_meta_hits", 0)) > 0:
            machine_summary["h_meta_pages"] += 1

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
        "machine_readable": machine_summary,
        "json_ld_pages": json_ld_pages,
        "json_ld_summary": {
            "total_pages": len(json_ld_pages),
            "pages_with_json_ld": sum(1 for item in json_ld_pages if item.get("present")),
            "valid_pages": sum(1 for item in json_ld_pages if item.get("applied_well")),
            "invalid_pages": sum(1 for item in json_ld_pages if item.get("present") and not item.get("applied_well")),
            "missing_pages": sum(1 for item in json_ld_pages if not item.get("present")),
        },
    }


def _audit_item(
    key: str,
    label: str,
    passed: bool,
    value: Any = None,
    evidence: str | None = None,
) -> Dict[str, Any]:
    item = {
        "key": key,
        "label": label,
        "passed": bool(passed),
        "status": "PASS" if passed else "MISS",
    }
    if value is not None:
        item["value"] = value
    if evidence:
        item["evidence"] = evidence
    return item


def _audit_section(section_id: str, label: str, items: list[Dict[str, Any]], summary: str | None = None) -> Dict[str, Any]:
    passed = sum(1 for item in items if item.get("passed"))
    return {
        "id": section_id,
        "label": label,
        "summary": summary or f"{passed}/{len(items)} checks passed",
        "passCount": passed,
        "totalCount": len(items),
        "items": items,
    }


def _build_verified_sections(crawl_result: Dict[str, Any], results: Dict[str, Any]) -> list[Dict[str, Any]]:
    origin = str(crawl_result.get("origin") or "").rstrip("/")
    file_presence = results["file_presence"]
    file_details = results.get("file_details") if isinstance(results.get("file_details"), dict) else {}
    meta = results["meta"]
    headings = results["headings"]
    structured_data = results["structured_data"]
    json_ld_pages = results.get("json_ld_pages") if isinstance(results.get("json_ld_pages"), list) else []
    json_ld_summary = results.get("json_ld_summary") if isinstance(results.get("json_ld_summary"), dict) else {}
    machine = results.get("machine_readable") if isinstance(results.get("machine_readable"), dict) else {}
    llms_quality = results.get("llms_txt_quality") if isinstance(results.get("llms_txt_quality"), dict) else {}
    entities = results["entities"] if isinstance(results.get("entities"), dict) else {}
    contact = entities.get("contact_information") if isinstance(entities.get("contact_information"), dict) else {}
    emails = contact.get("emails") if isinstance(contact.get("emails"), list) else []
    phones = contact.get("phones") if isinstance(contact.get("phones"), list) else []

    llms_notes = llms_quality.get("notes") if isinstance(llms_quality.get("notes"), list) else []
    llms_evidence = ", ".join(str(note) for note in llms_notes if str(note).strip()) or "quality checks passed"
    sitemap_value = (
        str(file_details.get("resolvedSitemapUrl")).strip()
        if str(file_details.get("resolvedSitemapUrl") or "").strip()
        else f"{origin}/sitemap.xml"
    )
    file_items = [
        _audit_item("llms_txt", "llms.txt", bool(file_presence.get("llms_txt")), value=f"{origin}/llms.txt"),
        _audit_item(
            "llms_txt_quality",
            "llms.txt quality",
            bool(llms_quality.get("passed")),
            value=f"{int(llms_quality.get('score', 0))}/{int(llms_quality.get('maxScore', 12))}",
            evidence=llms_evidence,
        ),
        _audit_item("ai_txt", "ai.txt", bool(file_presence.get("ai_txt")), value=f"{origin}/ai.txt"),
        _audit_item("robots_txt", "robots.txt", bool(file_presence.get("robots_txt")), value=f"{origin}/robots.txt"),
        _audit_item("sitemap", "sitemap.xml", bool(file_presence.get("sitemap")), value=sitemap_value),
    ]

    meta_items = [
        _audit_item("title", "Page title", bool(meta.get("title")), value="Present" if meta.get("title") else "Missing"),
        _audit_item(
            "meta_description",
            "Meta description",
            bool(meta.get("meta_description")),
            value="Present" if meta.get("meta_description") else "Missing",
        ),
        _audit_item("og_title", "OG title", bool(meta.get("og_title")), value="Present" if meta.get("og_title") else "Missing"),
        _audit_item(
            "og_description",
            "OG description",
            bool(meta.get("og_description")),
            value="Present" if meta.get("og_description") else "Missing",
        ),
        _audit_item("og_image", "OG image", bool(meta.get("og_image")), value="Present" if meta.get("og_image") else "Missing"),
        _audit_item("canonical", "Canonical URL", bool(meta.get("canonical")), value="Present" if meta.get("canonical") else "Missing"),
    ]

    heading_items = [
        _audit_item("h1_present", "H1 present", bool(headings.get("h1_present"))),
        _audit_item("h1_unique", "Single H1", bool(headings.get("h1_unique"))),
        _audit_item("h2_h3_hierarchy", "H2/H3 hierarchy", bool(headings.get("h2_h3_hierarchy"))),
    ]

    structured_items = [
        _audit_item(
            "structured_data",
            "Detected schema types",
            bool(structured_data),
            value=structured_data or ["None"],
            evidence=f"{len(structured_data)} type(s) found",
        ),
        _audit_item(
            "json_ld_page_coverage",
            "Valid JSON-LD pages",
            int(json_ld_summary.get("valid_pages", 0)) == int(json_ld_summary.get("total_pages", 0))
            and int(json_ld_summary.get("total_pages", 0)) > 0,
            value=f"{int(json_ld_summary.get('valid_pages', 0))}/{int(json_ld_summary.get('total_pages', 0))}",
            evidence=(
                f"missing {int(json_ld_summary.get('missing_pages', 0))}, "
                f"invalid {int(json_ld_summary.get('invalid_pages', 0))}"
            ),
        ),
        _audit_item(
            "faq_schema",
            "FAQPage schema",
            "FAQPage" in structured_data,
            value="FAQPage" if "FAQPage" in structured_data else "Missing",
        ),
        _audit_item(
            "faq_detected",
            "FAQ-like content",
            bool(results.get("faq_detected")),
            value="Detected" if results.get("faq_detected") else "Not detected",
        ),
    ]

    json_ld_page_items = []
    for page in json_ld_pages:
        if not isinstance(page, dict):
            continue
        types = page.get("types") if isinstance(page.get("types"), list) else []
        issues = page.get("issues") if isinstance(page.get("issues"), list) else []
        evidence_bits = [
            f"HTTP {int(page.get('status_code') or 0)}",
            f"blocks {int(page.get('valid_block_count') or 0)}/{int(page.get('block_count') or 0)} valid",
        ]
        if issues:
            evidence_bits.append("issues: " + ", ".join(str(issue) for issue in issues))
        json_ld_page_items.append(
            _audit_item(
                f"json_ld:{page.get('path') or page.get('url') or ''}",
                str(page.get("path") or page.get("url") or "/"),
                bool(page.get("applied_well")),
                value=types or ["None"],
                evidence=" | ".join(evidence_bits),
            )
        )

    entity_items = [
        _audit_item(
            "company_name",
            "Company name",
            bool(entities.get("company_name")),
            value=entities.get("company_name") or "Not found",
        ),
        _audit_item(
            "service_name",
            "Service name",
            bool(entities.get("service_name")),
            value=entities.get("service_name") or "Not found",
        ),
        _audit_item("emails", "Contact emails", bool(emails), value=emails or ["None"]),
        _audit_item("phones", "Contact phones", bool(phones), value=phones or ["None"]),
        _audit_item("location", "Location", bool(entities.get("location")), value=entities.get("location") or "Not found"),
        _audit_item(
            "entity_clarity",
            "Entity clarity",
            bool(entities.get("entity_clarity")),
            value="Clear" if entities.get("entity_clarity") else "Needs improvement",
            evidence=str(entities.get("page_url") or ""),
        ),
    ]

    machine_items = [
        _audit_item(
            "next_data_pages",
            "__NEXT_DATA__ pages",
            int(machine.get("next_data_pages", 0)) > 0,
            value=f"{int(machine.get('next_data_pages', 0))}/{int(machine.get('total_pages', 0))}",
        ),
        _audit_item(
            "next_data_article_pages",
            "__NEXT_DATA__ article payload",
            int(machine.get("next_data_article_pages", 0)) > 0,
            value=f"{int(machine.get('next_data_article_pages', 0))}/{int(machine.get('total_pages', 0))}",
        ),
        _audit_item(
            "article_meta_pages",
            "Article meta pages",
            int(machine.get("article_meta_pages", 0)) > 0,
            value=f"{int(machine.get('article_meta_pages', 0))}/{int(machine.get('total_pages', 0))}",
        ),
        _audit_item(
            "h_meta_pages",
            "h:* meta pages",
            int(machine.get("h_meta_pages", 0)) > 0,
            value=f"{int(machine.get('h_meta_pages', 0))}/{int(machine.get('total_pages', 0))}",
        ),
    ]

    crawled_pages = []
    for page in crawl_result.get("pages") or []:
        if not isinstance(page, CrawledPage):
            continue
        crawled_pages.append(
            _audit_item(
                page.path or page.url,
                page.path or "/",
                page.status_code < 400,
                value=page.url,
                evidence=f"HTTP {page.status_code} · depth {page.depth}",
            )
        )

    return [
        _audit_section("files", "File Presence", file_items),
        _audit_section("meta", "Meta Tags", meta_items),
        _audit_section("headings", "Heading Structure", heading_items),
        _audit_section("structured", "Structured Data & FAQ", structured_items),
        _audit_section(
            "json_ld_pages",
            "Page JSON-LD Coverage",
            json_ld_page_items,
            summary=(
                f"{int(json_ld_summary.get('valid_pages', 0))}/{int(json_ld_summary.get('total_pages', 0))} "
                "page(s) have valid JSON-LD"
            ),
        ),
        _audit_section("machine", "Machine-readable Signals", machine_items),
        _audit_section("entities", "Entity Signals", entity_items),
        _audit_section(
            "pages",
            "Crawled Pages",
            crawled_pages,
            summary=f"{len(crawled_pages)} page(s) crawled from the target URL",
        ),
    ]


async def run_geo_audit(url: str) -> Dict[str, Any]:
    crawl_result = await _crawl_site(url)

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        file_presence_raw = await _check_file_presence(crawl_result["origin"], client)
        if isinstance(file_presence_raw, tuple) and len(file_presence_raw) == 2:
            file_presence, file_details = file_presence_raw
        else:
            file_presence = file_presence_raw if isinstance(file_presence_raw, dict) else {}
            file_details = {}

    llms_quality = _analyze_llms_text(str(file_details.get("llmsTxtContent") or ""))

    aggregated = _aggregate_page_results(crawl_result["pages"])
    results = {
        "file_presence": file_presence,
        "file_details": file_details,
        "llms_txt_quality": llms_quality,
        **aggregated,
    }

    checks = {
        **results["file_presence"],
        "title": results["meta"]["title"],
        "meta_description": results["meta"]["meta_description"],
        "og_tags": results["meta"]["og_tags"],
        "faq_detected": results["faq_detected"],
        "structured_data": results["structured_data"],
        "machine_readable_payload": (
            int(results.get("machine_readable", {}).get("next_data_pages", 0)) > 0
            or int(results.get("machine_readable", {}).get("article_meta_pages", 0)) > 0
        ),
    }
    pages = []
    for page in crawl_result.get("pages") or []:
        if not isinstance(page, CrawledPage):
            continue
        pages.append(
            {
                "url": page.url,
                "path": page.path,
                "depth": page.depth,
                "status_code": page.status_code,
            }
        )
    verified_sections = _build_verified_sections(crawl_result, results)

    return {
        "url": crawl_result["target"],
        "geo_score": _score_geo(results),
        "checks": checks,
        "structured_data": results["structured_data"],
        "recommendations": _build_recommendations(results),
        "evidence": {
            "origin": crawl_result["origin"],
            "target": crawl_result["target"],
            "file_presence": results["file_presence"],
            "file_details": results["file_details"],
            "meta": results["meta"],
            "headings": results["headings"],
            "faq_detected": results["faq_detected"],
            "entities": results["entities"],
            "machine_readable": results["machine_readable"],
            "llms_txt_quality": results["llms_txt_quality"],
            "structured_data": results["structured_data"],
            "json_ld_summary": results["json_ld_summary"],
            "json_ld_pages": results["json_ld_pages"],
            "crawled_pages": pages,
        },
        "verified_sections": verified_sections,
    }
