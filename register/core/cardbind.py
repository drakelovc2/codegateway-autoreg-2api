# -*- coding: utf-8 -*-
"""Привязка карты на dashboard.codegateway.dev — браузер + серверный confirm.

Почему так:
  * Прямой POST на api.stripe.com с данными карты и `stripe.createPaymentMethod`
    из JS с сырыми данными запрещены мерчантом (`unsupported_publishable_key_
    tokenization_surface`).
  * `stripe.confirmCardSetup` из браузера проходит проверку поверхности, но
    зависает: Radar гоняет hCaptcha-челлендж, который в headless не решается.
  * Рабочая схема:
      1) браузер на домене мерчанта монтирует Card Element (разрешённая
         поверхность) и делает `createPaymentMethod` ЭЛЕМЕНТОМ → pm_…;
      2) уже серверно `POST /v1/setup_intents/<id>/confirm` с `payment_method=pm_…`
         по publishable-ключу — это не «tokenization», ограничение не срабатывает,
         и мы честно видим JSON-статус (succeeded / card_declined).

Бин 461994 (Visa, NP) проходит; 52792622911 (Mastercard, JP) — generic_decline.
"""
import json

from . import config
from .cardkit import Card, _stripe_post

_MOUNT_JS = """
() => {
  const div = document.createElement('div');
  div.id = 'cg_card_holder';
  div.style.cssText = 'position:fixed;left:0;top:0;z-index:999999;background:#fff;' +
                      'width:420px;padding:10px;';
  document.body.appendChild(div);
  const stripe = window.__cg.stripe;
  const elements = stripe.elements();
  const ce = elements.create('card');
  ce.mount(div);
  window.__cg.elements = elements;
  window.__cg.ce = ce;
  return 'ok';
}
"""

_TOKENIZE_JS = """
() => {
  const {stripe, ce} = window.__cg;
  return stripe.createPaymentMethod({type: 'card', card: ce}).then(r =>
    r.error ? {error: r.error.message, code: r.error.code}
            : {pm: r.paymentMethod.id,
               country: r.paymentMethod.card && r.paymentMethod.card.country,
               brand: r.paymentMethod.card && r.paymentMethod.card.brand,
               last4: r.paymentMethod.card && r.paymentMethod.card.last4});
}
"""

_LOAD_STRIPE_JS = """
() => new Promise((res, rej) => {
  if (typeof Stripe !== 'undefined') return res();
  const sc = document.createElement('script');
  sc.src = 'https://js.stripe.com/v3/';
  sc.onload = res;
  sc.onerror = () => rej(new Error('js.stripe.com load failed'));
  document.head.appendChild(sc);
})
"""


def tokenize_card(card, cookies, proxy=None, headless=True, timeout_ms=60000,
                  on_step=None):
    """PaymentMethod через Card Element на домене мерчанта.

    Возвращает {pm, country, brand, last4} или {error}.
    """
    def step(msg):
        if on_step:
            on_step(msg)

    from playwright.sync_api import sync_playwright

    pk = config.get("stripe_pk") or ""
    result = {"error": "не начато"}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            proxy={"server": proxy} if proxy else None)
        try:
            ctx = browser.new_context(
                viewport={"width": 1440, "height": 1200},
                user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"))
            ctx.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            for name, value in cookies.items():
                ctx.add_cookies([{"name": name, "value": value,
                                  "domain": "dashboard.codegateway.dev", "path": "/"}])
            page = ctx.new_page()
            page.goto("https://dashboard.codegateway.dev/en/account/settings/auto-topup",
                      wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(3500)
            page.evaluate(_LOAD_STRIPE_JS)
            page.evaluate("(pk) => { window.__cg = {stripe: Stripe(pk)}; }", pk)
            page.evaluate(_MOUNT_JS)
            page.wait_for_timeout(5000)

            frame = None
            for _ in range(40):
                for f in page.frames:
                    try:
                        if f.query_selector('input[name="cardnumber"]'):
                            frame = f
                            break
                    except Exception:
                        continue
                if frame:
                    break
                page.wait_for_timeout(500)
            if not frame:
                result = {"error": "Card Element не отрисовался"}
                return result
            frame.fill('input[name="cardnumber"]', card.pan)
            frame.fill('input[name="exp-date"]', f"{card.mm} / {card.yyyy[2:]}")
            frame.fill('input[name="cvc"]', card.cvv)
            page.wait_for_timeout(1200)
            # часть BIN'ов требует ZIP — Element рисует поле только для них
            for sel in ('input[name="postal-code"]', 'input[name="postalCode"]',
                        'input[autocomplete="postal-code"]'):
                try:
                    if frame.query_selector(sel):
                        frame.fill(sel, str(config.get("card_zip") or "10001"))
                        step("postal-code заполнен")
                        break
                except Exception:
                    continue
            page.wait_for_timeout(800)
            result = page.evaluate(_TOKENIZE_JS) or {"error": "пустой ответ createPaymentMethod"}
            if result.get("pm"):
                step(f"токен: {result['pm']} ({result.get('brand')} {result.get('country')})")
            else:
                step(f"токен не получен: {result.get('error') or result.get('code')}")
        except Exception as exc:
            result = {"error": f"{type(exc).__name__}: {exc}"}
            step(f"ошибка браузера: {result['error'][:180]}")
        finally:
            try:
                browser.close()
            except Exception:
                pass
    return result


def confirm_setup(client_secret, pm_id):
    """Серверный confirm SetupIntent по pm_… (без данных карты).

    Возвращает {"status": "succeeded"|"requires_action"|..., "error": {...}}.
    """
    pk = config.get("stripe_pk") or ""
    sid = str(client_secret).split("_secret_")[0]
    data = {"key": pk, "client_secret": client_secret, "payment_method": pm_id}
    resp = _stripe_post(f"/setup_intents/{sid}/confirm", data, timeout=60)
    if isinstance(resp, dict) and resp.get("error"):
        err = resp["error"] if isinstance(resp["error"], dict) else {"message": str(resp["error"])}
        return {"status": None, "error": err}
    return {"status": resp.get("status"), "raw": resp}


def bind_card(client_secret, card, cookies, proxy=None, headless=True,
              on_step=None):
    """Токенизировать карту в браузере и подтвердить SetupIntent серверно.

    Возвращает {"status": ..., "pm": ..., "error": ...}.
    """
    tok = tokenize_card(card, cookies, proxy=proxy, headless=headless,
                        on_step=on_step)
    if not tok.get("pm"):
        return {"status": None, "error": tok}
    res = confirm_setup(client_secret, tok["pm"])
    out = {"status": res.get("status"), "pm": tok["pm"],
           "brand": tok.get("brand"), "last4": tok.get("last4"),
           "country": tok.get("country")}
    if res.get("error"):
        out["error"] = res["error"]
    if on_step:
        on_step(f"confirm: {out['status']} {json.dumps(out.get('error') or {}, ensure_ascii=False)[:120]}")
    return out


__all__ = ["bind_card", "tokenize_card", "confirm_setup", "Card"]
