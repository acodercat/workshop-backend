"""
Auto-import every module in this package so all SQLAlchemy models are
registered with `Base.metadata`. This is what Alembic autogenerate relies on —
adding a new file under `app/models/` requires no manual edits here.
"""
import importlib
import pkgutil

from app.database import Base

for module_info in pkgutil.iter_modules(__path__):
    importlib.import_module(f"{__name__}.{module_info.name}")

from app.models.message import Message  # re-exports for convenience
from app.models.message_feedback import MessageFeedback
from app.models.sandbox import Sandbox
from app.models.session import Session
from app.models.session_files import SessionFile
from app.models.session_share import SessionShare
from app.models.user import User

__all__ = [
    "Base",
    "User",
    "Session",
    "SessionFile",
    "SessionShare",
    "Sandbox",
    "Message",
    "MessageFeedback",
]
