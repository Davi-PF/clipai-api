# Git Workflow, Branching & Atomic Commits — CLIPAÍ (`clipai-api`)

Este documento define o protocolo oficial de controle de versão, nomenclatura de branches, commits atômicos e fluxo de Pull Requests do repositório **`clipai-api`**.

---

## 1. Modelo de Branching (GitHub Flow)

O repositório adota o **GitHub Flow**:

- A branch **`main`** é a única branch permanente e deve estar **sempre estável, testada e pronta para deploy** no Railway.
- **É proibido** realizar `commit` ou `push` diretamente na `main`.
- Todo trabalho (feature, correção, refatoração, documentação ou configuração) deve nascer em uma branch criada a partir de `main` atualizada e retornar à `main` exclusivamente via **Pull Request (PR)**.

```bash
# Sempre sincronize a main antes de iniciar uma nova tarefa
git checkout main
git pull origin main
git checkout -b feat/<short-name>
```

---

## 2. Nomenclatura de Branches

Todas as branches devem ser escritas em **inglês**, em **kebab-case**, seguindo o prefixo correspondente à natureza da tarefa:

| Prefixo | Finalidade | Exemplo |
| :--- | :--- | :--- |
| `feat/<short-name>` | Novas funcionalidades de negócio ou endpoints | `feat/clips-presigned-upload` |
| `fix/<short-name>` | Correções de bugs em funcionalidades existentes | `fix/refresh-cookie-samesite` |
| `refactor/<short-name>` | Refatoração interna sem alterar comportamento externo | `refactor/clip-query-filters` |
| `docs/<short-name>` | Criação ou atualização de documentação | `docs/git-workflow` |
| `chore/<short-name>` | Dependências, CI/CD, templates ou configurações | `chore/sonar-coverage-threshold` |
| `test/<short-name>` | Adição ou ajuste exclusivo de testes automatizados | `test/clip-service-edge-cases` |
| `hotfix/<short-name>` | Correções críticas urgentes em produção | `hotfix/r2-head-object-timeout` |

---

## 3. Sincronização Cross-Repo (`clipai-api` <-> `clipai-web`)

O backend (`clipai-api`) e o frontend (`clipai-web`) evoluem de forma acoplada pelo contrato REST:

1. **Mesmo Nome de Branch:** Para funcionalidades integradas entre backend e frontend, utilize **exatamente o mesmo nome de branch** nos dois repositórios (ex.: `feat/clips-upload` em `clipai-api` e `feat/clips-upload` em `clipai-web`).
2. **Validação Pré-Merge do Contrato:** Antes de aprovar o merge do PR no `clipai-api`, verifique se houve qualquer alteração de rota, payload JSON, query param ou código de erro em [`docs/API_CONTRACT.md`](API_CONTRACT.md) e garanta que o contrato espelhado no `clipai-web` esteja sincronizado.

---

## 4. Política de Commits Atômicos & Conventional Commits

### 4.1. Formato da Mensagem (em Inglês)

```text
<type>(<scope>): <imperative lowercase description>
```

- **Máximo de 72 caracteres** na primeira linha.
- Escrever no **imperativo**, em **inglês** e sem ponto final (ex.: `add`, `fix`, `implement`, `update`, `remove`).
- **Tipos permitidos:** `feat`, `fix`, `refactor`, `docs`, `chore`, `test`, `ci`, `perf`.
- **Escopos válidos para o backend (`clipai-api`):**
  `auth`, `clips`, `collections`, `db`, `r2`, `email`, `security`, `config`, `ci`, `deps`, `docs`, `agents`, `github`, `git`.

### 4.2. O que é um Commit Atômico

Um commit é considerado **atômico** quando:
1. Representa **uma única mudança lógica coesa** (não mistura refatoração com feature nova, nem altera arquivos não relacionados).
2. Deixa o repositório em estado **executável e íntegro**, passando em `ruff check .`, `ruff format --check .` e `pytest`.
3. Permite `git revert` ou `git bisect` isolado sem quebrar outros módulos.

### 4.3. Exemplo Real de Quebra de Sprint em Commits Atômicos

Em uma sprint de upload de clipes (`feat/clips-presigned-upload`), organize o histórico em commits independentes usando `git add <arquivos>` seletivo:

```text
1. feat(db): add games, clips and tags models with alembic migration
2. feat(r2): implement async presigned url and head_object client
3. feat(clips): add clip repository and pydantic v2 schemas
4. feat(clips): implement clip upload url, confirm and crud endpoints
5. test(clips): add unit and integration tests for clip upload flow
6. docs: update api contract for clip presigned upload endpoints
```

> **Proibido:** Commits genéricos como `"wip"`, `"fixes"`, `"sprint 2"` ou `"update files"` agrupando migration, service, testes e ajustes aleatórios em um único commit.

---

## 5. Pull Requests & Estratégia de Merge

### 5.1. Critérios Obrigatórios para Merge na `main`
Todo trabalho entra na `main` exclusivamente via **Pull Request**, após:
1. Preenchimento completo do template `.github/pull_request_template.md`.
2. Passagem verde do workflow de CI (`.github/workflows/ci.yml`):
   - `ruff check .`
   - `ruff format --check .`
   - `pytest --cov=app --cov-fail-under=80`
3. Aprovação do **Quality Gate do SonarCloud** (cobertura em código novo >= 80%, 0 bugs, 0 vulnerabilidades, 0 security hotspots não revisados).

### 5.2. Estratégia de Merge e Limpeza
- Utilize **`Rebase and merge`** (preferencial, histórico linear) ou **`Create a merge commit`** para **preservar todos os commits atômicos** do PR.
- **NÃO utilize `Squash and merge`**, pois ele destrói a granularidade dos commits atômicos.
- **Delete a branch remota** imediatamente após o merge do PR para manter o repositório limpo.
