# P3.1 — Qwen Live Preflight

11 ноября 2026. `feature/harness-v1`, HEAD
`3ecb1e1b1d0b42b905ec591de00388ba9ac86ca9`.

**NEEDS_CONFIRMATION; LIVE пока заблокирован.** Это проверка готовности пилота,
не повтор аудита P2. Production, Coding, main и существующие журналы не менялись.
Создан только этот отчёт. Реальных HTTP/LLM-запросов: **0**.

## 1. Что фактически настроено

Источник: `llm_gateway.py:17–21,185–195,239–263`, `run_agent.py`, README,
раздел «Ограниченный LLM fallback»; установленный openJiuwen 0.1.20.post1.

| Точный ID из кода | Текущий допуск |
|---|---|
| qwen3.6-35b-a3b-noreason | Default; допустим после конфигурации и бюджетного preflight |
| qwen3.8-27b-noreason | Допустим при явном выборе модели |
| qwen3.6-35b-a3b | До HTTP запрещён без проверенного reasoning/output contract |
| qwen3.8-27b | То же |

Все четыре ID совпадают с локальным регламентом. Это **настройка кода**, не
проверка наличия моделей у провайдера. GET models и другие API не вызывались.
CLI выбирает модель через `--llm-model`; переменная LLM_MODEL не предусмотрена.

В процессе этой сессии BUDGET, LLM_BASE_URL и LLM_API_KEY отсутствуют.
`.env` не читался и автоматически CLI не загружается. Документированный механизм
передачи — environment (`README.md`, «Ограниченный LLM fallback»).
Gateway использует Chat Completions POST с Bearer, messages, max_tokens и
stream=false; base URL должен включать нужный префикс версии, например /v1,
если он требуется официальным API. Путь /v1 самостоятельно не добавляется.
Не следует подставлять исторический NeuralDeep URL вместо неизвестного
официального Proxy: их тождество не подтверждено.

Настоящий openJiuwen исполняет workflow агента; HTTP выполняет существующий
httpx gateway с общим BudgetManager. Нативные SDK calls вне зарезервированного
маршрута запрещены callback guard. Прежняя совместимость с локальным SDK/mock
не доказывает совместимость реального Proxy.

## 2. Безопасная команда настройки для владельца — без HTTP

Запускать в интерактивном Bash из корня репозитория. Ключ вводится скрыто,
не является текстом shell-команды и не записывается в файл. Подоболочка не
оставляет эти переменные в родительской оболочке; xtrace отключён.
Команда только проверяет синтаксис, не доказывает права на ключ или endpoint.
BUDGET=200 здесь — проверка предлагаемой конфигурации, **не разрешение расхода**.

```bash
cd ~/sidim-bez-dim-token-harness
(
  set +x
  IFS= read -r -p 'Official Proxy base URL: ' LLM_BASE_URL
  IFS= read -r -s -p 'API key (hidden): ' LLM_API_KEY
  printf '\n'
  export LLM_BASE_URL LLM_API_KEY
  export BUDGET=200
  .venv/bin/python -B - <<'PY'
import os
import httpx
try:
    url = httpx.URL(os.environ.get('LLM_BASE_URL', ''))
    url_ok = (url.scheme in ('http', 'https') and bool(url.host)
              and not any((url.username, url.password, url.query, url.fragment)))
except (httpx.InvalidURL, TypeError):
    url_ok = False
key = os.environ.get('LLM_API_KEY', '')
key_ok = bool(key.strip()) and '\r' not in key and '\n' not in key
budget_ok = os.environ.get('BUDGET') == '200'
print('URL_SYNTAX_OK=', url_ok, 'KEY_PRESENT_VALID=', key_ok,
      'PROPOSED_BUDGET_OK=', budget_ok, 'HTTP_SENT=0')
raise SystemExit(0 if url_ok and key_ok and budget_ok else 2)
PY
)
```

Не использовать env/printenv, echo ключа, curl с раскрытым Authorization,
ключ в аргументах процесса или `.env` в Git. Не запускать после этого обычный
run_agent.py как ограниченный пилот: он не имеет durable межпроцессного
лимита «один запрос» для нового эксперимента.

## 3. Подготовленный минимальный Direct Answer

**Предложение для отдельного разрешения:** новая синтетическая проба
`p3_proxy_direct_v1`, не повтор A1 и не запуск A2.

```json
{
  "model": "qwen3.6-35b-a3b-noreason",
  "messages": [
    {"role": "system", "content": "Return only one JSON object with a string field value containing the arithmetic result."},
    {"role": "user", "content": "{\"instruction\": \"Compute (7 - 3)\"}"}
  ],
  "max_tokens": 32,
  "stream": false
}
```

POST идёт на одобренный endpoint из environment; ключ — только в Bearer header.
Ожидаемый **внутренний** JSON: `{"value":"4"}`. Временный offline-прототип
Direct Answer реально вызывает текущий gateway с MockTransport и затем отдельно
проверяет JSON, точные ключи/строковый тип и значение по независимому oracle.
Он не установлен в production SOLVERS и не является готовым LIVE runner.

| Ограничение предложения | Значение |
|---|---|
| Новые HTTP | **1 максимум**, без redirects, retries и следующей пробы |
| Модель | Только **qwen3.6-35b-a3b-noreason** |
| Timeout | **20 секунд** |
| Max output | **32 токена** |
| Новый локальный лимит | **200 выходных эквивалентов**, после согласования scope истории |
| Input admission | estimate ≤1500, полный резерв ≤182.0 |
| Этот точный prompt | estimate=1217, полный резерв **153.7** |

Оценка input — bytes+1024, не доказанный tokenizer bound. Жёсткую удалённую
стоимость нельзя гарантировать одним локальным cap: нужен подтверждённый
контракт Proxy на input accounting и output limit. Unknown usage, auth/quota,
model/reasoning mismatch, превышение, timeout или неправильный ответ — stop;
неопределённый резерв удерживается, новая отправка запрещена.

Для будущего сравнения сохранены разные контракты ответа: Direct Answer → JSON
value → независимый oracle; MathIR → schema/semantic verifier → exact executor;
SearchQueryIR → graph verifier/provenance → answer. В offline-проверке Direct
Answer и MathIR на одном `Compute (7 - 3)` дали 4; отдельный минимальный
in-memory Search Graph дал 1967 с проверенными фактами. Новых datasets нет.
Это проверка обвязки, не качество Qwen и не экономическая выгода: Local Exact
решает такую арифметику бесплатно. Сравнение платных маршрутов требует отдельного
лимита/разрешения и одинаковых условий внутри каждой категории; первый запрос
не запускает его автоматически. MathIR/Compiler LIVE остаётся выключен.

## 4. Бюджет, one-shot и фактические offline-проверки

Конкурсная стоимость: `prompt / 10 + accounted_output`.
Included reasoning повторно не прибавляется; separate прибавляется только при
явном проверенном AccountingPolicy. Неизвестный положительный reasoning при
обычном noreason-конфиге блокирует дальнейшие calls.

Пройдено **18 mock-сценариев gateway**: оба noreason ID допускаются с моками,
оба reasoning ID отклоняются до HTTP; malformed inner JSON, неверная схема,
неверное значение; timeout и HTTP 401/403/429/500; missing/bad usage и unknown
reasoning; included/separate accounting; выход за output cap. Ошибки формата
после подтверждённого usage всё равно стоят токенов. Повтор после uncertain
failure не достигает mock-транспорта. Стоимость нормального mock-ответа
input=100/output=6 — 16.0; обе reasoning-схемы для 20 accounted output — 30.0.
Overrun output=33 при cap32 даёт известные 43.0 и заморозку, а не скрытое обнуление.

Также прошли **31/31 существующих тестов** ManagerTests и A1ResumeTests за
6.199 s: точные суммы, сохранение резервов, конкурентный claim, crash/новый
процесс, пустой/разорванный intent, fsync failure, recovery без resend,
запрет новых output-путей для обхода лимита и отсутствие секретов в результатах.
Все использовали вымышленную историю и временные state, не реальные журналы.
Guard запрещал любые сетевые события, dataset и private-state reads.
Production не изменён, полный прогон после P4 не повторялся без причины.

У реальных старых state-каталогов проверены только metadata, не записи:
существующие каталоги имеют 0700 и принадлежат текущему UID. Содержимое,
занятость конкретного слота и текущий расход из них не читались и не выводились.
Отсутствующий каталог не доказывает разрешение повторить старую попытку.

Существующие примитивы `a1_resume.state_directory/write_once` дают O_EXCL,
O_NOFOLLOW, 0600, file+directory fsync до HTTP. Любой intent потребляет слот,
включая разорванный; recover читает результат без новой отправки. Но это
доказательство **примитивов и старого resume**, не готовности нового Proxy-профиля.
Обычный BudgetManager run-scoped и сам по себе не переживает процесс.

Старый полный A1 не пройден; A2 продолжает требовать успешный LIVE receipt и
совпадающие fingerprints. Старые math/search pilots закреплены за NeuralDeep,
прежними задачами и историей, а math_d15/search_d15 теперь решаются локально.
Их существующие команды не подходят для нового Direct Answer. Смена endpoint,
receipt hashes, новый клон или каталог вывода не дают разрешения повторить calls.
Публичные 155.9 из прежней серии 900 — история, не проверенный текущий баланс.

## 5. Что необходимо до первой LIVE-команды

1. Владелец получает подтверждённый **официальный** base URL, авторизацию,
   доступность выбранного model ID, Chat Completions/usage/max_tokens contract
   и проверяет принадлежность/допустимость ключа. Секреты вводятся локально.
2. Владелец отдельно разрешает **один** новый HTTP с указанными выше моделью,
   timeout и caps. Определяет, новая ли это квота, или продолжение прежней:
   в последнем случае всю историю/неопределённые attempts нужно сохранить.
   Нельзя молча заменить общий BUDGET=900 новым 200 и забыть расходы.
3. **Минимальное предлагаемое исправление, не реализованное в P3.1:** добавить
   отдельный synthetic Direct Answer профиль в существующий diagnostic runner,
   используя прежние gateway/manager/write_once, не новый gateway и не production.
   Новый фиксированный series ID, persistent приватный state вне Git, привязка
   endpoint/model/prompt/runtime/caps, один claim независимо от output/cwd,
   modes check/live/recover, LIVE только явно. Не менять старые A1/A2 gates.
4. Проверить новый профиль offline на restart/crash/race, timeout/unknown usage,
   безопасный capture и исчерпание бюджета. Intent должен сохраняться на
   persistent FS до возможного HTTP, а не только во временном результате.
5. Только затем опубликовать **точную существующую** LIVE-команду нового профиля
   и ещё раз остановиться перед отправкой до явного разрешения на запуск.

**Готовой безопасной LIVE-команды сейчас нет.** Предлагать несуществующий флаг
или заменять runner однострочным gateway.complete/curl было бы обходом one-shot.
Параметры и wire body первого запроса зафиксированы выше; команда настройки
environment уже пригодна, но сама HTTP не выполняет.

Локальные результаты и проверенный offline-прототип:
`/tmp/p3-1-preflight-uwllthby/checks.json`, `checks.py` и логи.
Это временные материалы без реальных payload/keys, не persistent допуск LIVE.
Анализ уже полученного результата без нового запроса:

```bash
.venv/bin/python -B - <<'PY'
import json
from pathlib import Path
r = json.loads(Path('/tmp/p3-1-preflight-uwllthby/checks.json').read_text())
print({k: r[k] for k in ('real_http_requests', 'mock_gateway_scenarios', 'existing_tests')})
PY
```

Git: прежний HEAD, main/ветка Ильи и все исходники сохранены; index пуст.
Помимо прежнего незакоммиченного P2 отчёта добавлен только данный документ.
Commit/push/merge, реальные API, чтение holdout и изменения старых state не выполнялись.

**Ответ на вопрос о старте после разрешения: пока нет.** Нужны также
подтверждённая конфигурация и принятый durable профиль. После закрытия этих
условий возможен один отдельно разрешённый запрос; это не запуск A2/Compiler LIVE.
