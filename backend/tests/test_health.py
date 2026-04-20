"""Smoke tests for health + ready endpoints."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agentive_backend import __version__
from agentive_backend.app.main import create_app


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(create_app())


def test_health_endpoint_returns_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__


def test_correlation_id_echoed_in_response_headers(client: TestClient) -> None:
    response = client.get("/health")
    assert "X-Correlation-ID" in response.headers
    # Must be a valid UUID-ish string
    assert len(response.headers["X-Correlation-ID"]) >= 32


def test_correlation_id_preserved_when_provided(client: TestClient) -> None:
    given = "00000000-0000-7000-8000-000000000001"
    response = client.get("/health", headers={"X-Correlation-ID": given})
    assert response.headers["X-Correlation-ID"] == given
