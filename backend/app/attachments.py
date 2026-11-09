"""Manager-approval attachments in S3.

Files go straight from the browser to S3 with a presigned POST, so they never
pass through API Gateway or Lambda (both cap request size). Keys are scoped to
the uploader, and the backend checks that scope before attaching a file to a
request. Downloads use presigned GET links that expire after five minutes.
"""
from __future__ import annotations

import logging
import os
import re
import uuid
from typing import TYPE_CHECKING

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from .auth import User
    from .config import Settings

logger = logging.getLogger("reltio_access.attachments")

_EXPIRY_S = 300
_s3 = None


def _client():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3", config=Config(signature_version="s3v4"))
    return _s3


def safe_filename(name: str) -> str:
    base = os.path.basename(str(name).replace("\\", "/"))
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", base).strip(" .") or "attachment"
    return cleaned[:120]


def presign_upload(settings: "Settings", user: "User", filename: str, size: int) -> dict:
    if not settings.attachments_bucket:
        raise ValueError("Attachments aren’t set up for this deployment.")
    name = safe_filename(filename)
    content_type = settings.attachment_types.get(os.path.splitext(name)[1].lower())
    if not content_type:
        raise ValueError("Attach the email as .msg or .eml, or as a PDF or image.")
    if size > settings.attachment_max_bytes:
        raise ValueError(f"That file is too large. The limit is {settings.attachment_max_bytes // (1024 * 1024)} MB.")
    key = f"uploads/{user.oid}/{uuid.uuid4().hex}/{name}"
    post = _client().generate_presigned_post(
        Bucket=settings.attachments_bucket,
        Key=key,
        Fields={"Content-Type": content_type},
        Conditions=[{"Content-Type": content_type},
                    ["content-length-range", 1, settings.attachment_max_bytes]],
        ExpiresIn=_EXPIRY_S,
    )
    logger.info("Presigned upload for %s (%s, %d bytes)", name, user.upn, size)
    return {"url": post["url"], "fields": post["fields"], "key": key}


def verify_upload(settings: "Settings", user: "User", key: str) -> dict:
    if not key.startswith(f"uploads/{user.oid}/"):
        raise ValueError("That attachment wasn’t uploaded by you. Attach the file again.")
    try:
        head = _client().head_object(Bucket=settings.attachments_bucket, Key=key)
    except ClientError:
        raise ValueError("The attachment didn’t finish uploading. Attach the file again.")
    logger.info("Verified upload %s for %s", key, user.upn)
    return {"key": key, "filename": key.rsplit("/", 1)[-1], "size": int(head["ContentLength"]),
            "content_type": head.get("ContentType", "")}


def presign_download(settings: "Settings", key: str, filename: str) -> str:
    return _client().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.attachments_bucket, "Key": key,
                "ResponseContentDisposition": f'attachment; filename="{safe_filename(filename)}"'},
        ExpiresIn=_EXPIRY_S,
    )
