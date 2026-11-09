"""Tests for Microsoft Teams webhook notifications."""
import json
from unittest.mock import patch, MagicMock

import pytest

from app.config import Settings
from app.teams import _build_card, send_approval_request, send_partial_approval


@pytest.fixture()
def cfg():
    return Settings(
        entra_tenant_id="test-tenant",
        entra_api_audience="test-audience",
        teams_webhook_url="https://outlook.office.com/webhook/test",
        app_url="https://app.example.com/",
        magic_link_secret="test-secret",
        api_base_url="http://localhost:8000",
    )


@pytest.fixture()
def cfg_no_teams():
    return Settings(
        entra_tenant_id="test-tenant",
        entra_api_audience="test-audience",
        teams_webhook_url="",
    )


@pytest.fixture()
def sample_item():
    return {
        "request_id": "AR-20260101-ABC123",
        "requester_name": "Test User",
        "requester_upn": "test@lilly.com",
        "requester_oid": "user-oid",
        "global_id": "L999999",
        "email": "test@lilly.com",
        "country_code": "US",
        "dou": "Test DoU",
        "use_case": "Test Use Case",
        "reltio_roles": ["ROLE_A", "ROLE_B"],
        "tenants": ["tenant-a"],
        "required_approvers": [
            {"key": "alice-oid", "name": "Alice", "upn": "alice@lilly.com", "oid": "alice-oid"},
        ],
        "status": "PENDING",
        "approvals": {},
    }


@pytest.fixture()
def sample_summary():
    return {
        "Request": "AR-20260101-ABC123",
        "Requested by": "Test User",
        "Global ID": "L999999",
        "Email": "test@lilly.com",
        "Country": "US",
        "DoU": "Test DoU",
        "Use case": "Test Use Case",
        "Tenants": "tenant-a",
        "Reltio roles": "ROLE_A, ROLE_B",
    }


class TestBuildCard:
    def test_card_structure(self):
        card = _build_card(
            heading="Test heading",
            summary={"Key": "Value"},
            message="Test message",
            cta_url="https://example.com",
            cta_label="Click me",
        )
        assert card["type"] == "AdaptiveCard"
        assert card["version"] == "1.4"
        assert len(card["body"]) == 4
        assert card["actions"][0]["type"] == "Action.OpenUrl"
        assert card["actions"][0]["url"] == "https://example.com"
        assert card["actions"][0]["title"] == "Click me"

    def test_card_without_cta(self):
        card = _build_card(heading="Test", summary={"K": "V"}, message="Msg")
        assert "actions" not in card

    def test_card_facts(self):
        card = _build_card(heading="H", summary={"A": "1", "B": "2"}, message="M")
        fact_set = card["body"][3]
        assert fact_set["type"] == "FactSet"
        assert len(fact_set["facts"]) == 2
        assert fact_set["facts"][0]["title"] == "A"
        assert fact_set["facts"][0]["value"] == "1"


class TestSendApprovalRequest:
    def test_skips_when_no_webhook(self, cfg_no_teams, sample_item, sample_summary):
        with patch("app.teams._post_webhook") as mock:
            send_approval_request(cfg_no_teams, sample_item, sample_summary)
            mock.assert_not_called()

    def test_sends_with_magic_link(self, cfg, sample_item, sample_summary):
        with patch("app.teams._post_webhook") as mock:
            send_approval_request(cfg, sample_item, sample_summary,
                                  magic_link_urls=["https://example.com/review/token123"])
            mock.assert_called_once()
            webhook_url, card = mock.call_args[0]
            assert webhook_url == "https://outlook.office.com/webhook/test"
            assert card["actions"][0]["url"] == "https://example.com/review/token123"

    def test_sends_with_app_url_fallback(self, cfg, sample_item, sample_summary):
        with patch("app.teams._post_webhook") as mock:
            send_approval_request(cfg, sample_item, sample_summary)
            mock.assert_called_once()
            _, card = mock.call_args[0]
            assert "app.example.com" in card["actions"][0]["url"]

    def test_card_contains_request_id(self, cfg, sample_item, sample_summary):
        with patch("app.teams._post_webhook") as mock:
            send_approval_request(cfg, sample_item, sample_summary)
            _, card = mock.call_args[0]
            heading = card["body"][1]["text"]
            assert "AR-20260101-ABC123" in heading


class TestSendPartialApproval:
    def test_skips_when_no_webhook(self, cfg_no_teams, sample_item, sample_summary):
        decision = {"name": "Alice", "decision": "approve", "comment": ""}
        with patch("app.teams._post_webhook") as mock:
            send_partial_approval(cfg_no_teams, sample_item, sample_summary,
                                  decision, "Bob")
            mock.assert_not_called()

    def test_sends_partial(self, cfg, sample_item, sample_summary):
        decision = {"name": "Alice", "decision": "approve", "comment": ""}
        with patch("app.teams._post_webhook") as mock:
            send_partial_approval(cfg, sample_item, sample_summary,
                                  decision, "Bob Approver",
                                  magic_link_urls=["https://example.com/review/bob-token"])
            mock.assert_called_once()
            _, card = mock.call_args[0]
            msg = card["body"][2]["text"]
            assert "Alice" in msg
            assert "Bob Approver" in msg
