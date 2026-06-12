from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from app.common.filenames import InvalidFilenameError, validate_input_filename
from app.schemas.session import FileRef, MessageCreate
from app.services import file_storage
from config import settings


@pytest.mark.parametrize(
    "filename",
    [
        None,
        "",
        ".",
        "..",
        "../evil.pdf",
        "nested/evil.pdf",
        "/tmp/evil.pdf",
        "nested\\evil.pdf",
        "evil\0.pdf",
    ],
)
def test_validate_input_filename_rejects_path_like_names(filename):
    with pytest.raises(InvalidFilenameError, match="Invalid filename"):
        validate_input_filename(filename)


def test_validate_input_filename_accepts_basename():
    assert validate_input_filename("report.final.pdf") == "report.final.pdf"


@pytest.mark.parametrize(
    "filename",
    ["../evil.pdf", "nested/evil.pdf", "/tmp/evil.pdf", "nested\\evil.pdf"],
)
def test_file_ref_rejects_unsafe_filename(filename):
    with pytest.raises(ValidationError):
        FileRef(file_key="documents/key", filename=filename)


def test_generate_upload_url_rejects_unsafe_filename_before_bucket_access():
    with patch("app.services.file_storage.get_bucket") as get_bucket:
        with pytest.raises(InvalidFilenameError):
            file_storage.generate_upload_url("../evil.pdf", 1, file_storage.SESSION_FILE_POLICY)

    get_bucket.assert_not_called()


def test_generate_upload_url_uses_sanitized_basename_in_key():
    bucket = MagicMock()
    bucket.sign_url.return_value = "https://oss/upload"

    with patch("app.services.file_storage.get_bucket", return_value=bucket):
        response = file_storage.generate_upload_url("report.pdf", 1, file_storage.SESSION_FILE_POLICY)

    assert response.file_key.startswith("session-files/")
    assert response.file_key.endswith("/report.pdf")
    bucket.sign_url.assert_called_once()


def test_generate_upload_url_omits_tagging_when_feature_off(monkeypatch):
    monkeypatch.setattr(file_storage.settings, "oss_orphan_tagging_enabled", False)
    bucket = MagicMock()
    bucket.sign_url.return_value = "https://oss/upload"

    with patch("app.services.file_storage.get_bucket", return_value=bucket):
        response = file_storage.generate_upload_url("report.pdf", 1, file_storage.SESSION_FILE_POLICY)

    assert response.tagging_header is None
    # The signed headers must NOT include x-oss-tagging (else the client,
    # which sends nothing, would fail the signature).
    signed_headers = bucket.sign_url.call_args.kwargs["headers"]
    assert "x-oss-tagging" not in signed_headers


def test_generate_upload_url_signs_tagging_when_feature_on(monkeypatch):
    monkeypatch.setattr(file_storage.settings, "oss_orphan_tagging_enabled", True)
    bucket = MagicMock()
    bucket.sign_url.return_value = "https://oss/upload"

    with patch("app.services.file_storage.get_bucket", return_value=bucket):
        response = file_storage.generate_upload_url("report.pdf", 1, file_storage.SESSION_FILE_POLICY)

    # Response value and signed header must match exactly (signature contract).
    assert response.tagging_header == file_storage.ORPHAN_TAGGING_HEADER
    signed_headers = bucket.sign_url.call_args.kwargs["headers"]
    assert signed_headers["x-oss-tagging"] == file_storage.ORPHAN_TAGGING_HEADER


def test_clear_orphan_tag_deletes_object_tagging():
    bucket = MagicMock()
    with patch("app.services.file_storage.get_bucket", return_value=bucket):
        file_storage.clear_orphan_tag(file_storage.SESSION_FILE_POLICY, "session-files/x/y.pdf")
    bucket.delete_object_tagging.assert_called_once_with("session-files/x/y.pdf")


def test_file_ref_rejects_declared_size_above_cap():
    with pytest.raises(ValidationError):
        FileRef(
            file_key="documents/key",
            filename="report.pdf",
            size_bytes=settings.max_file_bytes + 1,
        )


def test_message_create_rejects_more_than_max_files():
    files = [
        FileRef(
            file_key=f"documents/key-{index}",
            filename="report.pdf",
            size_bytes=1,
        )
        for index in range(settings.max_message_files + 1)
    ]

    with pytest.raises(ValidationError):
        MessageCreate(content="hello", files=files)


def test_message_create_requires_declared_file_sizes():
    with pytest.raises(ValidationError, match="Each file must include size_bytes"):
        MessageCreate(
            content="hello",
            files=[FileRef(file_key="documents/key", filename="report.pdf")],
        )


def test_message_create_skills_defaults_to_empty_list():
    """Omitting ``skills`` is the common case (no chips in the composer);
    it must not raise and must produce ``[]`` so the worker's reminder
    gate stays off."""
    assert MessageCreate(content="hello").skills == []


def test_message_create_accepts_skills_list():
    body = MessageCreate(content="hi", skills=["deploy", "pdf-processing"])
    assert body.skills == ["deploy", "pdf-processing"]


def test_message_create_rejects_too_many_skills():
    with pytest.raises(ValidationError):
        MessageCreate(content="hi", skills=[f"skill-{i}" for i in range(21)])
