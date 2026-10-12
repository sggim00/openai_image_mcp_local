"""Register openai-image-local in Claude Desktop and check that the server starts.

    .venv\\Scripts\\python.exe register_claude.py

Finds every Claude Desktop config file on this PC (the Microsoft Store / MSIX install keeps
it under %LOCALAPPDATA%\\Packages\\Claude_*\\LocalCache\\Roaming\\Claude, not %APPDATA%),
adds the server entry with correct paths, keeps a .bak copy, and starts server.py once to
confirm it answers. Never overwrites a config file it cannot parse.
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
NAME = "openai-image-local"


def config_files() -> list[Path]:
    found: list[Path] = []
    if sys.platform.startswith("win"):
        appdata, local = os.environ.get("APPDATA", ""), os.environ.get("LOCALAPPDATA", "")
        dirs = [Path(appdata) / "Claude"] if appdata else []
        dirs += [Path(p) for p in glob.glob(os.path.join(local, "Packages", "Claude_*", "LocalCache", "Roaming", "Claude"))]
    elif sys.platform == "darwin":
        dirs = [Path.home() / "Library/Application Support/Claude"]
    else:
        dirs = [Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "Claude"]
    for d in dirs:
        if d.is_dir():  # only where Claude Desktop actually keeps its data
            found.append(d / "claude_desktop_config.json")
    return found


def server_python() -> Path:
    venv = HERE / ".venv" / ("Scripts/python.exe" if sys.platform.startswith("win") else "bin/python")
    return venv if venv.exists() else Path(sys.executable)


def register(cfg: Path, entry: dict) -> str:
    data: dict = {}
    if cfg.exists() and cfg.read_text(encoding="utf-8-sig").strip():
        text = cfg.read_text(encoding="utf-8-sig")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            line = text.splitlines()[e.lineno - 1] if e.lineno <= len(text.splitlines()) else ""
            return (f"NOT CHANGED — the file is not valid JSON (line {e.lineno}, column {e.colno}: {e.msg})\n"
                    f"      > {line.strip()}\n"
                    "      Claude Desktop ignores every server in a broken file. Fix that line (often a missing or extra comma,\n"
                    "      or single backslashes in a Windows path) and run this again.")
        if not isinstance(data, dict):
            return "NOT CHANGED — the file is not a JSON object."
        shutil.copy2(cfg, cfg.with_suffix(".json.bak"))
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        return 'NOT CHANGED — "mcpServers" is not an object.'
    old = servers.get(NAME)
    # earlier hand-written entries under similar names would start a second copy
    stale = [k for k in servers if k != NAME and "openai" in k.lower() and "image" in k.lower()]
    for k in stale:
        servers.pop(k)
    keep_env = (old or {}).get("env")
    servers[NAME] = dict(entry, **({"env": keep_env} if keep_env else {}))
    cfg.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    msg = "updated" if old else "added"
    if stale:
        msg += f" (removed duplicate entries: {', '.join(stale)})"
    return msg


async def self_test(py: Path) -> str:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=str(py), args=[str(HERE / "server.py")], cwd=str(HERE))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await asyncio.wait_for(s.initialize(), 60)
            tools = (await s.list_tools()).tools
    return f"ok — {len(tools)} tools: {', '.join(t.name for t in tools)}"


def main() -> int:
    py = server_python()
    entry = {"command": str(py), "args": [str(HERE / "server.py")]}
    print(f"Server command: {py} {HERE / 'server.py'}")
    if not (HERE / ".venv").exists():
        print("WARNING: .venv not found — run install.bat first.")

    print("\n[1/2] Starting server.py once...")
    t0 = time.time()
    try:
        print("  " + asyncio.run(self_test(py)) + f" ({time.time() - t0:.1f}s)")
    except Exception as e:
        print(f"  FAILED: {type(e).__name__}: {e}")
        print("  Claude Desktop would fail the same way. Run this to see the error:")
        print(f'    "{py}" "{HERE / "server.py"}"')
        return 1

    print("\n[2/2] Claude Desktop config")
    files = config_files()
    if not files:
        print("  Claude Desktop data folder not found. Open Claude Desktop once, then run this again,")
        print("  or use Settings -> Developer -> Edit Config and paste:")
        print(json.dumps({"mcpServers": {NAME: entry}}, indent=2))
        return 1
    ok = True
    for cfg in files:
        result = register(cfg, entry)
        ok &= not result.startswith("NOT CHANGED")
        print(f"  {cfg}\n    {result}")
    print("\nNow quit Claude Desktop completely (tray icon -> Quit, not just the window X) and start it again.")
    print(f"Then '{NAME}' appears under Settings -> Developer, and in the chat's tools menu.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
