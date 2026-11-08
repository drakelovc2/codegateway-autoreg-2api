#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Параллельный батч CodeGateway.

Ключевая находка 08.11.2026: Turnstile-токен НЕ одноразовый — один токен проходит
несколько регистраций с разных IP (проверено: один токен → 2 регистрации, обе 201).
Значит минт (~47 с) делается ОДИН раз на пачку, а аккаунты регистрируются
параллельно с разных прокси.

    python3 tools/batch_parallel.py --count 10 --workers 3 --pool dc
    python3 tools/batch_parallel.py --count 6 --workers 2 --rotator

Ограничители:
  * квота IP (~1-2 регистрации на IP, потом лимит 20-60 мин) — журнал free_at;
  * токен живёт ~5 мин (кэш обновляет его заранее);
  * карточный шаг — браузер на каждый аккаунт (память), поэтому workers ≤ 3-4.
"""
import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import results                      # noqa: E402
from core.register import ServiceClient, register_one   # noqa: E402

LOG_DIR = os.path.join(ROOT, "data", "logs")
STATE_PATH = os.path.join(ROOT, "data", "proxy_pool.json")
print_lock = threading.Lock()


def log(msg):
    os.makedirs(LOG_DIR, exist_ok=True)
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    with print_lock:
        print(line, flush=True)
        with open(os.path.join(LOG_DIR, "batch_parallel.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")


def _dc_pool():
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


def _json_pool(providers):
    """Линии из общего пула autoreg/proxy/proxies.json (alive+enabled)."""
    path = os.path.normpath(os.path.join(ROOT, "..", "proxy", "proxies.json"))
    try:
        data = json.load(open(path, encoding="utf-8")).get("proxies", [])
    except (OSError, ValueError):
        return []
    return [p["url"] for p in data
            if p.get("status") == "alive" and p.get("enabled")
            and p.get("provider") in providers and p.get("url")]


class TokenCache:
    """Один общий Turnstile-токен на пачку; пере-минт под локом, TTL ~4 мин."""

    def __init__(self, ttl=230):
        self.ttl = ttl
        self.lock = threading.Lock()
        self.token = ""
        self.minted_at = 0.0

    def get(self):
        with self.lock:
            if self.token and time.time() - self.minted_at < self.ttl:
                return self.token
            log("   минчу Turnstile (общий токен на пачку)…")
            try:
                self.token = ServiceClient().mint_turnstile() or ""
            except Exception as exc:  # noqa: BLE001
                log(f"   минт упал: {exc}")
                self.token = ""
            self.minted_at = time.time()
            return self.token


class Pool:
    """Пути к прокси + журнал квот IP (free_at), потокобезопасно."""

    def __init__(self, pool, rot, cooldown):
        self.pool = pool
        self.rot = rot
        self.cooldown = cooldown
        # RLock: next() держит lock и внутри вызывает mark() (тот же поток)
        self.lock = threading.RLock()
        self._rr = 0
        self.used = {}
        if os.path.exists(STATE_PATH):
            try:
                with open(STATE_PATH, encoding="utf-8") as f:
                    self.used = json.load(f).get("used", {})
            except ValueError:
                pass

    def _save(self):
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"used": self.used}, f)

    @staticmethod
    def _free_at(rec):
        return rec.get("free_at", 0) if isinstance(rec, dict) else float(rec)

    def mark(self, proxy, free_at=None, cooldown=None):
        if not proxy:
            return
        now = time.time()
        with self.lock:
            rec = self.used.get(proxy)
            rec = rec if isinstance(rec, dict) else {}
            rec["ts"] = now
            rec["free_at"] = free_at if free_at is not None else now + (cooldown or self.cooldown)
            self.used[proxy] = rec
            self._save()

    def next(self):
        if not self.pool:
            return self.rot.next() if self.rot is not None else None
        # ротирующие линии (cooldown=0): просто по кругу, каждый запрос — новый IP
        if self.cooldown <= 0:
            with self.lock:
                p = self.pool[self._rr % len(self.pool)]
                self._rr += 1
                return p
        while True:
            now = time.time()
            with self.lock:
                fresh = [p for p in self.pool if self._free_at(self.used.get(p, {})) <= now]
                if fresh:
                    choice = fresh[0]
                    self.mark(choice)
                    return choice
                choice = min(self.pool, key=lambda p: self._free_at(self.used.get(p, {})))
                wait = self._free_at(self.used[choice]) - now
            log(f"   все IP заняты, жду {int(max(wait, 0))} c…")
            time.sleep(max(wait, 0) + 5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=5)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--pool", choices=["dc", "decodo", "rotator"], default="dc")
    ap.add_argument("--ip-cooldown", dest="ip_cooldown", type=int, default=3600)
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--token-ttl", dest="token_ttl", type=int, default=230)
    ap.add_argument("--attempts", type=int, default=4,
                    help="попыток на аккаунт (новая прокси-линия при 429)")
    args = ap.parse_args()

    rot = None
    cooldown = args.ip_cooldown
    if args.pool == "dc":
        pool = _dc_pool()
        log(f"пул DC: {len(pool)} IP")
    elif args.pool == "decodo":
        pool = _json_pool({"decodo-autoreg", "decodo"})
        # decodo-линия ротирует IP на каждый запрос — кулдаун не нужен
        cooldown = 0
        log(f"пул decodo: {len(pool)} линий (ротация IP на запрос)")
    else:
        from core.proxy import make_rotator
        pool, rot = [], make_rotator()
        log(f"ротатор: {len(rot) if rot else 0} зон")

    proxies = Pool(pool, rot, cooldown)
    tokens = TokenCache(ttl=args.token_ttl)
    done = {"ok": 0, "fail": 0}
    done_lock = threading.Lock()

    def one(i):
        t0 = time.time()
        rec = {}
        for attempt in range(1, args.attempts + 1):
            proxy = args.proxy or proxies.next()
            log(f"── аккаунт {i}/{args.count} "
                f"(ip {str(proxy)[-30:] if proxy else 'direct'}, попытка {attempt}/{args.attempts}) ──")
            rec = register_one(proxy=proxy,
                               token_provider=tokens.get,
                               on_step=lambda m: log(f"   · {m}"))
            results.save_account(rec)
            if rec.get("rate_limited"):
                proxies.mark(proxy, free_at=time.time() + int(rec.get("retry_after") or args.ip_cooldown))
                if attempt < args.attempts:
                    log(f"   IP в лимите ({rec.get('retry_after')} c) — беру другой")
                    continue
            break
        if rec.get("api_key"):
            proxies.mark(proxy, free_at=time.time() + args.ip_cooldown)
        with done_lock:
            if rec.get("api_key"):
                done["ok"] += 1
                log(f"   ✓ {rec.get('email')} | key={rec['api_key'][:14]}… | "
                    f"bonus={rec.get('bonus_claimed')} | {int(time.time() - t0)}s")
            else:
                done["fail"] += 1
                log(f"   ✗ {rec.get('email')} | {(rec.get('error') or '?')[:140]}")

    t_all = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = [ex.submit(one, i) for i in range(1, args.count + 1)]
        for f in as_completed(futures):
            f.result()
    dt = int(time.time() - t_all)
    log(f"ИТОГО: ok={done['ok']} fail={done['fail']} за {dt}s "
        f"({dt / max(args.count, 1):.0f}s/акк при {args.workers} воркерах)")
    return 0 if done["fail"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
