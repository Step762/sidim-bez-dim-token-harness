# V4-H3 — изоляция Docker full-suite failure

Этап от 10 ноября; проверки продолжены и завершены **11 ноября 2026**.
Ветка `feature/harness-v1`, HEAD `bea1ec8e4f9b7028987a78c34de3f377c2808619`.

**Вердикт: BLOCKED.** Причину исторического H2 failure установить не удалось.
В H3 все выполненные варианты прошли, включая полные V4 и Q5, однако это не
доказывает исправление. Установленной причины/фикса нет (условие G.1), а Q5
не воспроизвёл отказ (условие G.2 для baseline exception). Поэтому **commit и
push не выполнены**. Это ограничение доказательств, а не скрытый новый failure
или утверждение, что текущие H3-прогоны падают.

## 1. Исходная ошибка и точный дедлайн

Прочитаны AGENTS.md, локальный регламент, H/H2 отчёт и сохранённые логи,
tests/test_runtime_limits.py, harness, planner, safe_files, gateway/ledger и
вызываемые Math/fallback функции. Pytest-конфигурации нет: проект использует
unittest, явный allowlist47 модулей и7 FQN исключений закрытого корпуса.
Никакого pytest-runner не добавляли и не подменяли.

Целевой тест:
`test_runtime_limits.RuntimeLimitTests.test_outer_workflow_timeout_cancels_network_and_holds_reserve`.
В нём **RuntimeLimits(2, .08)**: общий тестовый лимит2 s и operation limit
**80 ms**, а не30 ms. Соседние тесты имеют20/30/40 ms; они также не менялись.
Сам целевой тест, assertions ивсе его fixtures побайтно сохранены.

В H2 два полных Docker запуска завершились **790passed/1failure/7skipped**:
на [строке100](../tests/test_runtime_limits.py#L100) отсутствовал `a/answer.txt`.
Перед этой assertion прошли len(calls)==1, frozen budget и positive held reserve.
В логе есть полный unittest итог, процесс не был убит Docker timeout/OOM:
это assertion failure сexit1, неexit124/137. Оба исторических failure сохранены
и не заменены сегодняшними зелёными результатами.

H2 не сохранил counts/report целевого вызова,stdout из его StringIO или stage
timestamps. Их нельзя восстановить из одной assertion. Неизвестно, распознали
ли a, получил ли Local Exact79 иначалось ли save_answer. Отсутствие файла само
по себе не доказывает медленный fsync, GC, Docker scheduling или solver-regression.

## 2. Pipeline и границы установленного

Проверен реальный путь: Task discovery/load → Planner.features → local decision
→ Start/SolveTask/FallbackTask/End openJiuwen → workflow result → save_answer
→ Planner.finish. Используется настоящий Workflow, не имитация.

| Этап | Что известно |
|---|---|
|Старт процесса/импорты|Присутствуют в диагностических elapsed times, но не в unit-only duration. Нет timestamps старого H2 failure для атрибуции|
|До распознавания/Planner.features|Каждая синхронная операция ограничена signal.setitimer. Нет старого report, поэтому отказ preflight не исключён|
|Local Exact|В положительных контролях возвращает79. Нет доказательства, что он завершился в упавшем H2 вызове|
|SDK workflow|Ограничен asyncio.timeout(min(operation,remaining)); возможен timeout до публикации. Старый SDK result не сохранён|
|Atomic save|После solved-result: open сdirfd/O_NOFOLLOW/O_EXCL → write → flush/fchmod0644/fsync → повторная identity check → os.replace. Чистится только временный файл, не опубликованный answer|
|Завершение|После успешной публикации harness не удаляет answer. Старый failure означает, что кassertion публикации не было либо нет данных оином внешнем воздействии; точный предшествующий этап не установлен|
|Deadline процесса|Полный unittest дошёл до конца. Нет факта убийства процесса по Docker deadline|

Production defaults — **3540 s total /30 s operation**. CLI учитывает startup
вобщем лимите;операции используют monotonic clock. Смена календарной даты не
меняет deadline. Unit80 ms не является конкурсным нормативом, но его нельзя
отключать ради checkpoint. Ни production limits, ни test limits не повышались.

## 3. Сопоставимый Q5 в Docker

В `/tmp/v4-h3-45jci8fz/q5/` извлечены **75 оригинальных файлов** опубликованного
Q5 через git show: runtime/build inputs, все baseline tests/fixtures иpublic
diagnostics support. Каждый файл сверён сcommit. Дополнительно подключён общий
санитизирующий test-view adapter. Git checkout/worktree/ветки не менялись.

Q5 и V4 запускались на **одном чистом dependency image sidim-bez-dim:v4-h2**,
Python3.12.12/openJiuwen0.1.20.post1,сзаменой только нужных Q5 source/test файлов
через readonly mounts. Это не отдельный чистый Q5 Docker build; requirements
совпадают, поэтому используется одинаковое окружение для сравнения кода.
Q5 matrix — **40модулей/669методов**, V4 —47/798. Новых V4tests вQ5не было.

Одинаковые ограничения: network none,read-only root иtest mounts,tmpfs /tmp 2 GiB,
CPU 8, RAM 16 GiB, pids 512,cap-drop ALL,no-new-privileges,HOME=/tmp,BUDGET 0.
Свежий контейнер/интерпретатор для каждого полного контроля,контролируемые
полные запуски шли последовательно. Начальные probes не имели служебного
ACCEPTANCE_CPUS;последние raw/direct проверки выровнены сH2 (`=8`). Этот ключ
используется только smoke resource audit, не runtime. Все варианты прошли,
поэтому разницу окружения не объявляем причиной H2.

### Результаты

| Проверка | Run | Passed | Failed | Errors | Skipped | Unit time,s |
|---|---:|---:|---:|---:|---:|---:|
|import-all-v4|1|1|0|0|0|3.197|
|import-only-v4|1|1|0|0|0|3.261|
|new-prefix-v4|105|105|0|0|0|7.418|
|new-plus-runtime-v4|117|117|0|0|0|9.588|
|q5-full|669|662|0|0|7|421.801|
|q5-delegate-full|669|662|0|0|7|411.738|
|q5-raw-full|669|662|0|0|7|414.406|
|v4-gc-full|798|791|0|0|7|419.513|
|v4-delegate-full|798|791|0|0|7|415.720|
|v4-direct-capture|798|791|0|0|7|424.515|
|final-tool-class|13|13|0|0|0|4.049|

`seconds` в JSON некоторых probes включает imports; отдельная `unit_seconds`
извлечена из unittest summary. Для сравнения скорости их нельзя смешивать.

- import-all/import-only: целевой тест после47импортов или только runtime module.
- new-prefix:6новых модулей до target + target,105методов.
- new-plus-runtime:теже6модулей + весь RuntimeLimitTests,117методов.
- q5-full:полныйQ5 сснимками состояния внеcritical section.
- q5-delegate-full:исходный runner,только post-failure callback.
- **q5-raw-full:полныйQ5 вообще без дополнительных наблюдателей**,точный
  __main__ CLI,изменён только явный список40baseline modules.
- v4-gc-full:полныйV4,GC-only observer;v4-delegate-full:исходный runner
  сpost-failure callback;v4-direct-capture:формаH2 __main__+ACCEPTANCE_CPUS8,
  capture во временномhelper только после assertion failure.
- final-tool-class:CLI нового диагностического инструмента,13native runtime tests.

**Минимального воспроизводимого fail-сценария не найдено.** Импорты,новые6модулей
и их комбинация склассом runtime не воспроизвели H2. Дальнейшая механическая
бисекция без положительного fail-предиката недоказательна. Зелёный наблюдаемый
full run не доказывает ниroot fix,ниobserver effect: это тоже пока гипотеза.

## 4. Проверенные факторы

| Фактор | Наблюдение | Предел вывода |
|---|---|---|
|Порядок тестов|Оригинальный allowlist/порядок сохранён вfull,отдельно105/117prefix probes|Зависимость от конкретного предшественника не установлена|
|Временный каталог|Каждый test создаёт свой TemporaryDirectory;контейнеры имеют новый/tmp tmpfs|Нет FS timings старого failure|
|ENV|BUDGET 0между tests;fake2000внутриtarget;TMPDIR/PYTHONASYNCIODEBUG/PYTHONPATH отсутствовали вstate probes;реальные ключи не передавались|Не доказано,какое состояние было вупавшем H2 моменте|
|Module state/patches|Before-target Q5 state:оригинальные math_solver.solve_math иharness.save_answer;timer(0,0)|Не исключает ненаблюдённое состояние H2|
|FD/threads|В Q5 before-target снимках4 FD, 1 thread;после столько же|Нет признака утечки в этих контролях;это не диагностика failed V4|
|CPU/throttling|Тотжеquota8;вQ5 target контрольном интервале nr_throttled/throttled_usec не выросли|Нельзя переносить наблюдение наfailed H2|
|Прогрев|Свежий процесс,import-only иimport-all target проходят|Холодный стартне доказан причиной|
|GC|В V4gc-full внутриtarget наблюдаласьодна generation0 collection **0.698 ms**,stackвworkflow background task creation|Такойпаузы недостаточно для80 ms;GCfailed-runнеизвестен|
|Readonly/tmpfs|Ограничения теже,контролируемые записиуспешны,development0644|Самопосебе read-only rootне объясняетпропуск файла|

Ни Docker scheduling,ниfsync,ниGC не объявлены установленной причиной.
Исторические Q5 WS30–40ms fsync-проблемы изV4-D — другой опыт;они не являются
доказательством baseline exception для данного80 ms Docker failure.

## 5. Инструмент и отдельная находка

Добавлен [diagnostics/timeout_diagnosis.py](../diagnostics/timeout_diagnosis.py).
Онделегирует существующему allowed_acceptance.suite,проверяет физический
sanitized view иявный subset модулей,запрещает mixed repository/corpus,
не заменяет harness/solver/deadline. unittest.TestResult.addFailure observer
исполняется **только после assertion failure**. Он сохраняет выбранные counts,
статусы/attempts,global expiry иналичие первого файла;произвольные locals,
ключи,headers,payload/документы не копируются. Exit code совпадает сrunner:
упавший тест не становится успешным.

Пять инвариантов self-check прошли:synthetic assertion остаётся упавшей,
нужные поля захвачены,private canaries исключены,существующий output не
перезаписан,запуск против оригинального mixed репозитория отвергнут.
Настоящий Docker CLI нового инструмента:13/13 runtime tests. Root нужно
монтировать каксанитизированный test view;обычный workspace сcorpus.py
инструмент намеренно не принимает.

Конфигурация для полного V4 в подготовленном Docker view — `{}`:

```bash
python -B /audit/timeout_diagnosis.py /audit/config.json /audit-result/new-probe.json
```

Должны сохраняться network none,readonly mounts,тотжеimage/cgroups иallowlist.
Новый output обязателен. Пример выбирает full matrix;для Q5config.modules
содержит40baseline имён изснимка. Нельзя пользоваться этим кодом как LIVE runner.

Отдельно проверено: `diagnostics/metamorphic.py:79` использует
`with sqlite3.connect(':memory:') as db`. После возврата oracle сохранённая
ссылка наconnection остаётся usable(SELECT1работает). Это означает отсутствие
**явного close**:context manager управляет transaction,не закрывает connection.
Предложение для отдельного resource-lifecycle улучшения:
`with contextlib.closing(sqlite3.connect(':memory:')) as db, db:`.
Нужен тест close наsuccess/exception соstrong reference. **Причинная связь
сH2 не установлена**,изменение не применялось и не выдаётся заfix таймаута.

Во временном драйвере была исправлена ошибка печати `run` вместо `discovered`
послеуспешного сохранения одного контрольного JSON. Это не failure unittest:
Docker test process завершился0,JSON содержит669/662/7. Ошибка не скрыта и
не включена как solver/runtime failure.

## 6. Повторная функциональная проверка

Свежий WSL RuntimeLimitTests: **13/13**,2.620s,безреальных credentials/API.
Полный WSL791/0/7 изH остаётся исторической проверкой того же runtime/test
source;целиком безпричины не повторялся. Все40исходных H3-файлов сохранены
побайтно;tests/test_runtime_limits.py совпадает сHEAD. Изменённой production
версии,которой нужна новаяполная WSL-приёмка,нет.

Свежий Docker development —все64разрешённыхID во всех6режимах:

| Режим | Correct | Wrong | Skip | Reward | Пары2/2 /1/2 /0/2 |
|---|---:|---:|---:|---:|---|
|production|36|0|28|217/287|5/2/0|
|graph|37|0|27|219/287|5/2/0|
|math|36|0|28|217/287|5/2/0|
|both|37|0|27|219/287|5/2/0|
|compilerA|37|0|27|219/287|5/2/0|
|compilerB|37|0|27|219/287|5/2/0|

Все статусы,точные answers,mode0644иreward совпали сWSL H. Входные копии
безexpected/check,источники не менялись. Каждаяпара —новыйworkspace/manager;
5полных,2Coding1/2,0нулевых. 13другихпар иполный128не запускались.
Compiler A/B —толькоручной MockTransport,неоценка реальной Qwen.
НеисправныйH2unit иэтиуспешныеdevelopment результаты не смешиваются.

Q5 Local Exact остаётсяproduction;Graph/MathIR experimental,Compiler offline,
LIVE выключен,batch не подключён,недоверенныйCoding выключен. BudgetManager,
planner/LLM policy не изменены. Реальных LLM/NeuralDeep requests **0**;
A1/A2/private-state/history не читались,не сбрасывались ине менялись.

## 7. Git и дальнейшие действия

**Checkpoint не разрешён достигнутыми результатами:**
G.1 — установленная причина/фикс отсутствуют;G.2 — сопоставимыйQ5 не упал.
Нет основания называть наблюдавшийсяH2отказ baseline limitation или исключать
тест изприёмки. Неудалось доказательно приписать его новойV4версии тоже.
Это **unresolved historical blocker**,неустранённый одним зелёным повтором.

Точный следующий шаг приповторении:сохранитьpost-failure counts/outcomes и
фактсозданияtemp/rename дляa;добавлятьтолькоограниченныеin-memory markers
Workflow/SolveTask/save сmonotonic/CPU-time иучётомobserver overhead.
Стабильныйположительныйfail-case нужен допоследующейddmin-бисекции.
Не увеличивать80/30 ms,неудалятьassertion,необъявлятьunsupported планверным,
не ослаблятьsecurityрадиквалификации. SQLiteclose —отдельнаяконтролируемая
инфраструктурная правка,непредполагаемаяпричина безAB-доказательства.

Основные runtimeфайлы иисходный тест совпали сQ5;всепрежние40незакоммиченных
файловV4сохранены. НовыеH3файлы:этазаписка,публичные
[measurements.json](../diagnostics/results/v4_h3/measurements.json) идиагностическаяутилита.
Ни APIkeys,ниrawLIVE,state,intents,секреты,временныеviews/Dockerlogs не добавлялись.
Исторические benchmark/holdout-эталоны не менялись. Main/Coding refs исходные;
merge/reset/clean/force/prune не выполнялись. Staging пуст;commit/push нет.

Локальныевоспроизводимые материалы:
`/tmp/v4-h3-45jci8fz/`(Q5snapshot,configs,drivers,rawlogs,GC/phase controls).
Они не публикуютсявGit и могут исчезнутьпослеперезагрузки;агрегаты сохранены
вH3JSON. Файл stage_date10ноября соответствуетимениотчёта,completed_on_client_date11ноября
отражаетпродолжениепослеполуночи.

```text
## feature/harness-v1...origin/feature/harness-v1
 M .dockerignore
 M Dockerfile
 M README.md
 M plan_semantics.py
 M search_solver.py
 M tests/docker_smoke.py
 M tests/test_runtime.py
?? diagnostics/batch_feasibility.py
?? diagnostics/compiler_replay.py
?? diagnostics/metamorphic.py
?? diagnostics/results/v4_b/
?? diagnostics/results/v4_c/
?? diagnostics/results/v4_d/
?? diagnostics/results/v4_e/
?? diagnostics/results/v4_f/
?? diagnostics/results/v4_g/
?? diagnostics/results/v4_h/
?? diagnostics/results/v4_h2/
?? diagnostics/results/v4_h3/
?? diagnostics/reward_routing.py
?? diagnostics/timeout_diagnosis.py
?? math_ir.py
?? math_ir_parser.py
?? relation_graph.py
?? reports/v4_a_architecture_2026-11-10.md
?? reports/v4_b_first_solver_2026-11-10.md
?? reports/v4_c_second_solver_2026-11-10.md
?? reports/v4_d0_rules_audit_2026-11-10.md
?? reports/v4_d_compiler_2026-11-10.md
?? reports/v4_e_budget_routing_2026-11-10.md
?? reports/v4_f_metamorphic_2026-11-10.md
?? reports/v4_g_batch_2026-11-10.md
?? reports/v4_tuesday_2026-11-10.md
?? semantic_compiler.py
?? tests/allowed_acceptance.py
?? tests/test_batch_feasibility.py
?? tests/test_math_ir.py
?? tests/test_math_ir_workflow.py
?? tests/test_metamorphic.py
?? tests/test_relation_graph.py
?? tests/test_reward_routing.py
?? tests/test_semantic_compiler.py
?? reports/v4_h3_timeout_diagnosis_2026-11-10.md
```

**BLOCKED. V4-I0 не запускался.**
