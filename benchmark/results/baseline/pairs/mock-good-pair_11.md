# Benchmark diagnostic-128-v1

Режим: `mock-good`; backend: `host`; стратегия: `priority`; BUDGET=1000.
Выполнение: real agent, controlled local HTTP simulation. Недоверенный coding выключен.

| Тип | Всего | Верно | Неверно | Пропущено | Баллы / доступные | Время узлов, с | LLM, условные ед. |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| data_analysis | 1 | 1 | 0 | 0 | 5/5 | 0.1322 | 84.6 |
| math | 1 | 1 | 0 | 0 | 0/0 | 0.0549 | 108.1 |

Wall-clock: 6.824 с; собственно агент: 0.232 с.
Время по типам — сумма измерений workflow.invoke; загрузка, preflight и публикация входят только в общее время.
Budget state: `{"available": "807.3", "frozen": false, "limit": 1000, "requests": [{"estimated_input": 2062, "input_tokens": 116, "max_output": 256, "output_tokens": 73, "request_id": "0e014d5e6dbb4b48b4c5427581dc8c8e", "reserved_tenths": 4622, "spent_tenths": 846, "state": "settled", "task_id": "/tmp/benchmark-run-tt0ctnbs/tasks/data_analysis_h15"}, {"estimated_input": 1900, "input_tokens": 91, "max_output": 256, "output_tokens": 99, "request_id": "7ec568d823544a83b771f761c4939986", "reserved_tenths": 4460, "spent_tenths": 1081, "state": "settled", "task_id": "/tmp/benchmark-run-tt0ctnbs/tasks/math_h15"}], "reserved": "0.0", "spent": "192.7", "tasks": {"/tmp/benchmark-run-tt0ctnbs/tasks/data_analysis_h15": {"calls": 1, "reserved_tenths": 0, "spent_tenths": 846}, "/tmp/benchmark-run-tt0ctnbs/tasks/math_h15": {"calls": 1, "reserved_tenths": 0, "spent_tenths": 1081}}, "uncertain_calls": 0}`.

LLM usage здесь синтетическая статистика мока. При timeout/ошибке/нет usage известный расход — лишь нижняя граница; резерв не объявляется потраченным.
Числовая политика benchmark: точное равенство рациональных чисел (1/2 = 0.5). Текст: точное совпадение после внешнего whitespace. JSON: равенство структуры без повторов ключей.
Конкурсная совместимость нецелых чисел и структурированных ответов неизвестна; такие строки помечены отдельно. DONE без независимой проверки кода получает unverified и 0 баллов.

## Development / holdout

| Набор | Верно | Неверно | Пропущено | Баллы |
| --- | ---: | ---: | ---: | ---: |
| holdout | 2 | 0 | 0 | 5/5 |

## Все результаты

| ID | Сложность | Статус | Ответ | Эталон | Причина |
| --- | --- | --- | --- | --- | --- |
| data_analysis_h15 | medium | correct | -9 | -9 |  |
| math_h15 | medium | correct | 53 | 53 |  |
