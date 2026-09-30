"""Git mirror / bundle / LFS helpers.

Credentials are passed to git through GIT_CONFIG_* environment variables
(git >= 2.31) rather than argv or the remote URL, so tokens never show up
in `ps`, logs or the bundle's stored remote config.
"""
from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import tarfile
from pathlib import Path

API_GIT_BASE = os.environ.get("GH_GIT_URL", "https://github.com").rstrip("/")


class GitError(RuntimeError):
    pass


def _auth_env(auth_header: str | None) -> dict:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"}
    if auth_header:
        env.update({
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": auth_header,
        })
    return env


def github_auth_header(token: str) -> str:
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return f"Authorization: Basic {basic}"


def bearer_auth_header(token: str) -> str:
    return f"Authorization: Bearer {token}"


def run(args: list[str], cwd: Path | None = None, auth_header: str | None = None, timeout: int = 3 * 3600) -> str:
    p = subprocess.run(args, cwd=cwd, env=_auth_env(auth_header), capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise GitError(f"{' '.join(args[:3])} failed ({p.returncode}): {p.stderr.strip()[-2000:]}")
    return p.stdout


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def mirror_clone(url: str, dest: Path, auth_header: str | None) -> None:
    run(["git", "clone", "--mirror", "--quiet", url, str(dest)], auth_header=auth_header)


def ref_count(repo: Path) -> int:
    return len(run(["git", "for-each-ref", "--format=%(refname)"], cwd=repo).splitlines())


def create_bundle(repo: Path, out: Path) -> None:
    """Bundle every ref (branches, tags, and GitHub's refs/pull/* PR heads)."""
    run(["git", "bundle", "create", str(out), "--all"], cwd=repo)
    run(["git", "bundle", "verify", "--quiet", str(out)], cwd=repo)


def fetch_lfs(repo: Path, url: str, auth_header: str | None) -> Path | None:
    """Fetch all LFS objects for all refs; return a tar of them, or None if the repo has none."""
    try:
        run(["git", "lfs", "fetch", "--all", url], cwd=repo, auth_header=auth_header)
    except GitError as e:
        if "Not in a Git repository" in str(e) or "not found" in str(e).lower():
            return None
        raise
    objects = repo / "lfs" / "objects"
    if not objects.exists() or not any(objects.rglob("*")):
        return None
    out = repo.parent / f"{repo.name}.lfs.tar"
    with tarfile.open(out, "w") as tar:
        tar.add(objects, arcname="lfs/objects")
    return out


def bundle_heads(bundle: Path) -> dict[str, str]:
    out = run(["git", "bundle", "list-heads", str(bundle)])
    heads = {}
    for line in out.splitlines():
        sha, ref = line.split(" ", 1)
        heads[ref] = sha
    return heads


def ls_remote(url: str, auth_header: str | None) -> dict[str, str]:
    out = run(["git", "ls-remote", url], auth_header=auth_header)
    heads = {}
    for line in out.splitlines():
        sha, ref = line.split("\t", 1)
        heads[ref] = sha
    return heads
