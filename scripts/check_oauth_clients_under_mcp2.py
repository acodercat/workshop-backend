"""Would every stored OAuth client registration still validate under mcp 2.x?

**Read-only. Opens no transaction, writes nothing, changes nothing.** Run it
before deploying the SDK bump, once per database.

The one field that moved: ``OAuthClientInformationFull.client_id`` is optional in
mcp 1.x and **required** in 2.x. ``grant.stored_authorization`` validates
``mcp_server.oauth_client_encrypted`` through that model on every token
rotation, so a row written by 1.x holding ``"client_id": null`` raises
``ValidationError`` the first time 2.x reads it — and that Connector can never
rotate its grant again. Reachable only where a dynamic-registration server
answered without a ``client_id``, which 1.x accepted silently.

Everything else was checked and is not a hazard: ``OAuthToken`` is
field-identical across the two majors, and ``OAuthMetadata`` only gains optional
fields (both directions ignore extras, so a rollback reads 2.x rows fine).

Run it under the **1.x** environment — it does not import the SDK. The check is
the one rule 2.x adds, spelled out here rather than imported, so this script
gives the same answer whichever major happens to be installed.

    # from backend/ :
    uv run python scripts/check_oauth_clients_under_mcp2.py

Exit status is 0 when every row would validate, 1 when any would not — so it can
gate a deploy. A row that will not *decrypt* is reported separately and does not
fail the check: that is a rotated Fernet key, which this migration neither
causes nor fixes.
"""

from __future__ import annotations

import json
import os
import sys

# Make ``app`` / ``config`` importable when run as a plain file path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text  # noqa: E402

from app.services.crypto import decrypt_secret  # noqa: E402
from config import settings  # noqa: E402


def _rows() -> list[tuple[int, str, str]]:
    """Every Connector holding a client registration, id / name / ciphertext."""
    engine = create_engine(settings.db_uri.replace("+asyncpg", ""))
    try:
        with engine.connect() as conn:
            return [
                (row[0], row[1], row[2])
                for row in conn.execute(
                    text(
                        "SELECT id, name, oauth_client_encrypted FROM mcp_server "
                        "WHERE oauth_client_encrypted IS NOT NULL ORDER BY id"
                    )
                ).all()
            ]
    finally:
        engine.dispose()


def main() -> int:
    rows = _rows()
    print(f"Connectors holding an OAuth client registration: {len(rows)}")
    if not rows:
        print("Nothing to check — the SDK bump cannot break what is not there.")
        return 0

    unreadable: list[str] = []
    failing: list[str] = []
    for connector_id, name, ciphertext in rows:
        try:
            client = json.loads(decrypt_secret(ciphertext))
        except Exception as exc:
            unreadable.append(f"  id={connector_id} {name!r}: {type(exc).__name__}")
            continue
        # The whole rule 2.x adds, and ``not`` rather than a key check because
        # all three falsy spellings were tried against the 2.x model and all
        # three are refused: absent, explicitly null, and empty string.
        if not client.get("client_id"):
            failing.append(
                f"  id={connector_id} {name!r}: "
                f"client_id is {client.get('client_id')!r}"
            )

    if unreadable:
        print(
            f"\nCould not decrypt {len(unreadable)} row(s) — "
            f"a rotated key, unrelated to this:"
        )
        print("\n".join(unreadable))

    if failing:
        print(f"\nWOULD FAIL under mcp 2.x — {len(failing)} row(s):")
        print("\n".join(failing))
        print(
            "\nEach of these loses grant rotation the moment 2.x reads it. "
            "Re-authorize "
            "the Connector (which registers a client afresh) before deploying, or hold "
            "the deploy for this database."
        )
        return 1

    print(
        "\nEvery readable row carries a client_id. "
        "The bump is safe for this database."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
