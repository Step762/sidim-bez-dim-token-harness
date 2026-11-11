# Benchmark diagnostic-128-v1

Режим: `mock-good`; backend: `host`; стратегия: `priority`; BUDGET=1000.
Выполнение: real agent, controlled local HTTP simulation. Недоверенный coding выключен.

| Тип | Всего | Верно | Неверно | Пропущено | Баллы / доступные | Время узлов, с | LLM, условные ед. |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| math | 1 | 1 | 0 | 0 | 1/1 | 0.0052 | 0.0 |
| search | 1 | 1 | 0 | 0 | 5/5 | 0.1035 | 60.1 |

Wall-clock: 6.916 с; собственно агент: 0.148 с.
Время по типам — сумма измерений workflow.invoke; загрузка, preflight и публикация входят только в общее время.
Budget state: `{"available": "939.9", "frozen": false, "limit": 1000, "requests": [{"estimated_input": 1825, "input_tokens": 121, "max_output": 256, "output_tokens": 48, "request_id": "955235be911b442c9aec7db6e5959c91", "reserved_tenths": 4385, "spent_tenths": 601, "state": "settled", "task_id": "/tmp/benchmark-run-oyultf9i/tasks/search_h02"}], "reserved": "0.0", "spent": "60.1", "tasks": {"/tmp/benchmark-run-oyultf9i/tasks/search_h02": {"calls": 1, "reserved_tenths": 0, "spent_tenths": 601}}, "uncertain_calls": 0}`.

LLM usage здесь синтетическая статистика мока. При timeout/ошибке/нет usage известный расход — лишь нижняя граница; резерв не объявляется потраченным.
Числовая политика benchmark: точное равенство рациональных чисел (1/2 = 0.5). Текст: точное совпадение после внешнего whitespace. JSON: равенство структуры без повторов ключей.
Конкурсная совместимость нецелых чисел и структурированных ответов неизвестна; такие строки помечены отдельно. DONE без независимой проверки кода получает unverified и 0 баллов.

## Development / holdout

| Набор | Верно | Неверно | Пропущено | Баллы |
| --- | ---: | ---: | ---: | ---: |
| holdout | 2 | 0 | 0 | 6/6 |

## Все результаты

| ID | Сложность | Статус | Ответ | Эталон | Причина |
| --- | --- | --- | --- | --- | --- |
| math_h04 | medium | correct | -6 | -6 |  |
| search_h02 | hard | correct | 1996 | 1996 |  |
