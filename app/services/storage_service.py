"""Cloudflare R2 S3-compatible object storage service."""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import status

from app.core.config import Settings
from app.core.exceptions import AppError

logger = logging.getLogger(__name__)

_VIDEO_EXTENSION_BY_MIME: dict[str, str] = {
    "video/mp4": "mp4",
    "video/webm": "webm",
}

_THUMBNAIL_EXTENSION_BY_MIME: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}


@dataclass(frozen=True)
class StoredObjectMetadata:
    """Metadata returned by Cloudflare R2 HeadObject verification."""

    content_length: int
    content_type: str | None


class StorageService:
    """Async wrapper around boto3 S3 client configured for Cloudflare R2."""

    def __init__(self, settings: Settings, s3_client: Any = None) -> None:
        self._bucket_name = settings.r2_bucket_name
        self._public_base_url = settings.r2_public_base_url.rstrip("/")
        self._client = s3_client or boto3.client(
            "s3",
            endpoint_url=settings.effective_r2_endpoint_url,
            aws_access_key_id=settings.r2_access_key_id.get_secret_value(),
            aws_secret_access_key=settings.r2_secret_access_key.get_secret_value(),
            region_name="auto",
            config=Config(signature_version="s3v4"),
        )

    @staticmethod
    def build_video_key(user_id: UUID, clip_id: UUID, mime_type: str) -> str:
        """Build deterministic R2 object key for a clip video."""
        extension = _VIDEO_EXTENSION_BY_MIME.get(mime_type)
        if extension is None:
            raise AppError(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                code="UNSUPPORTED_MEDIA_TYPE",
                message="Formato de arquivo não suportado. Use apenas MP4 ou WebM.",
            )
        return f"clips/{user_id}/{clip_id}/video.{extension}"

    @staticmethod
    def build_thumbnail_key(user_id: UUID, clip_id: UUID, mime_type: str) -> str:
        """Build deterministic R2 object key for a clip thumbnail."""
        extension = _THUMBNAIL_EXTENSION_BY_MIME.get(mime_type)
        if extension is None:
            raise AppError(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                code="UNSUPPORTED_MEDIA_TYPE",
                message="Formato de thumbnail não suportado. Use JPEG, PNG ou WebP.",
            )
        return f"clips/{user_id}/{clip_id}/thumb.{extension}"

    async def generate_presigned_upload_url(
        self,
        object_key: str,
        content_type: str,
        expires_in: int = 600,
    ) -> str:
        """Generate a Presigned PUT URL for direct browser-to-R2 upload."""
        params = {
            "Bucket": self._bucket_name,
            "Key": object_key,
            "ContentType": content_type,
        }
        return await asyncio.to_thread(
            self._client.generate_presigned_url,
            "put_object",
            Params=params,
            ExpiresIn=expires_in,
        )

    async def verify_object_exists(self, object_key: str) -> StoredObjectMetadata:
        """Verify an uploaded object exists in R2 and return its size and Content-Type."""
        try:
            response = await asyncio.to_thread(
                self._client.head_object,
                Bucket=self._bucket_name,
                Key=object_key,
            )
        except (ClientError, BotoCoreError) as exc:
            logger.warning("R2 HeadObject failed for key=%s: %s", object_key, exc)
            raise AppError(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="UPLOAD_VERIFICATION_FAILED",
                message="Não foi possível confirmar o envio do arquivo no servidor de mídia.",
            ) from exc

        return StoredObjectMetadata(
            content_length=int(response.get("ContentLength", 0)),
            content_type=response.get("ContentType"),
        )

    async def delete_objects(self, keys: list[str]) -> None:
        """Delete one or more R2 objects, ignoring missing keys safely."""
        valid_keys = [key for key in keys if key]
        if not valid_keys:
            return

        delete_payload = {
            "Objects": [{"Key": key} for key in valid_keys],
            "Quiet": True,
        }
        try:
            await asyncio.to_thread(
                self._client.delete_objects,
                Bucket=self._bucket_name,
                Delete=delete_payload,
            )
        except (ClientError, BotoCoreError) as exc:
            logger.warning("Failed to delete objects from R2 (%s): %s", valid_keys, exc)

    def build_public_url(self, object_key: str) -> str:
        """Combine configured public R2 base URL with an object key."""
        clean_key = object_key.lstrip("/")
        return f"{self._public_base_url}/{clean_key}"
