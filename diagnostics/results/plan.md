# Cycle A: диагностика

Режим: plan; live=False.

Mock-результаты не подтверждают NeuralDeep. Реальные вызовы возможны только с отдельным разрешением.

## План

Модель: qwen3.6-35b-a3b-noreason. Предполагаемый endpoint: https://api.neuraldeep.ru/v1/chat/completions.
A1: до 3 запросов, BUDGET=600, сумма оценочных резервов 493.1.
A2: 12 задач, до 3 запросов, BUDGET=2500, оценочные резервы 2115.1.
Это планируемый расход, не доказанный верхний предел удалённого сервера.

| ID | Тип | Reward | Эталон оценщика | Локальный статус | Возможен LLM | Резерв при вызове | Причина выбора |
| --- | --- | ---: | --- | --- | --- | ---: | --- |
| math_d01 | math | 1 | 163 | correct | False | None | Дешёвый локальный контроль; LLM не должен вызываться. |
| math_d08 | math | 0 | 29 | skipped | False | None | Нулевая награда, степень словами: неподдерживаемая операция. |
| math_d09 | math | 3 | 7.24 | skipped | False | None | Явное round-half-up; модель потенциально понимает, грамматика нет. |
| math_d15 | math | 2 | 76 | skipped | True | 700 | Локальный отказ, проверяемая арифметика; также эквивалентный план add(-8). |
| search_d01 | search | 13 | 1974 | correct | False | 681.2 | Прямой факт из собственной Markdown-базы. |
| search_d05 | search | 3 | Porto | correct | False | 695.9 | Цепочка Mentor → Employer → Headquarters в разных документах. |
| search_d14 | search | 2 | 1978 | skipped | False | None | Непротиворечивые части профиля, отклонённые индексом как конфликт. |
| search_d15 | search | 5 | Porto | skipped | True | 694.3 | Непривычная формулировка и цепочка связей, доступные fallback. |
| data_analysis_d06 | data_analysis | 8 | 12 | correct | False | 721.1 | CSV с несколькими фильтрами и включёнными границами. |
| data_analysis_d11 | data_analysis | 1 | None | skipped | False | 710.6 | Повреждённое число: разумный отказ без запроса модели. |
| data_analysis_d12 | data_analysis | 13 | {"east":57,"west":23} | skipped | False | None | Группировка: необходимой операции нет в текущей схеме. |
| data_analysis_d14 | data_analysis | 8 | 35 | skipped | True | 720.8 | Сложная словесная фильтрация CSV, доступная fallback. |
