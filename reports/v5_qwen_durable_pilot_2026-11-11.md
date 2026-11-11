# P3.2 — Durable One-Shot Qwen Pilot Guard

11 ноября 2026. Ветка `feature/harness-v1`, HEAD
`3ecb1e1b1d0b42b905ec591de00388ba9ac86ca9`.

**Durable профиль проверен на моках; LIVE остаётся закрыт.**
Реальных HTTP/LLM-запросов: **0**. Production runtime не изменён.
Это узкий профиль существующего diagnostic runner, не второй gateway/framework.

## Файлы

- `diagnostics/qwen_profile.py` — фиксированный synthetic Direct Answer профиль,
  preflight, вызов через прежний gateway, durable intent/result и read-only recover.
- `diagnostics/fallback_pilot.py` — только dispatch `--qwen-direct` в этот профиль;
  прежние math/search ветви и их проверки не переписаны.
- `tests/test_qwen_profile.py` — 21 новый тест, включая реальные дочерние процессы
  с MockTransport и временными состояниями.
- `tests/allowed_acceptance.py` — новый модуль и его diagnostic dependency добавлены
  в явный allowlist для будущей полной регрессии.
- `tests/docker_smoke.py` — только ожидаемое число тестов: 835/828/7 вместо
  814/807/7, ровно +21 тест. Ограничения безопасности и assertions не ослаблены.
- `README.md` — число allowlisted тестовых модулей 49 вместо 48.
- Этот отчёт.

BudgetManager, gateway, harness, planner, run_agent, RuntimeLimits, Math/Search/Data,
Semantic Compiler и Coding не изменены. Старые a1_resume/cycle_a и реальные
durable journals также не изменены. Production LLM не запускался; новых env
переключателей или автоматической активации в production нет.

## Фиксированный контракт и цена

| Параметр | Значение |
|---|---|
| Профиль | p3_proxy_direct_v1 |
| Синтетическая инструкция | Compute (7 - 3) |
| Ожидаемый внутренний JSON | `{"value":"4"}` |
| Модель | Только qwen3.6-35b-a3b-noreason, явно переданная и подтверждённая владельцем |
| Новая попытка | Максимум одна; без retry и redirects |
| max_tokens / timeout | 32 /20 секунд |
| BUDGET | Явно 200 выходных эквивалентов; другое значение не подставляется |
| Input estimate | 1217 по существующей bytes+1024 эвристике |
| Резерв точного prompt | **153.7** =1217/10+32 |
| Предельный разрешённый резерв | **182.0** при estimate≤1500 |

Формула BudgetManager не менялась: input/10 + accounted output.
200 — локальный лимит, не прогноз расхода запроса. Фактическая стоимость
реального Proxy неизвестна. Эвристика input и неподтверждённый серверный cap
не дают абсолютной гарантии удалённой стоимости; при наблюдённом превышении
manager показывает расход и блокируется, не скрывает превышение.

История не обнуляется по умолчанию. Approval должен явно содержать полную
числовую историю input/accounted output и подтверждение её полноты.
Пустой список допустим только как явное утверждение владельца о соответствующей
квоте; отсутствующий список отвергается. Статистика прошлого восстанавливается
тем же BudgetManager до допуска новой попытки, а не вторым расчётом цены.

В тесте публичная история 155.9 плюс резерв153.7 **не помещается** в200:
preflight отказывает до intent и транспорта. Профиль не заменяет прежний
общий лимит900 новым200 и не выдаёт остаток старого API за новую квоту.
Scope квоты и отсутствие неизвестных расходов должен подтвердить владелец.

## Как исключается повтор со стороны runner

1. Новый фиксированный state находится в домашнем каталоге **OS account**, под
   `.local/state/sidim-bez-dim/p3_proxy_direct_v1`. Он не зависит от cwd, клона,
   HOME, output или endpoint и не пересекается с прежними project-local journals.
   CLI не предлагает другой `--state-dir` для получения новой попытки.
2. Родитель persistent store и каталог профиля должны принадлежать пользователю
   и иметь0700. Все компоненты открываются через прежний open_directory без
   symlink. Небезопасный/неизвестный store — отказ.
3. До gateway.complete создаётся `attempt.json` через существующий write_once:
   **O_CREAT|O_EXCL|O_NOFOLLOW**,0600, flush/fsync файла и fsync каталога.
   Эксклюзивно создаётся имя claim; полный JSON при аварии может быть разорван.
   Любой уже существующий claim блокирует вторую отправку, независимо от его JSON.
4. Два процесса могут одновременно пройти предварительные проверки, но только
   один получит O_EXCL. Проигравший получает чистый отказ, не вызывает транспорт.
5. Crash до создания claim не мог предшествовать отправке: сетевого вызова ещё
   не было. Empty private directory остаётся unclaimed. Crash после claim,
   включая момент до отправки, трактуется консервативно как возможная отправка.
6. Timeout/ошибка/неизвестный usage не освобождают попытку. Gateway удерживает
   неопределённый резерв; результат содержит uncertain. Если процесс погиб до
   result, recover возвращает uncertain и нулевой допуск новых requests.
7. Подтверждённый result фиксируется отдельно через O_EXCL+fsync. При recover
   проверяется связь с intent, восстанавливается числовой расход прежним manager;
   успешный или отклонённый ответ всё равно означает consumed. Повтор запрещён.
8. Recovery — только чтение. Повреждённые, неизвестные и orphaned состояния,
   неправильные права или типы файлов не превращаются в свободный слот.

Гарантия относится к отсутствию повторного инициирования запроса нашим runner
при сохранённом общем state. **Exactly-once доставка или обработка внешним
сервером не обещается.** Все конкурирующие процессы должны использовать тот же
persistent FS/OS account. Удаление state, новый хост или восстановление старой
резервной копии не являются разрешённым способом продолжения серии.

## Подтверждения, секреты и граница LIVE

Approval читается только как ограниченный обычный файл: owner0600 в owner0700
каталоге. Exact schema требует:

```text
proxy_confirmed = true
model_confirmed = true
output_cap_confirmed = true
history_complete = true
usage_contract = "noreason_completion"
contract_sha256 = digest текущих параметров/prompt и runtime/diagnostic sources
endpoint_sha256 = SHA-256 нормализованного endpoint Chat Completions
quota_scope_sha256 = идентификатор согласованного scope квоты в виде SHA-256
history = явный список {input_tokens, accounted_output_tokens}
```

Missing/false/неверно типизированное подтверждение, лишнее поле, stale fingerprint,
смена endpoint/prompt, отсутствие model/BUDGET/URL/key и недостаточный резерв
отклоняются до фиксации попытки. Поля approval — **утверждения владельца**, не
автоматическое доказательство подлинности Proxy, права на ключ или наличия квоты.
Код не генерирует успешное подтверждение самостоятельно.

В intent/result сохраняются только идентификатор фиксированного профиля,
hashes, числовая история/резерв/usage/cost, время и JSON/schema/value flags.
API key, Authorization/Bearer, URL, полный prompt/payload и текст ответа не
сериализуются. Recovery выводит только проверенную безопасную сводку, не
произвольное содержимое JSON. Это не новый capture реального ответа модели.

Профиль не читает benchmark/holdout и не принимает произвольную задачу.
Старые A1/A2 receipts не подделываются; новый result не проходит A2 gate.
Существующие ограничения старых math/search one-shot сохраняются.

**В этой версии единственная исполняющая функция — run_mock**, которая до
preflight требует именно httpx.MockTransport. CLI `--live` отклоняется;
реальный transport не конструируется. Наличие заполненного approval или
production environment само по себе не включает LIVE.

## Проверки

Итоговый запуск: **109 passed, 0 failed/errors, 0 skipped**, unittest70.210s:
новый test_qwen_profile плюс test_budget, test_a1_resume, test_fallback_pilot,
test_search_pilot. Внутри —21 новый тест. Отдельный целевой запуск21/21 также прошёл.

Проверены:

- первый подтверждённый mock-вызов, повтор и смена параметров/approval;
- новый процесс после completed и после crash с возможной отправкой;
- два одновременно запущенных процесса: ровно один mock sender;
- crash до claim, после durable claim до gateway и после входа в mock transport;
- fsync failures файла/каталога: ни одного mock send, имя остаётся занятым;
- timeout,401/403/429/500, missing usage, неизвестный reasoning, output overrun;
- отсутствие всех обязательных переменных, неподтверждённый Proxy/model/cap/history;
- исчерпание бюджета с сохранением истории, завышенный input estimate;
- повреждённый state, symlink и небезопасные permissions;
- отсутствие ключа/headers/prompt/сырого ответа в state и отдельные JSON/schema/value;
- отказ настоящему transport и CLI LIVE, неизменность запрета A2.

В первоначальном тестовом прогоне обнаружены два случая: несовпадение tuple/list
в самом тесте и необработанный FileExistsError у проигравшего процесса. Второе
не давало вторую отправку, но исправлено на явный ResumeBlocked. Затем добавлена
строгая проверка приватности approval и повторена вся выбранная регрессия.

Только моки, без localhost HTTP тоже. Guard фиксирует **0 попыток реальной сети**.
Регрессия старых pilots использовала отдельную уже санитизированную development
view и синтетические receipts; исходные mixed fixtures и реальные journals не
читались. Условия/эталоны закрытого корпуса не открывались.

Все49 модулей импортированы для **discovery-only**:835 тестов. Полный набор не
исполнялся: он содержит реальные loopback HTTP-тесты, а этот этап разрешает
только MockTransport. Ожидаемые835/828/7 в smoke — контроль состава будущего
полного запуска, **не заявление о828 пройденных тестах P3.2**. Docker не запускался.

Локальные проверочные материалы без реальных секретов:
`/tmp/p3-2-durable-njprarie/`, итог `regress-final.json`.

## Команды и следующий допуск

Показать только публичный фиксированный контракт, без сети/state/ключей:

```bash
.venv/bin/python -B -m diagnostics.fallback_pilot --qwen-direct --describe
```

После ручной подготовки приватного persistent parent и **подтверждённого**
approval вне Git, с environment по безопасной инструкции P3.1:

```bash
.venv/bin/python -B -m diagnostics.fallback_pilot --qwen-direct --check \
  --model qwen3.6-35b-a3b-noreason --approval /absolute/private/approval.json
.venv/bin/python -B -m diagnostics.fallback_pilot --qwen-direct --recover
```

Эти команды не отправляют HTTP. `--check` не создаёт claim и не является
LIVE-permit. `--recover` не требует ключей/approval и ничего не освобождает.
Не удалять state для повторной попытки. Полный production run_agent.py не
является заменой этому одноразовому профилю.

**Можно ли сейчас разрешить1HTTP? Нет.** Durable часть проверена, но официальный
URL, точный доступный model ID, права на ключ/квота, usage contract и фактическое
действие max_tokens всё ещё не подтверждены. Кроме этого, real transport
намеренно sealed по ограничению этапа. Следующий отдельно разрешённый шаг —
приёмка явного LIVE-dispatch в этом же профиле на прежнем gateway, с этими
подтверждениями и численным разрешением владельца. Переделка production runtime
для этого не требуется; в P3.2 LIVE не включался.

## Git

Нет commit/push/merge. HEAD и refs main/feature/coding-solver прежние.
Существующие P2/P3.1 reports сохранены побайтно. Состав после P3.2:

```text
 M README.md
 M diagnostics/fallback_pilot.py
 M tests/allowed_acceptance.py
 M tests/docker_smoke.py
?? diagnostics/qwen_profile.py
?? reports/v5_official_contract_2026-11-11.md
?? reports/v5_qwen_durable_pilot_2026-11-11.md
?? reports/v5_qwen_preflight_2026-11-11.md
?? tests/test_qwen_profile.py
```

**Итог: MOCK GUARD VERIFIED / LIVE DISABLED / PROXY NEEDS CONFIRMATION.**
