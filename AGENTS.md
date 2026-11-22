# AGENTS.md — Regras Permanentes para Agentes de Código (`clipai-api`)

Este documento define as diretrizes inegociáveis para qualquer Agente de IA ou desenvolvedor que atue no repositório **`clipai-api`** (Backend do **CLIPAÍ**).

---

## 1. Fonte Única de Verdade (`/docs`) — Leitura Obrigatória

Antes de criar, alterar ou refatorar qualquer arquivo de código, migration ou teste, você **DEVE** consultar os documentos em `/docs`:

1. [`docs/PRD.md`](docs/PRD.md): Regras de negócio, limites de upload, ciclo de vida dos clipes (`pending_upload` -> `ready` -> `failed`), privacidade (`public`, `friends`, `unlisted`) e escopo travado do MVP vs. Pós-MVP.
2. [`docs/DATABASE.md`](docs/DATABASE.md): Modelagem relacional completa do PostgreSQL, tabelas, tipos, FKs, índices, schema do `technical_metadata` (`JSONB`) e campos reservados para 2FA (`is_2fa_enabled`, `totp_secret_encrypted`).
3. [`docs/API_CONTRACT.md`](docs/API_CONTRACT.md): Contrato REST (`/api/v1/...`), payloads JSON de request/response, códigos HTTP, formato padronizado de erro e fluxo de upload via Presigned URL.
4. [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): Arquitetura em camadas (`routers`, `services`, `repositories`, `models`, `schemas`, `core`), árvore de diretórios, variáveis de ambiente e pipeline CI/CD.
5. [`docs/SECURITY.md`](docs/SECURITY.md): Argon2id, JWT + Refresh Token rotativo em cookie `HttpOnly`, CORS, proteção do Cloudflare R2, rate limiting e plano técnico do 2FA TOTP.
6. [`docs/GIT_WORKFLOW.md`](docs/GIT_WORKFLOW.md): Fluxo de branches (GitHub Flow), nomenclatura de branches, política de commits atômicos e estratégia de Pull Requests.

### Sincronização de Contratos
- **NUNCA** implemente um endpoint, altere um payload JSON ou modifique uma coluna do banco sem que a mudança esteja refletida em `docs/API_CONTRACT.md` e `docs/DATABASE.md`. O frontend (`clipai-web`) depende diretamente de `docs/API_CONTRACT.md`.

---

## 2. Regras Arquiteturais Inegociáveis

1. **Vídeos NUNCA Trafegam pelo FastAPI:**
   - É estritamente proibido criar endpoints que recebam `UploadFile` ou `multipart/form-data` para vídeos ou thumbnails de clipes. Todo upload deve ocorrer via **Presigned `PUT` URL** diretamente do navegador para o **Cloudflare R2**, seguido por `POST /api/v1/clips/{clip_id}/confirm-upload`.
2. **Separação Estrita de Camadas (`routers` -> `services` -> `repositories` -> `models`):**
   - `routers`: Apenas camada HTTP (status codes, cookies, `Depends`, schemas Pydantic). Zero lógica de negócio ou queries SQL.
   - `services`: Toda regra de negócio, controle transacional (`await session.commit()`), chamadas ao R2 e Resend.
   - `repositories`: Apenas queries SQLAlchemy 2.0 (`select()`, `insert()`, `update()`, `delete()`). Repositórios usam `await session.flush()`, mas **nunca** `session.commit()`.
   - `models`: Apenas definições ORM SQLAlchemy 2.0 (`Mapped`, `mapped_column`).
3. **100% Assíncrono (Non-Blocking Event Loop):**
   - Use `AsyncSession` (`sqlalchemy.ext.asyncio`) com `asyncpg`.
   - Qualquer chamada a SDK síncrono (como `boto3` para `head_object` ou `delete_objects`) deve ser encapsulada com `await asyncio.to_thread(...)`.
4. **Escopo Travado do MVP:**
   - Não implemente funcionalidades de Pós-MVP (Discord OAuth2, transcodificação HLS, comentários, grafo de amizades) a menos que explicitamente solicitado, mas **preserve** as colunas e campos de contrato já reservados para o 2FA (`is_2fa_enabled`, `totp_secret_encrypted`, `requires_2fa`, `temp_2fa_token`).

---

## 3. Convenções de Idioma e Padronização

- **Em Inglês (`en-US`):**
  - Todo o código-fonte (nomes de módulos, classes, funções, métodos, variáveis, constantes).
  - Nomes de tabelas, colunas, índices e migrations do Alembic.
  - Caminhos de rotas (`/api/v1/...`), query params, chaves JSON e códigos de erro (`error.code`, ex.: `EMAIL_NOT_VERIFIED`).
  - Mensagens de commit Git (padrão *Conventional Commits*: `feat:`, `fix:`, `test:`, `docs:`, `refactor:`, `chore:`).
- **Em Português do Brasil (`pt-BR`):**
  - Todas as mensagens de erro voltadas ao usuário final (`error.message` e `error.details[].message`).
  - Assuntos e corpos dos e-mails transacionais enviados via Resend.

---

## 4. Segurança e Proibição de Segredos Hardcoded

1. **Zero Segredos no Código:**
   - É terminantemente proibido inserir chaves de API, segredos JWT, credenciais do Cloudflare R2, URLs de banco com senha ou chaves do Resend em arquivos `.py`, testes ou configurações versionadas.
   - Toda configuração sensível deve vir de variáveis de ambiente carregadas via `app/core/config.py` (`pydantic-settings`).
2. **Segurança de Autenticação:**
   - Senhas sempre hasheadas com **Argon2id**.
   - Refresh Tokens nunca são retornados no corpo JSON; trafegam exclusivamente em cookie `HttpOnly`, `Secure`, `SameSite` e são armazenados no PostgreSQL apenas como hash `SHA-256`.

---

## 5. Padrão de Código Limpo e Quality Gate do SonarCloud

Todo código produzido deve passar sem avisos pelo **Ruff** e pelo **Quality Gate do SonarCloud**:

1. **Tipagem Estrita (Python 3.12+):**
   - Todas as funções e métodos devem possuir *type annotations* completas para parâmetros e retorno (use `X | None` em vez de `Optional[X]`, `list[str]` em vez de `List[str]`).
2. **Pydantic v2 e SQLAlchemy 2.0:**
   - Proibido usar APIs legadas do Pydantic v1 (`@validator`, `.dict()`, `class Config: orm_mode = True`). Use `@field_validator`, `@model_validator`, `.model_dump()` e `model_config = ConfigDict(from_attributes=True)`.
   - Proibido usar a API legada `session.query()` do SQLAlchemy 1.x. Use exclusivamente `select()` com `await session.execute(...)`.
3. **Métricas do SonarCloud:**
   - **Complexidade Cognitiva:** Mantenha funções enxutas e com responsabilidade única (complexidade cognitiva `< 15`). Extraia funções auxiliares privadas quando necessário.
   - **Zero Duplicação:** Evite blocos duplicados entre routers ou services; reutilize dependências em `app/core/dependencies.py` e schemas base.
   - **Zero Bugs / Zero Vulnerabilities / Zero Unreviewed Security Hotspots:** Trate exceções explicitamente (nunca use `except Exception: pass`).
   - **Cobertura Mínima:** **>= 80%** de cobertura de linhas e branches em código novo.

---

## 6. Obrigatoriedade de Testes Automatizados (`pytest` + `httpx`)

Nenhuma tarefa de implementação é considerada concluída sem seus respectivos testes automatizados:

1. **Para cada novo `Service` (`tests/unit/`):**
   - Testes unitários cobrindo o caminho feliz e todos os cenários de erro/exceção de negócio (ex.: limite de 100 MB excedido, MIME type inválido, token expirado, reuso de refresh token, permissão negada).
   - Serviços externos (`r2_client` e `email_client`) devem ser mockados (`unittest.mock.AsyncMock`).
2. **Para cada novo `Router` (`tests/integration/`):**
   - Testes de integração de ponta a ponta utilizando `httpx.AsyncClient` com `ASGITransport(app=app)`.
   - Validar status HTTP, formato do payload JSON de sucesso, cookies `HttpOnly` e contrato padronizado de erro (`error.code` e `error.message` em PT-BR).
3. **Checklist de Verificação Antes de Finalizar Qualquer Tarefa:**
   ```bash
   ruff check .
   ruff format --check .
   pytest --cov=app --cov-fail-under=80
   ```

---

## 7. Git & Branching Protocol

Todo agente de código deve seguir rigorosamente as diretrizes de [`docs/GIT_WORKFLOW.md`](docs/GIT_WORKFLOW.md):

1. **Consultar `docs/GIT_WORKFLOW.md`:** Leia o fluxo oficial de branches, escopos de commit e regras de PR antes de iniciar qualquer entrega.
2. **Nunca Commitar na `main`:** É estritamente proibido editar, commitar ou fazer push diretamente na branch `main`. Antes de modificar qualquer arquivo, verifique a branch atual (`git branch --show-current`) e crie uma branch adequada a partir da `main` (ex.: `feat/<short-name>`, `fix/<short-name>`, `docs/<short-name>`).
3. **Validar Antes de Commitar:** Sempre execute `ruff check .`, `ruff format --check .` e `pytest` com sucesso antes de criar qualquer commit.
4. **Commits Atômicos Obrigatórios:** Organize as alterações em commits pequenos, coesos e lógicos via `git add <arquivos>` seletivo, seguindo *Conventional Commits* em inglês (`<type>(<scope>): <imperative description>`, máx. 72 caracteres).

