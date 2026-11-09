"""Who is calling: validate the Entra (Azure AD) access token.

API Gateway's JWT authorizer also checks the token (see infra/template.yaml);
this module re-validates it and works out whether the caller is an approver.
"""
import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import jwt
from fastapi import Depends, Header, HTTPException, status

from .config import Settings, get_settings

logger = logging.getLogger("reltio_access.auth")

# PyJWKClient caches signing keys, so warm Lambda invocations reuse them.
_jwk_client: "jwt.PyJWKClient | None" = None


def _get_jwk_client(settings: Settings) -> jwt.PyJWKClient:
    global _jwk_client
    if _jwk_client is None:
        _jwk_client = jwt.PyJWKClient(settings.entra_jwks_uri)
    return _jwk_client


@dataclass(frozen=True)
class User:
    """The signed-in person: a requester, an approver, or both."""
    oid: str
    upn: str
    name: str


async def require_user(
    authorization: str = Header(default=""),
    settings: Settings = Depends(get_settings),
) -> User:
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token.",
                            headers={"WWW-Authenticate": "Bearer"})
    token = authorization.split(" ", 1)[1].strip()
    try:
        jwk_client = _get_jwk_client(settings)
        key = await asyncio.to_thread(jwk_client.get_signing_key_from_jwt, token)
        claims = jwt.decode(
            token, key.key, algorithms=["RS256"],
            audience=settings.entra_api_audience, issuer=settings.entra_issuer,
            options={"require": ["exp", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        logger.warning("Token rejected: %s", exc)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token.",
                            headers={"WWW-Authenticate": "Bearer"})

    if settings.requester_app_role and settings.requester_app_role not in (claims.get("roles") or []):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You don't have access to this tool.")

    oid = str(claims.get("oid") or claims.get("sub") or "")
    if not oid:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "Token lacks a user identifier (oid/sub).",
                            headers={"WWW-Authenticate": "Bearer"})
    upn = (claims.get("preferred_username") or claims.get("upn") or "").lower()
    user = User(oid=oid, upn=upn, name=claims.get("name") or upn)
    logger.info("Authenticated %s (%s)", user.upn, user.oid)
    return user


def approver_key(entry: dict) -> str:
    """Stable key for an approver: Entra object ID if configured, else UPN."""
    return (entry.get("oid") or entry.get("upn") or "").lower()


def matches(entry: dict, user: User) -> bool:
    if entry.get("oid"):
        return entry["oid"].lower() == user.oid.lower()
    return bool(entry.get("upn")) and entry["upn"].lower() == user.upn


def approver_entry(user: User, settings: Settings) -> Optional[dict]:
    return next((a for a in settings.approvers if matches(a, user)), None)
