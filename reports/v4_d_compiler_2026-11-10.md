# V4-D — Verified Semantic Compiler & Regression Closure

10 ноября 2026. Ветка `feature/harness-v1`, HEAD
`bea1ec8e4f9b7028987a78c34de3f377c2808619`.
Основания: [V4-A](v4_a_architecture_2026-11-10.md),
[V4-B](v4_b_first_solver_2026-11-10.md),
[V4-C](v4_c_second_solver_2026-11-10.md),
[V4-D0](v4_d0_rules_audit_2026-11-10.md), локальный регламент,
реальные исходники и установленные API.

## Результат и решение

Рабочий **offline-only** compiler-контур реализован и проверен через настоящий
workflow openJiuwen. Существующие production-файлы, экспериментальные Graph/MathIR,
Coding, scheduler, gateway, BudgetManager и старые тесты не изменялись в V4-D.
Новые маршруты не включены в SOLVERS, CLI, Docker или production по умолчанию.

Две гипотезы реализованы: A — короткий IR; B — IR со ссылками на источники.
Ни schema-valid JSON, ни исполнимый IR, ни правильная цитата сами по себе
не принимаются за доказательство смысла. Неподтверждённый смысл получает
`semantic_unverified`, без answer.txt.

На 33 вручную заданных replay-сценариях, каждый в A и B, неверных допусков нет.
Это **свойства проверок на подготовленной выборке**, а не точность Qwen.
Ключевое ограничение: семантический допуск пока требует существующего полного
детерминированного parser. В допущенной области бесплатный Graph/MathIR уже
умеет решать задачу; продемонстрированного выигрыша compiler над этим portfolio нет.
Поэтому оплачивать такие вызовы и включать compiler в production сейчас не следует.

**LIVE выключен принудительно. Реальных модельных запросов: 0.**
Старые A1/A2 gates, квитанции, private-state, durable intents и исторический
расход не читались, не переписывались и не сбрасывались.

## 1. Три прежних timing-failure

Вместо переключения ветки исходники Q5 извлечены `git show` в отдельный `/tmp`.
Тот же неизменённый `tests/test_runtime_limits.py` запущен на Q5 и текущем runtime.
Записаны длительности реальных Workflow.invoke и RuntimeLimits.sync, затем
отдельно `os.fsync`. Assertions и таймауты не ослаблялись.

| Старый тест | Изолированный Q5, 2 повтора | Текущий runtime, 2 повтора | Вывод |
| --- | --- | --- | --- |
|slow_fallback_preparation_does_not_block_next_local_task|2 failures|2 failures|Воспроизведён на Q5|
|slow_feature_probe_is_bounded_independently|1 failure, 1 pass|1 failure, 1 pass|Третий спорный сбой тоже воспроизведён на Q5|
|slow_first_local_does_not_block_second_or_trigger_fallback|2 pass|2 failures|V4-C уже зафиксировал Q5-failure; текущая причина та же публикация|

При сбое следующая локальная задача вычислялась примерно за 3–6 ms, затем
превышала тестовый лимит 30–40 ms в `save_answer`, на `flush/fsync`, перед публикацией.
Дополнительный инструментированный текущий прогон зафиксировал fsync
49.58–57.4 ms и save 50–58 ms. В успешных контролях fsync занимал 8–22 ms;
отдельные 12 прямых Q5 save тоже прошли. Это переменная задержка durable I/O,
а не подтверждение того, что диск всегда медленный или лимит можно игнорировать.

Умышленно медленные первые операции действительно прерываются; вычисление второй
задачи не зависает. Ответ не публикуется после истечения её узкого deadline,
как и требует существующий код. Доказанной новой solver-регрессии относительно Q5
не установлено. Три timing-проблемы классифицированы как существующие ранее;
исправление runtime/тестовых допусков в этот эксперимент не подмешивается.
Полную регрессию с такими failures нельзя объявлять зелёной.

Артефакты расследования: `/tmp/v4-d-17c8ey4f/timing-{q5,current}.json`,
`timing-fsync-{q5,current}.json`, соответствующие логи, `fsync.json`.
Это новые локальные диагностические файлы, не исторические LIVE-журналы.

В первом общем прогоне V4-D дополнительно упал старый
`test_synchronous_fallback_execution_is_bounded`. Он проверен отдельно без
изменения assertions: **Q5 2/2 failures, текущий runtime 2/2 failures**.
В Q5 зафиксированы fsync48.87–49.09ms, save49.44–49.75ms при лимите40ms;
в текущем runtime fsync51.39–53.08ms, save52.56–53.76ms.
`harness`, `runtime_limits`, `llm_fallback`, `math_solver`, `BudgetManager`,
`planner`, `llm_gateway` побайтно совпадают с Q5. Это ещё один воспроизведённый
старый I/O timing-сбой, не скрытый новый дефект compiler. Логи `fourth-*.json`.

## 2. Реализованный контур

Новые файлы:

- `semantic_compiler.py`: Contract, strict wire decoder, Outcome, OfflineRoute.
- `diagnostics/compiler_replay.py`: ручные сценарии, независимые эталоны, CLI replay.
- `tests/test_semantic_compiler.py`: контракты и реальные workflow/mocked gateway tests.
- `diagnostics/results/v4_d/measurements.json`: обезличенные метрики и fingerprints.
- Этот отчёт.

```text
Task → прежний Local Exact
       → unsupported + source/preflight admission
       → прежний LLMGateway + единый BudgetManager (только MockTransport)
       → strict_json → versioned schema → MathIR / Query
       → полное соответствие условию → evidence (B)
       → детерминированный executor → verifier
       → прежний harness.save_answer / safe skip
```

`Contract.prepared()` возвращает существующий Prepared. OfflineRoute временно
подставляет подготовку в planner и fallback в harness в явном context manager;
после выхода оба hook восстановлены. Вызовы идут через существующие компоненты
Start → SolveTask → FallbackTask → End и Workflow.invoke openJiuwen 0.1.20.post1.
Нет второго gateway, workflow-имитации, отдельного ledger или исполнения Python
из ответа модели. Обычный запуск проекта этот модуль не импортирует.

### Wire contract

A: `{"version":1,"kind":"math|search","ir":...}`.
B: тот же envelope плюс обязательный массив `evidence`.
Отказ: только `{"version":1,"kind":"math|search","status":"unsolvable"}`.
Лишние поля, дубли JSON-ключей, bool вместо version, неизвестный kind/operator
и неправильные типы не допускаются.

Math IR: `expr`, `unit`; literal — строковое точное число и canonical unit;
operation — whitelist `op`, список `args`, только для round/convert — `option`.
Переиспользуются Literal/Expr/MathIR, Fraction, unit table, validator, executor
и serializer V4-C. Поддерживаются арифметика, mean/sum/abs, явные проценты,
порядок действий, ограниченная степень, явная конверсия единиц и half-up.
Не добавлены уравнения, неизвестные величины, неявные единицы или новая NL-грамматика.

Search Query: точный root label/явный alias; упорядоченные hops
`mentor|employer × forward|inverse`; projection `label|born|founded|headquarters`.
Используются существующие Snapshot, Graph, Query, witness facts и full-scan verifier.
Одноимённые профили не объединяются, неподтверждённые ребро/root/alias не создаются.
Проверяется весь запрос, направление, порядок и projection; правильное число
с неправильным путём не является допустимым решением.

Data Analysis сохраняет прежний fallback-контракт, новый typed compiler для CSV
в этом этапе не создавался. Недоверенный Coding по-прежнему отключён.

### Evidence и ограничения

B возвращает ссылки `source`, SHA-256 исходного текста, `[start,end)` в символах.
Каталог формируется только из допущенных источников: instruction и bounded KB.
Он включает provenance фактов и заголовков. Ответ должен покрыть instruction,
использованный путь, терминальные факты, identity headings и relevant aliases.
Чужие/stale ссылки, дубликаты, пропущенное основание отклоняются. Ссылка не
используется как путь для произвольного чтения. Изменение KB после подготовки
обнаруживается повторным чтением и проверкой snapshot.

Оба варианта получают одинаковые факты и evidence catalog; A не возвращает ссылки.
Это сравнение именно формата ответа. Оно не доказывает, что передача каталога
в A оптимальна по токенам. Эталонный IR и вычисленный ответ в prompt не передаются.

Envelope ≤24000 символов; instruction ≤8000; старый gateway допускает контекст
≤18000 символов; дерево ≤256 узлов, depth ≤32, aggregate 2–16, hop ≤8,
evidence ≤128. Литералы и exact arithmetic наследуют bounded fractions,
exponent/unit/rounding ограничения V4-C. Ни eval/exec, ни произвольных операторов.
Превышение допуска — отказ, не обрезание смысловой части.

При финальной проверке устранён дефект **нового** prompt: общий safety prefix
предлагал legacy `status`-only отказ, а новый decoder требует version/kind/status.
Теперь инструкция предлагает один непротиворечивый envelope; добавлен отдельный тест.
Production prompt и validator не менялись.

## 3. Семантическая проверка и её пределы

Math заново компилирует полное поддержанное условие доверенным parser и сверяет
операторы, числа, units, порядок, output unit и rounding. Даже случайно совпавший
конечный ответ не оправдывает другое дерево. Search сверяет Query с полным
условием, затем независимый full-scan verifier заново проверяет источники,
identity, witnesses, противоречия в relevant scope и единственность.

Preflight приватно вычисляет канонический результат для domain/proof admission.
Он не отправляется модели. Это полезная защита, но означает, что для допущенного
языка модель уже не нужна, а произвольное новое NL-условие не стало проверяемым.
Для Math runtime-reference и candidate используют один численный executor:
это **не независимая вторая арифметическая реализация**. Независимость oracle
обеспечена ручными задачами/значениями; дефект общего parser/executor вне выборки
всё ещё может затронуть оба вычисления. В тесте искажённый после подготовки
результат executor отвергается. Общий parser остаётся доверенной базой.

В Search независим алгоритм проверки пути/фактов (scan вместо индексов),
но shared source parser и full-question grammar тоже остаются доверенной базой.
Evidence проверяет привязку к источнику, а не понимание любой английской фразы.
Неканоническое, хотя математически эквивалентное дерево может быть отклонено:
общей системы алгебраических доказательств здесь нет. Это явное ограничение coverage,
а не основание называть любой исполнившийся план правильным.
Негативные evidence-тесты не заменяют семантические тесты.

Статусы разделены: `json_rejected`, `schema_rejected`, `semantic_unverified`,
`evidence_rejected`, `executor_rejected`, `verification_rejected`,
`model_abstained`, `accepted`. Preflight-refusal учитывается до JSON/schema,
а отказ gateway не называется ошибкой математического executor.

## 4. Независимый replay: A против B

33 авторских сценария, 66 проверок. Планы написаны вручную как wire JSON,
не получены compile_grammar/compile_existing/compile_query. Ответы тоже заданы
вручную. Есть arithmetic/unit/order/rounding ошибки, потерянное и выдуманное число,
неправильная identity/relation, ambiguous inverse, конфликт, partial same-name,
malformed/duplicate JSON, неизвестный operator, injection, большой/deep AST,
неподтверждённый смысл и отказ модели. Корректные варианты включают exact Math,
forward/inverse цепочку, явный alias и нерелевантную injection-строку в KB.

| Метрика | A: IR | B: IR + evidence |
| --- | ---: | ---: |
|Корректный внутренний JSON / поступившие ответы|25/27|25/27|
|Schema-valid / поступившие ответы (включая malformed)|20/27|20/27|
|Schema-valid / корректный JSON|20/25|20/25|
|Semantic-valid / semantic evaluated|10/19|10/19|
|Executor correct / executed|10/10|9/9|
|Accepted-answer precision|10/10|9/9|
|Coverage / answerable scenarios|10/27 (37.04%)|9/27 (33.33%)|
|False acceptance / must-reject cases|0/23|0/24|
|Сумма символов подготовленных ответов|14087|26472|

6 сценариев останавливаются до модели из-за недоказанного смысла, поэтому
поступивших ответов27, а не33; корректных JSON из них25. Общий schema yield20/27 и условный20/25 приведены отдельно. После schema ещё один ответ — явный abstain,
поэтому semantic evaluated=19. Отклонённые до execution ответы не включены
в executor correctness. Из33 условий27 имеют ручной определённый ответ;
некоторые повторены с разными неисправными предложениями, это **не27независимых
случайных задач**. Покрытие показывает устойчивость к этим предложениям.

В `bad_evidence` IR и ответ правильны: A закономерно принимает его, B отвергает
повреждённую ссылку. Это не false acceptance A. B проверяет дополнительный
контракт, поэтому число must-reject cases другое. Оба варианта отвергают
семантически неверные IR. Размер в символах — не токенизация и не прогноз Qwen.
Injection проверяет инертность данных и local admission, не устойчивость реальной
модели: настоящая модель здесь не запускалась.

## 5. Стратегии на одинаковых входах

Использован существующий benchmark.evaluate/worker с временными runtime-копиями.
Q5 взят из указанного git commit. Graph/MathIR подставлены только в этих копиях;
benchmark-исходники/эталоны не редактировались. В compiler arms модельный ответ
вводится через httpx.MockTransport, тот же gateway и один manager на весь запуск.
План для разрешённого search_d12 написан вручную, не содержит ответа. Usage
100 input +100 output =110 расчётных единиц — **синтетический**, не измеренный.

| Development64 | Correct | Wrong | Skipped | Reward | Model mock calls |
| --- | ---: | ---: | ---: | ---: | ---: |
|Q5 Production|36|0|28|217/287|0|
|Production + Graph|37|0|27|219/287|0|
|Production + MathIR|36|0|28|217/287|0|
|Production + оба|37|0|27|219/287|0|
|Production + compiler A replay|37|0|27|219/287|1 (110 synthetic units)|
|Production + compiler B replay|37|0|27|219/287|1 (110 synthetic units)|

Все36 прежних development-correct сохранены с тем же ответом во всех режимах.
Новая относительно Q5 correct — только search_d12, ранее уже полученная V4-B;
это не новый прирост V4-D. Полные Q5 57/128 и306/503 остаются **историческими**,
новый full score и квалификация20пар с закрытыми входами здесь не измерялись.

Дополнительно все33 ручных сценария пропущены через полный workflow
Local Exact→fallback→save в шести режимах. Один manager на arm:

| Synthetic workflow cohort | Correct | Wrong | Skipped | Mock calls | Synthetic spent |
| --- | ---: | ---: | ---: | ---: | ---: |
|Production|0|0|33|0|0|
|+Graph|7|0|26|0|0|
|+MathIR|20|0|13|0|0|
|+оба|27|0|6|0|0|
|Compiler A|10|0|23|27|2970|
|Compiler B|9|0|24|27|2970|

В этом специально подобранном cohort production-формулировки не поддержаны;
результат0/33 не заменяет реальный benchmark36/64. Одинаковые условия с разными
поломанными ответами повторяются, поэтому нельзя трактовать27/33 как внешний
рейтинг обобщения. Но проверяется важное свойство: deterministic portfolio
решает поддержанный смысл, даже когда предложенный модельный IR неверен.
Compiler честно пропускает неверные предложения и списывает известный usage,
не делает repair/retry. Во всех arms wrong0; сохранённые файлы имеют0644,
резерв после подтверждённого usage0, бюджет не заморожен. При неизвестном usage
другие негативные тесты требуют сохранить резерв и заморозить платный маршрут.

### Граница development / holdout

В измерениях разрешён только development64; refs получены из development-строк
исторического опубликованного отчёта, проверены SHA входов manifest. Чтение
закрытых task paths, оригинальных mixed references.json и mocks.json блокируется
аудит-hook. Для старых diagnostic tests вручную восстановлены только3development
mock-плана. Закрытые43 задачи не запускались и не использованы для выбора IR/oracle.

Ограничение чистоты исследования: при поиске служебной функции генерации один
`rg` по исходнику corpus вывел также строки смешанных mock-fixtures, включая
не-development. Этот вывод не использовался для реализации, replay-эталонов
или измерений; сами закрытые task-файлы и оригинальные mixed reference/mock JSON
не открывались. Поэтому абсолютное утверждение «ни одного фрагмента holdout-
генератора не было видно» было бы неверно. Следующий независимый внешний тест
должен учитывать эту оговорку; раскрытые строки здесь не воспроизводятся.

## 6. Usage, попытки, LIVE и официальный Proxy

OfflineRoute принимает **только существующий LLMGateway с MockTransport**.
Реальный транспорт отклоняется до reserve/complete; нет env/CLI live-переключателя.
Вызов один; task_id помечается до await. После timeout/cancel того же объекта
route повтор запрещён; неизвестный usage сохраняет резерв и freeze общего manager.
Уже сохранённый Local Exact ответ не теряется. Невалидный IR с известным usage
оплачивается, но не публикуется. При нехватке полного резерва HTTP не начинается.

Process-local attempted set **не durable LIVE intent**: после перезапуска
он не является защитой от повторов. Поэтому просто заменить MockTransport
на реальный нельзя. Старый one-shot pilot не расширен и A2 не разблокирован.
Runtime fingerprints старого Docker allowlist не включают новые MathIR/compiler;
их нельзя выдавать за проверку именно нового контура.

По локальному регламенту «Модели и учёт токенов»: cost = input/10 + accounted
output, reasoning полностью, без двойного учёта уже включённого reasoning и без
cache discount. BudgetManager хранит обе стороны в tenths, лимит10×BUDGET —
это эквивалентный внутренний масштаб, не ошибка×10. Настройки не менялись.

В официальном примере Docker-запуска нет `-e BUDGET`; production автоматически
включает LLM только с BUDGET+LLM_BASE_URL+LLM_API_KEY. Самовольного default100000
не добавлено. Реальная финальная выдача BUDGET и контракт **официального** Proxy
не подтверждены; прежний NeuralDeep-пилот не доказывает их эквивалентность.

Перед первым отдельно разрешённым реальным compiler-вызовом обязательно:

1. Подтвердить окончательную команду judge, передачу/единицу BUDGET, отдельный
   ограниченный pilot-budget и сохранение исторических155.9; ничего не сбрасывать.
2. Получить endpoint/Chat Completions/model alias контракт официального Proxy;
   проверить max_tokens cap, стандартный usage, semantics reasoning/total и
   ошибки/квоты. Одна фиксированная разрешённая noreason-модель; chaining не включать.
3. Зафиксировать source fingerprints **всех** новых runtime/diagnostic модулей,
   wire contract, единственную development-задачу и запрет holdout.
4. Создать отдельно проверенный durable one-shot intent **до HTTP**, atomic fsync,
   lock/exclusive claim, restart/recover без resend неизвестной попытки. Старые
   A1/A2/one-shot gates не обходить и исторические журналы не перезаписывать.
5. Явное разрешение пользователя на модель/число запросов/лимит. По умолчанию0;
   никакого запуска через production CLI в обход диагностических ограничений.
6. Один manager, полный безопасный reserve до HTTP, подтверждённая допустимая
   оценка input, ограниченный output и timeout, общий deadline, без retry/repair.
   Unknown usage, reasoning surprise, overrun,401/429/timeout — stop/freeze,
   без второго запроса до ручного выяснения.
7. Семантический admission, JSON/schema/evidence, executor/verifier остаются
   обязательными. Private ограниченная диагностика, ключ только из env, никаких
   Authorization/payload/реальных документов в Git или публичном отчёте.
8. Проверить атомарный answer.txt0644 и deadlines через тот же workflow;
   закрыть регрессионный/packaging gate. Для контейнера отдельно добавить явные
   build-inputs и выполнить smoke; старый образ не считается новой приёмкой.

Без этих действий проект **не готов к LIVE compiler**. Здесь намеренно нет
готовой команды отправки реального запроса: текущий адаптер её запрещает.

## 7. Финальная проверка

**25/25 целевых compiler-тестов прошли**, 1.811s. Проверены оба контракта,
ручные негативные предложения, task isolation, mutation источников, live-disabled,
сохранение0644, budget-block, no-retry, подтверждённый usage до отказа,
unknown usage freeze и отмена выполняющегося mock-запроса с сохранением резерва.
Повтор raw replay после финального изменения prompt дал те же66результатов.
Все6development arms и6workflow cohort arms повторены на финальных исходниках;
итоги таблиц сохранены, source fingerprints обновлены.

**Финальная доступная WSL-регрессия:765 обнаружено,757passed,1failure,
7skipped,0errors,185.732s.** Все25новых compiler-тестов прошли.
Единственный failure:
`test_runtime_limits.RuntimeLimitTests.test_interrupted_publication_preserves_old_file_and_other_answers`.
Тест с30ms deadline прошёл проверку сохранности прежнего b/answer.txt, но не
получил оба новых соседних файла. Отдельно тот же неизменённый тест на Q5 дал
1failure/1pass: в failing case fsync третьей задачи46.34ms, save47.05ms.
Текущий изолированный контроль2pass; fsync7.89–12.21ms. Это подтверждённая
нестабильность прежней durable publication и на baseline, не новый compiler-defect.
Общий failing run не заменяется зелёным результатом изолированного контроля.

Предварительный общий прогон до финальной коррекции prompt:764 обнаружено,
753passed,4failures,7skipped,183.321s. Упали3первоначальных timing-теста плюс
synchronous_fallback_execution; в финальном прогоне эти4прошли. Повтор общего
набора сделан из-за исправления нового prompt/добавления25-го теста,
**не для поиска случайного зелёного прогона**. Больше полная регрессия не повторялась.
Исходные timeout tests/production limits/assertions не изменены.

Семь skipped — исходные corpus-wide проверки, которым необходимы закрытые
задачи/эталоны. Внешний guarded loader исключает их явно, не меняя файлы тестов;
они не засчитаны в passed. Остальные используют development-only metadata и
loopback-only network guard. Генерация полного corpus и full128 не выполнялись.

Окружение: Python3.12.3, openJiuwen0.1.20.post1, httpx0.28.1.
Docker не пересобирался: production не менялся, новые experiment modules не
добавлялись в build allowlist. Ни старый образ, ни прежние669Docker-тестов
не объявляются Docker-приёмкой V4-D. Полной зелёной release-приёмки нет.

`git diff --check`, syntax/whitespace новых файлов и pattern-scan секретов
прошли. SHA всех 31 унаследованного файла совпали со снимком начала этапа.
Новые machine metrics: [measurements.json](../diagnostics/results/v4_d/measurements.json).
Исторические benchmark/holdout/state не изменялись.

Команды воспроизведения новых проверок без сети:

```bash
.venv/bin/python -B -m unittest discover -s tests -p test_semantic_compiler.py -v
.venv/bin/python -B -m diagnostics.compiler_replay --output /tmp/v4-d-replay-NEW.json
```

CLI replay откажется перезаписывать существующий output. Для текущей guarded
регрессии сохранён `/tmp/v4-d-17c8ey4f/run_suite_final.py`, для6arm-сравнения —
`measure_final.py`; их запуск ограничивался `timeout900s` и `timeout300s`.
Development-only bootstrap, source snapshots и логи сохранены в том же каталоге.
Это локальные вспомогательные файлы измерения, не новый production runner.


## 8. Git и следующий приоритет

Все исходники и унаследованные отчёты из SHA-снимка начала V4-D сохранены
побайтно; новые файлы перечислены выше. Нет новых зависимостей. Нет реальных
ключей, private payload/receipts/intents в новых файлах. Staging пуст;
HEAD/ветка не менялись. Commit/push/merge не выполнялись.

```text
## feature/harness-v1...origin/feature/harness-v1
 M .dockerignore
 M Dockerfile
 M README.md
 M plan_semantics.py
 M search_solver.py
 M tests/test_runtime.py
?? diagnostics/compiler_replay.py
?? diagnostics/results/v4_b/
?? diagnostics/results/v4_c/
?? diagnostics/results/v4_d/
?? math_ir.py
?? math_ir_parser.py
?? relation_graph.py
?? reports/v4_a_architecture_2026-11-10.md
?? reports/v4_b_first_solver_2026-11-10.md
?? reports/v4_c_second_solver_2026-11-10.md
?? reports/v4_d0_rules_audit_2026-11-10.md
?? reports/v4_d_compiler_2026-11-10.md
?? semantic_compiler.py
?? tests/test_math_ir.py
?? tests/test_math_ir_workflow.py
?? tests/test_relation_graph.py
?? tests/test_semantic_compiler.py
```

Приоритет следующего отдельно заданного эксперимента — **измерить семантический
допуск на новых независимых перефразировках и композициях**, а не платить за
повторную компиляцию уже детерминированно решаемого языка. Заморозить небольшой
development-only набор с ручными oracle до правок; разделить ошибки parser,
meaning admission, IR, executor и identity. Проверить local portfolio первым,
compiler A/B лишь там, где можно независимо подтвердить смысл.

Если независимый verifier по-прежнему требует уже готового canonical IR,
разумная альтернатива — оставить compiler экспериментальным, принимать проверенные
локальные Graph/MathIR только после отдельной release-регрессии и уточнить Proxy/
BUDGET. Если появится действительно новый доказуемый semantic contract,
готовить отдельный ограниченный one-shot Qwen-пилот после закрытия P0, с новым
разрешением. Это план, не начатый следующий этап.
