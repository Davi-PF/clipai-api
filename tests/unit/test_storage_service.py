"""Unit tests for Cloudflare R2 StorageService."""

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError

from app.core.config import Settings
from app.core.exceptions import AppError
from app.services.storage_service import StorageService


def test_build_object_keys_and_public_url(test_settings: Settings) -> None:
    """Build deterministic R2 keys and public CDN URLs."""
    service = StorageService(test_settings, s3_client=MagicMock())
    user_id = uuid4()
    clip_id = uuid4()

    video_key = service.build_video_key(user_id, clip_id, "video/mp4")
    webm_key = service.build_video_key(user_id, clip_id, "video/webm")
    thumb_key = service.build_thumbnail_key(user_id, clip_id, "image/webp")
    avatar_uuid = uuid4()
    avatar_key = service.build_avatar_key(user_id, avatar_uuid, "image/png")

    assert video_key == f"clips/{user_id}/{clip_id}/video.mp4"
    assert webm_key == f"clips/{user_id}/{clip_id}/video.webm"
    assert thumb_key == f"clips/{user_id}/{clip_id}/thumb.webp"
    assert avatar_key == f"avatars/{user_id}/avatar-{avatar_uuid}.png"
    assert service.build_public_url(video_key) == f"https://cdn.clipai.test/{video_key}"

    with pytest.raises(AppError) as bad_video:
        service.build_video_key(user_id, clip_id, "video/avi")
    assert bad_video.value.status_code == 415

    with pytest.raises(AppError) as bad_thumb:
        service.build_thumbnail_key(user_id, clip_id, "image/gif")
    assert bad_thumb.value.status_code == 415

    with pytest.raises(AppError) as bad_avatar:
        service.build_avatar_key(user_id, avatar_uuid, "image/svg+xml")
    assert bad_avatar.value.status_code == 415


def test_storage_service_initializes_boto3_client_for_r2(test_settings: Settings) -> None:
    """Configure boto3 S3 client with R2 endpoint, auto region, and sigv4."""
    isolated = test_settings.model_copy(
        update={"r2_account_id": "test_account", "r2_endpoint_url": None}
    )
    with patch("app.services.storage_service.boto3.client") as mock_boto_client:
        StorageService(isolated)

    mock_boto_client.assert_called_once()
    args, kwargs = mock_boto_client.call_args
    assert args == ("s3",)
    assert kwargs["endpoint_url"] == "https://test_account.r2.cloudflarestorage.com"
    assert kwargs["region_name"] == "auto"
    assert kwargs["config"].signature_version == "s3v4"

    custom_endpoint = test_settings.model_copy(
        update={"r2_endpoint_url": "https://custom.r2.endpoint/"}
    )
    assert custom_endpoint.effective_r2_endpoint_url == "https://custom.r2.endpoint"


async def test_presigned_url_head_and_delete_operations(test_settings: Settings) -> None:
    """Verify async wrappers for generate_presigned_url, head_object, and delete_objects."""
    s3_mock = MagicMock()
    s3_mock.generate_presigned_url.return_value = "https://r2.example/presigned"
    s3_mock.head_object.return_value = {
        "ContentLength": 1024,
        "ContentType": "video/mp4",
    }
    service = StorageService(test_settings, s3_client=s3_mock)

    url = await service.generate_presigned_upload_url("clips/u/c/video.mp4", "video/mp4", 600)
    assert url == "https://r2.example/presigned"
    s3_mock.generate_presigned_url.assert_called_once_with(
        "put_object",
        Params={"Bucket": "clipai-test", "Key": "clips/u/c/video.mp4", "ContentType": "video/mp4"},
        ExpiresIn=600,
    )

    meta = await service.verify_object_exists("clips/u/c/video.mp4")
    assert meta.content_length == 1024
    assert meta.content_type == "video/mp4"

    await service.delete_objects([])
    s3_mock.delete_objects.assert_not_called()

    await service.delete_objects(["clips/u/c/video.mp4", ""])
    s3_mock.delete_objects.assert_called_once()


async def test_verify_and_delete_handle_r2_client_errors(test_settings: Settings) -> None:
    """Raise UPLOAD_VERIFICATION_FAILED on HeadObject errors and ignore Delete errors."""
    s3_mock = MagicMock()
    client_error = ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
    s3_mock.head_object.side_effect = client_error
    s3_mock.delete_objects.side_effect = client_error
    service = StorageService(test_settings, s3_client=s3_mock)

    with pytest.raises(AppError) as exc_info:
        await service.verify_object_exists("clips/u/c/missing.mp4")
    assert exc_info.value.status_code == 400
    assert exc_info.value.code == "UPLOAD_VERIFICATION_FAILED"

    await service.delete_objects(["clips/u/c/missing.mp4"])
