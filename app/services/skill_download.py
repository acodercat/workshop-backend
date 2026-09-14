"""Presigning a skill bundle for download — one definition, two callers.

The console and the customer API both hand out a URL to the same object,
and what a downloaded bundle is *opened* under — its filename and the
storage policy it was signed against — is a contract that must not
differ by which surface answered. Written out twice it would be
byte-identical twice, until one of them stopped being.

The surfaces differ in **who may ask**, not in what is signed:

- ``api/admin/skill.py`` — platform bundles only; a ``CUSTOM`` upload is
  a user's own file and answers 403.
- ``api/public/skill.py`` — OFFICIAL plus the caller's own CUSTOM.

Its own module rather than a helper beside one of them: the console has
never imported from ``api/public`` and neither has imported from the
other, and this is not the seam to open that on.
"""

import os

from app.models.skill import Skill
from app.schemas.file import DownloadUrlResponse
from app.services import file_storage
from app.services.file_storage import SKILL_POLICY


def skill_download_url(skill: Skill) -> DownloadUrlResponse:
    """A short-lived presigned GET for ``skill``'s bundle.

    ``skill.name`` is the kebab-case identifier — file-system friendly
    and stable. The original upload's filename (and the UUID-suffixed
    OSS key) are meaningless to the user; reuse only its extension, and
    fall back to ``.zip`` for a key that carries none.

    The TTL is ``file_storage``'s, not this module's: how long a signed
    URL lives is a property of the storage layer's signing, and a second
    number here would be a second thing to keep in step.
    """
    extension = os.path.splitext(skill.bundle_key)[1] or ".zip"
    url, expires_in = file_storage.generate_download_url(
        SKILL_POLICY,
        skill.bundle_key,
        f"{skill.name}{extension}",
    )
    return DownloadUrlResponse(download_url=url, file_key=skill.bundle_key, expires_in=expires_in)
