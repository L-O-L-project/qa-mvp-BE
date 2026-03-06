from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

try:
    from playwright.async_api import async_playwright
except Exception:  # pragma: no cover
    async_playwright = None


@dataclass
class CrawledPage:
    url: str
    path: str
    depth: int
    html: str
    status_code: int


def normalize_target(url: str) -> str:
    candidate = (url or "").strip()
    if not candidate:
        raise ValueError("url is required")
    if not candidate.startswith(("http://", "https://")):
        candidate = f"https://{candidate}"
    return candidate


def _normalize_path(url: str) -> str:
    p = urlparse(url).path or "/"
    if p != "/" and p.endswith("/"):
        p = p[:-1]
    return p


def _extract_paths_from_source(html: str) -> List[str]:
    candidates = set()
    for m in re.findall(r"(?:router\.push|navigate|href|to)\(\s*['\"](/[^'\"#?\s]{1,120})['\"]\s*\)", html):
        candidates.add(m)
    for m in re.findall(r"['\"](/(?:[a-zA-Z0-9_\-]+/){0,4}[a-zA-Z0-9_\-]+)['\"]", html):
        if len(m) > 1:
            candidates.add(m)
    return sorted(candidates)[:120]


async def _extract_paths_dynamic(url: str, origin: str) -> List[str]:
    if async_playwright is None:
        return []

    discovered: set[str] = set()
    try:
        async with async_playwright() as p:  # type: ignore[misc]
            browser = await p.chromium.launch(headless=True)
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

    return sorted([p for p in discovered if p.startswith("/")])[:150]


async def crawl_site(url: str, max_pages: int = 10, max_depth: int = 2) -> Dict[str, Any]:
    target = normalize_target(url)
    parsed = urlparse(target)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    verify_tls = os.getenv("QA_HTTP_VERIFY_TLS", "false").lower() in {"1", "true", "yes", "on"}

    queue: List[tuple[str, int]] = [(target, 0)]
    visited: set[str] = set()
    pages: List[CrawledPage] = []

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
            page = CrawledPage(
                url=str(response.url),
                path=_normalize_path(str(response.url)),
                depth=depth,
                html=html,
                status_code=response.status_code,
            )
            pages.append(page)

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

            if depth == 0 and os.getenv("QA_ANALYZE_DYNAMIC", "true").lower() in {"1", "true", "yes", "on"}:
                for dynamic_path in await _extract_paths_dynamic(str(response.url), origin):
                    absolute = urljoin(origin + "/", dynamic_path)
                    if depth < max_depth and absolute not in visited and all(q[0] != absolute for q in queue):
                        queue.append((absolute, depth + 1))

    if not pages:
        raise RuntimeError("no pages crawled")

    return {
        "origin": origin,
        "target": target,
        "pages": pages,
    }
