# codegateway2api — мост к пулу ключей CodeGateway

Локальный OpenAI/Anthropic-совместимый шлюз к `https://api.codegateway.dev/v1`
с ротацией ключей из авторегера. Порт **12067**, слушает `127.0.0.1`.

Банк ключей — **живой файл реггера**:
`autoreg/codegateway-register/data/accounts.jsonl` (поле `api_key`), читается
hot-reload по mtime: новый аккаунт, дописанный батчем, подхватывается без рестарта.

## Эндпоинты

| Метод | Путь | Что |
|---|---|---|
| POST | `/v1/chat/completions` | OpenAI chat (stream и non-stream, tools/functions) |
| POST | `/v1/messages` | Anthropic Messages (tool_use, Claude Code) |
| POST | `/v1/messages/count_tokens` | подсчёт токенов Anthropic |
| POST | `/v1/responses` | OpenAI Responses API (только OpenAI-модели) |
| POST | `/v1/embeddings` | embeddings |
| GET | `/v1/models` | каталог апстрима (100 моделей, кэш 10 мин) |
| GET | `/v1/balance` | баланс по ключам банка (без апстрима) |
| GET | `/health` | 200 всегда |
| GET | `/admin/status` | пул, маскированные ключи, статусы |
| POST | `/admin/reload` | перечитать банк (`{"reset_dead": true}` — снять паузы) |

## Пул

- стратегия `least_used` (или `round_robin` в `config.json → bridge.strategy`);
- `401` → ключ dead; `402`/квота → пауза (exhausted); `429` → кулдаун, после нескольких — длинная пауза;
- `5xx`/сеть → ретрай, после `fails_to_dead` → dead;
- одна попытка на ключ, не больше `max_attempts=3` на запрос;
- ключи в логи не пишутся — только маска; состояние в `data/state.json` (sha256-хэши);
- леджер запросов — `data/usage.jsonl`.

## Запуск

```bash
bash start.sh            # старт (log: bridge.log)
bash start.sh --status   # проверка
bash start.sh --stop
```

## Проверено живьём (08.11.2026)

```bash
curl -s http://127.0.0.1:12067/v1/models | jq '.data | length'          # 100
curl -s http://127.0.0.1:12067/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"claude-haiku-4-5","messages":[{"role":"user","content":"hi"}],"max_tokens":20}'
curl -s http://127.0.0.1:12067/v1/messages -H 'Content-Type: application/json' \
  -H 'anthropic-version: 2023-06-01' \
  -d '{"model":"claude-haiku-4-5","max_tokens":20,"messages":[{"role":"user","content":"hi"}]}'
```

Результаты:
- `/v1/models` → 100 моделей;
- chat `claude-haiku-4-5` → «Hey there!»; `gpt-4.1-mini` → «Hello there!»;
- **tools**: OpenAI → `tool_calls` + `finish_reason: tool_calls`; Anthropic → `tool_use` + `stop_reason: tool_use`;
- **stream** → SSE-чанки (role → content → ...);
- **Responses API** `gpt-4.1-mini` → 200; для anthropic-модели апстрим отдаёт
  корректную ошибку «Use POST /v1/messages» (мост её пробрасывает).

## Подключение инструментов

Claude Code / Anthropic-клиенты:
```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:12067
export ANTHROPIC_API_KEY=sk-cg-...        # любой ключ банка или пусто (client_keys=[])
```
OpenAI-клиенты:
```bash
export OPENAI_BASE_URL=http://127.0.0.1:12067/v1
export OPENAI_API_KEY=sk-cg-...
```

`client_keys` пуст → мост не проверяет клиентский ключ (слушает только loopback).
