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

_MTIMES: dict[str, float] = {}


def _hot_reload() -> None:
    """Pick up edits to the backend modules (gemini.py, seedance.py) without restarting Claude.
    Changes to server.py itself still need a restart."""
    import importlib

    for mod in (gemini, seedance):
        try:
            m = Path(mod.__file__).stat().st_mtime
        except OSError:
            continue
        if _MTIMES.setdefault(mod.__name__, m) != m:
            try:
                importlib.reload(mod)
                _MTIMES[mod.__name__] = m
                log.info("reloaded %s", mod.__name__)
            except Exception as e:  # keep the old code if the new file is broken
                log.warning("could not reload %s: %s", mod.__name__, e)

VERSION = "2026.10.27-gemini-v2v2"
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
                        previous: str | None, task: str | None,
                        videos: list[tuple[str, bytes, str]] | None = None) -> tuple[bytes, str]:
    """Gemini Omni -> (mp4 bytes, interaction id)"""
    if BACKEND == "fake":
        await asyncio.sleep(FAKE_DELAY)
        return b"FAKE MP4 " + prompt[:200].encode(), "fake_" + uuid.uuid4().hex[:8]
    return await gemini.render_video(prompt, images, aspect_ratio, resolution, previous, task, videos)


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


# Seedance only reads reference videos from public URLs. A local file (e.g. a Maya playblast), an
# earlier video's code, or a Drive file is shared through a temporary "anyone with the link" Google
# Drive link for the length of the task, then deleted / unshared again.
VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".webm")
DRIVE_FILE_RE = re.compile(r"drive\.google\.com/.*(?:/d/|[?&]id=)([A-Za-z0-9_-]{20,})")
GDRIVE_TOKEN = Path(os.path.expandvars(os.path.expanduser(
    os.environ.get("GDRIVE_TOKEN_FILE", str(HERE / ".gdrive_token.json")))))
MAX_REF_VIDEO_MB = 50


def _drive_ready() -> bool:
    return GDRIVE_TOKEN.is_file()


def _drive_client():
    from gdrive import Drive

    return Drive.from_file(GDRIVE_TOKEN)


def _video_seconds(p: Path) -> float | None:
    """Duration of an .mp4/.mov from its mvhd box (no ffmpeg needed)."""
    try:
        import struct

        data = p.read_bytes()
        i = data.find(b"mvhd")
        if i < 0:
            return None
        ver = data[i + 4]
        if ver == 1:
            scale, dur = struct.unpack(">IQ", data[i + 24:i + 36])
        else:
            scale, dur = struct.unpack(">II", data[i + 16:i + 24])
        return dur / scale if scale else None
    except Exception:
        return None


def _resolve_video_ref(ref: str, need_public: bool = True) -> str | dict:
    """http(s) URL -> as is; Drive link -> {"drive": id}; local file or earlier video code -> {"local": path}."""
    s = ref.strip().strip('"').strip("'")
    m = DRIVE_FILE_RE.search(s)
    if m:
        if not _drive_ready():
            raise RefError("Drive links need Google Drive set up on the PC (setup_mobile.bat).")
        return {"drive": m.group(1), "label": s}
    if re.match(r"^https?://", s, re.I):
        return s
    p = Path(os.path.expandvars(os.path.expanduser(s)))
    if not p.is_absolute() and (OUTPUT_DIR / p).exists():
        p = OUTPUT_DIR / p
    if not p.is_file():
        hit = _find_code(s)
        if hit and hit.get("media") == "video":
            p = Path(hit["path"])
        else:
            raise RefError(f"Reference video not found: {ref}. Give a local .mp4/.mov path, the code of an earlier "
                           "video, a Google Drive link or a public http(s) URL.")
    if p.suffix.lower() not in VIDEO_EXTS:
        raise RefError(f"{p.name} is not a video ({', '.join(VIDEO_EXTS)}).")
    if p.stat().st_size > MAX_REF_VIDEO_MB * 1024 * 1024:
        raise RefError(f"{p.name} is {p.stat().st_size / 2**20:.0f} MB; keep reference videos under "
                       f"{MAX_REF_VIDEO_MB} MB (shorter or 720p).")
    if need_public and not _drive_ready():
        raise RefError("Seedance can only read videos from a public link. To use a local video, set up Google "
                       "Drive on the PC (setup_mobile.bat) so a temporary link can be made, or give a public URL.")
    return {"local": str(p), "label": str(p)}


def _public_link(file_id: str) -> str:
    return f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"


async def _check_public(url: str) -> None:
    """Make sure the temporary link really serves the video without a login (sharing takes a moment)."""
    httpx = seedance.httpx

    last = ""
    for _ in range(6):
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
                r = await c.get(url, headers={"Range": "bytes=0-1023"})
            ctype = r.headers.get("content-type", "")
            if r.status_code in (200, 206) and "text/html" not in ctype:
                return
            last = f"HTTP {r.status_code} {ctype}"
        except httpx.HTTPError as e:
            last = str(e)
        await asyncio.sleep(2)
    raise RuntimeError(f"the temporary Drive link is not publicly readable ({last}). Your Google account may block "
                       "'anyone with the link' sharing; use a public URL instead.")


async def _video_urls(videos: list) -> tuple[list[str], callable]:
    """Turn prepared video refs into URLs Seedance can download. Returns (urls, cleanup)."""
    urls: list[str] = []
    undo: list = []

    def cleanup() -> None:
        for fn in reversed(undo):
            try:
                fn()
            except Exception as e:
                log.warning("temporary Drive link cleanup failed: %s", e)

    if all(isinstance(v, str) for v in videos):
        return list(videos), cleanup
    drive = await asyncio.to_thread(_drive_client)
    try:
        folder = None
        for v in videos:
            if isinstance(v, str):
                urls.append(v)
                continue
            if "local" in v:
                if folder is None:
                    root = os.environ.get("GDRIVE_FOLDER_ID", "").strip() or (await asyncio.to_thread(
                        drive.ensure_folder, os.environ.get("GDRIVE_ROOT_NAME", "openai_image_mcp"), "root"))["id"]
                    folder = (await asyncio.to_thread(drive.ensure_folder, "temp_video_refs", root))["id"]
                p = Path(v["local"])
                mime = {".mov": "video/quicktime", ".webm": "video/webm"}.get(p.suffix.lower(), "video/mp4")
                f = await asyncio.to_thread(drive.upload, p.read_bytes(), f"{uuid.uuid4().hex[:8]}_{p.name}", mime,
                                            folder, {"kind": "temp_video_ref"})
                undo.append(lambda fid=f["id"]: drive.delete(fid))
                await asyncio.to_thread(drive.share_anyone, f["id"])
                fid = f["id"]
            else:
                fid = v["drive"]
                perm = await asyncio.to_thread(drive.share_anyone, fid)
                undo.append(lambda fid=fid, perm=perm: drive.unshare(fid, perm))
            url = _public_link(fid)
            await _check_public(url)
            log.info("temporary public link for %s", v.get("label"))
            urls.append(url)
    except Exception:
        await asyncio.to_thread(cleanup)
        raise
    return urls, cleanup


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
    want_gemini = (provider or "").lower() == "gemini"
    videos = [_resolve_video_ref(v, need_public=not want_gemini) for v in (reference_videos or []) if v and v.strip()]
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
        if len(videos) > 1 or (videos and continued):
            raise RefError("Gemini edits one video at a time: give one reference video, or continue_from, not both.")
        if videos and not (isinstance(videos[0], dict) and "local" in videos[0]):
            raise RefError("For Gemini, the reference video must be a local file or the code of an earlier video "
                           "(it is uploaded to Gemini directly).")
        if videos:
            dur = _video_seconds(Path(videos[0]["local"]))
            if dur and dur > gemini.VIDEO_MAX_INPUT_SECONDS + 0.05:
                raise RefError(f"Gemini edits clips of at most {gemini.VIDEO_MAX_INPUT_SECONDS}s "
                               f"(this one is {dur:.1f}s). Trim it first.")
            spec["task"] = task or "edit"
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
        if age_h <= SEEDANCE_URL_HOURS:
            first: str | dict = continued["video_url"]
        elif _drive_ready():  # link expired: hand Seedance the local copy through a temporary Drive link
            first = {"local": continued["path"], "label": continue_from}
        else:
            hint = (f" Use its last frame `{continued['last_frame_code']}` as the first image of a new video instead."
                    if continued.get("last_frame_code") else "")
            raise RefError(f"The Seedance link of {continue_from} has expired ({age_h:.0f}h old, links last ~24h) "
                           "and Google Drive is not set up for temporary links." + hint)
        spec["videos"] = [first] + videos
        if len(files) and image_mode not in ("auto", "reference"):
            raise RefError("With continue_from, Seedance images can only be references (image_mode 'reference').")
        spec["image_mode"] = "reference"
    try:
        placeholders = [v if isinstance(v, str) else "https://check.invalid/v.mp4" for v in spec["videos"]]
        seedance.build_body(prompt, files, placeholders, model="check", ratio=aspect_ratio, resolution=resolution,
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
        vids = []
        for v in spec["videos"]:  # one local clip to edit
            vp = Path(v["local"])
            vids.append((vp.name, vp.read_bytes(),
                         {".mov": "video/quicktime", ".webm": "video/webm"}.get(vp.suffix.lower(), "video/mp4")))
        data, interaction = await _render_video(spec["prompt"], spec["files"], spec["aspect_ratio"],
                                                spec["resolution"], spec["previous"], spec["task"], vids)
        return data, {**common, "model": gemini.VIDEO_MODEL if BACKEND != "fake" else "fake",
                      "interaction_id": interaction}

    if BACKEND == "fake":
        await asyncio.sleep(FAKE_DELAY)
        r = {"video": b"FAKE MP4 " + spec["prompt"][:200].encode(), "task_id": "fake_" + uuid.uuid4().hex[:8],
             "model": "fake", "video_url": "https://example.invalid/fake.mp4", "duration": spec["duration"],
             "ratio": spec["aspect_ratio"], "resolution": spec["resolution"], "seed": None,
             "last_frame": _fake_image("LAST FRAME " + spec["prompt"], "1024x576")}
    else:
        urls, cleanup = await _video_urls(spec["videos"])
        try:
            r = await seedance.render_video(spec["prompt"], spec["files"], urls, ratio=spec["aspect_ratio"],
                                            resolution=spec["resolution"], duration=spec["duration"],
                                            generate_audio=spec["generate_audio"], image_mode=spec["image_mode"],
                                            fast=prov == "seedance-fast",
                                            on_task=lambda t: log.info("seedance task %s started", t))
        finally:
            await asyncio.to_thread(cleanup)
    meta = {**common, "model": r["model"], "task_id": r["task_id"], "video_url": r["video_url"],
            "duration": r["duration"], "aspect_ratio": r["ratio"], "resolution": r["resolution"], "seed": r["seed"],
            "generate_audio": spec["generate_audio"], "image_mode": spec["image_mode"],
            "reference_videos": [v if isinstance(v, str) else v.get("label") for v in spec["videos"]]}
    _save_last_frame(r, meta, spec["prompt"], prov, request)
    return r["video"], meta


def _save_last_frame(r: dict, meta: dict, prompt: str, prov: str, request: str | None) -> None:
    if r.get("last_frame"):
        try:
            im = PILImage.open(io.BytesIO(r["last_frame"]))
            fmt = "jpeg" if (im.format or "").lower() == "jpeg" else "png"
            lf = _save(r["last_frame"], fmt, {"kind": "last_frame", "prompt": "last frame of: " + prompt,
                                              "model": r["model"], "provider": prov, "task_id": r["task_id"]},
                       (request + "_last") if request else None)
            meta["last_frame_code"] = lf["id"][:8]
            meta["last_frame_path"] = lf["path"]
        except Exception as e:  # the video itself is what matters
            log.warning("could not save the last frame of %s: %s", r["task_id"], e)



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
  images for character / product consistency (image_mode "reference"), reference videos for motion,
  camera or editing, sound on/off (generate_audio).
- Video-to-video (e.g. turn a gray Maya playblast into photoreal footage): put the local .mp4 path in
  reference_videos, keep duration close to the source length, aspect_ratio matching the source, and say in
  the prompt "Use the reference video as the exact motion, camera and structure reference: keep the same
  mechanical design, proportions, joint layout and timing; only change materials, lighting and
  environment to ...". Local files go to Seedance through a temporary public Drive link that is removed
  afterwards; tell the user that once.
- Gemini can also EDIT one of the user's own clips (<= 10 s): provider "gemini" + reference_videos=[local
  path]. The clip is the source of truth for framing, motion and timing; the prompt says only what to
  change (e.g. "make it photoreal ... keep everything else exactly the same"). Often the most faithful
  option for restyling a playblast.
- check_setup tests the keys and the Seedance connection without spending credit.
- If a Seedance video FAILED with a network / timeout error, the task usually still finishes on
  BytePlus: call recover_video (no task id = newest unsaved task) instead of generating again. Returns the last frame as an image code too —
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
    _hot_reload()
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
    _hot_reload()
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
    _hot_reload()
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
    reference_videos: Annotated[list[str] | None, Field(description="Seedance only: up to 3 videos for motion / "
                                                                    "camera / structure reference or video-to-video "
                                                                    "restyling: a local .mp4/.mov path (e.g. a Maya "
                                                                    "playblast), the code of an earlier video, a "
                                                                    "Google Drive link, or a public http(s) URL. "
                                                                    "Local / Drive files get a temporary public Drive "
                                                                    "link that is removed after the task. Gemini: "
                                                                    "one local clip <= 10 s, edited directly (keeps "
                                                                    "its framing and motion).")] = None,
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
    _hot_reload()
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
async def recover_video(
    task_id: Annotated[str | None, Field(description="Seedance task id (cgt-...). Omit to fetch the newest finished "
                                                     "Seedance task that is not saved here yet.")] = None,
    filename: Annotated[str | None, Field(description="Optional file name (no extension) for the .mp4.")] = None,
) -> list:
    """Fetch a Seedance video whose wait was interrupted (network error, timeout, server restart) without paying again."""
    _hot_reload()
    if BACKEND == "fake":
        return ["fake backend"]
    saved = {m.get("task_id") for m in _index_all() if m.get("task_id")}
    try:
        if task_id:
            data = await seedance.get_task(task_id)
        else:
            items = await seedance.list_tasks(page_size=20)
            items = [t for t in items if t.get("id") not in saved]
            if not items:
                return ["No unsaved Seedance task found (every recent task is already saved here)."]
            items.sort(key=lambda t: t.get("created_at") or 0, reverse=True)
            data = items[0]
            task_id = data["id"]
        status = str(data.get("status", "")).lower()
        if task_id in saved:
            m = next(m for m in _index_all() if m.get("task_id") == task_id)
            return [f"{task_id} is already saved: code `{m['id'][:8]}` | {m['path']}"]
        if status in ("queued", "running"):
            return [f"{task_id} is still {status}. Call recover_video again in a minute (do not start a new video)."]
        if status != "succeeded":
            err = data.get("error") or {}
            return [f"{task_id} ended as {status}: {err.get('code', '')} {err.get('message', '')}".strip()]
        r = await seedance.collect(task_id, data)
    except (seedance.SeedanceError, Exception) as e:
        return [f"Could not recover: {type(e).__name__}: {e}"]
    prov = "seedance-fast" if r["model"] == seedance.MODEL_FAST else "seedance"
    meta = {"media": "video", "provider": prov, "model": r["model"], "task_id": task_id, "video_url": r["video_url"],
            "duration": r["duration"], "aspect_ratio": r["ratio"], "resolution": r["resolution"], "seed": r["seed"],
            "recovered": True}
    prompt = f"(recovered Seedance task {task_id})"
    _save_last_frame(r, meta, prompt, prov, filename)
    m = _save(r["video"], "mp4", {"kind": "video", "prompt": prompt, **meta}, filename)
    extra = f" | last frame image `{meta['last_frame_code']}`" if meta.get("last_frame_code") else ""
    return [f"Recovered {task_id} ({r['model']}, {r['duration']}s){extra} | code `{m['id'][:8]}` | saved: {m['path']}"]


@mcp.tool()
async def check_setup() -> str:
    """Check which providers work: API keys, a free Seedance connection test, Google Drive, server version."""
    _hot_reload()
    key, src = _api_key()
    gkey, gsrc = gemini.api_key()
    lines = [
        f"server version: {VERSION}",
        f"OpenAI (images): {'ok — ' + src if key else 'MISSING — ' + src}",
        f"Gemini (images + video): {'ok — ' + gsrc if gkey else 'MISSING — ' + gsrc}",
        f"Seedance (video): {await seedance.probe() if BACKEND != 'fake' else 'fake backend'}",
        f"Google Drive (temporary links for local reference videos, mobile worker): "
        f"{'ready — ' + str(GDRIVE_TOKEN) if _drive_ready() else 'not set up (run setup_mobile.bat)'}",
        f"default video provider: {DEFAULT_VIDEO_PROVIDER}; output folder: {OUTPUT_DIR}",
    ]
    return "\n".join(lines)


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
