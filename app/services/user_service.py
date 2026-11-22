"""Business logic service for user profiles and custom R2 avatar uploads."""

import re
from uuid import uuid4

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import AppError
from app.models.user import User
from app.repositories.user_repository import UserRepository
from app.schemas.user import (
    AvatarConfirmRequest,
    AvatarPresignedTarget,
    AvatarUploadUrlRequest,
    AvatarUploadUrlResponse,
    UserMeResponse,
    UserPublicResponse,
    UserPublicStats,
    UserUpdateRequest,
    resolve_display_name,
    resolve_user_avatar_url,
)
from app.services.storage_service import StorageService

_ALLOWED_AVATAR_MIMES = frozenset({"image/jpeg", "image/png", "image/webp"})
_AVATAR_KEY_SUFFIX_PATTERN = re.compile(r"^avatar-[0-9a-fA-F-]{36}\.(jpg|png|webp)$")


class UserService:
    """Orchestrates user profile updates, R2 avatar uploads, and public profile views."""

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        storage_service: StorageService,
    ) -> None:
        self._session = session
        self._settings = settings
        self._storage = storage_service
        self._user_repo = UserRepository(session)

    async def update_profile(
        self,
        current_user: User,
        payload: UserUpdateRequest,
    ) -> UserMeResponse:
        """Update display_name, bio, and avatar_source for the authenticated user."""
        fields_set = payload.model_fields_set

        if "display_name" in fields_set:
            current_user.display_name = payload.display_name
        if "bio" in fields_set:
            current_user.bio = payload.bio
        if "avatar_source" in fields_set and payload.avatar_source is not None:
            self._validate_avatar_source_transition(current_user, payload.avatar_source)
            current_user.avatar_source = payload.avatar_source
            current_user.avatar_url = resolve_user_avatar_url(
                current_user,
                public_base_url=self._settings.r2_public_base_url,
            )

        await self._user_repo.save(current_user)
        await self._session.commit()
        return UserMeResponse.model_validate(current_user)

    async def create_avatar_upload_url(
        self,
        current_user: User,
        payload: AvatarUploadUrlRequest,
    ) -> AvatarUploadUrlResponse:
        """Validate avatar image constraints and sign a Cloudflare R2 Presigned PUT URL."""
        if payload.mime_type not in _ALLOWED_AVATAR_MIMES:
            raise AppError(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                code="UNSUPPORTED_MEDIA_TYPE",
                message="Formato de imagem de avatar não suportado. Use JPEG, PNG ou WebP.",
            )
        if payload.size_bytes > self._settings.max_avatar_size_bytes:
            raise AppError(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                code="FILE_TOO_LARGE",
                message="A imagem de avatar não pode ultrapassar 5 MB.",
            )

        avatar_key = self._storage.build_avatar_key(
            current_user.id,
            uuid4(),
            payload.mime_type,
        )
        expires_in = self._settings.r2_presigned_url_expire_seconds
        upload_url = await self._storage.generate_presigned_upload_url(
            avatar_key,
            payload.mime_type,
            expires_in=expires_in,
        )
        return AvatarUploadUrlResponse(
            avatar_key=avatar_key,
            object_key=avatar_key,
            upload_url=upload_url,
            expires_in=expires_in,
            avatar_upload=AvatarPresignedTarget(
                url=upload_url,
                headers={"Content-Type": payload.mime_type},
            ),
        )

    async def confirm_avatar_upload(
        self,
        current_user: User,
        payload: AvatarConfirmRequest,
    ) -> UserMeResponse:
        """Verify avatar exists in R2 via HeadObject, clean up previous avatar, and activate it."""
        self._validate_owned_avatar_key(current_user, payload.avatar_key)

        meta = await self._storage.verify_object_exists(payload.avatar_key)
        content_type = meta.content_type.strip().lower() if meta.content_type else None
        if (
            meta.content_length <= 0
            or meta.content_length > self._settings.max_avatar_size_bytes
            or (content_type is not None and content_type not in _ALLOWED_AVATAR_MIMES)
        ):
            await self._storage.delete_objects([payload.avatar_key])
            raise AppError(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="UPLOAD_VERIFICATION_FAILED",
                message="A imagem de avatar enviada possui tamanho ou formato inválido.",
            )

        previous_key = current_user.avatar_key
        current_user.avatar_key = payload.avatar_key
        current_user.avatar_source = "custom"
        current_user.avatar_url = self._storage.build_public_url(payload.avatar_key)

        await self._user_repo.save(current_user)
        await self._session.commit()

        if previous_key and previous_key != payload.avatar_key:
            await self._storage.delete_objects([previous_key])

        return UserMeResponse.model_validate(current_user)

    async def get_public_profile(self, username: str) -> UserPublicResponse:
        """Return public profile and aggregated public stats for the given username."""
        user = await self._user_repo.get_by_username(username)
        if user is None or not user.is_active:
            raise AppError(
                status_code=status.HTTP_404_NOT_FOUND,
                code="RESOURCE_NOT_FOUND",
                message="O perfil de usuário solicitado não foi encontrado.",
            )

        raw_stats = await self._user_repo.get_public_stats(user.id)
        stats = UserPublicStats(**raw_stats)
        discord_linked = bool(user.discord_id)
        avatar_source = user.avatar_source or "none"

        return UserPublicResponse(
            id=user.id,
            username=user.username,
            display_name=resolve_display_name(user),
            bio=user.bio,
            avatar_url=resolve_user_avatar_url(
                user,
                public_base_url=self._settings.r2_public_base_url,
            ),
            avatar_source=avatar_source,  # type: ignore[arg-type]
            discord_linked=discord_linked,
            discord_username=user.discord_username if discord_linked else None,
            stats=stats,
            clips_count=stats.clips_count,
            total_views=stats.total_views,
            total_likes=stats.total_likes,
            collections_count=stats.collections_count,
            created_at=user.created_at,
        )

    async def unlink_discord(self, current_user: User) -> UserMeResponse:
        """Unlink Discord account, blocking with HTTP 400 if the user has no local password."""
        if not current_user.discord_id:
            raise AppError(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="BAD_REQUEST",
                message="Nenhuma conta do Discord está vinculada ao seu perfil.",
            )
        if not current_user.password_hash:
            raise AppError(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="PASSWORD_REQUIRED_BEFORE_UNLINK",
                message=(
                    "Você não possui uma senha cadastrada. Defina uma senha antes de "
                    "desvincular o Discord para não perder o acesso à sua conta."
                ),
            )

        current_user.discord_id = None
        current_user.discord_username = None
        current_user.discord_avatar_url = None
        current_user.discord_linked_at = None

        if current_user.avatar_source == "discord":
            if current_user.avatar_key:
                current_user.avatar_source = "custom"
                current_user.avatar_url = self._storage.build_public_url(current_user.avatar_key)
            else:
                current_user.avatar_source = "none"
                current_user.avatar_url = None

        await self._user_repo.save(current_user)
        await self._session.commit()
        return UserMeResponse.model_validate(current_user)

    async def use_discord_avatar(self, current_user: User) -> UserMeResponse:
        """Switch avatar_source to 'discord' when a Discord account is linked."""
        if not current_user.discord_id:
            raise AppError(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="BAD_REQUEST",
                message="Sua conta do Discord não está vinculada.",
            )

        current_user.avatar_source = "discord"
        current_user.avatar_url = current_user.discord_avatar_url
        await self._user_repo.save(current_user)
        await self._session.commit()
        return UserMeResponse.model_validate(current_user)

    @staticmethod
    def _validate_avatar_source_transition(user: User, target_source: str) -> None:
        if target_source == "custom" and not user.avatar_key:
            raise AppError(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="BAD_REQUEST",
                message="Você ainda não enviou um avatar personalizado.",
            )
        if target_source == "discord" and not user.discord_id:
            raise AppError(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="BAD_REQUEST",
                message="Sua conta do Discord não está vinculada.",
            )

    @staticmethod
    def _validate_owned_avatar_key(user: User, avatar_key: str) -> None:
        expected_prefix = f"avatars/{user.id}/"
        if not avatar_key.startswith(expected_prefix):
            raise AppError(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="BAD_REQUEST",
                message="Chave de avatar inválida para este usuário.",
            )
        suffix = avatar_key.removeprefix(expected_prefix)
        if not _AVATAR_KEY_SUFFIX_PATTERN.fullmatch(suffix):
            raise AppError(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="BAD_REQUEST",
                message="Formato de chave de avatar inválido.",
            )
