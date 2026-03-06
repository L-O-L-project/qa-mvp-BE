import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import app


class GeoTestPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_geo_test_page_served(self):
        res = self.client.get("/geo-test")
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/html", res.headers.get("content-type", ""))
        self.assertIn("GEO Audit Test Console", res.text)

    def test_geo_audit_requires_url(self):
        res = self.client.post("/api/geo-audit", json={})
        self.assertEqual(res.status_code, 400)
        body = res.json()
        self.assertEqual(body.get("detail", {}).get("errorCode"), "URL_REQUIRED")

    def test_geo_audit_runs_and_returns_result(self):
        fake_result = {
            "url": "https://example.com",
            "geo_score": 88,
            "checks": {"title": True},
            "structured_data": ["Organization"],
            "recommendations": [],
        }
        with patch("app.main.run_geo_audit", AsyncMock(return_value=fake_result)) as mocked:
            res = self.client.post("/api/geo-audit", json={"url": "https://example.com"})
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json(), fake_result)
            mocked.assert_awaited_once_with("https://example.com")
