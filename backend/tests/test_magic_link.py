"""Tests for magic-link email approval flow."""
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio
import httpx
from httpx import ASGITransport

os.environ.setdefault("ENTRA_TENANT_ID", "test-tenant")
os.environ.setdefault("ENTRA_API_AUDIENCE", "test-audience")
os.environ.setdefault("ROLES_MATRIX_LOCAL_PATH",
                       str(Path(__file__).resolve().parent.parent / "roles_matrix.sample.json"))

from app.auth import User, require_user
from app.config import Settings, get_settings
from app.main import app
from app import store, reltio, notifications, attachments, notify, provisioning, matrix
from app.magic_link import (create_token, verify_token, TokenError,
                            create_confirmation_token, verify_confirmation_token)

_MAGIC_SECRET = "test-magic-secret-key-for-hmac-signing"
_FAKE_TENANTS = ["tenant-a"]

_FAKE_CUSTOM_ROLES = [
    "ROLE_LILLY_EPH_GLOBAL_GCCP",
    "ROLE_LILLY_EPH_US_BASICSEARCH",
    "ROLE_LILLY_EPH_GLOBAL_ITANALYST_READONLY",
]

_DB: dict = {}
_NOTIFS: dict = {}
_UPLOADS: dict = {}


def _put_request(cfg, item):
    if item["request_id"] in _DB:
        raise store.Conflict("Duplicate request ID.")
    _DB[item["request_id"]] = dict(item)


def _get_request(cfg, request_id):
    item = _DB.get(request_id)
    return dict(item) if item else None


def _list_by_requester(cfg, oid, limit=50):
    return sorted([dict(i) for i in _DB.values() if i["requester_oid"] == oid],
                  key=lambda i: i["created_at"], reverse=True)[:limit]


def _list_by_status(cfg, status, limit=50):
    return sorted([dict(i) for i in _DB.values() if i["status"] == status],
                  key=lambda i: i["created_at"], reverse=True)[:limit]


async def _list_recent(cfg, limit=30):
    return sorted([dict(i) for i in _DB.values() if i["status"] in store.CLOSED],
                  key=lambda i: i.get("updated_at", ""), reverse=True)[:limit]


def _record_decision(cfg, request_id, approver, decision):
    item = _DB[request_id]
    if item["status"] != store.PENDING or approver in (item.get("approvals") or {}):
        raise store.Conflict("Already decided or you've already recorded a decision.")
    item.setdefault("approvals", {})[approver] = decision
    item["updated_at"] = store.now_iso()
    if decision["decision"] == "reject":
        item["status"] = store.REJECTED
    return dict(item)


def _transition(cfg, request_id, from_status, to_status, **fields):
    item = _DB.get(request_id)
    if not item or item["status"] != from_status:
        return False
    item["status"] = to_status
    item["updated_at"] = store.now_iso()
    item.update(fields)
    return True


async def _fake_get_customer_metadata(cfg):
    return {"tenants": list(_FAKE_TENANTS), "customer_specific_roles": list(_FAKE_CUSTOM_ROLES)}


async def _fake_get_valid_role_names(cfg):
    return set(_FAKE_CUSTOM_ROLES) | set(cfg.system_roles)


def _notif_put(cfg, recipient_oid, title, body, request_id=None, link=None):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _NOTIFS.setdefault(recipient_oid, []).insert(0, {
        "recipient_oid": recipient_oid, "sort_key": f"{now}#{secrets.token_hex(4)}",
        "title": title, "body": body, "request_id": request_id or "",
        "link": link or "", "read": False, "created_at": now,
    })


def _notif_list_recent(cfg, recipient_oid, limit=30):
    return list(_NOTIFS.get(recipient_oid, []))[:limit]


def _notif_count_unread(cfg, recipient_oid):
    return sum(1 for n in _NOTIFS.get(recipient_oid, []) if not n["read"])


async def _notif_mark_all_read(cfg, recipient_oid):
    items = _NOTIFS.get(recipient_oid, [])
    count = sum(1 for n in items if not n["read"])
    for n in items:
        n["read"] = True
    return count


def _presign_upload(cfg, user, filename, size):
    key = f"uploads/{user.oid}/{secrets.token_hex(4)}/{filename}"
    _UPLOADS[key] = {"filename": filename, "size": size}
    return {"url": "http://test/_fake_upload", "fields": {"key": key}, "key": key}


def _verify_upload(cfg, user, key):
    if not key.startswith(f"uploads/{user.oid}/"):
        raise ValueError("That attachment wasn't uploaded by you.")
    info = _UPLOADS.get(key) or {"filename": key.rsplit("/", 1)[-1], "size": 1024}
    return {"key": key, "filename": info["filename"], "size": info["size"],
            "content_type": "application/pdf"}


def _presign_download(cfg, key, filename):
    return f"https://fake-s3/{key}?filename={filename}"


async def _fake_provision(cfg, item):
    roles = item.get("approved_roles") or item["reltio_roles"]
    return {"action": "created", "added": {r: list(item["tenants"]) for r in roles},
            "already_had": {}, "warnings": []}


@pytest.fixture()
def ml_settings():
    return Settings(
        entra_tenant_id="test-tenant",
        entra_api_audience="test-audience",
        requests_table="TestRequests",
        attachments_bucket="test-bucket",
        approval_policy="any",
        approvers=[
            {"name": "Alice Approver", "upn": "alice@lilly.com", "oid": "alice-oid"},
        ],
        roles_matrix_local_path=str(Path(__file__).resolve().parent.parent / "roles_matrix.sample.json"),
        reltio_client_id="test-id",
        reltio_client_secret="test-secret",
        auth_base_url="https://mock-reltio",
        notify_sender="noreply@lilly.com",
        magic_link_secret=_MAGIC_SECRET,
        api_base_url="http://localhost:8000",
    )


@pytest.fixture()
def fake_user():
    return User(oid="user-oid", upn="user@lilly.com", name="Test User")


@pytest.fixture()
def approver_user():
    return User(oid="alice-oid", upn="alice@lilly.com", name="Alice Approver")


@pytest.fixture(autouse=True)
def _wire(ml_settings, fake_user):
    _DB.clear()
    _NOTIFS.clear()
    _UPLOADS.clear()

    patches = [
        (store, "put_request", _put_request),
        (store, "get_request", _get_request),
        (store, "list_by_requester", _list_by_requester),
        (store, "list_by_status", _list_by_status),
        (store, "list_recent", _list_recent),
        (store, "record_decision", _record_decision),
        (store, "transition", _transition),
        (reltio, "get_customer_metadata", _fake_get_customer_metadata),
        (reltio, "get_valid_role_names", _fake_get_valid_role_names),
        (notifications, "put", _notif_put),
        (notifications, "list_recent", _notif_list_recent),
        (notifications, "count_unread", _notif_count_unread),
        (notifications, "mark_all_read", _notif_mark_all_read),
        (attachments, "presign_upload", _presign_upload),
        (attachments, "verify_upload", _verify_upload),
        (attachments, "presign_download", _presign_download),
        (notify, "_send", lambda *a, **kw: None),
        (provisioning, "provision", _fake_provision),
    ]

    originals = [(mod, name, getattr(mod, name)) for mod, name, _ in patches]
    for mod, name, fake in patches:
        setattr(mod, name, fake)

    get_settings.cache_clear()
    matrix._matrix_cache.update({"index": None, "version": None, "loaded_at": 0.0})

    app.dependency_overrides[require_user] = lambda: fake_user
    app.dependency_overrides[get_settings] = lambda: ml_settings

    yield

    for mod, name, orig_val in originals:
        setattr(mod, name, orig_val)
    app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest_asyncio.fixture()
async def client():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _make_pending_item(request_id="AR-20260101-ABC123"):
    now = store.now_iso()
    item = {
        "request_id": request_id,
        "status": store.PENDING,
        "created_at": now,
        "updated_at": now,
        "requester_oid": "user-oid",
        "requester_upn": "user@lilly.com",
        "requester_name": "Test User",
        "global_id": "L999999",
        "email": "testuser@lilly.com",
        "country_code": "US",
        "dou": "~2578 Enterprise Profile-DOU-Global EPH IT Analyst Read Only V18",
        "use_case": "EPH IT Analyst Read Only",
        "justification": "Need access for testing",
        "reltio_roles": ["ROLE_LILLY_EPH_GLOBAL_ITANALYST_READONLY", "ROLE_API", "ROLE_UI", "ROLE_USER"],
        "ecdp_roles": [],
        "tenants": ["tenant-a"],
        "attachment": {"key": "uploads/user-oid/abc/approval.pdf", "filename": "approval.pdf", "size": 42000,
                       "content_type": "application/pdf"},
        "required_approvers": [
            {"key": "alice-oid", "name": "Alice Approver", "upn": "alice@lilly.com", "oid": "alice-oid"},
        ],
        "policy": "any",
        "approvals": {},
        "matrix_version": "v1",
        "result": None,
        "error": None,
    }
    _DB[request_id] = item
    return item


# ===== Unit tests for token create/verify =====

class TestTokenCreateVerify:
    def test_round_trip(self):
        token = create_token(_MAGIC_SECRET, "AR-20260101-ABC123", "alice-oid", 7)
        info = verify_token(_MAGIC_SECRET, token)
        assert info["request_id"] == "AR-20260101-ABC123"
        assert info["approver_oid"] == "alice-oid"

    def test_expired_token(self):
        token = create_token(_MAGIC_SECRET, "AR-20260101-ABC123", "alice-oid", -1)
        with pytest.raises(TokenError, match="expired"):
            verify_token(_MAGIC_SECRET, token)

    def test_tampered_token(self):
        token = create_token(_MAGIC_SECRET, "AR-20260101-ABC123", "alice-oid", 7)
        tampered = token[:-4] + "XXXX"
        with pytest.raises(TokenError, match="Invalid"):
            verify_token(_MAGIC_SECRET, tampered)

    def test_wrong_secret(self):
        token = create_token(_MAGIC_SECRET, "AR-20260101-ABC123", "alice-oid", 7)
        with pytest.raises(TokenError, match="Invalid"):
            verify_token("wrong-secret", token)


# ===== Route integration tests =====

class TestEmailReview:
    @pytest.mark.asyncio
    async def test_review_page_renders(self, client):
        item = _make_pending_item()
        token = create_token(_MAGIC_SECRET, item["request_id"], "alice-oid", 7)
        resp = await client.get(f"/api/email-review/{token}")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "Access request needs your approval" in resp.text
        assert item["global_id"] in resp.text
        assert item["requester_name"] in resp.text

    @pytest.mark.asyncio
    async def test_review_page_expired_token(self, client):
        _make_pending_item()
        token = create_token(_MAGIC_SECRET, "AR-20260101-ABC123", "alice-oid", -1)
        resp = await client.get(f"/api/email-review/{token}")
        assert resp.status_code == 400
        assert "expired" in resp.text.lower()

    @pytest.mark.asyncio
    async def test_review_page_invalid_token(self, client):
        resp = await client.get("/api/email-review/garbage-token")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_review_page_not_found(self, client):
        token = create_token(_MAGIC_SECRET, "AR-99999999-XXXXXX", "alice-oid", 7)
        resp = await client.get(f"/api/email-review/{token}")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_review_page_wrong_approver(self, client):
        _make_pending_item()
        token = create_token(_MAGIC_SECRET, "AR-20260101-ABC123", "wrong-oid", 7)
        resp = await client.get(f"/api/email-review/{token}")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_review_shows_already_decided(self, client):
        item = _make_pending_item()
        item["approvals"]["alice-oid"] = {"decision": "approve", "name": "Alice", "comment": "", "at": store.now_iso()}
        token = create_token(_MAGIC_SECRET, item["request_id"], "alice-oid", 7)
        resp = await client.get(f"/api/email-review/{token}")
        assert resp.status_code == 200
        assert "already" in resp.text.lower()


class TestEmailAttachment:
    @pytest.mark.asyncio
    async def test_attachment_redirect(self, client):
        item = _make_pending_item()
        token = create_token(_MAGIC_SECRET, item["request_id"], "alice-oid", 7)
        resp = await client.get(f"/api/email-attachment/{token}", follow_redirects=False)
        assert resp.status_code == 307
        assert "fake-s3" in resp.headers["location"]

    @pytest.mark.asyncio
    async def test_attachment_bad_token(self, client):
        resp = await client.get("/api/email-attachment/invalid-token")
        assert resp.status_code == 400


class TestEmailDecision:
    @pytest.mark.asyncio
    async def test_approve_sends_confirmation(self, client):
        item = _make_pending_item()
        token = create_token(_MAGIC_SECRET, item["request_id"], "alice-oid", 7)
        resp = await client.post("/api/email-decision", json={
            "token": token, "decision": "approve",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "confirmation_sent"
        assert data["decision"] == "approve"
        assert "masked_email" in data
        updated = _DB[item["request_id"]]
        assert "alice-oid" not in updated.get("approvals", {})

    @pytest.mark.asyncio
    async def test_reject_sends_confirmation(self, client):
        item = _make_pending_item()
        token = create_token(_MAGIC_SECRET, item["request_id"], "alice-oid", 7)
        resp = await client.post("/api/email-decision", json={
            "token": token, "decision": "reject", "comment": "Not justified",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "confirmation_sent"
        assert data["decision"] == "reject"
        updated = _DB[item["request_id"]]
        assert updated["status"] == store.PENDING

    @pytest.mark.asyncio
    async def test_reject_requires_comment(self, client):
        item = _make_pending_item()
        token = create_token(_MAGIC_SECRET, item["request_id"], "alice-oid", 7)
        resp = await client.post("/api/email-decision", json={
            "token": token, "decision": "reject",
        })
        assert resp.status_code == 400
        assert "comment" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_multiple_confirmation_requests_ok(self, client):
        item = _make_pending_item()
        token = create_token(_MAGIC_SECRET, item["request_id"], "alice-oid", 7)
        resp1 = await client.post("/api/email-decision", json={
            "token": token, "decision": "approve",
        })
        assert resp1.status_code == 200
        resp2 = await client.post("/api/email-decision", json={
            "token": token, "decision": "approve",
        })
        assert resp2.status_code == 200

    @pytest.mark.asyncio
    async def test_expired_token_rejected(self, client):
        _make_pending_item()
        token = create_token(_MAGIC_SECRET, "AR-20260101-ABC123", "alice-oid", -1)
        resp = await client.post("/api/email-decision", json={
            "token": token, "decision": "approve",
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_wrong_approver_rejected(self, client):
        _make_pending_item()
        token = create_token(_MAGIC_SECRET, "AR-20260101-ABC123", "wrong-oid", 7)
        resp = await client.post("/api/email-decision", json={
            "token": token, "decision": "approve",
        })
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_not_pending_rejected(self, client):
        item = _make_pending_item()
        item["status"] = store.COMPLETED
        token = create_token(_MAGIC_SECRET, item["request_id"], "alice-oid", 7)
        resp = await client.post("/api/email-decision", json={
            "token": token, "decision": "approve",
        })
        assert resp.status_code == 409


# ===== Confirmation token unit tests =====

class TestConfirmationToken:
    def test_round_trip(self):
        token = create_confirmation_token(
            _MAGIC_SECRET, "AR-20260101-ABC123", "alice-oid", "approve", "Looks good")
        info = verify_confirmation_token(_MAGIC_SECRET, token)
        assert info["request_id"] == "AR-20260101-ABC123"
        assert info["approver_oid"] == "alice-oid"
        assert info["decision"] == "approve"
        assert info["comment"] == "Looks good"

    def test_reject_round_trip(self):
        token = create_confirmation_token(
            _MAGIC_SECRET, "AR-20260101-ABC123", "alice-oid", "reject", "Not justified")
        info = verify_confirmation_token(_MAGIC_SECRET, token)
        assert info["decision"] == "reject"
        assert info["comment"] == "Not justified"

    def test_expired_confirmation(self):
        token = create_confirmation_token(
            _MAGIC_SECRET, "AR-20260101-ABC123", "alice-oid", "approve", "", expiry_minutes=-1)
        with pytest.raises(TokenError, match="expired"):
            verify_confirmation_token(_MAGIC_SECRET, token)

    def test_wrong_secret(self):
        token = create_confirmation_token(
            _MAGIC_SECRET, "AR-20260101-ABC123", "alice-oid", "approve", "")
        with pytest.raises(TokenError, match="Invalid"):
            verify_confirmation_token("wrong-secret", token)

    def test_regular_token_rejected_as_confirmation(self):
        token = create_token(_MAGIC_SECRET, "AR-20260101-ABC123", "alice-oid", 7)
        with pytest.raises(TokenError, match="Invalid"):
            verify_confirmation_token(_MAGIC_SECRET, token)


# ===== Email confirm endpoint tests =====

class TestEmailConfirm:
    @pytest.mark.asyncio
    async def test_confirm_approve(self, client):
        item = _make_pending_item()
        confirm_token = create_confirmation_token(
            _MAGIC_SECRET, item["request_id"], "alice-oid", "approve", "Looks good")
        resp = await client.get(f"/api/email-confirm/{confirm_token}")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "Approved" in resp.text
        assert "Decision confirmed" in resp.text
        updated = _DB[item["request_id"]]
        assert "alice-oid" in updated["approvals"]
        assert updated["approvals"]["alice-oid"]["decision"] == "approve"

    @pytest.mark.asyncio
    async def test_confirm_reject(self, client):
        item = _make_pending_item()
        confirm_token = create_confirmation_token(
            _MAGIC_SECRET, item["request_id"], "alice-oid", "reject", "Not justified")
        resp = await client.get(f"/api/email-confirm/{confirm_token}")
        assert resp.status_code == 200
        assert "Rejected" in resp.text
        updated = _DB[item["request_id"]]
        assert updated["status"] == store.REJECTED

    @pytest.mark.asyncio
    async def test_confirm_expired_token(self, client):
        _make_pending_item()
        confirm_token = create_confirmation_token(
            _MAGIC_SECRET, "AR-20260101-ABC123", "alice-oid", "approve", "",
            expiry_minutes=-1)
        resp = await client.get(f"/api/email-confirm/{confirm_token}")
        assert resp.status_code == 400
        assert "expired" in resp.text.lower()

    @pytest.mark.asyncio
    async def test_confirm_wrong_approver(self, client):
        _make_pending_item()
        confirm_token = create_confirmation_token(
            _MAGIC_SECRET, "AR-20260101-ABC123", "wrong-oid", "approve", "")
        resp = await client.get(f"/api/email-confirm/{confirm_token}")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_confirm_not_pending(self, client):
        item = _make_pending_item()
        item["status"] = store.COMPLETED
        confirm_token = create_confirmation_token(
            _MAGIC_SECRET, item["request_id"], "alice-oid", "approve", "")
        resp = await client.get(f"/api/email-confirm/{confirm_token}")
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_confirm_double_decision(self, client):
        item = _make_pending_item()
        confirm_token = create_confirmation_token(
            _MAGIC_SECRET, item["request_id"], "alice-oid", "approve", "")
        resp1 = await client.get(f"/api/email-confirm/{confirm_token}")
        assert resp1.status_code == 200
        confirm_token2 = create_confirmation_token(
            _MAGIC_SECRET, item["request_id"], "alice-oid", "approve", "")
        resp2 = await client.get(f"/api/email-confirm/{confirm_token2}")
        assert resp2.status_code == 409

    @pytest.mark.asyncio
    async def test_confirm_request_not_found(self, client):
        confirm_token = create_confirmation_token(
            _MAGIC_SECRET, "AR-99999999-XXXXXX", "alice-oid", "approve", "")
        resp = await client.get(f"/api/email-confirm/{confirm_token}")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_full_flow_decision_then_confirm(self, client):
        """End-to-end: email-decision sends confirmation, then email-confirm finalizes."""
        item = _make_pending_item()
        token = create_token(_MAGIC_SECRET, item["request_id"], "alice-oid", 7)
        resp = await client.post("/api/email-decision", json={
            "token": token, "decision": "approve",
        })
        assert resp.json()["status"] == "confirmation_sent"
        assert _DB[item["request_id"]]["status"] == store.PENDING
        assert "alice-oid" not in _DB[item["request_id"]].get("approvals", {})

        confirm_token = create_confirmation_token(
            _MAGIC_SECRET, item["request_id"], "alice-oid", "approve", "")
        resp2 = await client.get(f"/api/email-confirm/{confirm_token}")
        assert resp2.status_code == 200
        assert "Approved" in resp2.text
        updated = _DB[item["request_id"]]
        assert "alice-oid" in updated["approvals"]
