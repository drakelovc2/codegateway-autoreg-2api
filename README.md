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

## Ограничения

- Только для собственных/разрешённых аккаунтов и тестов. Не абузь сервисы.
- Прокси, почта, карты и `stripe_pk` — заполняешь сам; в репозитории их нет.
- Панель и мост слушают `127.0.0.1` — наружу не выставляй без авторизации.

## Лицензия

MIT + условие добросовестного использования — см. [LICENSE](LICENSE).
