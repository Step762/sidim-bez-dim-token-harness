# P3.2 continuation — явный NeuralDeep LIVE-dispatch

11 ноября 2026, feature/harness-v1, исходный HEAD
`c18566ef814f6e2b1d96f2301ef290a823f480f3`.

**Реальный запрос не выполнялся.** Подготовлен явный `--live` в существующем
`fallback_pilot --qwen-direct`, без нового runner/gateway и без изменения production.
Новый бюджет200 на эту пробу владелец отдельно подтвердил в текущем чате.
Старые серии, их бюджеты и state не сбрасывались.

## Изменения и ограничения

- `diagnostics/qwen_profile.py`: общий `_run` для mock/live; новый `run_live`
  требует именно `live=True`. CLI default и `--check/--describe/--recover`
  не отправляют запрос. Полное environment само по себе ничего не включает.
- Реальная цель зафиксирована: `https://api.neuraldeep.ru/v1/chat/completions`.
  URL всё равно обязательно поступает через LLM_BASE_URL; неизвестный URL
  или путь отклоняется до claim, даже с совпадающим approval на другой endpoint.
- `httpx.AsyncHTTPTransport(retries=0)` создаётся **после** O_EXCL+file/dir fsync
  intent. Вызов один, через существующий LLMGateway; redirects и env-proxy отключены.
  Транспорт закрывается через async context; удалённая отмена не гарантируется.
- Сохранились model/caps, private approval, hashes, история и бюджетные проверки.
  PROFILE и STATE_ROOT не изменены: даже прежний mock claim запрещает LIVE.
- `tests/test_qwen_profile.py`:8 новых тестов явного LIVE-пути, каждый подменяет
  реальный transport на MockTransport. Всего29 тестов профиля.
- `tests/docker_smoke.py`: только ожидаемый состав будущей suite843/836/7
  вместо835/828/7. Docker здесь не запускался.
- Этот отчёт. Остальные исходники/production-defaults не изменены.

Фиксированный пилот: **qwen3.6-35b-a3b-noreason**, максимум1HTTP,
max_tokens32, timeout20s, локальный BUDGET200.
Запрос: system требует один JSON со строковым value; instruction=`Compute (7 - 3)`;
ожидаемый внутренний JSON `{"value":"4"}`. Резерв153.7; верхний допустимый
резерв182.0 при input estimate≤1500. Реальный расход неизвестен.

Адрес, выбранный ID и принадлежность ключа программе NGU Huawei Hackathon
подтверждены владельцем. Это не новая проверка сервера и не доказательство
тождественности этого сервиса финальному конкурсному Proxy.

## Approval без ослабления защиты

Схема approval не изменена. Обязательны proxy/model/output-cap/history confirmation,
usage_contract, digest актуального contract, endpoint/quota-scope hashes и история.
Файл0600 и его каталог0700 должны принадлежать текущему OS account.
Смена исходников делает прежний approval устаревшим; существующий intent при этом
не освобождается и не перезаписывается.

Отдельный бюджет200 позволяет **явно** указать историю `[]` для нового локального
разрешения. Это не утверждение о нулевом историческом расходе аккаунта NeuralDeep
и не сброс старого лимита900/журналов. Баланс провайдера здесь не проверяется.

`output_cap_confirmed` не подтверждён новым сообщением владельца. Поэтому ниже
создаётся **неразрешающий шаблон** с False. Его нельзя менять на True только
ради прохождения проверки: сначала владелец должен подтвердить применимость
max_tokens к полной генерации выбранного noreason и ожидаемый usage-контракт.
Неверный или отсутствующий usage после отправки заморозит расход и закроет повтор.

## Подготовка владельцем — без HTTP

Команды ниже не выполнялись с настоящим ключом. Не публиковать approval/state,
не удалять их для повторного запуска. Шаблон не содержит ключа и не перезаписывает
существующий файл. Этот helper создания конфигурации не выполняет LLM-вызов.

```bash
cd ~/sidim-bez-dim-token-harness
umask 077
.venv/bin/python -B - <<'PY'
import os
from diagnostics import qwen_profile as p
from safe_files import open_directory
parent = p.STATE_ROOT.parent
parent.mkdir(parents=True, exist_ok=True, mode=0o700)
with open_directory(parent) as fd:
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or info.st_mode & 0o777 != 0o700:
        raise SystemExit('Private parent permissions must be reviewed')
    approval = {
        'proxy_confirmed': True, 'model_confirmed': True,
        'output_cap_confirmed': False,
        'history_complete': True, 'history': [],
        'usage_contract': 'noreason_completion',
        'contract_sha256': p.digest(p.contract()),
        'endpoint_sha256': p.hashlib.sha256(p.LIVE_ENDPOINT.encode()).hexdigest(),
        'quota_scope_sha256': p.digest('NGU NeuralDeep; new local grant200; p3_proxy_direct_v1; 2026-11-11'),
    }
    p.write_once(fd, 'neuraldeep-p3-approval.json', approval)
print('Non-authorising template created; output cap still requires confirmation')
PY
```

Proxy/model True отражают сведения владельца, а не наше обращение к API.
Проверить весь шаблон вручную; после реального подтверждения output-cap сохранить
соответствующее значение с теми же правами. Если информации нет — оставить False:
следующая команда безопасно откажет, не создав intent и не отправив HTTP.

Безопасная передача environment и **только preflight**:

```bash
(
  set +x
  export BUDGET=200
  export LLM_BASE_URL='https://api.neuraldeep.ru/v1'
  IFS= read -r -s -p 'NGU NeuralDeep key (hidden): ' LLM_API_KEY
  printf '\n'
  export LLM_API_KEY
  PILOT_APPROVAL=$(.venv/bin/python -B -c 'from diagnostics.qwen_profile import STATE_ROOT; print(STATE_ROOT.parent / "neuraldeep-p3-approval.json")')
  .venv/bin/python -B -m diagnostics.fallback_pilot --qwen-direct --check \
    --model qwen3.6-35b-a3b-noreason --approval "$PILOT_APPROVAL"
)
```

Ключ вводится без кавычек как данные read, не как текст команды; xtrace выключен,
в argv/файл он не попадает. Подоболочка не оставляет его в родительском environment.
Не применять echo/printenv или сохранение ключа в repo.
Успешный check — локальная проверка, не авторизация и не проверка баланса/сервера.

## Точная будущая команда одной разрешённой пробы — НЕ ЗАПУСКАТЬ СЕЙЧАС

Только после валидного approval, успешного preflight и **отдельного явного
разрешения владельца на один HTTP**. Блок читает ключ в своей подоболочке,
проверяет configuration и затем делает ровно одну попытку:

```bash
(
  set +x
  export BUDGET=200
  export LLM_BASE_URL='https://api.neuraldeep.ru/v1'
  IFS= read -r -s -p 'NGU NeuralDeep key (hidden): ' LLM_API_KEY
  printf '\n'
  export LLM_API_KEY
  PILOT_APPROVAL=$(.venv/bin/python -B -c 'from diagnostics.qwen_profile import STATE_ROOT; print(STATE_ROOT.parent / "neuraldeep-p3-approval.json")')
  .venv/bin/python -B -m diagnostics.fallback_pilot --qwen-direct --check \
    --model qwen3.6-35b-a3b-noreason --approval "$PILOT_APPROVAL" &&
  .venv/bin/python -B -m diagnostics.fallback_pilot --qwen-direct --live \
    --model qwen3.6-35b-a3b-noreason --approval "$PILOT_APPROVAL"
)
```

После любого исхода не повторять эту команду. Анализ без ключа и без HTTP:

```bash
.venv/bin/python -B -m diagnostics.fallback_pilot --qwen-direct --recover
```

Recover возвращает consumed или uncertain и запрещает повтор. Если процесса не
стало после intent, неизвестно, принял ли сервер запрос; это также не повод resend.
Exactly-once на сервере не обещается. Новый output/approval/endpoint не даёт
нового слота; CLI не предлагает изменить state directory.

## Проверки и оставшиеся риски

- **29/29** целевых тестов,57.458s.
- **117/117** mock-регрессий,93.890s unittest: новый профиль, BudgetManager,
  A1 resume и старые fallback/search pilots. Это включает29 новых/прежних тестов
  профиля, а не117+29 уникальных тестов.
- Проверены default/check/describe без транспорта, явный LIVE с MockTransport,
  retries0 и создание транспорта после минимум3 fsync, неверный endpoint,
  повтор, старый mock claim, гонка процессов, crash/factory failure, restart,
  timeout/401/429/500, missing/reasoning usage, finish length и overrun.
- Все LIVE-тесты подменяют фабрику транспорта; внешние sockets/DNS запрещены.
  Фактических HTTP и даже попыток реальной сети **0**.
- Полная suite только обнаружена:843. Полный WSL/Docker здесь не запускался;
  836 — ожидаемое число будущих passes, не результат этого этапа.
- Все четыре shell-блока прошли `bash -n`. Python-шаблон выполнен только
  во временном каталоге: права0600/0700 соблюдены, повторное создание запрещено,
  False для output-cap блокирует preflight без создания intent. Проверенные
  исходники совпадают с тестовым снимком;14 защищённых исходников и Git refs
  не изменились. Проверка секретов в четырёх изменённых/новых файлах совпадений
  не обнаружила; индекс пуст, `git diff --check` выполнен успешно.
- Реальный каталог профиля и его persistent parent при metadata-проверке
  отсутствовали. Они не создавались; старые journals/секретные файлы не читались.
  В environment этой сессии BUDGET/URL/key также отсутствуют.

Остаются риски: текущая доступность/квота аккаунта; действительный output cap и
детали usage/reasoning; bytes estimate не является доказанным tokenizer bound;
timeout не отменяет гарантированно удалённую генерацию. Неизвестный usage/сбой
могут стоить денег/токенов — они не записываются как бесплатный запрос.
Сохраняются строгие проверки model/finish_reason/JSON: совместимость сервера
может потребовать последующего разбора, но не автоматического повтора.
Статистика и JSON/schema/value flags сохраняются; сырой текст ответа по прежней
политике не записывается. Состояние должно сохраняться на одной persistent FS.

Production, BudgetManager и старые A1/A2 gates не изменены; новый профиль не
открывает A2 или MathIR/Compiler LIVE. Commit/push/merge не выполнялись.
Тестовые логи: `/tmp/neuraldeep-live-prep-3rzwpvkf/`, итог `regress-final.json`.

Git status: ветка `feature/harness-v1`, HEAD `c18566e`; изменены
`diagnostics/qwen_profile.py`, `tests/test_qwen_profile.py`, `tests/docker_smoke.py`;
новый файл — этот отчёт. Staged-файлов нет.

**Итог: explicit LIVE-dispatch подготовлен и проверен только на моках.
Фактический запуск отложен до отдельного разрешения и валидного approval.**

## P3.3 — экспериментальный checkpoint, approval заблокирован

Проверка публичных [Chat Completion docs](https://neuraldeep.ru/docs/chat.md)
и [полной справки](https://neuraldeep.ru/llms-full.txt) подтвердила наличие
`max_tokens` в примерах и отключение reasoning через выбранный `-noreason` ID.
Явной гарантии, что лимит32 охватывает все учитываемые выходные токены,
и описания связи `completion_tokens`/`reasoning_tokens` с этим пределом не найдено.
Поэтому валидный approval не создавался, `output_cap_confirmed` не подтверждён.
Следующий шаг — письменное подтверждение этих свойств от NeuralDeep.

Перед checkpoint отдельно проверен явный `--live` с синтетическим approval,
содержащим `output_cap_confirmed=False`: CLI возвращает2, транспорт не создаётся,
intent отсутствует. Реальные каталоги профиля и approval отсутствуют
(проверялись только метаданные). Никаких реальных HTTP-запросов не выполнялось.

В checkpoint входят только профиль, его mock-тесты, счётчик Docker suite
и этот отчёт. Production-defaults, Coding, старые журналы и эталоны не изменены.

Повторная проверка перед checkpoint: **117/117** mock-тестов, включая29 тестов
профиля;0 failures/errors/skips,92.795s вместе с discovery.
Сетевые подключения запрещены audit guard;0 попыток сети,0 реальных HTTP.
Discovery843 не означает новый полный прогон WSL/Docker.
Локальное свидетельство: `/tmp/neuraldeep-p33-checkpoint-1ypxm146/regress-final.json`.
