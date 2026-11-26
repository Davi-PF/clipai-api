"""Business logic service for collections CRUD and playlist clip management."""

from math import ceil
from uuid import UUID

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError
from app.models.collection import Collection, CollectionClip
from app.models.user import User
from app.repositories.clip_repository import ClipRepository
from app.repositories.collection_repository import CollectionRepository
from app.schemas.clip import resolve_owner_clip_metadata
from app.schemas.collection import (
    CollectionAddClipRequest,
    CollectionAddClipResponse,
    CollectionClipCardResponse,
    CollectionClipItemResponse,
    CollectionCreateRequest,
    CollectionDetailResponse,
    CollectionOwnerSummary,
    CollectionSummaryResponse,
    CollectionUpdateRequest,
)
from app.schemas.common import PaginatedResponse
from app.services.storage_service import StorageService


class CollectionService:
    """Orchestrates collection CRUD, visibility rules, and clip membership."""

    def __init__(
        self,
        session: AsyncSession,
        storage_service: StorageService,
    ) -> None:
        self._session = session
        self._storage = storage_service
        self._collection_repo = CollectionRepository(session)
        self._clip_repo = ClipRepository(session)

    async def list_collections(
        self,
        *,
        mine: bool = False,
        author: str | None = None,
        page: int = 1,
        page_size: int = 20,
        current_user: User | None = None,
    ) -> PaginatedResponse[CollectionSummaryResponse]:
        """List accessible collections with clip counts and preview thumbnails."""
        if mine and current_user is None:
            raise AppError(
                status_code=status.HTTP_401_UNAUTHORIZED,
                code="INVALID_TOKEN",
                message="Faça login para visualizar suas coleções.",
            )

        current_user_id = current_user.id if current_user else None
        collections, total = await self._collection_repo.list_collections(
            mine=mine,
            author_username=author,
            current_user_id=current_user_id,
            page=page,
            page_size=page_size,
        )
        metadata = await self._collection_repo.get_collections_preview_metadata(
            [coll.id for coll in collections],
            current_user_id=current_user_id,
        )

        items = [
            self._to_summary_response(
                coll,
                clips_count=metadata.get(coll.id, (0, []))[0],
                preview_keys=metadata.get(coll.id, (0, []))[1],
            )
            for coll in collections
        ]
        return PaginatedResponse.create(items=items, page=page, page_size=page_size, total=total)

    async def create_collection(
        self,
        payload: CollectionCreateRequest,
        current_user: User,
    ) -> CollectionSummaryResponse:
        """Create a new user collection."""
        collection = await self._collection_repo.create(
            user_id=current_user.id,
            title=payload.title,
            description=payload.description,
            privacy=payload.privacy,
            accent_color=payload.accent_color,
        )
        await self._session.commit()
        loaded = await self._collection_repo.get_by_id(collection.id)
        return self._to_summary_response(loaded or collection, clips_count=0, preview_keys=[])

    async def get_collection_detail(
        self,
        collection_id: UUID,
        *,
        page: int = 1,
        page_size: int = 20,
        current_user: User | None = None,
    ) -> CollectionDetailResponse:
        """Return collection metadata and its paginated ordered ready clips."""
        collection = await self._collection_repo.get_by_id(collection_id)
        if collection is None:
            raise self._not_found_error()

        if collection.privacy == "friends" and current_user is None:
            raise AppError(
                status_code=status.HTTP_401_UNAUTHORIZED,
                code="INVALID_TOKEN",
                message="Faça login para visualizar coleções restritas a amigos.",
            )

        current_user_id = current_user.id if current_user else None
        links, total = await self._collection_repo.list_collection_clips(
            collection.id,
            current_user_id=current_user_id,
            page=page,
            page_size=page_size,
        )

        clip_ids = [link.clip_id for link in links]
        liked_ids = (
            await self._clip_repo.get_user_liked_clip_ids(current_user.id, clip_ids)
            if current_user and clip_ids
            else set()
        )
        favorited_ids = (
            await self._clip_repo.get_user_favorited_clip_ids(current_user.id, clip_ids)
            if current_user and clip_ids
            else set()
        )

        clip_items = [
            self._to_clip_item_response(
                link,
                current_user_id=current_user_id,
                liked_ids=liked_ids,
                favorited_ids=favorited_ids,
            )
            for link in links
        ]

        total_pages = ceil(total / page_size) if total > 0 else 0
        return CollectionDetailResponse(
            id=collection.id,
            title=collection.title,
            description=collection.description,
            privacy=collection.privacy,  # type: ignore[arg-type]
            accent_color=collection.accent_color,
            clips_count=total,
            owner=CollectionOwnerSummary.model_validate(collection.owner),
            clips=clip_items,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
            created_at=collection.created_at,
            updated_at=collection.updated_at,
        )

    async def update_collection(
        self,
        collection_id: UUID,
        payload: CollectionUpdateRequest,
        current_user: User,
    ) -> CollectionSummaryResponse:
        """Update editable collection fields (owner only)."""
        collection = await self._get_owned_collection(collection_id, current_user)
        fields_set = payload.model_fields_set

        if "title" in fields_set and payload.title is not None:
            collection.title = payload.title
        if "description" in fields_set:
            collection.description = payload.description.strip() if payload.description else None
        if "privacy" in fields_set and payload.privacy is not None:
            collection.privacy = payload.privacy
        if "accent_color" in fields_set:
            collection.accent_color = payload.accent_color

        await self._session.commit()
        refreshed = await self._collection_repo.get_by_id(collection.id)
        metadata = await self._collection_repo.get_collections_preview_metadata(
            [collection.id],
            current_user_id=current_user.id,
        )
        clips_count, preview_keys = metadata.get(collection.id, (0, []))
        return self._to_summary_response(
            refreshed or collection,
            clips_count=clips_count,
            preview_keys=preview_keys,
        )

    async def delete_collection(self, collection_id: UUID, current_user: User) -> None:
        """Delete collection owned by the authenticated user."""
        collection = await self._get_owned_collection(collection_id, current_user)
        await self._collection_repo.delete(collection)
        await self._session.commit()

    async def add_clip_to_collection(
        self,
        collection_id: UUID,
        payload: CollectionAddClipRequest,
        current_user: User,
    ) -> CollectionAddClipResponse:
        """Idempotently add a ready clip to an owned collection."""
        collection = await self._get_owned_collection(collection_id, current_user)
        clip = await self._clip_repo.get_by_id(payload.clip_id)
        if clip is None:
            raise AppError(
                status_code=status.HTTP_404_NOT_FOUND,
                code="RESOURCE_NOT_FOUND",
                message="O clipe solicitado não foi encontrado.",
            )
        if clip.status != "ready":
            if clip.user_id != current_user.id:
                raise AppError(
                    status_code=status.HTTP_404_NOT_FOUND,
                    code="RESOURCE_NOT_FOUND",
                    message="O clipe solicitado não foi encontrado.",
                )
            raise AppError(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="BAD_REQUEST",
                message="Apenas clipes prontos podem ser adicionados a uma coleção.",
            )

        existing = await self._collection_repo.get_clip_link(collection.id, clip.id)
        if existing is not None:
            if payload.position is not None and existing.position != payload.position:
                existing.position = payload.position
                await self._session.commit()
            return CollectionAddClipResponse.model_validate(existing)

        position = (
            payload.position
            if payload.position is not None
            else await self._collection_repo.get_next_position(collection.id)
        )
        link = await self._collection_repo.add_clip(
            collection_id=collection.id,
            clip_id=clip.id,
            position=position,
        )
        await self._session.commit()
        return CollectionAddClipResponse.model_validate(link)

    async def remove_clip_from_collection(
        self,
        collection_id: UUID,
        clip_id: UUID,
        current_user: User,
    ) -> None:
        """Idempotently remove a clip from an owned collection."""
        collection = await self._get_owned_collection(collection_id, current_user)
        await self._collection_repo.remove_clip(collection_id=collection.id, clip_id=clip_id)
        await self._session.commit()

    async def _get_owned_collection(self, collection_id: UUID, current_user: User) -> Collection:
        collection = await self._collection_repo.get_by_id(collection_id)
        if collection is None:
            raise self._not_found_error()
        if collection.user_id != current_user.id:
            raise AppError(
                status_code=status.HTTP_403_FORBIDDEN,
                code="FORBIDDEN",
                message="Você não tem permissão para acessar ou alterar este recurso.",
            )
        return collection

    @staticmethod
    def _not_found_error() -> AppError:
        return AppError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="RESOURCE_NOT_FOUND",
            message="A coleção solicitada não foi encontrada.",
        )

    def _to_clip_item_response(
        self,
        link: CollectionClip,
        *,
        current_user_id: UUID | None,
        liked_ids: set[UUID],
        favorited_ids: set[UUID],
    ) -> CollectionClipItemResponse:
        file_size_bytes, technical_metadata = resolve_owner_clip_metadata(
            clip_user_id=link.clip.user_id,
            file_size_bytes=link.clip.file_size_bytes,
            technical_metadata=link.clip.technical_metadata,
            current_user_id=current_user_id,
        )
        return CollectionClipItemResponse(
            position=link.position,
            added_at=link.added_at,
            clip=CollectionClipCardResponse(
                id=link.clip.id,
                slug=link.clip.slug,
                title=link.clip.title,
                video_url=self._storage.build_public_url(link.clip.video_object_key),
                thumbnail_url=(
                    self._storage.build_public_url(link.clip.thumbnail_object_key)
                    if link.clip.thumbnail_object_key
                    else None
                ),
                file_size_bytes=file_size_bytes,
                duration_seconds=float(link.clip.duration_seconds),
                technical_metadata=technical_metadata,
                views_count=link.clip.views_count,
                likes_count=link.clip.likes_count,
                is_liked=link.clip.id in liked_ids,
                is_favorited=link.clip.id in favorited_ids,
                is_liked_by_me=link.clip.id in liked_ids,
                is_favorited_by_me=link.clip.id in favorited_ids,
            ),
        )

    def _to_summary_response(
        self,
        collection: Collection,
        *,
        clips_count: int,
        preview_keys: list[str],
    ) -> CollectionSummaryResponse:
        return CollectionSummaryResponse(
            id=collection.id,
            title=collection.title,
            description=collection.description,
            privacy=collection.privacy,  # type: ignore[arg-type]
            accent_color=collection.accent_color,
            clips_count=clips_count,
            preview_thumbnails=[self._storage.build_public_url(key) for key in preview_keys],
            owner=CollectionOwnerSummary.model_validate(collection.owner),
            created_at=collection.created_at,
            updated_at=collection.updated_at,
        )
