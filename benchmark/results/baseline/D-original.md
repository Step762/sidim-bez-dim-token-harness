# Benchmark diagnostic-128-v1

Режим: `mock-good`; backend: `host`; стратегия: `original`; BUDGET=1000.
Выполнение: real agent, controlled local HTTP simulation. Недоверенный coding выключен.

| Тип | Всего | Верно | Неверно | Пропущено | Баллы / доступные | Время узлов, с | LLM, условные ед. |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| coding | 32 | 0 | 0 | 32 | 0/131 | 0.0000 | 0.0 |
| data_analysis | 32 | 17 | 0 | 15 | 110/167 | 0.2628 | 203.9 |
| math | 32 | 15 | 0 | 17 | 67/116 | 0.1906 | 189.7 |
| search | 32 | 15 | 0 | 17 | 70/89 | 0.2716 | 179.9 |

Wall-clock: 7.946 с; собственно агент: 1.287 с.
Время по типам — сумма измерений workflow.invoke; загрузка, preflight и публикация входят только в общее время.
Budget state: `{"available": "426.5", "frozen": false, "limit": 1000, "requests": [{"estimated_input": 2088, "input_tokens": 123, "max_output": 256, "output_tokens": 107, "request_id": "ac5dc1ea44ae4f5cbb685d325c240499", "reserved_tenths": 4648, "spent_tenths": 1193, "state": "settled", "task_id": "/tmp/benchmark-run-i900i6ow/tasks/data_analysis_d14"}, {"estimated_input": 2062, "input_tokens": 116, "max_output": 256, "output_tokens": 73, "request_id": "ad880e46f43d48f38a2b63ba9a68e000", "reserved_tenths": 4622, "spent_tenths": 846, "state": "settled", "task_id": "/tmp/benchmark-run-i900i6ow/tasks/data_analysis_h15"}, {"estimated_input": 1880, "input_tokens": 86, "max_output": 256, "output_tokens": 73, "request_id": "6fa6242a73ba4f87b369a01c16c8d683", "reserved_tenths": 4440, "spent_tenths": 816, "state": "settled", "task_id": "/tmp/benchmark-run-i900i6ow/tasks/math_d15"}, {"estimated_input": 1900, "input_tokens": 91, "max_output": 256, "output_tokens": 99, "request_id": "9d1c49976c7f49bbb288ec20c9e2e8ae", "reserved_tenths": 4460, "spent_tenths": 1081, "state": "settled", "task_id": "/tmp/benchmark-run-i900i6ow/tasks/math_h15"}, {"estimated_input": 1823, "input_tokens": 120, "max_output": 256, "output_tokens": 50, "request_id": "61121a8b12b849e0a12308b70088911e", "reserved_tenths": 4383, "spent_tenths": 620, "state": "settled", "task_id": "/tmp/benchmark-run-i900i6ow/tasks/search_d15"}, {"estimated_input": 1825, "input_tokens": 121, "max_output": 256, "output_tokens": 48, "request_id": "969d0ebf07714d9591d672fd1109d186", "reserved_tenths": 4385, "spent_tenths": 601, "state": "settled", "task_id": "/tmp/benchmark-run-i900i6ow/tasks/search_h02"}, {"estimated_input": 1766, "input_tokens": 108, "max_output": 256, "output_tokens": 47, "request_id": "c96d5e79a2864a569fa4b0ab06d88275", "reserved_tenths": 4326, "spent_tenths": 578, "state": "settled", "task_id": "/tmp/benchmark-run-i900i6ow/tasks/search_h15"}], "reserved": "0.0", "spent": "573.5", "tasks": {"/tmp/benchmark-run-i900i6ow/tasks/data_analysis_d14": {"calls": 1, "reserved_tenths": 0, "spent_tenths": 1193}, "/tmp/benchmark-run-i900i6ow/tasks/data_analysis_h15": {"calls": 1, "reserved_tenths": 0, "spent_tenths": 846}, "/tmp/benchmark-run-i900i6ow/tasks/math_d15": {"calls": 1, "reserved_tenths": 0, "spent_tenths": 816}, "/tmp/benchmark-run-i900i6ow/tasks/math_h15": {"calls": 1, "reserved_tenths": 0, "spent_tenths": 1081}, "/tmp/benchmark-run-i900i6ow/tasks/search_d15": {"calls": 1, "reserved_tenths": 0, "spent_tenths": 620}, "/tmp/benchmark-run-i900i6ow/tasks/search_h02": {"calls": 1, "reserved_tenths": 0, "spent_tenths": 601}, "/tmp/benchmark-run-i900i6ow/tasks/search_h15": {"calls": 1, "reserved_tenths": 0, "spent_tenths": 578}}, "uncertain_calls": 0}`.

LLM usage здесь синтетическая статистика мока. При timeout/ошибке/нет usage известный расход — лишь нижняя граница; резерв не объявляется потраченным.
Числовая политика benchmark: точное равенство рациональных чисел (1/2 = 0.5). Текст: точное совпадение после внешнего whitespace. JSON: равенство структуры без повторов ключей.
Конкурсная совместимость нецелых чисел и структурированных ответов неизвестна; такие строки помечены отдельно. DONE без независимой проверки кода получает unverified и 0 баллов.

## Development / holdout

| Набор | Верно | Неверно | Пропущено | Баллы |
| --- | ---: | ---: | ---: | ---: |
| development | 25 | 0 | 39 | 153/287 |
| holdout | 22 | 0 | 42 | 94/216 |

## Все результаты

| ID | Сложность | Статус | Ответ | Эталон | Причина |
| --- | --- | --- | --- | --- | --- |
| coding_d01 | easy | skipped | None | DONE | unsafe |
| coding_d02 | easy | skipped | None | DONE | unsafe |
| coding_d03 | medium | skipped | None | DONE | unsafe |
| coding_d04 | medium | skipped | None | DONE | unsafe |
| coding_d05 | easy | skipped | None | DONE | unsafe |
| coding_d06 | medium | skipped | None | DONE | unsafe |
| coding_d07 | medium | skipped | None | DONE | unsafe |
| coding_d08 | medium | skipped | None | DONE | unsafe |
| coding_d09 | medium | skipped | None | DONE | unsafe |
| coding_d10 | medium | skipped | None | DONE | unsafe |
| coding_d11 | medium | skipped | None | DONE | unsafe |
| coding_d12 | medium | skipped | None | DONE | unsafe |
| coding_d13 | medium | skipped | None | DONE | unsafe |
| coding_d14 | medium | skipped | None | DONE | unsafe |
| coding_d15 | medium | skipped | None | None | unsafe |
| coding_d16 | medium | skipped | None | None | unsafe |
| coding_h01 | medium | skipped | None | DONE | unsafe |
| coding_h02 | medium | skipped | None | DONE | unsafe |
| coding_h03 | medium | skipped | None | DONE | unsafe |
| coding_h04 | medium | skipped | None | DONE | unsafe |
| coding_h05 | medium | skipped | None | DONE | unsafe |
| coding_h06 | medium | skipped | None | DONE | unsafe |
| coding_h07 | medium | skipped | None | DONE | unsafe |
| coding_h08 | medium | skipped | None | DONE | unsafe |
| coding_h09 | hard | skipped | None | DONE | unsafe |
| coding_h10 | medium | skipped | None | DONE | unsafe |
| coding_h11 | hard | skipped | None | DONE | unsafe |
| coding_h12 | medium | skipped | None | DONE | unsafe |
| coding_h13 | easy | skipped | None | DONE | unsafe |
| coding_h14 | medium | skipped | None | None | unsafe |
| coding_h15 | hard | skipped | None | DONE | unsafe |
| coding_h16 | medium | skipped | None | DONE | unsafe |
| data_analysis_d01 | easy | correct | 32 | 32 |  |
| data_analysis_d02 | easy | correct | 4 | 4 |  |
| data_analysis_d03 | easy | correct | -9 | -9 |  |
| data_analysis_d04 | easy | correct | 24 | 24 |  |
| data_analysis_d05 | medium | correct | 7 | 7 |  |
| data_analysis_d06 | medium | correct | 12 | 12 |  |
| data_analysis_d07 | medium | correct | 5 | 5 |  |
| data_analysis_d08 | easy | correct | 0 | 0 |  |
| data_analysis_d09 | medium | skipped | None | None | aggregate is undefined for an empty selection |
| data_analysis_d10 | medium | skipped | None | None | requested column is absent from CSV |
| data_analysis_d11 | medium | skipped | None | None | invalid or unsupported numeric value |
| data_analysis_d12 | hard | skipped | None | {"east":57,"west":23} | unsupported analysis instructions |
| data_analysis_d13 | medium | skipped | None | pin | unsupported analysis instructions |
| data_analysis_d14 | medium | correct | 35 | 35 |  |
| data_analysis_d15 | medium | skipped | None | 9 | filters must be joined with AND |
| data_analysis_d16 | medium | skipped | None | None | CSV cannot be read or contains invalid data |
| data_analysis_h01 | easy | correct | 4 | 4 |  |
| data_analysis_h02 | medium | correct | -9 | -9 |  |
| data_analysis_h03 | medium | correct | 1 | 1 |  |
| data_analysis_h04 | medium | correct | 23 | 23 |  |
| data_analysis_h05 | easy | correct | 0.15 | 3/20 |  |
| data_analysis_h06 | easy | correct | 1.5 | 1.5 |  |
| data_analysis_h07 | medium | skipped | None | 23/2 | unsupported analysis instructions |
| data_analysis_h08 | medium | skipped | None | 2 | unsupported analysis instructions |
| data_analysis_h09 | hard | skipped | None | east | unsupported analysis instructions |
| data_analysis_h10 | medium | skipped | None | None | insufficient_evidence |
| data_analysis_h11 | medium | skipped | None | None | missing, empty or duplicate CSV headers |
| data_analysis_h12 | medium | skipped | None | None | aggregate is undefined for an empty selection |
| data_analysis_h13 | medium | skipped | None | None | invalid or unsupported numeric value |
| data_analysis_h14 | medium | correct | -3 | -3 |  |
| data_analysis_h15 | medium | correct | -9 | -9 |  |
| data_analysis_h16 | hard | skipped | None | 358 | unsupported analysis instructions |
| math_d01 | easy | correct | 163 | 163 |  |
| math_d02 | easy | correct | -5.1 | -51/10 |  |
| math_d03 | easy | correct | 0.75 | 3/4 |  |
| math_d04 | easy | correct | -4 | -4 |  |
| math_d05 | medium | correct | 270 | 270 |  |
| math_d06 | medium | correct | 129 | 129 |  |
| math_d07 | easy | correct | 0.625 | 5/8 |  |
| math_d08 | medium | skipped | None | 29 | unsupported math statement or instructions |
| math_d09 | medium | skipped | None | 7.24 | unsupported math statement or instructions |
| math_d10 | medium | skipped | None | 74 | unsupported math statement or instructions |
| math_d11 | hard | skipped | None | 28 | unsupported math statement or instructions |
| math_d12 | medium | skipped | None | 34 | unsupported math statement or instructions |
| math_d13 | medium | skipped | None | None | division by zero |
| math_d14 | medium | skipped | None | None | unsupported math statement or instructions |
| math_d15 | medium | correct | 76 | 76 |  |
| math_d16 | medium | skipped | None | None | unsupported math statement or instructions |
| math_h01 | medium | correct | 87 | 87 |  |
| math_h02 | easy | correct | 0.725 | 29/40 |  |
| math_h03 | hard | correct | 2 | 2 |  |
| math_h04 | medium | correct | -6 | -6 |  |
| math_h05 | medium | skipped | None | -4 | unsupported math statement or instructions |
| math_h06 | medium | skipped | None | 18 | unsupported math statement or instructions |
| math_h07 | easy | skipped | None | 30 | unsupported math statement or instructions |
| math_h08 | hard | skipped | None | 133.1 | unsupported math statement or instructions |
| math_h09 | medium | skipped | None | 44 | unsupported math statement or instructions |
| math_h10 | easy | skipped | None | 10 | unsupported math statement or instructions |
| math_h11 | medium | correct | 26 | 26 |  |
| math_h12 | medium | skipped | None | 3 | unsupported math statement or instructions |
| math_h13 | medium | skipped | None | None | division by zero |
| math_h14 | medium | skipped | None | None | unsupported math statement or instructions |
| math_h15 | medium | correct | 53 | 53 |  |
| math_h16 | easy | correct | 30 | 30 |  |
| search_d01 | easy | correct | 1974 | 1974 |  |
| search_d02 | easy | correct | 2007 | 2007 |  |
| search_d03 | easy | correct | São Tomé | São Tomé |  |
| search_d04 | easy | correct | Aster Loom | Aster Loom |  |
| search_d05 | medium | correct | Porto | Porto |  |
| search_d06 | hard | correct | 1948 | 1948 |  |
| search_d07 | medium | skipped | None | Vela Nox | unsupported search question |
| search_d08 | medium | skipped | None | 1989 | missing or ambiguous born fact |
| search_d09 | medium | skipped | None | 1966 | entity or relation cannot be resolved |
| search_d10 | medium | skipped | None | None | conflicting or ambiguous entity definitions |
| search_d11 | medium | skipped | None | None | missing or ambiguous headquarters fact |
| search_d12 | hard | skipped | None | 1992 | entity or relation cannot be resolved |
| search_d13 | easy | correct | 1986 | 1986 |  |
| search_d14 | medium | skipped | None | 1978 | conflicting or ambiguous entity definitions |
| search_d15 | medium | correct | Porto | Porto |  |
| search_d16 | medium | skipped | None | None | insufficient_evidence |
| search_h01 | easy | correct | 2001 | 2001 |  |
| search_h02 | hard | correct | 1996 | 1996 |  |
| search_h03 | easy | correct | Ari Gale | Ari Gale |  |
| search_h04 | medium | correct | 1963 | 1963 |  |
| search_h05 | easy | correct | 2003 | 2003 |  |
| search_h06 | hard | skipped | None | ["Ari Gale","Nola Frost"] | unsupported search question |
| search_h07 | medium | skipped | None | None | entity or relation cannot be resolved |
| search_h08 | medium | skipped | None | 1991 | missing or ambiguous born fact |
| search_h09 | medium | skipped | None | 1988 | missing or ambiguous born fact |
| search_h10 | medium | skipped | None | None | conflicting or ambiguous entity definitions |
| search_h11 | medium | correct | 1998 | 1998 |  |
| search_h12 | medium | skipped | None | None | insufficient_evidence |
| search_h13 | medium | skipped | None | 1977 | conflicting or ambiguous entity definitions |
| search_h14 | medium | skipped | None | None | missing or ambiguous born fact |
| search_h15 | medium | correct | Faro | Faro |  |
| search_h16 | medium | skipped | None | None | missing or ambiguous born fact |
