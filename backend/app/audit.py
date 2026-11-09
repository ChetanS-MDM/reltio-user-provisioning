"""Structured audit log: one JSON line per action, to CloudWatch.

Point a log subscription at logger "reltio_access.audit" to forward these to
your SIEM. The request record in DynamoDB is the other half of the trail.
"""
import json
import logging

_logger = logging.getLogger("reltio_access.audit")


def event(actor, action: str, request_id: str, **details) -> None:
    _logger.info(json.dumps({
        "event": "reltio_access_request",
        "action": action,
        "request_id": request_id,
        "actor_oid": getattr(actor, "oid", None),
        "actor_upn": getattr(actor, "upn", None),
        **details,
    }, default=str))
