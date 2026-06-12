from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from src.api.dependencies import verify_api_key, verify_docs_access
from src.api.main import app

_RETAILER_ID = 1


@pytest.fixture
def client():
    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[verify_docs_access] = lambda: None
    app.state.sainsburys_retailer_id = _RETAILER_ID
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def client_no_auth():
    app.state.sainsburys_retailer_id = _RETAILER_ID
    with patch("src.api.dependencies._API_KEY", "test-secret"):
        yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# GET /docs
# ---------------------------------------------------------------------------

def test_docs_200_with_valid_key(client):
    res = client.get("/docs")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]


def test_docs_401_no_key(client_no_auth):
    res = client_no_auth.get("/docs")
    assert res.status_code == 401


def test_docs_401_wrong_key(client_no_auth):
    res = client_no_auth.get("/docs", headers={"X-API-Key": "wrong"})
    assert res.status_code == 401


# ---------------------------------------------------------------------------
# GET /openapi.json
# ---------------------------------------------------------------------------

def test_openapi_json_200(client):
    res = client.get("/openapi.json")
    assert res.status_code == 200
    data = res.json()
    assert "openapi" in data


# ---------------------------------------------------------------------------
# POST /docs-login
# ---------------------------------------------------------------------------

def test_docs_login_200(client_no_auth):
    res = client_no_auth.post("/docs-login", json={"api_key": "test-secret"})
    assert res.status_code == 200
    assert res.json() == {"ok": True}
    set_cookie = res.headers.get("set-cookie", "")
    assert "docs_session" in set_cookie
    assert "HttpOnly" in set_cookie


def test_docs_login_401_wrong_key(client_no_auth):
    res = client_no_auth.post("/docs-login", json={"api_key": "wrong"})
    assert res.status_code == 401


def test_docs_login_422_missing_field(client_no_auth):
    res = client_no_auth.post("/docs-login", json={})
    assert res.status_code == 422


# ---------------------------------------------------------------------------
# GET /docs — cookie auth
# ---------------------------------------------------------------------------

def test_docs_200_with_cookie(client_no_auth):
    client_no_auth.cookies.set("docs_session", "test-secret")
    response = client_no_auth.get("/docs")
    assert response.status_code == 200


def test_docs_401_invalid_cookie_no_header(client_no_auth):
    client_no_auth.cookies.set("docs_session", "wrong")
    res = client_no_auth.get("/docs")
    assert res.status_code == 401
