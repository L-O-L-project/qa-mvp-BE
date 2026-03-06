from app.analysis.geo_audit import build_recommendations, score_geo


def test_geo_score_full_points():
    payload = {
        "file_presence": {"llms_txt": True, "ai_txt": True, "robots_txt": True, "sitemap": True},
        "meta": {
            "title": True,
            "meta_description": True,
            "og_title": True,
            "og_description": True,
            "og_image": True,
            "canonical": True,
        },
        "headings": {"h1_present": True, "h1_unique": True, "h2_h3_hierarchy": True},
        "structured_data": ["Organization", "WebSite", "FAQPage"],
        "faq_detected": True,
        "entities": {"entity_clarity": True},
    }
    assert score_geo(payload) == 100


def test_recommendations_on_missing_checks():
    payload = {
        "file_presence": {"llms_txt": False, "ai_txt": False, "robots_txt": True, "sitemap": True},
        "meta": {"meta_description": False},
        "structured_data": [],
        "faq_detected": False,
        "entities": {"entity_clarity": False},
    }
    recs = build_recommendations(payload)
    assert any("llms.txt" in r for r in recs)
    assert any("FAQ" in r for r in recs)
    assert any("Organization schema" in r for r in recs)
