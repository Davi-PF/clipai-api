"""Unit tests for FriendshipService, NotificationService, InviteService, and register invite."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import AppError
from app.models.friendship import Friendship
from app.models.notification import Notification
from app.models.user import User
from app.schemas.auth import RegisterRequest
from app.services.auth_service import AuthService
from app.services.email_service import EmailService
from app.services.friendship_service import FriendshipService
from app.services.invite_service import InviteService
from app.services.notification_service import NotificationService
from app.services.storage_service import StorageService
from app.services.user_service import UserService


def _make_user(
    username: str,
    *,
    invite_code: str = "invCode12345",
    discord_id: str | None = None,
    discord_username: str | None = None,
    is_active: bool = True,
) -> User:
    now = datetime.now(UTC)
    return User(
        id=uuid4(),
        username=username,
        email=f"{username}@clipai.com.br",
        password_hash="hashed",
        display_name=username.upper(),
        invite_code=invite_code,
        discord_id=discord_id,
        discord_username=discord_username,
        is_email_verified=True,
        is_active=is_active,
        is_2fa_enabled=False,
        created_at=now,
        updated_at=now,
    )


async def test_friendship_service_send_accept_auto_accept_and_remove_guards() -> None:
    """Cover every happy path and error branch of FriendshipService."""
    session = AsyncMock(spec=AsyncSession)
    service = FriendshipService(session)
    service._friendship_repo = AsyncMock()
    service._user_repo = AsyncMock()
    service._notification_repo = AsyncMock()

    alice = _make_user("alice")
    bob = _make_user("bob")
    charlie = _make_user("charlie")
    now = datetime.now(UTC)

    # Self-request blocked -> 400
    with pytest.raises(AppError) as self_exc:
        await service.send_request(alice.id, alice)
    assert self_exc.value.status_code == 400

    # Missing or inactive target -> 404
    service._user_repo.get_by_id.return_value = None
    with pytest.raises(AppError) as missing_exc:
        await service.send_request(bob.id, alice)
    assert missing_exc.value.status_code == 404

    # Already friends -> 409
    service._user_repo.get_by_id.return_value = bob
    service._friendship_repo.get_between_users.return_value = Friendship(
        id=uuid4(),
        requester_id=alice.id,
        addressee_id=bob.id,
        status="accepted",
        requester=alice,
        addressee=bob,
        created_at=now,
        updated_at=now,
    )
    with pytest.raises(AppError) as dup_friend:
        await service.send_request(bob.id, alice)
    assert dup_friend.value.status_code == 409

    # Duplicate outgoing pending request -> 409
    service._friendship_repo.get_between_users.return_value = Friendship(
        id=uuid4(),
        requester_id=alice.id,
        addressee_id=bob.id,
        status="pending",
        requester=alice,
        addressee=bob,
        created_at=now,
        updated_at=now,
    )
    with pytest.raises(AppError) as dup_pending:
        await service.send_request(bob.id, alice)
    assert dup_pending.value.status_code == 409

    # Reverse pending request from Bob -> auto-accepted when Alice requests Bob
    reverse_req = Friendship(
        id=uuid4(),
        requester_id=bob.id,
        addressee_id=alice.id,
        status="pending",
        requester=bob,
        addressee=alice,
        created_at=now,
        updated_at=now,
    )
    service._friendship_repo.get_between_users.return_value = reverse_req
    auto_resp = await service.send_request(bob.id, alice)
    assert auto_resp.status == "accepted"
    assert auto_resp.auto_accepted is True
    assert reverse_req.status == "accepted"
    service._notification_repo.mark_entity_notifications_read.assert_awaited_once()
    service._notification_repo.create.assert_awaited_with(
        user_id=bob.id,
        actor_id=alice.id,
        notification_type="friend_accepted",
        entity_id=reverse_req.id,
    )

    # Fresh request creates pending record + friend_request notification
    new_f = Friendship(
        id=uuid4(),
        requester_id=alice.id,
        addressee_id=bob.id,
        status="pending",
        requester=alice,
        addressee=bob,
        created_at=now,
        updated_at=now,
    )
    service._friendship_repo.get_between_users.return_value = None
    service._friendship_repo.create.return_value = new_f
    sent_resp = await service.send_request(bob.id, alice)
    assert sent_resp.status == "pending"
    assert sent_resp.auto_accepted is False

    # Accept guards: 404, 403 (non-addressee), 409 (already accepted), 200 (happy path)
    service._friendship_repo.get_by_id.return_value = None
    with pytest.raises(AppError) as accept_404:
        await service.accept_request(uuid4(), bob)
    assert accept_404.value.status_code == 404

    service._friendship_repo.get_by_id.return_value = new_f
    with pytest.raises(AppError) as accept_403:
        await service.accept_request(new_f.id, alice)
    assert accept_403.value.status_code == 403

    accepted_resp = await service.accept_request(new_f.id, bob)
    assert accepted_resp.status == "accepted"
    assert accepted_resp.user.username == "alice"

    with pytest.raises(AppError) as accept_409:
        await service.accept_request(new_f.id, bob)
    assert accept_409.value.status_code == 409

    # Remove guards: 404, 403 (outsider), 204 (participant)
    service._friendship_repo.get_by_id.return_value = None
    with pytest.raises(AppError) as rem_404:
        await service.remove_or_cancel(uuid4(), alice)
    assert rem_404.value.status_code == 404

    service._friendship_repo.get_by_id.return_value = new_f
    with pytest.raises(AppError) as rem_403:
        await service.remove_or_cancel(new_f.id, charlie)
    assert rem_403.value.status_code == 403

    await service.remove_or_cancel(new_f.id, alice)
    service._friendship_repo.delete.assert_awaited_once_with(new_f)


async def test_notification_service_list_and_read_guards() -> None:
    """Verify NotificationService listing, single read ownership guard, and read-all."""
    session = AsyncMock(spec=AsyncSession)
    service = NotificationService(session)
    service._notification_repo = AsyncMock()

    alice = _make_user("alice")
    bob = _make_user("bob")
    notif = Notification(
        id=uuid4(),
        user_id=alice.id,
        actor_id=bob.id,
        type="friend_request",
        entity_id=uuid4(),
        is_read=False,
        created_at=datetime.now(UTC),
        actor=bob,
    )

    service._notification_repo.list_for_user.return_value = ([notif], 1, 1)
    listed = await service.list_notifications(alice)
    assert listed.total == 1
    assert listed.unread_count == 1
    assert listed.items[0].actor.username == "bob"

    service._notification_repo.get_by_id.return_value = None
    with pytest.raises(AppError) as missing:
        await service.mark_as_read(uuid4(), alice)
    assert missing.value.status_code == 404

    service._notification_repo.get_by_id.return_value = notif
    with pytest.raises(AppError) as forbidden:
        await service.mark_as_read(notif.id, bob)
    assert forbidden.value.status_code == 403

    async def _mark(item: Notification) -> Notification:
        item.is_read = True
        return item

    service._notification_repo.mark_as_read.side_effect = _mark
    read_resp = await service.mark_as_read(notif.id, alice)
    assert read_resp.is_read is True

    # Marking an already-read notification is idempotent without extra commit
    session.commit.reset_mock()
    read_again = await service.mark_as_read(notif.id, alice)
    assert read_again.is_read is True
    session.commit.assert_not_awaited()

    service._notification_repo.mark_all_as_read.return_value = 3
    all_resp = await service.mark_all_as_read(alice)
    assert all_resp.updated_count == 3


async def test_invite_service_preview_accept_idempotency_and_register_invite(
    test_settings: Settings,
) -> None:
    """Verify InviteService preview/accept/upgrade/idempotency and AuthService register invite."""
    session = AsyncMock(spec=AsyncSession)
    service = InviteService(session=session, settings=test_settings)
    service._user_repo = AsyncMock()
    service._friendship_repo = AsyncMock()
    service._notification_repo = AsyncMock()

    inviter = _make_user(
        "fallen",
        invite_code="fallenCode12",
        discord_id="999",
        discord_username="fallen_disc",
    )
    joiner = _make_user("fer", invite_code="ferCode12345")
    now = datetime.now(UTC)

    my_invite = service.get_my_invite(inviter)
    assert my_invite.invite_code == "fallenCode12"
    assert my_invite.invite_url == f"{test_settings.frontend_url}/invite/fallenCode12"

    # Unknown invite code -> 404
    service._user_repo.get_by_invite_code.return_value = None
    with pytest.raises(AppError) as not_found:
        await service.get_invite_preview("invalid")
    assert not_found.value.status_code == 404

    # Valid preview returns safe fields
    service._user_repo.get_by_invite_code.return_value = inviter
    preview = await service.get_invite_preview("fallenCode12")
    assert preview.username == "fallen"
    assert preview.discord_username == "fallen_disc"

    # Self-invite blocked -> 400
    with pytest.raises(AppError) as self_inv:
        await service.accept_invite("fallenCode12", inviter)
    assert self_inv.value.status_code == 400

    # New friendship created directly as accepted + notification sent
    created_f = Friendship(
        id=uuid4(),
        requester_id=inviter.id,
        addressee_id=joiner.id,
        status="accepted",
        created_at=now,
        updated_at=now,
    )
    service._friendship_repo.get_between_users.return_value = None
    service._friendship_repo.create.return_value = created_f
    acc_resp = await service.accept_invite("fallenCode12", joiner)
    assert acc_resp.already_friends is False
    assert acc_resp.friendship_id == created_f.id
    service._notification_repo.create.assert_awaited_with(
        user_id=inviter.id,
        actor_id=joiner.id,
        notification_type="friend_accepted",
        entity_id=created_f.id,
    )

    # Idempotent when already accepted (no extra notification)
    service._notification_repo.create.reset_mock()
    service._friendship_repo.get_between_users.return_value = created_f
    idem_resp = await service.accept_invite("fallenCode12", joiner)
    assert idem_resp.already_friends is True
    service._notification_repo.create.assert_not_awaited()

    # Pending friendship gets upgraded to accepted
    pending_f = Friendship(
        id=uuid4(),
        requester_id=joiner.id,
        addressee_id=inviter.id,
        status="pending",
        created_at=now,
        updated_at=now,
    )
    service._friendship_repo.get_between_users.return_value = pending_f
    service._friendship_repo.save.return_value = pending_f
    up_resp = await service.accept_invite("fallenCode12", joiner)
    assert up_resp.already_friends is False
    assert pending_f.status == "accepted"
    service._notification_repo.mark_entity_notifications_read.assert_awaited_once()

    # AuthService.register with valid invite_code creates friendship + notification
    email_service = MagicMock(spec=EmailService)
    email_service.send_verification_email = AsyncMock()
    auth_service = AuthService(
        session=session,
        settings=test_settings,
        email_service=email_service,
    )
    auth_service._user_repo = AsyncMock()
    auth_service._friendship_repo = AsyncMock()
    auth_service._notification_repo = AsyncMock()

    auth_service._user_repo.get_by_email.return_value = None
    auth_service._user_repo.get_by_username.return_value = None
    auth_service._user_repo.create.return_value = joiner
    auth_service._user_repo.get_by_invite_code.return_value = inviter
    auth_service._friendship_repo.create.return_value = created_f

    reg_resp = await auth_service.register(
        RegisterRequest(
            username="fer",
            email="fer@clipai.com.br",
            password="StrongPassword!2026",
            invite_code="  fallenCode12  ",
        )
    )
    assert reg_resp.user.username == "fer"
    auth_service._friendship_repo.create.assert_awaited_once_with(
        requester_id=inviter.id,
        addressee_id=joiner.id,
        status="accepted",
    )
    auth_service._notification_repo.create.assert_awaited_once_with(
        user_id=inviter.id,
        actor_id=joiner.id,
        notification_type="friend_accepted",
        entity_id=created_f.id,
    )

    # UserService.search_users enriches results with friendship_status
    storage = MagicMock(spec=StorageService)
    user_service = UserService(session=session, settings=test_settings, storage_service=storage)
    user_service._user_repo = AsyncMock()
    user_service._friendship_repo = AsyncMock()
    user_service._user_repo.search_users.return_value = ([inviter], 1)
    user_service._friendship_repo.get_between_user_and_many.return_value = {inviter.id: created_f}
    search_resp = await user_service.search_users("@fallen_disc", current_user=joiner)
    assert search_resp.total == 1
    assert search_resp.items[0].friendship_status == "accepted"
    assert search_resp.items[0].friendship_id == created_f.id
