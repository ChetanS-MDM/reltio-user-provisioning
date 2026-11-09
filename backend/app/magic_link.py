"""Magic-link tokens and standalone approval pages for email-based decisions."""
from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import jwt

if TYPE_CHECKING:
    from .config import Settings


class TokenError(Exception):
    pass


def create_token(secret: str, request_id: str, approver_oid: str, expiry_days: int = 7) -> str:
    payload = {
        "sub": approver_oid,
        "rid": request_id,
        "exp": datetime.now(timezone.utc) + timedelta(days=expiry_days),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def verify_token(secret: str, token: str) -> dict:
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise TokenError("This approval link has expired.")
    except jwt.InvalidTokenError:
        raise TokenError("Invalid approval link.")
    rid = payload.get("rid")
    sub = payload.get("sub")
    if not rid or not sub:
        raise TokenError("Invalid approval link.")
    return {"request_id": rid, "approver_oid": sub}


def create_confirmation_token(secret: str, request_id: str, approver_oid: str,
                              decision: str, comment: str,
                              expiry_minutes: int = 30) -> str:
    payload = {
        "typ": "confirm",
        "sub": approver_oid,
        "rid": request_id,
        "dec": decision,
        "cmt": comment,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=expiry_minutes),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def verify_confirmation_token(secret: str, token: str) -> dict:
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise TokenError("This confirmation link has expired.")
    except jwt.InvalidTokenError:
        raise TokenError("Invalid confirmation link.")
    if payload.get("typ") != "confirm":
        raise TokenError("Invalid confirmation link.")
    rid = payload.get("rid")
    sub = payload.get("sub")
    dec = payload.get("dec")
    if not rid or not sub or dec not in ("approve", "reject"):
        raise TokenError("Invalid confirmation link.")
    return {
        "request_id": rid,
        "approver_oid": sub,
        "decision": dec,
        "comment": payload.get("cmt", ""),
    }


def _esc(s) -> str:
    return html.escape(str(s))


def _base_css() -> str:
    return """
    *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
    body{min-height:100vh;font-family:Inter,system-ui,-apple-system,sans-serif;
         background:#0b0e13;color:#e2e6ec;display:flex;align-items:center;justify-content:center}
    body::before{content:'';position:fixed;inset:0;z-index:-1;
         background:linear-gradient(135deg,#0b0e13 0%,#131923 25%,#1a0a12 50%,#0d1520 75%,#0b0e13 100%);
         animation:drift 20s ease-in-out infinite alternate}
    @keyframes drift{0%{filter:hue-rotate(0deg)}100%{filter:hue-rotate(15deg)}}
    .card{width:100%;max-width:640px;margin:24px;padding:32px;
          background:rgba(255,255,255,.06);backdrop-filter:blur(20px) saturate(180%);
          -webkit-backdrop-filter:blur(20px) saturate(180%);
          border:1px solid rgba(255,255,255,.1);border-radius:16px;
          box-shadow:0 8px 32px rgba(0,0,0,.4)}
    .brand{display:flex;align-items:center;gap:12px;margin-bottom:24px;
           padding-bottom:16px;border-bottom:1px solid rgba(255,255,255,.08)}
    .brand-logo{font-family:Georgia,serif;font-size:22px;font-weight:700;color:#E01933}
    .brand-text{font-size:13px;color:rgba(255,255,255,.5)}
    h1{font-size:20px;font-weight:600;margin-bottom:20px;color:#fff}
    .field{display:flex;justify-content:space-between;padding:10px 0;
           border-bottom:1px solid rgba(255,255,255,.06)}
    .field:last-child{border-bottom:none}
    .label{color:rgba(255,255,255,.5);font-size:13px;min-width:120px;flex-shrink:0}
    .value{font-size:14px;text-align:right;word-break:break-word;max-width:380px}
    .roles{display:flex;flex-wrap:wrap;gap:6px;justify-content:flex-end}
    .chip{display:inline-block;padding:3px 10px;font-size:12px;border-radius:20px;
          background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.1)}
    .section{margin-top:24px}
    .section-title{font-size:13px;font-weight:600;color:rgba(255,255,255,.4);
                   text-transform:uppercase;letter-spacing:.05em;margin-bottom:12px}
    .actions{display:flex;gap:12px;margin-top:28px}
    .btn{flex:1;padding:12px;font-size:15px;font-weight:600;border:none;border-radius:10px;
         cursor:pointer;transition:all .2s}
    .btn-approve{background:#2f7d4f;color:#fff}
    .btn-approve:hover{background:#3a9460}
    .btn-reject{background:rgba(255,255,255,.06);color:#e2e6ec;border:1px solid rgba(255,255,255,.12)}
    .btn-reject:hover{background:rgba(255,255,255,.1)}
    .btn:disabled{opacity:.5;cursor:not-allowed}
    textarea{width:100%;padding:10px;font-size:14px;font-family:inherit;border-radius:8px;
             border:1px solid rgba(255,255,255,.12);background:rgba(255,255,255,.04);
             color:#e2e6ec;resize:vertical;min-height:60px;margin-top:8px}
    textarea:focus{outline:none;border-color:rgba(255,255,255,.25);
                   box-shadow:0 0 0 3px rgba(224,25,51,.15)}
    .attach-btn{display:inline-flex;align-items:center;gap:6px;padding:8px 16px;
                font-size:13px;font-weight:500;border-radius:8px;
                background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.1);
                color:#7db8f0;text-decoration:none;cursor:pointer;transition:all .2s}
    .attach-btn:hover{background:rgba(255,255,255,.1)}
    .note{font-size:13px;color:rgba(255,255,255,.4);margin-top:8px}
    .status{display:inline-block;padding:4px 12px;font-size:12px;font-weight:600;
            border-radius:20px;text-transform:uppercase;letter-spacing:.04em}
    .status-ok{background:rgba(47,125,79,.15);color:#5eba7d}
    .status-reject{background:rgba(178,58,48,.15);color:#e06050}
    .status-warn{background:rgba(138,109,14,.15);color:#d4a821}
    .error-msg{background:rgba(178,58,48,.1);border:1px solid rgba(178,58,48,.25);
               border-radius:8px;padding:12px;color:#e06050;font-size:14px;margin-top:8px}
    @media(prefers-color-scheme:light){
      body{background:#f7f8fa;color:#1b2230}
      body::before{background:linear-gradient(135deg,#f7f8fa 0%,#eef1f6 50%,#f7f8fa 100%)}
      .card{background:rgba(255,255,255,.85);border-color:rgba(0,0,0,.08);
            box-shadow:0 4px 24px rgba(0,0,0,.08)}
      h1{color:#1b2230}
      .label{color:#5b6473}
      .value{color:#1b2230}
      .field{border-color:rgba(0,0,0,.06)}
      .chip{background:rgba(0,0,0,.04);border-color:rgba(0,0,0,.08);color:#1b2230}
      textarea{background:#fff;border-color:rgba(0,0,0,.15);color:#1b2230}
      .btn-reject{background:rgba(0,0,0,.04);color:#1b2230;border-color:rgba(0,0,0,.12)}
      .attach-btn{background:rgba(0,0,0,.04);border-color:rgba(0,0,0,.08);color:#2B5F8A}
      .brand-text{color:#5b6473}
      .brand{border-color:rgba(0,0,0,.08)}
      .section-title{color:#5b6473}
      .note{color:#5b6473}
    }
    """


def render_review_page(item: dict, approver: dict, cfg: "Settings", token: str) -> str:
    e = _esc
    rid = item["request_id"]
    roles = item.get("approved_roles") or item["reltio_roles"]
    tenants_display = ", ".join(
        f"{cfg.tenant_labels.get(t, t)} ({t})" if t in cfg.tenant_labels else t
        for t in item["tenants"]
    )
    chips = "".join(f'<span class="chip">{e(r)}</span>' for r in roles)
    attach_url = f"{cfg.api_base_url.rstrip('/')}/api/email-attachment/{token}"
    decision_url = f"{cfg.api_base_url.rstrip('/')}/api/email-decision"

    approver_key = approver.get("key", "")
    decided = (item.get("approvals") or {}).get(approver_key)
    already_decided = decided is not None
    not_pending = item["status"] != "PENDING"

    if already_decided:
        banner = f"""<div class="error-msg" style="background:rgba(138,109,14,.1);border-color:rgba(138,109,14,.25);color:#d4a821">
            You have already {e(decided['decision'])}d this request.</div>"""
    elif not_pending:
        banner = f"""<div class="error-msg" style="background:rgba(138,109,14,.1);border-color:rgba(138,109,14,.25);color:#d4a821">
            This request is no longer pending (status: {e(item['status'])}).</div>"""
    else:
        banner = ""

    can_decide = not already_decided and not not_pending
    disabled = "" if can_decide else "disabled"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Review request {e(rid)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>{_base_css()}</style>
</head>
<body>
<div class="card">
  <div class="brand">
    <span class="brand-logo">Lilly</span>
    <span class="brand-text">Reltio Access Request</span>
  </div>
  <h1>Access request needs your approval</h1>
  <div style="font-size:13px;color:rgba(255,255,255,.6);margin:-8px 0 16px">
    Reviewing as <strong style="color:rgba(255,255,255,.9)">{e(approver.get('name', approver.get('upn', '')))}</strong>
  </div>
  {banner}
  <div class="section">
    <div class="section-title">Request details</div>
    <div class="field"><span class="label">Request ID</span><span class="value">{e(rid)}</span></div>
    <div class="field"><span class="label">Requested by</span><span class="value">{e(item['requester_name'])}</span></div>
    <div class="field"><span class="label">Global ID</span><span class="value">{e(item['global_id'])}</span></div>
    <div class="field"><span class="label">Email</span><span class="value">{e(item['email'])}</span></div>
    <div class="field"><span class="label">Country</span><span class="value">{e(item['country_code'])}</span></div>
    <div class="field"><span class="label">DoU</span><span class="value">{e(item['dou'])}</span></div>
    <div class="field"><span class="label">Use case</span><span class="value">{e(item['use_case'])}</span></div>
    <div class="field"><span class="label">Justification</span><span class="value">{e(item.get('justification', '—'))}</span></div>
    <div class="field"><span class="label">Tenants</span><span class="value">{e(tenants_display)}</span></div>
    <div class="field"><span class="label">Reltio roles</span><span class="value"><div class="roles">{chips}</div></span></div>
  </div>

  <div class="section">
    <div class="section-title">Attachment</div>
    <a href="{e(attach_url)}" target="_blank" class="attach-btn">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
      {e(item['attachment']['filename'])} ({item['attachment']['size'] // 1024} KB)
    </a>
  </div>

  <div class="section" id="decision-section">
    <div class="section-title">Your decision</div>
    <label style="font-size:13px;color:rgba(255,255,255,.5)">Comment (required for rejection)</label>
    <textarea id="comment" placeholder="Add a comment..." {disabled}></textarea>
    <div class="actions">
      <button class="btn btn-approve" id="approveBtn" {disabled} onclick="decide('approve')">Approve</button>
      <button class="btn btn-reject" id="rejectBtn" {disabled} onclick="decide('reject')">Reject</button>
    </div>
    <div id="result-msg"></div>
  </div>
</div>
<script>
async function decide(decision) {{
  var comment = document.getElementById('comment').value.trim();
  if (decision === 'reject' && !comment) {{
    document.getElementById('result-msg').innerHTML =
      '<div class="error-msg">Please add a comment explaining the rejection.</div>';
    return;
  }}
  document.getElementById('approveBtn').disabled = true;
  document.getElementById('rejectBtn').disabled = true;
  document.getElementById('result-msg').innerHTML = '<p class="note">Processing...</p>';
  try {{
    var res = await fetch('{e(decision_url)}', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{
        token: '{token}',
        decision: decision,
        comment: comment
      }})
    }});
    var data = await res.json();
    if (!res.ok) {{
      throw new Error(data.detail || 'Request failed');
    }}
    var section = document.getElementById('decision-section');
    var maskedEmail = data.masked_email || 'the approver';
    section.innerHTML = '<div style="text-align:center;padding:20px 0">' +
      '<div class="status status-warn">Confirmation required</div>' +
      '<p class="note" style="margin-top:12px">A confirmation email has been sent to <strong>' +
      maskedEmail + '</strong>.</p>' +
      '<p class="note">The decision will only take effect once confirmed via email. ' +
      'The confirmation link expires in 30 minutes.</p></div>';
  }} catch(err) {{
    document.getElementById('result-msg').innerHTML =
      '<div class="error-msg">' + err.message + '</div>';
    document.getElementById('approveBtn').disabled = false;
    document.getElementById('rejectBtn').disabled = false;
  }}
}}
</script>
</body>
</html>"""


def render_error_page(message: str) -> str:
    e = _esc
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Approval link error</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>{_base_css()}</style>
</head>
<body>
<div class="card">
  <div class="brand">
    <span class="brand-logo">Lilly</span>
    <span class="brand-text">Reltio Access Request</span>
  </div>
  <h1>Unable to open approval</h1>
  <div class="error-msg">{e(message)}</div>
  <p class="note" style="margin-top:16px">If you believe this is an error, please contact the EPH team.</p>
</div>
</body>
</html>"""


def render_confirm_result_page(decision: str, item: dict) -> str:
    e = _esc
    rid = item["request_id"]
    if decision == "approve":
        status_cls = "status-ok"
        label = "Approved"
        msg = "The access is being provisioned. The requester will be notified once complete."
    else:
        status_cls = "status-reject"
        label = "Rejected"
        msg = "The requester has been notified."
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Decision confirmed — {e(rid)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>{_base_css()}</style>
</head>
<body>
<div class="card">
  <div class="brand">
    <span class="brand-logo">Lilly</span>
    <span class="brand-text">Reltio Access Request</span>
  </div>
  <h1>Decision confirmed</h1>
  <div style="text-align:center;padding:20px 0">
    <div class="status {status_cls}">{label}</div>
    <p class="note" style="margin-top:12px">{e(msg)}</p>
    <div style="margin-top:24px;padding:16px;background:rgba(255,255,255,.04);border-radius:8px;text-align:left">
      <div class="field"><span class="label">Request ID</span><span class="value">{e(rid)}</span></div>
      <div class="field"><span class="label">Global ID</span><span class="value">{e(item['global_id'])}</span></div>
      <div class="field"><span class="label">Requested by</span><span class="value">{e(item['requester_name'])}</span></div>
    </div>
  </div>
</div>
</body>
</html>"""
