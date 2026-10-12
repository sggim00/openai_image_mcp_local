"""openai_image_mcp_local — OpenAI image generation as a LOCAL (stdio) MCP server.

For Claude Desktop / Claude Code on your own machine. No hosting, no upload page:
reference images are passed as local file paths, codes of earlier results, URLs or
"clipboard", and every result is saved straight into OUTPUT_DIR.

Tools
-----
- generate_image  : prompt (+ optional reference images) -> image
- generate_batch  : several prompts sharing the same references, rendered in parallel
- edit_image      : change an existing image, keeping the rest
- wait_for_images : collect results of jobs that took longer than one tool call
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
async def _render(prompt: str, refs: list[tuple[str, bytes, str]], size: str, quality: str,
                  background: str, output_format: str) -> tuple[bytes, str]:
    if BACKEND == "fake":
        if FAKE_DELAY:
            await asyncio.sleep(FAKE_DELAY)
        tag = f"[{len(refs)} ref] " if refs else ""
        return _fake_image(tag + prompt, "1024x1024" if size == "auto" else size), "png"
    kwargs: dict = dict(model=MODEL, prompt=prompt, size=size, quality=quality, output_format=output_format)
    if background != "auto":
        kwargs["background"] = background
    if refs:
        # The edits endpoint is how reference images reach the model; without it the
        # model only ever sees words.
        r = await openai_client().images.edit(image=refs if len(refs) > 1 else refs[0], **kwargs)
    else:
        r = await openai_client().images.generate(**kwargs)
    return base64.b64decode(r.data[0].b64_json), output_format


JOBS: dict[str, dict] = {}


def _start_job(kind: str, prompt: str, ref_labels: list[str], refs: list[tuple[str, bytes, str]],
               size: str, quality: str, background: str, output_format: str, filename: str | None) -> str:
    job_id = "job_" + uuid.uuid4().hex[:10]

    async def run() -> dict:
        t0 = time.time()
        try:
            data, fmt = await _render(prompt, refs, size, quality, background, output_format)
        except Exception as e:  # surfaced to Claude as text, never raised through MCP
            log.warning("%s failed after %.1fs: %s", job_id, time.time() - t0, e)
            return {"error": f"{type(e).__name__}: {e}", "seconds": time.time() - t0}
        meta = {"kind": kind, "prompt": prompt, "model": MODEL, "size": size, "quality": quality,
                "references": ref_labels, "seconds": round(time.time() - t0, 1)}
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
        texts.append(f"- {j}: done in {m['seconds']}s | code `{m['id'][:8]}` | saved: {m['path']}")
        images.append(Image(data=_preview(res["data"], preview_px), format="jpeg"))

    header = []
    if still:
        header.append(
            f"{len(still)} image(s) still rendering. Tell the user briefly, then call wait_for_images "
            f"with job_ids {json.dumps(still)} (do not start them again)."
        )
    if images:
        header.append(
            "Tell the user where each file was saved. The short code (or the file path) can be used in "
            "reference_images / image in later calls."
        )
    return ["\n".join(header + texts)] + images


# --------------------------------------------------------------------------
# MCP server
# --------------------------------------------------------------------------
INSTRUCTIONS = f"""\
Local server that creates and edits images with OpenAI's {MODEL}. Results are saved to {OUTPUT_DIR}.

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

Tools
- One image: generate_image. Several related images: ONE generate_batch call (renders in parallel).
- Changing an existing result: edit_image with its code or path.
- Rendering takes about 30-120s per image. If a call returns job ids that are still rendering, call
  wait_for_images with those ids; never re-submit the same prompt.
- Default quality is "{DEFAULT_QUALITY}". Use "low" for quick drafts, "high" only for finals.
- Afterwards reply briefly in the user's language with the saved file paths, and point out anything
  that clearly missed the request.
"""

mcp = MCPServer(name="openai-image-local", instructions=INSTRUCTIONS)

Quality = Literal["low", "medium", "high", "auto"]
OutFmt = Literal["png", "jpeg", "webp"]
Bg = Literal["auto", "opaque", "transparent"]
SizeDesc = "'1024x1024' (square), '1536x1024' (landscape), '1024x1536' (portrait) or 'auto'."
RefDesc = ("Reference images the model should see and follow (style / character / product). Each item is an "
           "absolute file path, the code of an earlier result, an http(s) URL, or 'clipboard'.")
NameDesc = "Optional file name (no extension) for the saved image. Default: date + prompt."


@mcp.tool()
async def generate_image(
    prompt: Annotated[str, Field(description="Detailed image prompt, preferably in English.")],
    reference_images: Annotated[list[str] | None, Field(description=RefDesc)] = None,
    size: Annotated[str, Field(description=SizeDesc)] = "1024x1024",
    quality: Annotated[Quality | None, Field(description="low / medium / high / auto. Omit for the default.")] = None,
    background: Annotated[Bg, Field(description="'transparent' for cut-out assets.")] = "auto",
    output_format: Annotated[OutFmt, Field(description="File format of the saved image.")] = "png",
    filename: Annotated[str | None, Field(description=NameDesc)] = None,
) -> list:
    """Create one image, optionally guided by reference images. Saves it locally and returns a preview, or a job id if still rendering."""
    try:
        labels, refs = await _load_refs(reference_images)
    except RefError as e:
        return [str(e)]
    job = _start_job("generate", prompt, labels, refs, size, quality or DEFAULT_QUALITY, background,
                     output_format, filename)
    return await _collect([job], WAIT_SECONDS)


@mcp.tool()
async def generate_batch(
    prompts: Annotated[list[str], Field(description=f"1-{MAX_BATCH} detailed English prompts, one per image.")],
    reference_images: Annotated[list[str] | None, Field(description=RefDesc + " Shared by every prompt.")] = None,
    size: Annotated[str, Field(description=SizeDesc)] = "1024x1024",
    quality: Annotated[Quality | None, Field(description="low / medium / high / auto. Omit for the default.")] = None,
    background: Annotated[Bg, Field(description="'transparent' for cut-out assets.")] = "auto",
    output_format: Annotated[OutFmt, Field(description="File format of the saved images.")] = "png",
) -> list:
    """Create several related images in parallel (e.g. every part of one character, or style variations)."""
    if not 1 <= len(prompts) <= MAX_BATCH:
        return [f"Give between 1 and {MAX_BATCH} prompts."]
    try:
        labels, refs = await _load_refs(reference_images)
    except RefError as e:
        return [str(e)]
    q = quality or DEFAULT_QUALITY
    jobs = [_start_job("generate", p, labels, refs, size, q, background, output_format, None) for p in prompts]
    return await _collect(jobs, WAIT_SECONDS)


@mcp.tool()
async def edit_image(
    instruction: Annotated[str, Field(description="What to change, in English. Also say what must stay the same.")],
    image: Annotated[str, Field(description="The image to edit: file path, code of an earlier result, URL, or 'clipboard'.")],
    reference_images: Annotated[list[str] | None, Field(description="Extra reference images (same formats).")] = None,
    size: Annotated[str, Field(description=SizeDesc)] = "auto",
    quality: Annotated[Quality | None, Field(description="low / medium / high / auto. Omit for the default.")] = None,
    background: Annotated[Bg, Field(description="'transparent' for cut-out assets.")] = "auto",
    output_format: Annotated[OutFmt, Field(description="File format of the saved image.")] = "png",
    filename: Annotated[str | None, Field(description=NameDesc)] = None,
) -> list:
    """Change an existing image while keeping the rest of it. Saves the result locally and returns a preview."""
    try:
        labels, refs = await _load_refs([image] + list(reference_images or []))
    except RefError as e:
        return [str(e)]
    job = _start_job("edit", instruction, labels, refs, size, quality or DEFAULT_QUALITY, background,
                     output_format, filename)
    return await _collect([job], WAIT_SECONDS)


@mcp.tool()
async def wait_for_images(
    job_ids: Annotated[list[str], Field(description="Job ids returned as still rendering.")],
) -> list:
    """Wait for images that were still rendering and return them."""
    return await _collect(job_ids, WAIT_SECONDS)


@mcp.tool()
def list_recent(limit: Annotated[int, Field(ge=1, le=50)] = 10) -> str:
    """List recent results (code, path, prompt) so earlier work can be reused as references."""
    items = [
        {
            "code": m["id"][:8],
            "kind": m.get("kind"),
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
        if key:
            log.info("OpenAI key loaded from %s", source)
        else:
            log.error("No OpenAI API key (%s). Set OPENAI_API_KEY or OPENAI_API_KEY_FILE in %s "
                      "or the MCP config 'env'.", source, HERE / ".env")
    log.info("openai_image_mcp_local: model=%s backend=%s output=%s", MODEL, BACKEND, OUTPUT_DIR)
    mcp.run("stdio")


if __name__ == "__main__":
    main()
