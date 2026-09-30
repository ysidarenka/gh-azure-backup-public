"""End-to-end test: fake GitHub API (file:// git remotes) + Azurite blob emulator.

    npm install -g azurite && pip install -r requirements.txt && python tests/e2e_local.py

Scenario: back up -> nothing changed -> delete a repo -> restore it (with issues)
-> refs identical to the original -> refuse to overwrite -> delete another, restore --all
-> verify -> list.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
WORK = Path(tempfile.mkdtemp(prefix="ghbackup-e2e-"))
REMOTES = WORK / "remotes"
os.environ["GH_GIT_URL"] = REMOTES.as_uri()  # must be set before ghbackup.git_ops is imported
sys.path.insert(0, str(ROOT))

AZURITE = ("DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
           "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;"
           "BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;")
USER = "octo"
REPOS: dict[str, dict] = {}
CREATED = {"issues": [], "comments": [], "closed": [], "labels": []}
ISSUES = [
    {"number": 1, "title": "Bug A", "body": "broken", "state": "open", "user": {"login": USER},
     "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-02T00:00:00Z", "labels": [{"name": "bug"}]},
    {"number": 2, "title": "PR", "state": "closed", "user": {"login": USER}, "pull_request": {},
     "created_at": "2026-01-03T00:00:00Z", "updated_at": "2026-01-03T00:00:00Z", "labels": []},
    {"number": 3, "title": "Idea", "body": "", "state": "closed", "state_reason": "completed",
     "user": {"login": "friend"}, "created_at": "2026-02-01T00:00:00Z", "updated_at": "2026-02-01T00:00:00Z",
     "labels": []},
]
COMMENTS = [{"id": 10, "issue_url": "https://api.github.com/repos/octo/notes/issues/1", "body": "same here",
             "user": {"login": "friend"}, "created_at": "2026-01-02T00:00:00Z"}]


def sh(*args, cwd=None) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


def bare(name: str) -> Path:
    return REMOTES / USER / f"{name}.git"


def register(name: str, private=True) -> None:
    REPOS[name] = {"name": name, "full_name": f"{USER}/{name}", "clone_url": bare(name).as_uri(),
                   "private": private, "description": f"{name} repo", "default_branch": "main",
                   "topics": ["personal"], "has_issues": True, "has_wiki": False, "fork": False,
                   "pushed_at": datetime.now(timezone.utc).isoformat()}


def make_repo(name: str, commits: int) -> None:
    sh("git", "init", "--bare", "-q", "-b", "main", str(bare(name)))
    if commits:
        wc = WORK / "wc" / name
        sh("git", "clone", "-q", str(bare(name)), str(wc))
        for i in range(commits):
            (wc / f"f{i}.md").write_text(f"{name} {i}\n")
            sh("git", "add", ".", cwd=wc)
            sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", f"c{i}", cwd=wc)
        sh("git", "tag", "v1", cwd=wc)
        sh("git", "checkout", "-qb", "draft", cwd=wc)
        sh("git", "push", "-q", "origin", "main", "draft", "--tags", cwd=wc)
    register(name)


class FakeGitHub(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code=200, body=None):
        data = json.dumps(body if body is not None else {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path.strip("/").split("/")
        since = parse_qs(u.query).get("since", [""])[0]
        if p == ["user"]:
            return self.reply(body={"login": USER})
        if p == ["user", "repos"]:
            return self.reply(body=list(REPOS.values()))
        if p[0] == "repos" and len(p) == 3:
            r = REPOS.get(p[2])
            if not r:
                return self.reply(404, {"message": "Not Found"})
            size = 1 if sh("git", "ls-remote", str(bare(p[2]))).strip() else 0
            return self.reply(body={**r, "size": size})
        if p[0] == "repos" and len(p) >= 4:
            tail = "/".join(p[3:])
            if tail == "issues":
                return self.reply(body=[i for i in ISSUES if i["updated_at"] >= since] if p[2] == "notes" else [])
            if tail == "issues/comments":
                return self.reply(body=[c for c in COMMENTS if c["created_at"] >= since] if p[2] == "notes" else [])
            if tail == "labels":
                return self.reply(body=[{"name": "bug", "color": "d73a4a"}])
            return self.reply(body=[])
        return self.reply(404)

    def do_POST(self):
        p = urlparse(self.path).path.strip("/").split("/")
        b = self.body()
        if p == ["user", "repos"]:
            sh("git", "init", "--bare", "-q", "-b", "main", str(bare(b["name"])))
            register(b["name"], b.get("private", True))
            return self.reply(201, REPOS[b["name"]])
        if p[-1] == "labels":
            CREATED["labels"].append(b["name"])
            return self.reply(201, b)
        if p[-1] == "issues":
            CREATED["issues"].append(b)
            return self.reply(201, {"number": len(CREATED["issues"])})
        if p[-1] == "comments":
            CREATED["comments"].append(b)
            return self.reply(201, b)
        return self.reply(404)

    def do_PATCH(self):
        b = self.body()
        if b.get("state") == "closed":
            CREATED["closed"].append(self.path)
        return self.reply(200, b)

    do_PUT = do_PATCH


def main() -> int:
    azurite = subprocess.Popen(["azurite-blob", "--silent", "--loose", "--skipApiVersionCheck",
                                "--location", str(WORK / "azurite")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeGitHub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ.update(GH_API_URL=f"http://127.0.0.1:{srv.server_port}", GH_BACKUP_TOKEN="t", GH_RESTORE_TOKEN="t",
                      AZURE_STORAGE_CONNECTION_STRING=AZURITE)
    try:
        for _ in range(100):
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", 10000)) == 0:
                    break
            time.sleep(0.3)
        from azure.storage.blob import BlobServiceClient

        svc = BlobServiceClient.from_connection_string(AZURITE)
        for c in ("github-backups", "backup-state"):
            svc.create_container(c)
        import ghbackup.__main__ as cli

        make_repo("notes", 4)
        make_repo("dotfiles", 2)
        make_repo("empty", 0)
        original = sh("git", "ls-remote", str(bare("notes")))
        dot_original = sh("git", "ls-remote", str(bare("dotfiles")))

        def manifest():
            c = svc.get_container_client("github-backups")
            last = sorted(b.name for b in c.list_blobs(name_starts_with="_runs/"))[-1]
            return {r["repo"]: r for r in json.loads(c.download_blob(last).readall())["results"]}

        common = ["--account", "unused"]
        assert cli.main(common + ["backup", "--tier", "none", "--workdir", str(WORK / "w")]) == 0
        m = manifest()
        assert m["octo/notes"]["code"] == "backed_up" and m["octo/empty"]["code"] == "empty", m
        assert any(k.endswith("/repo.json") for k in m["octo/notes"]["objects"])
        assert m["octo/notes"]["metadata"]["issues"] == 3
        print("✔ backup 1: all repos copied")

        time.sleep(1.1)
        assert cli.main(common + ["backup", "--tier", "none", "--workdir", str(WORK / "w")]) == 0
        assert all(not r["objects"] for r in manifest().values())
        print("✔ backup 2: nothing changed, nothing uploaded")

        # Simulate "I deleted my repo"
        shutil.rmtree(bare("notes"))
        del REPOS["notes"]
        assert cli.main(common + ["restore", "notes", "--with-issues"]) == 0
        restored = sh("git", "ls-remote", str(bare("notes")))
        pick = lambda s: sorted(x for x in s.splitlines() if "refs/heads/" in x or "refs/tags/" in x)  # noqa: E731
        assert pick(original) == pick(restored), (original, restored)
        assert REPOS["notes"]["private"] is True
        assert len(CREATED["issues"]) == 2 and "originally #1" in CREATED["issues"][0]["body"]
        assert CREATED["issues"][0]["labels"] == ["bug"] and CREATED["labels"] == ["bug"]
        assert len(CREATED["comments"]) == 1 and len(CREATED["closed"]) == 1
        print("✔ deleted repo restored: branches+tags identical, 2 issues (PR skipped), 1 comment, 1 closed")

        try:
            cli.main(common + ["restore", "notes"])
            raise AssertionError("should refuse to overwrite a non-empty repo")
        except SystemExit as e:
            assert "already exists" in str(e)
        print("✔ refuses to overwrite an existing repo")

        shutil.rmtree(bare("dotfiles"))
        del REPOS["dotfiles"]
        assert cli.main(common + ["restore", "--all"]) == 0
        assert pick(dot_original) == pick(sh("git", "ls-remote", str(bare("dotfiles"))))
        try:
            cli.main(common + ["restore", "--all", "--as", "x"])
            raise AssertionError("--as must be rejected with --all")
        except SystemExit as e:
            assert e.code == 2
        print("✔ restore --all: missing repo restored, existing and empty repos skipped")

        assert cli.main(common + ["verify", "--all"]) == 0
        assert cli.main(common + ["list"]) == 0
        assert cli.main(common + ["list", "octo/notes"]) == 0
        assert cli.main(common + ["restore", "dotfiles", "--local", str(WORK / "local")]) == 0
        assert (WORK / "local" / "repo.git" / "HEAD").exists()
        print("\nALL E2E CHECKS PASSED")
        return 0
    finally:
        srv.shutdown()
        azurite.terminate()


if __name__ == "__main__":
    sys.exit(main())
