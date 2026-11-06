<!-- docs/LLM_CONTEXT.md -->

# LLM Project Development Context

Всегда читать этот файл первым. Остальные docs — только по типу задачи.

## 1. Router

```text
              LLM_CONTEXT
                  │
        ┌─────────┼──────────┐
        ↓         ↓          ↓
    backend    frontend    infrastructure
        │         │          │
 Engineering   Frontend     Infra
 Guidelines   Guidelines  Instructions
                              │
                              └── services.yaml
                                  только если затронут shared
```

- Backend/Python/application/domain/data/tests/logging/config → `ENGINEERING_GUIDELINES.md`.
- HTML/CSS/JS/templates/browser UI → `FRONTEND_GUIDELINES.md`.
- Docker/Compose/deployment/networks/GPU/ports → `INFRASTRUCTURE_INSTRUCTIONS.md`.
- Shared service добавляется/меняется/используется → дополнительно `services.yaml`.
- Одна задача MAY требовать несколько документов.

## 2. Global invariants

1. **Перед изменением читать актуальные файлы.** Не писать по памяти, старому SHA/сообщению или предполагаемой структуре. Если доступен repository — проверить актуальный branch/HEAD, затем прочитать изменяемые файлы и связанные contracts/config/tests.

2. **Existing project > generic examples.** Guidelines задают направление, но не разрешают механически перестраивать существующий проект. При конфликте явно показать его и предложить согласованное решение.

3. **Всегда указывать relative path файла.** Если формат допускает comment, path MUST быть первой допустимой строкой комментария: shebang/служебный заголовок остаётся перед ним. Если пользователь просит полный файл — показывать полный актуальный файл.

4. **Не дублировать shared services.** Перед добавлением LLM/VLM/embedding, RabbitMQ, n8n или другого потенциально shared service прочитать Infrastructure Instructions + `services.yaml`.

5. **Не коммитить secrets.** `.env`, passwords, tokens, API/private keys не попадают в Git. `.env.example` — catalog/defaults/placeholders; `.env` — private environment-specific override.

6. **Behaviour change → tests.** Изменённая ключевая application/domain logic MUST иметь automated tests. Production bug fix MUST иметь regression test, если воспроизведение возможно.

7. **Infrastructure change → runtime discovery.** `services.yaml` описывает intended topology, не runtime fact. Проверить actual containers/networks/ports/GPU или запросить output у пользователя.

8. **Frontend change → Frontend Guidelines** и изучение existing base template/CSS/JS/tests.

9. **Shared change → Infrastructure Instructions + services.yaml.** Business project зависит от logical shared contract, а не physical model/GPU/TP/DP/host layout.

10. **Не расширять scope без необходимости.** Менять связанные файлы только если новое behaviour/rule реально на них влияет.

## 3. Priority

```text
explicit user requirements
        ↓
actual existing project / project-specific contracts
        ↓
relevant guideline from docs/
        ↓
services.yaml — only for shared
        ↓
framework defaults
```

Отклонение от MUST-rule требует явной технической причины. Неразрешённый конфликт
требований обозначать REQUIRES_OWNER_DECISION; не выбирать архитектуру молча.

## 4. Minimal preflight

Изучать только relevant sources, например:

```text
README / architecture docs
pyproject.toml / package.json
compose.yaml / configuration / .env.example
source tree / tests / migrations / CI
```

Для infrastructure/shared при наличии shell проверить relevant runtime (`docker ps`, networks/ports, `nvidia-smi`, `docker compose ps`, `./scripts/check.sh`).

## 5. Result

При существенном изменении кратко указать: изменяемые files/contracts/config, tests и команды проверки; нужен ли recreate/restart/migration/reindex.

При удалённой настройке давать небольшие проверяемые command blocks, если следующий шаг зависит от previous output.

