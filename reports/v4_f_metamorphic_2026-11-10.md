# V4-F — Independent Metamorphic Validation & Acceptance Gates

10 ноября 2026. Ветка `feature/harness-v1`, HEAD
`bea1ec8e4f9b7028987a78c34de3f377c2808619`.
Основания: AGENTS.md, локальный регламент, отчёты
[V4-B](v4_b_first_solver_2026-11-10.md),
[V4-C](v4_c_second_solver_2026-11-10.md),
[V4-D](v4_d_compiler_2026-11-10.md),
[V4-E](v4_e_budget_routing_2026-11-10.md), реальные публичные контракты компонентов.

## Результат и границы

Создана автоматическая фабрика с **34 семействами, 5 экземплярами на семейство,
170 парами base→changed и 540 проверками исполнения**. Seed20261110.
Это **300 различных входов instruction+files**, а не540независимых задач:
Graph/portfolio исполняют одинаковые Search-входы, compiler проверяет A/B,
часть мутаций меняет только предложенный IR. Повторы выделены в метриках.

После одного ограниченного исправления экспериментального verifier:
**410 правильных ответов,0wrong,130отказов; false acceptance0/120 обязательных
отказов; ошибочных отказов10**. Precision принятых410/410. Отказы не скрыты
за общей «нулевой ошибкой»:5относятся к свободной формулировке Math и5к строгому
Exact-first portfolio. Новые корректные задачи действительно решаются; режим
«отказать всем» не проходит положительные acceptance gates.

Исправлен **один** обобщаемый дефект: прежний MathIR verifier отвергал доказуемо
эквивалентную перестановку двух операндов точного add/mul. Остальные ограничения
не обходились. Production routing, Local Exact, Graph, Data, Coding,
BudgetManager, planner, gateway, Docker и LIVE-defaults **не менялись**.
Graph/MathIR/compiler автоматически не включены. Реальных LLM-запросов0.

## 1. Независимые oracle и контроль преобразований

Новый `diagnostics/metamorphic.py` не импортирует benchmark, его генератор,
эталоны или fixtures. Генерация/проверка модели не вызывает тестируемые parser,
compiler, executor или verifier; SUT импортируются только при исполнении.

- **Search:** заранее заданная модель ID→label/aliases/fields и явный путь
  forward/inverse. Независимый relational oracle сканирует модель, проверяет
  уникальное имя/alias, путь, кандидатов и проекцию. Документы строятся после
  определения смысла. Он не использует Graph, compile_query, KB parser или
  результат Graph для назначения expected.
- **Math:** прямые математические формулы над исходными числами, точные Fraction;
  процентовка/среднее/модуль/отношение не вычисляются через IR. Использование той
  же стандартной библиотеки точных чисел не является независимой реализацией
  самой рациональной арифметики, но алгоритм и expected независимы от SUT.
- **Data:** исходные записи загружаются в отдельную SQLite :memory:. Фиксированный
  параметризованный SELECT SUM/COUNT/MIN/MAX сверяется с прямой Python-формулой;
  AVG вычисляется точно из SQL SUM/COUNT, без SQLite float AVG. Task text/SQL
  из задачи не исполняется. Это test oracle, **не новый DataQueryIR/solver**;
  ответы выдаёт исключительно прежний production solve_data.
- **Compiler:** wire IR написаны вручную из чисел/ролей модели; ни один правильный
  план не берётся из compile_grammar/compile_existing/compile_query. Числа,
  потеря операции и неправильный порядок выбраны так, чтобы неправильный план
  действительно давал другой результат. В identity-мутации оба пути исполнимы
  и ведут к разным годам: тест не полагается на missing fact для отказа.

Перед каждым исполнением `audit` повторно проверяет model→oracle, text/model
binding и заявленный закон преобразования. Отдельный ограниченный fixture-binder
проверяет используемые математические и Search-формы, числа и порядок независимо
от NL-грамматики SUT. Это **не второй универсальный English solver**.

Проверены bijection переименования, сохранность рёбер, эквивалентность scientific
чисел, cross multiplication дробей, multiset строк при перестановке, точное
удвоение агрегатов, включённые границы и CSV round-trip с кавычками/разделителями.
Для source-conflict oracle становится неопределённым; для неверного IR правильное
условие остаётся решаемым, но именно предложенный план обязан быть отклонён.

В target-тестах намеренно портятся oracle, текст и модель одновременно,
CSV и закон expected delta; фабрика обязана завершиться ошибкой **до SUT**.
При разработке audit обнаружил собственную несовместимость порядка полей при
JSON round-trip модели; renderer приведён к детерминированному порядку.
Это исправление конструктора тестов, не один из двух разрешённых solver-дефектов.
Скрытые условия/эталоны для исправления фабрики не использовались.

## 2. Семейства и объём

| Категория | Семейств | Пары | Проверок компонентов |
| --- | ---: | ---: | ---: |
|Search|10|50|100Graph+100portfolio|
|Math|9|45|90MathIR|
|Data|5|25|50production Data|
|Compiler|10|50|100A+100B|
|Всего|34|170|540|

Search: bijective rename; квалифицированные одноимённые профили; неоднозначные
профили с одинаковым heading; порядок документов/строк фактов; независимый Alias;
дубликаты строк/полного документа; посторонний конфликт; релевантный конфликт;
inverse→forward и проверяемое расширение forward-цепочки.

Math: числовая замена с известным сдвигом среднего; отрицательные числа;
масштабирование дроби; проценты↔отношение; эквивалентная scientific запись;
вложенный модуль и перестановка независимых множителей; несовместимые единицы;
явная неоднозначность приоритета; свободный English paraphrase среднего.

Data: порядок строк, дубликаты, добавление строк ровно на обеих границах,
переименование колонок/вложенного файла, нерелевантные строки. В каждом семействе
пять экземпляров покрывают SUM,COUNT,MIN,MAX,AVG; есть отрицательные значения,
комма и двойная кавычка внутри SKU. Никаких новых Data-операций.

Compiler: неверный оператор; потеря операции; неверная вложенность/порядок;
изменившееся условие при старом IR; подмена существующей сущности;
несуществующее ребро; превышение depth; malformed JSON; конфликт источника;
математически эквивалентная перестановка слагаемых. A — IR, B — IR+source refs.
Evidence строится независимо по строкам/хешам исходных fixtures, не копируется
из Contract.allowed/reference_candidate. Новый набор не измеряет все возможные
повреждения evidence; прежние D-тесты stale/missing/duplicate spans сохранены.

Отдельно9метаморфических target-тестов до исправления и11после него проверяют
oracle, негативные сертификаты, реальный workflow, proof corruption и ограниченную
эквивалентность. Это не дополнительные независимые benchmark-задачи. Генерация
с seeds17/93 служит аудиту фабрики, не прибавлена к540SUT-проверкам.

## 3. Результаты по категориям после исправления

| Контур | Проверок | Correct | Wrong | Skip total | С единственным ответом | Coverage | Precision | Ошибочных отказов |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
|Search Graph напрямую|100|90|0|10|90|100%|100%|0|
|Search portfolio Exact→Graph|100|85|0|15|90|94.44%|100%|5|
|MathIR напрямую|90|75|0|15|80|93.75%|100%|5|
|Production Data|50|50|0|0|50|100%|100%|0|
|Compiler A|100|55|0|45|95|57.89%|100%|0|
|Compiler B|100|55|0|45|95|57.89%|100%|0|

Coverage = correct / trials с определённым независимым ответом.
У compiler знаменатель включает решаемые задачи с намеренно неправильным
предложенным планом. Поэтому это **не вероятность решить задачу моделью**:
оба варианта приняли55/55 корректных предложений и отклонили45/45 неправильных
или недоказуемых. Valid-proposal coverage у обоих100%.

Общий coverage410/500=82%, valid-proposal coverage410/420=97.62%.
130safe skips означают отсутствие выданного неверного ответа, **не130идеальных
решений об отказе**:120отказов требовались условиями/планом,10были излишне
консервативными. False acceptance0/120; в compiler0/90, отдельно A0/45 иB0/45.
Измеренная precision не доказывает нулевой риск за пределами авторской выборки.

Metamorphic consistency после исправления: **260/270=96.30%**:

| Контур | Согласованных преобразований |
| --- | ---: |
|Graph|50/50|
|Portfolio|45/50|
|MathIR|40/45|
|Data|25/25|
|Compiler A+B|100/100|

10несогласованностей совпадают с зарегистрированными ошибочными отказами.
Критерий включает правильный base и ожидаемое поведение changed, а не простое
совпадение двух одинаково неправильных ответов. Для ambiguity/conflict правильный
changed — отказ; для неверного IR — отказ от предложения при сохранении oracle.

**Executor wrong0, unexpected errors0, verifier implementation errors0** на
объявленных контрактных сценариях. Неверные числа, перепутанные отношения/identity
отклоняются semantic gate **до execution**, хотя JSON/schema корректны; это отдельно
проверено по phase fields. Также испорченные answer/path/terminal provenance
кандидата отвергаются независимым Graph verifier. Проверка правильного кандидата
проходит даже при запрещённом Graph.hop: verifier использует полный scan.

Эти результаты не доказывают универсального понимания английского: оба verifier
сохраняют общий parser/semantic admission как доверенную базу. Фабрика имеет
конечные семейства, короткие данные, ограниченные значения и только5draws;
она не является blind external test или статистической оценкой скрытого judge.

## 4. Крупнейшие отказы и одно исправление

До исправления на тех же540пробах: **400correct,0wrong,140skips,
20false refusals,250/270metamorphic consistency**. После:410/0/130,
10false refusals,260/270. Хеш входа каждой строки, task/engine/variant
совпали до/после; изменились только10положительных equivalent_plan replies.

### Исправлено: точная бинарная коммутативность

Проблема: `abs((a+b)*c)` и `abs((b+a)*c)` задают одинаковое точное вычисление,
но прежнее сравнение MathIR dataclass отвергало второе дерево. Отдельный новый
положительный тест на прежнем исходнике завершился MathIRRefusal. Это
воспроизведённый неверный отказ, а не гипотеза по коду.

Минимальная правка только в `math_ir_parser.py:verify`:

1. Прежние validate, units, nodes/depth/bits/options, hash полного instruction
   и повторная trusted compilation остаются обязательными.
2. Сравниваются output_unit, source_sha256, parser.
3. Строится bounded proof key дерева. Только у **бинарных add/mul** порядок двух
   children может переставляться; сохраняются все литералы с точными значениями
   и units, операторы, options и поддеревья.
4. Нет численного исполнения для признания смысла, удаления +0, сокращения,
   reassociation, изменения SUB/DIV/percent/power, переноса ROUND/CONVERT или
   угадывания единиц. N-ary SUM/MEAN также не переставляются этим правилом.

Основание безопасности: в уже проверенном exact typed IR эти две операции
коммутативны; нет float, побочных эффектов или произвольного пользовательского
кода. Перестановка не меняет результат/единицу и не теряет ни один source operand.
Это ограниченное доказательство эквивалентности, **не допуск по совпадению ответа**.

Положительные тесты включают scalar, метры и евро и запрещают вызов numeric
executor из verifier. Отрицательные сохраняют SUB/DIV/percent/increase/power,
неверную unit/source/precision, перенос округления и одинаковый результат
`8+3` против `8-(-3)` без допустимого структурного доказательства.
Старые tests/test_math_ir.py и tests/test_semantic_compiler.py не ослаблены.

**Использовано1из2разрешённых solver-исправлений.** Исполнитель, whitelist и
production Math не менялись. Compiler получил ту же улучшенную MathIR-проверку;
его preflight/JSON/evidence/бюджет/transport gates не менялись.

### Оставлены как ограничения

- **5 Math false refusals:** `Find the average of a,b and c` имеет понятный
  независимый смысл, но не входит в текущую полную grammar admission. Точечный
  regex не добавлялся. Общая поддержка перефразировок требует нового
  компиляционного контракта и проверки полного условия.
- **5 portfolio false refusals:** противоречивый Born самого root не относится
  к вопросу о его наставнике; Graph это доказывает, но Exact помечает профиль
  ambiguous, и wrapper намеренно сохраняет этот hard refusal. Graph напрямую
  отвечает, production portfolio — нет. Это консервативная политика маршрута;
  её не ослабляли ради synthetic coverage. Новый gate после hard refusal требует
  отдельной оценки identity/source semantics и не внедрён.
- Исправление не даёт общей алгебраической эквивалентности, произвольного NL,
  автоматического объединения одноимённых профилей или новых отношений.

## 5. Development и разрешённая qualification

Извлечение Q5 через git show в /tmp, без переключения ветки. Входы только
explicit allowlist: Math/Search/Data/Coding d01–d16. SHA разрешённых входов
проверены по manifest metadata; оригинальные эталоны/задачи не менялись.
Использованы опубликованные разрешённые ответы V4-D; незнакомый новый ответ
получил бы needs_reference и остановил измерение. Таких ответов0.

| Реальный SDK/workflow, local budget0 | Correct | Wrong | Skip | Reward | Сохранены старые36 |
| --- | ---: | ---: | ---: | ---: | --- |
|Q5 из checkpoint|36|0|28|217/287|Да|
|Текущий production|36|0|28|217/287|Да|
|Opt-in Graph portfolio|37|0|27|219/287|Да|
|Opt-in MathIR|36|0|28|217/287|Да|
|Оба opt-in|37|0|27|219/287|Да|

После verifier-fix повторены затронутые MathIR/combined arms и их пары;
результаты сохранены. Остальные исходники/режимы не менялись и не повторялись
без причины. Новая относительно Q5 Graph correct — прежняя search_d12,
а не новый прирост V4-F; MathIR-коммутативность улучшила приём планов, не число
development-условий. Нельзя объявлять общий новый full128/reward503 результат.

Из20фиксированных пар только **7полностью входят в allowlist**. Каждая выполнена
отдельно на новой копии задач, со своим manager/deadline, настоящим workflow,
без expected/check и без доверенного Coding. Во всех5arms:
**5пар2/2,2пары1/2,0пар0/2**. После исправления MathIR/combined — так же.
Два1/2 — coding_d01 иcoding_d07 в парах с корректным local ответом другого типа;
Coding имеет unsafe state, не подменяется DONE и не исполняется.

Это результат7разрешённых пар, **не новый score по20парам**. Оставшиеся13пар
не запускались и не анализировались по условиям/эталонам. Дополнительная новая
авторская Math+Search пара проходит2/2 через opt-in MathIR/Graph с реальными
вызовами обоих verifier и публикацией answer.txt0644, BUDGET0,0LLM.
Ни этап квалификации, ни приоритеты не угадываются по числу каталогов.

## 6. Acceptance gates по компонентам

| Вопрос | Search Graph / portfolio | MathIR | Semantic Compiler |
| --- | --- | --- | --- |
|Независимые классы обобщаются?|Да в10конечных семействах; direct90/90positive, portfolio85/90|Да в9семействах;75/80positive, свободный NL ограничен|Да на10IR-мутирующих семействах;110/110valid proposals после fix|
|Измеренный development gain?|+1correct,+2reward относительно Q5|0|Нового покрытия не измерено; replay не Qwen|
|Новые wrong/false accepts?|0/0;5излишних отказов wrapper|0/0;5NL-отказов|0/0;10equivalent-отказов устранены|
|Ограниченный feature flag?|Кандидат на отдельную local-only реализацию default-off; сейчас не добавлен|Возможен только экспериментальный local opt-in; выгода dev не доказана|Только offline/mock opt-in, LIVE запрещён|
|Можно безопасно отказаться?|Да: прежний Exact либо safe skip; hard refusals сохраняются|Да: Exact либо safe skip|Да: preflight/schema/proof refusal, без нового запроса|
|Что блокирует production?|Актуальная Docker/flag-приёмка, read-only/resource smoke, больше независимых данных, отдельное решение об активации|Нет dev gain; ограниченный NL/эквивалентности; packaging/real confirmation до LIVE|Нужны разрешённый Qwen-пилот, Proxy/BUDGET/one-shot, real quality/cost; source admission ограничен|

**Решение:** Graph — обоснованный кандидат на узкий local feature flag, но
не автоматическая активация. Будущий flag обязан сохранить Exact success first,
допуск только при прежнем unsupported, bounded Query, unique identity,
fresh provenance/full-scan verification и safe skip. Расширение fallback после
hard refusal — отдельный вопрос, а не часть текущего разрешения.

MathIR и compiler оставлены экспериментальными; **LIVE не включать без реального
разрешённого подтверждения**. Новое равенство proof keys не заменяет проверку
официального Proxy, BUDGET или durable intent. Старые A1/A2/one-shot guards,
история расходов и private state не менялись. Fingerprint будущего пилота обязан
включить изменённый MathIR verifier; старый allowlist hash недостаточен.

## 7. Регрессия, файлы, воспроизведение

**67/67 целевых проверок после исправления прошли**,5.135s:11новых
metamorphic/certificate/workflow методов,31прежний MathIR/workflow тест и25прежних
compiler-тестов. До исправления9новых методов прошли и показали20false refusals;
отдельный положительный commute-regression на прежнем verifier воспроизвёл
MathIRRefusal. Это ожидаемая демонстрация дефекта, не скрытый проходящий тест.

**Окончательная доступная WSL-регрессия: 790 обнаружено,
783passed, 0failures, 0errors,
7skipped,180.445s.** Предварительный прогон до
solver-fix:788обнаружено,781passed,0failures,7skipped,180.022s.
Повтор полного набора сделан из-за исправления verifier/двух новых защитных тестов,
не для получения случайного зелёного результата. Известные baseline timing-тесты
прошли в обоих прогонах; их прежняя WSL I/O нестабильность не объявлена устранённой.
Ни один прежний assertion, timeout, expected/check или безопасность Coding
не ослаблялись.

Семь skipped — те же corpus-wide проверки, которым необходимы закрытые входы
или эталоны. Их исключает внешний guarded runner, исходники тестов не изменены.
Поэтому заявляется783passed доступной регрессии, а не790/790 или новая full-holdout
приёмка. Использована прежняя39-oracle development test view из разрешённых
публичных OFFLINE-отчётов. Оригинальные mixed references.json/mocks.json/baseline
не открывались; audit guard запрещает task paths вне allowlist. Генератор
смешанного корпуса не искался, его закрытые условия/строки не изучались.

Все78унаследованных файлов сверены по SHA: изменён только разрешённый
math_ir_parser.py,77остальных совпали. Новые файлы проверены на syntax/whitespace
и форматы секретов, `git diff --check` прошёл, staging пуст.
Ни API-ключей, ни private receipts/intents/live logs в новых артефактах нет.
[Машинные результаты](../diagnostics/results/v4_f/measurements.json) содержат
до/после,540строк с phase/answer/input hash, семействами и отдельными знаменателями.
Первые локальные prototype-отчёты также сохранены в /tmp, не выданы за финальные.

Docker не собирался: production не подключён к изменённому экспериментальному
MathIR, его текущий образ не содержит этой новой проверки. Контейнерная приёмка
нового feature flag остаётся отдельным обязательным шагом перед активацией.


Созданы:

- `diagnostics/metamorphic.py` — модели, независимые oracle, certificate audit,
  фабрика, измерения, CLI без LIVE и без benchmark dependencies.
- `tests/test_metamorphic.py` — oracle/proof/negative acceptance/workflow gates.
- `diagnostics/results/v4_f/measurements.json` — before/after, метрики, source hashes,
  development и7разрешённых пар.
- Этот отчёт.

Из существующих файлов изменён **только math_ir_parser.py**, минимальное
структурное доказательство бинарной коммутативности. Остальные унаследованные
V4-файлы сохранены. Не изменены Coding, main, feature/coding-solver,
benchmark/holdout, реальные журналы и state. Нет новых зависимостей.

Безопасные команды:

```bash
.venv/bin/python -B -m unittest discover -s tests -p test_metamorphic.py -v
.venv/bin/python -B -m diagnostics.metamorphic --output /tmp/metamorphic-NEW.json
```

CLI не перезаписывает существующий output. Метаморфические пробы/targets имеют
внешний timeout90s, сравнение старого/нового verifier120s, development600s,
WSL900s. Scripts, копия прежнего verifier, первичные отчёты и логи сохранены
в `/tmp/v4-f-cu7gf1k_/`; исторические результаты не перезаписаны.

Следующий приоритет: отдельная default-off Graph flag/container приёмка и
расширение **независимого** корпуса, включая неопределённую identity/partial
sources. Затем — проверенный semantic admission для свободного языка и новые
ограниченные эквивалентности только по реальным трассам. Реальные Qwen-вызовы
требуют отдельного разрешения/Proxy и сохранения ранее установленных лимитов;
в этой работе они не выполнялись и следующий этап не начат.

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
?? diagnostics/metamorphic.py
?? diagnostics/results/v4_b/
?? diagnostics/results/v4_c/
?? diagnostics/results/v4_d/
?? diagnostics/results/v4_e/
?? diagnostics/results/v4_f/
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
?? reports/v4_f_metamorphic_2026-11-10.md
?? semantic_compiler.py
?? tests/test_math_ir.py
?? tests/test_math_ir_workflow.py
?? tests/test_metamorphic.py
?? tests/test_relation_graph.py
?? tests/test_reward_routing.py
?? tests/test_semantic_compiler.py
```
