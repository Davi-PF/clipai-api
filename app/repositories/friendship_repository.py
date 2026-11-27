"""SQLAlchemy 2.0 async repository for user friendships."""

from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.friendship import Friendship


class FriendshipRepository:
    """Data access layer for the friendships table."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, friendship_id: UUID) -> Friendship | None:
        """Fetch a friendship record by UUID with requester and addressee loaded."""
        stmt = (
            select(Friendship)
            .options(
                selectinload(Friendship.requester),
                selectinload(Friendship.addressee),
            )
            .where(Friendship.id == friendship_id)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_between_users(
        self,
        user_a_id: UUID,
        user_b_id: UUID,
    ) -> Friendship | None:
        """Fetch any friendship (pending or accepted) between two users in either direction."""
        stmt = (
            select(Friendship)
            .options(
                selectinload(Friendship.requester),
                selectinload(Friendship.addressee),
            )
            .where(
                or_(
                    and_(
                        Friendship.requester_id == user_a_id,
                        Friendship.addressee_id == user_b_id,
                    ),
                    and_(
                        Friendship.requester_id == user_b_id,
                        Friendship.addressee_id == user_a_id,
                    ),
                )
            )
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_between_user_and_many(
        self,
        user_id: UUID,
        other_user_ids: list[UUID],
    ) -> dict[UUID, Friendship]:
        """Batch-fetch friendships between user_id and a list of other users (zero N+1)."""
        if not other_user_ids:
            return {}

        stmt = select(Friendship).where(
            or_(
                and_(
                    Friendship.requester_id == user_id,
                    Friendship.addressee_id.in_(other_user_ids),
                ),
                and_(
                    Friendship.addressee_id == user_id,
                    Friendship.requester_id.in_(other_user_ids),
                ),
            )
        )
        rows = list((await self._session.execute(stmt)).scalars().all())
        mapping: dict[UUID, Friendship] = {}
        for friendship in rows:
            other_id = (
                friendship.addressee_id
                if friendship.requester_id == user_id
                else friendship.requester_id
            )
            mapping[other_id] = friendship
        return mapping

    async def are_friends(self, user_a_id: UUID, user_b_id: UUID) -> bool:
        """Return True when an accepted friendship exists between user_a_id and user_b_id."""
        if user_a_id == user_b_id:
            return False

        stmt = (
            select(Friendship.id)
            .where(
                Friendship.status == "accepted",
                or_(
                    and_(
                        Friendship.requester_id == user_a_id,
                        Friendship.addressee_id == user_b_id,
                    ),
                    and_(
                        Friendship.requester_id == user_b_id,
                        Friendship.addressee_id == user_a_id,
                    ),
                ),
            )
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def get_accepted_friend_ids(
        self,
        user_id: UUID,
        candidate_ids: list[UUID] | None = None,
    ) -> set[UUID]:
        """Return set of accepted friend UUIDs for user_id (optionally filtered to candidates)."""
        if candidate_ids is not None and not candidate_ids:
            return set()

        conditions = [
            Friendship.status == "accepted",
            or_(
                Friendship.requester_id == user_id,
                Friendship.addressee_id == user_id,
            ),
        ]
        if candidate_ids is not None:
            conditions.append(
                or_(
                    and_(
                        Friendship.requester_id == user_id,
                        Friendship.addressee_id.in_(candidate_ids),
                    ),
                    and_(
                        Friendship.addressee_id == user_id,
                        Friendship.requester_id.in_(candidate_ids),
                    ),
                )
            )

        stmt = select(Friendship.requester_id, Friendship.addressee_id).where(*conditions)
        rows = (await self._session.execute(stmt)).all()
        return {
            addressee_id if requester_id == user_id else requester_id
            for requester_id, addressee_id in rows
        }

    async def list_accepted_friends(
        self,
        user_id: UUID,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Friendship], int]:
        """List paginated accepted friendships for a user with both user records loaded."""
        base_filter = and_(
            Friendship.status == "accepted",
            or_(
                Friendship.requester_id == user_id,
                Friendship.addressee_id == user_id,
            ),
        )
        count_stmt = select(func.count(Friendship.id)).where(base_filter)
        total = int((await self._session.execute(count_stmt)).scalar_one())

        offset = (page - 1) * page_size
        items_stmt = (
            select(Friendship)
            .options(
                selectinload(Friendship.requester),
                selectinload(Friendship.addressee),
            )
            .where(base_filter)
            .order_by(Friendship.updated_at.desc())
            .offset(offset)
            .limit(page_size)
        )
        items = list((await self._session.execute(items_stmt)).scalars().all())
        return items, total

    async def list_pending_requests(
        self,
        user_id: UUID,
    ) -> tuple[list[Friendship], list[Friendship]]:
        """Return (received, sent) pending friend requests for a user."""
        received_stmt = (
            select(Friendship)
            .options(
                selectinload(Friendship.requester),
                selectinload(Friendship.addressee),
            )
            .where(
                Friendship.addressee_id == user_id,
                Friendship.status == "pending",
            )
            .order_by(Friendship.created_at.desc())
        )
        sent_stmt = (
            select(Friendship)
            .options(
                selectinload(Friendship.requester),
                selectinload(Friendship.addressee),
            )
            .where(
                Friendship.requester_id == user_id,
                Friendship.status == "pending",
            )
            .order_by(Friendship.created_at.desc())
        )
        received = list((await self._session.execute(received_stmt)).scalars().all())
        sent = list((await self._session.execute(sent_stmt)).scalars().all())
        return received, sent

    async def create(
        self,
        *,
        requester_id: UUID,
        addressee_id: UUID,
        status: str = "pending",
    ) -> Friendship:
        """Insert a new friendship record and flush to assign database defaults."""
        friendship = Friendship(
            requester_id=requester_id,
            addressee_id=addressee_id,
            status=status,
        )
        self._session.add(friendship)
        await self._session.flush()
        return friendship

    async def save(self, friendship: Friendship) -> Friendship:
        """Flush in-memory updates on a Friendship entity."""
        self._session.add(friendship)
        await self._session.flush()
        return friendship

    async def delete(self, friendship: Friendship) -> None:
        """Delete a friendship or pending request record."""
        await self._session.delete(friendship)
        await self._session.flush()
