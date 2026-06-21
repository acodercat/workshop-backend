"""One-time (idempotent) OSS bucket setup for orphan-upload reaping (M5).

Installs a lifecycle rule on the session-file bucket that deletes objects
still tagged ``lifecycle=orphan`` after ``--days`` (default 1). A presigned
upload is born with that tag (see ``file_storage.generate_upload_url`` when
``oss_orphan_tagging_enabled`` is on); binding the file to a session clears
the tag (``file_storage.clear_orphan_tag``), so only never-claimed uploads
match the rule and get reaped.

Run after enabling ``oss_orphan_tagging_enabled`` (and after the frontend
deploy that echoes the ``x-oss-tagging`` header). Also requires the bucket
CORS config to allow the ``x-oss-tagging`` request header on PUT — that's a
separate console/Terraform change this script does NOT touch.

Idempotent: it reads the bucket's current lifecycle rules, replaces only
the rule with our id, and writes them back, so existing rules are kept.

Usage:
    uv run python -m scripts.setup_oss_lifecycle              # days=1
    uv run python -m scripts.setup_oss_lifecycle --days 2
    uv run python -m scripts.setup_oss_lifecycle --dry-run
"""

import argparse

import oss2
from oss2.models import (
    BucketLifecycle,
    LifecycleExpiration,
    LifecycleRule,
    Tagging,
    TaggingRule,
)

from app.services.file_storage import (
    SESSION_FILE_POLICY,
    _ORPHAN_TAG_KEY,
    _ORPHAN_TAG_VALUE,
    get_bucket,
)

_RULE_ID = "reap-orphan-session-files"
_DEFAULT_DAYS = 1


def _orphan_rule(days: int) -> LifecycleRule:
    tagging_rule = TaggingRule()
    tagging_rule.add(_ORPHAN_TAG_KEY, _ORPHAN_TAG_VALUE)
    return LifecycleRule(
        id=_RULE_ID,
        # Objects live under ``session-files/{uuid}/{filename}``; the tag is
        # the real filter, the prefix just narrows the rule's scope.
        prefix=f"{SESSION_FILE_POLICY.key_prefix}/",
        status=LifecycleRule.ENABLED,
        expiration=LifecycleExpiration(days=days),
        tagging=Tagging(tagging_rule),
    )


def _existing_rules(bucket: oss2.Bucket) -> list[LifecycleRule]:
    try:
        return list(bucket.get_bucket_lifecycle().rules)
    except oss2.exceptions.NoSuchLifecycle:
        return []


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days", type=int, default=_DEFAULT_DAYS,
        help=f"Days a tagged-orphan object survives before deletion (default {_DEFAULT_DAYS}).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the resulting rule set without writing it.",
    )
    args = parser.parse_args()

    bucket = get_bucket(SESSION_FILE_POLICY.bucket_name)
    # Keep every rule except a prior copy of ours, then append the fresh one.
    kept = [r for r in _existing_rules(bucket) if r.id != _RULE_ID]
    rules = kept + [_orphan_rule(args.days)]

    print(
        f"bucket={SESSION_FILE_POLICY.bucket_name} rule={_RULE_ID} "
        f"prefix={SESSION_FILE_POLICY.key_prefix}/ "
        f"tag={_ORPHAN_TAG_KEY}={_ORPHAN_TAG_VALUE} expire_days={args.days} "
        f"(kept {len(kept)} other rule(s))"
    )
    if args.dry_run:
        print("dry-run: not written")
        return

    bucket.put_bucket_lifecycle(BucketLifecycle(rules))
    print("lifecycle rule installed")


if __name__ == "__main__":
    main()
