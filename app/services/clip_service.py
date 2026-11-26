"""Business logic service for games, tags, presigned clip uploads, and clip CRUD."""

import secrets
from decimal import Decimal
from uuid import UUID, uuid4

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import AppError
from app.models.clip import Clip
from app.models.user import User
from app.repositories.clip_repository import ClipRepository
from app.repositories.collection_repository import CollectionRepository
from app.repositories.game_repository import GameRepository
from app.repositories.tag_repository import TagRepository
from app.schemas.clip import (
    ClipAuthorSummary,
    ClipDetailResponse,
    ClipFavoriteResponse,
    ClipGameSummary,
    ClipLikeResponse,
    ClipSummaryResponse,
    ClipUpdateRequest,
    ClipViewResponse,
    PresignedUploadTarget,
    TechnicalMetadataSchema,
    UploadUrlRequest,
    UploadUrlResponse,
)
from app.schemas.common import PaginatedResponse
from app.schemas.game import GameResponse, TagListResponse, TagResponse
from app.services.storage_service import StorageService

_ALLOWED_VIDEO_MIMES = frozenset({"video/mp4", "video/webm"})
_ALLOWED_THUMBNAIL_MIMES = frozenset({"image/jpeg", "image/png", "image/webp"})
_MAX_PENDING_UPLOADS_PER_USER = 3


class ClipService:
    """Orchestrates games/tags discovery, direct-to-R2 clip uploads, and clip CRUD."""

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        storage_service: StorageService,
    ) -> None:
        self._session = session
        self._settings = settings
        self._storage = storage_service
        self._game_repo = GameRepository(session)
        self._tag_repo = TagRepository(session)
        self._clip_repo = ClipRepository(session)
        self._collection_repo = CollectionRepository(session)

    async def list_games(
        self,
        *,
        q: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> PaginatedResponse[GameResponse]:
        """Return paginated list of supported games."""
        games, total = await self._game_repo.list_games(q=q, page=page, page_size=page_size)
        return PaginatedResponse.create(
            items=[GameResponse.model_validate(game) for game in games],
            page=page,
            page_size=page_size,
            total=total,
        )

    async def list_tags(self, *, q: str | None = None, limit: int = 20) -> TagListResponse:
        """Return popular or filtered tags."""
        tags = await self._tag_repo.list_tags(q=q, limit=limit)
        return TagListResponse(items=[TagResponse.model_validate(tag) for tag in tags])

    async def create_upload_url(
        self,
        payload: UploadUrlRequest,
        current_user: User,
    ) -> UploadUrlResponse:
        """Validate upload constraints, persist pending_upload clip, and sign R2 PUT URLs."""
        self._validate_upload_limits(payload)
        await self._ensure_pending_quota(current_user.id)
        await self._validate_game_exists(payload.game_id)

        clip_id = uuid4()
        slug = await self._generate_unique_slug()
        video_key = self._storage.build_video_key(current_user.id, clip_id, payload.video_mime_type)
        thumb_key = (
            self._storage.build_thumbnail_key(
                current_user.id,
                clip_id,
                payload.thumbnail_mime_type,
            )
            if payload.thumbnail_mime_type
            else None
        )

        tags = await self._tag_repo.get_or_create_many(payload.tags)
        tech_meta = payload.technical_metadata.model_dump()
        tech_meta["mime_type"] = payload.video_mime_type
        tech_meta["container"] = "mp4" if payload.video_mime_type == "video/mp4" else "webm"

        await self._clip_repo.create(
            clip_id=clip_id,
            slug=slug,
            user_id=current_user.id,
            game_id=payload.game_id,
            title=payload.title,
            description=payload.description,
            privacy=payload.privacy,
            video_object_key=video_key,
            thumbnail_object_key=thumb_key,
            file_size_bytes=payload.video_size_bytes,
            duration_seconds=Decimal(str(round(payload.duration_seconds, 2))),
            mime_type=payload.video_mime_type,
            technical_metadata=tech_meta,
            tags=tags,
        )
        await self._session.commit()

        expires_in = self._settings.r2_presigned_url_expire_seconds
        video_url = await self._storage.generate_presigned_upload_url(
            video_key,
            payload.video_mime_type,
            expires_in=expires_in,
        )
        thumb_target: PresignedUploadTarget | None = None
        if thumb_key and payload.thumbnail_mime_type:
            thumb_url = await self._storage.generate_presigned_upload_url(
                thumb_key,
                payload.thumbnail_mime_type,
                expires_in=expires_in,
            )
            thumb_target = PresignedUploadTarget(
                url=thumb_url,
                headers={"Content-Type": payload.thumbnail_mime_type},
            )

        return UploadUrlResponse(
            clip_id=clip_id,
            slug=slug,
            status="pending_upload",
            expires_in=expires_in,
            video_upload=PresignedUploadTarget(
                url=video_url,
                headers={"Content-Type": payload.video_mime_type},
            ),
            thumbnail_upload=thumb_target,
        )

    async def confirm_upload(self, clip_id: UUID, current_user: User) -> ClipDetailResponse:
        """Verify uploaded objects exist in R2 via HeadObject and mark clip as ready."""
        clip = await self._get_owned_clip(clip_id, current_user)
        if clip.status != "pending_upload":
            raise AppError(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="BAD_REQUEST",
                message="Este clipe não está aguardando confirmação de envio.",
            )

        try:
            video_meta = await self._storage.verify_object_exists(clip.video_object_key)
            if (
                video_meta.content_length <= 0
                or video_meta.content_length > self._settings.max_video_size_bytes
            ):
                raise AppError(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    code="UPLOAD_VERIFICATION_FAILED",
                    message=(
                        "O arquivo de vídeo enviado possui tamanho inválido ou excede 100 MB."
                    ),
                )

            if clip.thumbnail_object_key:
                thumb_meta = await self._storage.verify_object_exists(clip.thumbnail_object_key)
                if (
                    thumb_meta.content_length <= 0
                    or thumb_meta.content_length > self._settings.max_thumbnail_size_bytes
                ):
                    raise AppError(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        code="UPLOAD_VERIFICATION_FAILED",
                        message="A thumbnail enviada possui tamanho inválido ou excede 5 MB.",
                    )
        except AppError:
            clip.status = "failed"
            await self._session.commit()
            await self._storage.delete_objects(
                [clip.video_object_key, clip.thumbnail_object_key or ""]
            )
            raise

        clip.file_size_bytes = video_meta.content_length
        clip.status = "ready"
        await self._session.commit()
        refreshed = await self._clip_repo.get_by_id(clip.id)
        return self._to_detail_response(refreshed or clip)

    async def cancel_upload(self, clip_id: UUID, current_user: User) -> None:
        """Cancel an unconfirmed upload, deleting the DB row and any R2 objects."""
        clip = await self._get_owned_clip(clip_id, current_user)
        if clip.status == "ready":
            raise AppError(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="BAD_REQUEST",
                message="Não é possível cancelar um envio já finalizado.",
            )
        await self._delete_clip_and_objects(clip)

    async def list_clips(
        self,
        *,
        q: str | None = None,
        game: str | None = None,
        game_id: UUID | None = None,
        game_slug: str | None = None,
        tag: str | None = None,
        author: str | None = None,
        mine: bool = False,
        favorited: bool = False,
        collection_id: UUID | None = None,
        sort: str = "recent",
        page: int = 1,
        page_size: int = 20,
        current_user: User | None = None,
    ) -> PaginatedResponse[ClipSummaryResponse]:
        """List ready clips applying search, filters, privacy, and interaction state."""
        if (mine or favorited) and current_user is None:
            raise AppError(
                status_code=status.HTTP_401_UNAUTHORIZED,
                code="INVALID_TOKEN",
                message="Faça login para visualizar seus clipes ou favoritos.",
            )

        if collection_id is not None:
            await self._ensure_collection_readable(collection_id, current_user)

        resolved_game_id, resolved_game_slug = self._resolve_game_filter(
            game=game,
            game_id=game_id,
            game_slug=game_slug,
        )
        clips, total = await self._clip_repo.list_clips(
            q=q,
            game_id=resolved_game_id,
            game_slug=resolved_game_slug,
            tag_slug=tag,
            author_username=author,
            mine=mine,
            favorited_by_user_id=current_user.id if (favorited and current_user) else None,
            collection_id=collection_id,
            current_user_id=current_user.id if current_user else None,
            sort=sort,
            page=page,
            page_size=page_size,
        )
        liked_ids, favorited_ids = await self._load_user_interaction_sets(clips, current_user)
        return PaginatedResponse.create(
            items=[
                self._to_summary_response(
                    clip,
                    is_liked=clip.id in liked_ids,
                    is_favorited=clip.id in favorited_ids,
                )
                for clip in clips
            ],
            page=page,
            page_size=page_size,
            total=total,
        )

    async def get_clip_detail(
        self,
        identifier: str,
        current_user: User | None = None,
    ) -> ClipDetailResponse:
        """Return full clip details by slug or UUID respecting privacy rules."""
        clip = await self._clip_repo.get_by_identifier(identifier)
        if clip is None:
            raise self._clip_not_found_error()

        is_owner = current_user is not None and clip.user_id == current_user.id
        if clip.status != "ready" and not is_owner:
            raise self._clip_not_found_error()

        if clip.privacy == "friends" and current_user is None:
            raise AppError(
                status_code=status.HTTP_401_UNAUTHORIZED,
                code="INVALID_TOKEN",
                message="Faça login para visualizar clipes restritos a amigos.",
            )

        liked_ids, favorited_ids = await self._load_user_interaction_sets([clip], current_user)
        return self._to_detail_response(
            clip,
            is_liked=clip.id in liked_ids,
            is_favorited=clip.id in favorited_ids,
        )

    async def like_clip(self, clip_id: UUID, current_user: User) -> ClipLikeResponse:
        """Idempotently like a ready accessible clip."""
        clip = await self._get_readable_ready_clip(clip_id, current_user)
        likes_count = await self._clip_repo.like_clip(user_id=current_user.id, clip_id=clip.id)
        await self._session.commit()
        return ClipLikeResponse(
            clip_id=clip.id,
            liked=True,
            is_liked_by_me=True,
            likes_count=likes_count,
        )

    async def unlike_clip(self, clip_id: UUID, current_user: User) -> ClipLikeResponse:
        """Idempotently remove a like from a ready accessible clip."""
        clip = await self._get_readable_ready_clip(clip_id, current_user)
        likes_count = await self._clip_repo.unlike_clip(user_id=current_user.id, clip_id=clip.id)
        await self._session.commit()
        return ClipLikeResponse(
            clip_id=clip.id,
            liked=False,
            is_liked_by_me=False,
            likes_count=likes_count,
        )

    async def favorite_clip(self, clip_id: UUID, current_user: User) -> ClipFavoriteResponse:
        """Idempotently favorite a ready accessible clip."""
        clip = await self._get_readable_ready_clip(clip_id, current_user)
        favorites_count = await self._clip_repo.favorite_clip(
            user_id=current_user.id,
            clip_id=clip.id,
        )
        await self._session.commit()
        return ClipFavoriteResponse(
            clip_id=clip.id,
            favorited=True,
            is_favorited_by_me=True,
            favorites_count=favorites_count,
        )

    async def unfavorite_clip(self, clip_id: UUID, current_user: User) -> ClipFavoriteResponse:
        """Idempotently remove a clip from user favorites."""
        clip = await self._get_readable_ready_clip(clip_id, current_user)
        favorites_count = await self._clip_repo.unfavorite_clip(
            user_id=current_user.id,
            clip_id=clip.id,
        )
        await self._session.commit()
        return ClipFavoriteResponse(
            clip_id=clip.id,
            favorited=False,
            is_favorited_by_me=False,
            favorites_count=favorites_count,
        )

    async def register_view(
        self,
        clip_id: UUID,
        current_user: User | None = None,
    ) -> ClipViewResponse:
        """Atomically increment views_count for a ready accessible clip."""
        clip = await self._get_readable_ready_clip(clip_id, current_user)
        views_count = await self._clip_repo.increment_views(clip.id)
        await self._session.commit()
        return ClipViewResponse(clip_id=clip.id, views_count=views_count)

    async def update_clip(
        self,
        clip_id: UUID,
        payload: ClipUpdateRequest,
        current_user: User,
    ) -> ClipDetailResponse:
        """Update editable clip fields owned by the authenticated user."""
        clip = await self._get_owned_clip(clip_id, current_user)
        fields_set = payload.model_fields_set

        if "title" in fields_set and payload.title is not None:
            clip.title = payload.title
        if "description" in fields_set:
            clip.description = payload.description.strip() if payload.description else None
        if "game_id" in fields_set:
            await self._validate_game_exists(payload.game_id)
            clip.game_id = payload.game_id
        if "privacy" in fields_set and payload.privacy is not None:
            clip.privacy = payload.privacy
        if "tags" in fields_set and payload.tags is not None:
            tags = await self._tag_repo.get_or_create_many(payload.tags)
            await self._clip_repo.replace_tags(clip, tags)

        await self._session.commit()
        refreshed = await self._clip_repo.get_by_id(clip.id)
        return self._to_detail_response(refreshed or clip)

    async def delete_clip(self, clip_id: UUID, current_user: User) -> None:
        """Delete clip from PostgreSQL and remove media objects from Cloudflare R2."""
        clip = await self._get_owned_clip(clip_id, current_user)
        await self._delete_clip_and_objects(clip)

    async def _delete_clip_and_objects(self, clip: Clip) -> None:
        """Remove a clip record from PostgreSQL and delete its R2 objects."""
        object_keys = [clip.video_object_key]
        if clip.thumbnail_object_key:
            object_keys.append(clip.thumbnail_object_key)

        await self._clip_repo.delete(clip)
        await self._session.commit()
        await self._storage.delete_objects(object_keys)

    def _validate_upload_limits(self, payload: UploadUrlRequest) -> None:
        """Validate video and optional thumbnail MIME types and byte limits."""
        if payload.video_mime_type not in _ALLOWED_VIDEO_MIMES:
            raise AppError(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                code="UNSUPPORTED_MEDIA_TYPE",
                message="Formato de arquivo não suportado. Use apenas MP4 ou WebM.",
            )
        if payload.video_size_bytes > self._settings.max_video_size_bytes:
            raise AppError(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                code="FILE_TOO_LARGE",
                message="O arquivo de vídeo não pode ultrapassar 100 MB.",
            )

        if payload.thumbnail_mime_type is not None:
            if payload.thumbnail_mime_type not in _ALLOWED_THUMBNAIL_MIMES:
                raise AppError(
                    status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                    code="UNSUPPORTED_MEDIA_TYPE",
                    message="Formato de thumbnail não suportado. Use JPEG, PNG ou WebP.",
                )
            if payload.thumbnail_size_bytes is None:
                raise AppError(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    code="VALIDATION_ERROR",
                    message="Informe o tamanho da thumbnail em bytes.",
                )
            if payload.thumbnail_size_bytes > self._settings.max_thumbnail_size_bytes:
                raise AppError(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    code="FILE_TOO_LARGE",
                    message="A imagem de thumbnail não pode ultrapassar 5 MB.",
                )

    async def _ensure_pending_quota(self, user_id: UUID) -> None:
        """Limit concurrent pending_upload records per user to mitigate R2 abuse."""
        pending = await self._clip_repo.count_pending_by_user(user_id)
        if pending >= _MAX_PENDING_UPLOADS_PER_USER:
            raise AppError(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                code="RATE_LIMIT_EXCEEDED",
                message=(
                    "Você atingiu o limite de envios pendentes simultâneos. "
                    "Conclua ou aguarde antes de iniciar um novo upload."
                ),
            )

    async def _validate_game_exists(self, game_id: UUID | None) -> None:
        """Ensure referenced game exists when game_id is supplied."""
        if game_id is None:
            return
        if await self._game_repo.get_by_id(game_id) is None:
            raise AppError(
                status_code=status.HTTP_404_NOT_FOUND,
                code="RESOURCE_NOT_FOUND",
                message="O jogo selecionado não foi encontrado.",
            )

    async def _generate_unique_slug(self) -> str:
        """Generate a short URL-safe unique slug for /c/[slug] sharing."""
        for _ in range(5):
            candidate = secrets.token_urlsafe(8)
            if await self._clip_repo.get_by_slug(candidate) is None:
                return candidate
        return secrets.token_urlsafe(10)

    async def _get_owned_clip(self, clip_id: UUID, current_user: User) -> Clip:
        """Fetch clip by UUID and enforce ownership."""
        clip = await self._clip_repo.get_by_id(clip_id)
        if clip is None:
            raise self._clip_not_found_error()
        if clip.user_id != current_user.id:
            raise AppError(
                status_code=status.HTTP_403_FORBIDDEN,
                code="FORBIDDEN",
                message="Você não tem permissão para acessar ou alterar este recurso.",
            )
        return clip

    @staticmethod
    def _resolve_game_filter(
        *,
        game: str | None,
        game_id: UUID | None,
        game_slug: str | None,
    ) -> tuple[UUID | None, str | None]:
        """Support unified 'game' query param (UUID or slug) plus explicit aliases."""
        if game and game.strip():
            raw_game = game.strip()
            try:
                return UUID(raw_game), game_slug
            except ValueError:
                return game_id, raw_game
        return game_id, game_slug

    @staticmethod
    def _clip_not_found_error() -> AppError:
        return AppError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="RESOURCE_NOT_FOUND",
            message="O clipe solicitado não foi encontrado.",
        )

    async def _get_readable_ready_clip(
        self,
        clip_id: UUID,
        current_user: User | None,
    ) -> Clip:
        """Fetch ready clip by UUID and verify privacy access."""
        clip = await self._clip_repo.get_by_id(clip_id)
        if clip is None or clip.status != "ready":
            raise self._clip_not_found_error()
        if clip.privacy == "friends" and current_user is None:
            raise AppError(
                status_code=status.HTTP_401_UNAUTHORIZED,
                code="INVALID_TOKEN",
                message="Faça login para interagir com clipes restritos a amigos.",
            )
        return clip

    async def _ensure_collection_readable(
        self,
        collection_id: UUID,
        current_user: User | None,
    ) -> None:
        """Verify collection exists and requester can view its clips."""
        collection = await self._collection_repo.get_by_id(collection_id)
        if collection is None:
            raise AppError(
                status_code=status.HTTP_404_NOT_FOUND,
                code="RESOURCE_NOT_FOUND",
                message="A coleção solicitada não foi encontrada.",
            )
        if collection.privacy == "friends" and current_user is None:
            raise AppError(
                status_code=status.HTTP_401_UNAUTHORIZED,
                code="INVALID_TOKEN",
                message="Faça login para visualizar coleções restritas a amigos.",
            )

    async def _load_user_interaction_sets(
        self,
        clips: list[Clip],
        current_user: User | None,
    ) -> tuple[set[UUID], set[UUID]]:
        """Batch-load liked and favorited clip ID sets for an authenticated user."""
        if current_user is None or not clips:
            return set(), set()
        clip_ids = [clip.id for clip in clips]
        liked_ids = await self._clip_repo.get_user_liked_clip_ids(current_user.id, clip_ids)
        favorited_ids = await self._clip_repo.get_user_favorited_clip_ids(
            current_user.id,
            clip_ids,
        )
        return liked_ids, favorited_ids

    def _to_summary_response(
        self,
        clip: Clip,
        *,
        is_liked: bool = False,
        is_favorited: bool = False,
    ) -> ClipSummaryResponse:
        return ClipSummaryResponse(
            id=clip.id,
            slug=clip.slug,
            title=clip.title,
            privacy=clip.privacy,  # type: ignore[arg-type]
            video_url=self._storage.build_public_url(clip.video_object_key),
            thumbnail_url=(
                self._storage.build_public_url(clip.thumbnail_object_key)
                if clip.thumbnail_object_key
                else None
            ),
            duration_seconds=float(clip.duration_seconds),
            views_count=clip.views_count,
            likes_count=clip.likes_count,
            favorites_count=clip.favorites_count,
            is_liked=is_liked,
            is_favorited=is_favorited,
            is_liked_by_me=is_liked,
            is_favorited_by_me=is_favorited,
            author=ClipAuthorSummary.model_validate(clip.author),
            game=ClipGameSummary.model_validate(clip.game) if clip.game else None,
            tags=[tag.slug for tag in clip.tags],
            created_at=clip.created_at,
        )

    def _to_detail_response(
        self,
        clip: Clip,
        *,
        is_liked: bool = False,
        is_favorited: bool = False,
    ) -> ClipDetailResponse:
        return ClipDetailResponse(
            id=clip.id,
            slug=clip.slug,
            title=clip.title,
            description=clip.description,
            status=clip.status,  # type: ignore[arg-type]
            privacy=clip.privacy,  # type: ignore[arg-type]
            video_url=self._storage.build_public_url(clip.video_object_key),
            thumbnail_url=(
                self._storage.build_public_url(clip.thumbnail_object_key)
                if clip.thumbnail_object_key
                else None
            ),
            file_size_bytes=clip.file_size_bytes,
            duration_seconds=float(clip.duration_seconds),
            mime_type=clip.mime_type,
            technical_metadata=TechnicalMetadataSchema.model_validate(
                clip.technical_metadata or {}
            ),
            views_count=clip.views_count,
            likes_count=clip.likes_count,
            favorites_count=clip.favorites_count,
            is_liked=is_liked,
            is_favorited=is_favorited,
            is_liked_by_me=is_liked,
            is_favorited_by_me=is_favorited,
            author=ClipAuthorSummary.model_validate(clip.author),
            game=ClipGameSummary.model_validate(clip.game) if clip.game else None,
            tags=[tag.slug for tag in clip.tags],
            created_at=clip.created_at,
            updated_at=clip.updated_at,
        )
