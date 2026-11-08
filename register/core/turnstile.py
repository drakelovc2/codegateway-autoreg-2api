# -*- coding: utf-8 -*-
"""Turnstile: бесплатный минт через camoufox (локально), платный fallback — capsolver.

solve(url, sitekey, proxy) → token | None
Режим выбирается config.turnstile_solver: camoufox | capsolver | auto.
"""
import json
import os
import subprocess
import time

import requests

from . import config

_MINT_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "camoufox_mint.py")


def mint_camoufox(url, sitekey=None, proxy=None, timeout=120, headed=False):
    """Токен через локальный camoufox ($0). None при фейле.

    headed=True — видимое окно для ручного прохождения капчи; таймаут
    поднимаем до 5 минут (скрипт внутри ждёт токен ~4 мин).
    """
    py = config.CAMOUFOX_PYTHON
    if not os.path.exists(py):
        print(f"  [turnstile] camoufox-python не найден: {py}")
        return None
    # -P: НЕ добавлять каталог скрипта (core/) в sys.path — иначе core/logging.py
    # перекрывает stdlib logging и camoufox падает на `from logging import NullHandler`.
    cmd = [py, "-P", _MINT_SCRIPT, url, sitekey or config.TURNSTILE_SITEKEY]
    if proxy:
        cmd += ["--proxy", proxy]
    if headed:
        cmd += ["--headed"]
        timeout = max(timeout, 300)
        print("  [turnstile] ОКНО camoufox открыто — пройди капчу руками, "
              "токен подхватится автоматически")
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONHOME", "PYTHONPATH", "LD_LIBRARY_PATH")}
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        print("  [turnstile] camoufox: таймаут")
        return None
    line = (p.stdout or "").strip().splitlines()
    if not line:
        print(f"  [turnstile] camoufox: пустой вывод (rc={p.returncode}) {(p.stderr or '')[-200:]}")
        return None
    try:
        data = json.loads(line[-1])
    except ValueError:
        print(f"  [turnstile] camoufox: мусор в выводе: {line[-1][:160]}")
        return None
    if data.get("token"):
        print(f"  [turnstile] camoufox: токен за {data.get('seconds', '?')}с ($0)")
        return data["token"]
    print(f"  [turnstile] camoufox: {data.get('error', 'нет токена')}")
    return None


def solve_capsolver(url, sitekey=None, timeout=90, poll=3):
    """Токен через capsolver (~$0.003). None при фейле/нет ключа."""
    key = config.CAPSOLVER_API_KEY
    if not key:
        print("  [turnstile] capsolver: нет ключа в config.json")
        return None
    try:
        r = requests.post(
            "https://api.capsolver.com/createTask",
            json={"clientKey": key, "task": {
                "type": "AntiTurnstileTaskProxyLess",
                "websiteURL": url,
                "websiteKey": sitekey or config.TURNSTILE_SITEKEY,
            }},
            timeout=15)
        data = r.json()
    except Exception as exc:
        print(f"  [turnstile] capsolver createTask: {exc}")
        return None
    if data.get("errorId") != 0:
        print(f"  [turnstile] capsolver: {data.get('errorDescription', data)}")
        return None

    task_id = data["taskId"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(poll)
        try:
            data = requests.post(
                "https://api.capsolver.com/getTaskResult",
                json={"clientKey": key, "taskId": task_id}, timeout=10).json()
        except Exception:
            continue
        if data.get("status") == "ready":
            print("  [turnstile] capsolver: токен получен")
            return data["solution"]["token"]
        if data.get("errorId") != 0:
            print(f"  [turnstile] capsolver: {data.get('errorDescription', data)}")
            return None
    print("  [turnstile] capsolver: таймаут")
    return None


def solve_peak(url, sitekey=None, proxy=None, timeout_ms=None):
    """Токен через Peak (api.peak.fo) — платный, ~$0.001–0.003 за решение.

    Peak требует, чтобы url заканчивался слэшем. С прокси задача
    `turnstiletask` (токен минтится С ТОГО ЖЕ IP, что и сабмит — важно, если
    сервис сверяет IP), без прокси — `turnstiletaskproxyless`.
    """
    key = config.get("peak_api_key")
    if not key:
        print("  [turnstile] peak: нет peak_api_key в config.json")
        return None
    url_slash = url if url.endswith("/") else url.rstrip("/") + "/"
    task = config.get("peak_task") or ("turnstiletask" if proxy else "turnstiletaskproxyless")
    payload = {"task_type": task, "sitekey": sitekey or config.TURNSTILE_SITEKEY, "url": url_slash}
    if proxy:
        payload["proxy"] = proxy
    try:
        r = requests.post("https://api.peak.fo/solve", json=payload,
                          headers={"X-API-Key": key, "Content-Type": "application/json"},
                          timeout=float(config.get("peak_timeout_sec") or 180))
        data = r.json()
    except Exception as exc:
        print(f"  [turnstile] peak: сеть {str(exc)[:100]}")
        return None
    if data.get("success"):
        tok = (data.get("data") or {}).get("token")
        if tok:
            print("  [turnstile] peak: токен получен")
            return tok
    print(f"  [turnstile] peak: {data.get('error') or data}")
    return None


def solve(url, proxy=None, sitekey=None):
    """Единая точка: режим из конфига, auto = camoufox → peak → capsolver.

    turnstile_mint_direct=True → минт идёт с домашнего IP, даже если регистрация
    через прокси (под datacenter-IP tokenharbor не отрисовывает виджет Turnstile,
    см. config.py). Turnstile не привязывает токен к IP сабмита.
    """
    mode = (config.TURNSTILE_SOLVER or "auto").lower()
    mint_proxy = None if config.TURNSTILE_MINT_DIRECT else proxy
    if mint_proxy is None and proxy and config.TURNSTILE_MINT_DIRECT:
        print("  [turnstile] минт напрямую (домашний IP), регистрация через прокси")
    if mode in ("camoufox", "auto"):
        token = mint_camoufox(url, sitekey, proxy=mint_proxy,
                              headed=bool(config.get("turnstile_headed", False)))
        if token or mode == "camoufox":
            return token
    if mode in ("peak", "auto"):
        token = solve_peak(url, sitekey, proxy=proxy)
        if token or mode == "peak":
            return token
    if mode in ("capsolver", "auto"):
        return solve_capsolver(url, sitekey)
    print(f"  [turnstile] неизвестный режим: {mode}")
    return None
