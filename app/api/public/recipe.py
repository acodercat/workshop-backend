"""User-authored recipe CRUD.

A recipe is a small prompt scaffold the user picks from the composer;
its ``instructions`` ride along to the worker as an axon system-reminder
(see ``app/models/recipe.py``). Unlike a skill, there is no uploaded
bundle — create / edit are plain JSON, and ``instructions`` are mutable
in place.

Visibility mirrors skills: a user sees their own CUSTOM recipes plus all
OFFICIAL ones. OFFICIAL recipes are operator-managed and read-only
through this surface.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select, update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db
from app.auth import authenticate_and_get_user
from app.models.recipe import Recipe, RecipeOrigin
from app.models.user import User
from app.rate_limit import limiter
from app.schemas.recipe import RecipeCreate, RecipeResponse, RecipeUpdate

logger = logging.getLogger(__name__)

router = APIRouter(tags=["recipes"])

# Per-user cap on CUSTOM recipes — bounds account-level abuse so one
# account can't flood the picker / table. Matches the skill upload cap
# (``skill_registration._MAX_SKILLS_PER_USER`` = 100), which is likewise
# about storage rather than per-turn cost: a recipe, like a skill, only
# reaches a turn the user picked it for.
_MAX_RECIPES_PER_USER = 100


@router.post("/recipes", status_code=status.HTTP_201_CREATED)
@limiter.limit("30/minute")
async def create_recipe(
    request: Request,
    body: RecipeCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> RecipeResponse:
    """Author a CUSTOM recipe owned by the caller.

    403 if the user is already at the per-user recipe cap.
    """
    # Race-acceptable like the skill upload cap: two concurrent creates could
    # each pass and put the user one over. Not worth a row lock for a
    # soft policy limit. OFFICIAL rows have ``user_id IS NULL`` so they
    # don't count against a user.
    used = await db.scalar(select(func.count(Recipe.id)).where(Recipe.user_id == current_user.id))
    if used is not None and used >= _MAX_RECIPES_PER_USER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Recipe limit reached ({_MAX_RECIPES_PER_USER})",
        )

    recipe = Recipe(
        user_id=current_user.id,
        origin=RecipeOrigin.CUSTOM,
        # Opaque, unique-per-user identifier — the human title lives in
        # ``display_name``, so ``name`` needn't be slugified from
        # (possibly all-CJK) free text.
        name=f"recipe-{uuid.uuid4().hex[:8]}",
        display_name=body.display_name,
        description=body.description,
        instructions=body.instructions,
    )
    db.add(recipe)
    await db.commit()
    await db.refresh(recipe)
    logger.info(
        "recipe created: id=%s user_id=%s name=%r",
        recipe.id,
        current_user.id,
        recipe.name,
    )
    return RecipeResponse.model_validate(recipe)


@router.get("/recipes")
async def list_recipes(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> list[RecipeResponse]:
    """Return recipes visible to the caller: their own CUSTOM recipes +
    all OFFICIAL recipes. Newest first.

    ``enabled`` is the recipe's own flag (no per-user override table —
    recipes are picked ad-hoc, not toggled like ambient skills).
    """
    stmt = (
        select(Recipe)
        .where((Recipe.user_id == current_user.id) | (Recipe.origin == RecipeOrigin.OFFICIAL))
        .order_by(Recipe.created_at.desc())
    )
    result = await db.execute(stmt)
    return [RecipeResponse.model_validate(recipe) for recipe in result.scalars().all()]


@router.patch("/recipes/{recipe_id}")
async def update_recipe(
    recipe_id: int,
    body: RecipeUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> RecipeResponse:
    """Edit a CUSTOM recipe owned by the caller.

    ``display_name`` / ``description`` / ``instructions`` / ``enabled``
    are all writable. OFFICIAL recipes are operator-managed → 403; a
    CUSTOM recipe owned by someone else → 404 (invisible here).
    """
    recipe = await _get_visible_recipe(db, recipe_id, current_user.id)
    if recipe.origin == RecipeOrigin.OFFICIAL:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot edit an OFFICIAL recipe",
        )

    # Collect non-null fields and apply them in a single UPDATE — avoids
    # the read-modify-write race where two concurrent PATCHes each load
    # the row, mutate one field, and the second commit wipes the first.
    updates = {
        field: value
        for field, value in body.model_dump(exclude_unset=True).items()
        if value is not None
    }
    if updates:
        await db.execute(sql_update(Recipe).where(Recipe.id == recipe.id).values(**updates))
        await db.commit()
        await db.refresh(recipe)
    return RecipeResponse.model_validate(recipe)


@router.delete("/recipes/{recipe_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_recipe(
    recipe_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> None:
    """Delete a CUSTOM recipe owned by the caller.

    No external cleanup (a recipe has no OSS bundle), so this is a plain
    row delete. In-flight turns are unaffected: each carries its own
    rendered snapshot on ``session_turn.recipe``.
    """
    recipe = await _get_owned_recipe(db, recipe_id, current_user.id)
    recipe_name = recipe.name
    await db.delete(recipe)
    await db.commit()
    logger.info(
        "recipe deleted: id=%s user_id=%s name=%r",
        recipe_id,
        current_user.id,
        recipe_name,
    )


async def _get_owned_recipe(
    db: AsyncSession,
    recipe_id: int,
    user_id: int,
) -> Recipe:
    """Fetch a CUSTOM recipe that belongs to ``user_id``.

    Used by DELETE — only the owner can drop their CUSTOM recipe.
    OFFICIAL recipes (no owner) never match, so a DELETE on one 404s
    rather than removing a shared platform row.
    """
    stmt = select(Recipe).where(
        Recipe.id == recipe_id,
        Recipe.user_id == user_id,
    )
    result = await db.execute(stmt)
    recipe = result.scalar_one_or_none()
    if recipe is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="recipe not found",
        )
    return recipe


async def _get_visible_recipe(
    db: AsyncSession,
    recipe_id: int,
    user_id: int,
) -> Recipe:
    """Fetch a recipe visible to ``user_id`` (own CUSTOM or any OFFICIAL).

    Used by PATCH — the caller then rejects the OFFICIAL case with 403,
    distinguishing "exists but you can't edit it" from a plain 404 on a
    CUSTOM recipe owned by someone else.
    """
    stmt = select(Recipe).where(
        Recipe.id == recipe_id,
        (Recipe.user_id == user_id) | (Recipe.origin == RecipeOrigin.OFFICIAL),
    )
    result = await db.execute(stmt)
    recipe = result.scalar_one_or_none()
    if recipe is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="recipe not found",
        )
    return recipe
