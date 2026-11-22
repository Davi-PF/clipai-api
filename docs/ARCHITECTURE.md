# Software Architecture — CLIPAÍ Backend (`clipai-api`)

## 1. Visão Geral da Arquitetura

O backend do **CLIPAÍ** segue uma **Arquitetura em Camadas Estrita (Layered Architecture)** sobre **Python 3.12+** e **FastAPI (100% assíncrono)**. A separação garante baixo acoplamento, alta testabilidade unitária e conformidade com os padrões de qualidade do **SonarCloud**.

```mermaid
flowchart LR
    Client["clipai-web (Browser)"] -->|HTTPS JSON / Cookie| Routers["Routers (HTTP Layer)"]
    Client -.->|Direct PUT / GET Media| R2["Cloudflare R2 (S3)"]
    Routers -->|DTOs (Pydantic v2)| Services["Services (Business Logic)"]
    Services -->|Domain Calls| Repositories["Repositories (Data Access)"]
    Services -->|S3 API (boto3)| R2
    Services -->|HTTPS API| Resend["Resend (Transactional Email)"]
    Repositories -->|SQLAlchemy 2.0 Async| DB[("PostgreSQL (Neon/Supabase)")]
```

---

## 2. Responsabilidades das Camadas e Regra de Dependência

O fluxo de dependência é **estritamente unidirecional**:
`routers` -> `services` -> `repositories` -> `models`

| Camada | Diretório | Responsabilidade | O que é PROIBIDO nesta camada |
| :--- | :--- | :--- | :--- |
| **Routers** | `app/routers/` | Definir rotas FastAPI, status HTTP, leitura/escrita de cookies (`refresh_token`), injeção de dependências (`Depends`) e serialização via `response_model`. | Conter regras de negócio, executar queries SQL/SQLAlchemy ou chamar `boto3`/Resend diretamente. |
| **Schemas** | `app/schemas/` | Contratos de entrada/saída e validação estrutural com **Pydantic v2** (`BaseModel`, `ConfigDict(from_attributes=True)`). | Acessar banco de dados ou conter lógica de infraestrutura. |
| **Services** | `app/services/` | Orquestrar casos de uso, aplicar regras de negócio, controle de transações (`session.commit()`), integração com **Cloudflare R2** e **Resend**, e lançar exceções de domínio com mensagens em PT-BR. | Manipular objetos `Request`/`Response` do FastAPI diretamente ou escrever queries SQLAlchemy cruas fora dos repositórios. |
| **Repositories** | `app/repositories/` | Encapsular todas as consultas e mutações no PostgreSQL usando **SQLAlchemy 2.0 Async** (`select`, `insert`, `update`, `delete`). | Chamar `session.commit()` (exclusivo da camada de serviço para atomicidade transacional) ou conter regras de negócio HTTP. |
| **Models** | `app/models/` | Mapeamento ORM declarativo (`DeclarativeBase`, `Mapped`, `mapped_column`) refletindo fielmente `docs/DATABASE.md`. | Importar `schemas`, `services` ou `routers`. |
| **Core** | `app/core/` | Configurações tipadas (`pydantic-settings`), engine/sessão assíncrona do banco, segurança (Argon2, JWT, criptografia TOTP), middlewares, rate limiter, clientes externos (R2 e Resend) e handlers globais de exceção. | Depender de `routers` ou regras específicas de domínio. |

---

## 3. Árvore de Diretórios do Projeto

```text
clipai-api/
├── .github/
│   └── workflows/
│       └── ci.yml                  # Pipeline: Ruff + Pytest (Coverage) + SonarCloud
├── alembic/
│   ├── versions/                   # Arquivos de migração versionados
│   ├── env.py                      # Configuração assíncrona do Alembic (asyncpg)
│   └── script.py.mako
├── app/
│   ├── __init__.py
│   ├── main.py                     # Ponto de entrada FastAPI, CORS, Exception Handlers, Lifespan
│   ├── core/
│   │   ├── __init__.py
│   │   ├── config.py               # Settings via pydantic-settings (.env)
│   │   ├── database.py             # AsyncEngine, async_sessionmaker e get_db()
│   │   ├── dependencies.py         # get_current_user, get_verified_user, get_optional_user
│   │   ├── exceptions.py           # Exceções de domínio e handler padronizado PT-BR
│   │   ├── rate_limit.py           # Rate limiter para rotas sensíveis
│   │   ├── r2_client.py            # Wrapper assíncrono sobre boto3 para Cloudflare R2
│   │   └── security.py             # Argon2id, JWT (access/verify/reset), SHA-256 e Fernet (2FA)
│   ├── models/
│   │   ├── __init__.py             # Exporta todos os models para autogenerate do Alembic
│   │   ├── base.py                 # DeclarativeBase + TimestampMixin
│   │   ├── user.py                 # User, RefreshToken
│   │   ├── game.py                 # Game
│   │   ├── clip.py                 # Clip, Tag, ClipTag, ClipLike, ClipFavorite
│   │   └── collection.py           # Collection, CollectionClip
│   ├── schemas/
│   │   ├── __init__.py
│   │   ├── common.py               # PaginatedResponse[T], ErrorResponse
│   │   ├── auth.py                 # RegisterRequest, LoginRequest, TokenResponse, etc.
│   │   ├── user.py                 # UserPublicResponse, UserMeResponse, UserUpdateRequest
│   │   ├── game.py                 # GameResponse, TagResponse
│   │   ├── clip.py                 # TechnicalMetadataSchema, UploadUrlRequest/Response, ClipResponse
│   │   └── collection.py           # CollectionCreateRequest, CollectionDetailResponse
│   ├── repositories/
│   │   ├── __init__.py
│   │   ├── user_repository.py
│   │   ├── refresh_token_repository.py
│   │   ├── game_repository.py
│   │   ├── tag_repository.py
│   │   ├── clip_repository.py
│   │   └── collection_repository.py
│   ├── services/
│   │   ├── __init__.py
│   │   ├── auth_service.py
│   │   ├── email_service.py
│   │   ├── user_service.py
│   │   ├── clip_service.py
│   │   └── collection_service.py
│   └── routers/
│       ├── __init__.py
│       ├── health.py
│       ├── auth.py
│       ├── users.py
│       ├── games.py
│       ├── tags.py
│       ├── clips.py
│       └── collections.py
├── docs/
│   ├── PRD.md
│   ├── DATABASE.md
│   ├── API_CONTRACT.md
│   ├── ARCHITECTURE.md
│   └── SECURITY.md
├── tests/
│   ├── conftest.py                 # Fixtures assíncronas (AsyncClient, DB isolado, mocks R2/Resend)
│   ├── unit/
│   │   ├── test_security.py
│   │   ├── test_auth_service.py
│   │   ├── test_clip_service.py
│   │   └── test_collection_service.py
│   └── integration/
│       ├── test_auth_router.py
│       ├── test_clips_router.py
│       ├── test_collections_router.py
│       └── test_users_router.py
├── .env.example
├── .gitignore
├── AGENTS.md
├── alembic.ini
├── pyproject.toml                  # Dependências, configuração do Ruff, Pytest e Coverage
├── railway.toml                    # Configuração de build, healthcheck e deploy no Railway
└── sonar-project.properties        # Configuração do SonarCloud
```

---

## 4. Estratégia de Banco de Dados e Migrations

1. **Driver Assíncrono:** Conexão PostgreSQL via `postgresql+asyncpg://...`.
2. **Pool e Serverless Compatibility (Neon / Supabase):**
   - `pool_pre_ping=True` habilitado no `create_async_engine` para evitar conexões órfãs após *scale-to-zero*.
   - Se utilizado com PgBouncer em modo Transaction (Supabase Pooler), configurar `connect_args={"statement_cache_size": 0}`.
3. **Estratégia de Migrations (Alembic):**
   - Toda alteração de schema deve gerar uma migration revisada em `alembic/versions/` (`alembic revision --autogenerate -m "description_in_english"`).
   - É **proibido** usar `Base.metadata.create_all()` no código de aplicação.
   - A `NamingConvention` explícita deve ser configurada no `DeclarativeBase` (`ix_`, `uq_`, `ck_`, `fk_`, `pk_`) para que o Alembic gere nomes determinísticos para constraints e índices.

---

## 5. Integrações Externas

### 5.1. Cloudflare R2 (`boto3` via S3 API)
- **Endpoint S3:** `https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com` com `region_name="auto"` e `signature_version="s3v4"`.
- **Não-Bloqueio do Event Loop:** Como `boto3` é síncrono, as chamadas de rede ao R2 (`head_object`, `delete_objects`) devem ser executadas via `await asyncio.to_thread(...)` dentro de `app/core/r2_client.py` (a geração local `generate_presigned_url` é puramente criptográfica em CPU, mas também encapsulada pelo cliente R2).
- **Padrão de Object Keys:**
  - Vídeo: `clips/{user_id}/{clip_id}/video.{ext}`
  - Thumbnail: `clips/{user_id}/{clip_id}/thumb.{ext}`
- **Entrega Pública (Leitura):** Os vídeos e thumbnails com `status = 'ready'` são servidos através do domínio público customizado do bucket R2 (`R2_PUBLIC_BASE_URL`, ex.: `https://cdn.clipai.com.br/{object_key}`), aproveitando o cache de borda da Cloudflare sem custo de egress.

### 5.2. Resend (E-mail Transacional)
- O módulo `app/services/email_service.py` realiza chamadas HTTP assíncronas via `httpx.AsyncClient` para a API do **Resend**.
- **Templates Transacionais do MVP (em PT-BR):**
  1. **Confirmação de Conta:** Link `https://<FRONTEND_URL>/verify-email?token=<jwt_verify_token>` (TTL: 24h).
  2. **Recuperação de Senha:** Link `https://<FRONTEND_URL>/reset-password?token=<jwt_reset_token>` (TTL: 30min).

---

## 6. Variáveis de Ambiente (`.env.example`)

Todo o projeto é configurado via variáveis de ambiente validadas na inicialização pelo `pydantic-settings`. Se qualquer variável obrigatória estiver ausente, a aplicação falha imediatamente no startup (*Fail-Fast*).

```dotenv
# ==============================================================================
# APPLICATION & ENVIRONMENT
# ==============================================================================
APP_NAME="clipai-api"
APP_ENV="development"                  # development | staging | production
APP_DEBUG=false
API_V1_PREFIX="/api/v1"
FRONTEND_URL="http://localhost:3000"   # Usado em links de e-mail e CORS
CORS_ALLOWED_ORIGINS="http://localhost:3000"

# ==============================================================================
# DATABASE (PostgreSQL - Neon / Supabase)
# ==============================================================================
DATABASE_URL="postgresql+asyncpg://postgres:postgres@localhost:5432/clipai"
DB_POOL_SIZE=10
DB_MAX_OVERFLOW=20
DB_ECHO_SQL=false

# ==============================================================================
# SECURITY & AUTHENTICATION
# ==============================================================================
JWT_SECRET_KEY="change-me-in-production-use-at-least-64-random-hex-characters"
JWT_ALGORITHM="HS256"
ACCESS_TOKEN_EXPIRE_MINUTES=15
REFRESH_TOKEN_EXPIRE_DAYS=7
EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS=24
PASSWORD_RESET_TOKEN_EXPIRE_MINUTES=30

# Cookie de Refresh Token
REFRESH_COOKIE_NAME="clipai_refresh_token"
REFRESH_COOKIE_SECURE=true             # false apenas em localhost HTTP
REFRESH_COOKIE_SAMESITE="lax"          # lax | strict
REFRESH_COOKIE_DOMAIN=""               # vazio ou domínio compartilhado em produção

# Criptografia para 2FA TOTP (Reservado para v1.1 - Chave Fernet Base64 de 32 bytes)
TOTP_ENCRYPTION_KEY="generate-with-cryptography-fernet-generate-key"
TOTP_ISSUER_NAME="CLIPAI"

# ==============================================================================
# CLOUDFLARE R2 (S3 Compatible Storage)
# ==============================================================================
R2_ACCOUNT_ID="your_cloudflare_account_id"
R2_ACCESS_KEY_ID="your_r2_access_key_id"
R2_SECRET_ACCESS_KEY="your_r2_secret_access_key"
R2_BUCKET_NAME="clipai-media"
R2_PUBLIC_BASE_URL="https://cdn.clipai.com.br"
R2_PRESIGNED_URL_EXPIRE_SECONDS=900    # 15 minutos

# Limites de Upload
MAX_VIDEO_SIZE_BYTES=104857600         # 100 MB
MAX_VIDEO_DURATION_SECONDS=120         # 120 segundos
MAX_THUMBNAIL_SIZE_BYTES=5242880       # 5 MB

# ==============================================================================
# RESEND (Transactional Email)
# ==============================================================================
RESEND_API_KEY="re_xxxxxxxxx"
EMAIL_FROM_ADDRESS="CLIPAÍ <onboarding@resend.dev>" # Trocar para noreply@clipai.com.br após verificar domínio
```

---

## 7. Qualidade, CI/CD e Deploy (GitHub Actions + SonarCloud + Railway)

### 7.1. Pipeline de Integração Contínua (`.github/workflows/ci.yml`)
Executado em todo `push` e `pull_request` para `main` e `develop`:

1. **Lint & Format Check (`Ruff`):**
   - `ruff check .` (verifica erros de lint, imports, segurança `flake8-bandit`/`S`, complexidade `C901`).
   - `ruff format --check .` (garante formatação padronizada).
2. **Testes Automatizados + Cobertura (`pytest`):**
   - `pytest --cov=app --cov-report=xml:coverage.xml --cov-fail-under=80`
   - Executa testes unitários e de integração com `httpx.AsyncClient`.
3. **Análise Estática e Quality Gate (`SonarCloud`):**
   - Envia o relatório `coverage.xml` para o `SonarSource/sonarcloud-github-action`.
   - Bloqueia o merge se o Quality Gate falhar (cobertura em código novo `< 80%`, bugs, vulnerabilidades, *security hotspots* ou duplicação `> 3%`).

### 7.2. Deploy Contínuo no Railway
- O **Railway** monitora a branch `main` e inicia o deploy somente após a aprovação do workflow de CI no GitHub Actions (*Wait for CI*).
- **Release / Start Command:**
  ```bash
  alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips="*"
  ```
- **Healthcheck do Railway:**
  - Rota: `/api/v1/health`
  - Timeout: `30s`
