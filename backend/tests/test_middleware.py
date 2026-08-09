"""Request id propagation, security headers and the body size guard.

None of this is reachable from the router test suites — they assert on JSON
bodies and status codes, never on headers — so the middleware stack has run on
every request in the suite without a single test ever looking at what it
attached to the response.
"""
from __future__ import annotations

from app.core.config import settings


class TestSecurityHeaders:
    def test_the_standard_headers_are_present_on_every_response(self, app_client):
        response = app_client.get("/health")

        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert response.headers["Permissions-Policy"] == (
            "camera=(), microphone=(), geolocation=()"
        )
        assert response.headers["Content-Security-Policy"] == (
            "default-src 'none'; frame-ancestors 'none'"
        )

    def test_hsts_is_absent_outside_production(self, app_client):
        """Sent from a local dev server it would pin the browser to https on
        localhost, breaking every other project on the machine."""
        assert settings.is_production is False

        response = app_client.get("/health")

        assert "Strict-Transport-Security" not in response.headers

    def test_headers_are_attached_to_an_error_response_too(self, app_client):
        response = app_client.get("/api/v1/organizations/me")

        assert response.status_code == 401
        assert response.headers["X-Content-Type-Options"] == "nosniff"


class TestRequestId:
    def test_a_request_id_is_minted_when_none_is_supplied(self, app_client):
        response = app_client.get("/health")

        assert response.headers["X-Request-ID"]

    def test_an_inbound_request_id_is_echoed_back(self, app_client):
        response = app_client.get(
            "/health", headers={"X-Request-ID": "trace-abc-123"}
        )

        assert response.headers["X-Request-ID"] == "trace-abc-123"

    def test_an_inbound_request_id_is_sanitised(self, app_client):
        """It lands in a log line and a database column; anything else is a
        cheap way to bloat both."""
        response = app_client.get(
            "/health", headers={"X-Request-ID": "bad<script>id;drop table"}
        )

        request_id = response.headers["X-Request-ID"]
        assert all(c.isalnum() or c in "-_" for c in request_id)

    def test_an_inbound_request_id_is_bounded_to_64_characters(self, app_client):
        response = app_client.get("/health", headers={"X-Request-ID": "a" * 500})

        assert len(response.headers["X-Request-ID"]) <= 64

    def test_the_response_time_header_is_a_number(self, app_client):
        response = app_client.get("/health")

        assert float(response.headers["X-Response-Time-ms"]) >= 0


class TestBodySizeLimit:
    def test_a_request_declaring_an_oversized_body_is_refused_before_it_is_read(
        self, app_client
    ):
        oversized = settings.max_upload_bytes + 1_048_576 + 1

        response = app_client.post(
            "/api/v1/auth/login",
            content=b"{}",
            headers={"content-length": str(oversized)},
        )

        assert response.status_code == 413
        assert response.json()["error"]["code"] == "payload_too_large"

    def test_a_malformed_content_length_is_a_400(self, app_client):
        response = app_client.post(
            "/api/v1/auth/login",
            content=b"{}",
            headers={"content-length": "not-a-number"},
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "bad_request"

    def test_an_ordinary_request_is_unaffected(self, app_client):
        response = app_client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@example.com", "password": "wrong-password"},
        )

        assert response.status_code in (401, 422)
