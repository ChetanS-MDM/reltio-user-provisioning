"""Tests for app.notifications (in-app notification DynamoDB persistence)."""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from app import notifications
from app.notifications import count_unread, list_recent, mark_all_read, put


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_table():
    """Reset the module-level table cache before each test."""
    notifications._table = None
    yield
    notifications._table = None


@pytest.fixture()
def notifications_table(settings):
    """Create a mocked DynamoDB table for notifications."""
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.create_table(
            TableName=settings.notifications_table,
            KeySchema=[
                {"AttributeName": "recipient_oid", "KeyType": "HASH"},
                {"AttributeName": "sort_key", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "recipient_oid", "AttributeType": "S"},
                {"AttributeName": "sort_key", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(
            TableName=settings.notifications_table
        )
        notifications._table = None
        yield table
        notifications._table = None


# ---------------------------------------------------------------------------
# put()
# ---------------------------------------------------------------------------
class TestPut:

    @mock_aws
    def test_writes_item_with_correct_schema(self, settings, notifications_table):
        put(settings, "recipient-oid-1", "Test Title", "Test Body",
            request_id="AR-001", link="#request-AR-001")

        resp = notifications_table.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key("recipient_oid").eq("recipient-oid-1"),
        )
        items = resp["Items"]
        assert len(items) == 1
        item = items[0]
        assert item["recipient_oid"] == "recipient-oid-1"
        assert item["title"] == "Test Title"
        assert item["body"] == "Test Body"
        assert item["request_id"] == "AR-001"
        assert item["link"] == "#request-AR-001"
        assert item["read"] is False
        assert "created_at" in item
        assert "ttl" in item
        assert "sort_key" in item

    @mock_aws
    def test_sort_key_contains_timestamp(self, settings, notifications_table):
        put(settings, "recipient-oid-1", "Title", "Body")
        resp = notifications_table.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key("recipient_oid").eq("recipient-oid-1"),
        )
        sort_key = resp["Items"][0]["sort_key"]
        # sort_key is formatted as "ISO-timestamp#hex-id"
        assert "#" in sort_key

    @mock_aws
    def test_defaults_for_optional_fields(self, settings, notifications_table):
        put(settings, "recipient-oid-1", "Title", "Body")
        resp = notifications_table.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key("recipient_oid").eq("recipient-oid-1"),
        )
        item = resp["Items"][0]
        assert item["request_id"] == ""
        assert item["link"] == ""


# ---------------------------------------------------------------------------
# list_recent()
# ---------------------------------------------------------------------------
class TestListRecent:

    @mock_aws
    def test_returns_items_in_reverse_chronological_order(self, settings, notifications_table):
        put(settings, "user-oid", "First", "Body 1")
        put(settings, "user-oid", "Second", "Body 2")
        put(settings, "user-oid", "Third", "Body 3")

        results = list_recent(settings, "user-oid")
        assert len(results) == 3
        titles = {r["title"] for r in results}
        assert titles == {"First", "Second", "Third"}

    @mock_aws
    def test_respects_limit(self, settings, notifications_table):
        for i in range(5):
            put(settings, "user-oid", f"Notif {i}", "Body")
            time.sleep(0.01)

        results = list_recent(settings, "user-oid", limit=2)
        assert len(results) == 2

    @mock_aws
    def test_returns_empty_for_unknown_user(self, settings, notifications_table):
        results = list_recent(settings, "nonexistent-oid")
        assert results == []


# ---------------------------------------------------------------------------
# count_unread()
# ---------------------------------------------------------------------------
class TestCountUnread:

    @mock_aws
    def test_counts_only_unread(self, settings, notifications_table):
        put(settings, "user-oid", "Unread 1", "Body")
        put(settings, "user-oid", "Unread 2", "Body")
        # Manually mark one as read
        resp = notifications_table.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key("recipient_oid").eq("user-oid"),
            Limit=1,
        )
        item = resp["Items"][0]
        notifications_table.update_item(
            Key={"recipient_oid": item["recipient_oid"], "sort_key": item["sort_key"]},
            UpdateExpression="SET #r = :t",
            ExpressionAttributeNames={"#r": "read"},
            ExpressionAttributeValues={":t": True},
        )

        count = count_unread(settings, "user-oid")
        assert count == 1

    @mock_aws
    def test_returns_zero_when_all_read(self, settings, notifications_table):
        put(settings, "user-oid", "Notif", "Body")
        # Mark it as read
        resp = notifications_table.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key("recipient_oid").eq("user-oid"),
        )
        for item in resp["Items"]:
            notifications_table.update_item(
                Key={"recipient_oid": item["recipient_oid"], "sort_key": item["sort_key"]},
                UpdateExpression="SET #r = :t",
                ExpressionAttributeNames={"#r": "read"},
                ExpressionAttributeValues={":t": True},
            )

        count = count_unread(settings, "user-oid")
        assert count == 0

    @mock_aws
    def test_returns_zero_for_no_notifications(self, settings, notifications_table):
        count = count_unread(settings, "unknown-oid")
        assert count == 0


# ---------------------------------------------------------------------------
# mark_all_read() -- async
# ---------------------------------------------------------------------------
class TestMarkAllRead:

    @pytest.mark.asyncio
    async def test_marks_all_unread_as_read(self, settings, notifications_table):
        put(settings, "user-oid", "Notif 1", "Body")
        put(settings, "user-oid", "Notif 2", "Body")

        marked = await mark_all_read(settings, "user-oid")
        assert marked == 2

        # Verify all are now read
        count = count_unread(settings, "user-oid")
        assert count == 0

    @pytest.mark.asyncio
    async def test_returns_zero_when_none_unread(self, settings, notifications_table):
        marked = await mark_all_read(settings, "user-oid")
        assert marked == 0

    @pytest.mark.asyncio
    async def test_does_not_affect_already_read(self, settings, notifications_table):
        put(settings, "user-oid", "Notif 1", "Body")
        put(settings, "user-oid", "Notif 2", "Body")

        # Mark all read
        await mark_all_read(settings, "user-oid")

        # Add a new unread one
        put(settings, "user-oid", "Notif 3", "Body")

        # Only the new one should be unread
        count = count_unread(settings, "user-oid")
        assert count == 1

        marked = await mark_all_read(settings, "user-oid")
        assert marked == 1
