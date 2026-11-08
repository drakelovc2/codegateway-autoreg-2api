# -*- coding: utf-8 -*-
"""NoneCap — hosted hCaptcha solver (api.nonecap.com).

POST /v1/solves?wait=N {type, sitekey, url} -> solve (200 solved / 202 pending)
GET  /v1/solves/{id}?wait=N             -> poll
Токен: P1_... → подставить в h-captcha-response; user_agent из ответа.

Биллинг: 1 кредит за успешный раунд, фейлы не списываются.
"""
import time

import requests

from . import config


class NoneCapError(Exception):
    pass


class NoneCap:
    def __init__(self, api_key=None, base=None, proxy=None):
        """proxy — прокси для самих запросов к API NoneCap (строка или dict)."""
        self.key = api_key or config.NONECAP_API_KEY
        if not self.key:
            raise NoneCapError("NONECAP_API_KEY не задан (config.py или env)")
        self.base = (base or config.NONECAP_BASE).rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        if isinstance(proxy, dict):
            proxy = proxy.get("raw")
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}

    # ── баланс ───────────────────────────────────────────
    # Ключ может быть без scope 'account' → /me отвечает 403: баланс недоступен,
    # но решать это не мешает (панель показывает «н/д»).
    def balance(self, timeout=20):
        r = self.session.get(f"{self.base}/me", timeout=timeout)
        if r.status_code != 200:
            raise NoneCapError(f"/me: {r.status_code} {r.text[:200]}")
        return r.json()

    # ── создание решения ─────────────────────────────────
    def create_solve(self, sitekey, url, rqdata=None, proxy=None, solve_type=None):
        """proxy — прокси, с которого решалка проходит челлендж (строка или dict)."""
        if isinstance(proxy, dict):
            proxy = proxy.get("raw")
        body = {"type": solve_type or config.HCAPTCHA_TYPE,
                "sitekey": sitekey, "url": url}
        if rqdata:
            body["rqdata"] = rqdata
        if proxy:
            body["proxy"] = proxy
        r = self.session.post(
            f"{self.base}/solves?wait={config.NONECAP_WAIT_SEC}",
            json=body, timeout=config.NONECAP_WAIT_SEC + 30,
        )
        if r.status_code == 402:
            raise NoneCapError(f"кредиты кончились: {r.text[:200]}")
        if r.status_code not in (200, 202):
            raise NoneCapError(f"create solve: {r.status_code} {r.text[:300]}")
        return r.json()

    # ── ожидание терминала ───────────────────────────────
    def wait_solve(self, solve_id, timeout=None):
        deadline = time.time() + (timeout or config.NONECAP_TIMEOUT_SEC)
        while time.time() < deadline:
            left = min(60, int(deadline - time.time()))
            if left <= 0:
                break
            r = self.session.get(f"{self.base}/solves/{solve_id}?wait={left}",
                                 timeout=left + 30)
            if r.status_code == 404:
                raise NoneCapError(f"solve {solve_id} исчез (404)")
            if r.status_code not in (200, 202):
                raise NoneCapError(f"poll solve: {r.status_code} {r.text[:200]}")
            data = r.json()
            if data.get("status") in ("solved", "failed", "cancelled", "expired"):
                return data
            time.sleep(1)
        raise NoneCapError(f"solve {solve_id}: не дождались за {timeout or config.NONECAP_TIMEOUT_SEC}s")

    # ── высокоуровнево: один токен ───────────────────────
    def solve_hcaptcha(self, sitekey=None, url=None, proxy=None):
        """Возвращает (token, user_agent). Бросает NoneCapError."""
        sitekey = sitekey or config.HCAPTCHA_SITEKEY
        url = url or config.SIGNUP_PAGE
        created = self.create_solve(sitekey, url, proxy=proxy)
        if created.get("status") == "solved":
            data = created
        else:
            solve_id = created.get("id")
            if not solve_id:
                raise NoneCapError(f"нет id в ответе: {created}")
            data = self.wait_solve(solve_id)
        if data.get("status") != "solved":
            err = (data.get("error") or {})
            raise NoneCapError(
                "solve не решён: %s %s" % (data.get("status"),
                                           err.get("code") or err.get("message") or ""))
        token = data.get("token")
        if not token or len(token) < 40:
            raise NoneCapError(f"пустой/короткий токен: {str(token)[:40]}")
        return token, data.get("user_agent")