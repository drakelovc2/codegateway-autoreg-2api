#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Фоновый добиватель до N аккаунтов CodeGateway.

Использует ВСЁ, что есть:
  * decodo-линии из autoreg/proxy/proxies.json (ротация IP на запрос);
  * brightdata-линии оттуда же (ISP/DC);
  * 20 статических IP зоны datacenter_proxy3 (журнал квот free_at);
  * ротатор ISP-зон.

Логика:
  * один общий Turnstile-токен на ~4 мин (минт локально, camoufox);
  * аккаунт = register → verify → карта → ключ;
  * 429 → берём следующий прокси, IP записывается в журнал с free_at;
  * behavioral_blocked → мягкий блок: IP в кулдаун, после 3 подряд — пауза 10 мин;
  * все IP заняты → ждём самый ранний free_at;
  * цель — N аккаунтов С КЛЮЧОМ в accounts.jsonl (старые считаются).

Запуск:
    nohup python3 -u tools/run_to_target.py --target 100 --workers 2 \
        > data/logs/run100.log 2>&1 &
"""
import argparse
import glob
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
STATE_PATH = os.path.join(ROOT, "data", "proxy_pool_state.json")
PROXIES_JSON = os.path.normpath(os.path.join(ROOT, "..", "proxy", "proxies.json"))
IPS_GLOB = os.path.normpath(os.path.join(ROOT, "..", "proxy", "ips-datacenter_proxy3*.txt"))
print_lock = threading.Lock()


def log(msg):
    os.makedirs(LOG_DIR, exist_ok=True)
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    with print_lock:
        print(line, flush=True)
        with open(os.path.join(LOG_DIR, "run_to_target.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")


class TokenCache:
    def __init__(self, ttl=230):
        self.ttl = ttl
        self.lock = threading.Lock()
        self.token = ""
        self.minted_at = 0.0

    def get(self):
        with self.lock:
            if self.token and time.time() - self.minted_at < self.ttl:
                return self.token
            log("   миню Turnstile (общий токен, ~5 мин)…")
            try:
                self.token = ServiceClient().mint_turnstile() or ""
            except Exception as exc:  # noqa: BLE001
                log(f"   минт упал: {exc}")
                self.token = ""
            self.minted_at = time.time()
            if self.token:
                log(f"   токен получен ({len(self.token)} симв.)")
            return self.token


def load_pools(only_isp=False):
    """(static, rotating): static — с кулдауном по IP, rotating — decodo (новый IP на запрос).

    only_isp=True — только жилые ISP-линии из proxies.json (браузерная рега:
    Cloudflare рисует Turnstile только на residential IP).
    """
    static, rotating = [], []
    try:
        data = json.load(open(PROXIES_JSON, encoding="utf-8")).get("proxies", [])
    except (OSError, ValueError):
        data = []
    for p in data:
        if p.get("status") != "alive" or not p.get("enabled") or not p.get("url"):
            continue
        prov = p.get("provider")
        ident = (str(p.get("id") or "") + " " + str(p.get("label") or "")).lower()
        if only_isp and "isp" not in ident:
            continue
        if prov == "decodo-autoreg" and not only_isp:
            rotating.append(p["url"])
        elif prov in ("brightdata", "decodo"):
            static.append(p["url"])
    if only_isp:
        return static, rotating
    for f in sorted(glob.glob(IPS_GLOB)):
        for line in open(f, encoding="utf-8"):
            line = line.strip()
            if not line or line.count(":") < 3:
                continue
            host, port, user, pw = line.split(":", 3)
            static.append(f"http://{user}:{pw}@{host}:{port}")
    return static, rotating


class Pool:
    def __init__(self, static, rotating, cooldown):
        self.static = list(dict.fromkeys(static))
        self.rotating = list(dict.fromkeys(rotating))
        self.cooldown = cooldown
        self.lock = threading.RLock()
        self.rr = 0
        self.used = {}
        if os.path.exists(STATE_PATH):
            try:
                self.used = json.load(open(STATE_PATH, encoding="utf-8")).get("used", {})
            except ValueError:
                pass

    def _save(self):
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"used": self.used}, f)

    @staticmethod
    def _fa(rec):
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
        # 1) свободные статические
        now = time.time()
        with self.lock:
            fresh = [p for p in self.static if self._fa(self.used.get(p, {})) <= now]
            if fresh:
                p = fresh[0]
                self.mark(p)
                return p
        # 2) ротирующие decodo — каждый запрос новый IP
        if self.rotating:
            with self.lock:
                p = self.rotating[self.rr % len(self.rotating)]
                self.rr += 1
                return p
        # 3) всё занято — ждём самый ранний
        if self.static:
            with self.lock:
                p = min(self.static, key=lambda x: self._fa(self.used.get(x, {})))
                wait = max(0, self._fa(self.used.get(p, {})) - time.time())
            log(f"   все IP заняты, жду {int(wait)} c…")
            time.sleep(wait + 5)
            return p
        return None


def count_ok():
    path = os.path.join(ROOT, "data", "accounts.jsonl")
    if not os.path.exists(path):
        return 0, 0
    ok = total = 0
    for line in open(path, encoding="utf-8"):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        total += 1
        if r.get("api_key"):
            ok += 1
    return ok, total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=100, help="сколько ВСЕГО аккаунтов с ключом нужно")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--attempts", type=int, default=6)
    ap.add_argument("--ip-cooldown", dest="ip_cooldown", type=int, default=1800)
    ap.add_argument("--max-hours", dest="max_hours", type=float, default=12)
    ap.add_argument("--only-isp", dest="only_isp", action="store_true",
                    help="только жилые ISP-линии (для браузерной реги")
    args = ap.parse_args()

    static, rotating = load_pools(only_isp=args.only_isp)
    log(f"пул: static={len(static)} rotating(decodo)={len(rotating)}")
    pool = Pool(static, rotating, args.ip_cooldown)
    tokens = TokenCache()
    lock = threading.Lock()
    stats = {"ok": 0, "fail": 0, "behavioral": 0}
    ok_start, total_start = count_ok()
    log(f"старт: аккаунтов с ключом {ok_start}/{args.target}")
    deadline = time.time() + args.max_hours * 3600

    def one(i):
        if time.time() > deadline:
            return
        rec = {}
        t0 = time.time()
        for attempt in range(1, args.attempts + 1):
            proxy = pool.next()
            if proxy is None:
                log("   нет прокси вообще — пауза 60 с")
                time.sleep(60)
                continue
            short = proxy.split("@")[-1]
            log(f"── #{i} попытка {attempt}/{args.attempts} ({short}) ──")
            try:
                rec = register_one(proxy=proxy, token_provider=tokens.get,
                                   on_step=lambda m: log(f"   · {m}"))
            except Exception as exc:  # noqa: BLE001
                rec = {"error": f"{type(exc).__name__}: {exc}"}
            results.save_account(rec)
            err = (rec.get("error") or "")
            if rec.get("rate_limited"):
                pool.mark(proxy, free_at=time.time() + int(rec.get("retry_after") or args.ip_cooldown))
                continue
            # неудачная попытка без лимита/бана IP — линию не выжигаем, отпускаем сразу
            if not rec.get("rate_limited") and "behavioral" not in rec \
                    and "turnstile_not_rendered" in err:
                pool.mark(proxy, free_at=0)
                continue
            if "behavioral_blocked" in err or "suspicious activity" in err or rec.get("behavioral"):
                with lock:
                    stats["behavioral"] += 1
                    n = stats["behavioral"]
                pool.mark(proxy, free_at=time.time() + args.ip_cooldown)
                if n % 3 == 0:
                    log("   3 behavioral-блока подряд — пауза 10 мин")
                    time.sleep(600)
                continue
            if rec.get("api_key"):
                break
            break
        with lock:
            if rec.get("api_key"):
                stats["ok"] += 1
                log(f"   ✓ {rec.get('email')} | bonus={rec.get('bonus_claimed')} | "
                    f"{int(time.time() - t0)}s | всего ok={stats['ok']}")
            else:
                stats["fail"] += 1
                log(f"   ✗ {rec.get('email')} | {(rec.get('error') or '?')[:120]}")

    t_all = time.time()
    idx = 0
    while time.time() < deadline:
        ok_now, _ = count_ok()
        if ok_now >= args.target:
            log(f"ЦЕЛЬ ДОСТИГНУТА: {ok_now}/{args.target}")
            break
        batch = max(args.workers * 2, 2)
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = []
            for _ in range(batch):
                idx += 1
                futs.append(ex.submit(one, idx))
            for f in as_completed(futs):
                f.result()
        log(f"— волна: ok={stats['ok']} fail={stats['fail']} behavioral={stats['behavioral']} "
            f"| всего в базе: {count_ok()[0]}/{args.target} | {int(time.time() - t_all)}с")
        # небольшая пауза между волнами, чтобы не долбить
        time.sleep(20)
    ok_now, total_now = count_ok()
    log(f"ИТОГО: {ok_now}/{args.target} с ключом (всего записей {total_now}), "
        f"ok={stats['ok']} fail={stats['fail']} behavioral={stats['behavioral']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
