"""Application configuration, loaded from environment variables.

Nothing secret is hard-coded. The Reltio client credential comes from AWS
Secrets Manager (RELTIO_SECRET_ARN); the client-id/secret fallbacks are for
local development only.

Lists people type by hand (tenant allowlist, origins, email domains, country
codes, system roles) are comma-separated strings. Structured settings
(approvers, tenant labels, attachment types) are JSON.
"""
from functools import cached_property, lru_cache
from typing import Dict, List, Literal, Optional

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_SYSTEM_ROLES = ",".join([
    "ROLE_API", "ROLE_API_EXPORT", "ROLE_API_NO_EXPORT", "ROLE_UI", "ROLE_USER",
    "ROLE_WORKFLOW", "ROLE_WORKFLOW_AFFILIATE", "ROLE_REVIEWER", "ROLE_REVIEWER_AFFILIATE",
    "ROLE_ACTIVITIES", "ROLE_RDM", "ROLE_RDM_EDIT", "ROLE_DATALOADER", "ROLE_EXTERNAL_MATCH",
    "ROLE_INTEGRATION_SPECIALIST", "ROLE_Integration_Development", "ACCEPT_CHANGE_REQUEST",
])


def _csv(value: Optional[str]) -> List[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Reltio Auth API ---------------------------------------------------
    auth_base_url: str = "https://auth.reltio.com"
    reltio_customer_id: str = "EliLilly"
    reltio_secret_arn: Optional[str] = None      # {"client_id": ..., "client_secret": ...}
    reltio_client_id: Optional[str] = None       # local dev only
    reltio_client_secret: Optional[str] = None   # local dev only

    # --- Entra (Azure AD) --------------------------------------------------
    entra_tenant_id: str = ""
    entra_api_audience: str = ""                 # API app client ID or api://<client-id>
    # Optional app role a person needs to submit requests. Empty = any signed-in Lilly user.
    requester_app_role: Optional[str] = None

    # --- Approvals ---------------------------------------------------------
    # JSON list of {"name": "...", "upn": "...", "oid": "..."}. When "oid" (the
    # Entra object ID) is present it's matched instead of the UPN; it never changes.
    approvers: List[Dict[str, str]] = Field(default_factory=list)
    approval_policy: Literal["all", "any"] = "all"

    # --- Request validation ------------------------------------------------
    global_id_pattern: str = r"^.+$"
    allowed_email_domains: str = "lilly.com"
    extra_country_codes: str = ""                # e.g. country-group codes, comma-separated

    # --- Storage -----------------------------------------------------------
    requests_table: str = "ReltioAccessRequests"
    attachments_bucket: Optional[str] = None
    attachment_max_bytes: int = 10 * 1024 * 1024
    attachment_types: Dict[str, str] = Field(default_factory=lambda: {
        ".msg": "application/vnd.ms-outlook",
        ".eml": "message/rfc822",
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
    })

    # --- Roles matrix (DoU -> roles), published by scripts/publish_matrix.py --
    roles_matrix_bucket: Optional[str] = None
    roles_matrix_key: str = "config/roles_matrix.json"
    roles_matrix_local_path: Optional[str] = None
    # Standard roles that exist in Reltio but aren't customer-specific roles.
    known_system_roles: str = _SYSTEM_ROLES

    # --- Tenants -----------------------------------------------------------
    tenant_allowlist: str = ""                   # comma-separated tenant IDs; empty = all
    # JSON {"<tenant id>": "<label>"}; requesters see labels, in this order.
    tenant_labels: Dict[str, str] = Field(default_factory=dict)

    # --- In-app notifications ------------------------------------------------
    notifications_table: str = "ReltioAccessNotifications"

    # --- Email notifications (optional, via Amazon SES) --------------------
    notify_sender: Optional[str] = None
    app_url: Optional[str] = None                # front-end URL, used in email links

    # --- Magic-link email approvals ----------------------------------------
    magic_link_secret: str = ""                  # HMAC signing key for approval tokens
    magic_link_expiry_days: int = 7
    api_base_url: str = ""                       # backend URL for magic links

    # --- Microsoft Teams webhook (optional) --------------------------------
    teams_webhook_url: str = ""                  # incoming webhook URL for approval notifications

    # --- Web ---------------------------------------------------------------
    cors_allowed_origins: str = ""               # comma-separated
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _check_required(self):
        if self.log_level.upper() != "DEBUG":
            if not self.entra_tenant_id:
                raise ValueError("ENTRA_TENANT_ID is required in non-debug mode")
            if not self.entra_api_audience:
                raise ValueError("ENTRA_API_AUDIENCE is required in non-debug mode")
        return self

    @cached_property
    def tenant_allowlist_ids(self) -> List[str]:
        return _csv(self.tenant_allowlist)

    @cached_property
    def cors_origins(self) -> List[str]:
        return _csv(self.cors_allowed_origins)

    @cached_property
    def email_domains(self) -> List[str]:
        return [d.lower() for d in _csv(self.allowed_email_domains)]

    @cached_property
    def extra_countries(self) -> List[str]:
        return [c.upper() for c in _csv(self.extra_country_codes)]

    @cached_property
    def system_roles(self) -> List[str]:
        return _csv(self.known_system_roles)

    @property
    def entra_issuer(self) -> str:
        return f"https://login.microsoftonline.com/{self.entra_tenant_id}/v2.0"

    @property
    def entra_jwks_uri(self) -> str:
        return f"https://login.microsoftonline.com/{self.entra_tenant_id}/discovery/v2.0/keys"


@lru_cache
def get_settings() -> Settings:
    return Settings()
