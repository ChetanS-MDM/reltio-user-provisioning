"""Optional email and in-app notifications.

Email: off unless NOTIFY_SENDER is set. Failures are logged and never block a request.
In-app: always written to DynamoDB (NotificationsTable).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Iterable, List

if TYPE_CHECKING:
    from .config import Settings

logger = logging.getLogger("reltio_access.notify")

_ses_client = None


def _ses():
    global _ses_client
    if _ses_client is None:
        import boto3
        _ses_client = boto3.client("ses")
    return _ses_client


def _esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _send(cfg: "Settings", to: Iterable[str], subject: str, body: str, html_body: str = "") -> None:
    recipients = sorted({a for a in to if a and "@" in a})
    if not cfg.notify_sender or not recipients:
        return
    try:
        email_body: dict = {"Text": {"Data": body, "Charset": "UTF-8"}}
        if html_body:
            email_body["Html"] = {"Data": html_body, "Charset": "UTF-8"}
        _ses().send_email(
            Source=cfg.notify_sender,
            Destination={"ToAddresses": recipients},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": email_body,
            },
        )
    except Exception as exc:
        logger.warning("Notification email to %s failed: %s", recipients, exc)


def _notify_in_app(cfg: "Settings", oids: List[str], title: str, body: str,
                    request_id: str | None = None) -> None:
    from . import notifications
    link = f"#request-{request_id}" if request_id else ""
    for oid in oids:
        if not oid:
            continue
        try:
            notifications.put(cfg, oid, title, body, request_id=request_id, link=link)
        except Exception as exc:
            logger.warning("In-app notification failed for %s: %s", oid, exc)


def _approver_oids(item: dict) -> List[str]:
    return [a.get("oid") or a.get("key", "") for a in item.get("required_approvers", [])]


def _link(cfg: "Settings", item: dict) -> str:
    return f"\n\nOpen the request: {cfg.app_url.rstrip('/')}#request-{item['request_id']}" if cfg.app_url else ""


def _summary(cfg: "Settings", item: dict) -> str:
    tenants = ", ".join(f"{cfg.tenant_labels.get(t, t)} ({t})" if t in cfg.tenant_labels else t
                        for t in item["tenants"])
    return "\n".join([
        f"Request:      {item['request_id']}",
        f"Requested by: {item['requester_name']}",
        f"Global ID:    {item['global_id']}",
        f"Email:        {item['email']}",
        f"Country:      {item['country_code']}",
        f"DoU:          {item['dou']}",
        f"Use case:     {item['use_case']}",
        f"Justification:{' ' + item['justification'] if item.get('justification') else ' —'}",
        f"Tenants:      {tenants}",
        f"Reltio roles: {', '.join(item.get('approved_roles') or item['reltio_roles'])}",
    ])


def _summary_dict(cfg: "Settings", item: dict) -> dict:
    tenants = ", ".join(
        f"{cfg.tenant_labels.get(t, t)} ({t})" if t in cfg.tenant_labels else t
        for t in item["tenants"]
    )
    roles = item.get("approved_roles") or item["reltio_roles"]
    return {
        "Request": item["request_id"],
        "Requested by": item["requester_name"],
        "Global ID": item["global_id"],
        "Email": item["email"],
        "Country": item["country_code"],
        "DoU": item["dou"],
        "Use case": item["use_case"],
        "Tenants": tenants,
        "Reltio roles": ", ".join(roles),
    }


def _html_body(subject: str, heading: str, summary: dict, message: str,
               cta_url: str = "", cta_label: str = "",
               status_color: str = "#8a6d0e") -> str:
    rows = "".join(
        f'<tr><td style="padding:6px 12px;color:#5b6473;font-size:13px;'
        f'white-space:nowrap;vertical-align:top">{_esc(k)}</td>'
        f'<td style="padding:6px 12px;font-size:13px">{_esc(str(v))}</td></tr>'
        for k, v in summary.items()
    )
    cta = ""
    if cta_url and cta_label:
        cta = (
            f'<tr><td colspan="2" style="padding:20px 24px 4px">'
            f'<a href="{_esc(cta_url)}" style="display:inline-block;padding:10px 24px;'
            f'background:#2B5F8A;color:#ffffff;text-decoration:none;'
            f'border-radius:6px;font-weight:600;font-size:14px">{_esc(cta_label)}</a>'
            f'</td></tr>'
        )
    return (
        f'<!DOCTYPE html><html><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width">'
        f'<title>{_esc(subject)}</title></head>'
        f'<body style="margin:0;padding:0;background:#f7f8fa;'
        f'font-family:Segoe UI,system-ui,-apple-system,sans-serif">'
        f'<table width="100%" cellpadding="0" cellspacing="0" style="background:#f7f8fa">'
        f'<tr><td align="center" style="padding:24px 16px">'
        f'<table width="100%" cellpadding="0" cellspacing="0" '
        f'style="max-width:560px;background:#ffffff;border-radius:8px;'
        f'overflow:hidden;border:1px solid #e2e6ec">'
        f'<tr><td style="background:#E01933;padding:16px 24px">'
        f'<span style="font-family:Georgia,serif;font-size:20px;font-weight:700;'
        f'color:#ffffff">Lilly</span>'
        f'<span style="color:rgba(255,255,255,.8);font-size:13px;margin-left:12px">'
        f'Reltio Access Request</span></td></tr>'
        f'<tr><td style="height:4px;background:{status_color}"></td></tr>'
        f'<tr><td style="padding:28px 24px 8px">'
        f'<h1 style="margin:0 0 16px;font-size:18px;font-weight:600;color:#1b2230">'
        f'{_esc(heading)}</h1>'
        f'<p style="margin:0 0 20px;font-size:14px;line-height:1.6;color:#1b2230">'
        f'{_esc(message)}</p></td></tr>'
        f'<tr><td style="padding:0 24px 20px">'
        f'<table width="100%" cellpadding="0" cellspacing="0" '
        f'style="background:#f7f8fa;border-radius:6px;border:1px solid #e2e6ec">'
        f'{rows}</table></td></tr>'
        f'{cta}'
        f'<tr><td style="padding:16px 24px;border-top:1px solid #e2e6ec;'
        f'font-size:12px;color:#5b6473">'
        f'Enterprise Profile Hub &mdash; Reltio Access Request</td></tr>'
        f'</table></td></tr></table></body></html>'
    )


def submitted(cfg: "Settings", item: dict) -> None:
    rid = item["request_id"]
    title = f"New request {rid}"
    body = f"{item['requester_name']} requested Reltio access."
    subject = f"Reltio access request {rid} needs your approval"
    summary = _summary_dict(cfg, item)
    magic_link_urls: list = []

    if cfg.magic_link_secret and cfg.api_base_url:
        from . import magic_link
        for approver in item["required_approvers"]:
            token = magic_link.create_token(
                cfg.magic_link_secret, rid,
                approver.get("oid") or approver.get("key", ""),
                cfg.magic_link_expiry_days,
            )
            cta_url = f"{cfg.api_base_url.rstrip('/')}/api/email-review/{token}"
            magic_link_urls.append(cta_url)
            plain = f"{body}\n\n{_summary(cfg, item)}\n\nReview and decide: {cta_url}"
            html = _html_body(subject=subject,
                              heading="New access request needs your approval",
                              summary=summary,
                              message=f"{item['requester_name']} has requested Reltio access and needs your approval.",
                              cta_url=cta_url, cta_label="Review & approve",
                              status_color="#8a6d0e")
            _send(cfg, [approver["upn"]], subject, plain, html)
    else:
        plain = f"{body}\n\n{_summary(cfg, item)}{_link(cfg, item)}"
        cta_url = f"{cfg.app_url.rstrip('/')}#request-{rid}" if cfg.app_url else ""
        html = _html_body(subject=subject,
                          heading="New access request needs your approval",
                          summary=summary,
                          message=f"{item['requester_name']} has requested Reltio access and needs your approval.",
                          cta_url=cta_url, cta_label="Review request",
                          status_color="#8a6d0e")
        _send(cfg, [a["upn"] for a in item["required_approvers"]], subject, plain, html)

    from . import teams
    teams.send_approval_request(cfg, item, summary, magic_link_urls or None)

    req_subject = f"Your Reltio access request {rid} has been submitted"
    cta_url = f"{cfg.app_url.rstrip('/')}#request-{rid}" if cfg.app_url else ""
    req_plain = (f"Your request {rid} has been submitted and is waiting for approval.\n\n"
                 f"{_summary(cfg, item)}{_link(cfg, item)}")
    req_html = _html_body(subject=req_subject,
                          heading="Request submitted",
                          summary=summary,
                          message=f"Your request {rid} has been submitted and is waiting for approval from {', '.join(a['name'] for a in item['required_approvers'])}.",
                          cta_url=cta_url, cta_label="View your request",
                          status_color="#2B5F8A")
    _send(cfg, [item["requester_upn"]], req_subject, req_plain, req_html)
    _notify_in_app(cfg, _approver_oids(item), title, body, request_id=rid)


def rejected(cfg: "Settings", item: dict, decision: dict) -> None:
    rid = item["request_id"]
    title = f"Request {rid} rejected"
    body = f"{decision['name']} rejected the request."
    subject = f"Reltio access request {rid} was rejected"
    plain = (f"{body}\n\nComment: {decision['comment']}\n\n"
             f"{_summary(cfg, item)}{_link(cfg, item)}")
    summary = _summary_dict(cfg, item)
    msg = f"{decision['name']} rejected request {rid}."
    if decision.get("comment"):
        msg += f" Comment: {decision['comment']}"
    html = _html_body(subject=subject,
                      heading="Request rejected",
                      summary=summary,
                      message=msg,
                      status_color="#5b6473")
    _send(cfg, [item["requester_upn"]], subject, plain, html)
    _notify_in_app(cfg, [item["requester_oid"]], title,
                   f"{body} Comment: {decision['comment']}", request_id=rid)


def completed(cfg: "Settings", item: dict, result: dict) -> None:
    rid = item["request_id"]
    ecdp = (f"\n\nECDP roles are granted separately: {', '.join(item['ecdp_roles'])}."
            if item.get("ecdp_roles") else "")
    title = f"Access granted ({rid})"
    body = "The access was approved and granted in Reltio."
    subject = f"Reltio access granted ({rid})"
    plain = f"{body}\n\n{_summary(cfg, item)}{ecdp}{_link(cfg, item)}"
    cta_url = f"{cfg.app_url.rstrip('/')}#request-{rid}" if cfg.app_url else ""
    summary = _summary_dict(cfg, item)
    html = _html_body(subject=subject,
                      heading="Reltio access granted",
                      summary=summary,
                      message=f"Access has been granted for {item['global_id']} on the requested tenants.",
                      cta_url=cta_url, cta_label="View request",
                      status_color="#2f7d4f")
    _send(cfg, [item["requester_upn"], item["email"]], subject, plain, html)
    _notify_in_app(cfg, [item["requester_oid"]], title, body, request_id=rid)


def provisioning_failed(cfg: "Settings", item: dict, error: str) -> None:
    rid = item["request_id"]
    title = f"Granting failed ({rid})"
    body = f"Approved but Reltio didn't accept the change. You can retry."
    subject = f"Reltio access request {rid} couldn't be granted"
    plain = (f"The request was approved, but Reltio didn't accept the change:\n{error}\n\n"
             f"You can retry from the request page.\n\n{_summary(cfg, item)}{_link(cfg, item)}")
    cta_url = f"{cfg.app_url.rstrip('/')}#request-{rid}" if cfg.app_url else ""
    summary = _summary_dict(cfg, item)
    html = _html_body(subject=subject,
                      heading="Access granting failed",
                      summary=summary,
                      message=f"The Reltio grant for {item['global_id']} failed. An approver can retry from the request page.",
                      cta_url=cta_url, cta_label="View request",
                      status_color="#b23a30")
    _send(cfg, [a["upn"] for a in item["required_approvers"]], subject, plain, html)
    _notify_in_app(cfg, _approver_oids(item), title, body, request_id=rid)


def cancelled(cfg: "Settings", item: dict) -> None:
    rid = item["request_id"]
    title = f"Request {rid} cancelled"
    body = f"{item['requester_name']} cancelled the request."
    subject = f"Reltio access request {rid} was cancelled"
    plain = f"{body}\n\n{_summary(cfg, item)}"
    summary = _summary_dict(cfg, item)
    html = _html_body(subject=subject,
                      heading="Request cancelled",
                      summary=summary,
                      message=f"{item['requester_name']} cancelled request {rid}.",
                      status_color="#5b6473")
    _send(cfg, [a["upn"] for a in item["required_approvers"]], subject, plain, html)
    _notify_in_app(cfg, _approver_oids(item), title, body, request_id=rid)


def partial_approved(cfg: "Settings", item: dict, decision: dict) -> None:
    rid = item["request_id"]
    remaining = [a for a in item["required_approvers"]
                 if a["key"] not in (item.get("approvals") or {})]
    title = f"{decision['name']} approved {rid}"
    body = f"Still waiting on {', '.join(a['name'] for a in remaining)}."
    subject = f"{decision['name']} approved {rid}"
    summary = _summary_dict(cfg, item)
    remaining_names = ", ".join(a["name"] for a in remaining)
    magic_link_urls: list = []

    if cfg.magic_link_secret and cfg.api_base_url:
        from . import magic_link
        for approver in remaining:
            token = magic_link.create_token(
                cfg.magic_link_secret, rid,
                approver.get("oid") or approver.get("key", ""),
                cfg.magic_link_expiry_days,
            )
            cta_url = f"{cfg.api_base_url.rstrip('/')}/api/email-review/{token}"
            magic_link_urls.append(cta_url)
            plain = (f"{decision['name']} approved the request. {body}\n\n"
                     f"{_summary(cfg, item)}\n\nReview and decide: {cta_url}")
            html_body = _html_body(subject=subject,
                                   heading="Partial approval recorded",
                                   summary=summary,
                                   message=f"{decision['name']} approved request {rid}. Still waiting on {remaining_names}.",
                                   cta_url=cta_url, cta_label="Review & approve",
                                   status_color="#8a6d0e")
            _send(cfg, [approver["upn"]], subject, plain, html_body)
        requester_plain = (f"{decision['name']} approved the request. {body}\n\n"
                           f"{_summary(cfg, item)}{_link(cfg, item)}")
        requester_html = _html_body(subject=subject,
                                    heading="Partial approval recorded",
                                    summary=summary,
                                    message=f"{decision['name']} approved request {rid}. Still waiting on {remaining_names}.",
                                    status_color="#8a6d0e")
        _send(cfg, [item["requester_upn"]], subject, requester_plain, requester_html)
    else:
        plain = f"{decision['name']} approved the request. {body}\n\n{_summary(cfg, item)}{_link(cfg, item)}"
        cta_url = f"{cfg.app_url.rstrip('/')}#request-{rid}" if cfg.app_url else ""
        html = _html_body(subject=subject,
                          heading="Partial approval recorded",
                          summary=summary,
                          message=f"{decision['name']} approved request {rid}. Still waiting on {remaining_names}.",
                          cta_url=cta_url, cta_label="Review request",
                          status_color="#8a6d0e")
        _send(cfg, [item["requester_upn"]] + [a["upn"] for a in remaining], subject, plain, html)

    from . import teams
    teams.send_partial_approval(cfg, item, summary, decision, remaining_names,
                                magic_link_urls or None)

    notify_oids = [item["requester_oid"]] + [a.get("oid") or a.get("key", "") for a in remaining]
    _notify_in_app(cfg, notify_oids, title, body, request_id=rid)


def confirmation_email(cfg: "Settings", approver: dict, item: dict,
                       decision: str, comment: str, confirm_url: str) -> None:
    rid = item["request_id"]
    action = "approval" if decision == "approve" else "rejection"
    subject = f"Confirm your {action} of Reltio request {rid}"
    summary = _summary_dict(cfg, item)
    msg = (f"You clicked '{decision}' on request {rid}. "
           f"Click the button below to confirm your decision. "
           f"This link expires in 30 minutes.")
    if comment:
        msg += f" Your comment: {comment}"
    plain = (f"Please confirm your {action} of request {rid}.\n\n"
             f"{_summary(cfg, item)}\n\nConfirm: {confirm_url}")
    html = _html_body(subject=subject,
                      heading=f"Confirm your {action}",
                      summary=summary,
                      message=msg,
                      cta_url=confirm_url,
                      cta_label=f"Confirm {action}",
                      status_color="#8a6d0e")
    _send(cfg, [approver.get("upn", "")], subject, plain, html)
