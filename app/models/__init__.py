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

from app.models.check_in import CheckIn  # re-exports for convenience
from app.models.invite_code import InviteCode, InviteCodeOrigin
from app.models.invite_redemption import InviteRedemption
from app.models.mcp_server import MCPServer, MCPServerAuthType
from app.models.mcp_server_catalog import MCPServerCatalogEntry
from app.models.message import Message
from app.models.message_feedback import MessageFeedback
from app.models.recipe import Recipe, RecipeOrigin
from app.models.sandbox import Sandbox
from app.models.session import Session
from app.models.session_context import SessionContext
from app.models.session_files import SessionFile
from app.models.session_share import SessionShare
from app.models.skill import Skill, SkillOrigin
from app.models.user import User, UserStatus
from app.models.user_feedback import FeedbackStatus, UserFeedback
from app.models.user_skill_preference import UserSkillPreference

__all__ = [
    "Base",
    "CheckIn",
    "FeedbackStatus",
    "InviteCode",
    "InviteCodeOrigin",
    "InviteRedemption",
    "MCPServer",
    "MCPServerAuthType",
    "MCPServerCatalogEntry",
    "Message",
    "MessageFeedback",
    "Recipe",
    "RecipeOrigin",
    "Sandbox",
    "Session",
    "SessionContext",
    "SessionFile",
    "SessionShare",
    "Skill",
    "SkillOrigin",
    "User",
    "UserFeedback",
    "UserSkillPreference",
    "UserStatus",
]
