#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Батч CodeGateway: N аккаунтов с паузой, ротация прокси, банк после каждого.

    python3 tools/batch.py --count 5 --pause 90
    python3 tools/batch.py --count 3 --pause 60 --proxy http://user:pass@host:port
    python3 tools/batch.py --count 5 --no-proxy

Пишет data/accounts.jsonl (core.results.save_account) и data/logs/batch.log.
Рейт-лимит по IP: короткое окно ~20 с, длинное ~1 ч — на длинном (429
c retryAfter > 120) аккаунт падает с систематической ошибкой, батч ждёт `--rate-sleep`.

Карта: генерация по бинам из config.json (461994 проходит, 52792622911 — нет),
привязка в браузере на домене мерчанта (core/cardbind.py), ходит напрямую.
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import results                      # noqa: E402
from core.register import register_one        # noqa: E402

LOG_DIR = os.path.join(ROOT, "data", "logs")


def log(msg):
    os.makedirs(LOG_DIR, exist_ok=True)
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(os.path.join(LOG_DIR, "batch.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _dc_pool():
    """Статические IP датацентр-зоны BRD (файл ips-datacenter_proxy3*.txt).

    Turnstile DC-IP не отрисовывает — минт идёт напрямую
    (config.turnstile_mint_direct), а регистрация уже через эти IP.
    """
    import glob
    base = os.path.normpath(os.path.join(ROOT, "..", "proxy"))
    files = sorted(glob.glob(os.path.join(base, "ips-datacenter_proxy3*.txt")))
    if not files:
        return []
    out = []
    for line in open(files[-1], encoding="utf-8"):
        line = line.strip()
        if not line or line.count(":") < 3:
            continue
        host, port, user, pw = line.split(":", 3)
        out.append(f"http://{user}:{pw}@{host}:{port}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--pause", type=int, default=90, help="секунд между аккаунтами")
    ap.add_argument("--proxy", default=None, help="один прокси на все аккаунты")
    ap.add_argument("--no-proxy", action="store_true", help="ходить напрямую")
    ap.add_argument("--rotator", action="store_true",
                    help="ротация прокси из единого пула (proxy/ПРОКСИ.json)")
    ap.add_argument("--pool", choices=["rotator", "dc"], default=None,
                    help="dc — пул статических IP datacenter_proxy3 (20 шт.)")
    ap.add_argument("--rate-sleep", dest="rate_sleep", type=int, default=600,
                    help="пауза при длинном рейт-лимите (сек)")
    ap.add_argument("--rate-retries", dest="rate_retries", type=int, default=2)
    ap.add_argument("--net-retries", dest="net_retries", type=int, default=3,
                    help="ретраев при мёртвой прокси-линии")
    ap.add_argument("--ip-cooldown", dest="ip_cooldown", type=int, default=3600,
                    help="сколько секунд не брать IP, который уже регал")
    args = ap.parse_args()

    rot = None
    pool = []
    if args.pool == "dc":
        pool = _dc_pool()
        log(f"пул DC: {len(pool)} IP")
    elif args.rotator or args.pool == "rotator":
        from core.proxy import make_rotator
        rot = make_rotator()
        log(f"ротатор: {len(rot) if rot else 0} зон")

    # Квота IP в CodeGateway персональная: после регистрации IP уходит в лимит
    # на ~20-60 мин. Держим журнал free_at, чтобы батч сам ждал самый ранний IP,
    # а не молотил в 429.
    state_path = os.path.join(ROOT, "data", "proxy_pool.json")
    state = {"used": {}}
    if os.path.exists(state_path):
        try:
            with open(state_path, encoding="utf-8") as f:
                state = json.load(f)
        except ValueError:
            pass
    used = state.setdefault("used", {})

    def _save_state():
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f)

    def mark(proxy, free_at=None, cooldown=None):
        if not proxy:
            return
        now = time.time()
        rec = used.get(proxy) if isinstance(used.get(proxy), dict) else {}
        rec["ts"] = now
        rec["free_at"] = free_at if free_at is not None else now + (cooldown or args.ip_cooldown)
        used[proxy] = rec
        _save_state()

    def next_proxy():
        if pool:
            now = time.time()
            fresh = [p for p in pool if used.get(p, {}).get("free_at", 0) <= now]
            if fresh:
                choice = fresh[0]
            else:
                choice = min(pool, key=lambda p: used.get(p, {}).get("free_at", 0))
                wait = max(0, used[choice].get("free_at", 0) - now)
                log(f"все IP в лимите, жду {int(wait)} c до освобождения {choice[-28:]}")
                time.sleep(wait + 5)
            mark(choice)
            return choice
        return rot.next() if rot is not None else None

    ok = fail = 0
    for i in range(1, args.count + 1):
        log(f"── аккаунт {i}/{args.count} ──")
        t0 = time.time()
        proxy = args.proxy
        if pool or rot is not None:
            proxy = next_proxy()
        if args.no_proxy:
            proxy = None
        rate_tries = 0
        net_tries = 0
        while True:
            try:
                rec = register_one(proxy=proxy,
                                   on_step=lambda m: log(f"   · {m}"))
            except Exception as exc:  # noqa: BLE001 — батч не должен падать целиком
                rec = {"error": f"{type(exc).__name__}: {exc}"}
            results.save_account(rec)
            if rec.get("api_key"):
                ok += 1
                log(f"   ✓ {rec.get('email')} | key={rec['api_key'][:14]}… | "
                    f"bonus={rec.get('bonus_claimed')} | {int(time.time() - t0)}s")
                break
            err = rec.get("error") or "неизвестно"
            log(f"   ✗ {rec.get('email')} | {err[:160]}")
            if rec.get("rate_limited"):
                # у каждого IP своя квота: запоминаем free_at и берём следующий IP
                mark(proxy, free_at=time.time() + int(rec.get("retry_after") or args.rate_sleep))
                rate_tries += 1
                if (pool or rot is not None) and rate_tries <= max(args.rate_retries, 0) * 3:
                    log(f"   этот IP в лимите ({rec.get('retry_after')} c) — беру следующий")
                    proxy = next_proxy()
                    continue
                wait = min(int(rec.get("retry_after") or args.rate_sleep), 3600)
                log(f"   рейт-лимит: жду {wait} c")
                time.sleep(wait)
                if pool or rot is not None:
                    proxy = next_proxy()
                continue
            # мёртвая прокси-линия (BD «No IPs in selected country», tunnel fail) —
            # не вина сервиса: меняем линию и пробуем ещё раз
            net_bad = any(w in err for w in ("ProxyError", "No IPs in selected country",
                                             "Tunnel connection failed", "network:"))
            if net_bad and net_tries < max(args.net_retries, 0) and (pool or rot is not None):
                net_tries += 1
                log(f"   прокси умерла, беру следующую линию ({net_tries}/{args.net_retries})")
                proxy = next_proxy()
                continue
            fail += 1
            break
        if i < args.count:
            time.sleep(args.pause)
    log(f"ИТОГО: ok={ok} fail={fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
