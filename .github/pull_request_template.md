## Resumo das Alterações
<!-- Descreva objetivamente o que foi implementado ou corrigido neste PR. -->

## Tipo de Mudança
- [ ] `feat`: Nova funcionalidade
- [ ] `fix`: Correção de bug
- [ ] `refactor`: Refatoração sem mudança de comportamento
- [ ] `docs`: Documentação
- [ ] `chore` / `ci`: Dependências, configurações ou CI/CD
- [ ] `test`: Testes automatizados

## Sincronização Cross-Repo (`clipai-web`)
- **Branch / PR relacionado no `clipai-web`:** <!-- Ex.: `feat/clips-upload` / #12 ou "Não aplicável" -->

## Checklist Obrigatório
- [ ] Commits atômicos seguindo *Conventional Commits* em inglês (ver `docs/GIT_WORKFLOW.md`)
- [ ] `ruff check .` e `ruff format --check .` passando sem avisos
- [ ] `pytest --cov=app --cov-fail-under=80` passando com sucesso
- [ ] `docs/API_CONTRACT.md` e `docs/DATABASE.md` atualizados caso haja alteração de endpoint, payload ou schema
- [ ] Migration do Alembic revisada (`upgrade` e `downgrade`), se aplicável
- [ ] Nenhum segredo, token ou arquivo `.env` incluído no commit
