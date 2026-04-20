"""Smoke tests for health + ready endpoints + correlation middleware."""

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
    cid = response.headers["X-Correlation-ID"]
    # Must be a valid UUID v7 — 36 char canonical form.
    assert len(cid) == 36
    assert cid.count("-") == 4


def test_correlation_id_preserved_when_valid_uuid_provided(client: TestClient) -> None:
    given = "00000000-0000-7000-8000-000000000001"
    response = client.get("/health", headers={"X-Correlation-ID": given})
    assert response.headers["X-Correlation-ID"] == given


def test_correlation_id_regenerated_when_invalid_supplied(client: TestClient) -> None:
    """Arbitrary (non-UUID) incoming values must be dropped (log injection defense)."""
    malicious = "abc\r\nX-Injected: foo"
    response = client.get("/health", headers={"X-Correlation-ID": malicious})
    echoed = response.headers["X-Correlation-ID"]
    # Middleware must replace the malicious value with a fresh UUID.
    assert echoed != malicious
    assert len(echoed) == 36
    assert "\r" not in echoed
    assert "\n" not in echoed


def test_correlation_id_regenerated_when_oversized(client: TestClient) -> None:
    """Oversized headers (> 64 chars) are rejected to prevent memory DoS."""
    oversized = "a" * 128
    response = client.get("/health", headers={"X-Correlation-ID": oversized})
    echoed = response.headers["X-Correlation-ID"]
    assert echoed != oversized
    assert len(echoed) == 36
