# -*- coding: utf-8 -*-
"""reCAPTCHA v2: свой браузер + свой vision-стек ($0), 2captcha как fallback.

Токен привязан к домену сайта, поэтому минтим его НА САМОЙ странице: рендерим
виджет в отдельный div, жмём чекбокс, а если выпал картинка-челлендж —
решаем сетку и кликаем по ячейкам.

Солверы сетки (`grid_solver`): classifier — ONNX YOLO11x-cls на 57k тайлов,
14 классов, ~0.2 с; vlm — OpenAI-совместимая VLM, ~15 с; auto — классификатор,
а сомнительные ячейки подтверждает VLM; both — объединение.

Тюнинг: `grid_sure_p` (порог уверенности), `grid_max_per_round` (сколько
ячеек жать за раунд), `grid_round_pause` (пауза на перерисовку).

Аудио-челлендж — в `vision.solve_audio_captcha` (Whisper-совместимый STT).
Свой путь стоит $0; verify идёт fetch'ем из той же страницы, так что
origin/TLS-контекст совпадает по построению.
"""
import base64
import hashlib
import json
import os
import re
import time

import requests

from . import config

TWOCAPTCHA_CREATE = "https://api.2captcha.com/createTask"
TWOCAPTCHA_RESULT = "https://api.2captcha.com/getTaskResult"

# Одно соединение на процесс: у VLM/классификатора это экономит TLS-хендшейк
# на каждом раунде сетки (в динамической их бывает 6–8 подряд).
_SESSION = requests.Session()

# recaptcha.net — зеркало для регионов, где google.com недоступен.
# Важно: через наши прокси www.google.com отдаёт ProxyError, а
# www.recaptcha.net — 200; токен тот же, sitekey тот же.
_LOAD_JS = """() => new Promise((resolve) => {
  if (window.grecaptcha && window.grecaptcha.render) return resolve(true);
  window.__mcap_onload = () => resolve(true);
  const srcs = [
    'https://www.recaptcha.net/recaptcha/api.js?render=explicit&onload=__mcap_onload',
    'https://www.google.com/recaptcha/api.js?render=explicit&onload=__mcap_onload'
  ];
  let i = 0;
  const next = () => {
    if (i >= srcs.length) return resolve(false);
    const s = document.createElement('script');
    s.src = srcs[i++];
    s.onerror = () => { s.remove(); next(); };
    document.head.appendChild(s);
  };
  next();
  setTimeout(() => resolve(!!(window.grecaptcha && window.grecaptcha.render)), 15000);
})"""

_RENDER_JS = """(sitekey) => {
  try {
    let box = document.getElementById('__mcap_box');
    if (!box) {
      box = document.createElement('div');
      box.id = '__mcap_box';
      box.style.cssText = 'position:fixed;left:24px;top:24px;z-index:2147483647;background:#fff;padding:8px;border-radius:8px;';
      document.body.appendChild(box);
    }
    if (window.__mcap_widget === undefined || window.__mcap_widget === null) {
      window.__mcap_done = 0;
      window.__mcap_widget = grecaptcha.render(box, {
        sitekey: sitekey,
        callback: () => { window.__mcap_done = 1; },
        'expired-callback': () => { window.__mcap_done = 0; }
      });
    }
    return String(window.__mcap_widget);
  } catch (e) { return 'ERR:' + e; }
}"""

_TOKEN_JS = """() => {
  try {
    const w = window.__mcap_widget;
    const v = (w === undefined || w === null) ? '' : grecaptcha.getResponse(w);
    return v || '';
  } catch (e) { return ''; }
}"""

# bframe челленджа: сетка ячеек + инструкция + VERIFY
_BFRAME = 'iframe[src*="api2/bframe"]'
_TABLE = 'table[class*="rc-imageselect-table"]'
_INSTR = '.rc-imageselect-desc-wrapper'
_TILES = 'table[class*="rc-imageselect-table"] td.rc-imageselect-tile'

GRID_PROMPT = (
    "This is a reCAPTCHA image grid ({rows}x{cols}). Cells are numbered "
    "left-to-right, top-to-bottom starting at 0. Instruction: {question}\n"
    "Look at each cell and decide if it contains the requested object. "
    "Answer ONLY JSON: {{\"cells\": [<indices>], \"confidence\": <0-1>}}"
)


def _log(msg):
    print(f"  [captcha] {msg}", flush=True)


# ── VLM ────────────────────────────────────────────────────────────────────
def vlm_cfg():
    base = (os.environ.get("VLM_API_BASE") or config.get("vlm_api_base") or "").rstrip("/")
    key = os.environ.get("VLM_API_KEY") or config.get("vlm_api_key") or ""
    model = os.environ.get("VLM_MODEL") or config.get("vlm_model") or ""
    return base, key, model


def ask_vlm(image_path, prompt, timeout=None):
    """Картинка + текст → ответ VLM (текст). Ошибка → пустая строка.

    Обязательно стримом: CPU-VLM думает 10–20 с, а приватный порт Instabox
    обрывает молчащий запрос на ~20-й секунде (502 Bad Gateway). SSE отдаёт
    заголовки сразу, и соединение живёт сколько нужно.
    """
    base, key, model = vlm_cfg()
    if not (base and key and model):
        _log("VLM не настроен (vlm_api_base/vlm_api_key/vlm_model)")
        return ""
    try:
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
    except OSError as exc:
        _log(f"VLM: не прочитать скриншот: {exc}")
        return ""
    body = {
        "model": model,
        "max_tokens": 300,
        "temperature": 0,
        "stream": True,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}},
        ]}],
    }
    try:
        r = _SESSION.post(base + "/chat/completions", json=body, timeout=timeout or 180,
                          stream=True,
                          headers={"Authorization": "Bearer " + key,
                                   "Content-Type": "application/json"})
        if r.status_code != 200:
            _log(f"VLM HTTP {r.status_code}: {r.text[:120]}")
            return ""
        out = []
        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk == "[DONE]":
                break
            try:
                d = json.loads(chunk)
            except ValueError:
                continue
            for ch in d.get("choices") or []:
                delta = ch.get("delta") or ch.get("message") or {}
                for field in ("content", "reasoning_content"):
                    if delta.get(field):
                        out.append(delta[field])
        return "".join(out).strip()
    except Exception as exc:
        _log(f"VLM: {type(exc).__name__}: {str(exc)[:100]}")
        return ""


# ── классификатор тайлов (быстрый путь) ────────────────────────────────────
def grid_cfg():
    base = (os.environ.get("GRID_API_BASE") or config.get("grid_api_base") or "").rstrip("/")
    key = os.environ.get("GRID_API_KEY") or config.get("grid_api_key") or ""
    return base, key


def classify_tiles(paths, question, timeout=25):
    """Оригиналы тайлов → (cells, confidence, label, probs).

    `probs` — {индекс: вероятность целевого класса}, чтобы вызывающий мог
    отделить уверенные ячейки от сомнительных и по сомнительным спросить
    VLM (см. green_threshold в solve_grid_challenge).

    В отличие от VLM, эта штука не «рассуждает», а классифицирует каждый
    тайл по 14 классам reCAPTCHA: ~0.2 с на сетку вместо ~15 с. Зато видит
    только те объекты, на которых обучена (светофор, зебра, автобус, …),
    поэтому в solve_grid_challenge она идёт первой, а VLM добирает.
    """
    base, key = grid_cfg()
    if not (base and key):
        return None, 0.0, "", {}
    tiles = []
    for path in paths:
        if not path:
            return None, 0.0, "", {}
        try:
            with open(path, "rb") as f:
                tiles.append(base64.b64encode(f.read()).decode())
        except OSError:
            return None, 0.0, "", {}
    try:
        r = _SESSION.post(base + "/v1/captcha/tiles",
                          json={"question": question, "tiles": tiles},
                          timeout=timeout,
                          headers={"Authorization": "Bearer " + key,
                                   "Content-Type": "application/json"})
        if r.status_code != 200:
            _log(f"классификатор HTTP {r.status_code}: {r.text[:120]}")
            return None, 0.0, "", {}
        d = r.json()
        probs = {int(x["cell"]): float(x.get("p_target") or 0.0)
                 for x in (d.get("detail") or []) if "cell" in x}
        return (d.get("cells") or [], float(d.get("confidence") or 0.0),
                d.get("label") or "", probs)
    except Exception as exc:
        _log(f"классификатор: {type(exc).__name__}: {str(exc)[:100]}")
        return None, 0.0, "", {}


def parse_cells(text, limit):
    """Ответ VLM → индексы ячеек. Терпимо к ```json, лишнему тексту.

    Модели отвечают по-разному: {"cells": [...]}, {"answer": [...]} (так
    делает LFM3-VL), просто списком. Любой из вариантов — валиден.
    """
    if not text:
        return [], 0.0
    cells, conf = [], 0.0
    for m in re.finditer(r"\{[\s\S]*?\}", text):
        try:
            j = json.loads(m.group(0))
        except (ValueError, TypeError):
            continue
        if not isinstance(j, dict):
            continue
        got = j.get("cells") or j.get("answer") or j.get("indices") or j.get("tiles")
        if got:
            cells = got
            try:
                conf = float(j.get("confidence") or 0)
            except (TypeError, ValueError):
                conf = 0.0
            break
    if not cells:
        nums = re.findall(r"\b(\d{1,2})\b", text)
        cells = [int(n) for n in nums]
    if isinstance(cells, dict):
        cells = [k for k, v in cells.items() if v]
    out = sorted({int(c) for c in cells
                  if isinstance(c, (int, str)) and re.fullmatch(r"\d+", str(c).strip())})
    return [c for c in out if 0 <= c < limit], conf


def _label_grid(img_path, rows, cols, out_path):
    """Нарисовать номера ячеек на скриншоте сетки (VLM так точнее)."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return img_path
    try:
        img = Image.open(img_path).convert("RGB")
        w, h = img.size
        d = ImageDraw.Draw(img, "RGBA")
        cw, ch = w / cols, h / rows
        for r in range(rows):
            for c in range(cols):
                idx = r * cols + c
                x, y = int(c * cw), int(r * ch)
                d.rectangle([x + 1, y + 1, x + cw - 2, y + ch - 2],
                            outline=(0, 200, 0, 255), width=2)
                tx, ty = x + 4, y + 3
                d.rectangle([tx - 2, ty - 1, tx + 20, ty + 13], fill=(0, 0, 0, 190))
                d.text((tx, ty), str(idx), fill=(255, 255, 0, 255))
        img.save(out_path)
        return out_path
    except Exception:
        return img_path


def _wait_tiles_loaded(page, timeout=15):
    """Дождаться полной загрузки картинок сетки.

    reCAPTCHA отдаёт прогрессивный JPEG: если снять скриншот сразу, VLM видит
    полузагруженную «дизерингую» картинку и гадает. Ждём img.complete и
    naturalWidth>0 у всех ячеек.
    """
    bf = page.frame_locator(_BFRAME)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            ready = bf.locator(_TILES).evaluate_all(
                "els => els.every(td => { const i = td.querySelector('img');"
                " return i && i.complete && i.naturalWidth > 0; })")
        except Exception:
            ready = False
        if ready:
            time.sleep(0.6)
            return True
        time.sleep(0.4)
    return False


def _challenge_error(page):
    """Текст ошибки под сеткой («Please try again» и т.п.), если есть."""
    try:
        bf = page.frame_locator(_BFRAME)
        el = bf.locator('.rc-imageselect-incorrect-response')
        if el.count() and el.first.is_visible():
            return el.first.inner_text(timeout=1500).strip()
    except Exception:
        pass
    return ""


def _selected_count(page):
    """Сколько ячеек реально отмечено (клик зарегистрировался)."""
    try:
        return page.frame_locator(_BFRAME).locator(
            'td.rc-imageselect-tile.rc-imageselect-tileselected').count()
    except Exception:
        return -1


def _tile_srcs(page):
    """URL'ы оригинальных payload-картинок ячеек (300x300)."""
    try:
        return page.frame_locator(_BFRAME).locator(_TILES).evaluate_all(
            "els => els.map(td => { const i = td.querySelector('img'); return i ? i.src : ''; }).filter(Boolean)")
    except Exception:
        return []


def _fetch_tiles(srcs, proxy, out_dir, tag):
    """Скачать оригиналы ячеек через тот же прокси → список путей.

    Параллельно: последовательно это 9 RTT через резидентный прокси (3–4 с
    на раунд), а динамическая сетка столько не ждёт.
    """
    from concurrent.futures import ThreadPoolExecutor

    def one(item):
        i, src = item
        path = os.path.join(out_dir, f"{tag}-tile{i}.jpg")
        try:
            r = requests.get(src, timeout=30,
                             proxies={"http": proxy, "https": proxy} if proxy else None,
                             headers={"User-Agent": config.UA, "Referer": "https://www.recaptcha.net/"})
            if r.status_code == 200 and r.content:
                with open(path, "wb") as f:
                    f.write(r.content)
                return path
        except Exception:
            pass
        return ""

    if not srcs:
        return []
    with ThreadPoolExecutor(max_workers=min(9, len(srcs))) as pool:
        return list(pool.map(one, enumerate(srcs)))


def _dedupe_sprite(paths, rows, cols, out_dir, tag):
    """Если все «тайлы» — один и тот же файл, это спрайт всей сетки.

    Новый reCAPTCHA отдаёт на все ячейки один 300×300 (или 400×400) JPEG,
    а показывает их через CSS-кроп. Кормить такой спрайт классификатору или
    клеить из него монтаж бессмысленно — режем его сами по строкам/столбцам.
    """
    real = [p for p in paths if p]
    if len(real) != len(paths) or len(real) < 2:
        return paths
    try:
        import hashlib
        digests = {hashlib.md5(open(p, "rb").read()).hexdigest() for p in real}
    except OSError:
        return paths
    if len(digests) != 1:
        return paths
    try:
        from PIL import Image
        with Image.open(real[0]) as im:
            im = im.convert("RGB")
            w, h = im.size
            cw, ch = w / cols, h / rows
            out = []
            for r in range(rows):
                for c in range(cols):
                    box = (int(c * cw), int(r * ch),
                           int((c + 1) * cw), int((r + 1) * ch))
                    path = os.path.join(out_dir, f"{tag}-cell{r * cols + c}.jpg")
                    im.crop(box).save(path, quality=95)
                    out.append(path)
        _log(f"спрайт {w}x{h} → {len(out)} ячеек")
        return out
    except Exception as exc:
        _log(f"спрайт: {type(exc).__name__}: {str(exc)[:60]}")
        return paths


def _compose_montage(paths, rows, cols, out_path, size=300):
    """Склеить оригиналы в одну сетку с номерами (VLM видит качество 1:1).

    Скриншот bframe даёт 378px из 300px исходника (апскейл + сглаживание),
    из-за чего картинки «шумят». Оригиналы payload — 300×300, их и показываем.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    canvas = Image.new("RGB", (cols * size, rows * size), (255, 255, 255))
    d = ImageDraw.Draw(canvas)
    for idx, path in enumerate(paths[:rows * cols]):
        r, c = divmod(idx, cols)
        if path:
            try:
                im = Image.open(path).convert("RGB").resize((size, size), Image.LANCZOS)
            except Exception:
                im = Image.new("RGB", (size, size), (220, 220, 220))
        else:
            im = Image.new("RGB", (size, size), (220, 220, 220))
        canvas.paste(im, (c * size, r * size))
        d.rectangle([c * size + 1, r * size + 1,
                     (c + 1) * size - 2, (r + 1) * size - 2],
                    outline=(0, 200, 0), width=3)
        d.rectangle([c * size + 4, r * size + 4,
                     c * size + 34, r * size + 28], fill=(0, 0, 0))
        d.text((c * size + 10, r * size + 8), str(idx), fill=(255, 255, 0))
    canvas.save(out_path, quality=90)
    return out_path


def _tile_blank(path):
    """Тайл уже нажат или пустой: почти однотонная картинка.

    В динамической сетке нажатая ячейка подменяется белой заглушкой, но
    классификатор на ней всё равно выдаёт класс (0.84 на пустом!). Такие
    тайлы не выбираем — иначе кликаем в пустоту и жжём раунд.
    """
    try:
        from PIL import Image, ImageStat
        with Image.open(path) as im:
            small = im.convert("L").resize((32, 32))
        st = ImageStat.Stat(small)
        # именно белая заглушка, а не «однотонное небо»: у неба средняя
        # яркость ниже, а разброс всё же есть
        return st.mean[0] > 235 and st.stddev[0] < 10
    except Exception:
        return False


def _blank_cells(shot_path, rows, cols):
    """Индексы белых (уже нажатых) ячеек — по СКРИНШОТУ сетки.

    Считать по скачанным payload'ам бесполезно: после клика reCAPTCHA
    подменяет ячейку визуально, а `img.src` продолжает отдавать старую
    картинку. Скриншот показывает то, что реально видит человек.
    """
    try:
        from PIL import Image, ImageStat
        with Image.open(shot_path) as im:
            gray = im.convert("L")
        w, h = gray.size
        cw, ch = w / cols, h / rows
        out = set()
        for r in range(rows):
            for c in range(cols):
                box = (int(c * cw), int(r * ch),
                       max(int((c + 1) * cw), int(c * cw) + 1),
                       max(int((r + 1) * ch), int(r * ch) + 1))
                st = ImageStat.Stat(gray.crop(box).resize((32, 32)))
                if st.mean[0] > 235 and st.stddev[0] < 10:
                    out.add(r * cols + c)
        return out
    except Exception:
        return set()


def _click_tile(page, tds, idx):
    """Клик по ячейке: обычный → force → по координатам → JS-события.

    В динамической сетке DOM подменяется на ходу, поэтому Playwright может
    не дождаться «stable» и упасть по таймауту. force=True кликает без
    ожиданий; если элемент перекрыт оверлеем или вылез за вьюпорт — жмём
    мышью по центру его bounding box; последний шанс — синтетические события.
    """
    tile = tds.nth(idx)
    for kwargs in ({"timeout": 4000, "force": True}, {"timeout": 2500}):
        try:
            tile.click(**kwargs)
            return True
        except Exception:
            continue
    try:
        tile.scroll_into_view_if_needed(timeout=2000)
        box = tile.bounding_box()
        if box and box["width"] > 1 and box["height"] > 1:
            page.mouse.click(box["x"] + box["width"] / 2,
                             box["y"] + box["height"] / 2)
            return True
    except Exception:
        pass
    try:
        tile.evaluate(
            "el => { const t = el.querySelector('img') || el;"
            " for (const type of ['mousedown','mouseup','click'])"
            "   t.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true})); }")
        return True
    except Exception:
        return False


def _click_verify(bf):
    """Нажать VERIFY так же терпимо, как и тайлы."""
    for kwargs in ({"timeout": 4000, "force": True}, {"timeout": 3000}):
        try:
            bf.locator("#recaptcha-verify-button").click(**kwargs)
            return True
        except Exception:
            continue
    try:
        bf.locator("#recaptcha-verify-button").evaluate("el => el.click()")
        return True
    except Exception:
        return False


def _checked_cells(shot_path, rows, cols):
    """Индексы уже отмеченных ячеек — по синей галочке в левом верхнем углу.

    В варианте «если таких нет, нажмите Skip» нажатая ячейка не белеет, а
    получает синий бейдж (#1870e8), которого нет в payload-спрайте. Без этой
    проверки цикл кликает одни и те же тайлы, сетка не меняется, и раунд
    сгорает целиком.
    """
    try:
        from PIL import Image, ImageStat
        with Image.open(shot_path) as im:
            img = im.convert("RGB")
        w, h = img.size
        cw, ch = w / cols, h / rows
        out = set()
        for r in range(rows):
            for c in range(cols):
                x0, y0 = int(c * cw), int(r * ch)
                box = (x0, y0, x0 + max(int(cw * 0.32), 1), y0 + max(int(ch * 0.32), 1))
                n = 0
                for px in img.crop(box).getdata():
                    # строго цвет бейджа (#1870e8): «синее небо/куртка» дают
                    # тысячи пикселей по мягкому фильтру и ломают 3×3
                    if (abs(px[0] - 24) < 40 and abs(px[1] - 112) < 45
                            and abs(px[2] - 232) < 35):
                        n += 1
                if n > 60:
                    out.add(r * cols + c)
        return out
    except Exception:
        return set()


def _done_cells(shot_path, rows, cols):
    """Ячейки, с которыми уже разобрались: белая заглушка или галочка."""
    return _blank_cells(shot_path, rows, cols) | _checked_cells(shot_path, rows, cols)


def _img_done(img):
    """Ячейка уже обработана: белая заглушка или синий бейдж в углу."""
    try:
        from PIL import ImageStat
        rgb = img.convert("RGB")
        w, h = rgb.size
        box = (0, 0, max(int(w * 0.32), 1), max(int(h * 0.32), 1))
        blue = sum(1 for px in rgb.crop(box).getdata()
                   if abs(px[0] - 24) < 40 and abs(px[1] - 112) < 45
                   and abs(px[2] - 232) < 35)
        if blue > max(30, int(w * h * 0.003)):
            return True
        st = ImageStat.Stat(rgb.convert("L").resize((32, 32)))
        return st.mean[0] > 235 and st.stddev[0] < 10
    except Exception:
        return False


def _tile_sig(tds, idx):
    """Отпечаток картинки ячейки (для проверки, что клик дошёл)."""
    try:
        return hashlib.md5(tds.nth(idx).screenshot(timeout=3000)).hexdigest()
    except Exception:
        return None


def _click_cells(page, bf, shot, rows, cols, cells):
    """Клики по одному с проверкой отклика каждой ячейки.

    Проверять «побелела ли ячейка» нельзя: в динамической 3×3 нажатый тайл
    подменяется НОВОЙ картинкой (белым он становится только когда сетка
    исчерпана), в 4×4 — получает галочку. Поэтому сравниваем отпечаток
    картинки до и после: изменился — клик дошёл, нет — жмём ещё раз.

    reCAPTCHA глотает нажатия пачкой: после клика сетка перерисовывается, и
    следующие клики бьют по уже подменённым узлам. Поэтому по одному.
    """
    tds = bf.locator(_TILES)
    ok = 0
    for idx in cells:
        before = _tile_sig(tds, idx)
        done = False
        for attempt in range(1, 4):
            if not _click_tile(page, tds, idx):
                time.sleep(0.25)
                continue
            time.sleep(0.45)
            after = _tile_sig(tds, idx)
            if not before or not after or after != before:
                done = True
                break
            _log(f"ячейка {idx}: клик не сработал, попытка {attempt}/3")
        if done:
            ok += 1
        else:
            _log(f"ячейка {idx} не отреагировала")
    return ok


def _grid_shape(page):
    """Число строк/столбцов видимой сетки (3x3 или 4x4) из bframe."""
    return page.frame_locator(_BFRAME).locator(_TABLE).first.evaluate(
        """(t) => {
          const rows = t.querySelectorAll('tr').length || 3;
          const first = t.querySelector('tr');
          const cols = first ? first.querySelectorAll('td').length : 3;
          return [rows, cols];
        }""")


def solve_grid_challenge(page, rounds=8, proxy=None):
    """Решить видимый челлендж-сетку. True — VERIFY нажат.

    4x4-«динамическая» сетка заменяет нажатые ячейки новыми — поэтому
    цикл: VLM → клики → подождать замену → снова, пока VLM не скажет
    «больше нет» → VERIFY. Для 3x3 хватает одного прохода + VERIFY.
    """
    bf = page.frame_locator(_BFRAME)
    try:
        bf.locator(_TABLE).first.wait_for(state="visible", timeout=15000)
    except Exception as exc:
        _log(f"сетка не появилась: {str(exc)[:80]}")
        return False

    out_dir = os.path.join(config.DATA_DIR, "captcha")
    os.makedirs(out_dir, exist_ok=True)
    for rnd in range(1, rounds + 1):
        try:
            question = bf.locator(_INSTR).first.inner_text(timeout=5000).strip()
        except Exception:
            question = "Select all images matching the instruction."
        question = re.sub(r"\s+", " ", question)
        try:
            rows, cols = _grid_shape(page)
        except Exception:
            rows, cols = 3, 3
        total = rows * cols
        _wait_tiles_loaded(page)
        tag = f"grid-{rows}x{cols}-{rnd}"
        shot = os.path.join(out_dir, tag + ".png")
        try:
            bf.locator(_TABLE).first.screenshot(path=shot, timeout=15000)
        except Exception as exc:
            _log(f"скриншот сетки: {str(exc)[:80]}")
        srcs = _tile_srcs(page)
        paths = _dedupe_sprite(_fetch_tiles(srcs, proxy, out_dir, tag),
                               rows, cols, out_dir, tag)

        # Динамическая сетка («Click verify once there are none left», 4×4)
        # подменяет нажатые ячейки и сама обновляется по таймеру. Раунд в
        # 15 с (VLM) не переживает: пока думаешь, сетка уже другая и клики
        # падают по таймауту. Поэтому здесь — только быстрый классификатор.
        dynamic = "none left" in question.lower() or total > 9

        # grid_solver: classifier (0.2 с, только 14 обученных классов) |
        # vlm (15 с, «понимает» любую инструкцию) | both (объединение, самое
        # полное но самое медленное) | auto = classifier, а если он не нашёл
        # ничего — vlm.
        mode = (config.get("grid_solver") or "auto").lower()
        if dynamic and mode == "auto":
            mode = "classifier"

        # Галочка — однозначный признак «уже нажато» (статическая 4×4).
        # А белый тайл в динамической 3×3 — временная заглушка на время
        # подгрузки новой картинки; поштучно его исключать нельзя, иначе
        # пропускаем вернувшийся объект и получаем «Please try again».
        # Белые считаем концом сетки, только когда белые ВСЕ.
        checked = _checked_cells(shot, rows, cols)
        blanks = _blank_cells(shot, rows, cols)
        exhausted = len(checked | blanks) >= total
        done = (checked | blanks) if exhausted else checked
        usable = [i for i in range(total) if i not in done]
        if done:
            _log(f"уже отмечено: {len(done)}"
                 + (" (сетка исчерпана)" if exhausted else ""))
        cells, conf, src = [], 0.0, ""
        probs = {}
        if mode in ("auto", "classifier", "both") and paths and all(paths):
            got, cconf, label, probs = classify_tiles(paths, question)
            if got:
                kept = [c for c in got if c in usable]
                skipped = len(got) - len(kept)
                if skipped:
                    _log(f"отброшено уже отмеченных: {skipped}")
                if kept:
                    # уверенные берём сразу, сомнительные отдаём VLM: на
                    # кропах из спрайта (100–112 px) классификатор путается
                    # на мелких объектах, а лишний клик = провал раунда
                    sure_p = float(config.get("grid_sure_p") or 0.85)
                    cells = [c for c in kept if probs.get(c, 1.0) >= sure_p]
                    uncertain = [c for c in kept if c not in cells]
                    conf, src = cconf, f"cls:{label}"
                    if uncertain:
                        _log(f"сомнительные: {uncertain} "
                             f"(p={[round(probs.get(c, 0), 2) for c in uncertain]})")
                else:
                    uncertain = []
            else:
                uncertain = []
        else:
            uncertain = []

        asked_vlm = False
        need_vlm = (mode in ("vlm", "both")
                    or (mode == "auto" and not cells)
                    or (uncertain and mode in ("auto", "classifier", "both")))
        if need_vlm:
            labeled = _compose_montage(paths, rows, cols,
                                       os.path.join(out_dir, tag + "-n.png"))
            if not labeled:
                labeled = _label_grid(shot, rows, cols, os.path.join(out_dir, tag + "-n2.png"))
            prompt = GRID_PROMPT.format(rows=rows, cols=cols, question=question)
            text = ask_vlm(labeled, prompt)
            asked_vlm = True
            vcells, vconf = parse_cells(text, total)
            if uncertain:
                # по сомнительным решает VLM: берём только то, что он подтвердил
                confirmed = [c for c in uncertain if c in vcells]
                if confirmed:
                    cells = sorted(set(cells) | set(confirmed))
                    conf = max(conf, vconf)
                    src += "+vlm"
                    _log(f"VLM подтвердил: {confirmed}")
            elif mode == "both" and cells:
                cells = sorted(set(cells) | set(vcells))
                conf = max(conf, vconf)
                src += "+vlm"
            elif vcells:
                cells, conf, src = vcells, vconf, "vlm"
            elif not cells:
                src = "vlm"
        if exhausted:
            cells = []          # сетка кончилась — только VERIFY
        # Лимит кликов за раунд: пачка нажатий ломает динамическую сетку —
        # она перерисовывается между кликами, и часть уходит в пустоту.
        limit = int(config.get("grid_max_per_round") or 3)
        if dynamic and len(cells) > limit:
            cells = sorted(cells, key=lambda c: -probs.get(c, 1.0))[:limit]
            _log(f"ограничение раунда: жмём {len(cells)} из {limit}")
        _log(f"сетка {rows}x{cols}, «{question[:60]}» → {cells} "
             f"(conf {conf:.2f}, {src or 'пусто'}{' , vlm' if asked_vlm and mode == 'both' else ''})")
        if not cells:
            break
        clicked = _click_cells(page, bf, shot, rows, cols, cells)
        # В динамической сетке «отмечено» всегда 0: нажатый тайл сразу
        # подменяется новым. Считаем только статическую.
        if not dynamic:
            sel = _selected_count(page)
            if sel >= 0:
                _log(f"отмечено ячеек: {sel} (кликали {len(cells)})")
        else:
            _log(f"кликов: {clicked}/{len(cells)}")
        if not dynamic:
            break
        # Пауза на перерисовку: сетка подменяет тайлы асинхронно, и
        # следующий скриншот/классификация по «полупустой» сетке врёт.
        time.sleep(float(config.get("grid_round_pause") or 1.8))
    try:
        if not _click_verify(bf):
            _log("VERIFY: не нажался")
        time.sleep(1.5)
        err = _challenge_error(page)
        _log("VERIFY нажат" + (f", ответ: {err[:80]}" if err else ""))
        return True
    except Exception as exc:
        _log(f"VERIFY: {str(exc)[:80]}")
        return False


def solve_in_page(page, sitekey, timeout=240, challenge_rounds=8, proxy=None):
    """Минт reCAPTCHA v2 в контексте страницы. Токен или None.

    page — playwright/camoufox Page уже на домене сервиса (иначе sitekey не
    примется: Google сверяет домен). Каждый челлендж решается VLM-солвером;
    при провале рекапча даёт новый челлендж — пробуем `challenge_rounds` раз.
    """
    if not sitekey:
        return None
    try:
        page.evaluate(_LOAD_JS)
    except Exception as exc:
        _log(f"api.js не поднялся: {str(exc)[:100]}")
        return None
    if str(page.evaluate(_RENDER_JS, sitekey)).startswith("ERR"):
        # повторная отрисовка того же виджета — ок, ошибку мог дать первый вызов
        pass
    try:
        fl = page.frame_locator('iframe[src*="api2/anchor"]')
        anchor = fl.locator("#recaptcha-anchor")
        anchor.wait_for(state="visible", timeout=20000)
        time.sleep(0.8)
        anchor.click(timeout=8000)
    except Exception as exc:
        _log(f"клик по чекбоксу: {str(exc)[:80]}")

    deadline = time.time() + timeout
    solved_rounds = 0
    while time.time() < deadline:
        try:
            tok = page.evaluate(_TOKEN_JS)
        except Exception:
            tok = ""
        if tok:
            _log(f"токен получен ({len(tok)} симв.)")
            return tok
        # есть ли челлендж, который надо решать
        try:
            challenge = page.frame_locator(_BFRAME).locator(_TABLE).first.count() > 0
        except Exception:
            challenge = False
        if challenge and solved_rounds < challenge_rounds:
            solved_rounds += 1
            _log(f"челлендж {solved_rounds}/{challenge_rounds}")
            solve_grid_challenge(page, proxy=proxy)
            time.sleep(3)
            continue
        time.sleep(2)
    return None


def solve_2captcha(sitekey, pageurl=None, timeout=180):
    """Fallback: RecaptchaV2TaskProxyless (так работал чужой фарм)."""
    key = (config.get("twocaptcha_key") or "").strip()
    if not key or not sitekey:
        return None
    siteurl = pageurl or (config.get("base_url") or "") + "/"
    try:
        r = requests.post(TWOCAPTCHA_CREATE, json={
            "clientKey": key,
            "task": {"type": "RecaptchaV2TaskProxyless",
                     "websiteURL": siteurl, "websiteKey": sitekey},
        }, timeout=30).json()
    except Exception as exc:
        _log(f"2captcha create: {str(exc)[:100]}")
        return None
    if r.get("errorId"):
        _log(f"2captcha create err: {r.get('errorCode')} {r.get('errorDescription')}")
        return None
    tid = r.get("taskId")
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(4)
        try:
            g = requests.post(TWOCAPTCHA_RESULT, json={"clientKey": key, "taskId": tid},
                              timeout=25).json()
        except Exception:
            continue
        if g.get("status") == "ready":
            tok = (g.get("solution") or {}).get("gRecaptchaResponse")
            _log(f"2captcha: токен получен ({len(tok or '')} симв.)")
            return tok
        if g.get("errorId"):
            _log(f"2captcha poll err: {g.get('errorCode')}")
            return None
    return None


def solve(page, sitekey, pageurl=None, proxy=None):
    """auto: браузер+VLM ($0) → 2captcha. Возвращает токен или None."""
    mode = (config.get("captcha_solver") or "auto").lower()
    if mode in ("vlm", "browser", "auto"):
        tok = solve_in_page(page, sitekey, proxy=proxy)
        if tok:
            return tok
        if mode in ("vlm", "browser"):
            return None
    return solve_2captcha(sitekey, pageurl=pageurl)


def info():
    """Для панели: чем решаем и что настроено."""
    mode = (config.get("captcha_solver") or "auto").lower()
    base, key, model = vlm_cfg()
    gbase, gkey = grid_cfg()
    has_2c = bool((config.get("twocaptcha_key") or "").strip())
    label = {"vlm": "свой браузер + VLM ($0)",
             "browser": "свой браузер + VLM ($0)",
             "2captcha": "2captcha",
             "auto": "свой браузер + VLM ($0) → 2captcha"}.get(mode, mode)
    return {"type": "reCAPTCHA v2", "solver": label, "vlm": model, "vlm_ready": bool(base and key and model),
            "grid_solver": (config.get("grid_solver") or "auto"),
            "grid_classifier": bool(gbase and gkey),
            "sitekey": config.get("recaptcha_sitekey") or "", "twocaptcha": has_2c}