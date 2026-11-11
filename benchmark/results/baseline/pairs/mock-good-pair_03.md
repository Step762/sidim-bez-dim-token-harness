# Benchmark diagnostic-128-v1

Режим: `mock-good`; backend: `host`; стратегия: `priority`; BUDGET=1000.
Выполнение: real agent, controlled local HTTP simulation. Недоверенный coding выключен.

| Тип | Всего | Верно | Неверно | Пропущено | Баллы / доступные | Время узлов, с | LLM, условные ед. |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| math | 2 | 2 | 0 | 0 | 2/2 | 0.1571 | 189.7 |

Wall-clock: 6.868 с; собственно агент: 0.192 с.
Время по типам — сумма измерений workflow.invoke; загрузка, preflight и публикация входят только в общее время.
Budget state: `{"available": "810.3", "frozen": false, "limit": 1000, "requests": [{"estimated_input": 1880, "input_tokens": 86, "max_output": 256, "output_tokens": 73, "request_id": "3131c68ad7294148b25b5aa3d57f6c40", "reserved_tenths": 4440, "spent_tenths": 816, "state": "settled", "task_id": "/tmp/benchmark-run-ifqcl2ge/tasks/math_d15"}, {"estimated_input": 1900, "input_tokens": 91, "max_output": 256, "output_tokens": 99, "request_id": "54141e64a5424c12bb48fb0421235498", "reserved_tenths": 4460, "spent_tenths": 1081, "state": "settled", "task_id": "/tmp/benchmark-run-ifqcl2ge/tasks/math_h15"}], "reserved": "0.0", "spent": "189.7", "tasks": {"/tmp/benchmark-run-ifqcl2ge/tasks/math_d15": {"calls": 1, "reserved_tenths": 0, "spent_tenths": 816}, "/tmp/benchmark-run-ifqcl2ge/tasks/math_h15": {"calls": 1, "reserved_tenths": 0, "spent_tenths": 1081}}, "uncertain_calls": 0}`.

LLM usage здесь синтетическая статистика мока. При timeout/ошибке/нет usage известный расход — лишь нижняя граница; резерв не объявляется потраченным.
Числовая политика benchmark: точное равенство рациональных чисел (1/2 = 0.5). Текст: точное совпадение после внешнего whitespace. JSON: равенство структуры без повторов ключей.
Конкурсная совместимость нецелых чисел и структурированных ответов неизвестна; такие строки помечены отдельно. DONE без независимой проверки кода получает unverified и 0 баллов.

## Development / holdout

| Набор | Верно | Неверно | Пропущено | Баллы |
| --- | ---: | ---: | ---: | ---: |
| development | 1 | 0 | 0 | 2/2 |
| holdout | 1 | 0 | 0 | 0/0 |

## Все результаты

| ID | Сложность | Статус | Ответ | Эталон | Причина |
| --- | --- | --- | --- | --- | --- |
| math_d15 | medium | correct | 76 | 76 |  |
| math_h15 | medium | correct | 53 | 53 |  |
