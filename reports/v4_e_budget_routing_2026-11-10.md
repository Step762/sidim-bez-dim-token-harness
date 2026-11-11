# V4-E — Reward-Aware Routing & Proxy Readiness

10 ноября 2026, Asia/Novosibirsk. Ветка `feature/harness-v1`, HEAD
`bea1ec8e4f9b7028987a78c34de3f377c2808619`.
Основания: локальный регламент, [D0](v4_d0_rules_audit_2026-11-10.md),
[D](v4_d_compiler_2026-11-10.md), текущие planner/budget/gateway/workflow,
явно разрешённые development-артефакты и новые синтетические тесты.

## Решение

**Production оставлен без изменений. LIVE не включён.** Создано воспроизводимое
офлайн-сравнение шести политик на прежнем replay, Planner и BudgetManager;
новый глобальный optimizer не внедрён. Политики показывают разный результат
при разных ограничениях; нет данных о вероятности успеха настоящего Qwen,
позволяющих обосновать замену текущего ранжирования.

Свежая проверка Q5 и текущего production на разрешённых development64:
**36 correct, 0 новых/изменённых ответов, 28 skipped, 217/287**;
в configured mock режиме тот же результат, **0 модельных вызовов**.
Все64 исхода/содержимых файлов совпали с опубликованными development-результатами
V4-D и друг с другом. Graph37/219 и MathIR остаются отдельными выключенными
экспериментами; их баллы production не приписываются.

Синтетические432 симуляции не являются real-Qwen accuracy/cost benchmark.
Реальных LLM-запросов во всём V4-E: **0**. Формула стоимости не менялась.

## 1. Что действительно делает текущий planner

Источники: `planner.py:features, Planner.next, estimate, finish, report`,
`harness.py:_execute_tasks`, `llm_fallback.py:prepare`, `llm_gateway.py:complete`,
`budget_manager.py:reserve,settle,mark_uncertain`.

1. Обнаружение/чтение задач выполняется прежним безопасным harness.
   `reward` загружается из task.json, не связан со сложностью по правилам.
2. Features — только синтаксис/метаданные: native form, наличие workspace/файлов,
   blocker; complexity = `1 + число цифровых фрагментов//3 + mentor/employer
   mentions + and/where/through/between mentions + длина текста//1000`.
   Это не измеренная трудоёмкость и не оценка качества модели.
3. Все допустимые local probes проходят до платной очереди. Один probe на задачу.
   Native local имеет приоритет, затем `reward × условная local probability`,
   complexity и стабильный путь. В локальном sort используются9/10 и1/4 —
   инженерные константы, не подтверждённые вероятности судьи.
4. Только отказ `unsupported` может получить fallback. Missing/invalid/conflicting
   evidence, unsafe, timed_out, unknown type не отправляются модели. Уже существующий
   непроверенный answer.txt не объявляется успехом и не заменяется платной догадкой.
5. До платной очереди: generation/accounting contract, прежний semantic preflight,
   bounded context, оценка input. При нехватке доказательств fallback не допускается.
6. Paid priority: `reward × p / forecast_cost`. p начинается с3/4 Math/Data
   и4/5 Search, делённых на `1+complexity/10`. Forecast в tenths:
   `estimated_input + 5×max_tokens`, то есть input/10 + половина output cap.
7. После≥3 завершённых settled попыток того же task_type/model прогноз расхода
   использует observed input ratio и mean accounted output. p сглаживается по
   факту `state=solved`. **Solved означает принятие валидатором и сохранение,
   не подтверждение правильности скрытым judge.** Текущий A уже адаптивен.
8. Admission независимо требует **полного** `estimated_input/10 + max_tokens`,
   не половины cap и не ожидаемой стоимости. В gateway до HTTP создаётся
   единственный фактический резерв в общем manager; planner только предварительно
   проверяет доступность, второго списания нет. Не поместившаяся задача не мешает выбрать более дешёвую.
9. Максимум одна paid attempt/task за этот запуск, без repair/retry.
   При unknown usage общий manager frozen, резерв удерживается; другие paid
   останавливаются. Локальные результаты уже записаны и сохраняются.

### Фактическая таблица маршрутов

| Условие | Маршрут | Что расходуется | Результат/следующий шаг |
| --- | --- | --- | --- |
|Native/поддержанная Math, Search, CSV|Local Exact, настоящий workflow|Локальное время, 0 LLM|Проверенный ответ сразу save/fsync/replace,0644|
|Неподдержанная форма, preflight доказуем|Deferred LLM после всех local|Полный reserve до одного HTTP|Строгий plan validator и локальный executor, затем save|
|Недостающие/противоречивые данные|Safe skip|Время проверки,0 LLM|Не угадывать факт/число|
|Повреждённые/опасные файлы|Safe skip / failure чтения|Локальная проверка|Остальные задачи продолжаются|
|Coding без explicit trust|Unsafe skip|0 LLM, нет исполнения кода|Не включать trust автоматически|
|Неизвестный task_type|Unsupported type|0 LLM|Остальные типы продолжаются|
|BUDGET отсутствует, CLI по умолчанию|Local only, manager0|0 LLM|Нет предполагаемого default100000|
|BUDGET0, явный disable, неполная конфигурация|Local only|0 LLM|Платный маршрут выключен|
|Положительный BUDGET+URL+KEY|Автоматически разрешён bounded fallback|Только после preflight/reserve|Не означает разрешение на диагностический LIVE/A2|
|Малый остаток/дорогой запрос|Budget blocked|Новых расходов нет|Выбираются ещё доступные задачи; local не отнимается|
|HTTP failure/timeout после reserve/неполный usage|Frozen paid path|Unknown cost+удержанный reserve|Никаких повторов; известные ответы не удаляются|
|Подтверждённый usage, плохой IR/finish/content|Rejected paid result|Фактический известный расход|Без нового запроса и answer.txt|
|Дедлайн операции/запуска|Timed out / time exhausted|Время; in-flight LLM cost может быть unknown|Сохраняются уже опубликованные ответы|

Свежие конечные состояния64:36solved,16unsafe,6insufficient_evidence,
2invalid_data; ещё4при local/BUDGET0 имеют budget_blocked, при configured mock —
unsupported из-за невозможности надёжного fallback context. Платных попыток0.
Состояние budget_blocked само по себе не доказывает невыполнимость условия.

### Конкретные ограничения и риски

| Недостаток | Наблюдение и последствие | Решение V4-E |
| --- | --- | --- |
|p не калибрована|3/4,4/5, complexity heuristic; единственный исторический real development успех Math недостаточен для остальных типов|Не объявлять EV истинным; интервальная чувствительность|
|Accepted ≠ correct|Адаптация может повышать p после ложного acceptance|Отдельно хранить validation и независимую offline grade; не кормить judge oracle planner|
|Cost forecast ≠ safe reserve|Вероятно дешёвый фактический ответ может не поместиться полным cap|Сохранить guard; измерять cap/input на разрешённых трассах|
|Нет гарантированного remote input bound|UTF-8 bytes+1024 framing — эвристика, Proxy может добавить overhead|Freeze при underrun estimate; подтвердить контракт перед LIVE|
|Time cost free tier|Долгие local probes могут занять значительную долю3540s|Измерять общий deadline; синтетический slow профиль показывает time-binding|
|CSV/KB размер не равен complexity|Длина instruction/цифры не описывают число строк/документов или время fsync|Не выдавать feature за точную прогнозную модель|
|Нулевой reward может получить paid attempt|В финальном reward/tie-break такой вызов невыгоден, но нулевой reward не исключает задачи квалификации2/2|Не угадывать фазу по количеству задач; отдельный final-only отказ требует подтверждённого контракта|
|Некорректный полный env останавливает CLI до local|Существующая намеренная fail-closed политика и тест|Оставить; проверить конфигурацию до конкурсного запуска|
|Durable I/O30ms-tests нестабильны|V4-D воспроизвёл на Q5; общий runtime30s не равен30ms теста|Не ослаблять deadlines/assertions, честно указывать незелёный gate|

Отдельного wrong penalty в score нет: неправильная попытка не приносит reward,
но тратит время/токены. При равном reward лишние токены ухудшают tie-break.
Это экономический риск, не штраф за wrong. Строгий validator остаётся политикой
безопасного качества; его не ослабляли для роста reward.

## 2. Шесть политик на одинаковых offline данных

Новый `diagnostics/reward_routing.py` расширяет только диагностический replay.
Переиспользованы прежний цикл replay, Planner, BudgetManager, full-reserve gate,
freeze и deadline. Нет нового production scheduler, сети, клиента или LIVE-флага.
Каждый arm получает копию одной заранее замороженной outcome/usage tape.

| Arm | Локальная очередь | Платная очередь |
| --- | --- | --- |
|A Q5|Прежняя|Прежний reward×adaptive p / adaptive forecast cost|
|B Local-first|Native first, меньшая complexity|Меньший forecast cost first|
|C Reward/cost|Прежняя|Reward / фиксированный forecast cost, без выдуманного p Qwen|
|D Conservative EV|Прежняя|Reward×нижняя граница p / полный safe reserve|
|E Два этапа|Прежняя|Сначала cost≤median eligible cost, затем дорогие из остатка; внутри reward/cost|
|F Adaptive conservative|Прежняя|Как существующая адаптация A, с вычетом sensitivity-band из p|

Во всех arms local-first сохранён: это не сравнение «local vs сразу LLM».
E — фиксированная медианная граница, не найденный оптимум; после settled usage
неиспользованный резерв доступен следующей задаче. F переиспользует уже имеющееся
обучение на расходе/validator outcome, не независимую новую ML-модель.
При нулевой нижней границе D/F продолжают допустимые попытки с равными score,
а не объявляют задачи невозможными. Возникает tie по стабильному ID — видимый риск
слишком широкого интервала. При band0 F в точности повторяет A.

Источник: существующий `synthetic_cohort`, 100задач, reward назначен независимо
от complexity, seeds20261109 и20261110. Профили cheap, long_output,
time_pressure, unknown_usage. Budgets0/10000/100000; bands0/1/4/1/2;
6policies ×8cohorts ×3budgets ×3bands = **432симуляции**.
Это интервалы чувствительности вокруг прежнего **некалиброванного synthetic
prior**, не доверительные интервалы настоящего Qwen. Validated/correct на этой
tape связаны по конструкции, поэтому synthetic wrong0 не измеряет false acceptance.

### Результаты: средний reward двух seeds, band1/4

| Профиль и BUDGET | A | B | C | D | E | F |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
|cheap,100000|291|291|291|291|291|291|
|cheap,10000|182.5|128|185.5|173.5|169.5|171.5|
|long_output,100000|263|198.5|262.5|259|204.5|260.5|
|long_output,10000|109.5|94|116|109.5|113.5|109.5|
|time_pressure,100000|289|262|288.5|288.5|285.5|289|
|unknown_usage,100000|234|185|255|243|204|221|

При BUDGET0 во всех arms сохраняются бесплатные локальные успехи,0paid calls.
Остальные комбинации и counts/cost/time/freeze сохранены machine-readable.

По72условиям на политику (включая повторяющиеся bands для не зависящих от них
политик), диапазон **reward delta относительно A**:

| Политика | Min delta | Max delta | Лучше / равно / хуже A |
| --- | ---: | ---: | --- |
|B|-91|0|0 /30 /42|
|C|-3|42|15 /42 /15|
|D|-94|42|8 /40 /24|
|E|-91|8|12 /36 /24|
|F|-39|0|0 /52 /20|

Это описательные сценарии, не72независимых статистических наблюдения и не
значимость победы. Ни одна альтернатива не доминирует A на всём наборе;
F-band0 совпадает с A, положительная осторожность может ухудшить ранжирование.
Даже C, полезный на отдельных дефицитных бюджетах, иногда хуже A.
**Не внедрять C/D/E/F в production на основании этих чисел.**

### Достаточно ли100000?

В cheap A обработал **54/54 и57/57 eligible paid задач**, расход49472.8 и49903.6,
резерв0, время заметно меньше часа. Остальное — local либо Coding-off, а не
задачи, пропущенные из-за бюджета. Здесь100000достаточно для всех допустимых
модельных попыток; precision модели и маршрутизация важнее knapsack-оптимизации.

В long_output A при100000сделал28/54 и30/57попыток, расход97294.3 и99038.7;
26 и27задач заблокированы reserve. В time_pressure A использовал около41139
единиц, но дошёл до3540s; время становится ограничением раньше токенов.
В unknown_usage разный порядок меняет, сколько награды получено до frozen gate;
неизвестный расход не объявляется нулевым, удерживается резерв.

Вывод условный: пока нет настоящих input/output/latency/acceptance трасс по
типам/моделям, **нельзя считать токены главным bottleneck, но нельзя и обещать,
что100000покроют конкурсный набор**. Эти данные задают будущие измерения.

## 3. Budget safety и graceful degradation

Стоимость по локальному разделу «Модели и учёт токенов»:
`input_tokens/10 + accounted_output_tokens`.
Внутренние tenths `I+10O` сравниваются с `10×BUDGET`; формула корректна и сохранена.
Reasoning включён полностью с учётом declared inclusive/separate semantics;
unknown contract — fail closed. Cache discount не применяется.

| Случай | Фактически проверенная защита |
| --- | --- |
|Нет BUDGET|CLI local-only, прямой from_env требует explicit integer; нет default100000|
|BUDGET положительный|Один manager для planner/gateway/SDK; полный reserve до HTTP|
|BUDGET0 / низкий / остаток1|0HTTP; local answer9 сохраняется0644|
|Дорогой full reserve, маленький forecast|Admission использует полный cap, не прогноз; все6arms блокируют|
|Два нормальных synthetic запроса|2calls,3answers вместе с local; spent60,reserved0|
|Timeout /503 /partial usage|1call, global freeze, reserve удержан, следующий paid не отправлен|
|Output65 при cap64|Known cost75 отражён полностью, freeze,1call; следующий запрещён|
|Input9999 выше local estimate|Known cost1019.9 отражён полностью, freeze,1call|
|Deadline in-flight|Старые regression tests и replay удерживают неопределённую попытку; no retry|
|Повреждённый model plan|Usage учитывается до semantic rejection; repair не запускается|

Все новые stress-пробы используют httpx.MockTransport, настоящий gateway,
BudgetManager и workflow. Принудительный local refusal нужен только для проверки
старого fallback на коротких синтетических условиях, **не для оценки покрытия**.
В baseline comparison никакие local solver не подменяются.

Рост input/output выше зарезервированной оценки нельзя предотвратить после того,
как недостоверный Proxy уже его сообщил. Код отражает реальную известную стоимость,
не обрезает её до cap и запрещает последующий расход. **Это не абсолютная гарантия
удалённого лимита**: нужны подтверждённые framing/tokenization/output-cap semantics
Proxy. Локальный limiter не должен зависеть от обещания remote stop/429/kill.

Ответ сохраняется сразу после проверки каждой задачи прежним atomic save,0644;
не ждёт окончания очереди. При unknown paid cost все local probes уже предшествовали
ему, их результаты не удаляются. Hard kill во время последней проверки/публикации
может потерять именно in-flight результат; избегать планирования впритык.
Общий production deadline3540s включает запуск, операции ограничены30s по умолчанию.

## 4. Proxy/BUDGET: подтверждённое и неизвестное

**Факт локального регламента:** официальный пример передаёт LLM_BASE_URL/KEY,
но не `-e BUDGET`; текст задаёт единицу output-equivalent и четыре модели.
При этом production auto-enable требует все3переменные. Это стыковочный blocker,
не повод ставить приблизительный100000 автоматически. Письменный регламент говорит
о remote stop, актуальное задание требует не полагаться на него — limiter автономен.

**Подтверждено кодом/офлайн-тестами:** POST Chat Completions, один selected model,
no redirects/env-proxy/retries/tools, timeout и response-byte limit, strict outer
JSON/usage/finish/content, SDK callbacks без unreserved calls; настоящий openJiuwen.
Это не доказывает совместимость официального Proxy с NeuralDeep.

**Не подтверждено:** endpoint/model aliases, max_tokens и reasoning cap, значение
usage.total/completion/reasoning на официальном Proxy, добавляемый framing,
ошибки/quota/remote stop и реальная выдача BUDGET. Наличие/принадлежность ключей
здесь не проверялись: реальные credentials не читались и не использовались.
Таблица CPU≤4 против командыCPU8 также остаётся вопросом организаторам.

Python-конструктор gateway также поддерживает явно переданный положительный
BudgetManager без env BUDGET (это используется в mock-тестах); это explicit
programmatic budget, не автоматический CLI-LIVE. Сам complete не проверяет
A1/A2-квитанции. Будущий pilot обязан проверять env/owner/preflight/durable gates
отдельно, а не пользоваться низкоуровневым вызовом для их обхода.

Local36/217 и Graph37/219 измерены ранее/повторно в разрешённой области;
реальная история трёх старых запросов155.9 — публичное подтверждение команды,
**не новая проверка текущего состояния квоты/всех intents**. Из одной решённой
math_d15 нельзя получить p_success Math/Search/Data или прогноз100задач.

## 5. Протокол ограниченной калибровки на11ноября

Это **проект нового отдельно разрешаемого эксперимента**, не готовый LIVE runner
и не способ разблокировать штатный A2. A1 неполный, старые A2/one-shot gates
сохранены. Текущий Semantic Compiler принимает только MockTransport.

### Входы и сравнение

Малый авторский набор без holdout, независимый от compiler-вывода:

- Math: `Compute mean(3, 9, 15)`, ручной oracle9. Local Exact фиксируется первым;
  MathIR локально вычисляет9. Два исследовательских ответа одной фиксированной
  модели: direct short и typed MathIR. Это сравнение представлений, не model chaining.
- Search: вопрос о годе рождения наставника `Neri Vale (botanist)`; два собственных
  профиля задают Mentor→Kori Ash и Born1976. Ручной oracle1976. Graph вычисляет
 1976 бесплатно; отдельная модельная проба строит Query, не новый solver/operator.

Эти условия подтверждают protocol/IR/cost на маленьких независимых данных,
**не демонстрируют новое покрытие поверх детерминированного portfolio**.
Direct answer оценивается только независимым research grader, не допускается
в production answer.txt без семантического verifier. В MathIR/Search отдельно
считаются API JSON, внутренний JSON, schema, meaning/evidence, execution и oracle grade.

### План максимум трёх новых HTTP, без автоматического продолжения

Одна модель во всех trials — `qwen3.6-35b-a3b-noreason`, если её alias и разрешение
подтвердит официальный Proxy. Каждый trial отдельно one-shot; никакого повторного
вызова после ошибки. Лимит модели на задачу соблюдается; иное правило организации
калибровки заранее согласовать. Время каждого HTTP20s, общий эксперимент≤180s.

Офлайн-размеры конкретных текущих prompts (UTF-8+1024 **heuristic**, не tokenizer):

| Trial | Input estimate | Output cap | Forecast full reserve | Context chars |
| --- | ---: | ---: | ---: | ---: |
|Direct short Math|1225|32|154.5|129|
|Qwen→MathIR|2490|256|505.0|1294|
|Qwen→Search Query|3686|256|624.6|2416|
|Всего новых|7401|544|1284.1|—|

С публичной историей155.9 прогноз суммарного консервативного резерва **1440.0**.
Старый pilot cap900 и остаток744.1 не гарантируют всю серию. Последовательные
фактические затраты могут оказаться меньше, но нельзя обещать это до измерения.
**Не увеличивать лимит автоматически.** Владелец задаёт явный `B_OWNER` и разрешает
число trial; до каждого начала надо проверить history+spent+held+full reserve≤limit.
Если cap остаётся900, допустим только отдельно одобренный помещающийся trial,
остальные остаются неразрешёнными/заблокированными. Даже1440 — лишь сумма наших
forecasts; без подтверждённых remote bounds это не гарантированный максимум.

### Preflight и lifecycle перед любым реальным вызовом

1. Получить официальный URL/model/accounting/output-cap контракт и окончательную
   env-команду judge. Ключ проверяется по наличию/владению без вывода значения;
   credentials только через env, не через файл отчёта/командную строку.
2. Согласовать `B_OWNER`, максимум новых HTTP и конкретные trial IDs. Проверить
   историю всех прежних затрат и unresolved durable intent.155.9 использовать
   только после подтверждения, что не было иных/неопределённых попыток.
3. Проверить предыдущие preflight/one-shot состояния read-only; нельзя сбрасывать
   каталог или менять fingerprints ради допуска. Для нового C1 нужен отдельный
   явно одобренный контракт/реализация, не запуск A2 мимо неполного A1.
4. Зафиксировать hash runtime+MathIR+Graph+compiler+diagnostics+synthetic inputs,
   model/endpoint digest, timeout/caps/semantic contract. Старого Docker allowlist
   fingerprint недостаточно: compiler/MathIR там не включены.
5. Сохранить durable exclusive intent до HTTP: atomic write+fsync, lock/claim,
   неизменяемая связь с trial/model/prompt hash/budget. Crash после claim означает
   consumed/unknown; `recover` анализирует, но не отправляет повторно.
6. Один общий manager с историей; отдельная allowlisted телеметрия **сырых**
   prompt/completion/reasoning и accounted_output из тех же usage_records/ledger.
   Это измерение, не второй независимый бюджет и не двойной charge reasoning.
7. До каждого HTTP — generation contract, context/input bound, full reserve,
   достаточный остаток времени на validation/save. No retry/repair/другой model.
8. Stop при401/403/429/503/timeout, partial/unknown usage, unexpected reasoning,
   input/output overrun или model mismatch; unknown claim и reserve сохраняются.
   Нет следующего trial, пока неоднозначность не выяснена вручную.
9. Known usage списывается даже при плохом IR/ответе. Записать latency и отдельные
   API/JSON/schema/semantics/execution/grade статусы; сохранить ограниченную
   synthetic диагностику приватно, без Authorization/ключей и реальных документов.
10. Сначала отдельно проверить новый durable контур офлайн и получить разрешение
    на выполнение. Нынешние production/defaults и диагностические LIVE запреты
    не менять ради калибровки. Остановка пилота не меняет production-репозиторий.

Критерий успеха calibration: совместимый API+полный корректный usage/cap,
воспроизводимая одноразовая отправка и верное ручное решение/IR proof.
Ошибка модели/валидатора — исследовательский результат, не причина автоматически
перезапускать trial. Для вывода о reward coverage нужны затем новые независимые
поддерживаемые условия и измеренная цена/latency, а не только эти3проверки.
**Сейчас запуск запрещён; executable LIVE-команды не подготовлено.**

## 6. Проверки, данные, файлы

**14/14 новых целевых тестов прошли**,1.478s на окончательном коде.
Проверены A=прежний replay, F-band0=A, отсутствие oracle leakage в порядке,
адаптация только по подтверждённой истории, C/E разница, full reserve вместо
forecast, no-network, immutability/determinism, отказ от неизвестных scope/source,
ключей/payload в input tape, все budget stress-сценарии через настоящий workflow.

**Окончательная доступная WSL-регрессия: 779 обнаружено,
772 passed, 0 failures, 0 errors,
7 skipped, 177.177s.** Предварительный прогон до
уточнения allowlist тоже дал772passed/0failures/7skipped,175.227s.
Повтор вызван окончательным ужесточением допуска диагностических трасс;
после него повторены target14,432симуляции и полный доступный набор.
Исходы всех432симуляций до/после изменения совпали точно.

Известные нестабильные baseline timing-тесты в этих двух прогонах прошли.
**Причина прежней нестабильности не исправлялась**; deadlines/assertions
не изменены. Успешный текущий прогон не доказывает исчезновения WSL fsync variance.

7skipped — прежние corpus-wide проверки, требующие закрытых входов/эталонов;
они исключены только внешним guarded runner, исходные файлы тестов не правились.
Это772passed доступной регрессии, а не779/779 без оговорок.

Соблюдён явный allowlist64ID: Math/Search/Data/Coding d01–d16. Для
regression создан временный **39-oracle test view** из уже опубликованных
разрешённых development-correct результатов V4-D и12строк старого OFFLINE-отчёта;
Coding d01 имеет явно написанный в task.json markerDONE и всё равно безопасно
пропускается. Metadata, не сохранённая в этих отчётах, обозначена not_retained,
не подменена догадками. Это не новая таблица эталонов benchmark и не настройка
solver. Все используемые тестами selection/expected сохранены; неиспользуемые
закрытые oracle не восстановлены и не открывались.

Отдельная проверка всех64исходов использует старые подтверждённые actual/status
V4-D, а не эту39-oracle view. Незнакомый новый ответ получил бы needs_reference;
таких ответов0, все64содержимых совпали, все36прежнихcorrect сохранены.
Q5 runtime извлечён git show в /tmp без смены ветки. При configured mock budget
100000calls0, поэтому реальную эффективность paid-policy на текущем development
измерить нельзя: естественных допущенных платных кандидатов нет.432synthetic
сценария заполняют исследовательский пробел, но не доказывают real improvement.
Фиксированные20пар/full128 и закрытые43задачи здесь не переоценивались.

Guard запрещает чтение любых task paths вне allowlist и оригинальных mixed
references.json/mocks.json/исторического mixed baseline JSON. Старые3development
mock-плана восстановлены только для тестов из разрешённых данных. Генератор
corpus не искался, его строки/закрытые fixtures повторно не изучались;
полный corpus не генерировался. Обычные import определения тестовых модулей
не используются для извлечения условий или oracle.

Сверены SHA всех74унаследованных runtime/test/report файлов: изменений0.
Новые файлы проверены на syntax/whitespace и форматы секретов;
`git diff --check` прошёл, staging пуст. Нет новых зависимостей.
Docker не перестраивался: runtime/build не менялись; текущая проверка — WSL,
а не новая Docker-приёмка или разрешение на production-активацию экспериментов.


Только новые файлы V4-E:

- `diagnostics/reward_routing.py` — шесть offline arms, sensitivity, CLI.
- `tests/test_reward_routing.py` —14целевых проверок routing и budget stress.
- `diagnostics/results/v4_e/measurements.json` — компактные итоги432симуляций,
  dev preservation, fingerprints, forecast и результаты тестов.
- Этот отчёт.

Ничего из прежних незакоммиченных V4-файлов не переписано. Нет новых зависимостей,
коммитов, push, merge, переключений веток, private-state изменений и реальных API.

Команды безопасного повторения только новых проверок:

```bash
.venv/bin/python -B -m unittest discover -s tests -p test_reward_routing.py -v
.venv/bin/python -B -m diagnostics.reward_routing --output /tmp/routing-NEW.json
```

CLI отказывается перезаписывать существующий отчёт. В текущей сессии применялись
конечные timeout90s для targets,240s для432симуляций,450s для dev-comparison,
900s для regression. Локальные scripts/logs: `/tmp/v4-e-easu_tv8/`.

## 7. Что внедрять и что оставить экспериментальным

Сохранить production A, формулу стоимости, полный reserve, немедленную atomic
публикацию, общий deadline, fail-closed unknown usage и safety gates. В V4-E не
обнаружено доказанного production budget guard-дефекта, требующего правки.
Приоритет перед LIVE — закрыть env/Proxy/accounting и durable trial approval,
затем получить разрешённые real traces. Разделять validator acceptance и oracle
correctness в исследованиях; не объявлять acceptance обучающей истиной judge.

Оставить экспериментальными B–F, локальные Graph/MathIR и compiler. Нельзя
повышать качество искусственным p, ослаблять смысловые проверки или включать
unsafe Coding. После измерений отдельно выбирать cap и дешёвый маршрут; бесплатный
проверенный алгоритм предпочтительнее повторяющего его LLM-вызова. Не начинать
новую ML-модель/глобальный optimizer, пока не измерен реальный bottleneck.

## Git status

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
?? diagnostics/results/v4_e/
?? diagnostics/reward_routing.py
?? math_ir.py
?? math_ir_parser.py
?? relation_graph.py
?? reports/v4_a_architecture_2026-11-10.md
?? reports/v4_b_first_solver_2026-11-10.md
?? reports/v4_c_second_solver_2026-11-10.md
?? reports/v4_d0_rules_audit_2026-11-10.md
?? reports/v4_d_compiler_2026-11-10.md
?? reports/v4_e_budget_routing_2026-11-10.md
?? semantic_compiler.py
?? tests/test_math_ir.py
?? tests/test_math_ir_workflow.py
?? tests/test_relation_graph.py
?? tests/test_reward_routing.py
?? tests/test_semantic_compiler.py
```
