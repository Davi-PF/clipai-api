"""Unit tests for visibility precedence, friendship checks, and privacy helpers."""

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError
from app.core.privacy import (
    FRIENDS_ONLY_ERROR_MESSAGE,
    allowed_profile_collection_privacies,
    are_friends,
    can_view_collection,
    can_view_profile_collection,
    ensure_clip_viewable,
    ensure_collection_viewable,
    resolve_effective_visibility,
)


@dataclass
class _StubUser:
    id: UUID
    collections_visibility: str = "public"


@dataclass
class _StubCollection:
    user_id: UUID
    privacy: str = "public"
    show_on_profile: bool = True


@dataclass
class _StubClip:
    user_id: UUID
    privacy: str = "public"


@pytest.mark.parametrize(
    ("global_visibility", "collection_privacy", "expected"),
    [
        ("public", "public", "public"),
        ("public", "friends", "friends"),
        ("public", "private", "private"),
        ("friends", "public", "friends"),
        ("friends", "friends", "friends"),
        ("friends", "private", "private"),
        ("private", "public", "private"),
        ("private", "friends", "private"),
        ("private", "private", "private"),
    ],
)
def test_resolve_effective_visibility_full_3x3_matrix(
    global_visibility: str,
    collection_privacy: str,
    expected: str,
) -> None:
    """Verify every cell of the 3x3 Global Ceiling precedence matrix."""
    assert resolve_effective_visibility(global_visibility, collection_privacy) == expected


def test_resolve_effective_visibility_handles_legacy_unlisted_and_normalization() -> None:
    """Ensure legacy unlisted/unknown values map safely to private and None defaults to public."""
    assert resolve_effective_visibility("public", "unlisted") == "private"
    assert resolve_effective_visibility("friends", "unlisted") == "private"
    assert resolve_effective_visibility("private", "unlisted") == "private"
    assert resolve_effective_visibility(" PUBLIC ", " Friends ") == "friends"
    assert resolve_effective_visibility(None, None) == "public"
    assert resolve_effective_visibility("unknown", "public") == "private"
    assert resolve_effective_visibility("public", "unknown") == "private"


@pytest.mark.parametrize("global_visibility", ["public", "friends", "private"])
@pytest.mark.parametrize("collection_privacy", ["public", "friends", "private"])
@pytest.mark.parametrize("viewer_role", ["anon", "non_friend", "friend", "owner"])
def test_profile_collection_visibility_matches_sql_filter_across_all_36_cases(
    global_visibility: str,
    collection_privacy: str,
    viewer_role: str,
) -> None:
    """Verify profile visibility predicate and SQL privacy filter agree on all 36 combinations."""
    owner = _StubUser(id=uuid4(), collections_visibility=global_visibility)
    other = _StubUser(id=uuid4())
    viewer = None if viewer_role == "anon" else (owner if viewer_role == "owner" else other)
    is_friend = viewer_role == "friend"
    collection = _StubCollection(
        user_id=owner.id,
        privacy=collection_privacy,
        show_on_profile=True,
    )

    effective = resolve_effective_visibility(global_visibility, collection_privacy)
    if viewer_role == "owner":
        expected = True
    elif viewer_role == "friend":
        expected = effective in ("public", "friends")
    else:
        expected = effective == "public"

    assert can_view_profile_collection(collection, owner, viewer, is_friend=is_friend) is expected

    allowed = allowed_profile_collection_privacies(
        global_visibility,
        is_owner=(viewer_role == "owner"),
        is_friend=is_friend,
    )
    assert (collection_privacy in allowed) is expected


def test_profile_collection_never_exposes_hidden_or_unlisted_collections() -> None:
    """Collections with show_on_profile=False or privacy='unlisted' never appear on profile."""
    owner = _StubUser(id=uuid4(), collections_visibility="public")
    other = _StubUser(id=uuid4())

    hidden = _StubCollection(user_id=owner.id, privacy="public", show_on_profile=False)
    unlisted = _StubCollection(user_id=owner.id, privacy="unlisted", show_on_profile=True)

    for viewer in (None, other, owner):
        assert can_view_profile_collection(hidden, owner, viewer, is_friend=True) is False
        assert can_view_profile_collection(unlisted, owner, viewer, is_friend=True) is False


def test_direct_collection_access_and_error_guards() -> None:
    """Verify direct collection viewing rules and 401/403 enforcement for friends vs non-friends."""
    owner_public = _StubUser(id=uuid4(), collections_visibility="public")
    owner_friends = _StubUser(id=owner_public.id, collections_visibility="friends")
    owner_private = _StubUser(id=owner_public.id, collections_visibility="private")
    other = _StubUser(id=uuid4())

    pub_coll = _StubCollection(user_id=owner_public.id, privacy="public", show_on_profile=False)
    friends_coll = _StubCollection(
        user_id=owner_public.id, privacy="friends", show_on_profile=False
    )
    priv_coll = _StubCollection(user_id=owner_public.id, privacy="private", show_on_profile=False)
    unlisted_coll = _StubCollection(
        user_id=owner_public.id, privacy="unlisted", show_on_profile=False
    )

    # Owner always has direct access regardless of collection privacy or global ceiling
    for coll in (pub_coll, friends_coll, priv_coll, unlisted_coll):
        assert can_view_collection(coll, owner_private, owner_private) is True
        ensure_collection_viewable(coll, owner_private, owner_private)

    # Public global ceiling
    assert can_view_collection(pub_coll, owner_public, None) is True
    assert can_view_collection(unlisted_coll, owner_public, None) is True
    assert can_view_collection(friends_coll, owner_public, other, is_friend=True) is True
    assert can_view_collection(friends_coll, owner_public, other, is_friend=False) is False
    assert can_view_collection(friends_coll, owner_public, None) is False
    assert can_view_collection(priv_coll, owner_public, other, is_friend=True) is False
    assert can_view_collection(priv_coll, owner_public, None) is False

    # Friends global ceiling restricts public collections to accepted friends
    assert can_view_collection(pub_coll, owner_friends, None) is False
    assert can_view_collection(pub_coll, owner_friends, other, is_friend=False) is False
    assert can_view_collection(pub_coll, owner_friends, other, is_friend=True) is True

    # Private global ceiling blocks all non-owners even for accepted friends
    assert can_view_collection(pub_coll, owner_private, other, is_friend=True) is False
    assert can_view_collection(unlisted_coll, owner_private, other, is_friend=True) is False

    # Error codes from ensure_collection_viewable
    with pytest.raises(AppError) as anon_friends:
        ensure_collection_viewable(friends_coll, owner_public, None)
    assert anon_friends.value.status_code == 401
    assert anon_friends.value.code == "INVALID_TOKEN"

    with pytest.raises(AppError) as non_friend_exc:
        ensure_collection_viewable(friends_coll, owner_public, other, is_friend=False)
    assert non_friend_exc.value.status_code == 403
    assert non_friend_exc.value.code == "FORBIDDEN"
    assert non_friend_exc.value.message == FRIENDS_ONLY_ERROR_MESSAGE

    with pytest.raises(AppError) as anon_global_friends:
        ensure_collection_viewable(pub_coll, owner_friends, None)
    assert anon_global_friends.value.status_code == 401

    with pytest.raises(AppError) as non_friend_global:
        ensure_collection_viewable(pub_coll, owner_friends, other, is_friend=False)
    assert non_friend_global.value.status_code == 403
    assert non_friend_global.value.message == FRIENDS_ONLY_ERROR_MESSAGE

    with pytest.raises(AppError) as other_private:
        ensure_collection_viewable(priv_coll, owner_public, other, is_friend=True)
    assert other_private.value.status_code == 403
    assert other_private.value.code == "FORBIDDEN"

    with pytest.raises(AppError) as anon_private:
        ensure_collection_viewable(priv_coll, owner_public, None)
    assert anon_private.value.status_code == 403

    with pytest.raises(AppError) as other_global_private:
        ensure_collection_viewable(pub_coll, owner_private, other, is_friend=True)
    assert other_global_private.value.status_code == 403

    # Fallback when owner is None defaults global visibility to public
    assert can_view_collection(pub_coll, None, None) is True
    assert can_view_collection(priv_coll, None, other) is False


def test_ensure_clip_viewable_enforces_accepted_friendship() -> None:
    """Verify friends-only clips require owner or accepted friendship."""
    owner = _StubUser(id=uuid4())
    other = _StubUser(id=uuid4())
    pub_clip = _StubClip(user_id=owner.id, privacy="public")
    friends_clip = _StubClip(user_id=owner.id, privacy="friends")

    ensure_clip_viewable(pub_clip, None)
    ensure_clip_viewable(friends_clip, owner)
    ensure_clip_viewable(friends_clip, other, is_friend=True)

    with pytest.raises(AppError) as anon_exc:
        ensure_clip_viewable(friends_clip, None)
    assert anon_exc.value.status_code == 401

    with pytest.raises(AppError) as non_friend_exc:
        ensure_clip_viewable(friends_clip, other, is_friend=False)
    assert non_friend_exc.value.status_code == 403
    assert non_friend_exc.value.message == FRIENDS_ONLY_ERROR_MESSAGE


async def test_are_friends_helper_delegates_to_friendship_repository() -> None:
    """Verify are_friends returns True when an accepted friendship exists and False otherwise."""
    session = AsyncMock(spec=AsyncSession)
    user_a = uuid4()
    user_b = uuid4()

    assert await are_friends(session, user_a, user_a) is False

    found_res = MagicMock()
    found_res.scalar_one_or_none.return_value = uuid4()
    session.execute.return_value = found_res
    assert await are_friends(session, user_a, user_b) is True

    none_res = MagicMock()
    none_res.scalar_one_or_none.return_value = None
    session.execute.return_value = none_res
    assert await are_friends(session, user_a, user_b) is False
