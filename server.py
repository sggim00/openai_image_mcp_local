"""openai_image_mcp_local — image (OpenAI gpt-image / Google Gemini) and video (Seedance / Gemini)
generation as a LOCAL (stdio) MCP server.

For Claude Desktop / Claude Code on your own machine. No hosting, no upload page:
reference images are passed as local file paths, codes of earlier results, URLs or
"clipboard", and every result is saved straight into OUTPUT_DIR.

Tools
-----
- generate_image  : prompt (+ optional reference images) -> image
- generate_batch  : several prompts sharing the same references, rendered in parallel
- edit_image      : change an existing image, keeping the rest
- generate_video  : prompt (+ optional images / reference videos) -> short video with sound
                    (Seedance 2.5 via BytePlus ModelArk, or Gemini Omni)
- wait_for_images : collect results of jobs (images or videos) that took longer than one tool call
- list_recent     : recent results (code, path, prompt)
- open_output_folder : open the output folder in Explorer / Finder

Run:  python server.py      (Claude Desktop / Claude Code start it for you over stdio)
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from PIL import Image as PILImage
from PIL import ImageDraw
from pydantic import Field

from mcp.server.mcpserver import Image, MCPServer

HERE = Path(__file__).resolve().parent

# stdout is the MCP channel in stdio mode -> logs MUST go to stderr.
logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_image_mcp_local")


# --------------------------------------------------------------------------
# Settings (.env next to this file, overridden by real environment variables)
# --------------------------------------------------------------------------
def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv(HERE / ".env")

import gemini  # noqa: E402  (reads its settings from the environment loaded above)
import seedance  # noqa: E402

MODEL = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-2")
DEFAULT_QUALITY = os.environ.get("DEFAULT_QUALITY", "medium")
BACKEND = os.environ.get("IMAGE_BACKEND", "openai")  # "openai" | "fake" (testing, no API calls)
FAKE_DELAY = float(os.environ.get("FAKE_DELAY", "0"))
OUTPUT_DIR = Path(os.path.expandvars(os.path.expanduser(
    os.environ.get("OUTPUT_DIR", str(Path.home() / "Pictures" / "openai_image_mcp"))
))).resolve()
PREVIEW_MAX = int(os.environ.get("PREVIEW_MAX_PX", "768"))
OPENAI_TIMEOUT = float(os.environ.get("OPENAI_TIMEOUT", "240"))
# One tool call waits this long, then hands back job ids (Claude calls wait_for_images).
WAIT_SECONDS = float(os.environ.get("TOOL_WAIT_SECONDS", "50"))
MAX_BATCH = 8
MAX_REFS = 16
INDEX = OUTPUT_DIR / ".index.jsonl"
CODE_RE = re.compile(r"^[0-9a-f]{6,32}$")
API_FORMATS = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

_client = None


def _api_key() -> tuple[str | None, str]:
    """Key from OPENAI_API_KEY, else from the file named by OPENAI_API_KEY_FILE.

    The file may hold just the key, a `OPENAI_API_KEY=sk-...` line, or anything else
    containing an `sk-...` token (comments with # are ignored)."""
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if key:
        return key, "OPENAI_API_KEY"
    file = os.environ.get("OPENAI_API_KEY_FILE", "").strip().strip('"').strip("'")
    if not file:
        return None, "neither OPENAI_API_KEY nor OPENAI_API_KEY_FILE is set"
    p = Path(os.path.expandvars(os.path.expanduser(file)))
    if not p.is_file():
        return None, f"OPENAI_API_KEY_FILE not found: {p}"
    text = p.read_text(encoding="utf-8-sig", errors="ignore")
    m = re.search(r"sk-[A-Za-z0-9_\-]{20,}", text)
    if m:
        return m.group(0), str(p)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if lines:
        line = lines[0].split("=", 1)[-1].strip().strip('"').strip("'")
        if line:
            return line, str(p)
    return None, f"no key found in {p}"


def openai_client():
    global _client
    if _client is None:
        from openai import AsyncOpenAI

        key, source = _api_key()
        if not key:
            raise RuntimeError(f"No OpenAI API key: {source}")
        # No automatic retries: a retried 4-minute timeout turns one slow image into a
        # 15-minute wait. Errors go back to Claude immediately instead.
        _client = AsyncOpenAI(api_key=key, timeout=OPENAI_TIMEOUT, max_retries=0)
    return _client


# --------------------------------------------------------------------------
# Output index (one JSON line per saved image, keeps the folder itself clean)
# --------------------------------------------------------------------------
def _index_all() -> list[dict]:
    if not INDEX.exists():
        return []
    out = []
    for line in INDEX.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _index_add(meta: dict) -> None:
    with INDEX.open("a", encoding="utf-8") as f:
        f.write(json.dumps(meta, ensure_ascii=False) + "\n")


def _find_code(code: str) -> dict | None:
    code = code.strip().lower().lstrip("#")
    if not CODE_RE.match(code):
        return None
    hits = [m for m in _index_all() if m["id"].startswith(code)]
    hits = [m for m in hits if Path(m["path"]).exists()]
    return hits[-1] if len({m["id"] for m in hits}) == 1 else None


def _slug(text: str, n: int = 40) -> str:
    s = re.sub(r"[^0-9A-Za-z가-힣]+", "_", text).strip("_")
    return (s[:n].rstrip("_") or "image")


def _save(data: bytes, fmt: str, meta: dict, filename: str | None) -> dict:
    image_id = uuid.uuid4().hex
    stem = _slug(filename) if filename else f"{datetime.now():%Y%m%d_%H%M%S}_{_slug(meta.get('prompt', ''))}"
    path = OUTPUT_DIR / f"{stem}_{image_id[:8]}.{'jpg' if fmt == 'jpeg' else fmt}"
    path.write_bytes(data)
    meta = {"id": image_id, "path": str(path), "created": time.time(), **meta}
    _index_add(meta)
    return meta


def _preview(data: bytes, max_px: int) -> bytes:
    """Downscaled JPEG so tool results stay light."""
    im = PILImage.open(io.BytesIO(data))
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = PILImage.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        im = bg
    else:
        im = im.convert("RGB")
    im.thumbnail((max_px, max_px))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80)
    return buf.getvalue()


def _fake_image(prompt: str, size: str) -> bytes:
    w, h = (1024, 1024)
    if re.match(r"^\d+x\d+$", size):
        w, h = (int(v) for v in size.split("x"))
    im = PILImage.new("RGB", (w, h), (40, 44, 60))
    ImageDraw.Draw(im).multiline_text((32, 32), f"FAKE BACKEND\n\n{prompt[:300]}", fill=(230, 230, 230))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


# --------------------------------------------------------------------------
# Reference images: path | code | URL | "clipboard"
# --------------------------------------------------------------------------
class RefError(Exception):
    pass


def _as_api_image(raw: bytes, name: str) -> tuple[str, bytes, str]:
    """PNG/JPEG/WebP pass through; anything else Pillow opens (TGA, TIFF, BMP...) -> PNG."""
    im = PILImage.open(io.BytesIO(raw))
    fmt = (im.format or "").lower()
    if fmt in API_FORMATS:
        return name, raw, API_FORMATS[fmt]
    buf = io.BytesIO()
    if im.mode not in ("RGB", "RGBA", "L", "LA"):
        im = im.convert("RGBA")
    im.save(buf, "PNG")
    return Path(name).stem + ".png", buf.getvalue(), "image/png"


def _clipboard_image() -> tuple[str, bytes]:
    from PIL import ImageGrab

    try:
        clip = ImageGrab.grabclipboard()
    except Exception as e:
        raise RefError(f"Could not read the clipboard: {e}")
    if isinstance(clip, list):  # files copied in Explorer / Finder
        files = [p for p in clip if Path(p).is_file()]
        if not files:
            raise RefError("The clipboard holds files, but none are readable images.")
        p = Path(files[0])
        return p.name, p.read_bytes()
    if clip is None:
        raise RefError("No image on the clipboard. Ask the user to copy an image (or give a file path).")
    buf = io.BytesIO()
    clip.save(buf, "PNG")
    return "clipboard.png", buf.getvalue()


async def _download(url: str) -> tuple[str, bytes]:
    import urllib.request

    def get() -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": "openai_image_mcp_local"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()

    name = Path(url.split("?")[0]).name or "reference.png"
    return name, await asyncio.to_thread(get)


async def _resolve_one(ref: str) -> tuple[str, tuple[str, bytes, str]]:
    """Return (label for metadata, (filename, bytes, mimetype))."""
    s = ref.strip().strip('"').strip("'")
    if s.lower() in ("clipboard", "클립보드", "clip"):
        name, raw = _clipboard_image()
        return "clipboard", _as_api_image(raw, name)
    if re.match(r"^https?://", s, re.I):
        name, raw = await _download(s)
        return s, _as_api_image(raw, name)
    p = Path(os.path.expandvars(os.path.expanduser(s)))
    if not p.is_absolute():
        alt = OUTPUT_DIR / p
        p = alt if alt.exists() else p.resolve()
    if p.is_file():
        return str(p), _as_api_image(p.read_bytes(), p.name)
    m = _find_code(s)
    if m:
        if m.get("media") == "video":
            raise RefError(f"{ref} is a video, not an image (use it as continue_from in generate_video)")
        p = Path(m["path"])
        return m["id"][:8], _as_api_image(p.read_bytes(), p.name)
    raise RefError(f"not found: {ref}")


async def _load_refs(refs: list[str] | None) -> tuple[list[str], list[tuple[str, bytes, str]]]:
    refs = [r for r in (refs or []) if r and r.strip()]
    if len(refs) > MAX_REFS:
        raise RefError(f"Too many reference images ({len(refs)}); the limit is {MAX_REFS}.")
    labels, files, bad = [], [], []
    for r in refs:
        try:
            label, f = await _resolve_one(r)
        except RefError as e:
            bad.append(str(e))
            continue
        except Exception as e:
            bad.append(f"{r}: {type(e).__name__}: {e}")
            continue
        labels.append(label)
        files.append(f)
    if bad:
        raise RefError(
            "Reference image problem — " + "; ".join(bad) + ". Use an absolute file path, the 8-char code "
            "of an earlier result (see list_recent), an http(s) URL, or 'clipboard'."
        )
    return labels, files


# --------------------------------------------------------------------------
# Rendering jobs
# --------------------------------------------------------------------------
PROVIDERS = ("auto", "openai", "gemini", "gemini-pro")


def openai_available() -> bool:
    return _api_key()[0] is not None


def pick_provider(requested: str | None, kind: str, n_refs: int, background: str) -> str:
    """Resolve 'auto' to a concrete provider.

    auto: new images -> OpenAI (strongest at composing a picture from a description),
          edits of an existing image -> Gemini (strongest at changing only what was asked).
    Overrides: transparent background -> OpenAI (Gemini cannot), more refs than OpenAI
    takes -> Gemini. Falls back to whichever provider has an API key."""
    req = (requested or "auto").lower()
    if req not in PROVIDERS:
        raise RefError(f"provider must be one of {', '.join(PROVIDERS)} (got {requested}).")
    if req.startswith("gemini") and background == "transparent":
        raise RefError("Gemini cannot make transparent backgrounds. Use provider 'openai' for cut-out assets.")
    if BACKEND == "fake":
        return "openai" if req == "auto" and kind != "edit" else ("gemini" if req == "auto" else req)
    has_o, has_g = openai_available(), gemini.available()
    if req == "auto":
        if background == "transparent":
            req = "openai"
        elif kind == "edit":
            req = "gemini"
        else:
            req = "openai"
        if req == "openai" and not has_o and has_g:
            req = "gemini"
        elif req.startswith("gemini") and not has_g and has_o:
            req = "openai"
    if req == "openai" and not has_o:
        raise RefError(f"No OpenAI API key ({_api_key()[1]}). Use provider 'gemini' or set the key.")
    if req.startswith("gemini") and not has_g:
        raise RefError(f"No Gemini API key ({gemini.api_key()[1]}). Set GEMINI_API_KEY_FILE in .env.")
    if req.startswith("gemini") and background == "transparent":
        raise RefError("Gemini cannot make transparent backgrounds. Use provider 'openai' for cut-out assets.")
    return req


def _convert(data: bytes, want: str) -> tuple[bytes, str]:
    """Gemini returns PNG or JPEG; convert when another format was asked for."""
    im = PILImage.open(io.BytesIO(data))
    have = (im.format or "png").lower()
    if have == want or want not in API_FORMATS:
        return data, (have if have in API_FORMATS else "png")
    if want == "jpeg" and im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    buf = io.BytesIO()
    im.save(buf, want.upper())
    return buf.getvalue(), want


async def _render(prompt: str, refs: list[tuple[str, bytes, str]], size: str, quality: str,
                  background: str, output_format: str, provider: str = "openai") -> tuple[bytes, str, str]:
    """-> (image bytes, format, model used)"""
    if BACKEND == "fake":
        if FAKE_DELAY:
            await asyncio.sleep(FAKE_DELAY)
        tag = f"[{provider}] " + (f"[{len(refs)} ref] " if refs else "")
        return _fake_image(tag + prompt, "1024x1024" if not re.match(r"^\d+x\d+$", size) else size), "png", "fake"
    if provider.startswith("gemini"):
        model = gemini.IMAGE_MODEL_PRO if provider == "gemini-pro" else gemini.IMAGE_MODEL
        data, model = await gemini.render_image(prompt, refs, size, quality, output_format, model)
        data, fmt = _convert(data, output_format)
        return data, fmt, model
    if ":" in size:  # an aspect ratio meant for Gemini
        size = {"1:1": "1024x1024"}.get(size) or ("1536x1024" if int(size.split(":")[0]) > int(size.split(":")[1])
                                                 else "1024x1536")
    kwargs: dict = dict(model=MODEL, prompt=prompt, size=size, quality=quality, output_format=output_format)
    if background != "auto":
        kwargs["background"] = background
    if refs:
        # The edits endpoint is how reference images reach the model; without it the
        # model only ever sees words.
        r = await openai_client().images.edit(image=refs if len(refs) > 1 else refs[0], **kwargs)
    else:
        r = await openai_client().images.generate(**kwargs)
    return base64.b64decode(r.data[0].b64_json), output_format, MODEL


async def _render_video(prompt: str, images: list[tuple[str, bytes, str]], aspect_ratio: str, resolution: str,
                        previous: str | None, task: str | None) -> tuple[bytes, str]:
    """Gemini Omni -> (mp4 bytes, interaction id)"""
    if BACKEND == "fake":
        await asyncio.sleep(FAKE_DELAY)
        return b"FAKE MP4 " + prompt[:200].encode(), "fake_" + uuid.uuid4().hex[:8]
    return await gemini.render_video(prompt, images, aspect_ratio, resolution, previous, task)


# --------------------------------------------------------------------------
# Video: Gemini Omni or Seedance, shared by the PC tool and the mobile worker
# --------------------------------------------------------------------------
VIDEO_PROVIDERS = ("auto", "gemini", "seedance", "seedance-fast")
DEFAULT_VIDEO_PROVIDER = os.environ.get("DEFAULT_VIDEO_PROVIDER", "seedance").lower()
GEMINI_RATIOS = ("16:9", "9:16")
GEMINI_RESOLUTIONS = ("360p", "720p", "1080p", "4k")
SEEDANCE_URL_HOURS = 23  # Seedance result links expire after ~24h


class VideoSpec(dict):
    """Validated video request (a dict so it can be logged / stored as-is)."""


def _video_key(provider: str) -> bool:
    if BACKEND == "fake":
        return True
    return seedance.available() if provider.startswith("seedance") else gemini.available()


def pick_video_provider(requested: str | None, *, continued: dict | None, n_images: int, n_videos: int,
                        aspect_ratio: str, resolution: str, duration: int | None, task: str | None) -> str:
    """Resolve 'auto' for video.

    auto: a continued video stays with the provider that made it; options only one provider has
    decide it (Seedance: other ratios, 480p, duration, >3 images, reference videos; Gemini: 360p/4k,
    task); otherwise DEFAULT_VIDEO_PROVIDER (seedance), falling back to whichever has an API key."""
    req = (requested or "auto").lower()
    if req not in VIDEO_PROVIDERS:
        raise RefError(f"video provider must be one of {', '.join(VIDEO_PROVIDERS)} (got {requested}).")
    if req == "auto":
        if continued:
            req = continued.get("provider") or "gemini"
        elif (n_videos or n_images > gemini.VIDEO_MAX_IMAGES or aspect_ratio not in GEMINI_RATIOS
              or resolution == "480p" or duration is not None):
            req = "seedance"
        elif resolution in ("360p", "4k") or task:
            req = "gemini"
        else:
            req = DEFAULT_VIDEO_PROVIDER if DEFAULT_VIDEO_PROVIDER in VIDEO_PROVIDERS[1:] else "seedance"
            if not _video_key(req):
                other = "gemini" if req.startswith("seedance") else "seedance"
                req = other if _video_key(other) else req
    if not _video_key(req):
        if req.startswith("seedance"):
            raise RefError(f"No Seedance API key ({seedance.api_key()[1]}). Set SEEDANCE_API_KEY_FILE in "
                           f"{HERE / '.env'}, or use provider 'gemini'.")
        raise RefError(f"No Gemini API key ({gemini.api_key()[1]}). Set GEMINI_API_KEY_FILE in {HERE / '.env'}.")
    return req


def prepare_video(prompt: str, labels: list[str], files: list[tuple[str, bytes, str]], *, provider: str | None,
                  aspect_ratio: str, resolution: str, duration: int | None = None, generate_audio: bool = True,
                  image_mode: str = "auto", continue_from: str | None = None,
                  reference_videos: list[str] | None = None, task: str | None = None) -> VideoSpec:
    """Check a video request against what the chosen provider can do. Raises RefError."""
    videos = [v.strip() for v in (reference_videos or []) if v and v.strip()]
    for v in videos:
        if not re.match(r"^https?://", v, re.I):
            raise RefError(f"Reference videos must be public http(s) URLs (got {v}). For an earlier video from "
                           "this server use continue_from=<its code>.")
    continued = None
    if continue_from:
        continued = _find_code(continue_from)
        if not continued or continued.get("media") != "video":
            raise RefError(f"{continue_from} is not the code of a video made by this server (see list_recent).")
    resolution = (resolution or "720p").lower()
    prov = pick_video_provider(provider, continued=continued, n_images=len(files), n_videos=len(videos),
                               aspect_ratio=aspect_ratio, resolution=resolution, duration=duration, task=task)
    spec = VideoSpec(prompt=prompt, provider=prov, labels=labels, files=files, videos=videos,
                     aspect_ratio=aspect_ratio, resolution=resolution, duration=duration,
                     generate_audio=generate_audio, image_mode=image_mode or "auto", task=task,
                     continue_from=continue_from, previous=None)

    if prov == "gemini":
        if continued and not continued.get("interaction_id"):
            raise RefError(f"{continue_from} was made by {continued.get('provider')}; continue it with that provider.")
        if videos:
            raise RefError("Gemini does not take reference videos; use provider 'seedance'.")
        if aspect_ratio not in GEMINI_RATIOS:
            raise RefError(f"Gemini video is 16:9 or 9:16 only (got {aspect_ratio}); use provider 'seedance'.")
        if resolution not in GEMINI_RESOLUTIONS:
            raise RefError(f"Gemini video resolution is {', '.join(GEMINI_RESOLUTIONS)} (got {resolution}).")
        if len(files) > gemini.VIDEO_MAX_IMAGES:
            raise RefError(f"Gemini takes at most {gemini.VIDEO_MAX_IMAGES} images for a video; use 'seedance'.")
        spec["previous"] = continued.get("interaction_id") if continued else None
        spec["model"] = gemini.VIDEO_MODEL
        return spec

    # Seedance: a continued video goes back in as a reference video (its link lives ~24h)
    if continued:
        if not continued.get("video_url"):
            raise RefError(f"{continue_from} was made by {continued.get('provider')}; continue it with that provider.")
        age_h = (time.time() - continued.get("created", 0)) / 3600
        if age_h > SEEDANCE_URL_HOURS:
            hint = (f" Use its last frame `{continued['last_frame_code']}` as the first image of a new video instead."
                    if continued.get("last_frame_code") else "")
            raise RefError(f"The Seedance link of {continue_from} has expired ({age_h:.0f}h old, links last ~24h)."
                           + hint)
        spec["videos"] = [continued["video_url"]] + videos
        if len(files) and image_mode not in ("auto", "reference"):
            raise RefError("With continue_from, Seedance images can only be references (image_mode 'reference').")
        spec["image_mode"] = "reference"
    try:
        seedance.build_body(prompt, files, spec["videos"], model="check", ratio=aspect_ratio, resolution=resolution,
                            duration=duration, generate_audio=generate_audio, image_mode=spec["image_mode"])
    except seedance.SeedanceError as e:
        raise RefError(str(e))
    spec["model"] = seedance.MODEL_FAST if prov == "seedance-fast" else seedance.MODEL
    return spec


async def render_video(spec: VideoSpec, request: str | None = None) -> tuple[bytes, dict]:
    """Run a prepared video request -> (mp4 bytes, metadata for _save). A Seedance last frame is
    saved as its own image (code in last_frame_code) so it can start the next shot."""
    prov = spec["provider"]
    common = {"media": "video", "provider": prov, "aspect_ratio": spec["aspect_ratio"],
              "resolution": spec["resolution"], "continued_from": spec["continue_from"],
              "references": spec["labels"]}
    if prov == "gemini":
        data, interaction = await _render_video(spec["prompt"], spec["files"], spec["aspect_ratio"],
                                                spec["resolution"], spec["previous"], spec["task"])
        return data, {**common, "model": gemini.VIDEO_MODEL if BACKEND != "fake" else "fake",
                      "interaction_id": interaction}

    if BACKEND == "fake":
        await asyncio.sleep(FAKE_DELAY)
        r = {"video": b"FAKE MP4 " + spec["prompt"][:200].encode(), "task_id": "fake_" + uuid.uuid4().hex[:8],
             "model": "fake", "video_url": "https://example.invalid/fake.mp4", "duration": spec["duration"],
             "ratio": spec["aspect_ratio"], "resolution": spec["resolution"], "seed": None,
             "last_frame": _fake_image("LAST FRAME " + spec["prompt"], "1024x576")}
    else:
        r = await seedance.render_video(spec["prompt"], spec["files"], spec["videos"], ratio=spec["aspect_ratio"],
                                        resolution=spec["resolution"], duration=spec["duration"],
                                        generate_audio=spec["generate_audio"], image_mode=spec["image_mode"],
                                        fast=prov == "seedance-fast")
    meta = {**common, "model": r["model"], "task_id": r["task_id"], "video_url": r["video_url"],
            "duration": r["duration"], "aspect_ratio": r["ratio"], "resolution": r["resolution"], "seed": r["seed"],
            "generate_audio": spec["generate_audio"], "image_mode": spec["image_mode"],
            "reference_videos": [v for v in spec["videos"] if v != r["video_url"]]}
    if r.get("last_frame"):
        try:
            im = PILImage.open(io.BytesIO(r["last_frame"]))
            fmt = "jpeg" if (im.format or "").lower() == "jpeg" else "png"
            lf = _save(r["last_frame"], fmt, {"kind": "last_frame", "prompt": "last frame of: " + spec["prompt"],
                                              "model": r["model"], "provider": prov, "task_id": r["task_id"]},
                       (request + "_last") if request else None)
            meta["last_frame_code"] = lf["id"][:8]
            meta["last_frame_path"] = lf["path"]
        except Exception as e:  # the video itself is what matters
            log.warning("could not save the last frame of %s: %s", r["task_id"], e)
    return r["video"], meta


JOBS: dict[str, dict] = {}


def _start_job(kind: str, prompt: str, ref_labels: list[str], refs: list[tuple[str, bytes, str]],
               size: str, quality: str, background: str, output_format: str, filename: str | None,
               provider: str = "openai") -> str:
    async def render() -> tuple[bytes, str, dict]:
        data, fmt, model = await _render(prompt, refs, size, quality, background, output_format, provider)
        return data, fmt, {"model": model, "provider": provider, "size": size, "quality": quality}

    return _start(kind, prompt, ref_labels, filename, render)


def _start(kind: str, prompt: str, ref_labels: list[str], filename: str | None, render) -> str:
    job_id = "job_" + uuid.uuid4().hex[:10]

    async def run() -> dict:
        t0 = time.time()
        try:
            data, fmt, extra = await render()
        except Exception as e:  # surfaced to Claude as text, never raised through MCP
            log.warning("%s failed after %.1fs: %s", job_id, time.time() - t0, e)
            return {"error": f"{type(e).__name__}: {e}", "seconds": time.time() - t0}
        meta = {"kind": kind, "prompt": prompt, "references": ref_labels, **extra,
                "seconds": round(time.time() - t0, 1)}
        meta = _save(data, fmt, meta, filename)
        log.info("%s done in %.1fs -> %s", job_id, time.time() - t0, meta["path"])
        return {"meta": meta, "data": data}

    JOBS[job_id] = {"task": asyncio.create_task(run()), "created": time.time(), "prompt": prompt}
    for jid in [j for j, v in JOBS.items() if v["task"].done() and time.time() - v["created"] > 3600]:
        JOBS.pop(jid, None)
    return job_id


async def _collect(job_ids: list[str], wait: float) -> list:
    tasks = {j: JOBS[j]["task"] for j in job_ids if j in JOBS}
    pending = [t for t in tasks.values() if not t.done()]
    if pending:
        await asyncio.wait(pending, timeout=wait)

    preview_px = PREVIEW_MAX if len(job_ids) == 1 else 512
    texts, images, still = [], [], []
    done_video = False
    for j in job_ids:
        if j not in tasks:
            texts.append(f"- {j}: unknown job (the server was restarted). Generate it again.")
            continue
        t = tasks[j]
        if not t.done():
            still.append(j)
            texts.append(f"- {j}: still rendering ({time.time() - JOBS[j]['created']:.0f}s so far)")
            continue
        res = t.result()
        if "error" in res:
            texts.append(f"- {j}: FAILED after {res['seconds']:.0f}s — {res['error']}")
            continue
        m = res["meta"]
        via = f" | {m.get('model')}" if m.get("model") else ""
        if m.get("media") == "video":
            extra = f" | {m['duration']}s clip" if m.get("duration") else ""
            if m.get("last_frame_code"):
                extra += f" | last frame image `{m['last_frame_code']}`"
            texts.append(f"- {j}: VIDEO done in {m['seconds']}s{via}{extra} | code `{m['id'][:8]}` | saved: {m['path']}")
            done_video = True
            continue
        texts.append(f"- {j}: done in {m['seconds']}s{via} | code `{m['id'][:8]}` | saved: {m['path']}")
        images.append(Image(data=_preview(res["data"], preview_px), format="jpeg"))

    header = []
    if still:
        header.append(
            f"{len(still)} job(s) still rendering. Tell the user briefly, then call wait_for_images "
            f"with job_ids {json.dumps(still)} (do not start them again)."
        )
    if images:
        header.append(
            "Tell the user where each file was saved. The short code (or the file path) can be used in "
            "reference_images / image in later calls."
        )
    if done_video:
        header.append(
            "Videos cannot be previewed here: give the user the saved .mp4 path. To change or lengthen a "
            "video, call generate_video with continue_from=<its code>. A Seedance video's last frame image "
            "code can be the first image of the next shot."
        )
    return ["\n".join(header + texts)] + images


# --------------------------------------------------------------------------
# MCP server
# --------------------------------------------------------------------------
INSTRUCTIONS = f"""\
Local server that creates and edits images (OpenAI {MODEL} or Google Gemini) and short videos
(ByteDance Seedance or Google {gemini.VIDEO_MODEL}). Results are saved to {OUTPUT_DIR}.

Choosing the image model (provider) — decide per request, it is your call
- "openai" ({MODEL}): best at NEW pictures from a description — composition, illustration and
  character design, faithful long prompts, clean layouts, transparent backgrounds (cut-out assets).
- "gemini" ({gemini.IMAGE_MODEL}): best at EDITING — change only what was asked and keep everything
  else (face, pose, lighting, style) identical; multi-turn tweaks; combining several reference
  images (up to 14) while keeping one character consistent; fast.
- "gemini-pro" ({gemini.IMAGE_MODEL_PRO}): Gemini's premium model — hardest edits, precise text in
  the image, brand / product consistency, up to 4K. Slower and more expensive.
- "auto" (default): generate_image / generate_batch -> openai, edit_image -> gemini, transparent
  background -> openai, and whichever has an API key if one is missing.
- Rules of thumb: "make / draw / design" -> openai. "change / fix / remove / add / recolor / keep the rest",
  turning a photo into another pose or outfit while keeping the person, or splitting an existing
  character into parts that must match it exactly -> gemini. If one provider's result misses, offer
  to retry with the other.

Reference images (IMPORTANT)
- This server runs on the user's computer, so it can read their files directly. When the user wants
  to use an existing picture (keep its style, make variations, split a character into parts, edit a
  photo), pass it in reference_images (or image for edit_image) instead of describing it in words.
- Accepted values: an absolute file path (png/jpg/webp/tga/tif/bmp...), the 8-char code of an earlier
  result, an http(s) URL, or "clipboard" (the image the user just copied).
- Images attached in the chat window do NOT reach this server. If the user attached one, ask for its
  file path, or ask them to copy it and say so, then use "clipboard".

Prompts
- Expand the request into a detailed English prompt. With references, say explicitly what to take
  from them, e.g. "Use the character in the reference image; keep exactly the same design, colors and
  proportions. Show ..."
- Keep any text that must appear in the image exactly as the user wrote it.

Video (generate_video) — choose the provider per request
- "seedance" ({seedance.MODEL}, ByteDance Seedance via BytePlus ModelArk): the default. 4-30 s clips
  (duration), any ratio (16:9, 9:16, 4:3, 3:4, 1:1, 21:9, adaptive), 480p/720p/1080p, strong motion and
  multi-shot storytelling, first-frame / first+last-frame control, up to {seedance.MAX_IMAGES} reference
  images for character / product consistency (image_mode "reference"), reference videos (public URLs)
  for motion or editing, sound on/off (generate_audio). Returns the last frame as an image code too —
  use it as the first image of the next shot for continuity.
- "seedance-fast" ({seedance.MODEL_FAST}): cheaper / quicker Seedance for drafts and tests.
- "gemini" ({gemini.VIDEO_MODEL}): multi-turn editing and extension of its own videos through
  continue_from (it remembers the conversation), 360p-4k, 16:9 / 9:16 only, up to 3 images.
- "auto": a continued video stays with its provider; Seedance-only or Gemini-only options decide;
  otherwise {DEFAULT_VIDEO_PROVIDER}. Seedance cannot see this server's local videos except through
  continue_from within ~24h of making them (its links expire).
- Describe camera, motion, mood and audio (or "no dialogue") in the prompt; there is no negative prompt,
  so state what to avoid in the prompt. Seedance follows shot-by-shot prompts well ("Shot 1: ... Shot 2: ...").
- To change or lengthen a finished video, call generate_video again with continue_from=<its code> and
  only the change ("make it night", "extend: the camera pulls back").
- A video takes about 1-5 minutes: the first call usually returns a job id; keep calling
  wait_for_images with it. Videos are not previewed; give the user the .mp4 path. Use 480p or
  seedance-fast for tests; long 1080p clips cost several dollars.

Tools
- One image: generate_image. Several related images: ONE generate_batch call (renders in parallel).
- Changing an existing result: edit_image with its code or path.
- Rendering takes about 30-120s per image. If a call returns job ids that are still rendering, call
  wait_for_images with those ids; never re-submit the same prompt.
- Default quality is "{DEFAULT_QUALITY}". Use "low" for quick drafts, "high" only for finals
  (for Gemini, "high" means 2K output and more thinking).
- Afterwards reply briefly in the user's language with the saved file paths and which model made
  them, and point out anything that clearly missed the request.
"""

mcp = MCPServer(name="openai-image-local", instructions=INSTRUCTIONS)

Quality = Literal["low", "medium", "high", "auto"]
OutFmt = Literal["png", "jpeg", "webp"]
Bg = Literal["auto", "opaque", "transparent"]
Provider = Literal["auto", "openai", "gemini", "gemini-pro"]
SizeDesc = ("'1024x1024' (square), '1536x1024' (landscape), '1024x1536' (portrait) or 'auto'. Gemini also "
            "takes an aspect ratio such as '16:9', '9:16', '4:5', '21:9'.")
RefDesc = ("Reference images the model should see and follow (style / character / product). Each item is an "
           "absolute file path, the code of an earlier result, an http(s) URL, or 'clipboard'.")
NameDesc = "Optional file name (no extension) for the saved image. Default: date + prompt."
ProviderDesc = ("Which model makes the image: 'openai' (new pictures, composition, transparent bg), 'gemini' "
                "(precise edits that keep the rest, multi-reference consistency), 'gemini-pro' (hardest edits, "
                "text in image, up to 4K), or 'auto'. Choose deliberately; see the server instructions.")


@mcp.tool()
async def generate_image(
    prompt: Annotated[str, Field(description="Detailed image prompt, preferably in English.")],
    reference_images: Annotated[list[str] | None, Field(description=RefDesc)] = None,
    size: Annotated[str, Field(description=SizeDesc)] = "1024x1024",
    quality: Annotated[Quality | None, Field(description="low / medium / high / auto. Omit for the default.")] = None,
    background: Annotated[Bg, Field(description="'transparent' for cut-out assets (OpenAI only).")] = "auto",
    output_format: Annotated[OutFmt, Field(description="File format of the saved image.")] = "png",
    filename: Annotated[str | None, Field(description=NameDesc)] = None,
    provider: Annotated[Provider, Field(description=ProviderDesc)] = "auto",
) -> list:
    """Create one image, optionally guided by reference images. Saves it locally and returns a preview, or a job id if still rendering."""
    try:
        labels, refs = await _load_refs(reference_images)
        prov = pick_provider(provider, "generate", len(refs), background)
    except RefError as e:
        return [str(e)]
    job = _start_job("generate", prompt, labels, refs, size, quality or DEFAULT_QUALITY, background,
                     output_format, filename, prov)
    return await _collect([job], WAIT_SECONDS)


@mcp.tool()
async def generate_batch(
    prompts: Annotated[list[str], Field(description=f"1-{MAX_BATCH} detailed English prompts, one per image.")],
    reference_images: Annotated[list[str] | None, Field(description=RefDesc + " Shared by every prompt.")] = None,
    size: Annotated[str, Field(description=SizeDesc)] = "1024x1024",
    quality: Annotated[Quality | None, Field(description="low / medium / high / auto. Omit for the default.")] = None,
    background: Annotated[Bg, Field(description="'transparent' for cut-out assets (OpenAI only).")] = "auto",
    output_format: Annotated[OutFmt, Field(description="File format of the saved images.")] = "png",
    provider: Annotated[Provider, Field(description=ProviderDesc)] = "auto",
) -> list:
    """Create several related images in parallel (e.g. every part of one character, or style variations)."""
    if not 1 <= len(prompts) <= MAX_BATCH:
        return [f"Give between 1 and {MAX_BATCH} prompts."]
    try:
        labels, refs = await _load_refs(reference_images)
        prov = pick_provider(provider, "generate", len(refs), background)
    except RefError as e:
        return [str(e)]
    q = quality or DEFAULT_QUALITY
    jobs = [_start_job("generate", p, labels, refs, size, q, background, output_format, None, prov) for p in prompts]
    return await _collect(jobs, WAIT_SECONDS)


@mcp.tool()
async def edit_image(
    instruction: Annotated[str, Field(description="What to change, in English. Also say what must stay the same.")],
    image: Annotated[str, Field(description="The image to edit: file path, code of an earlier result, URL, or 'clipboard'.")],
    reference_images: Annotated[list[str] | None, Field(description="Extra reference images (same formats).")] = None,
    size: Annotated[str, Field(description=SizeDesc)] = "auto",
    quality: Annotated[Quality | None, Field(description="low / medium / high / auto. Omit for the default.")] = None,
    background: Annotated[Bg, Field(description="'transparent' for cut-out assets (OpenAI only).")] = "auto",
    output_format: Annotated[OutFmt, Field(description="File format of the saved image.")] = "png",
    filename: Annotated[str | None, Field(description=NameDesc)] = None,
    provider: Annotated[Provider, Field(description=ProviderDesc + " 'auto' uses Gemini for edits.")] = "auto",
) -> list:
    """Change an existing image while keeping the rest of it. Saves the result locally and returns a preview."""
    try:
        labels, refs = await _load_refs([image] + list(reference_images or []))
        prov = pick_provider(provider, "edit", len(refs), background)
    except RefError as e:
        return [str(e)]
    job = _start_job("edit", instruction, labels, refs, size, quality or DEFAULT_QUALITY, background,
                     output_format, filename, prov)
    return await _collect([job], WAIT_SECONDS)


VideoProvider = Literal["auto", "gemini", "seedance", "seedance-fast"]


@mcp.tool()
async def generate_video(
    prompt: Annotated[str, Field(description="Detailed English description: subject, action, camera, style, sound. "
                                             "With continue_from: only the change or what happens next.")],
    images: Annotated[list[str] | None, Field(description="Images (same formats as reference_images). Gemini: 0-3. "
                                                         f"Seedance: 0-{seedance.MAX_IMAGES}. One = first frame; two = "
                                                         "first and last frame; three or more = subject / style "
                                                         "references (see image_mode).")] = None,
    aspect_ratio: Annotated[Literal["16:9", "9:16", "4:3", "3:4", "1:1", "21:9", "9:21", "adaptive"],
                            Field(description="Gemini: 16:9 or 9:16 only. Seedance: all; 'adaptive' follows the "
                                              "first image.")] = "16:9",
    resolution: Annotated[Literal["360p", "480p", "720p", "1080p", "4k"],
                          Field(description="720p default. Gemini: 360p/720p/1080p/4k. Seedance: 480p/720p/1080p "
                                            "(480p for cheap drafts).")] = "720p",
    duration: Annotated[int | None, Field(description="Seedance only: clip length 4-30 seconds, or -1 to let the "
                                                      "model decide. Omit for the model default (~5s).")] = None,
    generate_audio: Annotated[bool, Field(description="Seedance: generate sound (default true). Gemini always "
                                                      "makes sound; ask for 'no dialogue' etc. in the prompt.")] = True,
    image_mode: Annotated[Literal["auto", "first_frame", "first_last", "reference"],
                          Field(description="Seedance: how images are used. auto: 1 -> first frame, 2 -> first + "
                                            "last frame, 3+ -> references. 'reference' keeps a character / product "
                                            "consistent without fixing the first frame.")] = "auto",
    reference_videos: Annotated[list[str] | None, Field(description="Seedance only: up to 3 public http(s) video "
                                                                    "URLs for motion / camera / editing reference. "
                                                                    "For this server's own videos use "
                                                                    "continue_from.")] = None,
    continue_from: Annotated[str | None, Field(description="Code of an earlier video from this server, to edit or "
                                                           "extend it. Stays with the provider that made it "
                                                           "(Seedance: within ~24h).")] = None,
    provider: Annotated[VideoProvider, Field(description="'seedance' (Seedance 2.5: 4-30s, any ratio, many "
                                                         "references, first/last frame), 'seedance-fast' (cheaper "
                                                         "and faster Seedance 2.0 Fast), 'gemini' (Gemini Omni: "
                                                         "multi-turn edit / extend, 4k), or 'auto'.")] = "auto",
    task: Annotated[Literal["text_to_video", "image_to_video", "reference_to_video", "edit", "extend"] | None,
                    Field(description="Gemini only; normally leave empty and let the prompt decide.")] = None,
    filename: Annotated[str | None, Field(description="Optional file name (no extension) for the .mp4.")] = None,
) -> list:
    """Make a short video with sound (Seedance or Gemini Omni; text and/or images in, .mp4 out), or edit/extend an earlier one."""
    try:
        labels, refs = await _load_refs(images)
        spec = prepare_video(prompt, labels, refs, provider=provider, aspect_ratio=aspect_ratio,
                             resolution=resolution, duration=duration, generate_audio=generate_audio,
                             image_mode=image_mode, continue_from=continue_from,
                             reference_videos=reference_videos, task=task)
    except RefError as e:
        return [str(e)]

    async def render() -> tuple[bytes, str, dict]:
        data, meta = await render_video(spec, filename)
        return data, "mp4", meta

    job = _start("video", prompt, labels, filename, render)
    return await _collect([job], WAIT_SECONDS)


@mcp.tool()
async def wait_for_images(
    job_ids: Annotated[list[str], Field(description="Job ids returned as still rendering.")],
) -> list:
    """Wait for images or videos that were still rendering and return them."""
    return await _collect(job_ids, WAIT_SECONDS)


@mcp.tool()
def list_recent(limit: Annotated[int, Field(ge=1, le=50)] = 10) -> str:
    """List recent results (code, path, prompt) so earlier work can be reused as references."""
    items = [
        {
            "code": m["id"][:8],
            "kind": m.get("kind"),
            "media": m.get("media", "image"),
            "model": m.get("model"),
            "path": m["path"],
            "prompt": (m.get("prompt") or "")[:200],
            "created": datetime.fromtimestamp(m.get("created", 0)).strftime("%Y-%m-%d %H:%M"),
        }
        for m in reversed(_index_all())
        if Path(m["path"]).exists()
    ][:limit]
    if not items:
        return f"No images yet. Output folder: {OUTPUT_DIR}"
    return json.dumps(items, ensure_ascii=False, indent=2)


@mcp.tool()
def open_output_folder() -> str:
    """Open the output folder in Explorer / Finder."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(OUTPUT_DIR)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(OUTPUT_DIR)])
        else:
            subprocess.Popen(["xdg-open", str(OUTPUT_DIR)])
    except Exception as e:
        return f"Could not open {OUTPUT_DIR}: {e}"
    return f"Opened {OUTPUT_DIR}"


def main() -> None:
    if BACKEND == "openai":
        key, source = _api_key()
        gkey, gsource = gemini.api_key()
        skey, ssource = seedance.api_key()
        log.info("OpenAI key: %s", source if key else "MISSING (" + source + ")")
        log.info("Gemini key: %s", gsource if gkey else "MISSING (" + gsource + ")")
        log.info("Seedance key: %s", ssource if skey else "MISSING (" + ssource + ")")
        if not key and not gkey:
            log.error("No image API key. Set OPENAI_API_KEY_FILE and/or GEMINI_API_KEY_FILE in %s "
                      "or the MCP config 'env'.", HERE / ".env")
    log.info("openai_image_mcp_local: openai=%s gemini=%s/%s video=%s,%s/%s (default %s) backend=%s output=%s",
             MODEL, gemini.IMAGE_MODEL, gemini.IMAGE_MODEL_PRO, seedance.MODEL, seedance.MODEL_FAST,
             gemini.VIDEO_MODEL, DEFAULT_VIDEO_PROVIDER, BACKEND, OUTPUT_DIR)
    mcp.run("stdio")


if __name__ == "__main__":
    main()
