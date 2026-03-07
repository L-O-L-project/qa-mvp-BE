import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import app


class GeoDiscoveryApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_geo_discovery_requires_base_url(self):
        res = self.client.post("/api/geo-discovery", json={})
        self.assertEqual(res.status_code, 400)
        body = res.json()
        self.assertEqual(body.get("detail", {}).get("errorCode"), "BASE_URL_REQUIRED")

    def test_geo_discovery_accepts_url_alias(self):
        fake = {"ok": True, "analysisId": "py_analysis_1", "candidates": []}
        with patch("app.routers.discovery.analyze_site", AsyncMock(return_value=fake)) as mocked:
            res = self.client.post("/api/geo-discovery", json={"url": "https://example.com"})
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json(), fake)
            mocked.assert_awaited_once()
            called_args = mocked.await_args.args
            self.assertEqual(called_args[0], "https://example.com")

    def test_geo_discovery_passes_llm_routing_options(self):
        fake = {"ok": True, "analysisId": "py_analysis_2", "candidates": []}
        with patch("app.routers.discovery.analyze_site", AsyncMock(return_value=fake)) as mocked:
            res = self.client.post(
                "/api/geo-discovery",
                json={
                    "baseUrl": "https://example.com",
                    "llmRouting": {
                        "providers": ["openai", "ollama"],
                        "auth": {"openai": {"mode": "apiKey", "apiKey": "sk-test"}},
                    },
                    "llmModel": "gpt-4o-mini",
                },
            )
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json(), fake)

            mocked.assert_awaited_once()
            called_args = mocked.await_args.args
            called_kwargs = mocked.await_args.kwargs
            self.assertEqual(called_args[0], "https://example.com")
            self.assertEqual(called_kwargs.get("provider"), "openai,ollama")
            self.assertEqual(called_kwargs.get("model"), "gpt-4o-mini")
            self.assertEqual(
                called_kwargs.get("llm_auth"),
                {"openai": {"mode": "apiKey", "apiKey": "sk-test"}},
            )


if __name__ == "__main__":
    unittest.main()
