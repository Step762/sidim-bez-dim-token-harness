# Benchmark diagnostic-128-v1

Режим: `mock-good`; backend: `host`; стратегия: `priority`; BUDGET=1000.
Выполнение: real agent, controlled local HTTP simulation. Недоверенный coding выключен.

| Тип | Всего | Верно | Неверно | Пропущено | Баллы / доступные | Время узлов, с | LLM, условные ед. |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| data_analysis | 1 | 1 | 0 | 0 | 8/8 | 0.1032 | 119.3 |
| search | 1 | 1 | 0 | 0 | 5/5 | 0.0499 | 57.8 |

Wall-clock: 6.896 с; собственно агент: 0.192 с.
Время по типам — сумма измерений workflow.invoke; загрузка, preflight и публикация входят только в общее время.
Budget state: `{"available": "822.9", "frozen": false, "limit": 1000, "requests": [{"estimated_input": 2088, "input_tokens": 123, "max_output": 256, "output_tokens": 107, "request_id": "2403e0e1d9e74fafac2ed982c730fd7c", "reserved_tenths": 4648, "spent_tenths": 1193, "state": "settled", "task_id": "/tmp/benchmark-run-hklan15n/tasks/data_analysis_d14"}, {"estimated_input": 1766, "input_tokens": 108, "max_output": 256, "output_tokens": 47, "request_id": "8e97cb70f4424b3592cee122571e8032", "reserved_tenths": 4326, "spent_tenths": 578, "state": "settled", "task_id": "/tmp/benchmark-run-hklan15n/tasks/search_h15"}], "reserved": "0.0", "spent": "177.1", "tasks": {"/tmp/benchmark-run-hklan15n/tasks/data_analysis_d14": {"calls": 1, "reserved_tenths": 0, "spent_tenths": 1193}, "/tmp/benchmark-run-hklan15n/tasks/search_h15": {"calls": 1, "reserved_tenths": 0, "spent_tenths": 578}}, "uncertain_calls": 0}`.

LLM usage здесь синтетическая статистика мока. При timeout/ошибке/нет usage известный расход — лишь нижняя граница; резерв не объявляется потраченным.
Числовая политика benchmark: точное равенство рациональных чисел (1/2 = 0.5). Текст: точное совпадение после внешнего whitespace. JSON: равенство структуры без повторов ключей.
Конкурсная совместимость нецелых чисел и структурированных ответов неизвестна; такие строки помечены отдельно. DONE без независимой проверки кода получает unverified и 0 баллов.

## Development / holdout

| Набор | Верно | Неверно | Пропущено | Баллы |
| --- | ---: | ---: | ---: | ---: |
| development | 1 | 0 | 0 | 8/8 |
| holdout | 1 | 0 | 0 | 5/5 |

## Все результаты

| ID | Сложность | Статус | Ответ | Эталон | Причина |
| --- | --- | --- | --- | --- | --- |
| data_analysis_d14 | medium | correct | 35 | 35 |  |
| search_h15 | medium | correct | Faro | Faro |  |
