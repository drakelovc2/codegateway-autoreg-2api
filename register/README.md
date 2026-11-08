# codegateway-register — авторег CodeGateway ($2 на аккаунт за привязку карты)

Сервис: `dashboard.codegateway.dev` (API — `https://api.codegateway.dev/v1`,
OpenAI-совместимый). Панель реггера — **:9945**.

## Что делает

Полный флоу, проверенный живьём:

1. **Turnstile** на `/en/auth/register` — camoufox ($0, ~45 с). Токен в
   `POST /api/auth/register {email,password,inviteCode,turnstileToken}`.
   Инвайт необязателен (`inviteRequired:false`) — можно не передавать.
2. **Email-верификация обязательна**: письмо «Verify your CodeGateway email» →
   токен из ссылки → `POST /api/auth/verify-email {token}`.
3. **Карта → бонус $2**: `POST /api/auto-topup/setup-intent` → SetupIntent +
   `pk_live_…` → карта вбивается **в браузере на домене мерчанта** (Card Element;
   Stripe запрещает pk-токенизацию вне своих поверхностей) → `pm_…` →
   серверный `POST /v1/setup_intents/<id>/confirm` → `succeeded` → бонус
   зачисляется вебхуком (`card-bonus-status.claimed = true`, `cardBonusCents=200`).
4. **API-ключ**: `GET /api/v1/workspaces/` → `POST /api/v1/workspaces/<id>/api-keys {name}`
   → `sk-cg-…`.
5. **Проверка**: `GET https://api.codegateway.dev/v1/models` + chat по `claude-haiku-4-5`.

## Подводные камни (найдены живьём)

| Что | Деталь |
|---|---|
| Домены почты | часть catch-all доменов сервис режет (`domain_blacklisted`) — проверь свой; Gmail-плюс-алиасы **нормализуются** в базовый адрес (лимит «too many attempts for this email») |
| Рейт-лимит по IP | короткое окно ~20 c (`retryAfter:21`), длинное ~1 ч и больше. На длинном — менять прокси, не ждать |
| Сверка IP капчи | сервис может сверять IP минта Turnstile с IP сабмита → «минт дома, рега через прокси» даёт `captcha_failed`. Решение: минт через тот же прокси (`turnstiletask` в Peak) или браузер с того же IP |
| Behavioral-блок | при «грязных» IP сервис отдаёт `403 behavioral_blocked` — лечится только чистым residential |
| Stripe tokenization | `createPaymentMethod` с сырыми данными и `confirm` с `payment_method_data` → `unsupported_publishable_key_tokenization_surface`. Только Element на домене мерчанта |
| `confirmCardSetup` в браузере | проходит проверку поверхности, но **зависает**: Radar гоняет hCaptcha, в headless не решается. Поэтому confirm — серверный, с `payment_method=pm_…` |
| Бины | сгенерированные PAN проходят только у «авто-аппрув» диапазонов; обычные бины дают `card_declined/generic_decline`, неизвестные Stripe — `incorrect_number` (валидируется 8-значный префикс) |
| Прокси | некоторые провайдеры режут `CONNECT api.stripe.com`. HTTP к CodeGateway через прокси можно, карточный шаг — напрямую (`card_step_direct: true`) |
| Auto Top-Up | после привязки карты остаётся выключенным (`enabled:false`) — карта хранится, но автоматических списаний нет |

## Модели и цены — заполни САМ

<!-- models:start -->
| Модель | Тариф | Цена | Доступна |
|---|---|---|---|
| claude-sonnet-4-6 | промо | $2 бонус/акк | да |
| claude-haiku-4-5 | промо | $2 бонус/акк | да |
| gpt-5.x (см. /v1/models) | промо | $2 бонус/акк | да |
| gemini-* | промо | $2 бонус/акк | да |
<!-- models:end -->

## Запуск

```bash
cp config.example.json config.json   # заполни mail_*, proxies, card_*
./start.sh                           # панель :9945
python3 tools/batch.py --count 5 --pause 90 --rotator
python3 tools/batch_parallel.py --count 10 --workers 2 --pool dc
python3 tools/run_to_target.py --target 100 --workers 2 --only-isp
```

Один аккаунт из CLI:

```python
import sys; sys.path.insert(0, '.')
from core.register import register_one
print(register_one(on_step=print))
```

## Файлы

```
core/register.py    — HTTP-флоу (регистрация → verify → карта → ключ)
core/browser_reg.py — браузерная регистрация (camoufox, Turnstile решает сам)
core/cardbind.py    — карточный шаг: Card Element в Chromium → pm_… → серверный confirm
core/cardkit.py     — генератор PAN по BIN (Luhn) + чекер chkr.cc + Stripe API
core/turnstile.py   — минт: camoufox ($0) → Peak (api.peak.fo) → capsolver
core/cloudflare.py  — «Just a moment»/Turnstile на живой странице
config.example.json — все ключи с плейсхолдерами
tools/batch.py, tools/batch_parallel.py, tools/run_to_target.py — батчи
```

## Статус (проверено живьём)

- ✅ e2e: регистрация → verify → карта → `succeeded` → ключ `sk-cg-…` → чат 200
- ✅ 10+ аккаунтов с бонусом $2 в одном прогоне
- ⚠️ бонус зачисляется вебхуком с задержкой — реггер опрашивает
  `card-bonus-status` до `bonus_wait_sec` (60 с)
- ⚠️ браузерная рега проходит не на каждой прокси-линии: Turnstile рендерится
  только на чистых residential (иначе `turnstile_not_rendered`)
