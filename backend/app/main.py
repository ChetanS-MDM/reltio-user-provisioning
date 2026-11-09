"""FastAPI backend: Reltio access requests with two-person approval.

1. A signed-in person submits a Global ID, email, country code, DoU, tenants,
   and their manager's approval email (uploaded straight to S3).
2. The DoU is resolved against the roles matrix and every Reltio role is
   validated. That role set is frozen on the request, so approvers approve
   exactly what will be granted.
3. The configured approvers review the request in the app.
4. Once the approval policy is met, the backend creates the Reltio user, or
   adds the approved roles to the existing user, and records the outcome.

No route grants access without an approved request.
"""
import logging
import re
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import attachments, audit, magic_link, matrix, notifications, notify, provisioning, reltio, store
from .auth import User, approver_entry, approver_key, matches, require_user
from .config import Settings, get_settings
from .logging_config import correlation_id, setup_logging
from .models import DecisionRequest, EmailDecisionRequest, PresignRequest, SubmitRequest
from .reltio import ReltioError

settings = get_settings()
setup_logging(settings.log_level)
logger = logging.getLogger("reltio_access.main")

app = FastAPI(title="Reltio access requests", version="3.0.0")

if settings.cors_origins:
    _cors_headers = ["Authorization", "Content-Type"]
    if settings.log_level == "DEBUG":
        _cors_headers.append("X-Dev-User")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=_cors_headers,
    )


@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    cid = request.headers.get("x-amzn-requestid") or str(uuid.uuid4())
    token = correlation_id.set(cid)
    logger.info('"%s %s" started', request.method, request.url.path)
    try:
        response = await call_next(request)
        logger.info('"%s %s" -> %s', request.method, request.url.path, response.status_code)
        return response
    finally:
        correlation_id.reset(token)


STALE_PROVISIONING = timedelta(minutes=5)
_REQUEST_ID_RE = r"^AR-\d{8}-[A-F0-9]{6}$"
_ARN_RE = re.compile(r"arn:aws:[a-z0-9\-]+:[a-z0-9\-]*:\d{12}:[^\s,;\"']+")
_ACCT_RE = re.compile(r"\b\d{12}\b")


def _sanitize_error(raw: str | None) -> str | None:
    if not raw:
        return raw
    cleaned = _ARN_RE.sub("[redacted]", raw)
    cleaned = _ACCT_RE.sub("[redacted]", cleaned)
    if len(cleaned) > 200:
        cleaned = cleaned[:200] + "..."
    return cleaned


@app.exception_handler(ReltioError)
async def _reltio_error(_request, exc: ReltioError):
    return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})


# ------------------------------------------------------------------ helpers --

def _tenants(cfg: Settings, ids):
    return [{"id": t, "label": cfg.tenant_labels.get(t, t)} for t in ids]


async def _open_tenants(cfg: Settings):
    """Tenants people can request: allowlisted, labeled ones first in label order."""
    meta = await reltio.get_customer_metadata(cfg)
    allow = set(cfg.tenant_allowlist_ids)
    available = [t for t in meta["tenants"] if not allow or t in allow]
    labeled = [t for t in cfg.tenant_labels if t in available]
    return labeled + sorted(t for t in available if t not in cfg.tenant_labels)


def _approvers_for(cfg: Settings, user: User):
    """Everyone on the approver list except the requester: no self-approval."""
    return [{"key": approver_key(a), "name": a.get("name") or a.get("upn", ""), "upn": a.get("upn", "")}
            for a in cfg.approvers if not matches(a, user)]


def _email_ok(cfg: Settings, email: str) -> bool:
    m = re.fullmatch(r"[^\s@]+@([^\s@]+\.[^\s@]+)", email)
    if not m:
        return False
    domain = m.group(1).lower()
    return not cfg.email_domains or any(domain == d or domain.endswith("." + d) for d in cfg.email_domains)


def _policy_met(item: dict) -> bool:
    approved = {k for k, v in (item.get("approvals") or {}).items() if v.get("decision") == "approve"}
    required = {a["key"] for a in item["required_approvers"]}
    return bool(approved & required) if item.get("policy") == "any" else required <= approved


def _stale(item: dict) -> bool:
    """Provisioning that never finished (e.g. the Lambda timed out) can be retried."""
    if item["status"] != store.PROVISIONING:
        return False
    return datetime.now(timezone.utc) - datetime.fromisoformat(item["updated_at"]) > STALE_PROVISIONING


def _view(item: dict, user: User, cfg: Settings) -> dict:
    me = approver_entry(user, cfg)
    my_key = approver_key(me) if me else None
    decided = item.get("approvals") or {}
    required = item["required_approvers"]
    pending = item["status"] == store.PENDING
    mine = item["requester_oid"] == user.oid
    can_decide = (bool(me) and pending and my_key in {a["key"] for a in required}
                  and my_key not in decided)
    return {
        "request_id": item["request_id"],
        "status": item["status"],
        "created_at": item["created_at"],
        "updated_at": item["updated_at"],
        "requester": {"name": item["requester_name"], "upn": item["requester_upn"]},
        "global_id": item["global_id"],
        "email": item["email"],
        "country_code": item["country_code"],
        "dou": item["dou"],
        "use_case": item["use_case"],
        "justification": item.get("justification", ""),
        "role_detail": list(item.get("approved_roles") or item["reltio_roles"]) if can_decide else None,
        "reltio_roles": item["reltio_roles"],
        "ecdp_roles": item["ecdp_roles"],
        "tenants": _tenants(cfg, item["tenants"]),
        "attachment": {"filename": item["attachment"]["filename"], "size": item["attachment"]["size"]},
        "policy": item["policy"],
        "required_approvers": [{"name": a["name"], "decision": decided.get(a["key"])} for a in required],
        "result": item.get("result"),
        "error": _sanitize_error(item.get("error")),
        "viewer": {
            "is_requester": mine,
            "is_approver": me is not None,
            "can_decide": can_decide,
            "can_cancel": mine and pending,
            "can_retry": bool(me) and (item["status"] == store.FAILED or _stale(item)),
        },
    }


def _load(cfg: Settings, user: User, request_id: str) -> dict:
    item = store.get_request(cfg, request_id)
    # Requesters see their own requests; approvers see all. Anyone else gets a 404.
    if not item or (item["requester_oid"] != user.oid and not approver_entry(user, cfg)):
        raise HTTPException(404, "Request not found.")
    return item


async def _provision(cfg: Settings, request_id: str, actor: User) -> None:
    item = store.get_request(cfg, request_id)
    if not item:
        logger.error("Request %s vanished before provisioning", request_id)
        return
    try:
        result = await provisioning.provision(cfg, item)
    except Exception as exc:  # recorded on the request; approvers can retry
        message = str(exc)[:500] or exc.__class__.__name__
        store.transition(cfg, request_id, store.PROVISIONING, store.FAILED, error=message)
        audit.event(actor, "provision_failed", request_id, error=message)
        notify.provisioning_failed(cfg, item, message)
        return
    store.transition(cfg, request_id, store.PROVISIONING, store.COMPLETED, result=result, error=None)
    audit.event(actor, "provisioned", request_id, result=result)
    notify.completed(cfg, item, result)


# ------------------------------------------------------------------- routes --

@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/api/me")
async def me(user: User = Depends(require_user), cfg: Settings = Depends(get_settings)):
    return {"name": user.name, "upn": user.upn, "is_approver": approver_entry(user, cfg) is not None}


@app.get("/api/metadata")
async def metadata(user: User = Depends(require_user), cfg: Settings = Depends(get_settings)):
    return {
        "tenants": _tenants(cfg, await _open_tenants(cfg)),
        "your_approvers": [a["name"] for a in _approvers_for(cfg, user)],
        "approval_policy": cfg.approval_policy,
        "attachment": {"max_bytes": cfg.attachment_max_bytes, "extensions": sorted(cfg.attachment_types)},
        "email_domains": cfg.email_domains,
        "global_id_pattern": cfg.global_id_pattern,
        "extra_country_codes": cfg.extra_countries,
    }


@app.get("/api/resolve-dou")
async def resolve_dou(
    dou: str = Query(min_length=1, max_length=300),
    country: str = Query(default="", max_length=12),
    user: User = Depends(require_user),
    cfg: Settings = Depends(get_settings),
):
    """Preview what a DoU grants. The submit route resolves it again server-side."""
    code = None
    if country.strip():
        code = matrix.normalize_country(country, cfg)
        if not code:
            raise HTTPException(400, "Enter a two-letter country code, like US, or GLOBAL.")
    r = await matrix.resolve(cfg, dou, await reltio.get_valid_role_names(cfg), code)
    return {**asdict(r), "grantable": r.grantable}


@app.post("/api/attachments")
async def presign_attachment(body: PresignRequest, user: User = Depends(require_user),
                             cfg: Settings = Depends(get_settings)):
    try:
        return attachments.presign_upload(cfg, user, body.filename, body.size)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/requests", status_code=201)
async def submit(req: SubmitRequest, user: User = Depends(require_user),
                 cfg: Settings = Depends(get_settings)):
    global_id = req.global_id.strip().upper().replace("–", "-").replace("—", "-")
    if not global_id:
        raise HTTPException(400, "Enter a Global ID.")
    email = req.email.strip().lower()
    if not _email_ok(cfg, email):
        raise HTTPException(400, "Enter a Lilly email address.")
    country = matrix.normalize_country(req.country_code, cfg)
    if not country:
        raise HTTPException(400, "Enter a two-letter country code, like US, or GLOBAL.")

    # Resolve the DoU now and freeze the result: approvers approve exactly this.
    r = await matrix.resolve(cfg, req.dou, await reltio.get_valid_role_names(cfg), country)
    if not r.matched or r.error:
        raise HTTPException(400, r.error or "That DoU doesn't match any use case.")
    if r.retired:
        raise HTTPException(400, f'"{r.use_case}" is retired, so it cannot be requested.')
    if r.unknown_roles:
        logger.warning("Dropping roles not in Reltio: %s", r.unknown_roles)
        r.reltio_roles = [role for role in r.reltio_roles if role not in set(r.unknown_roles)]
    if not r.reltio_roles:
        raise HTTPException(400, f'"{r.use_case}" grants no Reltio roles.')

    open_tenants = set(await _open_tenants(cfg))
    tenants = list(dict.fromkeys(t.strip() for t in req.tenants if t.strip()))
    bad = [t for t in tenants if t not in open_tenants]
    if bad or not tenants:
        raise HTTPException(400, f"These tenants aren't open for requests: {', '.join(bad) or 'none selected'}.")

    approvers = _approvers_for(cfg, user)
    if not approvers:
        raise HTTPException(400, "No approvers are set up for your requests. Contact the EPH team.")

    try:
        attachment = attachments.verify_upload(cfg, user, req.attachment_key)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    for open_req in store.list_by_requester(cfg, user.oid):
        if open_req["status"] in (store.PENDING, store.PROVISIONING):
            raise HTTPException(
                409,
                f"You already have a pending request ({open_req['request_id']}). "
                f"Please wait for it to be approved, rejected, or cancel it before submitting a new one.",
            )

    now = store.now_iso()
    item = {
        "request_id": store.new_request_id(),
        "status": store.PENDING,
        "created_at": now,
        "updated_at": now,
        "requester_oid": user.oid,
        "requester_upn": user.upn,
        "requester_name": user.name or user.upn,
        "global_id": global_id,
        "email": email,
        "country_code": country,
        "dou": req.dou.strip(),
        "use_case": r.use_case,
        "justification": req.justification.strip(),
        "reltio_roles": r.reltio_roles,
        "ecdp_roles": r.ecdp_roles,
        "tenants": tenants,
        "attachment": attachment,
        "required_approvers": approvers,
        "policy": cfg.approval_policy,
        "approvals": {},
        "matrix_version": await matrix.version(cfg),
        "result": None,
        "error": None,
    }
    store.put_request(cfg, item)
    audit.event(user, "submitted", item["request_id"], global_id=global_id, use_case=r.use_case,
                roles=r.reltio_roles, tenants=tenants, attachment=attachment["key"])
    notify.submitted(cfg, item)
    return _view(item, user, cfg)


@app.get("/api/requests")
async def my_requests(limit: int = Query(default=50, ge=1, le=200),
                      user: User = Depends(require_user), cfg: Settings = Depends(get_settings)):
    items = store.list_by_requester(cfg, user.oid, limit=limit)
    return [_view(i, user, cfg) for i in items]


@app.get("/api/approvals")
async def approval_queue(limit: int = Query(default=30, ge=1, le=200),
                         user: User = Depends(require_user), cfg: Settings = Depends(get_settings)):
    me = approver_entry(user, cfg)
    if not me:
        raise HTTPException(403, "Only approvers can see the approval queue.")
    key = approver_key(me)
    pending = store.list_by_status(cfg, store.PENDING)
    waiting = [i for i in pending
               if key in {a["key"] for a in i["required_approvers"]} and key not in (i.get("approvals") or {})]
    waiting.sort(key=lambda i: i["created_at"])                     # oldest first
    waiting_ids = {i["request_id"] for i in waiting}
    others = [i for i in pending if i["request_id"] not in waiting_ids] + await store.list_recent(cfg, limit=limit)
    return {"waiting": [_view(i, user, cfg) for i in waiting],
            "recent": [_view(i, user, cfg) for i in others[:limit]]}


@app.get("/api/requests/{request_id}")
async def get_request(request_id: str = Path(pattern=_REQUEST_ID_RE),
                      user: User = Depends(require_user),
                      cfg: Settings = Depends(get_settings)):
    return _view(_load(cfg, user, request_id), user, cfg)


@app.get("/api/requests/{request_id}/attachment")
async def get_attachment(request_id: str = Path(pattern=_REQUEST_ID_RE),
                         user: User = Depends(require_user),
                         cfg: Settings = Depends(get_settings)):
    item = _load(cfg, user, request_id)
    audit.event(user, "attachment_opened", request_id)
    return {"url": attachments.presign_download(cfg, item["attachment"]["key"], item["attachment"]["filename"])}


@app.post("/api/requests/{request_id}/decision")
async def decide(request_id: str = Path(pattern=_REQUEST_ID_RE),
                 body: DecisionRequest = ..., user: User = Depends(require_user),
                 cfg: Settings = Depends(get_settings)):
    me = approver_entry(user, cfg)
    if not me:
        raise HTTPException(403, "Only approvers can decide requests.")
    item = _load(cfg, user, request_id)
    key = approver_key(me)
    if key not in {a["key"] for a in item["required_approvers"]}:
        raise HTTPException(403, "You can't approve this request because you're the requester.")
    comment = body.comment.strip()
    if body.decision == "reject" and not comment:
        raise HTTPException(400, "Add a comment explaining the rejection.")

    if body.decision == "approve" and body.approved_roles is not None:
        allowed = set(item["reltio_roles"])
        kept = [r for r in body.approved_roles if r in allowed]
        if not kept:
            raise HTTPException(400, "Keep at least one access item, or reject the request instead.")
        store.transition(cfg, request_id, store.PENDING, store.PENDING, approved_roles=kept)
        item["approved_roles"] = kept

    decision = {"decision": body.decision, "name": me.get("name") or user.name,
                "comment": comment, "at": store.now_iso()}
    try:
        item = store.record_decision(cfg, request_id, key, decision)
    except store.Conflict as exc:
        raise HTTPException(409, str(exc))
    audit.event(user, body.decision, request_id, comment=comment,
                approved_roles=item.get("approved_roles"))

    await _post_decision(cfg, request_id, item, body.decision, decision, user)
    return _view(store.get_request(cfg, request_id), user, cfg)


async def _post_decision(cfg: Settings, request_id: str, item: dict,
                         decision_type: str, decision: dict, actor: User) -> None:
    if decision_type == "reject":
        notify.rejected(cfg, item, decision)
    elif _policy_met(item) and store.transition(cfg, request_id, store.PENDING, store.PROVISIONING):
        await _provision(cfg, request_id, actor)
    elif decision_type == "approve":
        notify.partial_approved(cfg, item, decision)


@app.post("/api/requests/{request_id}/retry")
async def retry(request_id: str = Path(pattern=_REQUEST_ID_RE),
                user: User = Depends(require_user), cfg: Settings = Depends(get_settings)):
    if not approver_entry(user, cfg):
        raise HTTPException(403, "Only approvers can retry granting access.")
    item = _load(cfg, user, request_id)
    if not (store.transition(cfg, request_id, store.FAILED, store.PROVISIONING, error=None)
            or (_stale(item) and store.transition(cfg, request_id, store.PROVISIONING, store.PROVISIONING))):
        raise HTTPException(409, "Only a request that failed to grant access can be retried.")
    audit.event(user, "retry", request_id)
    await _provision(cfg, request_id, user)
    return _view(store.get_request(cfg, request_id), user, cfg)


@app.post("/api/requests/{request_id}/cancel")
async def cancel(request_id: str = Path(pattern=_REQUEST_ID_RE),
                 user: User = Depends(require_user), cfg: Settings = Depends(get_settings)):
    item = _load(cfg, user, request_id)
    if item["requester_oid"] != user.oid:
        raise HTTPException(403, "Only the requester can cancel a request.")
    if not store.transition(cfg, request_id, store.PENDING, store.CANCELLED):
        raise HTTPException(409, "Only a request that's still waiting for approval can be cancelled.")
    audit.event(user, "cancelled", request_id)
    notify.cancelled(cfg, item)
    return _view(store.get_request(cfg, request_id), user, cfg)


@app.get("/api/notifications")
async def get_notifications(user: User = Depends(require_user),
                            cfg: Settings = Depends(get_settings)):
    items = notifications.list_recent(cfg, user.oid)
    unread = sum(1 for i in items if not i.get("read"))
    return {"unread": unread, "items": items}


@app.post("/api/notifications/read-all")
async def mark_notifications_read(user: User = Depends(require_user),
                                  cfg: Settings = Depends(get_settings)):
    count = await notifications.mark_all_read(cfg, user.oid)
    return {"marked": count}


# ------------------------------------------------- email magic-link routes --

@app.get("/api/email-review/{token}")
async def email_review(token: str, cfg: Settings = Depends(get_settings)):
    if not cfg.magic_link_secret:
        return HTMLResponse(magic_link.render_error_page("Email approval links are not configured."), status_code=500)
    try:
        info = magic_link.verify_token(cfg.magic_link_secret, token)
    except magic_link.TokenError as exc:
        return HTMLResponse(magic_link.render_error_page(str(exc)), status_code=400)
    item = store.get_request(cfg, info["request_id"])
    if not item:
        return HTMLResponse(magic_link.render_error_page("Request not found."), status_code=404)
    approver = None
    for a in item["required_approvers"]:
        a_oid = a.get("oid") or a.get("key", "")
        if a_oid == info["approver_oid"]:
            approver = a
            break
    if not approver:
        return HTMLResponse(magic_link.render_error_page("You are not an approver for this request."), status_code=403)
    return HTMLResponse(magic_link.render_review_page(item, approver, cfg, token))


@app.get("/api/email-attachment/{token}")
async def email_attachment(token: str, cfg: Settings = Depends(get_settings)):
    if not cfg.magic_link_secret:
        raise HTTPException(500, "Email approval links are not configured.")
    try:
        info = magic_link.verify_token(cfg.magic_link_secret, token)
    except magic_link.TokenError as exc:
        raise HTTPException(400, str(exc))
    item = store.get_request(cfg, info["request_id"])
    if not item:
        raise HTTPException(404, "Request not found.")
    url = attachments.presign_download(cfg, item["attachment"]["key"], item["attachment"]["filename"])
    return RedirectResponse(url)


@app.post("/api/email-decision")
async def email_decision(body: EmailDecisionRequest, cfg: Settings = Depends(get_settings)):
    if not cfg.magic_link_secret:
        raise HTTPException(500, "Email approval links are not configured.")
    try:
        info = magic_link.verify_token(cfg.magic_link_secret, body.token)
    except magic_link.TokenError as exc:
        raise HTTPException(400, str(exc))

    item = store.get_request(cfg, info["request_id"])
    if not item:
        raise HTTPException(404, "Request not found.")
    if item["status"] != store.PENDING:
        raise HTTPException(409, f"This request is no longer pending (status: {item['status']}).")

    approver = None
    for a in item["required_approvers"]:
        a_oid = a.get("oid") or a.get("key", "")
        if a_oid == info["approver_oid"]:
            approver = a
            break
    if not approver:
        raise HTTPException(403, "You are not an approver for this request.")

    comment = body.comment.strip()
    if body.decision == "reject" and not comment:
        raise HTTPException(400, "Add a comment explaining the rejection.")

    confirm_token = magic_link.create_confirmation_token(
        cfg.magic_link_secret, info["request_id"], info["approver_oid"],
        body.decision, comment,
    )
    confirm_url = f"{cfg.api_base_url.rstrip('/')}/api/email-confirm/{confirm_token}"
    notify.confirmation_email(cfg, approver, item, body.decision, comment, confirm_url)

    email = approver.get("upn", "")
    if "@" in email:
        parts = email.split("@")
        local = parts[0]
        domain = parts[1]
        masked = local[0] + "***" + local[-1] + "@" + domain
    else:
        masked = "the approver"

    return {"status": "confirmation_sent", "decision": body.decision,
            "request_id": info["request_id"], "masked_email": masked}


@app.get("/api/email-confirm/{token}")
async def email_confirm(token: str, cfg: Settings = Depends(get_settings)):
    if not cfg.magic_link_secret:
        return HTMLResponse(magic_link.render_error_page("Email approval links are not configured."), status_code=500)
    try:
        info = magic_link.verify_confirmation_token(cfg.magic_link_secret, token)
    except magic_link.TokenError as exc:
        return HTMLResponse(magic_link.render_error_page(str(exc)), status_code=400)

    item = store.get_request(cfg, info["request_id"])
    if not item:
        return HTMLResponse(magic_link.render_error_page("Request not found."), status_code=404)
    if item["status"] != store.PENDING:
        return HTMLResponse(magic_link.render_error_page(
            f"This request is no longer pending (status: {item['status']})."), status_code=409)

    approver = None
    for a in item["required_approvers"]:
        a_oid = a.get("oid") or a.get("key", "")
        if a_oid == info["approver_oid"]:
            approver = a
            break
    if not approver:
        return HTMLResponse(magic_link.render_error_page("You are not an approver for this request."), status_code=403)

    key = approver["key"]
    decision_type = info["decision"]
    comment = info["comment"]

    decision = {"decision": decision_type, "name": approver.get("name", ""),
                "comment": comment, "at": store.now_iso()}
    try:
        item = store.record_decision(cfg, info["request_id"], key, decision)
    except store.Conflict:
        return HTMLResponse(magic_link.render_error_page(
            "This decision has already been recorded."), status_code=409)

    actor = User(oid=info["approver_oid"], upn=approver.get("upn", ""), name=approver.get("name", ""))
    audit.event(actor, decision_type, info["request_id"], comment=comment,
                approved_roles=item.get("approved_roles"))
    await _post_decision(cfg, info["request_id"], item, decision_type, decision, actor)
    return HTMLResponse(magic_link.render_confirm_result_page(decision_type, item))
