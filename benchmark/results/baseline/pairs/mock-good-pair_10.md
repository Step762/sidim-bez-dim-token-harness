# Benchmark diagnostic-128-v1

Режим: `mock-good`; backend: `host`; стратегия: `priority`; BUDGET=1000.
Выполнение: real agent, controlled local HTTP simulation. Недоверенный coding выключен.

| Тип | Всего | Верно | Неверно | Пропущено | Баллы / доступные | Время узлов, с | LLM, условные ед. |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| data_analysis | 1 | 0 | 0 | 1 | 0/8 | 0.0060 | 0.0 |
| math | 1 | 0 | 0 | 1 | 0/0 | 0.0066 | 0.0 |

Wall-clock: 6.605 с; собственно агент: 0.036 с.
Время по типам — сумма измерений workflow.invoke; загрузка, preflight и публикация входят только в общее время.
Budget state: `{"available": "1000.0", "frozen": false, "limit": 1000, "requests": [], "reserved": "0.0", "spent": "0.0", "tasks": {}, "uncertain_calls": 0}`.

LLM usage здесь синтетическая статистика мока. При timeout/ошибке/нет usage известный расход — лишь нижняя граница; резерв не объявляется потраченным.
Числовая политика benchmark: точное равенство рациональных чисел (1/2 = 0.5). Текст: точное совпадение после внешнего whitespace. JSON: равенство структуры без повторов ключей.
Конкурсная совместимость нецелых чисел и структурированных ответов неизвестна; такие строки помечены отдельно. DONE без независимой проверки кода получает unverified и 0 баллов.

## Development / holdout

| Набор | Верно | Неверно | Пропущено | Баллы |
| --- | ---: | ---: | ---: | ---: |
| holdout | 0 | 0 | 2 | 0/8 |

## Все результаты

| ID | Сложность | Статус | Ответ | Эталон | Причина |
| --- | --- | --- | --- | --- | --- |
| data_analysis_h08 | medium | skipped | None | 2 | unsupported analysis instructions |
| math_h06 | medium | skipped | None | 18 | unsupported math statement or instructions |
