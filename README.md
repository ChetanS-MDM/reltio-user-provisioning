# Reltio access requests

A small internal app for requesting Reltio access by DoU, with two-person
approval before anything is granted.

1. **Request.** A person signs in with Lilly SSO and submits their Global ID
   (L-ID), email, country code, DoU, a business justification, the tenant(s)
   they need, and their manager's approval email as an attachment.
2. **Check.** The backend resolves the DoU against the EPH roles matrix, fills
   the country code into country-specific roles
   (`ROLE_LILLY_EPH_<country>_BASICSEARCH` → `ROLE_LILLY_EPH_US_BASICSEARCH`),
   and checks that every role exists in Reltio. That role set is frozen on the
   request, so approvers approve exactly what will be granted.
3. **Approve.** The configured approvers review the request and open the
   attachment in the app. They can uncheck individual roles they don't want to
   grant (with a confirmation step), or reject with a comment. By default both
   must approve; one rejection closes the request. Nobody can approve their own
   request.
4. **Grant.** When the last approval lands, the backend creates the Reltio
   user, or adds the approved roles to the existing user without removing
   anything, and records exactly what changed.

No route grants access without an approved request.

```
Browser (MSAL / Entra SSO)
   │  Entra token                      ┌─────────────────────────────┐
   ├──────────────► API Gateway ──────►│ Lambda (FastAPI)            │
   │                (JWT authorizer)   │  requests   → DynamoDB      │
   │  presigned POST                   │  matrix     ← S3 (JSON)     │
   └──────────────► S3 attachments ◄───│  Reltio cred ← Secrets Mgr  │
                    (private, KMS)     │  grant      → Reltio Auth   │
                                       └─────────────────────────────┘
```

## Layout

```
backend/app/
  main.py            routes (see below)
  auth.py            Entra token validation; who is an approver
  matrix.py          DoU parsing, country substitution, role validation
  provisioning.py    create user, or merge roles into the existing user
  reltio.py          Reltio Auth client (token, metadata, get/create/update user)
  store.py           DynamoDB access with conditional state changes
  attachments.py     presigned upload/download for manager-approval emails
  notify.py          SES emails + in-app notifications
  audit.py           one JSON audit line per action
  logging_config.py  structured logging with correlation IDs
backend/tests/       pytest test suite (moto for DynamoDB/S3)
infra/template.yaml  SAM: Lambda, HTTP API, DynamoDB table, attachments bucket, CloudWatch alarms
frontend/index.html  the app (MSAL sign-in; New request, My requests, Approvals)
frontend/demo_mock.html  standalone mock demo (no backend required, identity switcher)
scripts/             extract_matrix.py, publish_matrix.py (Excel → S3 JSON)
scripts/dev/         one-off test email scripts (not for production)
```

Routes: `GET /api/me`, `GET /api/metadata`, `GET /api/resolve-dou`,
`POST /api/attachments`, `POST /api/requests`, `GET /api/requests`,
`GET /api/approvals`, `GET /api/requests/{id}`, `GET /api/requests/{id}/attachment`,
`POST /api/requests/{id}/decision`, `/retry`, `/cancel`.

## Request lifecycle

`PENDING` → (all approvals) → `PROVISIONING` → `COMPLETED` or `FAILED`.
From `PENDING`, one rejection gives `REJECTED`, and the requester can cancel
(`CANCELLED`). Every transition is a conditional DynamoDB write, so two
approvers clicking at once can't double-record or grant twice. Provisioning
is idempotent, so a `FAILED` request can be retried safely by an approver.

Email notifications are sent at each state change (submitted, partial approval,
completed, rejected, failed, cancelled) when `NOTIFY_SENDER` is configured.

## Setup

**Entra.** Two app registrations:
- *API app*: expose a `user_impersonation` scope. Requesters need no app role
  unless you set `REQUESTER_APP_ROLE`.
- *SPA app*: SPA platform, redirect URI = where `index.html` is hosted,
  delegated permission to the API scope.

**Approvers.** Set `ApproversJson` with their names and UPNs. Adding each
person's Entra object ID (`oid`) is more robust, because it never changes:

```json
[{"name": "Alice Approver", "upn": "alice@lilly.com", "oid": "<object ID>"},
 {"name": "Bob Approver",   "upn": "bob@lilly.com",   "oid": "<object ID>"}]
```

`ApprovalPolicy=all` (default) needs both; `any` needs one.

**Reltio service account.** A client_credentials client stored in Secrets
Manager as `{"client_id": "...", "client_secret": "..."}`. Creating users needs
`ROLE_ADMIN_USER` (or `ROLE_ADMIN_CUSTOMER`).

**Tenants.** `TenantAllowlist` limits what can be requested. `TenantLabelsJson`
gives requesters readable names:
`{"b0e8OhkxuytO3n3": "DEV B0", "N6QdWQ05wkTZjLA": "PROD"}`.

**Roles matrix.** Publish the Excel matrix as JSON whenever it changes:

```bash
cd scripts && pip install -r requirements.txt
python publish_matrix.py EPH_Security_Roles_Matrix_v108.xlsx --bucket <bucket>
```

## Install, run, deploy

```bash
cd backend && pip install -r requirements.txt

# local run
export ENTRA_TENANT_ID=... ENTRA_API_AUDIENCE=...
export RELTIO_CLIENT_ID=... RELTIO_CLIENT_SECRET=...
export ROLES_MATRIX_LOCAL_PATH=roles_matrix.sample.json
export APPROVERS='[{"name":"Alice","upn":"alice@lilly.com","oid":"alice-oid"}]'
export REQUESTS_TABLE=... ATTACHMENTS_BUCKET=...
export TENANT_ALLOWLIST=b0e8OhkxuytO3n3 CORS_ALLOWED_ORIGINS=http://localhost:5500
uvicorn app.main:app --reload

# deploy
cd infra && sam build && sam deploy --guided
```

Put the `ApiBaseUrl` output and the two Entra client IDs into `CONFIG` at the
top of the script in `frontend/index.html`, then host the file (S3 + CloudFront,
or any static-file server).

## Testing

```bash
cd backend
pip install -r requirements-dev.txt
pytest tests/ -v
```

Tests use `moto` to mock DynamoDB and S3. No AWS credentials needed.

## Demo

Open `frontend/demo_mock.html` in a browser for a standalone demo that requires
no backend or credentials. It simulates the entire flow in-browser with an
identity switcher between "You (requester)" and an approver.

## Configuration

| Env var | Meaning |
|---|---|
| `APPROVERS` | JSON list of approvers (`name`, `upn`, optional `oid`) |
| `APPROVAL_POLICY` | `all` (default) or `any` |
| `REQUESTER_APP_ROLE` | App role needed to submit (empty = any Lilly user) |
| `GLOBAL_ID_PATTERN` | Regex for Global IDs (default `^L\d{6}$`) |
| `ALLOWED_EMAIL_DOMAINS` | Comma-separated (default `lilly.com`, subdomains allowed) |
| `EXTRA_COUNTRY_CODES` | Comma-separated codes beyond ISO two-letter and `GLOBAL` |
| `TENANT_ALLOWLIST` | Comma-separated tenant IDs that can be requested |
| `TENANT_LABELS` | JSON map of tenant ID to label |
| `REQUESTS_TABLE` / `ATTACHMENTS_BUCKET` | Storage (set by the SAM stack) |
| `ATTACHMENT_MAX_BYTES` | Upload limit (default 10 MB) |
| `ROLES_MATRIX_BUCKET` / `ROLES_MATRIX_KEY` | Where the matrix JSON lives |
| `RELTIO_SECRET_ARN` / `RELTIO_CUSTOMER_ID` | Reltio service credential / customer |
| `ENTRA_TENANT_ID` / `ENTRA_API_AUDIENCE` | Token validation |
| `NOTIFY_SENDER` / `APP_URL` | Optional SES emails and the link they contain |
| `CORS_ALLOWED_ORIGINS` | Comma-separated front-end origins |
| `ALARM_SNS_TOPIC_ARN` | SNS topic for CloudWatch alarm notifications |

## Monitoring

CloudWatch alarms are provisioned when `AlarmSnsTopicArn` is set:
- Lambda errors (any error triggers)
- Lambda duration approaching timeout (>25s of 60s limit)
- API Gateway 5xx rate (>5 in 5 minutes)

Structured logs include a correlation ID (`[<request-id>]`) for tracing a
single request across auth, store, reltio, provisioning, and notify modules.

## Security notes

- The browser never holds a Reltio credential and never chooses roles; the
  DoU decides, and the server re-resolves it on submit.
- The Lambda role can read one secret, read the matrix object, read/write
  `uploads/*` in the attachments bucket, use the requests table, and send
  SES email from the configured sender only. Nothing else.
- Attachments: private bucket, KMS encryption, TLS-only policy, versioning,
  retained on stack deletion. Upload keys are scoped to the uploader and
  checked before a file is attached to a request. Download links expire after
  five minutes, and opening one is audited.
- Requests carry the requester, every decision (who, when, comment), the
  frozen role set (optionally trimmed by the approver), the matrix version,
  and the Reltio result.
- Error messages returned to clients are sanitized to strip AWS ARNs and
  account IDs.
- The frontend uses a Content-Security-Policy and SRI hashes on external
  scripts.
- Old requests expire after 365 days via DynamoDB TTL. Attachments expire
  after 365 days via S3 lifecycle.

## Known limits

- **Matrix typos block DoUs.** `ROLE_ACTIVITY` (should be `ROLE_ACTIVITIES`)
  and `ROLE_USER_ML_MATCH` don't validate, so both IT Analyst use cases can't
  be requested until the matrix is fixed.
- **RDM roles.** Roles are granted on the tenants the requester picks. Existing
  accounts hold `ROLE_RDM_EDIT` on separate RDM tenants, so use cases with RDM
  roles need a tenant mapping before they're granted correctly.
- **Malware scanning.** Turn on GuardDuty Malware Protection for S3 on the
  attachments bucket before go-live.
- **MSAL** loads from Microsoft's CDN with SRI in `index.html`; vendor it
  through Artifactory for production to remove the CDN dependency entirely.
