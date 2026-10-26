"""Manage the official Connector catalog (``mcp_server_catalog``).

The catalog is operator data, not code — this is how entries get in and out
until the admin console grows a CRUD for it. Publishing state is a flag rather
than a delete so Connectors users already added keep their ``catalog_id``.

Translations are set at ``add`` time only. Editing one afterwards belongs to
the console that is being built for exactly this, and a second way to write
the same column is worth less than the one place it will live.

Usage::

    # add an OAuth entry (most hosted MCP servers)
    uv run python -m scripts.manage_connector_catalog add \\
        --name Notion --url https://mcp.notion.com/mcp \\
        --auth-type OAUTH --description "Search and edit your Notion pages"

    # the same entry offered in both languages the app ships
    uv run python -m scripts.manage_connector_catalog add \\
        --name 天眼查 --url https://mcp.tianyancha.com/mcp --auth-type OAUTH \\
        --name-i18n "en-US=Tianyancha" \\
        --description-i18n "en-US=Search Chinese company records"

    uv run python -m scripts.manage_connector_catalog list
    uv run python -m scripts.manage_connector_catalog unpublish \\
        --url https://mcp.notion.com/mcp
    uv run python -m scripts.manage_connector_catalog publish \\
        --url https://mcp.notion.com/mcp

    # attach a pre-registered OAuth app (providers without DCR/CIMD, e.g.
    # Figma). The secret is read from $CATALOG_OAUTH_CLIENT_SECRET or an
    # interactive prompt — never from argv, which lands in shell history.
    uv run python -m scripts.manage_connector_catalog set-oauth-client \
        --url https://mcp.figma.com/mcp --client-id vFtd...
    uv run python -m scripts.manage_connector_catalog set-oauth-client \
        --url https://mcp.figma.com/mcp --clear

    # file an entry under one of the connectors page's tabs, by the name
    # the console shows — a typo fails here, a mistyped id would not
    uv run python -m scripts.manage_connector_catalog set-category \
        --url https://mcp.notion.com/mcp --category 生产力
    uv run python -m scripts.manage_connector_catalog set-category \
        --url https://mcp.notion.com/mcp --clear
"""

import argparse
import asyncio
import getpass
import os
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models.connector_category import ConnectorCategory
from app.models.mcp_server import MCPServerAuthType
from app.models.mcp_server_catalog import MCPServerCatalogEntry
from app.services import localized_text
from app.services.connector.catalog import INSTANTIABLE_AUTH_TYPES
from app.services.connector.url_identity import normalize_connector_url
from app.services.crypto import encrypt_secret

_URL_HELP = "the entry's MCP server URL, exactly as `list` prints it"
_CATEGORY_HELP = "a connector category, by the name the console shows"


class _LocaleMap(argparse.Action):
    """Collect repeated ``--x-i18n LOCALE=TEXT`` into one dict.

    Repeated pairs rather than a JSON blob: two locales is what the app ships,
    and quoting JSON inside a shell is where the typos live. Unsupported
    locales are dropped later by ``localized_text.clean_map``, which is also
    what the API path uses — one rule for what counts as a translation.
    """

    def __call__(self, parser, namespace, value, option_string=None):
        locale, separator, text = value.partition("=")
        if not separator or not text.strip():
            parser.error(f"{option_string} wants LOCALE=TEXT, got {value!r}")
        collected = getattr(namespace, self.dest, None) or {}
        collected[locale] = text
        setattr(namespace, self.dest, collected)


async def _add(db: AsyncSession, args: argparse.Namespace) -> int:
    existing = await _find(db, args.url)
    if existing is not None:
        print(
            f"the catalog already offers {args.url} (id={existing.id})",
            file=sys.stderr,
        )
        return 1

    category_id = None
    if args.category is not None:
        category_id = await _resolve_category_id(db, args.category)
        if category_id is None:
            return 1

    entry = MCPServerCatalogEntry(
        name=args.name,
        name_i18n=localized_text.clean_map(args.name_i18n),
        description_i18n=localized_text.clean_map(args.description_i18n),
        url=args.url,
        auth_type=MCPServerAuthType(args.auth_type),
        oauth_scope=args.oauth_scope,
        description=args.description,
        icon_url=args.icon_url,
        sort_order=args.sort_order,
        category_id=category_id,
    )
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    print(f"added {entry.name!r} (id={entry.id})")
    return 0


async def _set_oauth_client(db: AsyncSession, args: argparse.Namespace) -> int:
    """Attach, rotate or clear an entry's pre-registered OAuth app.

    Rotation is the same call as attachment — the columns are overwritten, and
    only *new* adds see the change: every existing Connector snapshotted the
    old app into its own row at add time.
    """
    entry = await _find(db, args.url)
    if entry is None:
        print(f"no catalog entry for {args.url}", file=sys.stderr)
        return 1

    if args.clear:
        entry.oauth_client_id = None
        entry.oauth_client_secret_encrypted = None
        await db.commit()
        print(f"{entry.name!r}: pre-registered OAuth app cleared")
        return 0

    if entry.auth_type != MCPServerAuthType.OAUTH:
        print(
            f"{entry.name!r} is {entry.auth_type.value}, not OAUTH — a"
            f" pre-registered app would never be used",
            file=sys.stderr,
        )
        return 1

    # From the environment or a prompt, never argv: a secret on the command
    # line outlives the command in shell history. Empty means a public client
    # (an app the provider issued without a secret).
    secret = os.environ.get("CATALOG_OAUTH_CLIENT_SECRET")
    if secret is None:
        secret = getpass.getpass("client secret (empty for a public client): ")
    entry.oauth_client_id = args.client_id
    entry.oauth_client_secret_encrypted = encrypt_secret(secret) if secret else None
    await db.commit()
    print(
        f"{entry.name!r}: OAuth app set (client_id={args.client_id},"
        f" secret={'stored encrypted' if secret else 'none — public client'})"
    )
    return 0


async def _set_category(db: AsyncSession, args: argparse.Namespace) -> int:
    """File an entry under a category, or return it to uncategorised.

    Shaped like ``set-oauth-client``: one address (``--url``), and a
    ``--clear`` flag rather than an empty ``--category``, so removing a
    value is spelled as its own instruction.
    """
    entry = await _find(db, args.url)
    if entry is None:
        print(f"no catalog entry for {args.url}", file=sys.stderr)
        return 1

    category_id = None
    if not args.clear:
        category_id = await _resolve_category_id(db, args.category)
        if category_id is None:
            return 1

    entry.category_id = category_id
    await db.commit()
    print(f"{entry.name!r}: category {'cleared' if args.clear else args.category}")
    return 0


async def _set_published(db: AsyncSession, url: str, published: bool) -> int:
    entry = await _find(db, url)
    if entry is None:
        print(f"no catalog entry for {url}", file=sys.stderr)
        return 1
    entry.published = published
    await db.commit()
    print(f"{entry.name!r} is now {'published' if published else 'unpublished'}")
    return 0


async def _list(db: AsyncSession) -> int:
    stmt = select(MCPServerCatalogEntry).order_by(
        MCPServerCatalogEntry.sort_order,
        MCPServerCatalogEntry.id,
    )
    entries = list((await db.execute(stmt)).scalars())
    if not entries:
        print("(catalog is empty)")
        return 0
    for entry in entries:
        state = "published" if entry.published else "unpublished"
        app_marker = f"  app={entry.oauth_client_id}" if entry.oauth_client_id else ""
        print(
            f"{entry.id:>4}  {entry.name:<20}{'*' if entry.name_i18n else ' '} "
            f"{entry.auth_type.value:<7} "
            f"{state:<12} {entry.url}{app_marker}"
        )
    return 0


async def _resolve_category_id(db: AsyncSession, name: str) -> int | None:
    """The id of the category named *name*, or ``None`` after saying so.

    By name rather than by id, the same choice ``upload_skill.py`` makes:
    a name is what the operator reads off ``GET /connector-categories``,
    and a typo in one ends here with the known names beside it instead of
    filing the entry under a tab nobody meant. Categories themselves are
    created on the admin API (``POST /connector-categories``) — the
    console has the routes but not yet a screen for them. This script
    manages catalog entries, not the taxonomy.
    """
    category_id = await db.scalar(
        select(ConnectorCategory.id).where(ConnectorCategory.name == name)
    )
    if category_id is None:
        known = (
            await db.scalars(select(ConnectorCategory.name).order_by(ConnectorCategory.name))
        ).all()
        print(
            f"no connector category named {name!r}; "
            f"known: {', '.join(known) or '(none)'}\n"
            "create one first: POST /connector-categories on the admin API",
            file=sys.stderr,
        )
    return category_id


async def _find(db: AsyncSession, url: str) -> MCPServerCatalogEntry | None:
    """The entry offering *url*, which is what identifies one — the catalog
    holds at most a single row per server, and ``name`` is only its label."""
    stmt = select(MCPServerCatalogEntry).where(MCPServerCatalogEntry.url == url)
    return (await db.execute(stmt)).scalar_one_or_none()


async def main(args: argparse.Namespace) -> int:
    # The same canonical spelling the API stores, so ``--url`` addresses an
    # entry however the operator typed it and ``add`` cannot seed a second
    # row for a server the catalog already offers.
    if getattr(args, "url", None):
        args.url = normalize_connector_url(args.url)
    async with AsyncSessionLocal() as db:
        if args.command == "add":
            return await _add(db, args)
        if args.command == "list":
            return await _list(db)
        if args.command == "publish":
            return await _set_published(db, args.url, True)
        if args.command == "unpublish":
            return await _set_published(db, args.url, False)
        if args.command == "set-oauth-client":
            return await _set_oauth_client(db, args)
        if args.command == "set-category":
            return await _set_category(db, args)
    return 1  # pragma: no cover — argparse rejects unknown commands first


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="add a catalog entry")
    add.add_argument("--name", required=True, help="display name, copied to the connector")
    add.add_argument("--url", required=True, help="remote MCP server URL (https)")
    # Only the two the add flow can complete: BEARER/HEADER would need a secret
    # per user that the catalog cannot hold (see catalog.INSTANTIABLE_AUTH_TYPES).
    add.add_argument(
        "--auth-type",
        default=MCPServerAuthType.NONE.value,
        choices=sorted(m.value for m in INSTANTIABLE_AUTH_TYPES),
    )
    add.add_argument("--oauth-scope", default=None)
    add.add_argument("--description", default=None)
    for field, example in (
        ("name", "Tianyancha"),
        ("description", "Search Chinese company records"),
    ):
        add.add_argument(
            f"--{field}-i18n",
            action=_LocaleMap,
            default=None,
            metavar="LOCALE=TEXT",
            help=f'a translated {field}, repeatable (e.g. --{field}-i18n "en-US={example}")',
        )
    add.add_argument("--icon-url", default=None)
    add.add_argument("--sort-order", type=int, default=0)
    add.add_argument("--category", default=None, help=_CATEGORY_HELP)

    sub.add_parser("list", help="list every entry, published or not")

    set_client = sub.add_parser(
        "set-oauth-client",
        help="attach, rotate or clear an entry's pre-registered OAuth app",
    )
    set_client.add_argument("--url", required=True, help=_URL_HELP)
    group = set_client.add_mutually_exclusive_group(required=True)
    group.add_argument("--client-id", help="the app's public client id")
    group.add_argument(
        "--clear",
        action="store_true",
        help="remove the app; adds fall back to DCR/CIMD",
    )

    set_category = sub.add_parser(
        "set-category",
        help="file an entry under a category, or clear it",
    )
    set_category.add_argument("--url", required=True, help=_URL_HELP)
    category_group = set_category.add_mutually_exclusive_group(required=True)
    category_group.add_argument("--category", help=_CATEGORY_HELP)
    category_group.add_argument(
        "--clear",
        action="store_true",
        help="return the entry to uncategorised",
    )

    for name, help_text in (
        ("publish", "list an entry in the catalog"),
        ("unpublish", "hide an entry without deleting it"),
    ):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("--url", required=True, help=_URL_HELP)

    return parser.parse_args()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(_parse_args())))
