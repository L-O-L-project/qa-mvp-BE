from fastapi.testclient import TestClient

from app.main import app


def test_health_route():
    client = TestClient(app)
    response = client.get('/health')
    assert response.status_code == 200
    assert response.json() == {'status': 'ok'}


def test_test_page_route_serves_html():
    client = TestClient(app)
    response = client.get('/')
    assert response.status_code == 200
    assert 'text/html' in response.headers.get('content-type', '')
    assert 'GEO Audit 테스트 페이지' in response.text
