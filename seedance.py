"""ByteDance Seedance backend for openai_image_mcp_local (BytePlus ModelArk, REST).

Video only:  text -> video, image -> video (first frame / first + last frame),
             multi-image references, reference videos (edit / extend), with generated sound.

    POST {API}/contents/generations/tasks        -> {"id": "cgt-..."}
    GET  {API}/contents/generations/tasks/{id}   -> {"status": "queued|running|succeeded|failed|cancelled|expired",
                                                      "content": {"video_url": ..., "last_frame_url": ...}}

The result URLs expire after about 24 hours, so the .mp4 (and the last frame) are downloaded
right away. Plain HTTPS through httpx; settings come from the same .env as server.py.
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import re
import time
from pathlib import Path

import httpx

# BytePlus ModelArk (international). For Volcengine Ark (China) use
# SEEDANCE_API_BASE=https://ark.cn-beijing.volces.com/api/v3 and doubao-seedance-* model ids.
API = os.environ.get("SEEDANCE_API_BASE", "https://ark.ap-southeast.bytepluses.com/api/v3").rstrip("/")
MODEL = os.environ.get("SEEDANCE_MODEL", "dreamina-seedance-2-5-260628")
MODEL_FAST = os.environ.get("SEEDANCE_MODEL_FAST", "dreamina-seedance-2-0-fast-260128")
TIMEOUT = float(os.environ.get("SEEDANCE_TIMEOUT", "1200"))
POLL = float(os.environ.get("SEEDANCE_POLL_SECONDS", "5"))
WATERMARK = os.environ.get("SEEDANCE_WATERMARK", "0").lower() in ("1", "true", "yes")
MAX_IMAGES = int(os.environ.get("SEEDANCE_MAX_IMAGES", "9"))
MAX_VIDEOS = 3
MAX_IMAGE_BYTES = 8 * 1024 * 1024  # larger references are re-encoded as JPEG before upload

RATIOS = ("16:9", "9:16", "4:3", "3:4", "1:1", "21:9", "9:21", "adaptive")
RESOLUTIONS = ("480p", "720p", "1080p")
IMAGE_MODES = ("auto", "first_frame", "first_last", "reference")
KEY_DIR_NAMES = ("byteplus", "seedance", "ark")


class SeedanceError(Exception):
    pass


# --------------------------------------------------------------------------
# API key: SEEDANCE_API_KEY / ARK_API_KEY, else SEEDANCE_API_KEY_FILE, else a key file in a
# "byteplus" folder next to the OpenAI / Gemini key folders (…/data/byteplus/*).
# --------------------------------------------------------------------------
def _key_from_file(p: Path) -> str | None:
    text = p.read_text(encoding="utf-8-sig", errors="ignore")
    m = re.search(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", text, re.I)
    if m:  # ModelArk keys are UUID-shaped
        return m.group(0)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if lines:
        line = lines[0].split("=", 1)[-1].strip().strip('"').strip("'")
        return line or None
    return None


def _sibling_key_files() -> list[Path]:
    """Key files in …/byteplus/ beside the folder that holds the OpenAI or Gemini key file."""
    out: list[Path] = []
    for env in ("GEMINI_API_KEY_FILE", "OPENAI_API_KEY_FILE"):
        f = os.environ.get(env, "").strip().strip('"').strip("'")
        if not f:
            continue
        data_dir = Path(os.path.expandvars(os.path.expanduser(f))).parent.parent
        for name in KEY_DIR_NAMES:
            d = data_dir / name
            if d.is_dir():
                files = sorted(x for x in d.iterdir() if x.is_file() and x.suffix.lower() in
                               (".api_key", ".key", ".txt", ""))
                out += [x for x in files if x not in out]
    return out


def api_key() -> tuple[str | None, str]:
    for env in ("SEEDANCE_API_KEY", "ARK_API_KEY"):
        key = os.environ.get(env, "").strip()
        if key:
            return key, env
    file = os.environ.get("SEEDANCE_API_KEY_FILE", "").strip().strip('"').strip("'")
    if file:
        p = Path(os.path.expandvars(os.path.expanduser(file)))
        if not p.is_file():
            return None, f"SEEDANCE_API_KEY_FILE not found: {p}"
        key = _key_from_file(p)
        return (key, str(p)) if key else (None, f"no key found in {p}")
    for p in _sibling_key_files():
        key = _key_from_file(p)
        if key:
            return key, f"{p} (found automatically)"
    return None, "neither SEEDANCE_API_KEY nor SEEDANCE_API_KEY_FILE is set"


def available() -> bool:
    return api_key()[0] is not None


def _headers() -> dict:
    key, source = api_key()
    if not key:
        raise SeedanceError(f"No Seedance (BytePlus ModelArk) API key: {source}")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


# --------------------------------------------------------------------------
# Request helpers
# --------------------------------------------------------------------------
def _data_uri(raw: bytes, mime: str) -> str:
    if len(raw) > MAX_IMAGE_BYTES:
        from PIL import Image as PILImage

        im = PILImage.open(io.BytesIO(raw)).convert("RGB")
        im.thumbnail((4096, 4096))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=90)
        raw, mime = buf.getvalue(), "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def image_roles(n: int, mode: str) -> list[str]:
    """How the given images are used.

    auto: 1 image -> first frame, 2 -> first + last frame, 3+ -> subject / style references."""
    mode = (mode or "auto").lower()
    if mode not in IMAGE_MODES:
        raise SeedanceError(f"image_mode must be one of {', '.join(IMAGE_MODES)} (got {mode}).")
    if n == 0:
        return []
    if mode == "auto":
        mode = "first_frame" if n == 1 else "first_last" if n == 2 else "reference"
    if mode == "first_frame":
        if n != 1:
            raise SeedanceError("image_mode 'first_frame' takes exactly 1 image.")
        return ["first_frame"]
    if mode == "first_last":
        if n != 2:
            raise SeedanceError("image_mode 'first_last' takes exactly 2 images (first frame, last frame).")
        return ["first_frame", "last_frame"]
    return ["reference_image"] * n


def build_body(prompt: str, images: list[tuple[str, bytes, str]], videos: list[str], *, model: str,
               ratio: str, resolution: str, duration: int | None, generate_audio: bool, image_mode: str,
               seed: int | None = None, camera_fixed: bool | None = None) -> dict:
    if len(images) > MAX_IMAGES:
        raise SeedanceError(f"Seedance takes at most {MAX_IMAGES} images (got {len(images)}).")
    if len(videos) > MAX_VIDEOS:
        raise SeedanceError(f"Seedance takes at most {MAX_VIDEOS} reference videos (got {len(videos)}).")
    if ratio not in RATIOS:
        raise SeedanceError(f"aspect_ratio for Seedance must be one of {', '.join(RATIOS)} (got {ratio}).")
    if resolution not in RESOLUTIONS:
        raise SeedanceError(f"resolution for Seedance must be one of {', '.join(RESOLUTIONS)} (got {resolution}).")
    if duration is not None and duration != -1 and not 4 <= duration <= 30:
        raise SeedanceError(f"duration for Seedance is 4-30 seconds, or -1 to let the model decide (got {duration}).")
    roles = image_roles(len(images), image_mode)
    if videos and any(r != "reference_image" for r in roles):
        raise SeedanceError("With reference videos, images can only be references (image_mode 'reference').")

    content: list[dict] = [{"type": "text", "text": prompt}]
    for (_name, raw, mime), role in zip(images, roles):
        content.append({"type": "image_url", "image_url": {"url": _data_uri(raw, mime)}, "role": role})
    for url in videos:
        content.append({"type": "video_url", "video_url": {"url": url}, "role": "reference_video"})
    body: dict = {"model": model, "content": content, "ratio": ratio, "resolution": resolution,
                  "generate_audio": bool(generate_audio), "watermark": WATERMARK, "return_last_frame": True}
    if duration is not None:
        body["duration"] = int(duration)
    if seed is not None:
        body["seed"] = int(seed)
    if camera_fixed is not None:
        body["camera_fixed"] = bool(camera_fixed)
    return body


def _error_text(r: httpx.Response) -> str:
    try:
        j = r.json()
        e = j.get("error", j)
        if isinstance(e, dict):
            msg = " ".join(str(e[k]) for k in ("code", "message") if e.get(k)) or str(e)
        else:
            msg = str(e)
    except Exception:
        msg = r.text[:500]
    return f"HTTP {r.status_code}: {msg}"


async def probe() -> str:
    """Free connectivity check: list one task. Returns a short status line."""
    key, source = api_key()
    if not key:
        return f"MISSING key — {source}"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=15)) as c:
            r = await c.get(f"{API}/contents/generations/tasks", headers=_headers(),
                            params={"page_num": 1, "page_size": 1})
    except httpx.HTTPError as e:
        return f"key ok ({source}) but cannot reach {API}: {type(e).__name__}: {e}"
    if r.status_code >= 400:
        return f"key from {source} REJECTED — {_error_text(r)}"
    return f"ok — key {source}, API reachable ({API})"


# --------------------------------------------------------------------------
# Public call
# --------------------------------------------------------------------------
async def render_video(prompt: str, images: list[tuple[str, bytes, str]], videos: list[str] | None = None, *,
                       ratio: str = "16:9", resolution: str = "720p", duration: int | None = None,
                       generate_audio: bool = True, image_mode: str = "auto", fast: bool = False,
                       seed: int | None = None, camera_fixed: bool | None = None) -> dict:
    """Run one video task to the end.

    Returns {"video": mp4 bytes, "last_frame": png/jpeg bytes or None, "task_id", "model",
             "video_url" (expires ~24h), "duration", "ratio", "resolution", "seed"}."""
    model = MODEL_FAST if fast else MODEL
    body = build_body(prompt, images, list(videos or []), model=model, ratio=ratio, resolution=resolution,
                      duration=duration, generate_audio=generate_audio, image_mode=image_mode, seed=seed,
                      camera_fixed=camera_fixed)
    async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=30), follow_redirects=True) as c:
        r = await c.post(f"{API}/contents/generations/tasks", headers=_headers(), json=body)
        if r.status_code >= 400:
            raise SeedanceError(_error_text(r))
        task_id = r.json().get("id")
        if not task_id:
            raise SeedanceError(f"no task id in the response: {r.text[:300]}")

        deadline = time.time() + TIMEOUT
        data: dict = {}
        while True:
            await asyncio.sleep(POLL)
            g = await c.get(f"{API}/contents/generations/tasks/{task_id}", headers=_headers())
            if g.status_code >= 400:
                if g.status_code >= 500 and time.time() < deadline:
                    continue  # transient; keep polling
                raise SeedanceError(f"task {task_id}: " + _error_text(g))
            data = g.json()
            status = str(data.get("status", "")).lower()
            if status == "succeeded":
                break
            if status in ("failed", "cancelled", "expired"):
                err = data.get("error") or {}
                msg = " ".join(str(err.get(k)) for k in ("code", "message") if isinstance(err, dict) and err.get(k))
                raise SeedanceError(f"task {task_id} {status}: {msg or 'no details'}")
            if time.time() > deadline:
                raise SeedanceError(f"task {task_id} still {status or 'pending'} after {TIMEOUT:.0f}s "
                                    "(it may still finish in the BytePlus console)")

        out = data.get("content") or {}
        url = out.get("video_url")
        if not url:
            raise SeedanceError(f"task {task_id} succeeded but has no video_url")
        v = await c.get(url, timeout=httpx.Timeout(600, connect=30))
        if v.status_code >= 400:
            raise SeedanceError(f"video download failed: HTTP {v.status_code}")
        last = None
        if out.get("last_frame_url"):
            try:
                lf = await c.get(out["last_frame_url"], timeout=httpx.Timeout(120, connect=30))
                last = lf.content if lf.status_code < 400 else None
            except httpx.HTTPError:
                last = None
    return {"video": v.content, "last_frame": last, "task_id": task_id, "model": data.get("model") or model,
            "video_url": url, "duration": data.get("duration"), "ratio": data.get("ratio") or ratio,
            "resolution": data.get("resolution") or resolution, "seed": data.get("seed")}
