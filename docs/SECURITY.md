# Security Guidelines & Architecture — CLIPAÍ (`clipai-api`)

## 1. Hashing de Senhas com Argon2id

Nenhuma senha é armazenada ou registrada em logs em texto puro. O CLIPAÍ utiliza **Argon2id** (vencedor da Password Hashing Competition e recomendado pela OWASP) via `pwdlib[argon2]` / `argon2-cffi`.

### Parâmetros Mínimos Obrigatórios (OWASP Recommended)
- **Variante:** `Argon2id` (resistente a ataques de *side-channel* e *GPU cracking*)
- **Memória (`memory_cost`):** `65536 KiB` (64 MB)
- **Iterações (`time_cost`):** `3`
- **Paralelismo (`parallelism`):** `4`
- **Tamanho do Salt:** `16 bytes` (gerado via CSPRNG por senha)
- **Tamanho do Hash:** `32 bytes`
- **Rehash Automático:** Caso os parâmetros de custo sejam aumentados no futuro, `check_needs_rehash()` atualiza o hash transparentemente durante o próximo login bem-sucedido.
- **Prevenção de Timing Attack no Login:** Quando um e-mail inexistente é informado no login, o serviço executa uma verificação Argon2 contra um hash *dummy* pré-calculado para igualar o tempo de resposta e impedir enumeração de contas por análise temporal.

---

## 2. Autenticação JWT e Rotação/Revogação de Refresh Tokens

### 2.1. Arquitetura Dual-Token

| Token | Transporte | TTL | Persistência no Banco | Finalidade |
| :--- | :--- | :---: | :--- | :--- |
| **Access Token (JWT)** | Header `Authorization: Bearer <token>` (mantido em memória no frontend) | **15 minutos** | Stateless (não persistido) | Autorizar chamadas às rotas da API (`/api/v1/...`). |
| **Refresh Token (Opaco)** | Cookie `clipai_refresh_token` (`HttpOnly; Secure; SameSite=Lax; Path=/api/v1/auth`) | **7 dias** | Persistido como hash **SHA-256** na tabela `refresh_tokens` | Emitir novo par de tokens sem expor credenciais de longa duração ao JavaScript (mitigação contra XSS). |

### 2.2. Claims Obrigatórias do JWT
Todo JWT emitido pelo backend (`access`, `email_verification`, `password_reset`, e futuramente `2fa_pending`) deve conter a claim `type` (escopo) validada estritamente pelo consumidor:
- `sub`: UUID do usuário (`str`)
- `type`: `"access"` | `"email_verification"` | `"password_reset"` | `"2fa_pending"`
- `iat`: Timestamp de emissão (UTC)
- `exp`: Timestamp de expiração (UTC)
- `jti`: Identificador único do token (`UUID v4`)

Um token com `type="email_verification"` ou `type="password_reset"` **jamais** é aceito pelo middleware `get_current_user` (que exige `type="access"`).

### 2.3. Rotação de Refresh Token e Detecção de Reuso (*Reuse Detection*)
1. **Emissão no Login:** Gera-se um token aleatório criptograficamente seguro (`secrets.token_urlsafe(48)`). O valor bruto vai apenas para o cookie `HttpOnly`, e seu hash `SHA-256` é salvo em `refresh_tokens` com um novo `family_id`.
2. **Rotação em `POST /api/v1/auth/refresh`:**
   - O backend calcula o `SHA-256` do cookie recebido e busca o registro em `refresh_tokens`.
   - Se o token for válido e `revoked_at IS NULL`, o token atual recebe `revoked_at = now()`, um novo Refresh Token é criado com o **mesmo `family_id`**, e `replaced_by_id` aponta para o novo registro.
3. **Detecção de Roubo/Replay:**
   - Se alguém apresentar um Refresh Token que **já possui `revoked_at` preenchido** (ou seja, já foi rotacionado anteriormente), o sistema assume que houve vazamento do cookie.
   - **Ação Imediata:** O backend executa `UPDATE refresh_tokens SET revoked_at = now() WHERE family_id = :family_id AND revoked_at IS NULL`, invalidando instantaneamente a sessão legítima e a do invasor, forçando novo login.
4. **Revogação Global por Usuário:**
   - Acionada automaticamente ao redefinir a senha (`POST /api/v1/auth/reset-password`) ou desativar a conta (`is_active = false`).

---

## 3. Política de CORS e Segurança de Headers

### 3.1. CORS no FastAPI
O `CORSMiddleware` é configurado sem curingas (`*`), restrito exclusivamente às origens declaradas em `CORS_ALLOWED_ORIGINS` (domínio do `clipai-web`):
- `allow_origins`: `[FRONTEND_URL]` (ex.: `["https://clipai.com.br"]`)
- `allow_credentials`: `True` (necessário para tráfego do cookie `HttpOnly`)
- `allow_methods`: `["GET", "POST", "PATCH", "DELETE", "OPTIONS"]`
- `allow_headers`: `["Authorization", "Content-Type", "Accept", "X-Request-ID"]`

### 3.2. CORS no Bucket Cloudflare R2
O bucket R2 (`clipai-media`) também deve ter política de CORS restrita ao domínio do `clipai-web`:
- `AllowedOrigins`: `["https://clipai.com.br", "http://localhost:3000"]`
- `AllowedMethods`: `["PUT", "GET", "HEAD"]`
- `AllowedHeaders`: `["Content-Type", "Content-Length"]`
- `MaxAgeSeconds`: `3600`

---

## 4. Proteção contra Upload Abusivo e Arquivos Órfãos no Cloudflare R2

Como o upload vai direto do navegador para o Cloudflare R2, o backend aplica 4 barreiras de defesa:

1. **Barreira 1 — Pré-validação Estrita antes da Assinatura (`POST /api/v1/clips/upload-url`):**
   - Exige usuário autenticado e com `is_email_verified = true`.
   - Rejeita qualquer `video_mime_type` fora de `{"video/mp4", "video/webm"}` e qualquer `thumbnail_mime_type` fora de `{"image/jpeg", "image/png", "image/webp"}`.
   - Rejeita `video_size_bytes > 104_857_600` (100 MB), `thumbnail_size_bytes > 5_242_880` (5 MB) ou `duration_seconds > 120.0`.
   - Limita a quantidade de uploads pendentes simultâneos por usuário (máximo de **3 clipes** em `status = 'pending_upload'` por usuário).

2. **Barreira 2 — Assinatura S3 v4 Restrita (Binding de Headers e Key Isolada):**
   - O `object_key` nunca usa o nome de arquivo enviado pelo usuário (prevenindo *Path Traversal*). É gerado deterministicamente pelo servidor: `clips/{user_id}/{clip_id}/video.{ext}`.
   - A Presigned `PUT` URL é assinada incluindo `Bucket`, `Key`, `ContentType` e `ContentLength` exatos, com expiração curta (`ExpiresIn=900` segundos / 15 minutos). Se o cliente tentar enviar um arquivo maior ou com `Content-Type` diferente do declarado, a própria Cloudflare R2 rejeita o `PUT` com `403 SignatureDoesNotMatch`.

3. **Barreira 3 — Verificação `HeadObject` na Confirmação (`POST /api/v1/clips/{clip_id}/confirm-upload`):**
   - O backend executa `head_object` no R2 para o vídeo e para a thumbnail.
   - Verifica se `ContentLength == clip.file_size_bytes` e `ContentType == clip.mime_type`.
   - Caso haja discrepância, o backend deleta imediatamente o objeto inválido no R2, marca o clipe como `status = 'failed'` e retorna `400 UPLOAD_VERIFICATION_FAILED`.

4. **Barreira 4 — Prevenção e Limpeza de Arquivos Órfãos:**
   - **Deleção Síncrona:** Ao excluir um clipe (`DELETE /api/v1/clips/{clip_id}`), o serviço remove os objetos `video_object_key` e `thumbnail_object_key` do bucket R2.
   - **Expurgo de `pending_upload` Abandonados:** Registros que permanecerem em `pending_upload` por mais de **30 minutos** são marcados como `failed` e seus respectivos `object_keys` têm deleção solicitada ao R2 (além de regra de *Object Lifecycle* no R2 para abortar uploads multipart incompletos em 1 dia).

---

## 5. Rate Limiting em Rotas Sensíveis

Implementado na camada de entrada por IP (e por `user_id` em rotas autenticadas). Quando excedido, retorna `429 Too Many Requests` (`error.code = "RATE_LIMIT_EXCEEDED"`) e o header `Retry-After`:

| Endpoint | Limite | Chave de Controle | Objetivo |
| :--- | :--- | :--- | :--- |
| `POST /api/v1/auth/register` | `5 req / minuto` | Endereço IP | Prevenir criação massiva de contas por bots |
| `POST /api/v1/auth/login` | `10 req / minuto` | Endereço IP + E-mail | Mitigar força bruta e *credential stuffing* |
| `POST /api/v1/auth/2fa/verify-login` | `5 req / minuto` | Endereço IP | Mitigar força bruta em códigos TOTP de 6 dígitos e códigos de backup |
| `POST /api/v1/auth/discord/callback` | `10 req / minuto` | Endereço IP | Limitar tentativas de troca de código OAuth2 |
| `POST /api/v1/auth/forgot-password` | `3 req / 15 minutos` | Endereço IP + E-mail | Evitar abuso de cota do Resend e spam |
| `POST /api/v1/auth/resend-verification` | `3 req / 15 minutos` | Endereço IP + E-mail | Evitar abuso de envio de e-mails |
| `POST /api/v1/clips/upload-url` | `10 req / hora` | `user_id` | Evitar geração abusiva de URLs pré-assinadas |
| `POST /api/v1/clips/{clip_id}/views` | `1 req / 10 minutos` | `(user_id ou IP) + clip_id` | Evitar inflação artificial do contador de views |

---

## 6. Autenticação em Dois Fatores (2FA TOTP via PyOTP + Fernet)

### 6.1. Criptografia do Segredo TOTP em Repouso e Códigos de Backup
- O segredo Base32 gerado por `pyotp.random_base32()` **nunca** é salvo em texto puro no PostgreSQL.
- Antes de persistir em `users.totp_secret_encrypted`, o valor é criptografado usando **Fernet (AES-128-CBC + HMAC-SHA256)** da biblioteca `cryptography`, utilizando a chave definida em `TOTP_ENCRYPTION_KEY`.
- Os 8 códigos de backup gerados na ativação (`XXXX-XXXX`) são exibidos em texto claro uma única vez ao usuário e armazenados em `users.totp_backup_codes` (`JSONB`) exclusivamente como hashes **SHA-256**. Quando um código de backup é utilizado, seu hash é removido atomicamente da lista.

### 6.2. Fluxo de Ativação, Desafio de Login e Desativação 2FA
1. **Setup (`POST /api/v1/auth/2fa/setup`):**
   - Usuário autenticado solicita configuração do 2FA.
   - Backend gera `secret = pyotp.random_base32()`, criptografa com Fernet e salva em `totp_secret_encrypted` mantendo `is_2fa_enabled = false`.
   - Retorna `manual_secret` e `otpauth_uri` (`issuer=CLIPAÍ`) para renderização do QR Code no frontend.
2. **Ativação (`POST /api/v1/auth/2fa/enable`):**
   - Usuário envia o primeiro código de 6 dígitos (`code`) gerado no Google Authenticator / Authy.
   - Backend descriptografa `totp_secret_encrypted`, valida com `pyotp.TOTP(secret).verify(code, valid_window=1)`, atualiza `is_2fa_enabled = true`, persiste os hashes `SHA-256` dos 8 códigos de backup em `totp_backup_codes` e retorna os códigos em texto claro.
3. **Login em Duas Etapas (`POST /api/v1/auth/login` ou `/auth/discord/callback` -> `POST /api/v1/auth/2fa/verify-login`):**
   - Se `user.is_2fa_enabled == true`, o login **não emite** o `access_token` nem o cookie `clipai_refresh_token`. Retorna `requires_2fa: true` e um `challenge_token` (JWT com `scope="2fa_challenge"` e TTL de 5 minutos).
   - O frontend solicita o código TOTP (ou código de backup) e chama `POST /api/v1/auth/2fa/verify-login` enviando `challenge_token` e `code`.
   - Após validar o TOTP (ou consumir um código de backup de uso único), o backend emite o `access_token` definitivo e o cookie `HttpOnly` `clipai_refresh_token`.
4. **Desativação (`POST /api/v1/auth/2fa/disable`):**
   - Exige a senha atual do usuário (quando `has_password = true`) + um código TOTP ou código de backup válido, definindo `is_2fa_enabled = false`, `totp_secret_encrypted = NULL` e `totp_backup_codes = NULL`.

---

## 7. Segurança da Integração Discord OAuth2 e Upload de Avatar no R2

1. **Proteção contra CSRF no OAuth2 (`state` JWT Assinado):**
   - `GET /api/v1/auth/discord/url` emite um JWT `state` de 10 minutos com `type="discord_oauth_state"`, `mode` (`login` ou `link`) e `user_id` (quando `mode="link"`).
   - `POST /api/v1/auth/discord/callback` valida a assinatura e expiração do `state` antes de contatar a API v10 do Discord.
2. **Prevenção de Pre-Account Takeover no Login Social:**
   - Ao autenticar via Discord com um `discord_id` inédito cujo e-mail já existe no banco, o vínculo automático só ocorre se o e-mail estiver verificado tanto no Discord (`verified: true`) quanto na conta local (`is_email_verified: true`).
3. **Proteção contra Lockout ao Desvincular Discord:**
   - `DELETE /api/v1/users/me/discord` bloqueia a remoção do vínculo com HTTP `400 PASSWORD_REQUIRED_BEFORE_UNLINK` caso `password_hash` seja nulo (`has_password = false`).
4. **Isolamento e Limpeza de Avatares no Cloudflare R2:**
   - `POST /api/v1/users/me/avatar/upload-url` restringe uploads a `image/jpeg`, `image/png` e `image/webp` com tamanho máximo de **5 MB**, gerando chaves determinísticas em `avatars/{user_id}/avatar-{uuid}.{ext}`.
   - `POST /api/v1/users/me/avatar/confirm` valida o prefixo da chave pertencente ao `user_id`, executa `HeadObject` no R2 e remove automaticamente o `avatar_key` anterior do bucket.
