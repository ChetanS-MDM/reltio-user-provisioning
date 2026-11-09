"""Shared fixtures for the backend test suite."""
import os
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

# ---------------------------------------------------------------------------
# Environment variables -- set BEFORE importing any app module so that
# pydantic-settings (which reads os.environ at class-instantiation time) and
# boto3 both pick up safe test values.
# ---------------------------------------------------------------------------
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")

# Ensure the backend package is importable regardless of cwd.
_backend = Path(__file__).resolve().parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))

from app.auth import User  # noqa: E402
from app.config import Settings  # noqa: E402

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
_ROLES_MATRIX_PATH = str(Path(__file__).resolve().parent.parent / "roles_matrix.sample.json")

_APPROVERS = [
    {"name": "Alice Approver", "upn": "alice@lilly.com", "oid": "alice-oid"},
    {"name": "Bob Approver", "upn": "bob@lilly.com", "oid": "bob-oid"},
]


@pytest.fixture()
def settings():
    """A Settings object wired for tests (no real AWS, no real Reltio)."""
    return Settings(
        entra_tenant_id="test-tenant",
        entra_api_audience="test-audience",
        requests_table="TestRequests",
        attachments_bucket="test-bucket",
        approval_policy="all",
        approvers=_APPROVERS,
        roles_matrix_local_path=_ROLES_MATRIX_PATH,
        reltio_client_id="test-id",
        reltio_client_secret="test-secret",
        auth_base_url="https://mock-reltio",
        notify_sender=None,
    )


# ---------------------------------------------------------------------------
# Moto DynamoDB
# ---------------------------------------------------------------------------
@pytest.fixture()
def dynamodb_table(settings):
    """Create a mocked DynamoDB table that matches infra/template.yaml."""
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.create_table(
            TableName=settings.requests_table,
            KeySchema=[
                {"AttributeName": "request_id", "KeyType": "HASH"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "request_id", "AttributeType": "S"},
                {"AttributeName": "status", "AttributeType": "S"},
                {"AttributeName": "created_at", "AttributeType": "S"},
                {"AttributeName": "requester_oid", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "by_status",
                    "KeySchema": [
                        {"AttributeName": "status", "KeyType": "HASH"},
                        {"AttributeName": "created_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "by_requester",
                    "KeySchema": [
                        {"AttributeName": "requester_oid", "KeyType": "HASH"},
                        {"AttributeName": "created_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(TableName=settings.requests_table)

        # Reset the store module's cached table reference so it picks up moto.
        from app import store
        store._table = None

        yield table

        # Clean up the cached reference after the test.
        store._table = None


# ---------------------------------------------------------------------------
# Moto S3
# ---------------------------------------------------------------------------
@pytest.fixture()
def s3_bucket(settings):
    """Create a mocked S3 bucket."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=settings.attachments_bucket)
        yield settings.attachments_bucket


# ---------------------------------------------------------------------------
# User fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def fake_user():
    return User(oid="user-oid", upn="user@lilly.com", name="Test User")


@pytest.fixture()
def approver_user():
    return User(oid="alice-oid", upn="alice@lilly.com", name="Alice Approver")
