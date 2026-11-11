# codegateway-autoreg-2api

Авторег аккаунтов **CodeGateway** (`dashboard.codegateway.dev`) + OpenAI/Anthropic-совместимый
мост к пулу их ключей. $2 приветственного кредита на аккаунт за привязку карты;
API — `https://api.codegateway.dev/v1`.

```
codegateway-autoreg-2api/
├── register/   ← регистратор + панель (:9945)
│   ├── core/   ← register_one / test_key / list_models, карты, капча, прокси, почта
│   ├── panel/  ← веб-панель
│   └── tools/  ← батчи (последовательный, параллельный, «до цели»)
└── bridge/     ← мост (:12067), пул ключей с hot-reload
```

## Цепочка

1. `register/` создаёт аккаунты: Turnstile (camoufox, $0) → email-verify → привязка карты
   (Stripe SetupIntent) → бонус $2 → API-ключ `sk-cg-…`.
2. Ключи пишутся в `register/data/accounts.jsonl`.
3. `bridge/` читает этот же файл (hot-reload по mtime) и раздаёт
   `/v1/chat/completions`, `/v1/messages` (Anthropic), `/v1/responses`, `/v1/models`
   с ротацией и обработкой 401/402/429.

## Быстрый старт

```bash
# 1) реггер
cd register
cp config.example.json config.json     # заполни mail_*, proxies, stripe_pk, card_*
pip install -r requirements.txt
./start.sh                             # панель http://127.0.0.1:9945

# 2) мост
cd ../bridge
cp config.example.json config.json
pip install -r requirements.txt
./start.sh                             # http://127.0.0.1:12067
```

## Подключение инструментов к мосту

```bash
# Claude Code / Anthropic-клиенты
export ANTHROPIC_BASE_URL=http://127.0.0.1:12067
export ANTHROPIC_API_KEY=sk-cg-...

# OpenAI-клиенты
export OPENAI_BASE_URL=http://127.0.0.1:12067/v1
export OPENAI_API_KEY=sk-cg-...
```

## Что важно знать (проверено живьём)

- **Turnstile**: camoufox минтит бесплатно (~45 с); Peak (api.peak.fo) — платный
  fallback и умеет минтить **с того же прокси**, что и сабмит (если сервис сверяет IP).
- **Stripe**: мерчант запрещает pk-токенизацию вне своих поверхностей — карту вводит
  браузер на домене мерчанта (Card Element), а `confirm` делается **серверно** с
  `payment_method=pm_…` (браузерный `confirmCardSetup` зависает на hCaptcha Radar).
- **Рейт-лимит на IP**: ~20 с короткое окно и 20 мин–часы длинное. Каждый IP даёт 1–2
  аккаунта, дальше нужен другой прокси.
- **Почта**: часть доменов в блок-листе; Gmail-плюс-алиасы нормализуются к базовому
  адресу и упираются в лимит на email.
- **Бины**: сгенерированные PAN проходят только у «авто-аппрув» диапазонов; Stripe
  валидирует 8-значный BIN (`incorrect_number` для неизвестных).

## Peak (api.peak.fo) — 1 000 бесплатных решений

Peak — платный солвер Turnstile/Cloudflare-5s, но при регистрации дают **1 000 решений
без карты**. Полезен как fallback к бесплатному camoufox и, главное, умеет минтить токен
**через твой прокси** (`turnstiletask`) — с того же IP, откуда пойдёт сабмит.

1. Открой <https://peak.fo/login> и нажми **«Create an account»** (кнопка под формой входа;
   отдельного `/register` нет — он редиректит на `/login`).
2. Укажи **email + пароль**. Gmail и свой домен подходят; одноразовые домены могут не
   пройти — если письма нет, проверь спам или смени домен.
3. Если просят подтвердить почту — перейди по ссылке из письма.
4. В дашборде <https://peak.fo/dashboard> → **API Keys** → создай ключ (`pk_…`).
   На аккаунт начисляются **1 000 бесплатных решений** (без карты).
5. Проверь баланс:
   ```bash
   curl -s -H "X-API-Key: pk_..." https://api.peak.fo/balance
   # {"success":true,"data":{"type":"pay_per_solve","balance":0.8607}}
   ```
6. Подключи к реггеру: `"peak_api_key": "pk_..."`, `"turnstile_solver": "auto"`.
   Режим `auto`: camoufox ($0) → Peak → capsolver.

### Запрос к Peak

```json
{"task_type": "turnstiletask", "sitekey": "0x4AAAAAAA...",
 "url": "https://example.com/", "proxy": "http://user:pass@1.2.3.4:8080"}
```

- `url` **обязан заканчиваться слэшем** (`.../register/`);
- поле именно `sitekey` (не `siteKey`);
- `turnstiletaskproxyless` — без прокси (sitekey+url), `cloudflare5stask` — url+proxy.

### Ограничения

- 1 000 решений — разово на аккаунт, дальше pay-per-solve.
- `challenge_flagged` = **IP грязный**, Cloudflare пометил его до решения; солвер не поможет,
  нужен чистый residential.
- Тестовые sitekey Cloudflare заблокированы (`task_blocked`).

## Ограничения

- Только для собственных/разрешённых аккаунтов и тестов. Не абузь сервисы.
- Прокси, почта, карты и `stripe_pk` — заполняешь сам; в репозитории их нет.
- Панель и мост слушают `127.0.0.1` — наружу не выставляй без авторизации.

## Лицензия

MIT + условие добросовестного использования — см. [LICENSE](LICENSE).

---

🔥 Telegram: [@antivibedrake](https://t.me/antivibedrake) · [@zakurulife](https://t.me/zakurulife)

Только для личного использования — см. [DISCLAIMER.md](DISCLAIMER.md).
