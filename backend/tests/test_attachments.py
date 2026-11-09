"""Tests for app.attachments (S3 presigned upload/download, file safety)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app import attachments
from app.attachments import presign_download, presign_upload, safe_filename, verify_upload
from app.auth import User


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_s3_client():
    """Reset the module-level S3 client cache before each test."""
    attachments._s3 = None
    yield
    attachments._s3 = None


@pytest.fixture()
def mock_s3():
    """Provide a mocked S3 client and inject it into the attachments module."""
    client = MagicMock()
    client.generate_presigned_post.return_value = {
        "url": "https://s3.example.com",
        "fields": {"key": "uploads/user-oid/abc123/test.pdf"},
    }
    client.generate_presigned_url.return_value = "https://s3.example.com/download"
    client.head_object.return_value = {
        "ContentLength": 1024,
        "ContentType": "application/pdf",
    }
    attachments._s3 = client
    return client


@pytest.fixture()
def user():
    return User(oid="user-oid", upn="user@lilly.com", name="Test User")


@pytest.fixture()
def other_user():
    return User(oid="other-oid", upn="other@lilly.com", name="Other User")


# ---------------------------------------------------------------------------
# safe_filename()
# ---------------------------------------------------------------------------
class TestSafeFilename:

    def test_strips_path_traversal(self):
        assert ".." not in safe_filename("../../etc/passwd")
        result = safe_filename("../../etc/passwd")
        assert "/" not in result
        assert "\\" not in result

    def test_strips_unix_path(self):
        assert safe_filename("/home/user/docs/file.pdf") == "file.pdf"

    def test_strips_windows_path(self):
        assert safe_filename("C:\\Users\\name\\file.pdf") == "file.pdf"

    def test_replaces_dangerous_characters(self):
        result = safe_filename("file<>:\"|?*.pdf")
        assert "<" not in result
        assert ">" not in result
        assert "|" not in result
        assert "?" not in result

    def test_truncates_to_120_chars(self):
        long_name = "a" * 200 + ".pdf"
        result = safe_filename(long_name)
        assert len(result) <= 120

    def test_preserves_safe_characters(self):
        assert safe_filename("report 2024.pdf") == "report 2024.pdf"

    def test_falls_back_to_attachment_for_empty(self):
        assert safe_filename("...") == "attachment"
        assert safe_filename("") == "attachment"

    def test_handles_dotfile(self):
        # Leading dots get stripped, so fallback is expected
        result = safe_filename(".hidden")
        assert result  # not empty


# ---------------------------------------------------------------------------
# presign_upload()
# ---------------------------------------------------------------------------
class TestPresignUpload:

    def test_generates_correct_key_pattern(self, settings, mock_s3, user):
        settings.attachments_bucket = "test-bucket"
        result = presign_upload(settings, user, "approval.pdf", 1024)
        assert result["key"].startswith(f"uploads/{user.oid}/")
        assert result["key"].endswith("/approval.pdf")
        assert result["url"] == "https://s3.example.com"

    def test_includes_presign_fields(self, settings, mock_s3, user):
        settings.attachments_bucket = "test-bucket"
        result = presign_upload(settings, user, "test.pdf", 1024)
        assert "fields" in result
        assert "url" in result
        assert "key" in result

    def test_calls_generate_presigned_post(self, settings, mock_s3, user):
        settings.attachments_bucket = "test-bucket"
        presign_upload(settings, user, "test.pdf", 1024)
        mock_s3.generate_presigned_post.assert_called_once()
        call_kwargs = mock_s3.generate_presigned_post.call_args[1]
        assert call_kwargs["Bucket"] == "test-bucket"

    def test_rejects_unsupported_extension(self, settings, mock_s3, user):
        settings.attachments_bucket = "test-bucket"
        with pytest.raises(ValueError, match="email"):
            presign_upload(settings, user, "malware.exe", 1024)

    def test_rejects_oversized_file(self, settings, mock_s3, user):
        settings.attachments_bucket = "test-bucket"
        with pytest.raises(ValueError, match="too large"):
            presign_upload(settings, user, "big.pdf", settings.attachment_max_bytes + 1)

    def test_raises_when_bucket_not_configured(self, settings, mock_s3, user):
        settings.attachments_bucket = None
        with pytest.raises(ValueError, match="Attachments"):
            presign_upload(settings, user, "test.pdf", 1024)


# ---------------------------------------------------------------------------
# verify_upload()
# ---------------------------------------------------------------------------
class TestVerifyUpload:

    def test_accepts_valid_key(self, settings, mock_s3, user):
        key = f"uploads/{user.oid}/abc123/approval.pdf"
        result = verify_upload(settings, user, key)
        assert result["key"] == key
        assert result["filename"] == "approval.pdf"
        assert result["size"] == 1024
        assert result["content_type"] == "application/pdf"

    def test_rejects_wrong_user(self, settings, mock_s3, user, other_user):
        key = f"uploads/{other_user.oid}/abc123/approval.pdf"
        with pytest.raises(ValueError, match="uploaded by you"):
            verify_upload(settings, user, key)

    def test_rejects_path_traversal_in_key(self, settings, mock_s3, user):
        key = "uploads/../../etc/passwd"
        with pytest.raises(ValueError):
            verify_upload(settings, user, key)

    def test_raises_when_object_missing(self, settings, mock_s3, user):
        from botocore.exceptions import ClientError
        mock_s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
        )
        key = f"uploads/{user.oid}/abc123/missing.pdf"
        with pytest.raises(ValueError, match="finish uploading"):
            verify_upload(settings, user, key)


# ---------------------------------------------------------------------------
# presign_download()
# ---------------------------------------------------------------------------
class TestPresignDownload:

    def test_generates_download_url(self, settings, mock_s3):
        url = presign_download(settings, "uploads/user-oid/abc/file.pdf", "file.pdf")
        assert url == "https://s3.example.com/download"

    def test_calls_generate_presigned_url_with_disposition(self, settings, mock_s3):
        presign_download(settings, "uploads/user-oid/abc/file.pdf", "file.pdf")
        mock_s3.generate_presigned_url.assert_called_once()
        call_args = mock_s3.generate_presigned_url.call_args
        params = call_args[1]["Params"] if "Params" in call_args[1] else call_args[0][1]
        assert "ResponseContentDisposition" in params
        assert "attachment" in params["ResponseContentDisposition"]
        assert "file.pdf" in params["ResponseContentDisposition"]
