"""Get a Google Drive refresh token for the mobile worker (run once on this PC).

    python get_drive_token.py [client_secret_xxx.json] [--out .gdrive_token.json]

1. Google Cloud console -> APIs & Services -> enable "Google Drive API".
2. OAuth consent screen: External, add yourself as a test user, then "Publish app"
   (In production). Unpublished "Testing" apps get refresh tokens that expire in 7 days.
3. Credentials -> Create credentials -> OAuth client ID -> "Desktop app" -> download JSON.
4. Run this script with that JSON. A browser opens; sign in and allow access
   (the "Google hasn't verified this app" screen is expected for a personal app:
   Advanced -> Go to ... (unsafe)).
5. The token is saved to .gdrive_token.json next to this script (keep it private).

Standard library only. Nothing is sent anywhere except Google's OAuth endpoints.
"""

from __future__ import annotations

import argparse
import base64
import glob
import hashlib
import http.server
import json
import os
import secrets
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/drive"  # read images you put in Drive + write results


def load_client(path: str) -> tuple[str, str]:
    data = json.load(open(path, encoding="utf-8"))
    c = data.get("installed") or data.get("web") or data
    if "client_id" not in c or "client_secret" not in c:
        raise SystemExit(f"{path} does not look like an OAuth client JSON (Desktop app).")
    return c["client_id"], c["client_secret"]


def run_flow(client_id: str, client_secret: str, open_browser=webbrowser.open,
             auth_url: str = AUTH_URL, token_url: str = TOKEN_URL, timeout: float = 300) -> dict:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" not in q and "error" not in q:
                self.send_response(404); self.end_headers(); return
            if q.get("state", [""])[0] != state:
                result["error"] = "state mismatch"
            else:
                result.update({k: v[0] for k, v in q.items()})
            ok = "code" in result
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = "완료! 이 창은 닫아도 됩니다." if ok else f"실패: {result.get('error')}"
            self.wfile.write(f"<h2 style='font-family:sans-serif'>{msg}</h2>".encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{srv.server_address[1]}/"
    url = auth_url + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
        "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    t = threading.Thread(target=srv.handle_request, daemon=True)
    srv.timeout = timeout
    t.start()
    print("Opening the browser for Google sign-in. If it does not open, visit:\n" + url + "\n", file=sys.stderr)
    open_browser(url)
    t.join(timeout + 5)
    srv.server_close()
    if "code" not in result:
        raise SystemExit(f"No authorization code received ({result.get('error', 'timed out')}).")

    body = urllib.parse.urlencode({
        "code": result["code"], "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect, "grant_type": "authorization_code", "code_verifier": verifier,
    }).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(token_url, data=body), timeout=60) as r:
            tok = json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Token exchange failed: {e.code} {e.read().decode()[:300]}")
    if "refresh_token" not in tok:
        raise SystemExit("Google returned no refresh_token. Remove the app's access at "
                         "https://myaccount.google.com/permissions and run this again.")
    return tok


def main(argv: list[str] | None = None, **flow_kw) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("client_json", nargs="?", help="OAuth client JSON (Desktop app). Default: client_secret*.json here")
    ap.add_argument("--out", default=os.environ.get("GDRIVE_TOKEN_FILE") or os.path.join(here, ".gdrive_token.json"),
                    help="Where to save the credentials (default: .gdrive_token.json next to this script)")
    a = ap.parse_args(argv)
    path = a.client_json or next(iter(sorted(glob.glob(os.path.join(here, "client_secret*.json")) +
                                             glob.glob("client_secret*.json"))), None)
    if not path or not os.path.exists(path):
        ap.error("pass the OAuth client JSON downloaded from Google Cloud (Desktop app)")
    cid, secret = load_client(path)
    tok = run_flow(cid, secret, **flow_kw)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump({"client_id": cid, "client_secret": secret, "refresh_token": tok["refresh_token"]}, f, indent=2)
    print(f"\nGoogle Drive access saved to {a.out} (keep it private).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
