"""Tests for app.audit (structured audit logging)."""
from __future__ import annotations

import json
import logging

import pytest

from app.audit import event
from app.auth import User


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def user():
    return User(oid="actor-oid", upn="actor@lilly.com", name="Actor Name")


# ---------------------------------------------------------------------------
# event()
# ---------------------------------------------------------------------------
class TestEvent:

    def test_emits_structured_json_log(self, caplog, user):
        with caplog.at_level(logging.INFO, logger="reltio_access.audit"):
            event(user, action="submit", request_id="AR-001")

        assert len(caplog.records) == 1
        payload = json.loads(caplog.records[0].message)
        assert payload["event"] == "reltio_access_request"
        assert payload["action"] == "submit"
        assert payload["request_id"] == "AR-001"
        assert payload["actor_oid"] == "actor-oid"
        assert payload["actor_upn"] == "actor@lilly.com"

    def test_includes_extra_details(self, caplog, user):
        with caplog.at_level(logging.INFO, logger="reltio_access.audit"):
            event(user, action="approve", request_id="AR-002",
                  comment="Looks good", decision="approve")

        payload = json.loads(caplog.records[0].message)
        assert payload["comment"] == "Looks good"
        assert payload["decision"] == "approve"

    def test_uses_correct_logger(self, caplog, user):
        with caplog.at_level(logging.INFO, logger="reltio_access.audit"):
            event(user, action="reject", request_id="AR-003")

        assert caplog.records[0].name == "reltio_access.audit"

    def test_handles_actor_without_attributes(self, caplog):
        """If actor is not a User but some other object, getattr returns None."""

        class MinimalActor:
            pass

        actor = MinimalActor()
        with caplog.at_level(logging.INFO, logger="reltio_access.audit"):
            event(actor, action="system_retry", request_id="AR-004")

        payload = json.loads(caplog.records[0].message)
        assert payload["actor_oid"] is None
        assert payload["actor_upn"] is None
        assert payload["action"] == "system_retry"

    def test_serializes_non_string_details(self, caplog, user):
        """Extra detail values that aren't strings should be serialized via default=str."""
        with caplog.at_level(logging.INFO, logger="reltio_access.audit"):
            event(user, action="provision", request_id="AR-005",
                  tenant_count=3, roles=["ROLE_A", "ROLE_B"])

        payload = json.loads(caplog.records[0].message)
        assert payload["tenant_count"] == 3
        assert payload["roles"] == ["ROLE_A", "ROLE_B"]

    def test_log_level_is_info(self, caplog, user):
        with caplog.at_level(logging.DEBUG, logger="reltio_access.audit"):
            event(user, action="cancel", request_id="AR-006")

        assert caplog.records[0].levelno == logging.INFO
