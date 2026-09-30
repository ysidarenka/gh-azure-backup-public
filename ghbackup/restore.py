"""Restore repositories (code, branches, tags, LFS, settings, optionally issues) from Azure."""
from __future__ import annotations

import gzip
import json
import logging
import os
import shutil
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from . import git_ops
from .azstore import Store
from .github import GitHub

log = logging.getLogger("ghbackup")


class Skipped(Exception):
    """Nothing to do for this repo (no restore point, or the target already has content)."""


def resolve_key(store: Store, repo: str) -> str:
    keys = [k for k in store.state_keys() if k.startswith("github/")]
    want = repo.strip("/")
    matches = [k for k in keys if k.endswith("/" + want) and (want.count("/") == 1 or k.split("/")[2] == want)]
    if len(matches) != 1:
        raise SystemExit(f"'{repo}' matched {len(matches)} backed-up repos: {matches[:10] or 'none'}. Use owner/repo.")
    return matches[0]


def restore_points(store: Store, key: str) -> list[str]:
    return sorted({n.rsplit("/", 1)[0] for n in store.names(key + "/") if n.endswith("/repo.bundle")})


def _merged_metadata(store: Store, key: str, upto: str) -> dict:
    """Replay full export + nightly deltas up to the chosen restore point."""
    issues, comments, labels = {}, {}, {}
    for blob in sorted(n for n in store.names(key + "/") if n.endswith("/metadata.json.gz")):
        if blob.rsplit("/", 1)[0] > upto:
            break
        fd, name = tempfile.mkstemp(suffix=".json.gz")
        os.close(fd)
        tmp = Path(name)
        store.get_file(blob, tmp)
        doc = json.loads(gzip.decompress(tmp.read_bytes()))
        tmp.unlink()
        issues.update({i["number"]: i for i in doc.get("issues", [])})
        comments.update({c["id"]: c for c in doc.get("issue_comments", [])})
        labels.update({lb["name"]: lb for lb in doc.get("labels", [])})
    return {"issues": issues, "comments": comments, "labels": labels}


def _restore_issues(gh: GitHub, target: str, meta: dict) -> int:
    for lb in meta["labels"].values():
        gh.request("POST", f"/repos/{target}/labels",
                   json={"name": lb["name"], "color": lb.get("color", "ededed"), "description": lb.get("description") or ""})
    by_issue: dict[int, list] = {}
    for c in meta["comments"].values():
        by_issue.setdefault(int(c["issue_url"].rsplit("/", 1)[-1]), []).append(c)
    n = 0
    for num, it in sorted(meta["issues"].items()):
        if "pull_request" in it:
            continue  # PRs cannot be recreated; their commits are in the bundle under refs/pull/*
        note = f"> Restored from backup · originally #{num} by @{it['user']['login']} on {it['created_at'][:10]}\n\n"
        r = gh.request("POST", f"/repos/{target}/issues", json={
            "title": it["title"], "body": note + (it.get("body") or ""),
            "labels": [lb["name"] for lb in it.get("labels", [])]})
        r.raise_for_status()
        new_no = r.json()["number"]
        for c in sorted(by_issue.get(num, []), key=lambda c: c["created_at"]):
            gh.request("POST", f"/repos/{target}/issues/{new_no}/comments", json={
                "body": f"> @{c['user']['login']} on {c['created_at'][:10]}\n\n{c.get('body') or ''}"})
            time.sleep(1)
        if it["state"] == "closed":
            gh.request("PATCH", f"/repos/{target}/issues/{new_no}",
                       json={"state": "closed", "state_reason": it.get("state_reason") or "completed"})
        n += 1
        time.sleep(1)  # stay under GitHub's content-creation limits (80/min)
    return n


def restore(opts, store: Store, gh: GitHub | None) -> int:
    """Restore one repo, several, or (opts.all) every backed-up repo."""
    if opts.all:
        keys = sorted(k for k in store.state_keys() if k.startswith("github/"))
    else:
        keys = list(dict.fromkeys(resolve_key(store, r) for r in opts.repo))
    many = opts.all or len(keys) > 1

    def one(key: str) -> str:
        if opts.local:
            work = Path(opts.local) / key.split("/", 1)[1] if many else Path(opts.local)
        else:
            work = Path(tempfile.mkdtemp(prefix="ghrestore-"))
        try:
            return restore_one(opts, store, gh, key, work)
        finally:
            if not opts.local:
                shutil.rmtree(work, ignore_errors=True)

    if not many:
        try:
            one(keys[0])
        except Skipped as e:
            raise SystemExit(str(e)) from None
        return 0

    results = []
    for key in keys:
        repo = key.split("/", 1)[1]
        try:
            results.append((repo, "restored", one(key)))
        except Skipped as e:
            print(f"Skipped {repo}: {e}")
            results.append((repo, "skipped", str(e)))
        except (Exception, SystemExit) as e:  # keep going: one broken repo must not block the rest
            log.error("%s: restore failed: %s", repo, e)
            results.append((repo, "failed", str(e)))

    count = {s: sum(r[1] == s for r in results) for s in ("restored", "skipped", "failed")}
    text = ("## Restore from Azure backup\n\n| Repos | Restored | Skipped | Failed |\n|---|---|---|---|\n"
            f"| {len(results)} | {count['restored']} | {count['skipped']} | {count['failed']} |\n")
    icon = {"restored": "✅", "skipped": "⏭️", "failed": "❌"}
    text += "".join(f"\n- {icon[s]} `{repo}`: {msg[:300]}" for repo, s, msg in results)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8").write(text + "\n")
    return 1 if count["failed"] else 0


def restore_one(opts, store: Store, gh: GitHub | None, key: str, work: Path) -> str:
    """Restore one backed-up repo using `work` as scratch space. Returns where it went."""
    points = restore_points(store, key)
    if opts.at:
        points = [p for p in points if opts.at in p]
    if not points:
        raise Skipped(f"No restore point for {key}" + (f" matching {opts.at}" if opts.at else ""))
    point = points[-1]

    exists = False
    if not opts.local:
        _, owner, name = key.split("/", 2)
        t_owner, t_name = opts.to or owner, opts.as_name or name
        target = f"{t_owner}/{t_name}"
        r = gh.get(f"/repos/{target}")
        exists = r.status_code == 200
        if exists and r.json().get("size", 0) > 0 and not opts.into_existing:
            raise Skipped(f"{target} already exists and is not empty. Use --as NEW_NAME, or --into-existing.")
    print(f"Restoring {key} from {point}")

    work.mkdir(parents=True, exist_ok=True)
    meta = store.get_file(f"{point}/repo.bundle", work / "repo.bundle")
    if meta.get("sha256") and meta["sha256"] != git_ops.sha256_file(work / "repo.bundle"):
        raise SystemExit("sha256 mismatch: backup object is corrupt, refusing to restore")
    mirror = work / "repo.git"
    if mirror.exists():
        shutil.rmtree(mirror)
    git_ops.run(["git", "clone", "--mirror", "--quiet", str(work / "repo.bundle"), str(mirror)])
    objects = set(store.names(point + "/"))
    if f"{point}/lfs.tar" in objects:
        store.get_file(f"{point}/lfs.tar", work / "lfs.tar")
        with tarfile.open(work / "lfs.tar") as tar:
            tar.extractall(mirror, filter="data")
    settings = store.get_json(f"{point}/repo.json") if f"{point}/repo.json" in objects else {}
    if f"{point}/wiki.bundle" in objects:
        store.get_file(f"{point}/wiki.bundle", work / "wiki.bundle")

    if opts.local:
        print(f"Local mirror: {mirror}\n  git clone {mirror} my-checkout")
        return str(mirror)

    if not exists:
        body = {"name": t_name, "private": settings.get("private", True),
                "description": settings.get("description") or f"Restored from backup {point}",
                "homepage": settings.get("homepage") or "",
                "has_issues": settings.get("has_issues", True), "has_wiki": settings.get("has_wiki", True),
                "has_projects": settings.get("has_projects", False)}
        path = "/user/repos" if t_owner == gh.login() else f"/orgs/{t_owner}/repos"
        cr = gh.request("POST", path, json=body)
        if cr.status_code >= 300:
            raise SystemExit(f"Could not create {target}: {cr.status_code} {cr.text[:300]}")
        print(f"Created {target} ({'private' if body['private'] else 'public'})")

    url = f"{git_ops.API_GIT_BASE}/{target}.git"
    header = git_ops.github_auth_header(gh.auth.token())
    # GitHub's refs/pull/* are read-only: push branches and tags.
    git_ops.run(["git", "push", "--quiet", url, "refs/heads/*:refs/heads/*", "refs/tags/*:refs/tags/*"],
                cwd=mirror, auth_header=header)
    if (mirror / "lfs" / "objects").exists():
        git_ops.run(["git", "lfs", "push", "--all", url], cwd=mirror, auth_header=header)
    if settings.get("default_branch"):
        gh.request("PATCH", f"/repos/{target}", json={"default_branch": settings["default_branch"]})
    if settings.get("topics"):
        gh.request("PUT", f"/repos/{target}/topics", json={"names": settings["topics"]})
    print(f"Pushed branches, tags{' and LFS objects' if (mirror / 'lfs').exists() else ''} to {target}")

    if (work / "wiki.bundle").exists():
        try:
            wiki = work / "wiki.git"
            git_ops.run(["git", "clone", "--mirror", "--quiet", str(work / "wiki.bundle"), str(wiki)])
            git_ops.run(["git", "push", "--quiet", "--force", f"{git_ops.API_GIT_BASE}/{target}.wiki.git",
                         "refs/heads/*:refs/heads/*"], cwd=wiki, auth_header=header)
            print("Wiki restored")
        except git_ops.GitError:
            print("Wiki not restored: create any page in the new repo's Wiki tab once, then re-run with "
                  "--into-existing (GitHub only accepts wiki pushes after the wiki exists).", file=sys.stderr)

    if opts.with_issues:
        n = _restore_issues(gh, target, _merged_metadata(store, key, point))
        print(f"Recreated {n} issues with comments (new numbers; original number/author/date noted in each)")
    print(f"Done: https://github.com/{target}")
    return f"https://github.com/{target}"
