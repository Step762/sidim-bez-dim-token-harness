# Benchmark diagnostic-128-v1

Режим: `mock-good`; backend: `host`; стратегия: `priority`; BUDGET=1000.
Выполнение: real agent, controlled local HTTP simulation. Недоверенный coding выключен.

| Тип | Всего | Верно | Неверно | Пропущено | Баллы / доступные | Время узлов, с | LLM, условные ед. |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| math | 1 | 1 | 0 | 0 | 2/2 | 0.0467 | 81.6 |
| search | 1 | 1 | 0 | 0 | 5/5 | 0.1340 | 62.0 |

Wall-clock: 7.215 с; собственно агент: 0.238 с.
Время по типам — сумма измерений workflow.invoke; загрузка, preflight и публикация входят только в общее время.
Budget state: `{"available": "856.4", "frozen": false, "limit": 1000, "requests": [{"estimated_input": 1823, "input_tokens": 120, "max_output": 256, "output_tokens": 50, "request_id": "eea30407fa034083a2cc664687e1ef81", "reserved_tenths": 4383, "spent_tenths": 620, "state": "settled", "task_id": "/tmp/benchmark-run-c57bzeoj/tasks/search_d15"}, {"estimated_input": 1880, "input_tokens": 86, "max_output": 256, "output_tokens": 73, "request_id": "55353c83ab2d4ccc9e77527ca869bd80", "reserved_tenths": 4440, "spent_tenths": 816, "state": "settled", "task_id": "/tmp/benchmark-run-c57bzeoj/tasks/math_d15"}], "reserved": "0.0", "spent": "143.6", "tasks": {"/tmp/benchmark-run-c57bzeoj/tasks/math_d15": {"calls": 1, "reserved_tenths": 0, "spent_tenths": 816}, "/tmp/benchmark-run-c57bzeoj/tasks/search_d15": {"calls": 1, "reserved_tenths": 0, "spent_tenths": 620}}, "uncertain_calls": 0}`.

LLM usage здесь синтетическая статистика мока. При timeout/ошибке/нет usage известный расход — лишь нижняя граница; резерв не объявляется потраченным.
Числовая политика benchmark: точное равенство рациональных чисел (1/2 = 0.5). Текст: точное совпадение после внешнего whitespace. JSON: равенство структуры без повторов ключей.
Конкурсная совместимость нецелых чисел и структурированных ответов неизвестна; такие строки помечены отдельно. DONE без независимой проверки кода получает unverified и 0 баллов.

## Development / holdout

| Набор | Верно | Неверно | Пропущено | Баллы |
| --- | ---: | ---: | ---: | ---: |
| development | 2 | 0 | 0 | 7/7 |

## Все результаты

| ID | Сложность | Статус | Ответ | Эталон | Причина |
| --- | --- | --- | --- | --- | --- |
| math_d15 | medium | correct | 76 | 76 |  |
| search_d15 | medium | correct | Porto | Porto |  |
