"""Transactional e-mail service backed by Resend with CLIPAÍ HTML templates."""

import html
import logging

import httpx
from fastapi import status

from app.core.config import Settings
from app.core.exceptions import AppError

logger = logging.getLogger(__name__)

_RESEND_API_URL = "https://api.resend.com/emails"


def _render_clipai_email_html(
    *,
    greeting_name: str,
    title: str,
    body_text: str,
    cta_label: str,
    cta_url: str,
    footer_note: str,
) -> str:
    """Render a clean PT-BR HTML e-mail using CLIPAÍ dark visual identity."""
    safe_name = html.escape(greeting_name)
    safe_title = html.escape(title)
    safe_body = html.escape(body_text)
    safe_cta_label = html.escape(cta_label)
    safe_cta_url = html.escape(cta_url, quote=True)
    safe_footer = html.escape(footer_note)

    body_style = (
        "margin:0;padding:0;background-color:#061C2C;color:#B7C7D6;"
        "font-family:'Segoe UI',Arial,sans-serif;"
    )
    card_style = (
        "max-width:560px;background-color:#193347;border:1px solid #4F6478;"
        "border-radius:12px;overflow:hidden;"
    )
    btn_style = (
        "display:inline-block;padding:12px 24px;font-size:14px;font-weight:700;"
        "color:#061C2C;text-decoration:none;letter-spacing:0.6px;text-transform:uppercase;"
    )

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{safe_title}</title>
</head>
<body style="{body_style}">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0"
         style="background-color:#061C2C;padding:32px 16px;">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0"
               style="{card_style}">
          <tr>
            <td style="padding:28px 32px 16px 32px;border-bottom:1px solid #334B60;">
              <span style="font-size:26px;font-weight:800;letter-spacing:2px;color:#B7C7D6;">
                CLIPAÍ
              </span>
            </td>
          </tr>
          <tr>
            <td style="padding:28px 32px;">
              <h1 style="margin:0 0 16px 0;font-size:20px;color:#B7C7D6;">{safe_title}</h1>
              <p style="margin:0 0 12px 0;font-size:15px;line-height:1.6;color:#B7C7D6;">
                Olá, <strong>{safe_name}</strong>!
              </p>
              <p style="margin:0 0 24px 0;font-size:15px;line-height:1.6;color:#91A1B2;">
                {safe_body}
              </p>
              <table role="presentation" cellspacing="0" cellpadding="0" style="margin:0 0 24px 0;">
                <tr>
                  <td style="border-radius:8px;background-color:#2EB886;">
                    <a href="{safe_cta_url}" target="_blank" rel="noopener noreferrer"
                       style="{btn_style}">
                      {safe_cta_label}
                    </a>
                  </td>
                </tr>
              </table>
              <p style="margin:0 0 8px 0;font-size:12px;line-height:1.5;color:#6C7F91;">
                Se o botão não funcionar, copie e cole o link abaixo no navegador:
              </p>
              <p style="margin:0 0 20px 0;font-size:12px;line-height:1.5;word-break:break-all;
                        color:#2EB886;">
                {safe_cta_url}
              </p>
              <p style="margin:0;font-size:12px;line-height:1.5;color:#91A1B2;">
                {safe_footer}
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


class EmailService:
    """Dispatches transactional authentication e-mails via Resend."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _build_frontend_link(self, route: str, token: str) -> str:
        """Build full frontend action URL using FRONTEND_URL."""
        base_url = self._settings.frontend_url.rstrip("/")
        clean_route = route.lstrip("/")
        return f"{base_url}/{clean_route}?token={token}"

    async def _send_email(
        self,
        *,
        to_email: str,
        subject: str,
        html_body: str,
        action_url: str,
    ) -> None:
        """Deliver e-mail via Resend or safely log link in non-production environments."""
        api_key = (
            self._settings.resend_api_key.get_secret_value().strip()
            if self._settings.resend_api_key
            else ""
        )

        if not api_key:
            if self._settings.is_production:
                raise RuntimeError("RESEND_API_KEY is required in production.")
            logger.info(
                "[DEV EMAIL] Destinatário=%s | Assunto=%s | Link=%s",
                to_email,
                subject,
                action_url,
            )
            return

        payload = {
            "from": self._settings.email_from_address,
            "to": [to_email],
            "subject": subject,
            "html": html_body,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(_RESEND_API_URL, json=payload, headers=headers)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.exception(
                "Resend rejected transactional e-mail (status=%s): %s",
                exc.response.status_code,
                exc.response.text,
            )
            raise AppError(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                code="INTERNAL_SERVER_ERROR",
                message=(
                    "Não foi possível enviar o e-mail no momento. Tente novamente mais tarde."
                ),
            ) from exc
        except httpx.HTTPError as exc:
            logger.exception("Failed to send transactional e-mail via Resend: %s", exc)
            raise AppError(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                code="INTERNAL_SERVER_ERROR",
                message=(
                    "Não foi possível enviar o e-mail no momento. Tente novamente mais tarde."
                ),
            ) from exc

    async def send_verification_email(
        self,
        *,
        to_email: str,
        username: str,
        token: str,
    ) -> None:
        """Send account verification e-mail in PT-BR."""
        verify_url = self._build_frontend_link("verify-email", token)
        html_body = _render_clipai_email_html(
            greeting_name=username,
            title="Confirme seu e-mail no CLIPAÍ",
            body_text=(
                "Sua conta foi criada! Para começar a enviar seus clipes e criar coleções, "
                "confirme seu endereço de e-mail clicando no botão abaixo."
            ),
            cta_label="Confirmar e-mail",
            cta_url=verify_url,
            footer_note=(
                "Este link expira em 24 horas. Se você não criou esta conta, ignore este e-mail."
            ),
        )
        await self._send_email(
            to_email=to_email,
            subject="Confirme seu e-mail — CLIPAÍ",
            html_body=html_body,
            action_url=verify_url,
        )

    async def send_password_reset_email(
        self,
        *,
        to_email: str,
        username: str,
        token: str,
    ) -> None:
        """Send password recovery e-mail in PT-BR."""
        reset_url = self._build_frontend_link("reset-password", token)
        html_body = _render_clipai_email_html(
            greeting_name=username,
            title="Redefinição de senha — CLIPAÍ",
            body_text=(
                "Recebemos uma solicitação para redefinir a senha da sua conta no CLIPAÍ. "
                "Clique no botão abaixo para escolher uma nova senha."
            ),
            cta_label="Redefinir senha",
            cta_url=reset_url,
            footer_note=(
                "Este link é válido por 30 minutos e pode ser usado apenas uma vez. "
                "Se não foi você, sua senha permanece segura."
            ),
        )
        await self._send_email(
            to_email=to_email,
            subject="Redefina sua senha — CLIPAÍ",
            html_body=html_body,
            action_url=reset_url,
        )
