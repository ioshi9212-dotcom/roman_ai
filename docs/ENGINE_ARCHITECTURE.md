# Roman AI: карта игрового движка

## Цель

У Roman AI **один действующий конвейер хода**. Отдельные Python-модули отвечают за память, сохранения и правила, но не должны конкурировать за обработку `prepareTurn`, `commitTurn`, `resumeSession`.

Это **не** 35 независимых runtime-систем: суффикс `_runtime.py` не означает, что модуль запускает свой движок.

## Рабочий путь от Custom GPT

```text
Custom GPT Actions
    ↓
main.py                         HTTP / OpenAPI
    ↓
operation_service.py            request_id, идемпотентность, транзакции
    ↓
turn_pipeline.py                ЕДИНСТВЕННЫЙ gameplay dispatcher
    ├── session_runtime.py      чтение состояния, roster, continuity
    ├── session_documents.py    V5 файлы, канонические представления
    ├── storage.py              хранилище; /data/sessions
    ├── relationship_file_runtime.py   relationships.json
    ├── private_knowledge_runtime.py / knowledge_persistence_runtime.py
    │                            знание персонажа и его запись
    ├── npc_intent.py / npc_intent_runtime.py / story_thread.py
    ├── scene_presence_runtime.py / location_runtime.py
    └── runtime_access.py       ДВА авторских документа:
                                 runtime/rules.md
                                 runtime/scene_builder.md
```

`main.py` импортирует `turn_pipeline.continue_session` напрямую. `operation_service.py` теперь напрямую вызывает `turn_pipeline.prepare_turn_packet`, `commit_turn`, `commit_audit`. Это устраняет зависимость игровых API от того, кто последним подменил одноимённые функции в `session_runtime.py`.

## Что означает `app/__init__.py`

При импорте `app` вызываются четыре **setup-only** адаптера (для старых draft-форматов) и `turn_pipeline.install()` для совместимости со старыми импортами. Подмена `session_runtime.*` для *старых Python-клиентов и тестов* пока сохраняется, но производственные Action-эндпоинты больше на неё не полагаются.

Запрещается подключать ещё один `runtime_*.install()` поверх этого конвейера. Новую механику добавлять как функции/проверки, вызываемые явно из `turn_pipeline.py`, а не через цепочки monkey-patch.

## Какие слои устарели

Два неиспользуемых установщика отношений `relationship_growth_runtime.py` и `relationship_policy_runtime.py` удалены. Их не подключал ни `app/__init__.py`, ни действующий turn pipeline; их вызовы цеплялись друг за друга, но не участвовали в работе Actions.

Существуют старые модули `runtime_fixes_compat.py`, `living_world_runtime.py`, `scene_logic_runtime.py`, `narrative_guardrails_runtime.py` и ряд старых `install()`. **Не удаляйте их массово**: часть используется историческими тестами либо активными вспомогательными функциями. Удаление возможно только после поиска импортов/вызовов и проверки совместимости. `runtime_fixes.py`, `stability_runtime.py`, `writer_first_runtime.py` содержат активно используемые функции, несмотря на старые имена.

## Что категорически не менять при упрощении

- Формат `/data/sessions/<session_id>`, V5 JSON-документы, миграции и неизменность существующих ID.
- RAW-материалы и скрытый лор, личные знания каждого NPC и POV, режиссёрскую хронологию.
- Отношения NPC→POV и NPC→NPC, инициативу NPC, правила Skin Builder.
- Идемпотентность `request_id`, поэтапное чтение пакета, `commitTurn`, откат и восстановление.
- Точное содержимое `runtime/rules.md` и `runtime/scene_builder.md`, если изменение специально не запросили.

## Проблема Amvera (20.10.2026) отделена от рефакторинга

На Amvera присутствовали `source_override`, `state_override` и `return_snapshot` в `session_runtime.py`. В GitHub `main` этих свободных переменных не было. Следовательно, причина последовательных 500: **код на Amvera отличался от GitHub**, а не число модулей.

После развертывания проверяйте `GET /health`: `session_runtime_blob_sha` должен совпадать с Git blob SHA `app/session_runtime.py` на целевой ветке. Не удаляйте постоянный том `/data` для исправления mismatch.

## Проверки перед слиянием

`tests/test_pipeline_architecture.py` закрепляет прямой маршрут Actions, отсутствие неопределённых `*_override` и разделение двух авторских правил. Кроме него, обязательно прогонять текущий GitHub Actions `current-runtime` набор (V5 старт, память, отношения, откаты, аудит, 61/300 ходов). Отдельная ветка, зелёные проверки, затем решение о слиянии. 
