# -*- coding: utf-8 -*-
"""Конфиг реггера из config.json (рядом с папкой core/).

Скопировал шаблон под новый сервис — заполни `base_url` и прочее СВОИМ.
Значения ниже — только безопасные дефолты; секретов и адресов конкретного
сервиса здесь нет."""
import json
import os

_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_DIR)
CONFIG_PATH = os.path.join(ROOT, "config.json")
DATA_DIR = os.path.join(ROOT, "data")

_DEFAULTS = {
    "base_url": "https://СЕРВИС.example",
    "turnstile_sitekey": "",
    # camoufox | capsolver | auto (camoufox → capsolver fallback)
    "turnstile_solver": "auto",
    # python с установленным camoufox (см. autoreg/labs/stack-lab/.venv)
    "camoufox_python": os.path.join(ROOT, "..", "labs", "stack-lab", ".venv", "bin", "python"),
    "capsolver_api_key": None,
    # Сколько ждать, пока Cloudflare-interstitial уйдёт сам (core/cloudflare.py).
    # Больше ~60 с смысла нет: если не ушёл, дело в отпечатке или IP.
    "cloudflare_timeout_sec": 45,

    # ── reCAPTCHA v2 (core/recaptcha.py) ─────────────────────────────────
    "recaptcha_sitekey": "",
    # auto | browser | vlm | 2captcha. auto = свой браузер ($0) → 2captcha
    "captcha_solver": "auto",
    "twocaptcha_key": "",
    # Порядок солверов сетки: auto | classifier | vlm | both
    #   auto       классификатор → VLM, если классификатор ничего не нашёл
    #   classifier только ONNX (~0.2 с), vlm только VLM (~15 с), both — объединение
    "grid_solver": "auto",
    # Тюнинг сетки: сколько ячеек жать за раунд (пачка ломает динамическую
    # сетку), порог уверенности классификатора (ниже — подтверждает VLM)
    # и пауза на перерисовку тайлов.
    "grid_max_per_round": 3,
    "grid_sure_p": 0.85,
    "grid_round_pause": 1.8,
    # Свой ONNX-классификатор тайлов (14 классов reCAPTCHA), см.
    # провайдеры/sandboxes-vm/instabox-vision.md. Пусто — сразу VLM.
    "grid_api_base": "",
    "grid_api_key": "",
    # OpenAI-совместимая VLM под сетку (vlm_* читает и env VLM_API_BASE/…)
    "vlm_api_base": "",
    "vlm_api_key": "",
    "vlm_model": "",
    # Whisper-совместимый STT под аудио-челлендж (env STT_API_BASE/…)
    "stt_api_base": "",
    "stt_api_key": "",
    "stt_model": "whisper-small",

    # ── hCaptcha / NoneCap (core/hcaptcha.py), capsolver (core/turnstile.py) ─
    # Страница, где стоит капча: нужно солверу как referer/url.
    "signup_page": "",
    "turnstile_mint_direct": False,
    "hcaptcha_sitekey": "",
    # hcaptcha | hcaptcha_enterprise — тип задачи в NoneCap
    # (энтерпрайзный sitekey обычным типом даёт token_not_granted)
    "hcaptcha_type": "hcaptcha",
    # Ключ nc_live_… с https://nonecap.com (1 кредит = 1 решение)
    "nonecap_api_key": "",
    "nonecap_base": "https://api.nonecap.com/v1",
    "nonecap_wait_sec": 60,
    "nonecap_timeout_sec": 180,
    "nonecap_via_proxy": False,

    "mail_base": "https://apimail.drakezakuru.com",
    "mail_admin_key": "",
    "mail_domain": "example.com",
    # Мультидомен: список доменов catch-all. Если непуст — приоритетнее
    # одиночного mail_domain, адрес берёт случайный домен из списка.
    # домены catch-all; подставь свои.
    "mail_domains": [],
    # Домены, к которым добавлять точку в конце (FQDN-обход блок-листа).
    # Пусто — точка не добавляется ни к кому.
    "mail_dot_domains": [],
    # Второй источник: плюс-алиасы одного Gmail-ящика (user+jv...@gmail.com),
    # чтение по IMAP (app-password). Пусто/disabled — не используется.
    "gmail": {
        "enabled": False,
        "user": "",
        "app_password": "",
        "alias_prefix": "jv",
    },
    # Только gmail-алиасы, без catch-all. Нужно, когда apimail недоступен
    # (дневной лимит Cloudflare D1, лежащий воркер) — тогда чтение идёт
    # по IMAP и подмена источника на ходу не нужна.
    "mail_only_gmail": False,
    "mail_ua": "curl/8.5.0",
    "mail_timeout_sec": 150,
    "mail_poll_sec": 5,
    "address_prefix": "",

    # Минтить капчу НАПРЯМУЮ (домашний IP), даже когда регистрация через прокси.
    # Если сервис сверяет IP минта и сабмита — расхождение ловится ошибкой
    # (напр. «a bit fast»). Правильно по умолчанию — mint через тот же прокси
    # (false), см. GUIDE §2. Сверка IP есть НЕ у всех сервисов — проверяй.
    "turnstile_mint_direct": False,

    "proxies": [],
    # Bright Data зоны с session-ротацией (GUIDE §2): свежий IP на каждый акк.
    # Пусто → используем статический data/proxies.txt.
    "brd_zones": [],
    # ISP-зоны BRD: СТАТИЧЕСКИЕ IP. Приоритетнее brd_zones. Нужны, когда сервис
    # сверяет IP минта капчи с IP сабмита (см. IspRotator, GUIDE §2).
    "isp_zones": [],
    # Гео-фильтр зон: оставить только эти страны (["us"] для гео-ограниченных
    # сервисов). Пусто — без фильтра, как раньше.
    "proxy_countries": [],
    # Приоритет ротатора: isp (статичный IP) | dc (свежий IP на аккаунт).
    # Для сервисов без сверки IP капчи выгоднее dc.
    "proxy_prefer": "isp",
    "proxies_file": os.path.join(DATA_DIR, "proxies.txt"),
    "proxy_check_timeout": 10,
    "proxy_check_workers": 20,

    "batch_interval_sec": 25,
    "circuit_breaker_fails": 5,

    "output_jsonl": os.path.join(DATA_DIR, "accounts.jsonl"),
    "output_combo": os.path.join(DATA_DIR, "accounts.txt"),
    # Two-phase: аккаунты, зарегистрированные, но ещё не подтверждённые.
    # Лежат отдельно от готовых, чтобы `verify` взял их и дождался письма.
    "output_pending": os.path.join(DATA_DIR, "pending.jsonl"),

    # Vision-капча (сетка/выбор картинки) и STT для аудио-капчи.
    # Пусто → шаг vision в реггере честно падает, а не молчит.
    "vlm_api_base": None,
    "vlm_api_key": None,
    "vlm_model": None,
    "stt_api_base": None,
    "stt_api_key": None,
    "stt_model": "whisper-1",

    # запасной список free-моделей СВОЕГО сервиса, если /v1/models по ключу не
    # ответил. Заполни под свой сервис (напр. ["model-a:free", "model-b:free"]).
    "fallback_free_models": [],
}

_cfg = dict(_DEFAULTS)
if os.path.exists(CONFIG_PATH):
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        _cfg.update(json.load(f))

# ── ЕДИНЫЙ ИСТОЧНИК ПРОКСИ ──────────────────────────────────────────────────
# Зоны берём из bridges2api/autoreg/proxy/ПРОКСИ.json — ОБЩЕГО файла для всех реггеров
# и мостов, а не хардкодим в своём config.json. Так при смене аккаунта Bright
# Data (или его suspend) правится ОДИН файл, а не десяток.
# Если в источнике autoreg.enabled=false (прокси выключен вручную) — зоны не навязываем,
# реггер ходит напрямую. Локальный config.json может переопределить вручную.
_CENTRAL_PROXY = os.path.normpath(os.path.join(ROOT, "..", "proxy", "ПРОКСИ.json"))
_central_used = False


def _load_central_proxy():
    """Подтянуть зоны/креды из единого источника. True, если источник применился."""
    global _central_used
    try:
        with open(_CENTRAL_PROXY, "r", encoding="utf-8") as f:
            central = json.load(f)
    except (OSError, ValueError):
        return
    if not (central.get("autoreg") or {}).get("enabled"):
        return
    prov = (central.get("providers") or {}).get("brightdata") or {}
    isp, dc = [], []
    for z in prov.get("zones", []):
        rec = {"zone": z.get("zone"), "password": z.get("password")}
        if z.get("country"):
            rec["country"] = z["country"]
        (isp if z.get("kind") == "isp" else dc).append(rec)
    if isp:
        _cfg["isp_zones"] = isp
    if dc:
        _cfg["brd_zones"] = dc
    if prov.get("customer"):
        _cfg["brd_customer"] = prov["customer"]
    if prov.get("host"):
        _cfg["brd_host"] = prov["host"]
    _central_used = True


_load_central_proxy()

for _k, _v in _cfg.items():
    globals()[_k.upper()] = _v

CENTRAL_PROXY_PATH = _CENTRAL_PROXY
CENTRAL_PROXY_USED = _central_used

BASE_URL = _cfg["base_url"].rstrip("/")
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


def get(key, default=None):
    return _cfg.get(key, default)
