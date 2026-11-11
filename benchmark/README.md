# Диагностический benchmark V2 №5

`diagnostic-128-v1`: 128 задач, по 32 math/search/data_analysis/coding.
Это искусственное равномерное распределение, не прогноз набора организаторов.
Баллы этого benchmark нельзя переводить в конкурсный результат.

## Независимость и разделение

`dataset/tasks/<id>/task.json` содержит только task_id, task_type, instruction,
reward. Каждый workspace независим. Ответы, сложность, причины и скрытые примеры
функций находятся отдельно в `dataset/references.json`. `dataset/mocks.json`
содержит вручную составленные контрольные планы. Они не копируются в задачи.

`corpus.py` использует стандартную библиотеку, не импортирует production-код.
Арифметика проверена вручную и Fraction/Decimal. Для CSV используются отдельные
SQL-спецификации над новыми наблюдениями. Coding содержит заранее написанные
безвредные программы, публичные тесты и отдельные примеры функций у оценщика.
Сложность назначена автором, не выведена из успеха агента.

Development/holdout: по 64 задачи, по 16 каждого типа. Seeds: 20261105/20261106.
Они задают reward и часть наблюдений; сценарии и split версионированы явно.
Семейства сценариев между выборками не пересекаются: это не случайный split
строк одного шаблона. Поддерживаемые и неподдерживаемые формулировки включены
намеренно, без изменения продукционных правил.

Автор знает устройство агента: это не внешний слепой экзамен. Защита от
подгонки: набор составлен до измерений, эталоны не получены от решателей,
продукционный код не менялся, результаты обеих выборок показаны полностью.
Перед первым полным baseline исправлены метаданные сложности четырёх простых
coding-задач; входы, ответы, split и reward не менялись. Ошибка дробного
резерва исправлена только в оценщике. Для последующей разработки используйте
development; holdout запускайте после фиксации изменения без автоматического
добавления правил по его провалам. Малый авторский holdout не доказывает
статистического обобщения на конкурс. Интеграционные unit-тесты агента здесь
используют только development; проверка эталонов и структуры охватывает обе
выборки, не вызывая production-решатели для получения ответов.

## Воспроизведение

Команды из корня проекта; реальные LLM и внешние API не используются.

```bash
python3 -B benchmark/corpus.py --output /tmp/benchmark-fresh
.venv/bin/python -B benchmark/evaluate.py --mode local --output /tmp/bench-local
.venv/bin/python -B benchmark/evaluate.py --split development --output /tmp/bench-dev
.venv/bin/python -B benchmark/evaluate.py --split holdout --output /tmp/bench-holdout
.venv/bin/python -B benchmark/evaluate.py --task-id data_analysis_d12 --output /tmp/bench-case
.venv/bin/python -B benchmark/evaluate.py --mode mock-good --output /tmp/bench-good
.venv/bin/python -B benchmark/evaluate.py --mode mock-wrong --output /tmp/bench-wrong
.venv/bin/python -B benchmark/evaluate.py --mode mock-timeout --output /tmp/bench-timeout
.venv/bin/python -B benchmark/evaluate.py --mode mock-no-usage --output /tmp/bench-no-usage
.venv/bin/python -B benchmark/evaluate.py --mode unavailable --output /tmp/bench-outage
.venv/bin/python -B benchmark/evaluate.py --mode mock-good --strategy reward --budget 500 --output /tmp/bench-reward
.venv/bin/python -B benchmark/run_suite.py --output-dir /tmp/bench-suite
# Продолжить после прерывания с совместимыми сохранёнными результатами:
.venv/bin/python -B benchmark/run_suite.py --output-dir /tmp/bench-suite --resume
```

Генератор отказывается перезаписывать изменённые файлы; оценщик требует новый
путь отчёта. Manifest проверяет SHA-256 входов. Отчёты содержат также hashes
эталонов/mock-планов и всех 15 файлов runtime; resume проверяет их, версию
политики сравнения, IDs, режим и бюджет. Время и request
IDs могут меняться; данные, планы и mock-расход воспроизводимы.

Матрица сравнивает priority/original/reward с бюджетами 1000 и 500 на одних
задачах/планах. Второй бюджет добавлен после измерения: 1000 хватило на все
семь fallback-вызовов. Сохранены оба сравнения, задачи и правила не менялись.
20 пар заранее заданы в manifest и выполняются отдельно в local и mock-good.
Все задачи пар имеют определённый эталон; 2/2 не гарантирует конкурсный отбор.

## Что измеряется

Оценщик копирует только выбранные inputs и запускает worker отдельным Python
с минимальным окружением. Worker вызывает настоящий harness.run_tasks и workflow
openJiuwen; обёртка Workflow.invoke лишь измеряет исходный вызов с теми же
аргументами. Официальный CLI покрыт runtime-тестами и Docker smoke.

На host эталоны исключены из argv, окружения, task.json и модельного контекста;
это входной контракт, не дополнительная файловая песочница. Worker не имеет
trusted-code параметра. Тренировочный Python не исполняется. Отдельного trusted
coding-прогона здесь нет; DONE без независимой проверки кода не даёт баллов.

B имитирует HTTP 503. C использует реальный loopback HTTP и контролируемые
ответы: семь корректных планов, неверные планы, задержку и отсутствие usage.
Сервер не читает references.json. Это имитации модели, не её измеренная
точность. Worker не принимает внешний proxy URL и не использует ключи из
окружения пользователя. Счётчики usage синтетически вычисляются по длине текста,
не являются измерением токенизации. Цена: input/10 + output. При неизвестном
usage показаны известный нижний расход, удержанный резерв и frozen state;
резерв не выдаётся за подтверждённый расход.

Статусы: correct, wrong, skipped, unverified (например, непроверенный DONE).
Баллы получает только correct. Ответ на задачу без определённого результата
считается wrong. Пропуски делятся на обоснованные и ложные.

Политики: точное равенство рациональных чисел (1/2 = 0.5, 1/3 != 0.333);
точный текст после внешнего whitespace; канонический JSON без повторных ключей,
с сохранением различий true/1 и 1/1.0.
Неизвестная конкурсная совместимость нецелых/JSON-ответов помечена отдельно.
Эти правила диагностики не приписываются скрытому оценщику.

false_positive_fraction = wrong / созданные ответы (null, если ответов нет).
justified_refusal_recall = обоснованные отказы / задачи без определённого ответа.
Доля обоснованных среди всех отказов считается отдельно. possible_reward —
сумма reward всех задач с определённым эталоном, включая пропускаемый coding.

Время по типам/сложности — сумма workflow.invoke. Импорт, preflight, сохранение
и запуск процесса входят только в общее wall-clock. Wall измеряется от создания
рабочей копии до чтения результатов; первичная валидация manifest и запись
отчётов в него не входят. Нулевое время узлов coding
означает отказ на preflight, а не бесплатную обработку metadata.

## Docker

```bash
.venv/bin/python -B benchmark/evaluate.py --backend docker --mode local \
  --image sidim-bez-dim:harness-v2-math --output /tmp/bench-docker
.venv/bin/python -B tests/docker_smoke.py --image sidim-bez-dim:harness-v2-math
```

Отдельный Docker benchmark проверяет все 128 задач в режиме A. Монтируются
копии tasks, один worker.py read-only и пустой каталог trace. Эталоны и mock
fixtures остаются на host. Используются network none, read-only root, tmpfs
2 ГиБ, cap-drop ALL, no-new-privileges, 16 ГиБ RAM, 8 CPU и 512 процессов.
Docker socket не монтируется; настройки Desktop/WSL не меняются.

Mock-режимы пока проверены только на host. Полный Docker smoke отдельно
монтирует unit-тесты и их benchmark-фикстуры read-only для тестирования оценщика.
Это другой контейнер. Аудит чистого образа проверяет отсутствие /app/benchmark;
образ не содержит ни набора, ни эталонов, ни отчётов. Allowlist .dockerignore
не менялся. Скачивание данных и установка зависимостей при запуске не нужны.
