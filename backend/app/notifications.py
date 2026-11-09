"""In-app notification persistence (DynamoDB).

Table: recipient_oid (PK), sort_key (SK = ISO timestamp + notif_id, for ordering).
Each item has a TTL of 90 days and a ``read`` boolean flag.
"""
from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, List

import boto3

if TYPE_CHECKING:
    from .config import Settings

_table = None


def _tbl(settings: "Settings"):
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(settings.notifications_table)
    return _table


def _ttl_epoch(days: int = 90) -> int:
    return int((datetime.now(timezone.utc) + timedelta(days=days)).timestamp())


def put(settings: "Settings", recipient_oid: str, title: str, body: str,
        request_id: str | None = None, link: str | None = None) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    nid = secrets.token_hex(4)
    _tbl(settings).put_item(Item={
        "recipient_oid": recipient_oid,
        "sort_key": f"{now}#{nid}",
        "title": title,
        "body": body,
        "request_id": request_id or "",
        "link": link or "",
        "read": False,
        "created_at": now,
        "ttl": _ttl_epoch(),
    })


def list_recent(settings: "Settings", recipient_oid: str, limit: int = 30) -> List[dict]:
    from .store import plain
    resp = _tbl(settings).query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("recipient_oid").eq(recipient_oid),
        ScanIndexForward=False,
        Limit=limit,
    )
    return [plain(i) for i in resp.get("Items", [])]


def count_unread(settings: "Settings", recipient_oid: str) -> int:
    resp = _tbl(settings).query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("recipient_oid").eq(recipient_oid),
        FilterExpression=boto3.dynamodb.conditions.Attr("read").eq(False),
        Select="COUNT",
    )
    return resp.get("Count", 0)


async def mark_all_read(settings: "Settings", recipient_oid: str) -> int:
    items = list_recent(settings, recipient_oid, limit=100)
    unread = [i for i in items if not i.get("read")]
    if not unread:
        return 0
    tbl = _tbl(settings)

    def _update(item):
        tbl.update_item(
            Key={"recipient_oid": item["recipient_oid"], "sort_key": item["sort_key"]},
            UpdateExpression="SET #r = :t",
            ExpressionAttributeNames={"#r": "read"},
            ExpressionAttributeValues={":t": True},
        )

    await asyncio.gather(*(asyncio.to_thread(_update, i) for i in unread))
    return len(unread)
