"""One-off: fill ``session_site.title`` from each site's ``<title>``.

Sites published without a ``title`` arg have a NULL title. This reads
each such site's ``{slug}/index.html`` straight from the OSS preview
bucket (SDK, not HTTP — avoids DNS / forced-download / serving quirks),
parses the HTML ``<title>``, and sets it.

Careful / idempotent:
- Only touches rows where ``title IS NULL`` (never clobbers a value).
- Skips a site whose object is gone (``NoSuchKey``) or whose page has no
  non-empty ``<title>`` — those stay NULL.
- Dry-run by default; pass ``--apply`` to write.

    uv run python -m scripts.archive.backfill_session_site_title           # dry-run
    uv run python -m scripts.archive.backfill_session_site_title --apply   # write
"""

import asyncio
import html
import re
import sys

import oss2
from sqlalchemy import text

from app.database import AsyncSessionLocal
from config import settings

_PREVIEW_BUCKET = "axon-preview"
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _preview_bucket() -> oss2.Bucket:
    auth = oss2.Auth(settings.oss_access_key_id, settings.oss_access_key_secret)
    return oss2.Bucket(auth, settings.oss_endpoint, _PREVIEW_BUCKET)


def _extract_title(raw: bytes) -> str | None:
    match = _TITLE_RE.search(raw.decode("utf-8", "replace"))
    if not match:
        return None
    title = re.sub(r"\s+", " ", html.unescape(match.group(1)).strip())
    return title[:255] or None


def _fetch_index(bucket: oss2.Bucket, slug: str) -> bytes | None:
    try:
        return bucket.get_object(f"{slug}/index.html").read()
    except oss2.exceptions.NoSuchKey:
        return None


async def main() -> None:
    apply = "--apply" in sys.argv
    bucket = _preview_bucket()
    async with AsyncSessionLocal() as db:
        slugs = (
            (
                await db.execute(
                    text("SELECT slug FROM session_site WHERE title IS NULL ORDER BY created_at")
                )
            )
            .scalars()
            .all()
        )
        print(f"{len(slugs)} rows with NULL title")

        updates: list[tuple[str, str]] = []
        no_index = no_title = 0
        for slug in slugs:
            raw = await asyncio.to_thread(_fetch_index, bucket, slug)
            if raw is None:
                no_index += 1
                print(f"  [no index.html] {slug}")
                continue
            title = _extract_title(raw)
            if not title:
                no_title += 1
                print(f"  [no <title>]    {slug}")
                continue
            updates.append((slug, title))
            print(f"  {slug} -> {title!r}")

        print(
            f"\nresolved {len(updates)} titles; "
            f"{no_index} missing index.html; {no_title} without a <title>"
        )
        if not apply:
            print("DRY RUN — re-run with --apply to write.")
            return
        for slug, title in updates:
            await db.execute(
                text("UPDATE session_site SET title = :t WHERE slug = :s AND title IS NULL"),
                {"t": title, "s": slug},
            )
        await db.commit()
        print(f"updated {len(updates)} rows")


if __name__ == "__main__":
    asyncio.run(main())
