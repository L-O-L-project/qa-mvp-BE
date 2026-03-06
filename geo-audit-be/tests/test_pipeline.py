import asyncio

from app.analysis.pipeline import run_geo_audit
from app.crawler import CrawledPage


def test_run_geo_audit_aggregates_multi_page_signals(monkeypatch):
    async def fake_crawl_site(_url: str):
        home_html = """
        <html><head><title>Home</title></head>
        <body><h1>Home</h1><a href='/about'>About</a></body></html>
        """
        about_html = """
        <html>
          <head>
            <meta name='description' content='desc'>
            <meta property='og:title' content='OG title'>
            <meta property='og:description' content='OG description'>
            <meta property='og:image' content='https://example.com/img.png'>
            <script type='application/ld+json'>
              {"@context":"https://schema.org", "@type":"Organization"}
            </script>
          </head>
          <body>
            <h1>About</h1>
            <section class='faq'>FAQ block</section>
            <p>Contact us: hello@example.com</p>
          </body>
        </html>
        """
        return {
            "origin": "https://example.com",
            "target": "https://example.com",
            "pages": [
                CrawledPage(url="https://example.com/", path="/", depth=0, html=home_html, status_code=200),
                CrawledPage(url="https://example.com/about", path="/about", depth=1, html=about_html, status_code=200),
            ],
        }

    async def fake_file_presence(_origin, _client):
        return {
            "llms_txt": False,
            "ai_txt": False,
            "robots_txt": True,
            "sitemap": True,
        }

    monkeypatch.setattr("app.analysis.pipeline.crawl_site", fake_crawl_site)
    monkeypatch.setattr("app.analysis.pipeline.check_file_presence", fake_file_presence)

    result = asyncio.run(run_geo_audit("https://example.com"))

    assert result["url"] == "https://example.com"
    assert result["checks"]["meta_description"] is True
    assert result["checks"]["og_tags"] is True
    assert result["checks"]["faq_detected"] is True
    assert "Organization" in result["checks"]["structured_data"]
    assert 0 <= result["geo_score"] <= 100
