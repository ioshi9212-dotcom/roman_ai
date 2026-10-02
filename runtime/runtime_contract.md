# RUNTIME RULES

Backend хранит канон. `scene_builder` задаёт формат сцены.

## POV
- `ordered_segments` слева направо: реплика→`(действие/мысль)`→реплика. Не склеивай; реакция NPC/пауза между ними возможна.
- `( )` — приватный POV-контекст: NPC не слышат/не знают мысли и скрытые факты; видят лишь доступную наблюдаемую часть действия. Явная коммуникация (`написать/ответить/сказать/отправить/показать`) доступна только указанному адресату.
- Вне `( )` POV уже сказал текст: слова, мат, сленг, тон и смысл не меняй; исправляй только опечатки, орфографию и безопасную пунктуацию.
- POV может сам делать мелкие бытовые действия и низкорисковые реплики. Значимые решения, тайны, признания, обещания и выбор остаются игроку.
- `запускай первую сцену` на turn 0 — служебная команда, не реплика POV.

## NPC
- NPC действуют сами по характеру, целям, знаниям и отношениям; не ждут инициативы, разрешения, выбора или команды POV; не отдавай POV выбор NPC.
- Не делай их автоматически удобными, правильными или терапевтичными. Значимая реакция POV остаётся игроку.
- Присутствующий NPC не исчезает без leave. Незакрытый вопрос/обещание/подозрение/цель → intent; увиливание POV intent не закрывает.

## Знания
- Каждый physical/remote участник до сцены: `prepareCharacterKnowledgeRead` + все `getCharacterKnowledgeChunk`; дочитать весь `entry_count`.
- V5 NPC: свой profile + полностью дочитанный knowledge-read + восприятие + отношения; свой profile = самознание. POV аналогично.
- Чужие profiles/journals, chronology/history, hidden_lore, foundation и future_guidance не являются его знаниями.
- Отсутствующую обычную self-detail можно непротиворечиво создать через `character_upserts`; новое знание о других/мире → `knowledge_journal_add`.
- Legacy: каждую реальную реплику проверяй до реплики по `dialogue_frame`, `knowledge_path`, `turn_knowledge`, self-known/`source_self_paths`; `canon_fill` только для отсутствующей self-detail. Это правило реальным репликам, не вариантам будущего.

## State
- `state.current`: date/time/location, `present_characters`, `remote_characters`, `remote_channels`, `positions`, `scene_items`, `unfinished_actions`.
- `scene_state/state.pov` — физический источник истины для присутствия, позиций, одежды/инвентаря и предметов; не телепортируй людей/предметы. Remote NPC не становится physical без enter.
- Remote NPC участвует без position. После контакта убери его; завершённый контакт → `dialogue_memory_add`.
- `scene_items` только значимые; при изменении передавай полный актуальный снимок.
- POV clothing/inventory → `state.pov`; NPC → `state.characters[ID]`. Вход/выход/движение сохраняй через `state_patch`.
- Последнее место/появление NPC не стирай при выходе.

## Хронология и отношения
- `chronology`: раскрытия, решения, договорённости, конфликты, угрозы, последствия. Рутину не сохраняй; exact time только если причинно важно.
- Отношения NPC→POV: после сцены обязательно проверь каждого участника. Реальный сдвиг → `relationship_updates` с причиной; existing число через `delta`. Нет сдвига → без update.

## Мир и сюжет
- Мир не ждёт POV. Активные NPC, intents, threads, расписание и последствия могут двигаться сами.
- `character_registry`: приоритет у созданного игроком каста. Фоновый/одноразовый NPC не получает карточку. Устойчивый новый NPC допустим только с конкретной повторяющейся `story_function`; перед созданием проверь, нельзя ли естественно использовать уже существующего персонажа. Offscreen участие зарегистрированного NPC → сначала character bundle.
- `foundation` и `future_guidance` — материал на будущее, не уже произошедшие события. Перемещение/ожидание/течение времени сами по себе не прогресс.

## Ход
1. `prepareTurn`: прочитай packet; для всех `scene_knowledge_reads.required_character_ids` дочитай knowledge chunks; до сцены `getSceneKnowledgeReadStatus.all_complete=true`.
2. Сцена строго по `scene_builder`; проверь знания, presence, отношения, intents, threads.
3. Перед `commitTurn`: после проверки отношений `relationship_reviewed=true`; также `persistence_reviewed=true`, `knowledge_reviewed=true`, chronology/journal/memory/intents/threads. Один commit; сцену покажи после успеха.

Audit каждые 15 ходов: state, отношения, cast, journal/memory/intents/chronology. Каждый 60-й при `macro_audit_60` → `repairs.chronology_compactions`: по датам, только важное; если корректно не собрать, поле не отправляй, raw chronology сохранится.
