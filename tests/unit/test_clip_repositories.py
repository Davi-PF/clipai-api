"""Unit tests for GameRepository, TagRepository, and ClipRepository."""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.clip import Clip, ClipView, Tag
from app.models.game import Game
from app.repositories.clip_repository import ClipRepository
from app.repositories.game_repository import GameRepository
from app.repositories.tag_repository import TagRepository, normalize_tag_slug


async def test_game_repository_queries() -> None:
    """Verify GameRepository lookup and paginated search."""
    session = AsyncMock(spec=AsyncSession)
    repo = GameRepository(session)
    game = Game(id=uuid4(), name="CS2", slug="cs2")

    lookup_result = MagicMock()
    lookup_result.scalar_one_or_none.return_value = game
    session.execute.return_value = lookup_result

    assert await repo.get_by_id(game.id) is game
    assert await repo.get_by_slug("CS2") is game

    count_result = MagicMock()
    count_result.scalar_one.return_value = 1
    list_result = MagicMock()
    list_result.scalars.return_value.all.return_value = [game]
    session.execute.side_effect = [count_result, list_result]

    items, total = await repo.list_games(q="CS", page=1, page_size=20)
    assert total == 1
    assert items == [game]


async def test_tag_repository_normalization_and_creation() -> None:
    """Verify TagRepository normalizes slugs, deduplicates, and reuses existing tags."""
    assert normalize_tag_slug("  Headshot Insano!! ") == "headshot-insano"
    assert normalize_tag_slug("___") == ""

    session = AsyncMock(spec=AsyncSession)
    session.add = MagicMock()
    repo = TagRepository(session)

    existing_tag = Tag(id=uuid4(), name="clutch", slug="clutch")
    list_result = MagicMock()
    list_result.scalars.return_value.all.return_value = [existing_tag]
    session.execute.return_value = list_result

    assert await repo.list_tags(q="clu", limit=10) == [existing_tag]
    assert await repo.get_or_create_many([]) == []

    session.execute.return_value = list_result
    tags = await repo.get_or_create_many(["Clutch", "clutch", "AWP Play", "!!!"])
    assert [t.slug for t in tags] == ["clutch", "awp-play"]
    session.add.assert_called_once()
    session.flush.assert_awaited()


async def test_clip_repository_crud_and_filtering() -> None:
    """Verify ClipRepository queries, create, tag replacement, and list filters."""
    session = AsyncMock(spec=AsyncSession)
    session.add = MagicMock()
    repo = ClipRepository(session)

    clip_id = uuid4()
    user_id = uuid4()
    clip = Clip(
        id=clip_id,
        slug="abc123slug",
        user_id=user_id,
        game_id=None,
        title="Ace na Mirage",
        status="ready",
        privacy="public",
        video_object_key=f"clips/{user_id}/{clip_id}/video.mp4",
        thumbnail_object_key=None,
        file_size_bytes=1024,
        duration_seconds=Decimal("15.00"),
        mime_type="video/mp4",
        technical_metadata={},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    lookup_result = MagicMock()
    lookup_result.scalar_one_or_none.return_value = clip
    session.execute.return_value = lookup_result

    assert await repo.get_by_identifier(str(clip_id)) is clip
    assert await repo.get_by_identifier("abc123slug") is clip

    count_result = MagicMock()
    count_result.scalar_one.return_value = 2
    session.execute.return_value = count_result
    assert await repo.count_pending_by_user(user_id) == 2

    tag = Tag(id=uuid4(), name="ace", slug="ace")
    created = await repo.create(
        clip_id=uuid4(),
        slug="newslug12",
        user_id=user_id,
        game_id=None,
        title="Novo Clipe",
        description=None,
        privacy="public",
        video_object_key="clips/u/c/video.mp4",
        thumbnail_object_key=None,
        file_size_bytes=2048,
        duration_seconds=Decimal("10.00"),
        mime_type="video/mp4",
        technical_metadata={},
        tags=[tag],
    )
    assert created.slug == "newslug12"

    await repo.replace_tags(created, [tag])
    await repo.delete(created)
    session.delete.assert_awaited_once_with(created)

    list_count = MagicMock()
    list_count.scalar_one.return_value = 1
    list_items = MagicMock()
    list_items.scalars.return_value.all.return_value = [clip]
    session.execute.side_effect = [list_count, list_items, list_count, list_items]

    items, total = await repo.list_clips(
        q="mirage",
        game_slug="cs2",
        tag_slug="ace",
        author_username="fallen",
        mine=False,
        current_user_id=user_id,
        sort="popular",
    )
    assert total == 1
    assert items == [clip]

    mine_items, mine_total = await repo.list_clips(
        mine=True,
        current_user_id=user_id,
        sort="recent",
    )
    assert mine_total == 1
    assert mine_items == [clip]


async def test_clip_repository_featured_most_viewed_period_and_view_recording() -> None:
    """Verify ClipRepository featured/most_viewed/period SQL queries and ClipView tracking."""
    session = AsyncMock(spec=AsyncSession)
    session.add = MagicMock()
    repo = ClipRepository(session)

    clip_id = uuid4()
    viewer_id = uuid4()
    view_res = MagicMock()
    view_res.scalar_one.return_value = 42
    session.execute.return_value = view_res

    updated_views = await repo.increment_views(clip_id, viewer_id=viewer_id)
    assert updated_views == 42
    added_view = session.add.call_args[0][0]
    assert isinstance(added_view, ClipView)
    assert added_view.clip_id == clip_id
    assert added_view.viewer_id == viewer_id

    # Test featured/most_viewed/recent combinations across period windows
    for sort_opt, period_opt in [
        ("featured", "all"),
        ("featured", "7d"),
        ("most_viewed", "all"),
        ("most_viewed", "30d"),
        ("recent", "7d"),
    ]:
        count_res = MagicMock()
        count_res.scalar_one.return_value = 0
        items_res = MagicMock()
        items_res.scalars.return_value.all.return_value = []
        session.execute.side_effect = [count_res, items_res]

        items, total = await repo.list_clips(sort=sort_opt, period=period_opt)
        assert items == []
        assert total == 0

        list_stmt = session.execute.call_args_list[-1][0][0]
        compiled_sql = str(list_stmt.compile(dialect=postgresql.dialect()))
        if sort_opt == "featured":
            assert "favorites_count" in compiled_sql or "period_favorites" in compiled_sql
        if sort_opt == "most_viewed":
            assert "views_count" in compiled_sql or "clip_views" in compiled_sql

    top_res = MagicMock()
    top_res.scalar_one_or_none.return_value = clip_id
    session.execute.side_effect = None
    session.execute.return_value = top_res
    assert await repo.get_top_highlight_clip_id(current_user_id=viewer_id, period="7d") == clip_id
    assert await repo.get_top_highlight_clip_id(current_user_id=None, period="all") == clip_id
