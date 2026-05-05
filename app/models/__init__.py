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
from app.models.sandbox import Sandbox
from app.models.session import Session
from app.models.user import User

__all__ = ["Base", "User", "Session", "Sandbox", "Message"]
