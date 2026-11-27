"""Unit tests for CollectionRepository and ClipRepository social/engagement methods."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.collection import Collection, CollectionClip
from app.repositories.clip_repository import ClipRepository
from app.repositories.collection_repository import CollectionRepository


async def test_clip_repository_likes_favorites_views_and_batch_lookups() -> None:
    """Verify idempotent like/favorite counters, view increments, and batch queries."""
    session = AsyncMock(spec=AsyncSession)
    session.add = MagicMock()
    repo = ClipRepository(session)
    user_id = uuid4()
    clip_id = uuid4()

    # Like when not yet liked -> inserts + increments
    none_res = MagicMock()
    none_res.scalar_one_or_none.return_value = None
    count_one = MagicMock()
    count_one.scalar_one.return_value = 1
    session.execute.side_effect = [none_res, count_one]
    assert await repo.like_clip(user_id=user_id, clip_id=clip_id) == 1
    session.add.assert_called_once()

    # Like when already liked -> idempotent return without insert
    exists_res = MagicMock()
    exists_res.scalar_one_or_none.return_value = user_id
    session.execute.side_effect = [exists_res, count_one]
    assert await repo.like_clip(user_id=user_id, clip_id=clip_id) == 1

    # Unlike when liked -> decrements; when not liked -> idempotent
    deleted_res = MagicMock()
    deleted_res.scalar_one_or_none.return_value = clip_id
    count_zero = MagicMock()
    count_zero.scalar_one.return_value = 0
    session.execute.side_effect = [deleted_res, count_zero, none_res, count_zero]
    assert await repo.unlike_clip(user_id=user_id, clip_id=clip_id) == 0
    assert await repo.unlike_clip(user_id=user_id, clip_id=clip_id) == 0

    # Favorite & unfavorite idempotent paths
    session.execute.side_effect = [
        none_res,
        count_one,
        exists_res,
        count_one,
        deleted_res,
        count_zero,
        none_res,
        count_zero,
    ]
    assert await repo.favorite_clip(user_id=user_id, clip_id=clip_id) == 1
    assert await repo.favorite_clip(user_id=user_id, clip_id=clip_id) == 1
    assert await repo.unfavorite_clip(user_id=user_id, clip_id=clip_id) == 0
    assert await repo.unfavorite_clip(user_id=user_id, clip_id=clip_id) == 0

    # Atomic view increment
    views_res = MagicMock()
    views_res.scalar_one.return_value = 42
    session.execute.side_effect = [views_res]
    assert await repo.increment_views(clip_id) == 42

    # Batch liked/favorited lookups (empty + populated)
    assert await repo.get_user_liked_clip_ids(user_id, []) == set()
    assert await repo.get_user_favorited_clip_ids(user_id, []) == set()

    batch_res = MagicMock()
    batch_res.scalars.return_value.all.return_value = [clip_id]
    session.execute.side_effect = [batch_res, batch_res]
    assert await repo.get_user_liked_clip_ids(user_id, [clip_id]) == {clip_id}
    assert await repo.get_user_favorited_clip_ids(user_id, [clip_id]) == {clip_id}

    # Featured + favorited + collection_id filter query construction
    list_count = MagicMock()
    list_count.scalar_one.return_value = 0
    list_items = MagicMock()
    list_items.scalars.return_value.all.return_value = []
    session.execute.side_effect = [list_count, list_items]
    items, total = await repo.list_clips(
        favorited_by_user_id=user_id,
        collection_id=uuid4(),
        current_user_id=user_id,
        sort="featured",
    )
    assert items == []
    assert total == 0


async def test_collection_repository_crud_previews_and_membership() -> None:
    """Verify CollectionRepository queries, batch previews, and clip links."""
    session = AsyncMock(spec=AsyncSession)
    session.add = MagicMock()
    repo = CollectionRepository(session)
    user_id = uuid4()
    clip_id = uuid4()
    coll = Collection(
        id=uuid4(),
        user_id=user_id,
        title="Melhores Clutches",
        description="Compilado",
        privacy="public",
        accent_color="#2EB886",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    lookup = MagicMock()
    lookup.scalar_one_or_none.return_value = coll
    session.execute.return_value = lookup
    assert await repo.get_by_id(coll.id) is coll

    created = await repo.create(
        user_id=user_id,
        title="Nova",
        description=None,
        privacy="private",
        accent_color="#E8B04B",
        show_on_profile=True,
    )
    assert created.accent_color == "#E8B04B"
    assert created.privacy == "private"
    assert created.show_on_profile is True

    # Profile showcase listing (empty short-circuit and queried path)
    assert await repo.list_profile_collections(owner_id=user_id, allowed_privacies=()) == ([], 0)
    profile_count = MagicMock()
    profile_count.scalar_one.return_value = 1
    profile_items = MagicMock()
    profile_items.scalars.return_value.all.return_value = [coll]
    session.execute.side_effect = [profile_count, profile_items]
    prof_list, prof_total = await repo.list_profile_collections(
        owner_id=user_id,
        allowed_privacies=("public", "friends"),
    )
    assert prof_total == 1
    assert prof_list == [coll]

    # List collections (anon, auth, mine)
    count_res = MagicMock()
    count_res.scalar_one.return_value = 1
    list_res = MagicMock()
    list_res.scalars.return_value.all.return_value = [coll]
    session.execute.side_effect = [count_res, list_res, count_res, list_res, count_res, list_res]
    items, total = await repo.list_collections(author_username="fallen", current_user_id=None)
    assert total == 1
    assert items == [coll]
    await repo.list_collections(mine=False, current_user_id=user_id)
    await repo.list_collections(mine=True, current_user_id=user_id)

    # Preview metadata batch
    assert await repo.get_collections_preview_metadata([], current_user_id=None) == {}
    preview_res = MagicMock()
    preview_res.all.return_value = [(coll.id, "clips/u/c/thumb.webp"), (coll.id, None)]
    session.execute.side_effect = [preview_res]
    previews = await repo.get_collections_preview_metadata([coll.id], current_user_id=user_id)
    assert previews[coll.id] == (2, ["clips/u/c/thumb.webp"])

    # Collection clips listing and membership ops
    link = CollectionClip(collection_id=coll.id, clip_id=clip_id, position=0)
    links_res = MagicMock()
    links_res.scalars.return_value.all.return_value = [link]
    link_one = MagicMock()
    link_one.scalar_one_or_none.return_value = link
    next_pos = MagicMock()
    next_pos.scalar_one.return_value = 1
    session.execute.side_effect = [count_res, links_res, link_one, next_pos, MagicMock()]

    links, link_total = await repo.list_collection_clips(coll.id, current_user_id=None)
    assert link_total == 1
    assert links == [link]
    assert await repo.get_clip_link(coll.id, clip_id) is link
    assert await repo.get_next_position(coll.id) == 1

    added = await repo.add_clip(collection_id=coll.id, clip_id=clip_id, position=1)
    assert added.position == 1
    await repo.remove_clip(collection_id=coll.id, clip_id=clip_id)
    await repo.delete(coll)
    session.delete.assert_awaited_once_with(coll)
