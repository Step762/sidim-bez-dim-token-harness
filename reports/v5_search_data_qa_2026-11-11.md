# P4 — Independent Search & Data QA fixes

11 ноября 2026. Ветка `feature/harness-v1`.
Исходный опубликованный checkpoint: `ac8d89ccacdb0646f21305bae88c519ae847f90a`.

**Локальная приёмка P4 пройдена. Docker-приёмка предстоит.**
Оба исходных QA-кейса решены через настоящий workflow openJiuwen, без LLM.
Разрешённая полная WSL-регрессия: **807 passed, 0 failures/errors, 7 skipped**.
Development: прежние 36 production correct и 37 Graph correct сохранены,
новых wrong нет. Новые возможности подтверждены независимыми синтетическими
задачами; прироста на фиксированных 64 development-задачах нет.

Публикуется локально проверенный checkpoint согласно отдельному разрешению P4
при недоступном Docker, а не окончательно принятый релиз V4/V5. Причина двух
исторических Docker H2 failures по-прежнему не установлена.

## 1. Причины и минимальные исправления

### Search: правильный порядок композиции

Вопрос `Who is the employer of Alice Smith's mentor?` раньше первым совпадал
с шаблоном possessive-окончания `... 's mentor`: внешний Employer ошибочно
оказывался внутри субъекта, а Mentor — запрашиваемым последним полем.
Та же проблема была в рекурсивном разборе выражений. Graph видел одновременно
possessive и of и мог отказываться от однозначной поддержанной конструкции.

Это не только потеря ответа. В независимой KB, где существуют оба пути,
старый Exact мог вернуть ответ по **Employer → Mentor** вместо **Mentor → Employer**.
Новые тесты специально задают разные конечные значения для этих путей.

Исправление:

- В общих шаблонах вопросов внешний `the employer/mentor of ...` разбирается
  прежде шаблона possessive-окончания.
- Небольшой общий `search_solver.relation_term` отделяет внешнее отношение:
  `the employer of X's mentor` = `employer(mentor(X))`,
  `X's employer's mentor` = `mentor(employer(X))`.
- Exact semantic spec и Graph compiler используют одно правило разбора,
  а не две противоречащие реализации. Исполнители, KB parser и структура
  provenance не заменены.
- Проверки идентичности, алиасов, конфликтов, неизвестных значений и ограничений
  глубины сохранены. Неизвестное отношение не превращается в догадку.
- Graph по-прежнему строит свидетельства каждого перехода и проверяет их
  независимым scan verifier. Повторное чтение snapshot, spans, hashes и отказ
  от поддельного ответа проверены тестами.

Ни имён людей, ни названий компаний, ни готовых ответов в production-правилах нет.
Graph остаётся экспериментальным: исправление грамматики не включает его в SOLVERS.

### Data: явный альтернативный префикс CSV

Вопрос `Using sales.csv, what is the average of amount where day >= 2?`
не доходил до вычислений: scalar parser принимал только начальную форму
`The file <filename>. ...`. Существующий Using был доступен отдельным формам
grouped/ranking, но не обычным агрегатам.

Добавлен узкий полный шаблон `Using <relative-file>.csv, <question>?`.
После него работают прежние aggregate/WHERE parser и streaming CSV executor.
Сохранены прежние `_SUFFIX`, точная арифметика Fraction и правила пустой выборки.
Поддержаны SUM, COUNT, MIN, MAX, AVG/average, row count и прежний sales-вопрос.
Нового DataQueryIR, SQL executor, зависимостей или LLM-обхода не добавлено.

Имя файла берётся из единственного явного префикса, а не поиском всех упоминаний
`.csv`: строковое значение фильтра само может содержать имя с `.csv`.
Неоднозначные префиксы с двумя файлами, повторные команды и неизвестные агрегаты
отклоняются. Абсолютные пути и `..` отклоняются до открытия; прежний open_text
проверяет относительный workspace, symlink/hardlink/тип/размер и гонки открытия.
FIFO в новом маршруте не блокирует чтение.

Граница поддержки остаётся явной: нужны запятая после файла и поддержанный полный
вопрос. Произвольный порядок слов или filename с пробелами не угадывается.

## 2. Фактические QA-результаты

Оба задания собраны независимо как временные task.json/workspace без
expected/check, запущены вместе через неизменённый `harness.run_tasks` и
production SOLVERS с BudgetManager(0).

| Кейс | Исходные данные | answer.txt | Проверка |
|---|---|---|---|
| Search | Alice Smith → Mentor Bob Jones → Employer TechCorp | `TechCorp` | Рядом с task.json, 0644 |
| Data | amount/day: (10,1), (20,2), (30,3); day >=2; average(amount) | `25` | Рядом с task.json, 0644 |

Итог: solved=2, skipped=0, failed=0, подтверждённый LLM-расход **0**.
Запись выполняет прежний атомарный save_answer; harness не менялся.

## 3. Независимые тесты и отрицательные случаи

Добавлен один модуль `tests/test_search_data_qa.py`: **16 методов**,
с несколькими содержательными подслучаями. До исправления 12 из 16 методов
имели неуспешные подслучаи; unittest показал 11 failures и 14 errors, поскольку
учитывает каждый провал subTest. Это не 25 разных тестовых методов.
После исправления все 16 методов прошли за 1.819 s.

Search проверяет:

- Другие имена и компании, разные окончания правильного и обратного пути.
- Эквивалентные possessive/of формулировки, смешанные двух- и трёхшаговые цепочки,
  Unicode apostrophe, явные алиасы и конечный Headquarters.
- Порядок документов и точные дубликаты; hashes и evidence spans всех фактов пути.
- Отказ verifier от поддельного ответа.
- Отсутствующие факты, unknown, конфликтующие профили, alias/heading collision,
  literal-expression ambiguity и неподдержанные альтернативы/дополнительные команды.
- Изоляцию KB разных задач и отсутствие answer.txt у нерешённой задачи.

Data проверяет:

- Изменённые filename/столбцы/значения, вложенный путь, отрицательные дроби.
- Все существующие scalar aggregates, COUNT rows, включённые границы,
  перестановку AND-условий и точные правила пустого результата.
- CSV с кавычками, запятыми внутри поля и значимыми двойными пробелами в строке.
- Отсутствующие/повреждённые CSV, дубликаты заголовков и отсутствующие столбцы.
- Два файла/неполный префикс/лишние инструкции/неизвестную операцию.
- Абсолютные пути, `..`, symlink и FIFO.
- Независимость задач, 0644 и отсутствие answer.txt после ошибки.

Эталоны этих проверок заданы вручную из независимых миров и таблиц, а не вычислены
самим parser. Сетевые подключения тестов запрещены; реальные ключи не требуются.

## 4. Полная WSL-регрессия и development

Полная регрессия запущена в физической development-only копии прежним
`diagnostics/timeout_diagnosis.py` и explicit allowlist: теперь **48 модулей**.
Внешняя сеть запрещена, HTTP unit tests используют только localhost/MockTransport.
Старые семь corpus-wide FQN исключены; исходный mixed corpus/oracles,
закрытые условия/эталоны и приватные state не читались.
Фильтрация manifest metadata выполнена до чтения task/workspace файлов.

| Проверка | Результат |
|---|---|
| Новые QA-тесты | 16/16 passed |
| Полная разрешённая WSL-регрессия | 814 discovered, **807 passed, 0 failures, 0 errors, 7 skipped**, 202.225 s |
| Production: 64 разрешённых development | **36 correct, 0 wrong, 28 skipped, 217/287 reward** |
| Graph experimental: те же 64 | **37 correct, 0 wrong, 27 skipped, 219/287** |
| MathIR experimental | 36 /0 /28, 217/287 |
| Graph+MathIR experimental | 37 /0 /27, 219/287 |
| Offline Compiler A/B с ручными mock-планами | Каждый 37 /0 /27, 219/287; не качество реальной Qwen |
| 7 полностью разрешённых qualification pairs, каждый маршрут | 5 пар 2/2, 2 пары 1/2, 0 пар 0/2; остальные пары не запускались |
| Официальные примеры на очищенных временных копиях | Safe mode **6/8**, Coding безопасно пропущен |
| Новые реальные API-запросы | **0** |

Сравнены все 64 точных ответа, status, reward и mode каждого маршрута с
публичными WSL V4-H результатами. Все совпали. Прежние **36 production correct
и 37 Graph correct** сохранены, новых development correct/wrong нет.
Расширение покрытия подтверждено новыми QA-семействами; его нельзя выдавать
за прирост на неизменившемся benchmark. Полный 128 и закрытый корпус не запускались.

Официальные ответы сохранились: Math 389/1735, Search 1983/Osaka, Data 158/284.
Все шесть файлов рядом с task.json имеют 0644. Expected/check удалены из копий.
Недоверенные Coding-задачи не исполнялись. Unit suite выполняла только прежние
доверенные синтетические coding-fixtures.

## 5. Docker, ограничения и checkpoint

`timeout --kill-after=2s 12s docker info --format '{{.ServerVersion}}'`
вернул exit 1: Desktop CLI сообщает
`The command 'docker' could not be found in this WSL 2 distro` и рекомендует
включить WSL Integration. До Engine API дойти не удалось; это не доказательство
поломки или остановки самого daemon. Автоматических изменений Docker не было.

**Docker build/smoke P4 не выполнены.** Сохранённые 976 MB относятся к прежнему
образу, а не проверке изменённых P4 исходников. Новая сборка необходима:
search_solver, plan_semantics, relation_graph и data_solver изменены.
CPU/RAM/read-only/tmpfs/caps/security не ослаблялись, Dockerfile не менялся.

После восстановления Docker отдельная приёмка может использовать:

```bash
timeout --kill-after=10s 1000s docker build --no-cache -t sidim-bez-dim:p4-check .
.venv/bin/python -B tests/docker_smoke.py --image sidim-bez-dim:p4-check \
  --output /tmp/p4-docker-acceptance-new
```

Output root должен быть новым. Smoke сохраняет реальные resource/security
проверки, default entrypoint, safe examples, права, budget guards и отсутствие
закрытых данных. Ожидаемые числа обновлены с 798/791/7 до **814/807/7** ровно
на 16 новых тестов; проверки failures/errors и исходные дедлайны не ослаблены.
Исторический H2 timeout blocker этим этапом не закрыт.

Production defaults, BudgetManager, planner, gateway, run_agent, harness,
RuntimeLimits, safe_files, Math и Coding остались прежними. MathIR/Compiler LIVE
не включён. Неизвестные вопросы, неоднозначная идентичность и отсутствующие
данные по-прежнему требуют safe skip. Поддержка произвольного языка не заявляется.

## 6. Файлы, результаты и Git

В P4 checkpoint входят только следующие **10 файлов**:

```text
README.md
data_solver.py
plan_semantics.py
relation_graph.py
search_solver.py
tests/allowed_acceptance.py
tests/docker_smoke.py
tests/test_search_data_qa.py
diagnostics/results/v5_p4/measurements.json
reports/v5_search_data_qa_2026-11-11.md
```

Исходные benchmark/holdout и исторические reports/results не изменены.
Новый JSON содержит публичные результаты разрешённых development-задач,
QA-ответы, новые source hashes и точную статистику тестов; приватных LIVE
журналов, ключей, payload production или state/intent в нём нет.

Ранее созданный `reports/v5_official_contract_2026-11-11.md` сохранён побайтно
и **не включается** в отдельный P4 commit. Поэтому после публикации P4 ожидается
один прежний untracked P2 report, а не полностью пустой git status.
Main и feature/coding-solver refs не менялись; нет merge/reset/clean/force push.

Снимок перед staging/коммитом:

```text
 M README.md
 M data_solver.py
 M plan_semantics.py
 M relation_graph.py
 M search_solver.py
 M tests/allowed_acceptance.py
 M tests/docker_smoke.py
?? diagnostics/results/v5_p4/measurements.json
?? reports/v5_official_contract_2026-11-11.md
?? reports/v5_search_data_qa_2026-11-11.md
?? tests/test_search_data_qa.py
```

Перед публикацией: explicit git add только этих 10 файлов, проверка staged bytes,
секретов и `git diff --cached --check`; обычный push только в feature/harness-v1,
сверка локального/удалённого SHA. Итоговый SHA сообщается после выполнения,
его нельзя заранее подставить в этот отчёт.

Локальные невключаемые в Git логи/guarded test view:
`/tmp/p4-search-data-scgimik9/`. Они могут исчезнуть после перезапуска;
сводка и результаты сохранены в
[measurements.json](../diagnostics/results/v5_p4/measurements.json).
Для повторного test run нужен новый output-файл:

```bash
timeout --kill-after=5s 900s .venv/bin/python -B diagnostics/timeout_diagnosis.py \
  /tmp/p4-search-data-scgimik9/suite-config.json /tmp/p4-search-data-scgimik9/new-suite.json
```

Для loopback HTTP-тестов нужны локальные sockets; внешний доступ остаётся запрещён.

**Итог: P4 LOCAL VERIFIED / DOCKER PENDING. Финальная готовность не заявляется.**
