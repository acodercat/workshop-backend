"""Every migration module imports.

A migration that imports app code (``a3d7f61b8e04`` imports the Connector
URL normaliser) breaks the moment that module moves — and the break shows
only at ``alembic upgrade head``, on deploy. Walking the revisions loads
each module, so the suite catches it first.
"""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

_BACKEND = Path(__file__).resolve().parent.parent


def test_every_revision_loads_and_there_is_one_head() -> None:
    script = ScriptDirectory.from_config(Config(str(_BACKEND / "alembic.ini")))

    revisions = list(script.walk_revisions())

    assert revisions
    assert len(script.get_heads()) == 1
