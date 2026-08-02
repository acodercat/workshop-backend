"""Manage the official Connector catalog (``mcp_server_catalog``).

The catalog is operator data, not code — this is how entries get in and out
until the admin console grows a CRUD for it. Publishing state is a flag rather
than a delete so Connectors users already added keep their ``catalog_id``.

Usage::

    # add an OAuth entry (most hosted MCP servers)
    uv run python -m scripts.manage_connector_catalog add \\
        --key notion --name Notion --url https://mcp.notion.com/mcp \\
        --auth-type OAUTH --description "Search and edit your Notion pages"

    uv run python -m scripts.manage_connector_catalog list
    uv run python -m scripts.manage_connector_catalog unpublish --key notion
    uv run python -m scripts.manage_connector_catalog publish --key notion
"""

import argparse
import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models.mcp_server import MCPServerAuthType
from app.models.mcp_server_catalog import MCPServerCatalogEntry
from app.services.mcp.catalog import INSTANTIABLE_AUTH_TYPES


async def _add(db: AsyncSession, args: argparse.Namespace) -> int:
    existing = await _find(db, args.key)
    if existing is not None:
        print(f"key {args.key!r} already exists (id={existing.id})", file=sys.stderr)
        return 1

    entry = MCPServerCatalogEntry(
        key=args.key,
        name=args.name,
        url=args.url,
        auth_type=MCPServerAuthType(args.auth_type),
        oauth_scope=args.oauth_scope,
        description=args.description,
        icon_url=args.icon_url,
        sort_order=args.sort_order,
    )
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    print(f"added {entry.key!r} (id={entry.id})")
    return 0


async def _set_published(db: AsyncSession, key: str, published: bool) -> int:
    entry = await _find(db, key)
    if entry is None:
        print(f"no catalog entry with key {key!r}", file=sys.stderr)
        return 1
    entry.published = published
    await db.commit()
    print(f"{key!r} is now {'published' if published else 'unpublished'}")
    return 0


async def _list(db: AsyncSession) -> int:
    stmt = select(MCPServerCatalogEntry).order_by(
        MCPServerCatalogEntry.sort_order, MCPServerCatalogEntry.id,
    )
    entries = list((await db.execute(stmt)).scalars())
    if not entries:
        print("(catalog is empty)")
        return 0
    for entry in entries:
        state = "published" if entry.published else "unpublished"
        print(
            f"{entry.id:>4}  {entry.key:<20} {entry.auth_type.value:<7} "
            f"{state:<12} {entry.url}"
        )
    return 0


async def _find(db: AsyncSession, key: str) -> MCPServerCatalogEntry | None:
    stmt = select(MCPServerCatalogEntry).where(MCPServerCatalogEntry.key == key)
    return (await db.execute(stmt)).scalar_one_or_none()


async def main(args: argparse.Namespace) -> int:
    async with AsyncSessionLocal() as db:
        if args.command == "add":
            return await _add(db, args)
        if args.command == "list":
            return await _list(db)
        if args.command == "publish":
            return await _set_published(db, args.key, True)
        if args.command == "unpublish":
            return await _set_published(db, args.key, False)
    return 1  # pragma: no cover — argparse rejects unknown commands first


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="add a catalog entry")
    add.add_argument("--key", required=True, help="stable slug, e.g. notion")
    add.add_argument("--name", required=True, help="display name, copied to the connector")
    add.add_argument("--url", required=True, help="remote MCP server URL (https)")
    # Only the two the add flow can complete: BEARER/HEADER would need a secret
    # per user that the catalog cannot hold (see catalog.INSTANTIABLE_AUTH_TYPES).
    add.add_argument(
        "--auth-type", default=MCPServerAuthType.NONE.value,
        choices=sorted(m.value for m in INSTANTIABLE_AUTH_TYPES),
    )
    add.add_argument("--oauth-scope", default=None)
    add.add_argument("--description", default=None)
    add.add_argument("--icon-url", default=None)
    add.add_argument("--sort-order", type=int, default=0)

    sub.add_parser("list", help="list every entry, published or not")

    for name, help_text in (
        ("publish", "list an entry in the catalog"),
        ("unpublish", "hide an entry without deleting it"),
    ):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("--key", required=True)

    return parser.parse_args()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(_parse_args())))
