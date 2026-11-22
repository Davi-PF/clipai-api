"""Discord OAuth2 v10 client service using httpx.AsyncClient."""

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import status

from app.core.config import Settings
from app.core.exceptions import AppError

logger = logging.getLogger(__name__)

_DISCORD_AUTHORIZE_URL = "https://discord.com/api/oauth2/authorize"
_DISCORD_TOKEN_URL = "https://discord.com/api/v10/oauth2/token"  # noqa: S105
_DISCORD_USER_ME_URL = "https://discord.com/api/v10/users/@me"


@dataclass(frozen=True)
class DiscordUserProfile:
    """Normalized Discord identity returned by /api/v10/users/@me."""

    discord_id: str
    username: str
    display_name: str
    email: str | None
    email_verified: bool
    avatar_url: str | None


class DiscordOAuthService:
    """Handles Discord OAuth2 authorization URL building and code exchange."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def build_authorization_url(self, *, state: str) -> str:
        """Build official Discord OAuth2 authorization URL with identify and email scopes."""
        client_id = (self._settings.discord_client_id or "").strip()
        if not client_id:
            raise AppError(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="BAD_REQUEST",
                message="A integração com o Discord OAuth2 não está configurada no servidor.",
            )
        query = urlencode(
            {
                "client_id": client_id,
                "redirect_uri": self._settings.discord_redirect_uri,
                "response_type": "code",
                "scope": "identify email",
                "state": state,
                "prompt": "consent",
            }
        )
        return f"{_DISCORD_AUTHORIZE_URL}?{query}"

    async def exchange_code_for_profile(self, code: str) -> DiscordUserProfile:
        """Exchange OAuth2 authorization code for token and fetch /users/@me profile."""
        client_id = (self._settings.discord_client_id or "").strip()
        client_secret = (
            self._settings.discord_client_secret.get_secret_value().strip()
            if self._settings.discord_client_secret is not None
            else ""
        )
        if not client_id or not client_secret:
            raise AppError(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="BAD_REQUEST",
                message="Credenciais do Discord OAuth2 não estão configuradas no servidor.",
            )

        token_form = {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._settings.discord_redirect_uri,
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                token_resp = await client.post(
                    _DISCORD_TOKEN_URL,
                    data=token_form,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                token_resp.raise_for_status()
                token_data: dict[str, Any] = token_resp.json()
                discord_access_token = token_data.get("access_token")
                if not isinstance(discord_access_token, str) or not discord_access_token.strip():
                    raise AppError(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        code="DISCORD_OAUTH_FAILED",
                        message="Não foi possível obter o token de acesso do Discord.",
                    )

                user_resp = await client.get(
                    _DISCORD_USER_ME_URL,
                    headers={"Authorization": f"Bearer {discord_access_token}"},
                )
                user_resp.raise_for_status()
                profile_data: dict[str, Any] = user_resp.json()
        except AppError:
            raise
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Discord OAuth2 API returned status=%s: %s",
                exc.response.status_code,
                exc.response.text,
            )
            raise AppError(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="DISCORD_OAUTH_FAILED",
                message="Código de autorização do Discord inválido ou expirado.",
            ) from exc
        except httpx.HTTPError as exc:
            logger.exception("Discord OAuth2 network failure: %s", exc)
            raise AppError(
                status_code=status.HTTP_502_BAD_GATEWAY,
                code="DISCORD_OAUTH_FAILED",
                message="Falha de comunicação com os servidores do Discord. Tente novamente.",
            ) from exc

        return self._parse_profile(profile_data)

    @staticmethod
    def _parse_profile(data: dict[str, Any]) -> DiscordUserProfile:
        discord_id = str(data.get("id") or "").strip()
        if not discord_id:
            raise AppError(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="DISCORD_OAUTH_FAILED",
                message="Não foi possível identificar o perfil retornado pelo Discord.",
            )

        raw_username = str(data.get("username") or f"discord_{discord_id}").strip()
        global_name = str(data.get("global_name") or raw_username).strip()

        avatar_hash = data.get("avatar")
        if isinstance(avatar_hash, str) and avatar_hash.strip():
            clean_hash = avatar_hash.strip()
            ext = "gif" if clean_hash.startswith("a_") else "png"
            avatar_url: str | None = (
                f"https://cdn.discordapp.com/avatars/{discord_id}/{clean_hash}.{ext}?size=256"
            )
        elif isinstance(data.get("avatar_url"), str) and data["avatar_url"].strip():
            avatar_url = data["avatar_url"].strip()
        else:
            avatar_url = None

        raw_email = data.get("email")
        email = (
            raw_email.strip().lower() if isinstance(raw_email, str) and raw_email.strip() else None
        )
        email_verified = bool(data.get("verified", False))

        return DiscordUserProfile(
            discord_id=discord_id,
            username=raw_username,
            display_name=global_name[:50],
            email=email,
            email_verified=email_verified,
            avatar_url=avatar_url,
        )
