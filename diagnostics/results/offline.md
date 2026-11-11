# Cycle A: диагностика

Режим: offline_suite; live=False.

Mock-результаты не подтверждают NeuralDeep. Реальные вызовы возможны только с отдельным разрешением.

## valid

Расход: `{"limit": 2500, "spent": "90.0", "reserved": "0.0", "available": "2410.0", "frozen": false, "uncertain_calls": 0}`.

| ID | Оценка | Категория | Причина | Запрос отправлен | План | Исполнение |
| --- | --- | --- | --- | --- | --- | --- |
| math_d01 | correct | success | local | False | not_assessed | success |
| math_d08 | skipped | routing | context_or_local_evidence_gate | False | not_assessed | not_reached |
| math_d09 | skipped | routing | context_or_local_evidence_gate | False | not_assessed | not_reached |
| math_d15 | correct | success | llm | True | equivalent | success |
| search_d01 | correct | success | local | False | not_assessed | success |
| search_d05 | correct | success | local | False | not_assessed | success |
| search_d14 | skipped | routing | context_or_local_evidence_gate | False | not_assessed | not_reached |
| search_d15 | correct | success | llm | True | equivalent | success |
| data_analysis_d06 | correct | success | local | False | not_assessed | success |
| data_analysis_d11 | skipped | appropriate_skip | no_reliable_answer | False | not_assessed | not_reached |
| data_analysis_d12 | skipped | routing | context_or_local_evidence_gate | False | not_assessed | not_reached |
| data_analysis_d14 | correct | success | llm | True | equivalent | success |
## wrong_plan

Расход: `{"limit": 2500, "spent": "30.0", "reserved": "0.0", "available": "2470.0", "frozen": false, "uncertain_calls": 0}`.

| ID | Оценка | Категория | Причина | Запрос отправлен | План | Исполнение |
| --- | --- | --- | --- | --- | --- | --- |
| math_d01 | correct | success | local | False | not_assessed | success |
| math_d15 | skipped | model_meaning | incorrect_plan | True | incorrect | not_reached |
## equivalent_rejected

Расход: `{"limit": 2500, "spent": "30.0", "reserved": "0.0", "available": "2470.0", "frozen": false, "uncertain_calls": 0}`.

| ID | Оценка | Категория | Причина | Запрос отправлен | План | Исполнение |
| --- | --- | --- | --- | --- | --- | --- |
| math_d01 | correct | success | local | False | not_assessed | success |
| math_d15 | skipped | validator_rejection | equivalent_plan_rejected | True | equivalent | not_reached |
## execution_error

Расход: `{"limit": 2500, "spent": "30.0", "reserved": "0.0", "available": "2470.0", "frozen": false, "uncertain_calls": 0}`.

| ID | Оценка | Категория | Причина | Запрос отправлен | План | Исполнение |
| --- | --- | --- | --- | --- | --- | --- |
| math_d01 | correct | success | local | False | not_assessed | success |
| math_d15 | skipped | execution | executor_failed | True | equivalent | error |
## unroutable

Расход: `{"limit": 2500, "spent": "0.0", "reserved": "0.0", "available": "2500.0", "frozen": false, "uncertain_calls": 0}`.

| ID | Оценка | Категория | Причина | Запрос отправлен | План | Исполнение |
| --- | --- | --- | --- | --- | --- | --- |
| math_d08 | skipped | routing | context_or_local_evidence_gate | False | not_assessed | not_reached |
| search_d14 | skipped | routing | context_or_local_evidence_gate | False | not_assessed | not_reached |
| data_analysis_d12 | skipped | routing | context_or_local_evidence_gate | False | not_assessed | not_reached |
## http_400

Расход: `{"limit": 2500, "spent": "0.0", "reserved": "700.0", "available": "0.0", "frozen": true, "uncertain_calls": 1}`.

| ID | Оценка | Категория | Причина | Запрос отправлен | План | Исполнение |
| --- | --- | --- | --- | --- | --- | --- |
| math_d01 | correct | success | local | False | not_assessed | success |
| math_d15 | skipped | api_or_budget | http_400 | True | not_assessed | not_reached |
## http_401

Расход: `{"limit": 2500, "spent": "0.0", "reserved": "700.0", "available": "0.0", "frozen": true, "uncertain_calls": 1}`.

| ID | Оценка | Категория | Причина | Запрос отправлен | План | Исполнение |
| --- | --- | --- | --- | --- | --- | --- |
| math_d01 | correct | success | local | False | not_assessed | success |
| math_d15 | skipped | api_or_budget | http_401 | True | not_assessed | not_reached |
## http_429

Расход: `{"limit": 2500, "spent": "0.0", "reserved": "700.0", "available": "0.0", "frozen": true, "uncertain_calls": 1}`.

| ID | Оценка | Категория | Причина | Запрос отправлен | План | Исполнение |
| --- | --- | --- | --- | --- | --- | --- |
| math_d01 | correct | success | local | False | not_assessed | success |
| math_d15 | skipped | api_or_budget | http_429 | True | not_assessed | not_reached |
## http_500

Расход: `{"limit": 2500, "spent": "0.0", "reserved": "700.0", "available": "0.0", "frozen": true, "uncertain_calls": 1}`.

| ID | Оценка | Категория | Причина | Запрос отправлен | План | Исполнение |
| --- | --- | --- | --- | --- | --- | --- |
| math_d01 | correct | success | local | False | not_assessed | success |
| math_d15 | skipped | api_or_budget | http_500 | True | not_assessed | not_reached |
## http_503

Расход: `{"limit": 2500, "spent": "0.0", "reserved": "700.0", "available": "0.0", "frozen": true, "uncertain_calls": 1}`.

| ID | Оценка | Категория | Причина | Запрос отправлен | План | Исполнение |
| --- | --- | --- | --- | --- | --- | --- |
| math_d01 | correct | success | local | False | not_assessed | success |
| math_d15 | skipped | api_or_budget | http_503 | True | not_assessed | not_reached |
## timeout

Расход: `{"limit": 2500, "spent": "0.0", "reserved": "700.0", "available": "0.0", "frozen": true, "uncertain_calls": 1}`.

| ID | Оценка | Категория | Причина | Запрос отправлен | План | Исполнение |
| --- | --- | --- | --- | --- | --- | --- |
| math_d01 | correct | success | local | False | not_assessed | success |
| math_d15 | skipped | api_or_budget | timeout | True | not_assessed | not_reached |
## missing_usage

Расход: `{"limit": 2500, "spent": "0.0", "reserved": "700.0", "available": "0.0", "frozen": true, "uncertain_calls": 1}`.

| ID | Оценка | Категория | Причина | Запрос отправлен | План | Исполнение |
| --- | --- | --- | --- | --- | --- | --- |
| math_d01 | correct | success | local | False | not_assessed | success |
| math_d15 | skipped | api_or_budget | usage_missing | True | not_assessed | not_reached |
## partial_usage

Расход: `{"limit": 2500, "spent": "0.0", "reserved": "700.0", "available": "0.0", "frozen": true, "uncertain_calls": 1}`.

| ID | Оценка | Категория | Причина | Запрос отправлен | План | Исполнение |
| --- | --- | --- | --- | --- | --- | --- |
| math_d01 | correct | success | local | False | not_assessed | success |
| math_d15 | skipped | api_or_budget | usage_incomplete | True | not_assessed | not_reached |
## exhausted

Расход: `{"limit": 1, "spent": "0.0", "reserved": "0.0", "available": "1.0", "frozen": false, "uncertain_calls": 0}`.

| ID | Оценка | Категория | Причина | Запрос отправлен | План | Исполнение |
| --- | --- | --- | --- | --- | --- | --- |
| math_d01 | correct | success | local | False | not_assessed | success |
| math_d15 | skipped | api_or_budget | budget_blocked | False | not_assessed | not_reached |
## zero_budget

Расход: `{"limit": 0, "spent": "0.0", "reserved": "0.0", "available": "0.0", "frozen": false, "uncertain_calls": 0}`.

| ID | Оценка | Категория | Причина | Запрос отправлен | План | Исполнение |
| --- | --- | --- | --- | --- | --- | --- |
| math_d01 | correct | success | local | False | not_assessed | success |
| math_d15 | skipped | api_or_budget | budget_blocked | False | not_assessed | not_reached |
## truncated_json

Расход: `{"limit": 2500, "spent": "30.0", "reserved": "0.0", "available": "2470.0", "frozen": false, "uncertain_calls": 0}`.

| ID | Оценка | Категория | Причина | Запрос отправлен | План | Исполнение |
| --- | --- | --- | --- | --- | --- | --- |
| math_d01 | correct | success | local | False | not_assessed | success |
| math_d15 | skipped | response_format | plan_json_invalid | True | not_assessed | not_reached |
## outer_json_invalid

Расход: `{"limit": 2500, "spent": "0.0", "reserved": "700.0", "available": "0.0", "frozen": true, "uncertain_calls": 1}`.

| ID | Оценка | Категория | Причина | Запрос отправлен | План | Исполнение |
| --- | --- | --- | --- | --- | --- | --- |
| math_d01 | correct | success | local | False | not_assessed | success |
| math_d15 | skipped | response_format | envelope_json_invalid | True | not_assessed | not_reached |
## partial_body

Расход: `{"limit": 2500, "spent": "0.0", "reserved": "700.0", "available": "0.0", "frozen": true, "uncertain_calls": 1}`.

| ID | Оценка | Категория | Причина | Запрос отправлен | План | Исполнение |
| --- | --- | --- | --- | --- | --- | --- |
| math_d01 | correct | success | local | False | not_assessed | success |
| math_d15 | skipped | api_or_budget | partial_response | True | not_assessed | not_reached |
## finish_length

Расход: `{"limit": 2500, "spent": "522.0", "reserved": "0.0", "available": "1978.0", "frozen": false, "uncertain_calls": 0}`.

| ID | Оценка | Категория | Причина | Запрос отправлен | План | Исполнение |
| --- | --- | --- | --- | --- | --- | --- |
| math_d01 | correct | success | local | False | not_assessed | success |
| math_d15 | skipped | response_format | generation_truncated | True | not_assessed | not_reached |
## content_array

Расход: `{"limit": 2500, "spent": "30.0", "reserved": "0.0", "available": "2470.0", "frozen": false, "uncertain_calls": 0}`.

| ID | Оценка | Категория | Причина | Запрос отправлен | План | Исполнение |
| --- | --- | --- | --- | --- | --- | --- |
| math_d01 | correct | success | local | False | not_assessed | success |
| math_d15 | skipped | response_format | unsupported_content_type | True | not_assessed | not_reached |
## save_error

Расход: `{"limit": 2500, "spent": "30.0", "reserved": "0.0", "available": "2470.0", "frozen": false, "uncertain_calls": 0}`.

| ID | Оценка | Категория | Причина | Запрос отправлен | План | Исполнение |
| --- | --- | --- | --- | --- | --- | --- |
| math_d01 | correct | success | local | False | not_assessed | success |
| math_d15 | skipped | answer_publication | save_failed | True | equivalent | success |
## format_error

Расход: `{"limit": 2500, "spent": "30.0", "reserved": "0.0", "available": "2470.0", "frozen": false, "uncertain_calls": 0}`.

| ID | Оценка | Категория | Причина | Запрос отправлен | План | Исполнение |
| --- | --- | --- | --- | --- | --- | --- |
| math_d01 | correct | success | local | False | not_assessed | success |
| math_d15 | wrong | answer_publication | format_or_written_value | True | equivalent | success |

Проверки классификации: passed=True; failures=0.
