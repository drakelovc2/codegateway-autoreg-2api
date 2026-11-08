#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Минт Turnstile-токена через camoufox — БЕСПЛАТНЫЙ локальный путь.

Адаптировано из autoreg/legacy/wikivibe-autoreg/solver/camoufox_mint.py
(проверено stack-lab: camoufox headless минтит токен, playwright/patchright — нет).

Запускать python'ом, где стоит camoufox (по умолчанию config.camoufox_python):
  python camoufox_mint.py URL SITEKEY [--proxy http://user:pass@host:port]

Печатает одну JSON-строку:
  {"token": "0.xxx…", "seconds": 11.8}   или   {"error": "…"}
Код возврата: 0 — токен есть, 4 — токен не появился, 5 — ошибка.
"""
import argparse
import json
import random
import string
import sys
import time
from urllib.parse import unquote, urlsplit

POLLS = 24          # ~60 с ожидания токена (Turnstile-PoW под нагрузкой небыстрый)
POLL_MS = 2500
READY_TIMEOUT_MS = 20000   # сколько ждать готовности виджета перед кликом по submit


def browser_proxy(raw):
    """Строка прокси → dict {server, username, password} для camoufox."""
    if not raw:
        return None
    s = str(raw).strip()
    if "://" not in s:
        s = "http://" + s
    try:
        u = urlsplit(s)
        if not u.hostname or not u.port:
            return None
    except ValueError:
        return None
    out = {"server": f"{(u.scheme or 'http').lower()}://{u.hostname}:{u.port}"}
    if u.username:
        out["username"] = unquote(u.username)
        out["password"] = unquote(u.password or "")
    return out


def mint(url, sitekey=None, proxy=None, headed=False):
    from camoufox.sync_api import Camoufox  # type: ignore

    if headed:
        print("[mint] окно открыто — пройди капчу руками, токен подхватится сам",
              file=sys.stderr)
    opts = {"headless": not headed, "humanize": True, "locale": "en-US"}
    pw_proxy = browser_proxy(proxy)
    if pw_proxy:
        opts["proxy"] = pw_proxy
        try:
            from camoufox.geolocation import geoip_allowed
            geoip_allowed()
            opts["geoip"] = True      # таймзона/локаль по выходному IP прокси
        except Exception:
            opts["i_know_what_im_doing"] = True

    js = ("() => { const i = document.querySelector('input[name=\"cf-turnstile-response\"]');"
          " return i && i.value ? i.value : null; }")
    t0 = time.time()
    with Camoufox(**opts) as browser:
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        time.sleep(3)
        token = page.evaluate(js)
        if token:
            return {"token": token, "seconds": round(time.time() - t0, 1)}

        # interaction-only Turnstile: виджет исполняется на submit формы.
        # ГЛАВНАЯ ГРАБЛЯ (набито 09.10): жать submit надо ПОСЛЕ того, как форма
        # отрисовалась (иначе клик уходит «в пустоту»), но НЕ ждать iframe
        # виджета — у interaction-only его нет до взаимодействия, и ожидание
        # только жжёт время. Правильный признак готовности — поле email.
        try:
            page.wait_for_selector('input[name="email"], input[type="email"]',
                                   timeout=READY_TIMEOUT_MS)
        except Exception:
            pass  # не дождались — всё равно пробуем заполнить/кликнуть

        # Заполняем форму случайными данными и жмём кнопку — токен минтится,
        # сам сабмит с мусорным email ничего не регистрирует.
        dummy = "mint_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        clicked = False
        try:
            email_el = page.query_selector('input[name="email"], input[type="email"]')
            pass_el = page.query_selector('input[name="password"], input[type="password"]')
            if email_el and pass_el:
                email_el.fill(f"{dummy}@example.com")
                # сайт требует пароль ≥12 символов — иначе submit блокируется
                # клиентской валидацией и виджет не срабатывает
                pass_el.fill("Mint!" + dummy + "9zQw")
                btn = (page.query_selector('button[type="submit"]')
                       or page.query_selector('button:has-text("Create account")')
                       or page.query_selector('button:has-text("Sign up")'))
                if btn:
                    btn.click()
                    clicked = True
        except Exception:
            pass  # форма может отсутствовать — тогда просто ждём виджет

        # Клик мог не сработать (баннер куки/оверлей) — повторяем на всякий
        # случай один раз, но не блокируемся: поллинг ниже ловит токен и так.
        if not clicked:
            try:
                page.click('button:has-text("Create account")', timeout=5000)
            except Exception:
                pass

        polls = 96 if headed else POLLS   # ручное прохождение: ~4 мин на клик
        for i in range(polls):
            token = page.evaluate(js)
            if token:
                return {"token": token, "seconds": round(time.time() - t0, 1)}
            if headed and i % 4 == 3:
                # после фейл-сабмита форма сбрасывает поля — рефиллим,
                # чтобы «Create account» снова стала кликабельной
                try:
                    em = page.query_selector('input[name="email"], input[type="email"]')
                    pw = page.query_selector('input[name="password"], input[type="password"]')
                    if em and pw and not em.input_value():
                        em.fill(f"{dummy}@example.com")
                        pw.fill("Mint!" + dummy + "9zQw")
                except Exception:
                    pass
            time.sleep(POLL_MS / 1000)
    return {"error": "токен не появился за отведённое время"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("sitekey", nargs="?", default=None)
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--headed", action="store_true",
                    help="видимое окно для ручного прохождения капчи")
    args = ap.parse_args()
    try:
        out = mint(args.url, args.sitekey, proxy=args.proxy, headed=args.headed)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 5
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("token") else 4


if __name__ == "__main__":
    sys.exit(main())
