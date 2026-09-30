"""Back up every repository you own to Azure Blob Storage.

Per repo, per run:
  code     - `git clone --mirror` -> verified `git bundle` (all branches, tags, PR refs),
             only when the repo was pushed since the last backup, and at least every
             --full-every days, so there is always a recent complete copy.
  lfs      - all Git LFS objects (tar), with code.
  wiki     - wiki bundle, with code.
  repo     - repo settings (description, visibility, topics, default branch), with code.
  metadata - issues, PRs, comments, releases changed since the last run.

Layout: github/<owner>/<repo>/<yyyy>/<mm>/<dd>/<run_id>/{repo.bundle,lfs.tar,wiki.bundle,repo.json,metadata.json.gz}
        _runs/<yyyy>/<mm>/<dd>/<run_id>.json   (manifest with sha256 of every object)
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import re
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from . import git_ops
from .azstore import Store
from .github import GitHub

log = logging.getLogger("ghbackup")


def _optional(gh: GitHub, url: str, **params) -> list[dict]:
    try:
        return list(gh.paginate(url, **params))
    except requests.HTTPError as e:
        log.debug("skipping %s: %s", url, e)
        return []


def export_metadata(gh: GitHub, repo: dict, since: str | None, dest: Path) -> dict:
    base = f"/repos/{repo['full_name']}"
    q = {"since": since} if since else {}
    pulls = []
    for pr in gh.paginate(f"{base}/pulls", state="all", sort="updated", direction="desc"):
        if since and pr["updated_at"] < since:
            break
        pulls.append(pr)
    doc = {
        "since": since,
        "issues": list(gh.paginate(f"{base}/issues", state="all", **q)) if repo.get("has_issues", True) else [],
        "issue_comments": list(gh.paginate(f"{base}/issues/comments", **q)),
        "pulls": pulls,
        "pull_review_comments": list(gh.paginate(f"{base}/pulls/comments", **q)),
        "releases": list(gh.paginate(f"{base}/releases")),
    }
    if not since:
        doc["labels"] = _optional(gh, f"{base}/labels")
        doc["milestones"] = _optional(gh, f"{base}/milestones", state="all")
    with gzip.open(dest, "wt", encoding="utf-8") as fh:
        json.dump(doc, fh)
    return {k: len(v) for k, v in doc.items() if isinstance(v, list)}


def backup_repo(repo: dict, gh: GitHub, store: Store, run_id: str, opts) -> dict:
    owner, name = repo["full_name"].split("/", 1)
    key = f"github/{owner}/{name}"
    now = datetime.now(timezone.utc)
    state = store.read_state(key)
    last_full = state.get("last_full_at")
    full = opts.full or not last_full or now - datetime.fromisoformat(last_full) > timedelta(days=opts.full_every)
    changed = full or repo.get("pushed_at") != state.get("pushed_at")
    prefix = f"{key}/{now:%Y/%m/%d}/{run_id}"
    res = {"repo": repo["full_name"], "status": "ok", "code": "unchanged", "full": full, "objects": {}}
    new = dict(state)
    tmp = Path(tempfile.mkdtemp(dir=opts.workdir))

    def put(blob: str, path: Path) -> None:
        sha = git_ops.sha256_file(path)
        store.put_file(f"{prefix}/{blob}", path, sha)
        res["objects"][f"{prefix}/{blob}"] = {"sha256": sha, "bytes": path.stat().st_size}

    try:
        header = git_ops.github_auth_header(gh.auth.token())
        if changed:
            mirror = tmp / "repo.git"
            git_ops.mirror_clone(repo["clone_url"], mirror, header)
            if git_ops.ref_count(mirror) == 0:
                res["code"] = "empty"
            else:
                git_ops.create_bundle(mirror, tmp / "repo.bundle")
                put("repo.bundle", tmp / "repo.bundle")
                res["code"] = "backed_up"
                new["last_point"] = prefix
                if not opts.no_lfs:
                    lfs = git_ops.fetch_lfs(mirror, repo["clone_url"], header)
                    if lfs:
                        put("lfs.tar", lfs)
                if not opts.no_wiki and repo.get("has_wiki"):
                    try:
                        git_ops.mirror_clone(re.sub(r"\.git$", ".wiki.git", repo["clone_url"]), tmp / "wiki.git", header)
                        if git_ops.ref_count(tmp / "wiki.git"):
                            git_ops.create_bundle(tmp / "wiki.git", tmp / "wiki.bundle")
                            put("wiki.bundle", tmp / "wiki.bundle")
                    except git_ops.GitError:
                        pass  # has_wiki is true even if no page was ever created
                settings = {k: repo.get(k) for k in (
                    "name", "full_name", "description", "homepage", "private", "visibility", "default_branch",
                    "topics", "has_issues", "has_wiki", "has_projects", "archived", "fork", "pushed_at")}
                (tmp / "repo.json").write_text(json.dumps(settings, indent=2))
                put("repo.json", tmp / "repo.json")
            new["pushed_at"] = repo.get("pushed_at")

        if not opts.no_metadata:
            since = None if full else state.get("metadata_until")
            counts = export_metadata(gh, repo, since, tmp / "metadata.json.gz")
            res["metadata"] = counts
            if full or any(v for k, v in counts.items() if k != "releases"):
                put("metadata.json.gz", tmp / "metadata.json.gz")
            new["metadata_until"] = now.isoformat()

        if full:
            new["last_full_at"] = now.isoformat()
        new.update(last_run=run_id, clone_url=repo["clone_url"])
        store.write_state(key, new)
    except Exception as e:  # noqa: BLE001 - one bad repo must not stop the run
        log.exception("backup failed for %s", repo["full_name"])
        res.update(status="failed", error=str(e)[-800:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return res


def run(opts, gh: GitHub, store: Store) -> int:
    started = time.time()
    now = datetime.now(timezone.utc)
    run_id = f"{now:%Y%m%dT%H%M%SZ}-{os.environ.get('GITHUB_RUN_ID', 'local')}"
    Path(opts.workdir).mkdir(parents=True, exist_ok=True)

    repos = gh.list_repos(opts.affiliation)
    repos = [r for r in repos
             if (opts.include_forks or not r.get("fork"))
             and not any(re.search(p, r["full_name"]) for p in opts.exclude)
             and (not opts.only or r["name"] in opts.only or r["full_name"] in opts.only)]
    log.info("backing up %d repositories", len(repos))
    with ThreadPoolExecutor(max_workers=opts.workers) as pool:
        results = list(pool.map(lambda r: backup_repo(r, gh, store, run_id, opts), repos))

    elapsed = time.time() - started
    store.put_json(f"_runs/{now:%Y/%m/%d}/{run_id}.json",
                   {"run_id": run_id, "started_at": now.isoformat(), "duration_s": round(elapsed),
                    "results": sorted(results, key=lambda r: r["repo"])})
    failed = [r for r in results if r["status"] != "ok"]
    size = sum(o["bytes"] for r in results for o in r["objects"].values())
    text = (f"## GitHub → Azure backup `{run_id}`\n\n"
            "| Repos | Backed up | Unchanged | Empty | Failed | Uploaded | Duration |\n|---|---|---|---|---|---|---|\n"
            f"| {len(results)} | {sum(r['code'] == 'backed_up' for r in results)} "
            f"| {sum(r['code'] == 'unchanged' for r in results)} | {sum(r['code'] == 'empty' for r in results)} "
            f"| {len(failed)} | {size / 1e6:.1f} MB | {elapsed / 60:.1f} min |\n")
    text += "".join(f"\n- ❌ `{r['repo']}`: {r['error'][:300]}" for r in failed)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a").write(text + "\n")
    return 1 if failed else 0
