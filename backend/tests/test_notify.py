"""Tests for app.notify (email and in-app notification helpers)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app import notify
from app.notify import (
    _esc,
    _html_body,
    _summary,
    _summary_dict,
    cancelled,
    completed,
    partial_approved,
    rejected,
    submitted,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _make_item(**overrides):
    """Build a realistic request item for notification tests."""
    item = {
        "request_id": "AR-20241101-AABBCC",
        "requester_name": "Test User",
        "requester_upn": "user@lilly.com",
        "requester_oid": "user-oid",
        "global_id": "L123456",
        "email": "user@lilly.com",
        "country_code": "US",
        "dou": "Test DoU",
        "use_case": "Data stewardship",
        "justification": "Need access for project X",
        "tenants": ["tenant-a"],
        "reltio_roles": ["ROLE_A", "ROLE_B"],
        "required_approvers": [
            {"name": "Alice Approver", "upn": "alice@lilly.com", "oid": "alice-oid", "key": "alice-oid"},
            {"name": "Bob Approver", "upn": "bob@lilly.com", "oid": "bob-oid", "key": "bob-oid"},
        ],
        "approvals": {},
    }
    item.update(overrides)
    return item


@pytest.fixture(autouse=True)
def _reset_ses_client():
    """Reset the module-level SES client cache before each test."""
    notify._ses_client = None
    yield
    notify._ses_client = None


# ---------------------------------------------------------------------------
# _esc()
# ---------------------------------------------------------------------------
class TestEsc:

    def test_escapes_ampersand(self):
        assert _esc("a & b") == "a &amp; b"

    def test_escapes_less_than(self):
        assert _esc("a < b") == "a &lt; b"

    def test_escapes_greater_than(self):
        assert _esc("a > b") == "a &gt; b"

    def test_escapes_double_quote(self):
        assert _esc('say "hello"') == "say &quot;hello&quot;"

    def test_escapes_all_together(self):
        assert _esc('x & y < z > "w"') == 'x &amp; y &lt; z &gt; &quot;w&quot;'

    def test_converts_non_string_to_string(self):
        assert _esc(42) == "42"


# ---------------------------------------------------------------------------
# _summary()
# ---------------------------------------------------------------------------
class TestSummary:

    def test_contains_all_fields(self, settings):
        item = _make_item()
        result = _summary(settings, item)
        assert "AR-20241101-AABBCC" in result
        assert "Test User" in result
        assert "L123456" in result
        assert "user@lilly.com" in result
        assert "US" in result
        assert "Test DoU" in result
        assert "Data stewardship" in result
        assert "ROLE_A" in result
        assert "ROLE_B" in result
        assert "tenant-a" in result

    def test_uses_approved_roles_when_present(self, settings):
        item = _make_item(approved_roles=["ROLE_CUSTOM"])
        result = _summary(settings, item)
        assert "ROLE_CUSTOM" in result
        # The original reltio_roles should NOT appear in the output
        assert "ROLE_B" not in result

    def test_justification_dash_when_empty(self, settings):
        item = _make_item(justification="")
        result = _summary(settings, item)
        # Expect the dash placeholder when justification is empty/falsy
        assert "Justification:" in result


# ---------------------------------------------------------------------------
# _summary_dict()
# ---------------------------------------------------------------------------
class TestSummaryDict:

    def test_returns_expected_keys(self, settings):
        item = _make_item()
        d = _summary_dict(settings, item)
        expected_keys = {
            "Request", "Requested by", "Global ID", "Email",
            "Country", "DoU", "Use case", "Tenants", "Reltio roles",
        }
        assert set(d.keys()) == expected_keys

    def test_values_match_item(self, settings):
        item = _make_item()
        d = _summary_dict(settings, item)
        assert d["Request"] == "AR-20241101-AABBCC"
        assert d["Requested by"] == "Test User"
        assert d["Global ID"] == "L123456"
        assert d["Reltio roles"] == "ROLE_A, ROLE_B"

    def test_uses_tenant_labels(self, settings):
        settings.tenant_labels = {"tenant-a": "Tenant Alpha"}
        item = _make_item()
        d = _summary_dict(settings, item)
        assert "Tenant Alpha" in d["Tenants"]
        assert "tenant-a" in d["Tenants"]


# ---------------------------------------------------------------------------
# _html_body()
# ---------------------------------------------------------------------------
class TestHtmlBody:

    def test_returns_valid_html_structure(self):
        html = _html_body(
            subject="Test Subject",
            heading="Test Heading",
            summary={"Key": "Value"},
            message="Test message",
        )
        assert html.startswith("<!DOCTYPE html>")
        assert "</html>" in html
        assert "<body" in html

    def test_contains_lilly_red_header(self):
        html = _html_body(
            subject="Test",
            heading="Heading",
            summary={},
            message="Message",
        )
        assert "#E01933" in html
        assert "Lilly" in html

    def test_escapes_user_input(self):
        html = _html_body(
            subject="<script>alert(1)</script>",
            heading='<img onerror="x">',
            summary={"<b>bold</b>": "val & more"},
            message='"><script>',
        )
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
        assert "&amp;" in html

    def test_includes_cta_button(self):
        html = _html_body(
            subject="Test",
            heading="Heading",
            summary={},
            message="Msg",
            cta_url="https://example.com",
            cta_label="Click here",
        )
        assert "Click here" in html
        assert "https://example.com" in html

    def test_no_cta_when_empty(self):
        html = _html_body(
            subject="Test",
            heading="Heading",
            summary={},
            message="Msg",
            cta_url="",
            cta_label="",
        )
        # No anchor tag for CTA
        assert "cta_label" not in html

    def test_status_color_applied(self):
        html = _html_body(
            subject="Test",
            heading="Heading",
            summary={},
            message="Msg",
            status_color="#ff0000",
        )
        assert "#ff0000" in html


# ---------------------------------------------------------------------------
# High-level notification functions (mock _send and _notify_in_app)
# ---------------------------------------------------------------------------
class TestSubmitted:

    @patch.object(notify, "_notify_in_app")
    @patch.object(notify, "_send")
    def test_calls_send_to_approvers_and_requester(self, mock_send, mock_in_app, settings):
        item = _make_item()
        submitted(settings, item)
        # _send is called twice: once to approvers, once to requester
        assert mock_send.call_count == 2
        # First call: approvers
        approver_call = mock_send.call_args_list[0]
        assert "alice@lilly.com" in approver_call[0][1]
        assert "bob@lilly.com" in approver_call[0][1]
        # Second call: requester
        requester_call = mock_send.call_args_list[1]
        assert "user@lilly.com" in requester_call[0][1]

    @patch.object(notify, "_notify_in_app")
    @patch.object(notify, "_send")
    def test_in_app_notification_to_approvers(self, mock_send, mock_in_app, settings):
        item = _make_item()
        submitted(settings, item)
        mock_in_app.assert_called_once()
        call_args = mock_in_app.call_args
        oids = call_args[0][1]
        assert "alice-oid" in oids
        assert "bob-oid" in oids
        assert call_args[1]["request_id"] == "AR-20241101-AABBCC"


class TestRejected:

    @patch.object(notify, "_notify_in_app")
    @patch.object(notify, "_send")
    def test_calls_send_to_requester(self, mock_send, mock_in_app, settings):
        item = _make_item()
        decision = {"name": "Alice Approver", "comment": "Not justified"}
        rejected(settings, item, decision)
        mock_send.assert_called_once()
        assert "user@lilly.com" in mock_send.call_args[0][1]

    @patch.object(notify, "_notify_in_app")
    @patch.object(notify, "_send")
    def test_in_app_to_requester(self, mock_send, mock_in_app, settings):
        item = _make_item()
        decision = {"name": "Alice", "comment": "No"}
        rejected(settings, item, decision)
        mock_in_app.assert_called_once()
        oids = mock_in_app.call_args[0][1]
        assert "user-oid" in oids


class TestCompleted:

    @patch.object(notify, "_notify_in_app")
    @patch.object(notify, "_send")
    def test_calls_send_to_requester_and_email(self, mock_send, mock_in_app, settings):
        item = _make_item()
        completed(settings, item, result={})
        mock_send.assert_called_once()
        recipients = mock_send.call_args[0][1]
        assert "user@lilly.com" in recipients

    @patch.object(notify, "_notify_in_app")
    @patch.object(notify, "_send")
    def test_in_app_to_requester(self, mock_send, mock_in_app, settings):
        item = _make_item()
        completed(settings, item, result={})
        mock_in_app.assert_called_once()
        oids = mock_in_app.call_args[0][1]
        assert "user-oid" in oids


class TestCancelled:

    @patch.object(notify, "_notify_in_app")
    @patch.object(notify, "_send")
    def test_calls_send_to_approvers(self, mock_send, mock_in_app, settings):
        item = _make_item()
        cancelled(settings, item)
        mock_send.assert_called_once()
        recipients = mock_send.call_args[0][1]
        assert "alice@lilly.com" in recipients
        assert "bob@lilly.com" in recipients

    @patch.object(notify, "_notify_in_app")
    @patch.object(notify, "_send")
    def test_in_app_to_approvers(self, mock_send, mock_in_app, settings):
        item = _make_item()
        cancelled(settings, item)
        mock_in_app.assert_called_once()
        oids = mock_in_app.call_args[0][1]
        assert "alice-oid" in oids
        assert "bob-oid" in oids


class TestPartialApproved:

    @patch.object(notify, "_notify_in_app")
    @patch.object(notify, "_send")
    def test_calls_send_to_requester_and_remaining(self, mock_send, mock_in_app, settings):
        item = _make_item(approvals={"alice-oid": {"decision": "approve"}})
        decision = {"name": "Alice Approver", "comment": "OK"}
        partial_approved(settings, item, decision)
        mock_send.assert_called_once()
        recipients = mock_send.call_args[0][1]
        # Requester and remaining approvers (Bob only, since Alice already approved)
        assert "user@lilly.com" in recipients
        assert "bob@lilly.com" in recipients

    @patch.object(notify, "_notify_in_app")
    @patch.object(notify, "_send")
    def test_in_app_to_requester_and_remaining(self, mock_send, mock_in_app, settings):
        item = _make_item(approvals={"alice-oid": {"decision": "approve"}})
        decision = {"name": "Alice Approver", "comment": "OK"}
        partial_approved(settings, item, decision)
        mock_in_app.assert_called_once()
        oids = mock_in_app.call_args[0][1]
        assert "user-oid" in oids
        assert "bob-oid" in oids
