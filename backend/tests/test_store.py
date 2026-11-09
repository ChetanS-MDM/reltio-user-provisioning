"""Tests for app.store (DynamoDB persistence layer)."""
import pytest
from moto import mock_aws

from app import store
from app.store import (
    PENDING,
    PROVISIONING,
    COMPLETED,
    Conflict,
    get_request,
    list_by_requester,
    list_by_status,
    now_iso,
    put_request,
    record_decision,
    transition,
)


def _make_item(request_id="AR-20241101-AABBCC", requester_oid="user-oid",
               status=PENDING, **overrides):
    """Build a minimal valid request item."""
    ts = now_iso()
    item = {
        "request_id": request_id,
        "status": status,
        "requester_oid": requester_oid,
        "created_at": ts,
        "updated_at": ts,
        "global_id": "L123456",
        "email": "user@lilly.com",
        "country_code": "US",
        "dou": "Test DoU",
        "tenants": ["tenant-a"],
        "reltio_roles": ["ROLE_A"],
        "approvals": {},
    }
    item.update(overrides)
    return item


# ---------------------------------------------------------------------------
# put_request + get_request roundtrip
# ---------------------------------------------------------------------------
@mock_aws
def test_put_and_get_roundtrip(settings, dynamodb_table):
    item = _make_item()
    put_request(settings, item)
    fetched = get_request(settings, item["request_id"])
    assert fetched is not None
    assert fetched["request_id"] == item["request_id"]
    assert fetched["status"] == PENDING
    assert fetched["global_id"] == "L123456"


# ---------------------------------------------------------------------------
# put_request duplicate raises ConditionalCheckFailedException
# ---------------------------------------------------------------------------
@mock_aws
def test_put_duplicate_raises(settings, dynamodb_table):
    item = _make_item()
    put_request(settings, item)
    from botocore.exceptions import ClientError
    with pytest.raises(ClientError, match="ConditionalCheckFailedException"):
        put_request(settings, item)


# ---------------------------------------------------------------------------
# list_by_requester
# ---------------------------------------------------------------------------
@mock_aws
def test_list_by_requester(settings, dynamodb_table):
    put_request(settings, _make_item(request_id="AR-01", requester_oid="user-a", created_at="2024-01-01T00:00:00+00:00"))
    put_request(settings, _make_item(request_id="AR-02", requester_oid="user-a", created_at="2024-01-02T00:00:00+00:00"))
    put_request(settings, _make_item(request_id="AR-03", requester_oid="user-b", created_at="2024-01-03T00:00:00+00:00"))

    results = list_by_requester(settings, "user-a")
    ids = [r["request_id"] for r in results]
    assert "AR-01" in ids
    assert "AR-02" in ids
    assert "AR-03" not in ids


# ---------------------------------------------------------------------------
# list_by_status
# ---------------------------------------------------------------------------
@mock_aws
def test_list_by_status(settings, dynamodb_table):
    put_request(settings, _make_item(request_id="AR-P1", status=PENDING, created_at="2024-01-01T00:00:00+00:00"))
    put_request(settings, _make_item(request_id="AR-C1", status=COMPLETED, created_at="2024-01-02T00:00:00+00:00"))

    pending = list_by_status(settings, PENDING)
    ids = [r["request_id"] for r in pending]
    assert "AR-P1" in ids
    assert "AR-C1" not in ids


# ---------------------------------------------------------------------------
# record_decision
# ---------------------------------------------------------------------------
@mock_aws
def test_record_decision_approve(settings, dynamodb_table):
    item = _make_item()
    put_request(settings, item)

    decision = {"decision": "approve", "comment": "Looks good."}
    updated = record_decision(settings, item["request_id"], "alice-oid", decision)

    assert updated["approvals"]["alice-oid"]["decision"] == "approve"
    # An approval alone does not change status (it stays PENDING until all approve).
    assert updated["status"] == PENDING


@mock_aws
def test_record_decision_reject_changes_status(settings, dynamodb_table):
    item = _make_item()
    put_request(settings, item)

    decision = {"decision": "reject", "comment": "Denied."}
    updated = record_decision(settings, item["request_id"], "alice-oid", decision)

    assert updated["status"] == "REJECTED"


@mock_aws
def test_record_decision_duplicate_raises_conflict(settings, dynamodb_table):
    item = _make_item()
    put_request(settings, item)

    decision = {"decision": "approve", "comment": ""}
    record_decision(settings, item["request_id"], "alice-oid", decision)

    with pytest.raises(Conflict, match="already"):
        record_decision(settings, item["request_id"], "alice-oid", decision)


# ---------------------------------------------------------------------------
# transition
# ---------------------------------------------------------------------------
@mock_aws
def test_transition_success(settings, dynamodb_table):
    item = _make_item(status=PENDING)
    put_request(settings, item)

    ok = transition(settings, item["request_id"], from_status=PENDING, to_status=PROVISIONING)
    assert ok is True

    fetched = get_request(settings, item["request_id"])
    assert fetched["status"] == PROVISIONING


@mock_aws
def test_transition_wrong_current_status(settings, dynamodb_table):
    item = _make_item(status=COMPLETED)
    put_request(settings, item)

    ok = transition(settings, item["request_id"], from_status=PENDING, to_status=PROVISIONING)
    assert ok is False

    # Status should be unchanged.
    fetched = get_request(settings, item["request_id"])
    assert fetched["status"] == COMPLETED
