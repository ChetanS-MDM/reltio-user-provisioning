"""Tests for app.models (Pydantic request bodies)."""
import pytest
from pydantic import ValidationError

from app.models import DecisionRequest, PresignRequest, SubmitRequest


# ---------------------------------------------------------------------------
# PresignRequest
# ---------------------------------------------------------------------------
class TestPresignRequest:

    def test_valid(self):
        req = PresignRequest(filename="approval.pdf", size=1024)
        assert req.filename == "approval.pdf"
        assert req.size == 1024

    def test_size_too_large(self):
        with pytest.raises(ValidationError, match="less than or equal to 50000000"):
            PresignRequest(filename="big.pdf", size=50_000_001)

    def test_size_zero(self):
        with pytest.raises(ValidationError, match="greater than 0"):
            PresignRequest(filename="empty.pdf", size=0)

    def test_empty_filename(self):
        with pytest.raises(ValidationError, match="at least 1 character"):
            PresignRequest(filename="", size=100)


# ---------------------------------------------------------------------------
# SubmitRequest
# ---------------------------------------------------------------------------
class TestSubmitRequest:

    def test_valid(self):
        req = SubmitRequest(
            global_id="L123456",
            email="user@lilly.com",
            country_code="US",
            dou="~2578 Enterprise Profile-DOU-Global EPH IT Analyst Read Only V18",
            justification="Need access for project work.",
            tenants=["tenant-a"],
            attachment_key="uploads/abc.pdf",
        )
        assert req.email == "user@lilly.com"
        assert req.tenants == ["tenant-a"]
        assert req.justification == "Need access for project work."

    def test_invalid_email_no_at(self):
        with pytest.raises(ValidationError, match="string_pattern_mismatch|pattern"):
            SubmitRequest(
                global_id="L123456",
                email="nope",
                country_code="US",
                dou="Some DoU",
                justification="Reason.",
                tenants=["t1"],
                attachment_key="uploads/a.pdf",
            )

    def test_empty_tenants(self):
        with pytest.raises(ValidationError, match="at least 1"):
            SubmitRequest(
                global_id="L123456",
                email="user@lilly.com",
                country_code="US",
                dou="Some DoU",
                justification="Reason.",
                tenants=[],
                attachment_key="uploads/a.pdf",
            )

    def test_whitespace_only_tenants(self):
        with pytest.raises(ValidationError, match="[Aa]t least one tenant"):
            SubmitRequest(
                global_id="L123456",
                email="user@lilly.com",
                country_code="US",
                dou="Some DoU",
                justification="Reason.",
                tenants=["  ", "\t"],
                attachment_key="uploads/a.pdf",
            )


# ---------------------------------------------------------------------------
# DecisionRequest
# ---------------------------------------------------------------------------
class TestDecisionRequest:

    def test_approve(self):
        req = DecisionRequest(decision="approve")
        assert req.decision == "approve"
        assert req.comment == ""
        assert req.approved_roles is None

    def test_reject(self):
        req = DecisionRequest(decision="reject", comment="Not justified.")
        assert req.decision == "reject"
        assert req.comment == "Not justified."

    def test_invalid_decision(self):
        with pytest.raises(ValidationError, match="literal_error|Input should be"):
            DecisionRequest(decision="maybe")

    def test_comment_stripping(self):
        req = DecisionRequest(decision="approve", comment="  ok  ")
        assert req.comment == "ok"

    def test_approved_roles_optional(self):
        req = DecisionRequest(decision="approve", approved_roles=["ROLE_A", "ROLE_B"])
        assert req.approved_roles == ["ROLE_A", "ROLE_B"]

        req2 = DecisionRequest(decision="approve")
        assert req2.approved_roles is None
