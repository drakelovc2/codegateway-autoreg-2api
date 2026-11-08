# -*- coding: utf-8 -*-
"""Cloudflare на живой странице playwright/patchright: «Just a moment» и Turnstile.

Чем отличается от `core/turnstile.py`: тот минтит токен ОТДЕЛЬНЫМ процессом
(camoufox) и отдаёт строку — годится, когда капча лежит на форме. Здесь —
работа с уже открытой страницей: пройти interstitial, дождаться `cf_clearance`,
вытащить токен виджета из DOM и, если совсем никак, добить capsolver'ом.

    from . import cloudflare
    if cloudflare.wait_challenge(page):          # прошли «Just a moment»
        ...
    tok = cloudflare.turnstile_token(page)       # значение cf-turnstile-response
    jar = cloudflare.cf_clearance(context)       # {'cf_clearance': '...'}

Почему это вообще работает на playwright: managed challenge решается тем, что
браузер честно исполняет JS и отдаёт отпечаток. Стоковый Chromium часто
палится (заголовки, WebGL, `navigator.webdriver`), поэтому по умолчанию
используем движки из `core/engines.py` — camoufox (Gecko) или patchright
(пропатченный Chromium). Чистый playwright оставлен как baseline.

Ключ capsolver — `capsolver_api_key` в config.json (он же у turnstile.py).
Без ключа модуль работает бесплатно: ждёт и жмёт чекбокс.
"""
import time

from . import config

# селекторы виджета Turnstile (в iframe и без)
_TURNSTILE_IFRAMES = (
    'iframe[src*="challenges.cloudflare.com"]',
    'iframe[title*="Cloudflare"]',
    'iframe[title*="human"]',
)
_TURNSTILE_INPUTS = (
    'input[name="cf-turnstile-response"]',
    'textarea[name="cf-turnstile-response"]',
)
_CHALLENGE_MARKS = (
    "just a moment",
    "checking your browser",
    "verifying you are human",
    "attention required",
    "cf-chl",
    "challenge-platform",
)


def _log(msg):
    print(f"  [cloudflare] {msg}", flush=True)


def is_challenge(page):
    """Страница сейчас показывает interstitial Cloudflare?"""
    try:
        title = (page.title() or "").lower()
    except Exception:
        title = ""
    if any(m in title for m in _CHALLENGE_MARKS):
        return True
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""
    if "cf-chl" in url or "/cdn-cgi/challenge-platform/" in url:
        return True
    try:
        # сам виджет Turnstile в DOM — тоже «челлендж», но уже решаемый
        if page.locator(_TURNSTILE_IFRAMES[0]).count():
            return True
    except Exception:
        pass
    return False


def wait_challenge(page, timeout=None, poll=1.0):
    """Ждать, пока interstitial уйдёт сам. True — ушёл.

    Managed challenge обычно проходит за 5–10 с, если браузер не палится.
    Много ждать бессмысленно: если через ~45 с не ушёл, дело в отпечатке
    или IP, и надо менять движок/прокси, а не ждать.
    """
    timeout = timeout or int(config.get("cloudflare_timeout_sec") or 45)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_challenge(page):
            _log(f"challenge пройден за {round(timeout - (deadline - time.time()), 1)} с")
            return True
        time.sleep(poll)
    _log(f"challenge не ушёл за {timeout} с")
    return False


def click_turnstile(page, timeout=8000):
    """Клик по чекбоксу Turnstile, если виджет висит и ждёт нажатия.

    Внутри iframe чекбокс — это `#cf-stage`, `label` или div с ролью
    checkbox; жмём самую внутреннюю подходящую цель. Ошибки не фатальны:
    виджет часто проходит и без клика.
    """
    for frame_sel in _TURNSTILE_IFRAMES:
        try:
            fl = page.frame_locator(frame_sel)
            for sel in ('input[type="checkbox"]', '#cf-stage', 'label', '[role="checkbox"]'):
                loc = fl.locator(sel)
                if not loc.count():
                    continue
                try:
                    loc.first.click(timeout=timeout, force=True)
                except Exception:
                    # координатный клик: элемент бывает перекрыт оверлеем
                    box = loc.first.bounding_box()
                    if box and box["width"] > 1:
                        page.mouse.click(box["x"] + box["width"] / 2,
                                         box["y"] + box["height"] / 2)
                    else:
                        continue
                _log(f"клик по Turnstile ({sel})")
                time.sleep(2)
                return True
        except Exception:
            continue
    return False


def turnstile_token(page):
    """Значение cf-turnstile-response из DOM или ''."""
    for sel in _TURNSTILE_INPUTS:
        try:
            loc = page.locator(sel)
            if loc.count():
                tok = loc.first.input_value(timeout=2000)
                if tok:
                    return tok
        except Exception:
            continue
    return ""


def cf_clearance(context):
    """Cookie cf_clearance из контекста (её и требует сервер)."""
    try:
        for c in context.cookies():
            if c.get("name") == "cf_clearance":
                return c.get("value") or ""
    except Exception:
        pass
    return ""
def solve_turnstile_capsolver(url, sitekey=None, timeout=90, poll=3):
    """Токен виджета через capsolver (~$0.003). None, если нет ключа/фейл.

    AntiTurnstileTaskProxyLess: токен не привязан к IP сабмита, поэтому
    заворачивать сам запрос в прокси аккаунта не нужно.
    """
    key = config.CAPSOLVER_API_KEY
    if not key:
        return None
    sitekey = sitekey or config.get("turnstile_sitekey") or ""
    if not sitekey:
        _log("capsolver: не задан sitekey")
        return None
    import requests
    try:
        r = requests.post("https://api.capsolver.com/createTask", json={
            "clientKey": key,
            "task": {"type": "AntiTurnstileTaskProxyLess",
                     "websiteURL": url, "websiteKey": sitekey},
        }, timeout=15)
        data = r.json()
    except Exception as exc:
        _log(f"capsolver createTask: {type(exc).__name__}: {exc}")
        return None
    if data.get("errorId") != 0:
        _log(f"capsolver: {data.get('errorDescription', data)}")
        return None
    task_id = data.get("taskId")
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(poll)
        try:
            data = requests.post("https://api.capsolver.com/getTaskResult", json={
                "clientKey": key, "taskId": task_id}, timeout=10).json()
        except Exception:
            continue
        if data.get("status") == "ready":
            _log("capsolver: токен получен")
            return ((data.get("solution") or {}).get("token")
                    or (data.get("solution") or {}).get("gRecaptchaResponse"))
        if data.get("errorId") != 0:
            _log(f"capsolver: {data.get('errorDescription', data)}")
            return None
    _log("capsolver: таймаут")
    return None


def pass_cloudflare(page, sitekey=None, inject=True):
    """Провести страницу через Cloudflare. True — чисто.

    Порядок: ждём само → жмём чекбокс → ждём снова → capsolver (если ключ).
    `inject=True` и найденный токен capsolver'а подставляем в скрытое поле
    формы, чтобы страница его приняла без DOM-виджета.
    """
    if not is_challenge(page):
        return True
    if wait_challenge(page):
        return True

    click_turnstile(page)
    if wait_challenge(page, timeout=30):
        return True

    tok = solve_turnstile_capsolver(page.url, sitekey)
    if not tok:
        return False
    if inject:
        try:
            for sel in _TURNSTILE_INPUTS:
                loc = page.locator(sel)
                if loc.count():
                    loc.first.evaluate("(el, v) => { el.value = v; "
                                       "el.dispatchEvent(new Event('input', {bubbles: true}));"
                                       "el.dispatchEvent(new Event('change', {bubbles: true})); }", tok)
                    break
            else:
                page.evaluate(
                    "(v) => { const i = document.createElement('input');"
                    " i.type = 'hidden'; i.name = 'cf-turnstile-response'; i.value = v;"
                    " document.body.appendChild(i); }", tok)
        except Exception as exc:
            _log(f"вставка токена: {type(exc).__name__}: {exc}")
    return wait_challenge(page, timeout=30)
