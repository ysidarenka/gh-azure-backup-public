"""GitHub access: a fine-grained PAT, the local `gh` CLI login, or a GitHub App.

Token sources, first match wins:
  1. GH_BACKUP_TOKEN (GH_RESTORE_TOKEN for restore) -> fine-grained personal access token
  2. GH_APP_ID + GH_APP_PRIVATE_KEY  -> GitHub App installed on your account (backup only;
                                        short-lived tokens, refreshed automatically)
  3. `gh auth token`                 -> whatever you are logged in as locally (nothing stored)
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from datetime import datetime
from typing import Iterator

import jwt
import requests

log = logging.getLogger(__name__)
API = os.environ.get("GH_API_URL", "https://api.github.com").rstrip("/")


class TokenAuth:
    kind = "token"

    def __init__(self, token: str):
        self._token = token

    def token(self) -> str:
        return self._token


class AppAuth:
    kind = "app"

    def __init__(self, app_id: str, private_key: str, owner: str | None):
        self.app_id, self.key, self.owner = str(app_id), private_key, owner
        self._tok: tuple[str, float] | None = None
        self._lock = threading.Lock()

    def _jwt(self) -> str:
        now = int(time.time())
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self.app_id}, self.key, algorithm="RS256")

    def _installation_id(self) -> int:
        h = {"Authorization": f"Bearer {self._jwt()}", "Accept": "application/vnd.github+json"}
        if self.owner:
            for path in (f"/users/{self.owner}/installation", f"/orgs/{self.owner}/installation"):
                r = requests.get(API + path, headers=h, timeout=30)
                if r.ok:
                    return r.json()["id"]
        r = requests.get(API + "/app/installations", headers=h, timeout=30)
        r.raise_for_status()
        if not r.json():
            raise RuntimeError("The GitHub App is not installed on any account")
        return r.json()[0]["id"]

    def token(self) -> str:
        with self._lock:
            if self._tok and self._tok[1] - time.time() > 600:
                return self._tok[0]
            r = requests.post(f"{API}/app/installations/{self._installation_id()}/access_tokens",
                              headers={"Authorization": f"Bearer {self._jwt()}",
                                       "Accept": "application/vnd.github+json"}, timeout=30)
            r.raise_for_status()
            body = r.json()
            self._tok = (body["token"], datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00")).timestamp())
            return body["token"]


def auth_from_env(owner: str | None = None, token_env: str = "GH_BACKUP_TOKEN"):
    if os.environ.get(token_env):
        return TokenAuth(os.environ[token_env])
    if token_env == "GH_BACKUP_TOKEN" and os.environ.get("GH_APP_ID") and os.environ.get("GH_APP_PRIVATE_KEY"):
        return AppAuth(os.environ["GH_APP_ID"], os.environ["GH_APP_PRIVATE_KEY"], owner)
    try:
        tok = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()
        if tok:
            return TokenAuth(tok)
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass
    raise SystemExit(f"No GitHub credentials: set {token_env}, GH_APP_ID + GH_APP_PRIVATE_KEY, or run `gh auth login`.")


class GitHub:
    def __init__(self, auth):
        self.auth = auth
        self.s = requests.Session()

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self.auth.token()}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28"}

    def request(self, method: str, url: str, **kw) -> requests.Response:
        url = url if url.startswith("http") else API + url
        for attempt in range(8):
            r = self.s.request(method, url, headers=self._h(), timeout=60, **kw)
            limited = r.status_code in (403, 429) and (
                r.headers.get("X-RateLimit-Remaining") == "0" or "Retry-After" in r.headers)
            if limited:
                wait = int(r.headers.get("Retry-After") or
                           max(int(r.headers.get("X-RateLimit-Reset", time.time() + 60)) - time.time(), 1)) + 3
                log.warning("GitHub rate limit; sleeping %ss", wait)
                time.sleep(min(wait, 3600))
                continue
            if r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            return r
        return r

    def get(self, url: str, **params) -> requests.Response:
        return self.request("GET", url, params=params or None)

    def paginate(self, url: str, **params) -> Iterator[dict]:
        params = {"per_page": 100, **params}
        while url:
            r = self.request("GET", url, params=params)
            if r.status_code in (404, 410):
                return
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict):  # e.g. {"total_count": n, "repositories": [...]}
                data = next((v for v in data.values() if isinstance(v, list)), [])
            yield from data
            url, params = r.links.get("next", {}).get("url"), None

    def login(self) -> str | None:
        if self.auth.kind == "app":
            return None
        r = self.get("/user")
        r.raise_for_status()
        return r.json()["login"]

    def list_repos(self, affiliation: str = "owner") -> list[dict]:
        if self.auth.kind == "app":
            return list(self.paginate("/installation/repositories"))
        return list(self.paginate("/user/repos", affiliation=affiliation, sort="full_name"))
