import unittest
from unittest.mock import AsyncMock, patch

from app.services.geo_audit import CrawledPage, run_geo_audit


class GeoAuditDetailTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_geo_audit_returns_verified_sections_and_evidence(self):
        html = """
        <html>
          <head>
            <title>Acme QA Platform</title>
            <meta name="description" content="Ship safer releases" />
            <meta property="og:title" content="Acme QA Platform" />
            <meta property="og:description" content="Ship safer releases" />
            <meta property="og:image" content="https://example.com/og.png" />
            <link rel="canonical" href="https://example.com/" />
            <script type="application/ld+json">
              {"@context":"https://schema.org","@type":["Organization","FAQPage"]}
            </script>
          </head>
          <body>
            <h1>Acme QA Platform</h1>
            <h2>Common Questions</h2>
            <section class="faq">
              <p>What is it? How does it work? Why use it?</p>
            </section>
            <p>Contact us at hello@example.com or +1 415 555 1234.</p>
            <p>San Francisco, CA</p>
          </body>
        </html>
        """
        invalid_json_ld_html = """
        <html>
          <head>
            <title>Acme Docs</title>
            <script type="application/ld+json">
              {"@context":"https://schema.org","@type":
            </script>
          </head>
          <body>
            <h1>Docs</h1>
          </body>
        </html>
        """
        crawl_result = {
            "origin": "https://example.com",
            "target": "https://example.com",
            "pages": [
                CrawledPage(
                    url="https://example.com",
                    path="/",
                    depth=0,
                    html=html,
                    status_code=200,
                ),
                CrawledPage(
                    url="https://example.com/docs",
                    path="/docs",
                    depth=1,
                    html=invalid_json_ld_html,
                    status_code=200,
                )
            ],
        }
        file_presence = {
            "llms_txt": True,
            "ai_txt": False,
            "robots_txt": True,
            "sitemap": True,
        }

        with patch("app.services.geo_audit._crawl_site", AsyncMock(return_value=crawl_result)), patch(
            "app.services.geo_audit._check_file_presence",
            AsyncMock(return_value=file_presence),
        ):
            result = await run_geo_audit("https://example.com")

        self.assertIn("evidence", result)
        self.assertIn("verified_sections", result)
        self.assertEqual(result["evidence"]["origin"], "https://example.com")
        self.assertEqual(len(result["evidence"]["crawled_pages"]), 2)
        self.assertEqual(result["evidence"]["crawled_pages"][0]["status_code"], 200)
        self.assertEqual(result["evidence"]["json_ld_summary"]["total_pages"], 2)
        self.assertEqual(result["evidence"]["json_ld_summary"]["valid_pages"], 1)
        self.assertEqual(result["evidence"]["json_ld_summary"]["invalid_pages"], 1)

        sections = {section["id"]: section for section in result["verified_sections"]}
        self.assertIn("files", sections)
        self.assertIn("structured", sections)
        self.assertIn("json_ld_pages", sections)
        self.assertIn("pages", sections)
        self.assertEqual(sections["pages"]["totalCount"], 2)

        structured_item = next(item for item in sections["structured"]["items"] if item["key"] == "structured_data")
        self.assertTrue(structured_item["passed"])
        self.assertIn("Organization", structured_item["value"])
        page_json_ld_item = next(item for item in sections["structured"]["items"] if item["key"] == "json_ld_page_coverage")
        self.assertEqual(page_json_ld_item["value"], "1/2")

        json_ld_section = sections["json_ld_pages"]
        docs_item = next(item for item in json_ld_section["items"] if item["label"] == "/docs")
        self.assertFalse(docs_item["passed"])
        self.assertIn("Invalid JSON-LD: parse failed", docs_item["evidence"])

        entity_section = sections["entities"]
        emails_item = next(item for item in entity_section["items"] if item["key"] == "emails")
        self.assertIn("hello@example.com", emails_item["value"])

    async def test_run_geo_audit_detects_next_data_and_korean_faq(self):
        html = """
        <html>
          <head>
            <title>옵티플로우 GEO 가이드</title>
            <meta property="og:title" content="옵티플로우 GEO 가이드" />
            <script id="__NEXT_DATA__" type="application/json">
              {"props":{"pageProps":{"article":{"title":"옵티플로우 GEO 가이드","id":1}}}}
            </script>
          </head>
          <body>
            <h3>옵티플로우 GEO 가이드</h3>
            <section class="qna">
              <p>자주 묻는 질문</p>
              <p>어떻게 시작하나요?</p>
              <p>왜 필요한가요?</p>
            </section>
          </body>
        </html>
        """
        crawl_result = {
            "origin": "https://example.com",
            "target": "https://example.com",
            "pages": [
                CrawledPage(
                    url="https://example.com",
                    path="/",
                    depth=0,
                    html=html,
                    status_code=200,
                ),
            ],
        }
        file_presence = {
            "llms_txt": False,
            "ai_txt": False,
            "robots_txt": True,
            "sitemap": False,
        }

        with patch("app.services.geo_audit._crawl_site", AsyncMock(return_value=crawl_result)), patch(
            "app.services.geo_audit._check_file_presence",
            AsyncMock(return_value=file_presence),
        ):
            result = await run_geo_audit("https://example.com")

        self.assertTrue(result["checks"]["faq_detected"])
        self.assertTrue(result["checks"]["machine_readable_payload"])
        self.assertTrue(result["evidence"]["headings"]["h1_present"])
        self.assertEqual(result["evidence"]["machine_readable"]["next_data_pages"], 1)
