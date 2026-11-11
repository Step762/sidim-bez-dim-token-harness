# V4-C — Typed Math IR & Exact Executor

10 ноября2026. Ветка `feature/harness-v1`, опубликованный baseline
`bea1ec8e4f9b7028987a78c34de3f377c2808619`.
Основания: [V4-A](v4_a_architecture_2026-11-10.md),
[V4-B](v4_b_first_solver_2026-11-10.md), локальный регламент,
math_solver/plan_semantics и прежние Math-тесты.

## Результат и решение о подключении

Созданы **отдельные экспериментальные** `math_ir.py`, `math_ir_parser.py`,
два файла тестов и machine-readable результаты. Существующие Math, Search,
CSV, harness/openJiuwen, BudgetManager, runtime_limits, Docker-конфигурация,
README и все незакоммиченные результаты V4-B **не менялись**. Их SHA сверены
до и после этапа. V4-B Search Graph автоматически не включался.

MathIR реализует общий типизированный язык вычислений, точный исполнитель,
два compiler, semantic verifier и serializer. **Production не подключён**:
польза новых классов доказана на независимых авторских примерах, но прироста
Math на текущем development нет; полный набор с закрытыми задачами не оценивался.
Экспериментальные результаты не приписываются штатному запуску.

| Режим, development64 | Correct | Wrong | Skipped | Reward | Изменение к production/Q5-dev |
| --- | ---: | ---: | ---: | ---: | --- |
| Production |36|0|28|217/287|0|
| MathIR experimental |36|0|28|217/287|0|
| Search Graph experimental |37|0|27|219/287|+1Search,+2reward|
| Оба эксперимента |37|0|27|219/287|+1Search,+2reward; Math-прироста нет|

Во всех четырёх режимах **36/36 прежних development-correct сохранены точно**.
Новая correct в Graph/combined — прежняя V4-B `search_d12`,1992,reward2;
это не новый Math-успех. `search_d14` и три невозможных/неоднозначных Math
остаются отказами. Никаких реальных LLM-вызовов:0; BUDGET0, запросов0.

Исторические full-Q5 **57/128,0wrong,306/503** не выдаются за свежий результат.
Закрытые43holdout-skipped не читались/не анализировались/не запускались.
Сохранение всех57 новым full-прогоном не заявляется; production-исходники
сохранены побайтно в состоянии начала V4-C.

## 1. Что означает «18 пробелов» и что разрешено выбирать

В V4-A число18 означает **все выполнимые не-Coding-пропуски полного benchmark**:
7Math+6Search+5Data, а не18 development-задач. Отдельный новый development-набор18
в доступных материалах не предоставлен. Уточнение не подменяется чтением holdout.

В development16Math агент уже решает **13/13 имеющих определённый ответ**.
Остались:

| ID | Reward | Семья | Причина и действие |
| --- | ---: | --- | --- |
|math_d13|3|Нарушение области определения|37/0; конечного числового ответа нет, отказ|
|math_d14|13|Недостаток операнда|Неизвестно число участников; не подставлять default|
|math_d16|1|Неоднозначный приоритет|Implicit multiplication без соглашения; не угадывать|

Ни один из этих трёх отказов не требует «ещё одной поддержанной операции».
Все7 оставшихся выполнимых Math-пропусков из агрегата находятся вне разрешённого
семантического анализа; их семейства здесь не раскрывались и не использовались
для выбора реализации. Поэтому нельзя обещать benchmark-прирост Math.

Выбор по обобщаемости и проверяемости новых независимых условий:

1. Композиция точной арифметики, явный порядок/скобки, проценты и изменения.
2. Арифметическое среднее и абсолютное значение — малые самостоятельные операции,
   легко проверяются ручными Fraction-эталонами и входят в более сложные деревья.
3. Числовое отношение/частное и явные метрические переводы единиц.
4. Явное half-up округление вложенного результата, floor, ограниченная целая степень.

**Не реализованы вслепую:** произвольные текстовые пропорции, weighted mean,
процентные пункты/неявные сложные проценты, линейные уравнения с переменными,
конверсия валют, compound physics dimensions. Для них нужен новый semantic
контракт и независимые fixtures. Простое `ratio(a,b)` — числовое частное,
не обещание решать любое условие о пропорции.

## 2. Typed IR и точный исполнитель

### Структуры

```text
Literal(value: Fraction, unit: canonical_unit)
Expr(op, args: tuple[Literal|Expr], option: str)
MathIR(root, output_unit, source_sha256, parser=existing|grammar)
Quantity(value: Fraction, unit)
```

IR — неизменяемые dataclass, никакого пользовательского кода/имён функций/SQL.
`Literal` требует **именно Fraction**, float/bool и неизвестная unit запрещены.
Десятичная и научная запись читаются из исходного литерала точно, а не из
binary float. Decimal используется в **независимом oracle** округления;
для исполнителя Fraction достаточно и он не ограничен Decimal context precision.

Операции: `add/sub/mul/div/floordiv`, `neg/abs/floor`, `sum/mean`,
`percent/increase_percent/decrease_percent`, `power`, `round`, `convert`.
Дерево явно кодирует порядок. Для before/step/LTR старый parser формирует
соответствующую вложенную последовательность; для новой грамматики действуют
обычный приоритет, скобки и последовательность операторов. Implicit multiplication
не добавлена. Общего поиска неизвестной переменной нет.

`execute` получает **только MathIR**, не instruction и не task_dir. Вычисляет
точные Quantity, проверяет деление на0,0^0,0 в отрицательной степени,
рост дробей и время. Никакой сети, чтения CSV/KB или исполнения task text.
Отсутствующий операнд не получает значение0/1 по умолчанию.

### Размерности и конверсия

Канонические units: scalar, длина m/cm/km, объём l/ml, время s/min/h,
отдельные euro/dollar/pound/yen, box/item/unit/coin/point/apple.
Bare dollar/pound не переименовываются в неподтверждённые USD/GBP.
Plural/spelling aliases заданы таблицей; generic units не сливаются с items.

- Add/Sub/Mean/Sum требуют одинаковой **явной** единицы.
- Умножение поддерживает scalar×quantity; произведение двух физических
  размерностей не реализовано и отвергается.
- Деление на scalar сохраняет unit; одинаковые units в числителе/знаменателе
  сокращаются до scalar. Разные units требуют явного перевода заранее.
- Percent rate — scalar; результат имеет unit исходной величины.
- Convert допускается только по фиксированным **точным** коэффициентам одной
  размерности:1m=100cm,1km=1000m,1l=1000ml,1min=60s,1h=3600s.
  Курсы валют и соответствия apple/box/item не угадываются.
- `1m+20cm` отвергается; `1m+(convert(20cm) to m)` разрешено.
  Тем самым не предполагается, в какой единице пользователь ожидал число.
- Power пока только dimensionless base и bounded literal integer exponent.
  Abs/Neg/Floor/Round сохраняют unit; округление происходит в **текущей** единице
  узла, поэтому порядок convert/round не переставляется.

### Serializer

Используется прежний `math_solver.format_math`: точный integer, конечная decimal
либо сокращённая дробь. Нет float, скрытого округления, пояснений или суффикса
единицы в answer.txt. Half-up считает значение точно; fixed-width trailing zeros
не обещаются. Тип результата и output_unit проверяются перед сериализацией.

Это **инженерная конвенция текущего solver/benchmark**, а не доказанный формат
для любого официального скрытого judge. В локальных примерах Math check=exact_int;
универсальная совместимость дробей/decimal требует отдельного подтверждения.
Экспериментальный статус сохраняется в том числе из-за этого ограничения.

## 3. A: адаптер прежнего deterministic parser

`compile_existing(instruction) -> MathIR` вызывает существующий ограниченный
`math_solver._request`. Не вызывает `solve_math`, `_evaluate`, `_warehouse` или
`calculate_math` для получения значения при компиляции.

Переносит структуры expression/binary AST, left_to_right, steps, round, power,
cost и warehouse в общий IR. В арифметическом AST используются только уже
проверенные Constant/UnaryOp/BinOp и исходное написание числа. Никакого eval/exec.
Warehouse преобразуется в typed box-дерево с floor/share, double, ship/receive;
cost — в count×unit_price+fee с явным once/per_item.

Найден и исправлен **дефект нового адаптера**, без изменения старого solver:
первоначальный поиск unit по всему purchase-тексту смешивал `5 items` с `7 euros`.
Теперь валюта берётся из **семантического поля цены** уже совпавшего _COST,
а count и price остаются разными ролями. Добавлены независимые items/boxes/units/
apples покупки. Изменение не подогнано к названию товара benchmark.

Преимущества A: сохраняет развитую полную grammar admission и13существующих
Math-классов development. Недостаток: сам по себе **не расширяет** язык задач;
его purpose — единый backend/typed contract и возможность сравнить с B.

## 4. B: небольшая композиционная грамматика

`compile_grammar(instruction) -> MathIR` не вызывает прежний parser.
Tokenizer и recursive descent полностью потребляют смысловую часть условия.
Неизвестный token, лишняя фраза, пропущенная скобка или неопределённое условие — отказ.
Служебный suffix разрешён только по прежнему полному _SUFFIX; пример echo42
не становится арифметическим операндом. SHA связывает полный исходный instruction.

```text
expr      := product ((+ | - | plus | minus) product)*
product   := unary ((* | / | times | divided by) unary)*
unary     := (+ | - | plus | minus) unary | primary
primary   := number [unit] | (expr) | registered_term
```

Registered terms композируются с любым подходящим expr:

- `abs(expr)`, `the absolute value of (expr)`;
- `mean(a,b,...)`, `average(...)`, `the mean/average of (...)`;
- `sum(a,b,...)`, `the sum of (...)`;
- `ratio(a,b)`, `floor(expr)`, `power(expr, literal_integer)`;
- `RATE percent of (expr)`;
- `increase/decrease (expr) by RATE percent`;
- `convert (expr) to UNIT`;
- `round (expr) to N decimal places using round-half-up`.

Названия в скобках — terminal symbols нашего языка, не вызовы Python-функций.
Нет произвольных identifiers, attribute access, imports или user-defined functions.
Эквивалентные numeric/word и mean/average формы дают одинаковые узлы.
A `Start with100.Increase by20percent.Subtract5` и B
`increase(100) by20percent minus5` дают одинаковый root.

Пример: `Compute 25 percent of (abs(-12) + 8)` компилируется как
`percent(add(abs(Literal(-12)), Literal(8)), Literal(25))`.
Исполнитель получает12, затем20, затем5; serializer выдаёт только `5`.
Перенос abs за скобки или потеря8 меняет структуру и отвергается verifier,
даже если предложенный план содержит те же числа.

**Граница обобщения:** это ограниченный композиционный язык с некоторыми
естественно-языковыми терминалами, не универсальный parser английских задач.
Скобки в новых percent/aggregate формах намеренно требуются для явной области
операции; форма без них может быть понятна человеку, но здесь будет safe skip.

## 5. Verifier: что доказывается и что не доказывается

`verify(instruction, IR)`:

1. Проверяет envelope, точные типы, whitelist операций, arity, literal/exponent/
   precision bounds, размер/глубину, units и output_unit.
2. Сверяет SHA полного instruction.
3. Заново компилирует **полное поддержанное условие** доверенным выбранным parser
   и сравнивает структуру IR, операнды, единицы, порядок и options, а не только
   числа или конечный результат. Extra/omitted nodes отвергаются.

Негативные тесты меняют add/sub, число, order, conversion target, rounding
precision и source. Удаление `+0` даёт **то же значение**, но отвергается:
равенство результата не считается доказательством корректного перевода.
Verifier не вызывает numeric executor для принятия семантики.
Числовая область определения (например, вычисленный divisor0) проверяется
при точном исполнении; до публикации такой отказ превращается в safe skip.

**Общая trusted computing base:** canonical compiler и проверяемый compiler
используют одну grammar/adapter. Verifier защищает от поддельного/неполного IR,
но **не является независимым понимателем естественного языка** и не обнаружит
все общие ошибки самого parser. Эти ошибки проверяются независимыми Fraction/
Decimal oracle, разными формулировками и small-domain матрицей125комбинаций.
Никакого заявления о формальном доказательстве произвольного NL нет.

Это также ограничивает будущий LLM compiler: валидный JSON/IR с неверной
интерпретацией не принимается, но текст, не проходящий trusted admission,
пока останется отказом даже при разумном модельном плане.

## 6. Сравнение на одинаковых задачах

### Только compiler→IR→verifier→executor, без Exact bypass

| Источник | A existing | B grammar | Wrong |
| --- | --- | --- | ---: |
|16developmentMath|13correct,3skip|4correct,12skip|0/0|
|14новых независимых положительных Math|2correct,12skip|13correct,1skip|0/0|
|18новых независимых отрицательных|18safe skip|18safe skip|0/0|

Development-эталоны находятся только в parent grader; IR/агент их не получают.
A/B измерены на тех же входах. Ответы не используются при выборе IR.

**Выбрана комбинация:** стабильный Exact первым, затем B для новых композиций,
A как bridge старых форм в общий IR. У A объективно выше старое покрытие,
у B — новые компонуемые классы. На14новых позитивных вместе14/14;
18негативных остаются отказами. Нельзя заменить существующий solver одним B:
он потеряет9из13разрешённых правильных Math в raw-режиме.

### Портфели через настоящий workflow

Отдельная авторская cohort:14положительных Math,1новый Search qualifier,
18отрицательных Math; ручные эталоны не выведены из solver.

| Режим | Correct из15позитивных | Wrong | Safe/unsupported skipped из33 | Запросов |
| --- | ---: | ---: | ---: | ---: |
|Production|2|0|31|0|
|MathIR experimental|14|0|19|0|
|Search Graph experimental|3|0|30|0|
|Оба|15|0|18|0|

Это **синтетическое диагностическое покрытие**, не конкурсные баллы и не новый
benchmark-score. Измеренный development-score отдельно приведён в начале.

Для четырёх development-прогонов использован прежний `benchmark.evaluate`/worker
с development-view64 и настоящим openJiuwen. Каждый experimental runtime копируется
в отдельный `/tmp` каталог; только в этой копии harness выбирает нужный SOLVERS.
Production-файлы и штатная команда не получают новых флагов/env-настроек.
Каждое answer.txt публикуется прежним атомарным save_answer с0644; source files
не меняются. Ни check/expected, ни эталон не передаётся агенту.

## 7. Ресурсы, безопасность и проверки

- Instruction8000символов; исходный literal80символов; scientific exponent≤100.
- Node256,operation64,depth32,aggregate2–16аргументов.
- Exact numerator/denominator≤2048bit; integer power abs≤64; round places0–100.
- Рост power проверяется **до возведения**, остальные exact операции ограничены
  bounded operands и проверкой результата. Нет произвольного BigInt exponent.
- Structural validation имеет monotonic1s cap, execute — отдельный cap≤1s;
  проверка производится при посещении узлов/после операции. Это **не hard kill
  для каждой возможной операции Python/старого parser**. В workflow сохраняется
  настоящий внешний sync RuntimeLimits30s и общий3540s; они не менялись.
- Unary/parenthesis nesting и token count512 ограничены до построения большого дерева.
- Поддельные unit/unknown op/wrong arity/float/bool/изменённый envelope отклоняются.
- Source text не выполняется; no eval/exec. Изолированное существующее ast.parse
  используется только через прежний whitelist adapterA. Новые modules не читают
  workspace, секреты или сеть.

Целевые проверки: **31/31 прошли** после purchase-role исправления,1.931s.
Включают125знаковых комбинаций с независимым целочисленным oracle,
Decimal round-half-up signed ties,53-bit float trap, дроби, units/conversions,
неправильную семантику и explicit production-off, реальный openJiuwen workflow,
отказы без answer.txt, сохранение0644, продолжение после отказа и совместный
запуск двух экспериментов. Synthetic network запрещён socket.connect guard.

**Окончательная доступная WSL-регрессия:740обнаружено,730passed,
3failures,7skipped,0errors,194.403s. Все31новых MathIR-тест прошли.**

Точные failures:

- `test_runtime_limits.RuntimeLimitTests.test_slow_fallback_preparation_does_not_block_next_local_task`;
- `test_runtime_limits.RuntimeLimitTests.test_slow_feature_probe_is_bounded_independently`;
- `test_runtime_limits.RuntimeLimitTests.test_slow_first_local_does_not_block_second_or_trigger_fallback`.

Это неизменённые старые тесты30–40ms: следующая контрольная задача не сохраняет
answer.txt в заданном узком окне. Однократный контроль тех же3проверок на
исходниках **Q5 из git show**, без импорта MathIR, дал1pass/2failures за1.312s
(preparation и first-local воспроизведены, feature-probe в этом контроле прошёл).
Это подтверждает воспроизводимость двух сбоев на baseline в текущей среде,
но не доказывает точную причину третьего/всей нестабильности. В V4-B также
зафиксированы переменные failures этой группы. Нельзя назвать регрессию зелёной
или списать её целиком на нагрузку без расследования.

Первый промежуточный full-прогон до окончательного purchase-role исправления
тоже обнаружил740тестов,3failures,7skipped за216.772s; он сохранён отдельно,
не выдан за окончательную приёмку. После исправления повторены целевые тесты,
все четыре development-конфигурации и полный доступный набор. Больше повторов
до случайного зелёного результата не выполнялось. Таймаутные тесты и лимиты
не изменены; проблемы runtime не смешиваются с разработкой MathIR.

Семь прежних corpus-wide тестов, требующих закрытых условий/эталонов, исключаются
явно по той же no-holdout policy V4-B. Остальные тесты используют разрешённый
development loader; audit-hook запрещает оригинальные holdout paths и mixed
references.json. Их нельзя объявить «все740passed» с незаявленными исключениями.
Известные V4-B baseline сбои30–40ms timing-тестов не исправлялись и лимиты ради
зелёного результата не ослаблялись.

Docker не перестраивался. Новые экспериментальные modules **не добавлены** в
Docker allowlist/COPY: production их не импортирует; V4-B build-файлы сохранены.
Для будущей контейнерной приёмки MathIR потребуются явное включение файлов и smoke;
старый образ не объявляется образом с новым экспериментом.

## 8. Статус, файлы и следующий compiler-контракт

Новые файлы этого этапа:

- `math_ir.py` — types,units,validation,executor,serializer.
- `math_ir_parser.py` — A/B,semantic verify,явный `solve_math_ir`.
- `tests/test_math_ir.py`, `tests/test_math_ir_workflow.py`.
- `diagnostics/results/v4_c/measurements.json`.
- Этот отчёт.

Существующие Math и все файлы V4-B побайтно совпадают со снимком начала V4-C;
новые результаты не смешиваются с `diagnostics/results/v4_b`. Публичная точка
будущего подключения — `solve_math_ir(instruction)`, тот же str/int-or-UnsupportedMath
контракт. Сейчас ни production SOLVERS, ни CLI, ни LLM fallback её не используют.

Перед LLM-as-Compiler нужны: versioned strict JSON decoder в whitelist IR,
доказуемое source admission/покрытие фраз, явный unit/round/output contract,
проверка source evidence, независимые новые задачи и полный regression gate.
Модель не должна получать право назначать unit scale, новую операцию или rounding
policy. Любой будущий вызов обязан использовать прежние gateway/BudgetManager,
полный резерв и no-retry; этот этап таких вызовов/адаптера не реализует.

Нерешённые риски: общий grammar/verifier TCB, ограниченный NL,
никакого measured Math-dev прироста, неизвестная совместимость нецелых ответов
с judge, неподдержанные уравнения/compound dimensions, отсутствующая Docker-приёмка,
и ограничения полной регрессии при запрете holdout. Новые реальные API-трассы
не собраны и не разрешены. При текущих данных подключать к production рано.

Итоговый Git status:

```text
## feature/harness-v1...origin/feature/harness-v1
 M .dockerignore
 M Dockerfile
 M README.md
 M plan_semantics.py
 M search_solver.py
 M tests/test_runtime.py
?? diagnostics/results/v4_b/
?? diagnostics/results/v4_c/
?? math_ir.py
?? math_ir_parser.py
?? relation_graph.py
?? reports/v4_a_architecture_2026-11-10.md
?? reports/v4_b_first_solver_2026-11-10.md
?? reports/v4_c_second_solver_2026-11-10.md
?? tests/test_math_ir.py
?? tests/test_math_ir_workflow.py
?? tests/test_relation_graph.py
```

Все6tracked изменений и5исходных untracked V4-A/B позиций уже были на старте;
V4-C добавил только перечисленные6новых файлов (metrics-каталог содержит один JSON).
HEAD остался `bea1ec8e4f9b7028987a78c34de3f377c2808619`.
Проверены `git diff --check`, синтаксис новых Python-файлов, whitespace и
известные форматы ключей/приватных ключей новых артефактов. Секретные файлы
не сканировались и не читались.
Commit/push/merge, main/Coding/benchmark/holdout не изменялись. Исторические
журналы и отчёты сохранены. Этап заканчивается отчётом, без следующей разработки.

## Воспроизведение

```bash
timeout 60s .venv/bin/python -B -m unittest discover -s tests -p 'test_math_ir*.py' -v
```

Development и четыре конфигурации воспроизведены временным измерительным
скриптом `/tmp/v4-c-86rk_tl_/measure_final.py`, использующим существующий grader.
Guarded suite — `/tmp/v4-c-86rk_tl_/run_suite_final.py`. Скрипты/сырые логи
остаются в /tmp, не входят в runtime; после очистки /tmp нужен новый такой же
разрешённый development-view. Unit/workflow tests и чистые metrics доступны
в репозитории. Ключи/приватные LIVE/state/intent не читались и не публиковались.
