"""Route integration tests for the Reltio User Provisioning FastAPI backend.

Each route is exercised through the real FastAPI router with in-memory fakes
for DynamoDB, S3, Reltio API, and email/notification services.  The roles
matrix is resolved from the sample file on disk (the real ``matrix.resolve``
runs, so DoU parsing and role expansion are tested end-to-end).
"""

import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio
import httpx
from httpx import ASGITransport

# ---------------------------------------------------------------------------
# Environment -- must be set before any app module import so that
# pydantic-settings and the module-level ``get_settings()`` in main.py
# can construct a valid Settings object.
# ---------------------------------------------------------------------------
os.environ.setdefault("ENTRA_TENANT_ID", "test-tenant")
os.environ.setdefault("ENTRA_API_AUDIENCE", "test-audience")
os.environ.setdefault("ROLES_MATRIX_LOCAL_PATH",
                       str(Path(__file__).resolve().parent.parent / "roles_matrix.sample.json"))

from app.auth import User, require_user  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402
from app import (  # noqa: E402
    store, reltio, notifications, attachments, notify, provisioning, matrix,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_FAKE_TENANTS = ["tenant-a", "tenant-b"]

# Customer-specific roles that appear in roles_matrix.sample.json.  The fake
# ``get_valid_role_names`` returns these plus the system roles from Settings.
_FAKE_CUSTOM_ROLES = [
    "ROLE_LILLY_EPH_GLOBAL_GCCP",
    "ROLE_LILLY_EPH_US_BASICSEARCH",
    "ROLE_LILLY_EPH_GLOBAL_COMSEARCH",
    "ROLE_LILLY_EPH_EXPORT_NOEMAIL",
    "ROLE_LILLY_EPH_GLOBAL_CODS",
    "ROLE_LILLY_EPH_GLOBAL_CODS_V2",
    "ROLE_LILLY_EPH_GLOBAL_DS",
    "ROLE_LILLY_EPH_GLOBAL_DC",
    "ROLE_LILLY_EPH_GLOBAL_ITANALYST",
    "ROLE_LILLY_EPH_GLOBAL_ITANALYST_READONLY",
    "ROLE_LILLY_EPH_GLOBAL_GCC",
    "ROLE_LILLY_EPH_GLOBAL_WEBSITEREG",
    "ROLE_LILLY_EPH_GLOBAL_LSC",
    "ROLE_LILLY_EPH_GLOBAL_MIH",
    "ROLE_LILLY_EPH_GLOBAL_LEGAL",
    "ROLE_LILLY_EPH_GLOBAL_TRANSPARENCY_REPORTING",
    "ROLE_LILLY_EPH_GLOBAL_ETHICS_COMPLIANCE",
    "ROLE_USER_ML_MATCH",
    "ROLE_VEEVA_CRM",
    "ROLE_LILLY_EPH_PHYSICIAN_EXCLUSION",
    "ROLE_LILLY_EPH_PHYSICIAN_EXCLUSION_EDIT",
    "ROLE_LILLY_EPH_GLOBAL_MDU_SUSPECTMATCH",
    "ROLE_LILLY_EPH_DCR_GLOBAL",
    "ROLE_LILLY_EPH_REGCONSUMER_US",
    "ROLE_LILLY_EPH_REGDS_US",
    "ROLE_LILLY_EPH_US_RESSEARCH",
    "ROLE_LILLY_EPH_US_TLSESEARCH",
    "ROLE_LILLY_EPH_US_TLSESEARCHEMAIL",
    "ROLE_ACTIVITY",
]

# ---------------------------------------------------------------------------
# Shared in-memory state (cleared per test by the _wire fixture)
# ---------------------------------------------------------------------------
_DB: dict = {}
_NOTIFS: dict = {}
_UPLOADS: dict = {}


# ===================================================================
# Fake implementations (mirror the pattern from run_local.py)
# ===================================================================

# --- store ---

def _put_request(cfg, item):
    if item["request_id"] in _DB:
        raise store.Conflict("Duplicate request ID.")
    _DB[item["request_id"]] = dict(item)


def _get_request(cfg, request_id):
    item = _DB.get(request_id)
    return dict(item) if item else None


def _list_by_requester(cfg, oid, limit=50):
    items = [dict(i) for i in _DB.values() if i["requester_oid"] == oid]
    return sorted(items, key=lambda i: i["created_at"], reverse=True)[:limit]


def _list_by_status(cfg, status, limit=50):
    items = [dict(i) for i in _DB.values() if i["status"] == status]
    return sorted(items, key=lambda i: i["created_at"], reverse=True)[:limit]


async def _list_recent(cfg, limit=30):
    items = [dict(i) for i in _DB.values() if i["status"] in store.CLOSED]
    return sorted(items, key=lambda i: i.get("updated_at", ""), reverse=True)[:limit]


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


# --- reltio ---

async def _fake_get_customer_metadata(cfg):
    return {"tenants": list(_FAKE_TENANTS),
            "customer_specific_roles": list(_FAKE_CUSTOM_ROLES)}


async def _fake_get_valid_role_names(cfg):
    return set(_FAKE_CUSTOM_ROLES) | set(cfg.system_roles)


# --- notifications ---

def _notif_put(cfg, recipient_oid, title, body, request_id=None, link=None):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    nid = secrets.token_hex(4)
    _NOTIFS.setdefault(recipient_oid, []).insert(0, {
        "recipient_oid": recipient_oid,
        "sort_key": f"{now}#{nid}",
        "title": title,
        "body": body,
        "request_id": request_id or "",
        "link": link or "",
        "read": False,
        "created_at": now,
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


# --- attachments ---

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


# --- provisioning ---

async def _fake_provision(cfg, item):
    roles = item.get("approved_roles") or item["reltio_roles"]
    return {"action": "created",
            "added": {r: list(item["tenants"]) for r in roles},
            "already_had": {}, "warnings": []}


# ===================================================================
# Fixtures
# ===================================================================

@pytest.fixture(autouse=True)
def _wire(settings, fake_user):
    """Replace external dependencies with in-memory fakes for every test."""
    _DB.clear()
    _NOTIFS.clear()
    _UPLOADS.clear()

    # Pairs: (module, attribute_name, fake_value)
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

    # Clear caches so each test gets a fresh start.
    get_settings.cache_clear()
    matrix._matrix_cache.update({"index": None, "version": None, "loaded_at": 0.0})

    # Dependency overrides -- route handlers receive test objects.
    app.dependency_overrides[require_user] = lambda: fake_user
    app.dependency_overrides[get_settings] = lambda: settings

    yield

    # Restore originals.
    for mod, name, orig_val in originals:
        setattr(mod, name, orig_val)
    app.dependency_overrides.clear()
    get_settings.cache_clear()
    matrix._matrix_cache.update({"index": None, "version": None, "loaded_at": 0.0})


@pytest_asyncio.fixture
async def client():
    """Async HTTP client wired to the FastAPI app (no network)."""
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


# ===================================================================
# Helpers
# ===================================================================

def _set_user(user: User):
    """Switch the active test user mid-test."""
    app.dependency_overrides[require_user] = lambda: user


def _make_item(request_id=None, requester_oid="user-oid", status=store.PENDING,
               **overrides):
    """Build a complete request item matching the shape ``submit`` produces."""
    ts = store.now_iso()
    rid = request_id or store.new_request_id()
    item = {
        "request_id": rid,
        "status": status,
        "created_at": ts,
        "updated_at": ts,
        "requester_oid": requester_oid,
        "requester_upn": "user@lilly.com",
        "requester_name": "Test User",
        "global_id": "L123456",
        "email": "testuser@lilly.com",
        "country_code": "US",
        "dou": "Consent consumer (GCCP)",
        "use_case": "Consent consumer (GCCP)",
        "justification": "Testing.",
        "reltio_roles": ["ROLE_LILLY_EPH_GLOBAL_GCCP", "ROLE_API_EXPORT"],
        "ecdp_roles": [],
        "tenants": ["tenant-a"],
        "attachment": {
            "key": "uploads/user-oid/abc123/approval.pdf",
            "filename": "approval.pdf",
            "size": 1024,
            "content_type": "application/pdf",
        },
        "required_approvers": [
            {"key": "alice-oid", "name": "Alice Approver", "upn": "alice@lilly.com"},
            {"key": "bob-oid", "name": "Bob Approver", "upn": "bob@lilly.com"},
        ],
        "policy": "all",
        "approvals": {},
        "matrix_version": "v108",
        "result": None,
        "error": None,
    }
    item.update(overrides)
    return item


def _seed(request_id=None, **overrides) -> dict:
    """Create a request item and store it in the fake DB."""
    item = _make_item(request_id=request_id, **overrides)
    _DB[item["request_id"]] = item
    return item


def _submit_payload(**overrides) -> dict:
    """Default payload for ``POST /api/requests``."""
    payload = {
        "global_id": "L123456",
        "email": "testuser@lilly.com",
        "country_code": "US",
        "dou": "Consent consumer (GCCP)",
        "justification": "I need this access for testing.",
        "tenants": ["tenant-a"],
        "attachment_key": "uploads/user-oid/abc123/approval.pdf",
    }
    payload.update(overrides)
    return payload


# ===================================================================
# GET /healthz
# ===================================================================

@pytest.mark.asyncio
async def test_healthz(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ===================================================================
# GET /api/me
# ===================================================================

@pytest.mark.asyncio
async def test_me_regular_user(client):
    resp = await client.get("/api/me")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Test User"
    assert data["upn"] == "user@lilly.com"
    assert data["is_approver"] is False


@pytest.mark.asyncio
async def test_me_approver(client, approver_user):
    _set_user(approver_user)
    resp = await client.get("/api/me")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Alice Approver"
    assert data["upn"] == "alice@lilly.com"
    assert data["is_approver"] is True


# ===================================================================
# GET /api/metadata
# ===================================================================

@pytest.mark.asyncio
async def test_metadata_returns_tenants_and_approvers(client):
    resp = await client.get("/api/metadata")
    assert resp.status_code == 200
    data = resp.json()
    tenant_ids = [t["id"] for t in data["tenants"]]
    assert "tenant-a" in tenant_ids
    assert "tenant-b" in tenant_ids
    # The regular user is not an approver, so both approvers are listed.
    assert len(data["your_approvers"]) == 2
    assert data["approval_policy"] == "all"
    assert "attachment" in data
    assert data["attachment"]["max_bytes"] > 0
    assert isinstance(data["email_domains"], list)


# ===================================================================
# GET /api/resolve-dou
# ===================================================================

@pytest.mark.asyncio
async def test_resolve_dou_happy_path(client):
    resp = await client.get("/api/resolve-dou",
                            params={"dou": "Consent consumer (GCCP)", "country": "US"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["matched"] is True
    assert data["grantable"] is True
    assert "ROLE_LILLY_EPH_GLOBAL_GCCP" in data["reltio_roles"]


@pytest.mark.asyncio
async def test_resolve_dou_unmatched(client):
    resp = await client.get("/api/resolve-dou",
                            params={"dou": "Totally Nonexistent Use Case"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["matched"] is False
    assert data["grantable"] is False


# ===================================================================
# POST /api/attachments
# ===================================================================

@pytest.mark.asyncio
async def test_presign_attachment(client):
    resp = await client.post("/api/attachments",
                             json={"filename": "approval.pdf", "size": 1024})
    assert resp.status_code == 200
    data = resp.json()
    assert "url" in data
    assert "key" in data
    assert data["key"].startswith("uploads/user-oid/")


# ===================================================================
# POST /api/requests  (submit)
# ===================================================================

@pytest.mark.asyncio
async def test_submit_happy_path(client):
    _UPLOADS["uploads/user-oid/abc123/approval.pdf"] = {
        "filename": "approval.pdf", "size": 1024,
    }
    resp = await client.post("/api/requests", json=_submit_payload())
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "PENDING"
    assert data["global_id"] == "L123456"
    assert data["email"] == "testuser@lilly.com"
    assert data["use_case"] == "Consent consumer (GCCP)"
    assert len(data["tenants"]) == 1
    assert data["tenants"][0]["id"] == "tenant-a"
    assert "ROLE_LILLY_EPH_GLOBAL_GCCP" in data["reltio_roles"]
    assert data["viewer"]["is_requester"] is True
    assert data["viewer"]["can_cancel"] is True
    # Item stored in fake DB.
    assert len(_DB) == 1
    stored = next(iter(_DB.values()))
    assert stored["status"] == store.PENDING
    # In-app notifications sent to both approvers.
    assert len(_NOTIFS.get("alice-oid", [])) >= 1
    assert len(_NOTIFS.get("bob-oid", [])) >= 1


@pytest.mark.asyncio
async def test_submit_bad_email_rejected(client):
    resp = await client.post("/api/requests",
                             json=_submit_payload(email="user@gmail.com"))
    assert resp.status_code == 400
    assert "email" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_submit_bad_country_rejected(client):
    resp = await client.post("/api/requests",
                             json=_submit_payload(country_code="XYZ"))
    assert resp.status_code == 400
    assert "country" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_submit_bad_tenant_rejected(client):
    resp = await client.post("/api/requests",
                             json=_submit_payload(tenants=["nonexistent-tenant"]))
    assert resp.status_code == 400
    assert "tenant" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_submit_duplicate_detected(client):
    _seed(
        requester_oid="user-oid",
        global_id="L123456",
        use_case="Consent consumer (GCCP)",
        tenants=["tenant-a"],
    )
    _UPLOADS["uploads/user-oid/abc123/approval.pdf"] = {
        "filename": "approval.pdf", "size": 1024,
    }
    resp = await client.post("/api/requests", json=_submit_payload())
    assert resp.status_code == 409
    assert "already" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_submit_retired_dou_rejected(client):
    resp = await client.post(
        "/api/requests",
        json=_submit_payload(dou="Commercial Customer Search and Find"),
    )
    assert resp.status_code == 400
    assert "retired" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_submit_unmatched_dou_rejected(client):
    resp = await client.post(
        "/api/requests",
        json=_submit_payload(dou="This DoU Does Not Exist At All"),
    )
    assert resp.status_code == 400
    # The error message mentions "doesn't match" or similar.
    assert "match" in resp.json()["detail"].lower()


# ===================================================================
# GET /api/requests  (my requests)
# ===================================================================

@pytest.mark.asyncio
async def test_list_my_requests(client):
    _seed(request_id="AR-20241101-AABB01", requester_oid="user-oid")
    _seed(request_id="AR-20241101-AABB02", requester_oid="user-oid")
    _seed(request_id="AR-20241101-AABB03", requester_oid="other-oid")
    resp = await client.get("/api/requests")
    assert resp.status_code == 200
    data = resp.json()
    ids = [r["request_id"] for r in data]
    assert "AR-20241101-AABB01" in ids
    assert "AR-20241101-AABB02" in ids
    assert "AR-20241101-AABB03" not in ids


@pytest.mark.asyncio
async def test_list_my_requests_empty(client):
    resp = await client.get("/api/requests")
    assert resp.status_code == 200
    assert resp.json() == []


# ===================================================================
# GET /api/approvals
# ===================================================================

@pytest.mark.asyncio
async def test_approval_queue_approver_sees_pending(client, approver_user):
    _set_user(approver_user)
    _seed(request_id="AR-20241101-AAA001")
    resp = await client.get("/api/approvals")
    assert resp.status_code == 200
    data = resp.json()
    assert "waiting" in data
    assert "recent" in data
    waiting_ids = [r["request_id"] for r in data["waiting"]]
    assert "AR-20241101-AAA001" in waiting_ids


@pytest.mark.asyncio
async def test_approval_queue_non_approver_gets_403(client):
    resp = await client.get("/api/approvals")
    assert resp.status_code == 403


# ===================================================================
# GET /api/requests/{id}
# ===================================================================

@pytest.mark.asyncio
async def test_get_single_request(client):
    _seed(request_id="AR-20241101-AABB01", requester_oid="user-oid")
    resp = await client.get("/api/requests/AR-20241101-AABB01")
    assert resp.status_code == 200
    data = resp.json()
    assert data["request_id"] == "AR-20241101-AABB01"
    assert data["status"] == "PENDING"
    assert data["viewer"]["is_requester"] is True


@pytest.mark.asyncio
async def test_get_request_not_found(client):
    resp = await client.get("/api/requests/AR-20241101-FFFFFF")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_request_approver_sees_others(client, approver_user):
    """An approver can view a request submitted by someone else."""
    _set_user(approver_user)
    _seed(request_id="AR-20241101-AABB01", requester_oid="user-oid")
    resp = await client.get("/api/requests/AR-20241101-AABB01")
    assert resp.status_code == 200
    data = resp.json()
    assert data["viewer"]["is_requester"] is False
    assert data["viewer"]["is_approver"] is True


# ===================================================================
# GET /api/requests/{id}/attachment
# ===================================================================

@pytest.mark.asyncio
async def test_get_attachment_presign(client):
    _seed(request_id="AR-20241101-AABB01", requester_oid="user-oid")
    resp = await client.get("/api/requests/AR-20241101-AABB01/attachment")
    assert resp.status_code == 200
    data = resp.json()
    assert "url" in data
    assert "approval.pdf" in data["url"]


# ===================================================================
# POST /api/requests/{id}/decision
# ===================================================================

@pytest.mark.asyncio
async def test_approve_partial(client, approver_user):
    """With 'all' policy and two approvers, one approval keeps the request PENDING."""
    _set_user(approver_user)
    _seed(request_id="AR-20241101-AABB01")
    resp = await client.post(
        "/api/requests/AR-20241101-AABB01/decision",
        json={"decision": "approve", "comment": "Looks good."},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "PENDING"


@pytest.mark.asyncio
async def test_approve_meets_policy_triggers_provisioning(client, approver_user):
    """When all required approvers have approved, the request provisions."""
    _set_user(approver_user)  # Alice
    _seed(
        request_id="AR-20241101-AABB01",
        approvals={
            "bob-oid": {
                "decision": "approve", "name": "Bob Approver",
                "comment": "", "at": store.now_iso(),
            },
        },
    )
    resp = await client.post(
        "/api/requests/AR-20241101-AABB01/decision",
        json={"decision": "approve", "comment": "Approved."},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "COMPLETED"
    assert _DB["AR-20241101-AABB01"]["status"] == store.COMPLETED


@pytest.mark.asyncio
async def test_reject_request(client, approver_user):
    _set_user(approver_user)
    _seed(request_id="AR-20241101-AABB01")
    resp = await client.post(
        "/api/requests/AR-20241101-AABB01/decision",
        json={"decision": "reject", "comment": "Not justified."},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "REJECTED"
    assert _DB["AR-20241101-AABB01"]["status"] == store.REJECTED


@pytest.mark.asyncio
async def test_reject_without_comment_returns_400(client, approver_user):
    _set_user(approver_user)
    _seed(request_id="AR-20241101-AABB01")
    resp = await client.post(
        "/api/requests/AR-20241101-AABB01/decision",
        json={"decision": "reject", "comment": ""},
    )
    assert resp.status_code == 400
    assert "comment" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_decide_non_approver_returns_403(client):
    _seed(request_id="AR-20241101-AABB01")
    resp = await client.post(
        "/api/requests/AR-20241101-AABB01/decision",
        json={"decision": "approve", "comment": ""},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_approve_with_scoped_roles(client, approver_user):
    """Approver can approve a subset of requested roles."""
    _set_user(approver_user)
    _seed(
        request_id="AR-20241101-AABB01",
        reltio_roles=["ROLE_LILLY_EPH_GLOBAL_GCCP", "ROLE_API_EXPORT"],
        # Bob already approved so Alice's approval meets the "all" policy.
        approvals={
            "bob-oid": {
                "decision": "approve", "name": "Bob Approver",
                "comment": "", "at": store.now_iso(),
            },
        },
    )
    resp = await client.post(
        "/api/requests/AR-20241101-AABB01/decision",
        json={
            "decision": "approve",
            "comment": "Only the custom role.",
            "approved_roles": ["ROLE_LILLY_EPH_GLOBAL_GCCP"],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "COMPLETED"
    # The scoped-down roles should be stored.
    stored = _DB["AR-20241101-AABB01"]
    assert stored.get("approved_roles") == ["ROLE_LILLY_EPH_GLOBAL_GCCP"]


# ===================================================================
# POST /api/requests/{id}/cancel
# ===================================================================

@pytest.mark.asyncio
async def test_cancel_happy_path(client):
    _seed(request_id="AR-20241101-AABB01", requester_oid="user-oid")
    resp = await client.post("/api/requests/AR-20241101-AABB01/cancel")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "CANCELLED"
    assert _DB["AR-20241101-AABB01"]["status"] == store.CANCELLED


@pytest.mark.asyncio
async def test_cancel_not_owner_returns_403(client, approver_user):
    _set_user(approver_user)
    _seed(request_id="AR-20241101-AABB01", requester_oid="user-oid")
    resp = await client.post("/api/requests/AR-20241101-AABB01/cancel")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_cancel_not_pending_returns_409(client):
    _seed(request_id="AR-20241101-AABB01", requester_oid="user-oid",
          status=store.COMPLETED)
    resp = await client.post("/api/requests/AR-20241101-AABB01/cancel")
    assert resp.status_code == 409


# ===================================================================
# POST /api/requests/{id}/retry
# ===================================================================

@pytest.mark.asyncio
async def test_retry_happy_path(client, approver_user):
    _set_user(approver_user)
    _seed(request_id="AR-20241101-AABB01", status=store.FAILED)
    resp = await client.post("/api/requests/AR-20241101-AABB01/retry")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_retry_not_failed_returns_409(client, approver_user):
    _set_user(approver_user)
    _seed(request_id="AR-20241101-AABB01", status=store.PENDING)
    resp = await client.post("/api/requests/AR-20241101-AABB01/retry")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_retry_non_approver_returns_403(client):
    _seed(request_id="AR-20241101-AABB01", status=store.FAILED)
    resp = await client.post("/api/requests/AR-20241101-AABB01/retry")
    assert resp.status_code == 403


# ===================================================================
# GET /api/notifications
# ===================================================================

@pytest.mark.asyncio
async def test_notifications_list(client):
    _NOTIFS["user-oid"] = [
        {"recipient_oid": "user-oid", "sort_key": "2024-01-01T00:00:00#abc",
         "title": "First", "body": "Body 1", "read": False,
         "created_at": "2024-01-01T00:00:00"},
        {"recipient_oid": "user-oid", "sort_key": "2024-01-01T00:01:00#def",
         "title": "Second", "body": "Body 2", "read": True,
         "created_at": "2024-01-01T00:01:00"},
    ]
    resp = await client.get("/api/notifications")
    assert resp.status_code == 200
    data = resp.json()
    assert data["unread"] == 1
    assert len(data["items"]) == 2


@pytest.mark.asyncio
async def test_notifications_empty(client):
    resp = await client.get("/api/notifications")
    assert resp.status_code == 200
    data = resp.json()
    assert data["unread"] == 0
    assert data["items"] == []


# ===================================================================
# POST /api/notifications/read-all
# ===================================================================

@pytest.mark.asyncio
async def test_mark_all_read(client):
    _NOTIFS["user-oid"] = [
        {"recipient_oid": "user-oid", "sort_key": "2024-01-01T00:00:00#abc",
         "title": "A", "body": "B", "read": False,
         "created_at": "2024-01-01T00:00:00"},
        {"recipient_oid": "user-oid", "sort_key": "2024-01-01T00:01:00#def",
         "title": "C", "body": "D", "read": False,
         "created_at": "2024-01-01T00:01:00"},
    ]
    resp = await client.post("/api/notifications/read-all")
    assert resp.status_code == 200
    assert resp.json()["marked"] == 2
    assert all(n["read"] for n in _NOTIFS["user-oid"])


@pytest.mark.asyncio
async def test_mark_all_read_when_none_unread(client):
    _NOTIFS["user-oid"] = [
        {"recipient_oid": "user-oid", "sort_key": "2024-01-01T00:00:00#abc",
         "title": "A", "body": "B", "read": True,
         "created_at": "2024-01-01T00:00:00"},
    ]
    resp = await client.post("/api/notifications/read-all")
    assert resp.status_code == 200
    assert resp.json()["marked"] == 0
