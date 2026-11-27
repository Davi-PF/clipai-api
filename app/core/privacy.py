"""Visibility precedence, friendship checks, and privacy helpers for clips and collections."""

from typing import Any, Literal, Protocol
from uuid import UUID

from fastapi import status
from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError
from app.models.friendship import Friendship
from app.repositories.friendship_repository import FriendshipRepository

CollectionsVisibility = Literal["public", "friends", "private"]
CollectionPrivacy = Literal["public", "friends", "private", "unlisted"]
EffectiveVisibility = Literal["public", "friends", "private"]

FRIENDS_ONLY_ERROR_MESSAGE = "Este conteúdo é visível apenas para amigos."

_VISIBILITY_RANK: dict[str, int] = {
    "private": 0,
    "friends": 1,
    "public": 2,
}
_RANK_TO_VISIBILITY: dict[int, EffectiveVisibility] = {
    0: "private",
    1: "friends",
    2: "public",
}


class _UserLike(Protocol):
    id: UUID
    collections_visibility: str


class _ViewerLike(Protocol):
    id: UUID


class _CollectionLike(Protocol):
    user_id: UUID
    privacy: str
    show_on_profile: bool


class _ClipLike(Protocol):
    user_id: UUID
    privacy: str


async def are_friends(
    session: AsyncSession,
    user_a_id: UUID,
    user_b_id: UUID,
) -> bool:
    """Return True when user_a_id and user_b_id have an accepted friendship."""
    return await FriendshipRepository(session).are_friends(user_a_id, user_b_id)


def is_accepted_friend_clause(viewer_id: UUID, owner_id_col: Any) -> Any:
    """Build SQLAlchemy EXISTS clause matching accepted friendships without N+1 queries."""
    return exists(
        select(1).where(
            Friendship.status == "accepted",
            or_(
                and_(
                    Friendship.requester_id == viewer_id,
                    Friendship.addressee_id == owner_id_col,
                ),
                and_(
                    Friendship.addressee_id == viewer_id,
                    Friendship.requester_id == owner_id_col,
                ),
            ),
        )
    )


def resolve_effective_visibility(
    global_visibility: str | None,
    collection_privacy: str | None,
) -> EffectiveVisibility:
    """Resolve effective visibility applying the Global Ceiling rule (most restrictive wins).

    Levels ranked from most restrictive to least restrictive:
      private (0) < friends (1) < public (2).
    Legacy 'unlisted' or unknown values map to rank 0 ('private') for profile ceiling purposes.
    """
    normalized_global = (global_visibility or "public").strip().lower()
    normalized_collection = (collection_privacy or "public").strip().lower()
    global_rank = _VISIBILITY_RANK.get(normalized_global, 0)
    collection_rank = _VISIBILITY_RANK.get(normalized_collection, 0)
    return _RANK_TO_VISIBILITY[min(global_rank, collection_rank)]


def _resolve_direct_access_visibility(
    global_visibility: str | None,
    collection_privacy: str | None,
) -> EffectiveVisibility:
    """Resolve effective visibility for direct collection access by ID.

    Preserves legacy 'unlisted' direct-link behavior while still respecting the
    owner's global collections_visibility master switch.
    """
    normalized_collection = (collection_privacy or "public").strip().lower()
    effective_collection = (
        "public" if normalized_collection == "unlisted" else normalized_collection
    )
    return resolve_effective_visibility(global_visibility, effective_collection)


def can_view_collection(
    collection: _CollectionLike,
    owner: _UserLike | None,
    viewer: _ViewerLike | None,
    *,
    is_friend: bool = False,
) -> bool:
    """Return True when the viewer is allowed to open a collection directly by ID."""
    if viewer is not None and viewer.id == collection.user_id:
        return True

    global_visibility = (
        getattr(owner, "collections_visibility", None) if owner is not None else None
    )
    effective = _resolve_direct_access_visibility(global_visibility, collection.privacy)
    if effective == "public":
        return True
    if effective == "friends":
        return viewer is not None and is_friend
    return False


def can_view_profile_collection(
    collection: _CollectionLike,
    owner: _UserLike,
    viewer: _ViewerLike | None,
    *,
    is_friend: bool = False,
) -> bool:
    """Return True when a collection should appear on the owner's public profile showcase."""
    if not bool(getattr(collection, "show_on_profile", False)):
        return False

    normalized_privacy = (collection.privacy or "").strip().lower()
    if normalized_privacy == "unlisted":
        return False

    if viewer is not None and viewer.id == owner.id:
        return True

    effective = resolve_effective_visibility(
        getattr(owner, "collections_visibility", None),
        normalized_privacy,
    )
    if effective == "public":
        return True
    if effective == "friends":
        return viewer is not None and is_friend
    return False


def allowed_profile_collection_privacies(
    global_visibility: str | None,
    *,
    is_owner: bool,
    is_friend: bool = False,
) -> tuple[str, ...]:
    """Return SQL-filterable collection.privacy values visible on a user's profile showcase."""
    if is_owner:
        return ("public", "friends", "private")

    normalized_global = (global_visibility or "public").strip().lower()
    if normalized_global == "private":
        return ()
    if is_friend:
        return ("public", "friends") if normalized_global in ("public", "friends") else ()
    return ("public",) if normalized_global == "public" else ()


def ensure_collection_viewable(
    collection: _CollectionLike,
    owner: _UserLike | None,
    viewer: _ViewerLike | None,
    *,
    is_friend: bool = False,
) -> None:
    """Raise 401/403 AppError when the viewer cannot access the given collection."""
    if can_view_collection(collection, owner, viewer, is_friend=is_friend):
        return

    global_visibility = (
        getattr(owner, "collections_visibility", None) if owner is not None else None
    )
    effective = _resolve_direct_access_visibility(global_visibility, collection.privacy)
    if effective == "friends":
        if viewer is None:
            raise AppError(
                status_code=status.HTTP_401_UNAUTHORIZED,
                code="INVALID_TOKEN",
                message="Faça login para visualizar coleções restritas a amigos.",
            )
        raise AppError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="FORBIDDEN",
            message=FRIENDS_ONLY_ERROR_MESSAGE,
        )
    raise AppError(
        status_code=status.HTTP_403_FORBIDDEN,
        code="FORBIDDEN",
        message="Você não tem permissão para acessar esta coleção.",
    )


def ensure_clip_viewable(
    clip: _ClipLike,
    viewer: _ViewerLike | None,
    *,
    is_friend: bool = False,
) -> None:
    """Raise 401/403 AppError when the viewer cannot access a friends-only clip."""
    if clip.privacy != "friends":
        return
    if viewer is not None and viewer.id == clip.user_id:
        return
    if viewer is None:
        raise AppError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="INVALID_TOKEN",
            message="Faça login para visualizar clipes restritos a amigos.",
        )
    if not is_friend:
        raise AppError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="FORBIDDEN",
            message=FRIENDS_ONLY_ERROR_MESSAGE,
        )
