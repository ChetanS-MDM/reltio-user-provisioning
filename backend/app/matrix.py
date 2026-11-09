"""DoU parsing and roles-matrix resolution.

The matrix (use case -> roles) is published as JSON to S3 and read once per
cold start. resolve() turns a DoU into the roles it authorizes, fills the
country code into country-specific role names, and checks each role against
what Reltio knows, so a typo in the matrix can't silently grant less access.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from .config import Settings

logger = logging.getLogger("reltio_access.matrix")

_matrix_cache: Dict[str, object] = {"index": None, "version": None, "loaded_at": 0.0}
_MATRIX_TTL_S = 900

# Country placeholders in the matrix, e.g. "<2 DIGIT Country Code or GLOBAL>".
PLACEHOLDER = re.compile(r"<[^<>]*>")


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", str(name)).strip().lower()


def parse_dou(dou: str) -> str:
    """'~2578 Enterprise Profile-DOU-Global EPH IT Analyst Read Only V18'
    -> 'Global EPH IT Analyst Read Only'

    The descriptor ('Read Only') is kept: 'Global EPH IT Analyst' and
    'Global EPH IT Analyst Read Only' are different use cases.
    """
    s = str(dou).strip()
    s = re.sub(r"^~?\s*\d+\s*", "", s)
    s = re.sub(r"(?i)^enterprise profile\s*-\s*dou\s*-\s*", "", s)
    s = re.sub(r"(?i)\s*V\d+\s*$", "", s)
    return re.sub(r"\s+", " ", s).strip()


def normalize_country(code: Optional[str], settings: "Settings") -> Optional[str]:
    """Two letters (ISO 3166 alpha-2), GLOBAL, or a configured extra code."""
    c = (code or "").strip().upper()
    if re.fullmatch(r"[A-Z]{2}", c) or c == "GLOBAL" or c in settings.extra_countries:
        return c
    return None


def _fetch_from_s3(bucket: str, key: str) -> dict:
    import boto3
    obj = boto3.client("s3").get_object(Bucket=bucket, Key=key)
    return json.loads(obj["Body"].read())


async def _load_index(settings: "Settings") -> Dict[str, dict]:
    if _matrix_cache["index"] is not None and time.time() - _matrix_cache["loaded_at"] < _MATRIX_TTL_S:
        return _matrix_cache["index"]  # type: ignore[return-value]
    if settings.roles_matrix_bucket:
        raw = await asyncio.to_thread(_fetch_from_s3, settings.roles_matrix_bucket, settings.roles_matrix_key)
    elif settings.roles_matrix_local_path:
        with open(settings.roles_matrix_local_path) as f:
            raw = json.load(f)
    else:
        raise RuntimeError("No roles matrix configured (set ROLES_MATRIX_BUCKET).")
    index = {_norm(name): {"use_case": name, **entry} for name, entry in raw["use_cases"].items()}
    _matrix_cache["index"] = index
    _matrix_cache["version"] = raw.get("source_version")
    _matrix_cache["loaded_at"] = time.time()
    logger.info("Loaded roles matrix version=%s use_cases=%d", _matrix_cache["version"], len(index))
    return index


async def version(settings: "Settings") -> Optional[str]:
    await _load_index(settings)
    return _matrix_cache["version"]  # type: ignore[return-value]


@dataclass
class ResolveResult:
    dou: str
    use_case: str
    matched: bool = True
    retired: bool = False
    country_scoped: bool = False
    reltio_roles: List[str] = field(default_factory=list)   # exist in Reltio; would be granted
    unknown_roles: List[str] = field(default_factory=list)  # named by the matrix, not in Reltio
    ecdp_roles: List[str] = field(default_factory=list)     # shown only; granted in ECDP
    error: Optional[str] = None

    @property
    def grantable(self) -> bool:
        return (self.matched and not self.retired and not self.error
                and bool(self.reltio_roles))


async def resolve(settings: "Settings", dou: str, valid_reltio_roles: set,
            country_code: Optional[str] = None) -> ResolveResult:
    use_case = parse_dou(dou)
    entry = (await _load_index(settings)).get(_norm(use_case))
    if entry is None:
        return ResolveResult(dou=dou, use_case=use_case, matched=False,
                             error=f"No use case in the roles matrix matches “{use_case}”. Check the DoU text.")

    raw = list(entry.get("reltio_roles", []))
    result = ResolveResult(
        dou=dou, use_case=entry["use_case"], retired=bool(entry.get("retired")),
        country_scoped=any(PLACEHOLDER.search(r) for r in raw),
        ecdp_roles=list(entry.get("ecdp_roles", [])),
    )
    if result.country_scoped and not country_code:
        result.error = "This DoU is country-specific. Enter a country code to see its roles."
        return result

    roles: List[str] = []
    for r in raw:
        name = PLACEHOLDER.sub(country_code, r) if country_code else r
        if name not in roles:
            roles.append(name)
    result.reltio_roles = [r for r in roles if r in valid_reltio_roles]
    result.unknown_roles = [r for r in roles if r not in valid_reltio_roles]
    return result
