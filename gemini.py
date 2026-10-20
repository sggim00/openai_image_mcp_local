"""Google Gemini backend for openai_image_mcp_local (REST, Interactions API).

- Images: Nano Banana models (default gemini-nano-banana-2.1, Pro = gemini-3-pro-image)
- Video:  Gemini Omni Flash (default gemini-omni-1.1-flash), multi-turn editing / extension
          through previous_interaction_id

Plain HTTPS through httpx (already installed with the openai package), so there is no
extra SDK to keep in step with the API. Settings come from the same .env as server.py.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import time
from pathlib import Path

import httpx

API = os.environ.get("GEMINI_API_BASE", "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
IMAGE_MODEL = os.environ.get("GEMINI_IMAGE_MODEL", "gemini-nano-banana-2.1")
IMAGE_MODEL_PRO = os.environ.get("GEMINI_IMAGE_MODEL_PRO", "gemini-3-pro-image")
VIDEO_MODEL = os.environ.get("GEMINI_VIDEO_MODEL", "gemini-omni-1.1-flash")
IMAGE_TIMEOUT = float(os.environ.get("GEMINI_TIMEOUT", "240"))
VIDEO_TIMEOUT = float(os.environ.get("GEMINI_VIDEO_TIMEOUT", "900"))
MAX_REFS = 14
VIDEO_MAX_IMAGES = 3

# OpenAI-style size -> Gemini aspect ratio. A ratio string ("16:9") is passed straight through.
SIZE_TO_ASPECT = {"1024x1024": "1:1", "1536x1024": "3:2", "1024x1536": "2:3"}
IMAGE_ASPECTS = {"1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9",
                 "1:4", "4:1", "1:8", "8:1"}
# quality -> output resolution (Gemini bills by resolution, not by "quality")
QUALITY_TO_SIZE = {"low": "1K", "medium": "1K", "auto": "1K", "high": "2K"}


class GeminiError(Exception):
    pass


# --------------------------------------------------------------------------
# API key: GEMINI_API_KEY, else the file named by GEMINI_API_KEY_FILE
# --------------------------------------------------------------------------
def api_key() -> tuple[str | None, str]:
    for env in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        key = os.environ.get(env, "").strip()
        if key:
            return key, env
    file = os.environ.get("GEMINI_API_KEY_FILE", "").strip().strip('"').strip("'")
    if not file:
        return None, "neither GEMINI_API_KEY nor GEMINI_API_KEY_FILE is set"
    p = Path(os.path.expandvars(os.path.expanduser(file)))
    if not p.is_file():
        return None, f"GEMINI_API_KEY_FILE not found: {p}"
    text = p.read_text(encoding="utf-8-sig", errors="ignore")
    m = re.search(r"AIza[0-9A-Za-z_\-]{30,}", text)
    if m:
        return m.group(0), str(p)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if lines:
        line = lines[0].split("=", 1)[-1].strip().strip('"').strip("'")
        if line:
            return line, str(p)
    return None, f"no key found in {p}"


def available() -> bool:
    return api_key()[0] is not None


def _headers() -> dict:
    key, source = api_key()
    if not key:
        raise GeminiError(f"No Gemini API key: {source}")
    return {"x-goog-api-key": key, "Content-Type": "application/json"}


# --------------------------------------------------------------------------
# Request / response helpers
# --------------------------------------------------------------------------
def aspect_for(size: str | None) -> str | None:
    """'1536x1024' -> '3:2', '16:9' -> '16:9', 'auto'/None -> None (model decides / keeps input ratio)."""
    if not size or size == "auto":
        return None
    if size in SIZE_TO_ASPECT:
        return SIZE_TO_ASPECT[size]
    if re.match(r"^\d+:\d+$", size):
        return size
    m = re.match(r"^(\d+)x(\d+)$", size)
    if m:  # nearest supported ratio
        w, h = int(m.group(1)), int(m.group(2))
        r = w / h
        return min(IMAGE_ASPECTS, key=lambda a: abs(int(a.split(":")[0]) / int(a.split(":")[1]) - r))
    return None


def _input(prompt: str, images: list[tuple[str, bytes, str]]) -> list[dict] | str:
    if not images:
        return prompt
    items: list[dict] = [{"type": "image", "data": base64.b64encode(raw).decode("ascii"), "mime_type": mime}
                         for _name, raw, mime in images]
    items.append({"type": "text", "text": prompt})
    return items


def _media_items(obj, kind: str) -> list[dict]:
    """Every {"type": kind, "data"|"uri": ...} block in an interaction, in order.

    Searches the whole response so it works with the raw REST shape (steps[].content[])
    and with SDK-style convenience fields (output_image / output_video)."""
    found: list[dict] = []
    if not isinstance(obj, dict):
        return found

    def walk(o):
        if isinstance(o, dict):
            if o.get("type") in ("user_input", "thought"):
                return  # our own input images come back in user_input steps
            if o.get("type") == kind and (o.get("data") or o.get("uri")):
                found.append(o)
                return
            for v in o.values():
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    for step in obj.get("steps") or []:
        if isinstance(step, dict) and step.get("type") == "model_output":
            walk(step.get("content") or [])
    if not found:
        for k in (f"output_{kind}", f"output{kind.capitalize()}"):
            v = obj.get(k)
            if isinstance(v, dict) and (v.get("data") or v.get("uri")):
                found.append(v)
    if not found:
        walk({k: v for k, v in obj.items() if k != "input"})
    return found


def _texts(obj) -> str:
    out: list[str] = []
    for step in (obj.get("steps") or []) if isinstance(obj, dict) else []:
        if step.get("type") != "model_output":
            continue
        for c in step.get("content") or []:
            if c.get("type") == "text" and c.get("text"):
                out.append(c["text"])
    return " ".join(out).strip()


def _error_text(r: httpx.Response) -> str:
    try:
        j = r.json()
        e = j.get("error", j)
        msg = e.get("message") if isinstance(e, dict) else str(e)
    except Exception:
        msg = r.text[:500]
    return f"HTTP {r.status_code}: {msg}"


async def _create(body: dict, timeout: float) -> dict:
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=30)) as c:
        r = await c.post(f"{API}/interactions", headers=_headers(), json=body)
        if r.status_code >= 400:
            raise GeminiError(_error_text(r))
        data = r.json()
        # Synchronous by default; if the service ever answers before it is finished, poll.
        deadline = time.time() + timeout
        while str(data.get("status", "completed")).lower() in ("in_progress", "queued", "pending", "running") \
                and data.get("id") and time.time() < deadline:
            await asyncio.sleep(5)
            g = await c.get(f"{API}/interactions/{data['id']}", headers=_headers())
            if g.status_code >= 400:
                raise GeminiError(_error_text(g))
            data = g.json()
        status = str(data.get("status", "completed")).lower()
        if status in ("failed", "cancelled", "incomplete"):
            raise GeminiError(f"interaction {status}: {data.get('error') or _texts(data) or 'no details'}")
        return data


async def _fetch_uri(uri: str, timeout: float) -> bytes:
    """Download a Files API result, waiting until the file is ACTIVE."""
    m = re.search(r"files/([^/:?]+)", uri)
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=30), follow_redirects=True) as c:
        if m:
            deadline = time.time() + timeout
            while time.time() < deadline:
                g = await c.get(f"{API}/files/{m.group(1)}", headers=_headers())
                if g.status_code >= 400:
                    break  # fall through and try the download link itself
                state = str(g.json().get("state", "ACTIVE")).upper()
                if state == "ACTIVE":
                    break
                if state == "FAILED":
                    raise GeminiError("the generated file failed to process")
                await asyncio.sleep(3)
            url = f"{API}/files/{m.group(1)}:download?alt=media"
        else:
            url = uri
        r = await c.get(url, headers=_headers())
        if r.status_code >= 400:
            raise GeminiError("download failed: " + _error_text(r))
        return r.content


async def _media_bytes(item: dict, timeout: float) -> bytes:
    if item.get("data"):
        return base64.b64decode(item["data"])
    return await _fetch_uri(item["uri"], timeout)


# --------------------------------------------------------------------------
# Public calls
# --------------------------------------------------------------------------
async def render_image(prompt: str, refs: list[tuple[str, bytes, str]], size: str | None, quality: str,
                       output_format: str, model: str | None = None) -> tuple[bytes, str]:
    """Generate or edit an image. Returns (bytes, model used)."""
    if len(refs) > MAX_REFS:
        raise GeminiError(f"Gemini accepts at most {MAX_REFS} reference images (got {len(refs)}).")
    model = model or IMAGE_MODEL
    fmt = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/png"}.get(output_format, "image/png")
    rf: dict = {"type": "image", "mime_type": fmt, "image_size": QUALITY_TO_SIZE.get(quality, "1K")}
    aspect = aspect_for(size)
    if aspect:
        rf["aspect_ratio"] = aspect
    body: dict = {"model": model, "input": _input(prompt, refs), "response_format": rf}
    if quality == "high" and model == IMAGE_MODEL:
        body["generation_config"] = {"thinking_level": "high"}
    data = await _create(body, IMAGE_TIMEOUT)
    items = _media_items(data, "image")
    if not items:
        why = _texts(data)
        raise GeminiError("no image in the response" + (f" — model said: {why[:400]}" if why else
                                                          " (possibly blocked by the safety filter)"))
    return await _media_bytes(items[-1], IMAGE_TIMEOUT), model


async def render_video(prompt: str, images: list[tuple[str, bytes, str]], aspect_ratio: str, resolution: str,
                       previous_interaction_id: str | None = None, task: str | None = None) -> tuple[bytes, str]:
    """Generate (or continue/edit) a short video. Returns (mp4 bytes, interaction id)."""
    if len(images) > VIDEO_MAX_IMAGES:
        raise GeminiError(f"Video accepts at most {VIDEO_MAX_IMAGES} images (got {len(images)}).")
    rf: dict = {"type": "video", "aspect_ratio": aspect_ratio, "resolution": resolution}
    if resolution in ("1080p", "4k"):
        rf["delivery"] = "uri"  # inline base64 is meant for small files
    body: dict = {"model": VIDEO_MODEL, "input": _input(prompt, images), "response_format": rf}
    if previous_interaction_id:
        body["previous_interaction_id"] = previous_interaction_id
    if task:
        body["generation_config"] = {"video_config": {"task": task}}
    data = await _create(body, VIDEO_TIMEOUT)
    items = _media_items(data, "video")
    if not items:
        why = _texts(data)
        raise GeminiError("no video in the response" + (f" — model said: {why[:400]}" if why else
                                                          " (possibly blocked by the safety filter)"))
    return await _media_bytes(items[-1], VIDEO_TIMEOUT), str(data.get("id") or "")
