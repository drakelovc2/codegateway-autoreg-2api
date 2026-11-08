# -*- coding: utf-8 -*-
"""Vision-капча и аудио-капча как шаги флоу, а не разовые скрипты.

Два независимых солвера, оба необязательные — включаются конфигом:

    from . import vision
    # сетка «выбери все светофоры» / «нажми на фигуру»
    cells = vision.solve_grid_captcha(page, prompt="Select all traffic lights")
    # → [3, 9]  (индексы ячеек в сетке 3×3)

    # аудио-капча reCAPTCHA: скачать mp3 → STT → текст
    text = vision.solve_audio_captcha(audio_url)

Провайдеры — OpenAI-совместимый VLM и любой Whisper-совместимый STT.
Ключи берутся из config.json (`vlm_*`, `stt_*`) или env `VLM_API_BASE` /
`VLM_API_KEY` / `VLM_MODEL` / `STT_API_BASE` / `STT_API_KEY` / `STT_MODEL`.

Зачем отдельный модуль. У нас VLM уже дважды писался точечно под слайдер
(`zai/slider_solver.py`). Повторять это в каждом реггере не нужно: сетка
ячеек — общая задача, различается только текст промпта.
"""
import base64
import json
import os
import re
import urllib.error
import urllib.request

from . import config

GRID_PROMPT = (
    "This is a captcha grid. {question}\n"
    "Number the cells left-to-right, top-to-bottom starting at 0. "
    "Answer ONLY JSON: {{\"cells\": [<indices>], \"confidence\": <0-1>}}"
)


def _cfg(name, env, default=None):
    return os.environ.get(env) or config.get(name) or default


def _vlm_cfg():
    return (_cfg("vlm_api_base", "VLM_API_BASE"),
            _cfg("vlm_api_key", "VLM_API_KEY"),
            _cfg("vlm_model", "VLM_MODEL"))


def available():
    base, key, model = _vlm_cfg()
    return bool(base and key and model)


def _img_b64(path_or_bytes):
    if isinstance(path_or_bytes, (bytes, bytearray)):
        return base64.b64encode(bytes(path_or_bytes)).decode()
    with open(path_or_bytes, "rb") as f:
        return base64.b64encode(f.read()).decode()


def _payload(image, prompt, max_tokens, stream):
    return json.dumps({
        "model": _vlm_cfg()[2],
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64," + _img_b64(image)}},
        ]}],
        "max_tokens": max_tokens,
        "temperature": 0,
        **({"stream": True} if stream else {}),
    }).encode()


def _read_sse(resp):
    """Склеить дельты SSE в текст. reasoning_content тоже берём.

    Нужно потому, что не-стриминговый ответ на CPU-VLM идёт 10–20 с, а
    приватный порт Instabox обрывает молчащий запрос примерно на 20-й
    секунде (502). Стрим отдаёт заголовки сразу и живёт сколько нужно.
    """
    out = []
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if chunk == "[DONE]":
            break
        try:
            data = json.loads(chunk)
        except ValueError:
            continue
        for choice in data.get("choices") or []:
            delta = choice.get("delta") or choice.get("message") or {}
            for field in ("content", "reasoning_content"):
                if delta.get(field):
                    out.append(delta[field])
    return "".join(out)


def ask_vlm(image, prompt, max_tokens=600, timeout=180, stream=True):
    """Один запрос к VLM. Возвращает текст ответа или бросает RuntimeError."""
    base, key, model = _vlm_cfg()
    if not (base and key and model):
        raise RuntimeError("VLM не настроен: config.vlm_api_base/vlm_api_key/vlm_model")

    url = base.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json",
               "Authorization": "Bearer " + key}

    def call(with_stream):
        req = urllib.request.Request(
            url, data=_payload(image, prompt, max_tokens, with_stream), headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if with_stream:
                return _read_sse(r)
            data = json.load(r)
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        return (msg.get("content") or "") + "\n" + (msg.get("reasoning_content") or "")

    if stream:
        try:
            return call(True)
        except urllib.error.HTTPError as exc:
            body = exc.read()[:200]
            # провайдер без SSE — дожимаем обычным запросом
            if exc.code not in (400, 404, 405, 501):
                raise RuntimeError(f"VLM HTTP {exc.code}: {body!r}") from exc
    try:
        return call(False)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"VLM HTTP {exc.code}: {exc.read()[:200]!r}") from exc


_CELL_KEYS = ("cells", "answer", "tiles", "selected", "indices")


def _json_objects(text):
    """Все сбалансированные {...} из текста, которые парсятся как JSON."""
    for start, ch in enumerate(text):
        if ch != "{":
            continue
        depth = 0
        for end in range(start, len(text)):
            if text[end] == "{":
                depth += 1
            elif text[end] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        yield json.loads(text[start:end + 1])
                    except ValueError:
                        pass
                    break


def _ints(value):
    """Привести что угодно к списку индексов: [0,4,6], "0,4,6", {"1": true}."""
    if isinstance(value, dict):
        value = [k for k, v in value.items() if v]
    out = []
    for item in (value if isinstance(value, (list, tuple)) else [value]):
        if isinstance(item, bool):
            continue
        if isinstance(item, (int, float)):
            out.append(int(item))
        elif isinstance(item, str):
            found = re.findall(r"\d+", item)
            if found:
                out.append(int(found[0]))
    return out


def _parse_cells(text):
    """Достать список индексов из вольного ответа модели.

    Форматы отличаются: `{"cells": [...]}`, `{"answer": [...]}`, голая
    строка `answer: [0,4,6]` или просто `[0,4,6]`. Модель, которая
    ответила правильно, но не тем ключом, не должна давать пустой клик.
    """
    if not text:
        return None, 0.0

    for obj in _json_objects(text):
        for key in _CELL_KEYS:
            if key in obj:
                nums = _ints(obj[key])
                if nums:
                    try:
                        conf = float(obj.get("confidence") or 0.6)
                    except (TypeError, ValueError):
                        conf = 0.6
                    return nums, conf

    # ключ без валидного JSON вокруг: "cells": [0, 4, 6]
    m = re.search(r'"?(?:%s)"?\s*:\s*\[([^\]]*)\]' % "|".join(_CELL_KEYS), text)
    if m:
        nums = [int(n) for n in re.findall(r"\d+", m.group(1))]
        if nums:
            return nums, 0.5

    # совсем без ключа — единственный JSON-массив чисел в ответе
    for m in re.finditer(r"\[([\d\s,]+)\]", text):
        nums = [int(n) for n in re.findall(r"\d+", m.group(1))]
        if nums:
            return nums, 0.4
    return None, 0.0


def solve_grid_captcha(page, question="Select all images matching the prompt.",
                       cells=9, out_path="data/captcha-grid.png"):
    """Снять сетку со страницы и вернуть индексы ячеек для клика.

    `cells` — ожидаемое число ячеек (3×3 = 9, 4×4 = 16). Модель может
    вернуть индексы вне диапазона — они отбрасываются.
    """
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    page.screenshot(path=out_path, full_page=False)
    prompt = GRID_PROMPT.format(question=question)
    text = ask_vlm(out_path, prompt)
    idx, conf = _parse_cells(text)
    if not idx:
        return [], 0.0
    clean = sorted({i for i in idx if 0 <= i < cells})
    return clean, conf


def solve_image_choice(page, question, options=4, out_path="data/captcha-choice.png"):
    """Капча-выбор из N картинок: вернуть индексы картинок, которые просят."""
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    page.screenshot(path=out_path, full_page=False)
    prompt = (
        f"{question}\nThere are {options} image options, numbered left-to-right "
        f"from 0. Answer ONLY JSON: {{\"cells\": [<indices>], \"confidence\": <0-1>}}")
    text = ask_vlm(out_path, prompt)
    idx, conf = _parse_cells(text)
    return sorted({i for i in (idx or []) if 0 <= i < options}), conf


# ── аудио-капча ───────────────────────────────────────────────────────────

def _stt_cfg():
    return (_cfg("stt_api_base", "STT_API_BASE"),
            _cfg("stt_api_key", "STT_API_KEY"),
            _cfg("stt_model", "STT_MODEL", "whisper-1"))


def transcribe(audio_path, language=None, timeout=90):
    """Аудио → текст через Whisper-совместимый эндпоинт (multipart)."""
    base, key, model = _stt_cfg()
    if not (base and key):
        raise RuntimeError("STT не настроен: config.stt_api_base/stt_api_key")

    with open(audio_path, "rb") as f:
        audio = f.read()

    boundary = "----autoreg" + base64.urlsafe_b64encode(os.urandom(9)).decode().rstrip("=")
    parts = []

    def field(name, value):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                     f'name="{name}"\r\n\r\n{value}\r\n'.encode())

    field("model", model)
    if language:
        field("language", language)
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
        f"filename=\"audio.mp3\"\r\nContent-Type: audio/mpeg\r\n\r\n".encode()
        + audio + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)

    req = urllib.request.Request(
        base.rstrip("/") + "/audio/transcriptions", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"STT HTTP {exc.code}: {exc.read()[:200]!r}") from exc
    return (data.get("text") or "").strip()


def solve_audio_captcha(page, download_to="data/captcha-audio.mp3",
                        language=None):
    """Нажать кнопку аудио-капчи, скачать mp3, распознать.

    Возвращает распознанный текст. Ошибка — если кнопки/URL нет или STT
    не настроен: молча возвращать пустую строку нельзя, иначе реггер
    отправит пустое поле и получит невнятный отказ.
    """
    os.makedirs(os.path.dirname(download_to) or ".", exist_ok=True)

    # reCAPTCHA: кнопка переключения на аудио, затем ссылка на mp3
    for sel in ('#recaptcha-audio-button', 'button[title*="audio" i]',
                '[aria-label*="audio" i]'):
        try:
            el = page.locator(sel).first
            if el.count() and el.is_visible():
                el.click(timeout=5000)
                page.wait_for_timeout(1500)
                break
        except Exception:
            continue

    url = None
    for sel in ('audio#audio-source', 'audio source', 'a[href$=".mp3"]'):
        try:
            el = page.locator(sel).first
            if el.count():
                url = el.get_attribute("src") or el.get_attribute("href")
                if url:
                    break
                try:
                    url = el.evaluate("e => e.src || e.currentSrc")
                except Exception:
                    pass
                if url:
                    break
        except Exception:
            continue

    if not url:
        raise RuntimeError("аудио-капча: не нашёл ссылку на mp3")

    with urllib.request.urlopen(url, timeout=60) as r, open(download_to, "wb") as f:
        f.write(r.read())

    text = transcribe(download_to, language=language)
    if not text:
        raise RuntimeError("аудио-капча: STT вернул пустой текст")
    return text


def step(page, cfg):
    """Шаг флоу: единая точка для vision-капчи в реггере.

    cfg = {"kind": "grid"|"choice"|"audio", "question": ..., "cells": 9,
           "selector": "..."} — что нашлось в recon.
    Возвращает dict с результатом, чтобы реггер сам решил, как применить.
    """
    kind = (cfg.get("kind") or "grid").lower()
    if kind == "audio":
        return {"text": solve_audio_captcha(page, language=cfg.get("language"))}
    if kind == "choice":
        idx, conf = solve_image_choice(page, cfg.get("question") or "Choose the matching image",
                                       options=int(cfg.get("options") or 4))
        return {"indices": idx, "confidence": conf}
    idx, conf = solve_grid_captcha(page, cfg.get("question") or "Select the matching cells",
                                   cells=int(cfg.get("cells") or 9))
    return {"indices": idx, "confidence": conf}