# -*- coding: utf-8 -*-
"""codegateway-register — авторег CodeGateway (dashboard.codegateway.dev).

Флоу (всё проверено живьём 08.11.2026):
  1. Turnstile на /en/auth/register — camoufox ($0), токен переиспользуется 5 мин.
  2. POST /api/auth/register {email,password,inviteCode,turnstileToken} → 201 + cookies.
     Часть почтовых доменов сервис режет (domain_blacklisted) — проверь свой.
     Рейт-лимит по IP: короткое окно ~20 c, длинное ~1 час.
  3. Письмо «Verify your CodeGateway email» в твой catch-all → токен из ссылки
     → POST /api/auth/verify-email {token}.
  4. Карта: POST /api/auto-topup/setup-intent → client_secret; браузер (Card Element
     на домене мерчанта) → pm_…; серверный confirm → succeeded. Бонус $2
     зачисляется автоматически (card-bonus-status.claimed = true).
  5. GET /api/v1/workspaces/ → workspace.id → POST .../api-keys {name} → sk-cg-…
  6. Проверка ключа: GET https://api.codegateway.dev/v1/models (+ chat).

Бины: 461994 (Visa, NP) проходит; 52792622911 (Mastercard, JP) — generic_decline.
"""
import json
import re
import secrets
import string
import time

import requests

from . import cardkit, config
from .cardbind import bind_card
from .mail import MailClient
from .turnstile import solve as solve_turnstile

API_BASE = (config.get("api_base") or "https://api.codegateway.dev/v1").rstrip("/")

REGISTER_PATH = "/api/auth/register"
VERIFY_PATH = "/api/auth/verify-email"
ME_PATH = "/api/auth/me"
WORKSPACES_PATH = "/api/v1/workspaces/"
BONUS_PATH = "/api/user/card-bonus-status"
SETUP_INTENT_PATH = "/api/auto-topup/setup-intent"


class ServiceError(Exception):
    pass


def _gen_password(length=18):
    chars = string.ascii_letters + string.digits + "!@#$%^&*"
    pw = [secrets.choice(string.ascii_uppercase), secrets.choice(string.ascii_lowercase),
          secrets.choice(string.digits), secrets.choice("!@#$%^&*")]
    pw += [secrets.choice(chars) for _ in range(length - 4)]
    secrets.SystemRandom().shuffle(pw)
    return "".join(pw)


def _parse_verify_token(raw):
    m = re.search(r"/auth/verify-email\?token=([A-Za-z0-9_\-\.]+)", raw or "")
    return m.group(1) if m else None


class ServiceClient:
    def __init__(self, proxy=None):
        self.base = config.BASE_URL.rstrip("/")
        self.proxy = proxy
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": config.UA,
            "Origin": self.base,
            "Content-Type": "application/json",
        })
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}

    # ── низкий уровень ──────────────────────────────────────────────────
    def _json(self, method, path, referer=None, **kw):
        headers = {}
        if referer:
            headers["Referer"] = referer
        timeout = kw.pop("timeout", 45)
        try:
            r = self.session.request(method, self.base + path, headers=headers,
                                     timeout=timeout, **kw)
        except requests.RequestException as exc:
            return 0, {"code": "network", "error": str(exc)[:200]}
        try:
            data = r.json()
        except ValueError:
            data = {"raw": r.text[:300]}
        return r.status_code, data

    def cookies(self):
        return {c.name: c.value for c in self.session.cookies}

    # ── шаги ────────────────────────────────────────────────────────────
    def register(self, email, password, turnstile_token=None, invite=None):
        invite = config.get("invite_code") if invite is None else invite
        body = {"email": email, "password": password,
                "inviteCode": invite or "",
                "turnstileToken": turnstile_token or ""}
        status, data = self._json("POST", REGISTER_PATH,
                                  referer=config.get("signup_url"), json=body)
        if status == 201 and data.get("success"):
            return {"ok": True, "status": status, "data": data}
        code = data.get("code") or ""
        out = {"ok": False, "status": status, "code": code,
               "error": data.get("error") or data.get("message") or str(data)[:200]}
        if status == 429:
            out["retryable"] = True
            out["retry_after"] = int(data.get("retryAfter") or 25)
        elif code in ("captcha_required", "captcha_failed"):
            out["retryable"] = True
            out["need_captcha"] = True
        elif code in ("invite_code_rate_limited", "invite_code_required"):
            # инвайт исчерпан — пробуем без него (inviteRequired:false на сервисе)
            out["retryable"] = True
            out["drop_invite"] = True
        return out

    def mint_turnstile(self):
        """Токен Turnstile через camoufox. None — не получилось.

        turnstile_mint_direct=true → минт с домашнего IP, даже если регистрация
        через прокси (DC-прокси Cloudflare не отрисовывает виджет).
        """
        url = config.get("signup_url") or f"{self.base}/en/auth/register"
        sitekey = config.get("turnstile_sitekey") or ""
        mint_proxy = None if config.get("turnstile_mint_direct") else self.proxy
        return solve_turnstile(url, proxy=mint_proxy, sitekey=sitekey)

    def verify_email(self, token):
        status, data = self._json("POST", VERIFY_PATH,
                                  referer=f"{self.base}/en/auth/verify-email",
                                  json={"token": token})
        ok = status == 200 and bool((data.get("data") or {}).get("verified"))
        return {"ok": ok, "status": status, "data": data}

    def me(self):
        return self._json("GET", ME_PATH)

    def workspaces(self):
        status, data = self._json("GET", WORKSPACES_PATH)
        items = []
        if isinstance(data, dict):
            items = (data.get("data") or {}).get("workspaces") or []
        return status, items

    def workspace_id(self):
        status, items = self.workspaces()
        if not items:
            return None, f"workspaces: HTTP {status}"
        ws = items[0].get("workspace") or {}
        return ws.get("id"), None

    def card_bonus_status(self):
        return self._json("GET", BONUS_PATH)

    def setup_intent(self):
        status, data = self._json("POST", SETUP_INTENT_PATH,
                                  referer=f"{self.base}/en/account/settings/auto-topup")
        if status == 200 and data.get("client_secret"):
            return data
        return {"error": f"setup-intent HTTP {status}: {str(data)[:200]}"}

    def create_api_key(self, workspace_id, name=None):
        name = name or "codegateway-reg"
        path = f"/api/v1/workspaces/{workspace_id}/api-keys"
        status, data = self._json("POST", path, json={"name": name})
        d = (data.get("data") or {}) if isinstance(data, dict) else {}
        key = d.get("key") or d.get("api_key")
        if status in (200, 201) and key:
            return {"ok": True, "key": key, "id": d.get("id"),
                    "prefix": d.get("key_prefix")}
        return {"ok": False, "status": status,
                "error": (data.get("message") if isinstance(data, dict) else str(data)[:200])}

    def connection_test(self, api_key):
        return self._json("POST", "/api/connection-test", json={"apiKey": api_key})


# ── по ключу, без сессии ─────────────────────────────────────────────────

def list_models(api_key, proxy=None, timeout=30):
    try:
        r = requests.get(f"{API_BASE}/models",
                         headers={"Authorization": f"Bearer {api_key}",
                                  "User-Agent": config.UA},
                         proxies={"http": proxy, "https": proxy} if proxy else None,
                         timeout=timeout)
        if r.ok:
            return [m.get("id", "") for m in (r.json().get("data") or [])]
    except requests.RequestException:
        pass
    return []


def test_key(api_key, model=None, proxy=None):
    """Один chat-запрос по ключу → (ok, detail)."""
    models = list_models(api_key, proxy=proxy)
    if not models:
        return False, "ключ не отдал /v1/models"
    model = model or next((m for m in models if "haiku" in m.lower()), models[0])
    try:
        r = requests.post(f"{API_BASE}/chat/completions",
                          headers={"Authorization": f"Bearer {api_key}",
                                   "User-Agent": config.UA},
                          json={"model": model,
                                "messages": [{"role": "user", "content": "Say hi in 2 words"}],
                                "max_tokens": 20},
                          proxies={"http": proxy, "https": proxy} if proxy else None,
                          timeout=60)
        if r.ok:
            content = r.json()["choices"][0]["message"]["content"]
            return True, f"{model}: {content[:60]}"
        return False, f"HTTP {r.status_code}: {r.text[:150]}"
    except (requests.RequestException, KeyError, IndexError) as exc:
        return False, str(exc)[:150]


# ── полный флоу ──────────────────────────────────────────────────────────

def register_one(email=None, password=None, proxy=None, mail=None, on_step=None,
                 card=None, turnstile_token=None, token_provider=None):
    """Регистрация одного аккаунта CodeGateway.

    on_step(str) — прогресс. card — готовая Card или строка PAN|MM|YYYY|CVV.
    turnstile_token — готовый токен (не минтить заново; токен переиспользуется
    ~5 мин, проверено: один токен → несколько регистраций с разных IP).
    token_provider() — функция, возвращающая свежий токен (кэш/обновление).
    Возвращает dict: email, password, api_key, bonus, card… или error.
    """
    def step(msg):
        if on_step:
            on_step(msg)
        else:
            print(f"  {msg}")

    mail = mail or MailClient()
    email = email or mail.new_address()
    email_send = mail.send_address(email)
    password = password or _gen_password()
    started = time.time()
    client = ServiceClient(proxy=proxy)
    acc = {"email": email, "password": password}

    # 1. signup
    step(f"1/6: signup {email_send}")
    result = None
    invite = config.get("invite_code")
    if str(config.get("signup_mode") or "http").lower() == "browser":
        # живой браузер: считает «behavioral» по отпечатку HTTP-клиента → браузер проходит
        from .browser_reg import browser_signup
        br = browser_signup(email_send, password, invite=invite,
                            headless=config.get("signup_headless", True),
                            proxy=proxy if config.get("signup_browser_via_proxy") else None,
                            on_step=step)
        if not br.get("ok"):
            err = {**acc, "error": f"browser signup: {br.get('error')}"}
            for k in ("rate_limited", "retry_after", "behavioral"):
                if br.get(k):
                    err[k] = br[k]
            return err
        for k, v in (br.get("cookies") or {}).items():
            client.session.cookies.set(k, v, domain="dashboard.codegateway.dev")
        result = {"ok": True}
    else:
        token = turnstile_token
        for attempt in range(1, 4):
            if token_provider is not None:
                token = token_provider()
            elif not token:
                token = client.mint_turnstile()
            if not token:
                step("1/6: Turnstile не решён (camoufox), пробую без токена")
            res = client.register(email_send, password, token, invite=invite)
            if res.get("ok"):
                result = res
                break
            step(f"1/6: попытка {attempt}: {res.get('code') or res.get('status')}: {res.get('error')}")
            if res.get("drop_invite"):
                step("1/6: инвайт исчерпан — регистрирую без него")
                invite = ""
                continue
            if res.get("status") == 429:
                wait = int(res.get("retry_after") or 30)
                if wait > 120:
                    # длинная маска IP (до часа) — ждать бессмысленно, нужен прокси
                    return {**acc, "error": f"signup: IP в рейт-лимите ещё {wait} c",
                            "rate_limited": True, "retry_after": wait}
                step(f"1/6: рейт-лимит, жду {wait} c")
                time.sleep(wait)
                continue
            if res.get("retryable"):
                if res.get("need_captcha"):
                    token = None  # токен протух/отклонён — следующий круг минтит свежий
                time.sleep(5)
                continue
            return {**acc, "error": f"signup: {res.get('error')}"}
    if not result:
        return {**acc, "error": "signup: ретраи исчерпаны"}

    # 2. verify-письмо
    step("2/6: жду письмо с verify-ссылкой...")
    link, _subj = mail.wait_link(
        email, sender_hint=config.get("mail_sender_hint") or "codegateway",
        since_ts=started)
    token = _parse_verify_token(link or "")
    if not token:
        for m in mail.list_mails(email, limit=5):
            token = _parse_verify_token(m.get("raw", ""))
            if token:
                break
    if not token:
        return {**acc, "error": "verify-ссылка не пришла/не разобрана"}
    step("3/6: подтверждаю e-mail")
    ver = client.verify_email(token)
    if not ver.get("ok"):
        return {**acc, "error": "verify-email: " + json.dumps(ver.get("data"), ensure_ascii=False)[:150]}

    status, me = client.me()
    if status != 200:
        return {**acc, "error": f"me: HTTP {status} {str(me)[:120]}"}
    acc["user_id"] = (me.get("data") or {}).get("userId")

    # 3. карта + бонус
    step("4/6: привязываю карту (SetupIntent)")
    bound = _bind_card_step(client, card, step)
    if bound.get("error"):
        acc["card_error"] = str(bound["error"])[:300]
        step(f"4/6: карта не прошла: {acc['card_error'][:150]}")
    else:
        acc["card"] = {k: bound.get(k) for k in ("brand", "last4", "country", "status")}
        acc["card_line"] = bound.get("card")
        step(f"4/6: карта {bound.get('brand')} *{bound.get('last4')} → {bound.get('status')}")

    _st, bonus = client.card_bonus_status()
    # бонус приходит вебхуком Stripe — даём ему пару секунд, опрашиваем
    claimed = bool((bonus or {}).get("claimed"))
    wait_sec = int(config.get("bonus_wait_sec") or 60)
    deadline = time.time() + wait_sec
    while not claimed and time.time() < deadline:
        time.sleep(5)
        _st, bonus = client.card_bonus_status()
        claimed = bool((bonus or {}).get("claimed"))
    acc["bonus"] = bonus if isinstance(bonus, dict) else {}
    acc["bonus_claimed"] = claimed

    # 4. ключ
    step("5/6: создаю API-ключ...")
    ws_id, ws_err = client.workspace_id()
    if not ws_id:
        return {**acc, "error": f"workspace: {ws_err}"}
    key_res = client.create_api_key(ws_id)
    if not key_res.get("ok"):
        return {**acc, "error": f"api-key: {key_res.get('error')}"}
    acc["api_key"] = key_res["key"]
    acc["workspace_id"] = ws_id

    # 5. проверка ключа
    step("6/6: проверяю ключ...")
    ok, detail = test_key(acc["api_key"])
    acc["key_ok"] = ok
    acc["key_detail"] = detail
    acc["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if not ok:
        acc["error"] = f"ключ не работает: {detail}"
    return acc


def _bind_card_step(client, card, step):
    """SetupIntent + подбор карты (по бинам из конфига) + серверный confirm."""
    si = client.setup_intent()
    if si.get("error"):
        return {"error": si["error"]}
    bins = config.get("card_bins") or []
    attempts = max(int(config.get("card_max_attempts") or 6), 1)
    last = None
    for i in range(1, attempts + 1):
        try:
            c = cardkit.pick_card(bins=bins, mode=None, fixed=card)
        except Exception as exc:
            return {"error": f"карта: {exc}"}
        res = bind_card(si["client_secret"], c, client.cookies(),
                        proxy=None if config.get("card_step_direct", True) else client.proxy)
        last = {**res, "card": c.line()}
        if res.get("status") in ("succeeded", "processing"):
            return last
        step(f"4/6: карта #{i} отклонена ({(res.get('error') or {}).get('decline_code') or res.get('error')}), пробую следующую")
    err = (last or {}).get("error") or "все карты отклонены"
    return {"error": err, "last": last}
