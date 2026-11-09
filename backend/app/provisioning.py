"""Turn an approved request into Reltio access.

New L-ID: create the user with the approved roles on the approved tenants.
Existing L-ID: add the approved roles and tenants to what the user already has.
Nothing is ever removed, and running it twice changes nothing the second time,
so a failed attempt can safely be retried.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Dict, List, Tuple

if TYPE_CHECKING:
    from .config import Settings

logger = logging.getLogger("reltio_access.provisioning")

# Fields the Reltio Auth server manages itself. They're stripped before a PUT,
# because PUT replaces the whole user object.
SERVER_MANAGED = {
    "createdDate", "createdBy", "updatedDate", "updatedBy", "lastLoginDate",
    "lastPasswordChangeDate", "encrypted", "numberFailedLoginAttempts",
    "accountNonLockedFailedMFALoginAttempts", "entity", "roles", "tenants",
}


class ProvisioningError(Exception):
    pass


def build_new_user(customer: str, username: str, email: str,
                   roles: List[str], tenants: List[str]) -> dict:
    return {
        "customer": customer,
        "username": username,
        "email": email,
        "enabled": True,
        "externalUser": True,      # Lilly users sign in with SSO
        "locale": "en",
        "timezone": "UTC",
        "userPermissions": {"roles": {r: list(tenants) for r in roles}},
        "groups": [],
    }


def merge_roles(existing: dict, roles: List[str], tenants: List[str]
                ) -> Tuple[dict, Dict[str, List[str]], Dict[str, List[str]]]:
    """Add roles x tenants to an existing user. Returns (body, added, already_had)."""
    perms = dict(existing.get("userPermissions") or {})
    current = {r: list(ts) for r, ts in (perms.get("roles") or {}).items()}
    added: Dict[str, List[str]] = {}
    already: Dict[str, List[str]] = {}
    for role in roles:
        have = current.get(role, [])
        new = [t for t in tenants if t not in have]
        old = [t for t in tenants if t in have]
        if new:
            current[role] = have + new
            added[role] = new
        if old:
            already[role] = old
    perms["roles"] = current
    body = {k: v for k, v in existing.items() if k not in SERVER_MANAGED}
    body["userPermissions"] = perms
    return body, added, already


async def provision(settings: "Settings", item: dict) -> dict:
    from . import reltio  # imported here so the helpers above stay dependency-free

    username = item["global_id"]
    roles = list(item.get("approved_roles") or item["reltio_roles"])
    tenants = list(item["tenants"])

    # Roles were validated at submission; check again in case Reltio changed since.
    valid = await reltio.get_valid_role_names(settings)
    missing = [r for r in roles if r not in valid]
    if missing:
        logger.warning("Skipping roles not recognized by Reltio: %s", missing)
        roles = [r for r in roles if r in valid]
        if not roles:
            raise ProvisioningError(f"Reltio doesn't recognize any of the requested roles: {', '.join(missing)}.")

    logger.info("Looking up Reltio user %s", username)
    existing = await reltio.get_user(settings, username)
    if existing is None:
        logger.info("User %s not found; creating with %d roles on %d tenants",
                     username, len(roles), len(tenants))
        await reltio.create_user(settings, build_new_user(
            settings.reltio_customer_id, username, item["email"], roles, tenants))
        logger.info("Created Reltio user %s", username)
        return {"action": "created", "added": {r: tenants for r in roles},
                "already_had": {}, "warnings": [f"Skipped roles not in Reltio: {', '.join(missing)}"] if missing else []}

    if existing.get("customer") not in (None, settings.reltio_customer_id):
        raise ProvisioningError(f"{username} belongs to Reltio customer {existing.get('customer')}, "
                                f"not {settings.reltio_customer_id}. Nothing was changed.")

    warnings = []
    if missing:
        warnings.append(f"Skipped roles not in Reltio: {', '.join(missing)}")
    if (existing.get("email") or "").lower() != item["email"].lower():
        warnings.append(f"Reltio has {existing.get('email') or 'no email'} for {username}; "
                        "the email wasn’t changed.")
    if existing.get("enabled") is False:
        warnings.append(f"{username} is disabled in Reltio, so the account stays disabled.")

    body, added, already = merge_roles(existing, roles, tenants)
    if not added:
        logger.info("User %s already has all requested roles; no change", username)
        return {"action": "no_change", "added": {}, "already_had": already, "warnings": warnings}
    logger.info("Updating existing user %s with %d new role-tenant grants", username, len(added))
    await reltio.update_user(settings, username, body)
    logger.info("Updated Reltio user %s successfully", username)
    return {"action": "updated", "added": added, "already_had": already, "warnings": warnings}
