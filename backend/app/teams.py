"""Microsoft Teams incoming webhook notifications via Adaptive Cards."""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, List
from urllib.request import Request, urlopen
from urllib.error import URLError

if TYPE_CHECKING:
    from .config import Settings

logger = logging.getLogger("reltio_access.teams")


def _fact(title: str, value: str) -> dict:
    return {"title": title, "value": value}


def _build_card(heading: str, summary: dict, message: str,
                cta_url: str = "", cta_label: str = "",
                accent_color: str = "attention") -> dict:
    facts = [_fact(k, str(v)) for k, v in summary.items()]

    body: list = [
        {
            "type": "ColumnSet",
            "columns": [
                {
                    "type": "Column",
                    "width": "auto",
                    "items": [{
                        "type": "TextBlock",
                        "text": "Lilly",
                        "weight": "Bolder",
                        "size": "Large",
                        "color": "Accent",
                    }],
                },
                {
                    "type": "Column",
                    "width": "stretch",
                    "verticalContentAlignment": "Center",
                    "items": [{
                        "type": "TextBlock",
                        "text": "Reltio Access Request",
                        "isSubtle": True,
                        "spacing": "None",
                    }],
                },
            ],
        },
        {
            "type": "TextBlock",
            "text": heading,
            "weight": "Bolder",
            "size": "Medium",
            "wrap": True,
        },
        {
            "type": "TextBlock",
            "text": message,
            "wrap": True,
            "spacing": "Small",
        },
        {
            "type": "FactSet",
            "facts": facts,
        },
    ]

    actions: list = []
    if cta_url and cta_label:
        actions.append({
            "type": "Action.OpenUrl",
            "title": cta_label,
            "url": cta_url,
            "style": "positive",
        })

    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": body,
    }
    if actions:
        card["actions"] = actions

    return card


def _post_webhook(webhook_url: str, card: dict) -> None:
    payload = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "contentUrl": None,
            "content": card,
        }],
    }
    data = json.dumps(payload).encode("utf-8")
    req = Request(webhook_url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=10) as resp:
            resp.read()
    except (URLError, OSError) as exc:
        logger.warning("Teams webhook failed: %s", exc)


def send_approval_request(cfg: "Settings", item: dict, summary: dict,
                          magic_link_urls: List[str] | None = None) -> None:
    if not cfg.teams_webhook_url:
        return

    rid = item["request_id"]
    requester = item["requester_name"]

    if magic_link_urls and len(magic_link_urls) == 1:
        cta_url = magic_link_urls[0]
    elif cfg.app_url:
        cta_url = f"{cfg.app_url.rstrip('/')}#request-{rid}"
    else:
        cta_url = ""

    card = _build_card(
        heading=f"New access request {rid}",
        summary=summary,
        message=f"{requester} has requested Reltio access and needs approval.",
        cta_url=cta_url,
        cta_label="Review & Approve",
    )
    _post_webhook(cfg.teams_webhook_url, card)


def send_partial_approval(cfg: "Settings", item: dict, summary: dict,
                          decision: dict, remaining_names: str,
                          magic_link_urls: List[str] | None = None) -> None:
    if not cfg.teams_webhook_url:
        return

    rid = item["request_id"]

    if magic_link_urls and len(magic_link_urls) == 1:
        cta_url = magic_link_urls[0]
    elif cfg.app_url:
        cta_url = f"{cfg.app_url.rstrip('/')}#request-{rid}"
    else:
        cta_url = ""

    card = _build_card(
        heading=f"Partial approval: {rid}",
        summary=summary,
        message=f"{decision['name']} approved request {rid}. Still waiting on {remaining_names}.",
        cta_url=cta_url,
        cta_label="Review & Approve",
    )
    _post_webhook(cfg.teams_webhook_url, card)
