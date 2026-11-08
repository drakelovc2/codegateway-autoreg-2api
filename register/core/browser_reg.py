# -*- coding: utf-8 -*-
"""Браузерная регистрация CodeGateway — настоящий Firefox (camoufox).

Зачем: сервис считает «behavioral» по отпечатку HTTP-клиента и режет голые
`requests`-регистрации (`behavioral_blocked`). Живой браузер проходит: Turnstile
виджет решается сам, на сервер уходит обычный браузерный трафик.

Флоу:
  1. открыть /en/auth/register (домашний IP, без прокси);
  2. заполнить email / invite / password / confirm;
  3. дождаться, пока Turnstile положит токен в `input[name=cf-turnstile-response]`;
  4. поставить чекбокс terms и нажать «Create Account»;
  5. дождаться ухода со страницы регистрации (verify-pending / dashboard) либо ошибки;
  6. отдать cookies (access_token/refresh_token) — дальше verify/карта/ключ идут по HTTP.
"""
import time
from urllib.parse import unquote, urlsplit

from . import config

_EMAIL = "#register-email"
_INVITE = "#register-invite-code"
_PASSWORD = "#register-password"
_CONFIRM = "#register-confirm-password"
_TERMS = "#agree-terms"
_TURNSTILE = 'input[name="cf-turnstile-response"]'


def _proxy_dict(raw):
    """'http://user:pass@host:port' → {server, username, password} для camoufox."""
    if not raw:
        return None
    s = str(raw).strip()
    if "://" not in s:
        s = "http://" + s
    u = urlsplit(s)
    if not u.hostname:
        return {"server": s}
    d = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
    if u.username:
        d["username"] = unquote(u.username)
    if u.password:
        d["password"] = unquote(u.password)
    return d


def _log(on_step, msg):
    if on_step:
        on_step(msg)


def browser_signup(email, password, invite=None, headless=None, proxy=None,
                   timeout_sec=180, on_step=None):
    """Регистрация браузером. Возвращает {ok, cookies, url, error}."""
    from camoufox.sync_api import Camoufox

    if headless is None:
        headless = bool(config.get("signup_headless", True))
    signup_url = config.get("signup_url") or f"{config.BASE_URL}/en/auth/register"
    if invite is None:
        invite = config.get("invite_code") or ""

    result = {"ok": False, "cookies": {}, "url": None, "error": None}
    launched = False
    try:
        kwargs = {"headless": headless, "i_know_what_im_doing": True}
        pxy = _proxy_dict(proxy)
        if pxy:
            kwargs["proxy"] = pxy
            kwargs["geoip"] = True  # под прокси camoufox должен подогнать tz/locale
        with Camoufox(**kwargs) as browser:
            launched = True
            page = browser.new_page()
            api = {"status": None, "body": ""}

            def _on_resp(r):
                if "/api/auth/register" in r.url:
                    api["status"] = r.status
                    try:
                        api["body"] = r.text()[:300]
                    except Exception:
                        pass

            page.on("response", _on_resp)
            _log(on_step, f"browser: открываю {signup_url}")
            page.goto(signup_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)

            page.fill(_EMAIL, email)
            if invite:
                try:
                    page.fill(_INVITE, invite)
                except Exception:
                    pass
            page.fill(_PASSWORD, password)
            page.fill(_CONFIRM, password)

            # Turnstile: ждём, пока виджет положит токен
            _log(on_step, "browser: жду Turnstile…")
            token = ""
            ts_wait = float(config.get("turnstile_wait_sec") or 45)
            deadline = time.time() + min(timeout_sec, ts_wait)
            while time.time() < deadline:
                try:
                    token = page.eval_on_selector(_TURNSTILE, "el => el.value") or ""
                except Exception:
                    token = ""
                if token:
                    break
                page.wait_for_timeout(1500)
            if token:
                _log(on_step, f"browser: Turnstile решён ({len(token)} симв.)")
                page.wait_for_timeout(2000)  # дать React-колбэку отметить "solved"
            else:
                # без токена форма не сабмитится вообще — сразу на другую линию
                result["error"] = "turnstile_not_rendered"
                _log(on_step, "browser: Turnstile не отрисовался — линия не годится")
                return result

            try:
                page.check(_TERMS, timeout=5000)
            except Exception:
                pass
            _log(on_step, "browser: Submit…")
            clicked = False
            for attempt in range(2):
                try:
                    page.click('button[type="submit"]', timeout=8000, force=(attempt > 0))
                    clicked = True
                    break
                except Exception:
                    page.wait_for_timeout(1000)
            if not clicked:
                try:
                    page.evaluate(
                        "() => { const b = document.querySelector('button[type=\"submit\"]');"
                        " if (b) b.click(); }")
                    clicked = True
                except Exception:
                    pass
            # ждём запрос; если React-хендлер не сработал — жмём ещё раз
            for _ in range(3):
                page.wait_for_timeout(4000)
                if api.get("status") is not None:
                    break
                try:
                    page.evaluate(
                        "() => { const f = document.querySelector('form');"
                        " const b = document.querySelector('button[type=\"submit\"]');"
                        " if (b) b.click(); else if (f) f.requestSubmit(); }")
                except Exception:
                    pass

            # ждём уход со страницы регистрации или текст ошибки
            deadline = time.time() + min(timeout_sec, 120)
            err = ""
            while time.time() < deadline:
                url = page.url or ""
                if "/auth/register" not in url:
                    break
                txt = ""
                try:
                    txt = page.inner_text("body") or ""
                except Exception:
                    pass
                low = txt.lower()
                for marker in ("already registered", "invalid invite", "captcha",
                               "too many", "suspicious", "failed", "error"):
                    if marker in low:
                        # вытащим строку с ошибкой
                        for line in txt.splitlines():
                            if marker in line.lower() and len(line.strip()) > 3:
                                err = line.strip()[:160]
                                break
                page.wait_for_timeout(1500)

            result["url"] = page.url
            try:
                result["cookies"] = {c["name"]: c["value"] for c in page.context.cookies()}
            except Exception:
                pass
            ok = (api["status"] == 201
                  or ("/auth/register" not in (page.url or "")
                      and bool(result["cookies"].get("access_token"))))
            result["ok"] = ok
            result["api_status"] = api["status"]
            result["api_body"] = api["body"]
            if api["status"] == 429:
                import json as _json
                try:
                    result["retry_after"] = int(_json.loads(api["body"]).get("retryAfter") or 0)
                except Exception:
                    result["retry_after"] = 0
                result["rate_limited"] = True
            if api["status"] == 403 and "suspicious" in (api["body"] or "").lower():
                result["behavioral"] = True
            if not ok:
                result["error"] = err or f"остались на {page.url} (api={api['status']}, cookies: {list(result['cookies'])})"
            _log(on_step, f"browser: {'OK' if ok else 'FAIL'} {page.url}")
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        _log(on_step, f"browser: исключение {result['error'][:180]}")
    return result
