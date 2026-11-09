"""DynamoDB persistence for access requests.

Table (see infra/template.yaml):
  key  request_id
  GSI  by_status     (status, created_at)
  GSI  by_requester  (requester_oid, created_at)

Every state change is a conditional write, so two approvers clicking at the
same moment can't record a decision twice or grant access twice.
"""
from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, List, Optional

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from .config import Settings

PENDING = "PENDING"
PROVISIONING = "PROVISIONING"
COMPLETED = "COMPLETED"
REJECTED = "REJECTED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"
CLOSED = [PROVISIONING, COMPLETED, FAILED, REJECTED, CANCELLED]

_table = None


class Conflict(Exception):
    """The request wasn't in the state the write required."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ttl_epoch(days: int = 365) -> int:
    return int((datetime.now(timezone.utc) + timedelta(days=days)).timestamp())


def new_request_id() -> str:
    return f"AR-{datetime.now(timezone.utc):%Y%m%d}-{secrets.token_hex(3).upper()}"


def _tbl(settings: "Settings"):
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(settings.requests_table)
    return _table


def plain(obj: Any) -> Any:
    """DynamoDB returns numbers as Decimal; turn them back into int/float."""
    if isinstance(obj, list):
        return [plain(x) for x in obj]
    if isinstance(obj, dict):
        return {k: plain(v) for k, v in obj.items()}
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    return obj


def put_request(settings: "Settings", item: dict) -> None:
    item.setdefault("ttl", _ttl_epoch())
    _tbl(settings).put_item(Item=item, ConditionExpression="attribute_not_exists(request_id)")


def get_request(settings: "Settings", request_id: str) -> Optional[dict]:
    item = _tbl(settings).get_item(Key={"request_id": request_id}).get("Item")
    return plain(item) if item else None


def _query(settings: "Settings", index: str, key: str, value: str, limit: int) -> List[dict]:
    resp = _tbl(settings).query(IndexName=index, KeyConditionExpression=Key(key).eq(value),
                                ScanIndexForward=False, Limit=limit)
    return [plain(i) for i in resp.get("Items", [])]


def list_by_requester(settings: "Settings", oid: str, limit: int = 50) -> List[dict]:
    return _query(settings, "by_requester", "requester_oid", oid, limit)


def list_by_status(settings: "Settings", status: str, limit: int = 50) -> List[dict]:
    return _query(settings, "by_status", "status", status, limit)


async def list_recent(settings: "Settings", limit: int = 30) -> List[dict]:
    batches = await asyncio.gather(
        *(asyncio.to_thread(list_by_status, settings, s, limit) for s in CLOSED)
    )
    items: List[dict] = []
    for batch in batches:
        items += batch
    items.sort(key=lambda i: i.get("updated_at", ""), reverse=True)
    return items[:limit]


def record_decision(settings: "Settings", request_id: str, approver: str, decision: dict) -> dict:
    """Record one approver's decision. A rejection also closes the request."""
    names = {"#s": "status", "#ap": "approvals", "#k": approver, "#u": "updated_at"}
    values = {":d": decision, ":now": now_iso(), ":pending": PENDING}
    update = "SET #ap.#k = :d, #u = :now"
    if decision["decision"] == "reject":
        update += ", #s = :rejected"
        values[":rejected"] = REJECTED
    try:
        resp = _tbl(settings).update_item(
            Key={"request_id": request_id},
            UpdateExpression=update,
            ConditionExpression="#s = :pending AND attribute_not_exists(#ap.#k)",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise Conflict("This request was already decided, or you’ve already recorded a decision.")
        raise
    return plain(resp["Attributes"])


def transition(settings: "Settings", request_id: str, from_status: str, to_status: str,
               **fields: Any) -> bool:
    """Move from one status to another only if the request is still in from_status."""
    names = {"#s": "status", "#u": "updated_at"}
    values = {":from": from_status, ":to": to_status, ":now": now_iso()}
    sets = ["#s = :to", "#u = :now"]
    for i, (name, value) in enumerate(fields.items()):
        names[f"#f{i}"] = name
        values[f":v{i}"] = value
        sets.append(f"#f{i} = :v{i}")
    try:
        _tbl(settings).update_item(
            Key={"request_id": request_id},
            UpdateExpression="SET " + ", ".join(sets),
            ConditionExpression="#s = :from",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
