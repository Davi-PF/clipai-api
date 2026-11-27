"""Unit tests for the pure Global Ceiling collection privacy helper."""

from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest

from app.core.exceptions import AppError
from app.core.privacy import (
    allowed_profile_collection_privacies,
    can_view_collection,
    can_view_profile_collection,
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
@pytest.mark.parametrize("viewer_role", ["anon", "other", "owner"])
def test_profile_collection_visibility_matches_sql_filter_across_all_27_cases(
    global_visibility: str,
    collection_privacy: str,
    viewer_role: str,
) -> None:
    """Verify profile visibility predicate and SQL privacy filter agree on all 27 combinations."""
    owner = _StubUser(id=uuid4(), collections_visibility=global_visibility)
    other = _StubUser(id=uuid4())
    viewer = None if viewer_role == "anon" else (owner if viewer_role == "owner" else other)
    collection = _StubCollection(
        user_id=owner.id,
        privacy=collection_privacy,
        show_on_profile=True,
    )

    effective = resolve_effective_visibility(global_visibility, collection_privacy)
    if viewer_role == "owner":
        expected = True
    elif viewer_role == "other":
        expected = effective in ("public", "friends")
    else:
        expected = effective == "public"

    assert can_view_profile_collection(collection, owner, viewer) is expected

    allowed = allowed_profile_collection_privacies(
        global_visibility,
        is_owner=(viewer_role == "owner"),
        is_authenticated=(viewer is not None),
    )
    assert (collection_privacy in allowed) is expected


def test_profile_collection_never_exposes_hidden_or_unlisted_collections() -> None:
    """Collections with show_on_profile=False or privacy='unlisted' never appear on profile."""
    owner = _StubUser(id=uuid4(), collections_visibility="public")
    other = _StubUser(id=uuid4())

    hidden = _StubCollection(user_id=owner.id, privacy="public", show_on_profile=False)
    unlisted = _StubCollection(user_id=owner.id, privacy="unlisted", show_on_profile=True)

    for viewer in (None, other, owner):
        assert can_view_profile_collection(hidden, owner, viewer) is False
        assert can_view_profile_collection(unlisted, owner, viewer) is False


def test_direct_collection_access_and_error_guards() -> None:
    """Verify direct collection viewing rules and 401/403 enforcement."""
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
    assert can_view_collection(friends_coll, owner_public, other) is True
    assert can_view_collection(friends_coll, owner_public, None) is False
    assert can_view_collection(priv_coll, owner_public, other) is False
    assert can_view_collection(priv_coll, owner_public, None) is False

    # Friends global ceiling restricts public collections to authenticated users
    assert can_view_collection(pub_coll, owner_friends, None) is False
    assert can_view_collection(pub_coll, owner_friends, other) is True

    # Private global ceiling blocks all non-owners even for public/unlisted collections
    assert can_view_collection(pub_coll, owner_private, other) is False
    assert can_view_collection(unlisted_coll, owner_private, other) is False

    # Error codes from ensure_collection_viewable
    with pytest.raises(AppError) as anon_friends:
        ensure_collection_viewable(friends_coll, owner_public, None)
    assert anon_friends.value.status_code == 401
    assert anon_friends.value.code == "INVALID_TOKEN"

    with pytest.raises(AppError) as anon_global_friends:
        ensure_collection_viewable(pub_coll, owner_friends, None)
    assert anon_global_friends.value.status_code == 401

    with pytest.raises(AppError) as other_private:
        ensure_collection_viewable(priv_coll, owner_public, other)
    assert other_private.value.status_code == 403
    assert other_private.value.code == "FORBIDDEN"

    with pytest.raises(AppError) as anon_private:
        ensure_collection_viewable(priv_coll, owner_public, None)
    assert anon_private.value.status_code == 403

    with pytest.raises(AppError) as other_global_private:
        ensure_collection_viewable(pub_coll, owner_private, other)
    assert other_global_private.value.status_code == 403

    # Fallback when owner is None defaults global visibility to public
    assert can_view_collection(pub_coll, None, None) is True
    assert can_view_collection(priv_coll, None, other) is False
