"""Local, no-credentials demo of the full request lifecycle against a fake "DEV B0" tenant.

Run:  cd backend && python demo_local.py

What's real:    the FastAPI routes in app/main.py, DoU resolution in app/matrix.py,
                role-merge logic in app/provisioning.py, DynamoDB state-machine logic
                in app/store.py (just backed by a dict instead of a real table).
What's faked:   Entra auth (require_user is overridden with fixed users), the Reltio
                Auth API (app/reltio.py functions), S3 attachments (app/attachments.py
                functions). No AWS or Reltio credentials are used or required.
"""
import json
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

# --- settings, before importing the app -----------------------------------
os.environ.update({
    "ENTRA_TENANT_ID": "demo-tenant",
    "ENTRA_API_AUDIENCE": "demo-audience",
    "ROLES_MATRIX_LOCAL_PATH": str(BACKEND_DIR / "roles_matrix.sample.json"),
    "APPROVERS": json.dumps([
        {"name": "Revathy Surendran", "upn": "revathy.surendran@lilly.com", "oid": "revathy-oid"},
        {"name": "Brenda Newbold", "upn": "brenda.newbold@lilly.com", "oid": "brenda-oid"},
    ]),
    "APPROVAL_POLICY": "all",
    "TENANT_ALLOWLIST": "b0e8OhkxuytO3n3",
    "TENANT_LABELS": json.dumps({"b0e8OhkxuytO3n3": "DEV B0"}),
    "CORS_ALLOWED_ORIGINS": "http://localhost",
    "ATTACHMENTS_BUCKET": "demo-bucket",
})

from fastapi.testclient import TestClient  # noqa: E402

from app import attachments, reltio, store  # noqa: E402
from app.auth import User, require_user  # noqa: E402
from app.main import app  # noqa: E402

# --- fake DynamoDB (app/store.py) ------------------------------------------
_DB: dict = {}

def _put_request(cfg, item):
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

def _list_recent(cfg, limit=30):
    items = [dict(i) for i in _DB.values() if i["status"] in store.CLOSED]
    return sorted(items, key=lambda i: i.get("updated_at", ""), reverse=True)[:limit]

def _record_decision(cfg, request_id, approver, decision):
    item = _DB[request_id]
    if item["status"] != store.PENDING or approver in (item.get("approvals") or {}):
        raise store.Conflict("This request was already decided, or you've already recorded a decision.")
    item.setdefault("approvals", {})[approver] = decision
    item["updated_at"] = store.now_iso()
    if decision["decision"] == "reject":
        item["status"] = store.REJECTED
    return dict(item)

def _transition(cfg, request_id, from_status, to_status, **fields):
    item = _DB[request_id]
    if item["status"] != from_status:
        return False
    item["status"] = to_status
    item["updated_at"] = store.now_iso()
    item.update(fields)
    return True

store.put_request = _put_request
store.get_request = _get_request
store.list_by_requester = _list_by_requester
store.list_by_status = _list_by_status
store.list_recent = _list_recent
store.record_decision = _record_decision
store.transition = _transition

# --- fake Reltio Auth API (app/reltio.py) ----------------------------------
_RELTIO_USERS: dict = {}

async def _get_customer_metadata(settings):
    return {
        "tenants": ["b0e8OhkxuytO3n3"],
        "customer_specific_roles": [
            "ROLE_LILLY_EPH_US_BASICSEARCH",
            "ROLE_LILLY_EPH_GLOBAL_BASICSEARCH",
        ],
    }

async def _get_user(settings, username):
    return _RELTIO_USERS.get(username)

async def _create_user(settings, user_object):
    _RELTIO_USERS[user_object["username"]] = {**user_object, "createdDate": "2026-11-01T00:00:00Z"}
    return _RELTIO_USERS[user_object["username"]]

async def _update_user(settings, username, user_object):
    _RELTIO_USERS[username] = {**user_object, "updatedDate": "2026-11-01T00:00:00Z"}
    return _RELTIO_USERS[username]

reltio.get_customer_metadata = _get_customer_metadata
reltio.get_user = _get_user
reltio.create_user = _create_user
reltio.update_user = _update_user

# --- fake S3 attachments (app/attachments.py) ------------------------------
def _presign_upload(settings, user, filename, size):
    key = f"uploads/{user.oid}/demo/{filename}"
    return {"url": "https://demo-bucket.s3.amazonaws.com/", "fields": {"key": key}, "key": key}

def _verify_upload(settings, user, key):
    if not key.startswith(f"uploads/{user.oid}/"):
        raise ValueError("That attachment wasn't uploaded by you. Attach the file again.")
    return {"key": key, "filename": key.rsplit("/", 1)[-1], "size": 42_000, "content_type": "application/pdf"}

attachments.presign_upload = _presign_upload
attachments.verify_upload = _verify_upload

# --- fixed users, swapped via the require_user dependency override --------
requester = User(oid="jane-oid", upn="jane.requester@lilly.com", name="Jane Requester")
revathy = User(oid="revathy-oid", upn="revathy.surendran@lilly.com", name="Revathy Surendran")
brenda = User(oid="brenda-oid", upn="brenda.newbold@lilly.com", name="Brenda Newbold")

_current = {"user": requester}
app.dependency_overrides[require_user] = lambda: _current["user"]


def as_(user):
    _current["user"] = user


def show(title, resp):
    print(f"\n--- {title} [{resp.status_code}] ---")
    print(json.dumps(resp.json(), indent=2))


client = TestClient(app)

print("=" * 70)
print("DEMO: Reltio access request, dev tenant DEV B0 (b0e8OhkxuytO3n3)")
print("=" * 70)

as_(requester)
show("GET /api/me (requester)", client.get("/api/me"))
show("GET /api/metadata", client.get("/api/metadata"))

dou_text = "~2578 Enterprise Profile-DOU-Basic Search and Find V3"
show("GET /api/resolve-dou (preview)",
     client.get("/api/resolve-dou", params={"dou": dou_text, "country": "US"}))

show("POST /api/attachments (presign)",
     client.post("/api/attachments", json={"filename": "manager-approval.pdf", "size": 42_000}))

submit_resp = client.post("/api/requests", json={
    "global_id": "L123456",
    "email": "jane.requester@lilly.com",
    "country_code": "US",
    "dou": dou_text,
    "tenants": ["b0e8OhkxuytO3n3"],
    "attachment_key": f"uploads/{requester.oid}/demo/manager-approval.pdf",
})
show("POST /api/requests (submit)", submit_resp)
request_id = submit_resp.json()["request_id"]

as_(revathy)
show("GET /api/approvals (Revathy's queue)", client.get("/api/approvals"))
show("POST decision: Revathy approves",
     client.post(f"/api/requests/{request_id}/decision", json={"decision": "approve", "comment": "Looks right."}))

as_(brenda)
show("POST decision: Brenda approves (second approval triggers the grant)",
     client.post(f"/api/requests/{request_id}/decision", json={"decision": "approve", "comment": "Approved."}))

as_(requester)
show("GET /api/requests/{id} (final state)", client.get(f"/api/requests/{request_id}"))

print("\n--- What Reltio now has for L123456 (fake tenant) ---")
print(json.dumps(_RELTIO_USERS["L123456"], indent=2))
