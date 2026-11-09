"""Tests for app.auth (identity helpers and header parsing)."""
import pytest
from fastapi import HTTPException

from app.auth import User, approver_entry, approver_key, matches, require_user


# ---------------------------------------------------------------------------
# matches()
# ---------------------------------------------------------------------------
class TestMatches:

    def test_match_by_oid(self):
        entry = {"oid": "abc-123", "upn": "different@lilly.com"}
        user = User(oid="ABC-123", upn="user@lilly.com", name="U")
        assert matches(entry, user) is True

    def test_match_by_upn_when_no_oid(self):
        entry = {"upn": "user@lilly.com"}
        user = User(oid="irrelevant", upn="user@lilly.com", name="U")
        assert matches(entry, user) is True

    def test_no_match(self):
        entry = {"oid": "other-oid", "upn": "other@lilly.com"}
        user = User(oid="my-oid", upn="me@lilly.com", name="U")
        assert matches(entry, user) is False

    def test_oid_takes_precedence_over_upn(self):
        """When OID is present but doesn't match, UPN is NOT checked."""
        entry = {"oid": "wrong-oid", "upn": "user@lilly.com"}
        user = User(oid="my-oid", upn="user@lilly.com", name="U")
        assert matches(entry, user) is False


# ---------------------------------------------------------------------------
# approver_key()
# ---------------------------------------------------------------------------
class TestApproverKey:

    def test_uses_oid_when_present(self):
        assert approver_key({"oid": "ABC", "upn": "a@lilly.com"}) == "abc"

    def test_falls_back_to_upn(self):
        assert approver_key({"upn": "Bob@Lilly.com"}) == "bob@lilly.com"

    def test_empty_entry(self):
        assert approver_key({}) == ""


# ---------------------------------------------------------------------------
# approver_entry()
# ---------------------------------------------------------------------------
class TestApproverEntry:

    def test_finds_approver(self, settings, approver_user):
        entry = approver_entry(approver_user, settings)
        assert entry is not None
        assert entry["name"] == "Alice Approver"

    def test_returns_none_for_non_approver(self, settings, fake_user):
        assert approver_entry(fake_user, settings) is None


# ---------------------------------------------------------------------------
# require_user() -- header parsing
# ---------------------------------------------------------------------------
class TestRequireUser:

    @pytest.mark.asyncio
    async def test_missing_bearer_prefix(self, settings):
        with pytest.raises(HTTPException) as exc_info:
            await require_user(authorization="Token abc123", settings=settings)
        assert exc_info.value.status_code == 401
        assert "bearer" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_empty_authorization(self, settings):
        with pytest.raises(HTTPException) as exc_info:
            await require_user(authorization="", settings=settings)
        assert exc_info.value.status_code == 401
