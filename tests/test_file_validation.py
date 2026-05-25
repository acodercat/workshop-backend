from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from app.common.file_limits import MAX_FILE_BYTES, MAX_MESSAGE_FILES
from app.common.filenames import InvalidFilenameError, validate_input_filename
from app.schemas.session import FileRef, MessageCreate
from app.services import file_storage


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
            file_storage.generate_upload_url("../evil.pdf", 1)

    get_bucket.assert_not_called()


def test_generate_upload_url_uses_sanitized_basename_in_key():
    bucket = MagicMock()
    bucket.sign_url.return_value = "https://oss/upload"

    with patch("app.services.file_storage.get_bucket", return_value=bucket):
        response = file_storage.generate_upload_url("report.pdf", 1)

    assert response.file_key.startswith("documents/")
    assert response.file_key.endswith("/report.pdf")
    bucket.sign_url.assert_called_once()


def test_file_ref_rejects_declared_size_above_cap():
    with pytest.raises(ValidationError):
        FileRef(
            file_key="documents/key",
            filename="report.pdf",
            size_bytes=MAX_FILE_BYTES + 1,
        )


def test_message_create_rejects_more_than_max_files():
    files = [
        FileRef(
            file_key=f"documents/key-{index}",
            filename="report.pdf",
            size_bytes=1,
        )
        for index in range(MAX_MESSAGE_FILES + 1)
    ]

    with pytest.raises(ValidationError):
        MessageCreate(content="hello", files=files)


def test_message_create_requires_declared_file_sizes():
    with pytest.raises(ValidationError, match="Each file must include size_bytes"):
        MessageCreate(
            content="hello",
            files=[FileRef(file_key="documents/key", filename="report.pdf")],
        )
