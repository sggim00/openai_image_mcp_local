#!/usr/bin/env bash
# macOS / Linux installer
set -e
cd "$(dirname "$0")"
DIR="$(pwd)"

[ -d .venv ] || python3 -m venv .venv
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt
[ -f .env ] || [ ! -f .env.example ] || cp .env.example .env

cat <<EOF

Done.
 1) Put your OpenAI key in: $DIR/.env
 2) Claude Desktop config (macOS: ~/Library/Application Support/Claude/claude_desktop_config.json):

    "openai-image-local": {
      "command": "$DIR/.venv/bin/python",
      "args": ["$DIR/server.py"]
    }

 3) Or for Claude Code:
    claude mcp add openai-image-local -s user -- "$DIR/.venv/bin/python" "$DIR/server.py"

 Then fully quit and restart Claude Desktop.
EOF
