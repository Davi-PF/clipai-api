"""Unit tests for EmailService (Resend integration and dev fallback logging)."""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.core.config import Settings
from app.core.exceptions import AppError
from app.services.email_service import EmailService


async def test_email_service_sends_verification_and_reset_via_resend(
    test_settings: Settings,
) -> None:
    """Send PT-BR HTML e-mails via Resend API when RESEND_API_KEY is configured."""
    service = EmailService(test_settings)

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    with patch("app.services.email_service.httpx.AsyncClient", return_value=mock_client):
        await service.send_verification_email(
            to_email="player@clipai.com.br",
            username="fallen<br>",
            token="verify-tok-123",
        )
        await service.send_password_reset_email(
            to_email="player@clipai.com.br",
            username="fallen",
            token="reset-tok-456",
        )

    assert mock_client.post.await_count == 2
    first_call_kwargs = mock_client.post.call_args_list[0].kwargs
    assert first_call_kwargs["json"]["to"] == ["player@clipai.com.br"]
    assert (
        "http://localhost:3000/verify-email?token=verify-tok-123"
        in first_call_kwargs["json"]["html"]
    )
    assert "fallen&lt;br&gt;" in first_call_kwargs["json"]["html"]

    second_call_kwargs = mock_client.post.call_args_list[1].kwargs
    assert (
        "http://localhost:3000/reset-password?token=reset-tok-456"
        in second_call_kwargs["json"]["html"]
    )


async def test_email_service_logs_link_in_dev_when_resend_key_missing(
    test_settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Log safe action link in dev when RESEND_API_KEY is not configured."""
    dev_settings = test_settings.model_copy(
        update={"app_env": "development", "resend_api_key": None}
    )
    service = EmailService(dev_settings)

    with caplog.at_level(logging.INFO):
        await service.send_verification_email(
            to_email="dev@clipai.com.br",
            username="devuser",
            token="dev-token-999",
        )

    assert "[DEV EMAIL]" in caplog.text
    assert "http://localhost:3000/verify-email?token=dev-token-999" in caplog.text


async def test_email_service_raises_app_error_on_resend_failure(
    test_settings: Settings,
) -> None:
    """Convert Resend HTTP errors into standardized 500 AppError."""
    service = EmailService(test_settings)
    mock_client = AsyncMock()
    mock_client.post.side_effect = httpx.ConnectError("network timeout")
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    with (
        patch("app.services.email_service.httpx.AsyncClient", return_value=mock_client),
        pytest.raises(AppError) as exc_info,
    ):
        await service.send_password_reset_email(
            to_email="player@clipai.com.br",
            username="fallen",
            token="reset-tok",
        )

    assert exc_info.value.status_code == 500
    assert exc_info.value.code == "INTERNAL_SERVER_ERROR"


async def test_email_service_logs_resend_status_error_details(
    test_settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Log Resend HTTP status and response body when Resend rejects a request."""
    service = EmailService(test_settings)
    request = httpx.Request("POST", "https://api.resend.com/emails")
    response = httpx.Response(
        403,
        request=request,
        text='{"statusCode":403,"message":"Domain not verified"}',
    )
    mock_response = MagicMock()
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "403 Forbidden",
        request=request,
        response=response,
    )

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    with (
        patch("app.services.email_service.httpx.AsyncClient", return_value=mock_client),
        pytest.raises(AppError) as exc_info,
    ):
        await service.send_verification_email(
            to_email="player@clipai.com.br",
            username="fallen",
            token="tok",
        )

    assert exc_info.value.status_code == 500
    assert "status=403" in caplog.text
    assert "Domain not verified" in caplog.text


async def test_email_service_blocks_missing_key_in_production(test_settings: Settings) -> None:
    """Never allow silent console fallback when running in production."""
    prod_settings = test_settings.model_copy(
        update={"app_env": "production", "resend_api_key": None}
    )
    service = EmailService(prod_settings)

    with pytest.raises(RuntimeError, match="RESEND_API_KEY is required"):
        await service.send_verification_email(
            to_email="prod@clipai.com.br",
            username="produser",
            token="tok",
        )
