# Benchmark diagnostic-128-v1

Режим: `mock-good`; backend: `host`; стратегия: `priority`; BUDGET=1000.
Выполнение: real agent, controlled local HTTP simulation. Недоверенный coding выключен.

| Тип | Всего | Верно | Неверно | Пропущено | Баллы / доступные | Время узлов, с | LLM, условные ед. |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| math | 2 | 0 | 0 | 2 | 0/16 | 0.0084 | 0.0 |

Wall-clock: 6.663 с; собственно агент: 0.025 с.
Время по типам — сумма измерений workflow.invoke; загрузка, preflight и публикация входят только в общее время.
Budget state: `{"available": "1000.0", "frozen": false, "limit": 1000, "requests": [], "reserved": "0.0", "spent": "0.0", "tasks": {}, "uncertain_calls": 0}`.

LLM usage здесь синтетическая статистика мока. При timeout/ошибке/нет usage известный расход — лишь нижняя граница; резерв не объявляется потраченным.
Числовая политика benchmark: точное равенство рациональных чисел (1/2 = 0.5). Текст: точное совпадение после внешнего whitespace. JSON: равенство структуры без повторов ключей.
Конкурсная совместимость нецелых чисел и структурированных ответов неизвестна; такие строки помечены отдельно. DONE без независимой проверки кода получает unverified и 0 баллов.

## Development / holdout

| Набор | Верно | Неверно | Пропущено | Баллы |
| --- | ---: | ---: | ---: | ---: |
| development | 0 | 0 | 1 | 0/3 |
| holdout | 0 | 0 | 1 | 0/13 |

## Все результаты

| ID | Сложность | Статус | Ответ | Эталон | Причина |
| --- | --- | --- | --- | --- | --- |
| math_d09 | medium | skipped | None | 7.24 | unsupported math statement or instructions |
| math_h08 | hard | skipped | None | 133.1 | unsupported math statement or instructions |
