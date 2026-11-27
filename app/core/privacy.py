"""Pure visibility precedence and privacy helpers for user collections (Global Ceiling)."""

from typing import Literal, Protocol
from uuid import UUID

from fastapi import status

from app.core.exceptions import AppError

CollectionsVisibility = Literal["public", "friends", "private"]
CollectionPrivacy = Literal["public", "friends", "private", "unlisted"]
EffectiveVisibility = Literal["public", "friends", "private"]

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
        return viewer is not None
    return False


def can_view_profile_collection(
    collection: _CollectionLike,
    owner: _UserLike,
    viewer: _ViewerLike | None,
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
        return viewer is not None
    return False


def allowed_profile_collection_privacies(
    global_visibility: str | None,
    *,
    is_owner: bool,
    is_authenticated: bool,
) -> tuple[str, ...]:
    """Return SQL-filterable collection.privacy values visible on a user's profile showcase."""
    if is_owner:
        return ("public", "friends", "private")

    normalized_global = (global_visibility or "public").strip().lower()
    if normalized_global == "private":
        return ()
    if is_authenticated:
        return ("public", "friends") if normalized_global in ("public", "friends") else ()
    return ("public",) if normalized_global == "public" else ()


def ensure_collection_viewable(
    collection: _CollectionLike,
    owner: _UserLike | None,
    viewer: _ViewerLike | None,
) -> None:
    """Raise 401/403 AppError when the viewer cannot access the given collection."""
    if can_view_collection(collection, owner, viewer):
        return

    global_visibility = (
        getattr(owner, "collections_visibility", None) if owner is not None else None
    )
    effective = _resolve_direct_access_visibility(global_visibility, collection.privacy)
    if effective == "friends" and viewer is None:
        raise AppError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="INVALID_TOKEN",
            message="Faça login para visualizar coleções restritas a amigos.",
        )
    raise AppError(
        status_code=status.HTTP_403_FORBIDDEN,
        code="FORBIDDEN",
        message="Você não tem permissão para acessar esta coleção.",
    )
