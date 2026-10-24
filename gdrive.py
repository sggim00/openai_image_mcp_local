"""Minimal Google Drive v3 client (standard library only) for the mobile worker.

Auth: an OAuth refresh token for your own Google account, stored in .gdrive_token.json
by get_drive_token.py. The full `drive` scope is needed because request files are created
by Claude's Google Drive connector (another app), which narrower scopes cannot see.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

FOLDER_MIME = "application/vnd.google-apps.folder"
FIELDS = "id,name,mimeType,createdTime,modifiedTime,webViewLink,appProperties,parents,size"


class DriveError(Exception):
    def __init__(self, msg: str, status: int = 0):
        super().__init__(msg)
        self.status = status


def q(s: str) -> str:
    """Escape a string literal for a Drive query."""
    return s.replace("\\", "\\\\").replace("'", "\\'")


def load_credentials(path: Path) -> dict | None:
    if not path.is_file():
        return None
    d = json.loads(path.read_text(encoding="utf-8"))
    if all(d.get(k) for k in ("client_id", "client_secret", "refresh_token")):
        return d
    return None


class Drive:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str, timeout: float = 120):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.timeout = timeout
        base = os.environ.get("GDRIVE_API_BASE", "https://www.googleapis.com").rstrip("/")
        self.api = base + "/drive/v3"
        self.upload_api = base + "/upload/drive/v3/files"
        self.token_url = os.environ.get("GDRIVE_TOKEN_URL", "https://oauth2.googleapis.com/token")
        self._token: str | None = None
        self._token_exp = 0.0

    @classmethod
    def from_file(cls, path: Path) -> "Drive":
        cred = load_credentials(path)
        if not cred:
            raise DriveError(f"No Google Drive credentials in {path}. Run get_drive_token.py first.")
        return cls(cred["client_id"], cred["client_secret"], cred["refresh_token"])

    # ------------------------------------------------------------------ http
    def _access_token(self, force: bool = False) -> str:
        if not force and self._token and time.time() < self._token_exp - 60:
            return self._token
        body = urllib.parse.urlencode({
            "client_id": self.client_id, "client_secret": self.client_secret,
            "refresh_token": self.refresh_token, "grant_type": "refresh_token",
        }).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(self.token_url, data=body), timeout=60) as r:
                d = json.loads(r.read())
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors="ignore")[:300]
            hint = (" The refresh token expired or was revoked: run get_drive_token.py again "
                    "(and publish the OAuth app so tokens stop expiring after 7 days).") if "invalid_grant" in text else ""
            raise DriveError(f"Google token refresh failed ({e.code}): {text}.{hint}", e.code)
        self._token = d["access_token"]
        self._token_exp = time.time() + float(d.get("expires_in", 3600))
        return self._token

    def request(self, method: str, url: str, params: dict | None = None, body: bytes | None = None,
                content_type: str | None = None, raw: bool = False):
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        last: Exception | None = None
        force = False
        for attempt in range(4):
            headers = {"Authorization": f"Bearer {self._access_token(force)}"}
            if content_type:
                headers["Content-Type"] = content_type
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = r.read()
                return data if raw else (json.loads(data) if data else {})
            except urllib.error.HTTPError as e:
                text = e.read().decode(errors="ignore")[:400]
                last = DriveError(f"Drive {method} {url.split('?')[0]} -> {e.code}: {text}", e.code)
                if e.code == 401 and not force:
                    force = True
                    continue
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise last
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = DriveError(f"Drive {method} {url.split('?')[0]}: {e}")
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise last
        raise last  # pragma: no cover

    # ------------------------------------------------------------------ files
    def list(self, query: str, page_size: int = 100, order: str = "createdTime") -> list[dict]:
        out, token = [], None
        while True:
            params = {"q": query, "pageSize": page_size, "orderBy": order, "fields": f"nextPageToken,files({FIELDS})",
                      "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
            if token:
                params["pageToken"] = token
            d = self.request("GET", f"{self.api}/files", params)
            out += d.get("files", [])
            token = d.get("nextPageToken")
            if not token or len(out) >= page_size:
                return out[:page_size] if page_size else out

    def meta(self, file_id: str) -> dict:
        return self.request("GET", f"{self.api}/files/{file_id}", {"fields": FIELDS, "supportsAllDrives": "true"})

    def download(self, file_id: str) -> bytes:
        return self.request("GET", f"{self.api}/files/{file_id}", {"alt": "media", "supportsAllDrives": "true"}, raw=True)

    def export_text(self, file_id: str) -> bytes:
        return self.request("GET", f"{self.api}/files/{file_id}/export", {"mimeType": "text/plain"}, raw=True)

    def create_folder(self, name: str, parent: str) -> dict:
        body = json.dumps({"name": name, "mimeType": FOLDER_MIME, "parents": [parent]}).encode()
        return self.request("POST", f"{self.api}/files", {"fields": FIELDS, "supportsAllDrives": "true"},
                            body, "application/json; charset=UTF-8")

    def find_folder(self, name: str, parent: str) -> dict | None:
        hits = self.list(f"name = '{q(name)}' and mimeType = '{FOLDER_MIME}' and '{parent}' in parents "
                         "and trashed = false", page_size=1)
        return hits[0] if hits else None

    def ensure_folder(self, name: str, parent: str) -> dict:
        return self.find_folder(name, parent) or self.create_folder(name, parent)

    def upload(self, data: bytes, name: str, mime: str, parent: str, props: dict | None = None,
               description: str = "") -> dict:
        meta: dict = {"name": name, "parents": [parent], "mimeType": mime}
        if props:
            meta["appProperties"] = {k: str(v)[:100] for k, v in props.items()}
        if description:
            meta["description"] = description[:4000]
        boundary = "oaimcp" + os.urandom(12).hex()
        body = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(meta, ensure_ascii=False)}\r\n--{boundary}\r\nContent-Type: {mime}\r\n\r\n"
        ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
        return self.request("POST", self.upload_api,
                            {"uploadType": "multipart", "fields": FIELDS, "supportsAllDrives": "true"},
                            body, f"multipart/related; boundary={boundary}")

    def update_content(self, file_id: str, data: bytes, mime: str) -> dict:
        return self.request("PATCH", f"{self.upload_api}/{file_id}",
                            {"uploadType": "media", "fields": FIELDS, "supportsAllDrives": "true"}, data, mime)

    def share_anyone(self, file_id: str) -> str:
        """Let anyone with the link read the file. Returns the permission id (for unshare)."""
        body = json.dumps({"type": "anyone", "role": "reader"}).encode()
        d = self.request("POST", f"{self.api}/files/{file_id}/permissions", {"supportsAllDrives": "true"},
                         body, "application/json; charset=UTF-8")
        return d.get("id", "anyoneWithLink")

    def unshare(self, file_id: str, permission_id: str) -> None:
        self.request("DELETE", f"{self.api}/files/{file_id}/permissions/{permission_id}", {"supportsAllDrives": "true"})

    def delete(self, file_id: str) -> None:
        self.request("DELETE", f"{self.api}/files/{file_id}", {"supportsAllDrives": "true"})

    def update(self, file_id: str, add_parent: str | None = None, remove_parent: str | None = None,
               props: dict | None = None) -> dict:
        params = {"fields": FIELDS, "supportsAllDrives": "true"}
        if add_parent:
            params["addParents"] = add_parent
        if remove_parent:
            params["removeParents"] = remove_parent
        body = json.dumps({"appProperties": props} if props else {}).encode()
        return self.request("PATCH", f"{self.api}/files/{file_id}", params, body, "application/json; charset=UTF-8")
