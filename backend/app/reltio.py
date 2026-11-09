"""Thin async client for the Reltio Auth API.

Holds the Reltio service credential server-side, mints and caches the
client_credentials token (~60 min lifetime), and exposes only the calls this
app needs.
"""
import asyncio
import json
import logging
import time
from typing import Any, Dict, Optional
from urllib.parse import quote

import httpx

from .config import Settings

logger = logging.getLogger("reltio_access.reltio")

_credentials: Optional[Dict[str, str]] = None
_token_cache: Dict[str, Any] = {"token": None, "expires_at": 0.0}
_meta_cache: Dict[str, Any] = {"data": None, "at": 0.0}
_TOKEN_SAFETY_BUFFER_S = 60
_META_TTL_S = 300

_token_lock = asyncio.Lock()
_meta_lock = asyncio.Lock()

_TRANSIENT_CODES = frozenset({429, 502, 503, 504})
_MAX_RETRIES = 2
_RETRY_BACKOFF_S = 1.0

_http: Optional[httpx.AsyncClient] = None


def _client() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(timeout=30, limits=httpx.Limits(max_connections=10))
    return _http


class ReltioError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def _load_credentials(settings: Settings) -> Dict[str, str]:
    global _credentials
    if _credentials is not None:
        return _credentials
    if settings.reltio_secret_arn:
        import boto3

        secret = boto3.client("secretsmanager").get_secret_value(SecretId=settings.reltio_secret_arn)
        data = json.loads(secret["SecretString"])
        _credentials = {"client_id": data["client_id"], "client_secret": data["client_secret"]}
    elif settings.reltio_client_id and settings.reltio_client_secret:
        _credentials = {"client_id": settings.reltio_client_id, "client_secret": settings.reltio_client_secret}
    else:
        raise ReltioError("No Reltio credential configured (set RELTIO_SECRET_ARN).", 500)
    return _credentials


async def _get_token(settings: Settings) -> str:
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"]:
        return _token_cache["token"]
    async with _token_lock:
        now = time.time()
        if _token_cache["token"] and now < _token_cache["expires_at"]:
            return _token_cache["token"]
        creds = await asyncio.to_thread(_load_credentials, settings)
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = await _client().post(
                    f"{settings.auth_base_url}/oauth/token",
                    auth=(creds["client_id"], creds["client_secret"]),
                    data={"grant_type": "client_credentials"},
                )
                if resp.status_code == 200:
                    body = resp.json()
                    _token_cache["token"] = body["access_token"]
                    _token_cache["expires_at"] = now + int(body.get("expires_in", 3600)) - _TOKEN_SAFETY_BUFFER_S
                    return _token_cache["token"]
                if resp.status_code not in _TRANSIENT_CODES or attempt == _MAX_RETRIES:
                    logger.error("Token request failed: %s %s", resp.status_code, resp.text[:300])
                    raise ReltioError("Couldn't sign the service account in to Reltio.")
                logger.warning("Token transient %s (attempt %d)", resp.status_code, attempt + 1)
            except httpx.TransportError as exc:
                last_exc = exc
                if attempt == _MAX_RETRIES:
                    raise ReltioError(f"Token request transport error: {exc}")
                logger.warning("Token transport error (attempt %d): %s", attempt + 1, exc)
            await asyncio.sleep(_RETRY_BACKOFF_S * (2 ** attempt))
        raise last_exc or ReltioError("Token request failed after retries.")


async def _headers(settings: Settings) -> Dict[str, str]:
    return {"Authorization": f"Bearer {await _get_token(settings)}"}


async def _retry(method: str, url: str, settings: Settings, **kwargs) -> httpx.Response:
    """HTTP call with retry on transient errors."""
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = await getattr(_client(), method)(url, headers=await _headers(settings), **kwargs)
            if resp.status_code not in _TRANSIENT_CODES or attempt == _MAX_RETRIES:
                return resp
            logger.warning("Transient %s on %s %s (attempt %d)", resp.status_code, method.upper(), url, attempt + 1)
        except httpx.TransportError as exc:
            last_exc = exc
            if attempt == _MAX_RETRIES:
                raise
            logger.warning("Transport error on %s %s (attempt %d): %s", method.upper(), url, attempt + 1, exc)
        await asyncio.sleep(_RETRY_BACKOFF_S * (2 ** attempt))
    raise last_exc  # type: ignore[misc]


async def get_customer_metadata(settings: Settings) -> Dict[str, Any]:
    """The customer's tenants and customer-specific roles (cached for 5 minutes)."""
    if _meta_cache["data"] and time.time() - _meta_cache["at"] < _META_TTL_S:
        return _meta_cache["data"]
    async with _meta_lock:
        if _meta_cache["data"] and time.time() - _meta_cache["at"] < _META_TTL_S:
            return _meta_cache["data"]
        url = f"{settings.auth_base_url}/oauth/customers/{settings.reltio_customer_id}"
        resp = await _client().get(url, headers=await _headers(settings))
        if resp.status_code != 200:
            logger.error("Customer metadata failed: %s %s", resp.status_code, resp.text[:300])
            raise ReltioError("Couldn't read the tenant and role list from Reltio.")
        data = resp.json()
        _meta_cache["data"] = {"tenants": data.get("tenants", []),
                               "customer_specific_roles": data.get("customerSpecificRoles", [])}
        _meta_cache["at"] = time.time()
        return _meta_cache["data"]


async def get_valid_role_names(settings: Settings) -> set:
    meta = await get_customer_metadata(settings)
    return set(meta["customer_specific_roles"]) | set(settings.system_roles)


def _user_url(settings: Settings, username: str) -> str:
    return f"{settings.auth_base_url}/oauth/users/{quote(username, safe='')}"


async def get_user(settings: Settings, username: str) -> Optional[Dict[str, Any]]:
    resp = await _retry("get", _user_url(settings, username), settings)
    if resp.status_code == 200:
        return resp.json()
    if resp.status_code in (404, 403):
        if resp.status_code == 403:
            logger.info("get_user %s returned 403 — treating as new user", username)
        return None
    logger.error("get_user failed: %s %s", resp.status_code, resp.text[:300])
    raise ReltioError(f"Couldn't look up {username} in Reltio ({resp.status_code}).")


async def create_user(settings: Settings, user_object: Dict[str, Any]) -> Any:
    """POST /oauth/users. Reltio requires an array body."""
    resp = await _retry("post", f"{settings.auth_base_url}/oauth/users", settings, json=[user_object])
    if resp.status_code in (200, 201):
        return resp.json()
    if resp.status_code == 403:
        raise ReltioError("The Reltio service account isn't allowed to create users "
                          "(it needs ROLE_ADMIN_USER or ROLE_ADMIN_CUSTOMER).")
    logger.error("create_user failed: %s %s", resp.status_code, resp.text[:500])
    raise ReltioError(f"Reltio rejected the new user: {resp.text[:300]}")


async def update_user(settings: Settings, username: str, user_object: Dict[str, Any]) -> Any:
    """PUT /oauth/users/{username}. Replaces the whole user, so send the merged object."""
    resp = await _retry("put", _user_url(settings, username), settings, json=user_object)
    if resp.status_code == 200:
        return resp.json()
    if resp.status_code == 403:
        raise ReltioError("The Reltio service account isn't allowed to update users "
                          "(Reltio's docs say updates need ROLE_ADMIN_CUSTOMER).")
    logger.error("update_user failed: %s %s", resp.status_code, resp.text[:500])
    raise ReltioError(f"Reltio rejected the role update: {resp.text[:300]}")
