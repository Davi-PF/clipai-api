"""Unit tests for FriendshipRepository, NotificationRepository, and UserRepository search/invite."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.friendship import Friendship
from app.models.notification import Notification
from app.models.user import User
from app.repositories.clip_repository import ClipRepository
from app.repositories.collection_repository import CollectionRepository
from app.repositories.friendship_repository import FriendshipRepository
from app.repositories.notification_repository import NotificationRepository
from app.repositories.user_repository import UserRepository


def _make_user(username: str = "fallen_br", invite_code: str = "inviteCode12") -> User:
    now = datetime.now(UTC)
    return User(
        id=uuid4(),
        username=username,
        email=f"{username}@clipai.com.br",
        password_hash="hashed",
        display_name=username,
        invite_code=invite_code,
        is_email_verified=True,
        is_active=True,
        created_at=now,
        updated_at=now,
    )


async def test_friendship_repository_queries_and_mutations() -> None:
    """Verify FriendshipRepository lookups, batch fetch, status lists, and mutations."""
    session = AsyncMock(spec=AsyncSession)
    session.add = MagicMock()
    repo = FriendshipRepository(session)

    user_a = _make_user("user_a", "codeA1234567")
    user_b = _make_user("user_b", "codeB1234567")
    now = datetime.now(UTC)
    friendship = Friendship(
        id=uuid4(),
        requester_id=user_a.id,
        addressee_id=user_b.id,
        status="accepted",
        requester=user_a,
        addressee=user_b,
        created_at=now,
        updated_at=now,
    )

    one_res = MagicMock()
    one_res.scalar_one_or_none.return_value = friendship
    session.execute.return_value = one_res
    assert await repo.get_by_id(friendship.id) is friendship
    assert await repo.get_between_users(user_a.id, user_b.id) is friendship

    # Empty batch lookup short-circuits
    assert await repo.get_between_user_and_many(user_a.id, []) == {}

    many_res = MagicMock()
    many_res.scalars.return_value.all.return_value = [friendship]
    session.execute.return_value = many_res
    assert await repo.get_between_user_and_many(user_a.id, [user_b.id]) == {user_b.id: friendship}
    assert await repo.get_between_user_and_many(user_b.id, [user_a.id]) == {user_a.id: friendship}

    # are_friends: self is always False, found returns True, None returns False
    assert await repo.are_friends(user_a.id, user_a.id) is False
    id_res = MagicMock()
    id_res.scalar_one_or_none.return_value = friendship.id
    session.execute.return_value = id_res
    assert await repo.are_friends(user_a.id, user_b.id) is True

    # get_accepted_friend_ids: empty candidates short-circuit + normal fetch
    assert await repo.get_accepted_friend_ids(user_a.id, []) == set()
    pairs_res = MagicMock()
    pairs_res.all.return_value = [(user_a.id, user_b.id)]
    session.execute.return_value = pairs_res
    assert await repo.get_accepted_friend_ids(user_a.id, [user_b.id]) == {user_b.id}
    assert await repo.get_accepted_friend_ids(user_b.id) == {user_a.id}

    # list_accepted_friends
    count_res = MagicMock()
    count_res.scalar_one.return_value = 1
    items_res = MagicMock()
    items_res.scalars.return_value.all.return_value = [friendship]
    session.execute.side_effect = [count_res, items_res]
    items, total = await repo.list_accepted_friends(user_a.id, page=1, page_size=20)
    assert total == 1
    assert items == [friendship]

    # list_pending_requests
    recv_res = MagicMock()
    recv_res.scalars.return_value.all.return_value = [friendship]
    sent_res = MagicMock()
    sent_res.scalars.return_value.all.return_value = []
    session.execute.side_effect = [recv_res, sent_res]
    received, sent = await repo.list_pending_requests(user_b.id)
    assert received == [friendship]
    assert sent == []

    # create, save, delete
    session.execute.side_effect = None
    created = await repo.create(requester_id=user_a.id, addressee_id=user_b.id, status="pending")
    assert created.status == "pending"
    saved = await repo.save(created)
    assert saved is created
    await repo.delete(created)
    session.delete.assert_awaited_once_with(created)
    session.commit.assert_not_awaited()


async def test_notification_repository_queries_and_read_updates() -> None:
    """Verify NotificationRepository creation, pagination, unread count, and bulk updates."""
    session = AsyncMock(spec=AsyncSession)
    session.add = MagicMock()
    repo = NotificationRepository(session)

    user_id = uuid4()
    actor_id = uuid4()
    entity_id = uuid4()

    created = await repo.create(
        user_id=user_id,
        actor_id=actor_id,
        notification_type="friend_request",
        entity_id=entity_id,
    )
    assert created.type == "friend_request"
    assert created.is_read is False

    one_res = MagicMock()
    one_res.scalar_one_or_none.return_value = created
    session.execute.return_value = one_res
    assert await repo.get_by_id(created.id) is created

    # list_for_user with unread_only=False (3 queries: total, unread, items)
    total_res = MagicMock()
    total_res.scalar_one.return_value = 3
    unread_res = MagicMock()
    unread_res.scalar_one.return_value = 2
    list_res = MagicMock()
    list_res.scalars.return_value.all.return_value = [created]
    session.execute.side_effect = [total_res, unread_res, list_res]
    items, total, unread_count = await repo.list_for_user(user_id, unread_only=False)
    assert items == [created]
    assert total == 3
    assert unread_count == 2

    # list_for_user with unread_only=True (2 queries: total == unread_count, items)
    session.execute.side_effect = [unread_res, list_res]
    items_u, total_u, unread_u = await repo.list_for_user(user_id, unread_only=True)
    assert items_u == [created]
    assert total_u == 2
    assert unread_u == 2

    # mark_as_read, mark_all_as_read, mark_entity_notifications_read
    updated = await repo.mark_as_read(created)
    assert updated.is_read is True

    update_res = MagicMock()
    update_res.rowcount = 4
    session.execute.side_effect = None
    session.execute.return_value = update_res
    assert await repo.mark_all_as_read(user_id) == 4
    assert (
        await repo.mark_entity_notifications_read(
            entity_id=entity_id,
            user_id=user_id,
            notification_type="friend_request",
        )
        == 4
    )
    session.commit.assert_not_awaited()


async def test_user_repository_invite_lookup_and_search_plus_friendship_sql() -> None:
    """Verify invite_code lookup, @discord user search, and EXISTS SQL in clip/collection repos."""
    session = AsyncMock(spec=AsyncSession)
    user_repo = UserRepository(session)
    user = _make_user("coldzera", "coldInvite99")

    assert await user_repo.get_by_invite_code("   ") is None
    one_res = MagicMock()
    one_res.scalar_one_or_none.return_value = user
    session.execute.return_value = one_res
    assert await user_repo.get_by_invite_code("coldInvite99") is user

    # Empty search short-circuits
    assert await user_repo.search_users("  ", exclude_user_id=uuid4()) == ([], 0)

    count_res = MagicMock()
    count_res.scalar_one.return_value = 1
    list_res = MagicMock()
    list_res.scalars.return_value.all.return_value = [user]
    session.execute.side_effect = [count_res, list_res]
    results, total = await user_repo.search_users("@cold_disc", exclude_user_id=uuid4())
    assert total == 1
    assert results == [user]
    search_sql = str(session.execute.call_args_list[-1][0][0].compile(dialect=postgresql.dialect()))
    assert "discord_username" in search_sql

    # Verify ClipRepository and CollectionRepository emit EXISTS (SELECT 1 FROM friendships ...)
    clip_repo = ClipRepository(session)
    coll_repo = CollectionRepository(session)
    viewer_id = uuid4()

    empty_count = MagicMock()
    empty_count.scalar_one.return_value = 0
    empty_items = MagicMock()
    empty_items.scalars.return_value.all.return_value = []
    session.execute.side_effect = [empty_count, empty_items, empty_count, empty_items]

    await clip_repo.list_clips(current_user_id=viewer_id)
    clip_sql = str(session.execute.call_args_list[-1][0][0].compile(dialect=postgresql.dialect()))
    assert "EXISTS" in clip_sql.upper()
    assert "friendships" in clip_sql

    await coll_repo.list_collections(current_user_id=viewer_id)
    coll_sql = str(session.execute.call_args_list[-1][0][0].compile(dialect=postgresql.dialect()))
    assert "EXISTS" in coll_sql.upper()
    assert "friendships" in coll_sql
    assert isinstance(
        Notification(user_id=viewer_id, actor_id=user.id, type="friend_request"), Notification
    )
