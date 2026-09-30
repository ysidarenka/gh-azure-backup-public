"""Restore test: prove backups can be restored, without touching GitHub."""
from __future__ import annotations

import random
import shutil
import tempfile
from pathlib import Path

from . import git_ops
from .azstore import Store


def verify(opts, store: Store) -> int:
    states = {k: store.read_state(k) for k in store.state_keys()}
    candidates = [k for k, s in states.items() if s.get("last_point")]
    sample = candidates if opts.all else random.sample(candidates, min(opts.sample, len(candidates)))
    bad = 0
    print(f"Verifying {len(sample)} of {len(candidates)} backed-up repos\n")
    for key in sorted(sample):
        point = states[key]["last_point"]
        tmp = Path(tempfile.mkdtemp())
        try:
            meta = store.get_file(f"{point}/repo.bundle", tmp / "repo.bundle")
            if meta.get("sha256") and meta["sha256"] != git_ops.sha256_file(tmp / "repo.bundle"):
                raise git_ops.GitError("sha256 mismatch")
            git_ops.run(["git", "clone", "--mirror", "--quiet", str(tmp / "repo.bundle"), str(tmp / "r.git")])
            git_ops.run(["git", "fsck", "--full", "--no-progress"], cwd=tmp / "r.git")
            refs = git_ops.ref_count(tmp / "r.git")
            print(f"  ✅ {key}  ({refs} refs, {point.rsplit('/', 1)[-1]})")
        except Exception as e:  # noqa: BLE001
            bad += 1
            print(f"  ❌ {key}: {str(e)[:200]}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(sample) - bad} OK, {bad} failed")
    return 1 if bad else 0


def list_backups(opts, store: Store) -> int:
    if opts.repo:
        from .restore import resolve_key, restore_points

        key = resolve_key(store, opts.repo)
        for p in restore_points(store, key):
            print(p)
        return 0
    rows = sorted((k, store.read_state(k)) for k in store.state_keys())
    print(f"{'repository':50} {'last code backup':28} last full")
    for k, s in rows:
        point = (s.get("last_point") or "(empty repo)").split("/")
        when = "/".join(point[3:6]) if len(point) > 5 else point[0]
        print(f"{k.removeprefix('github/'):50} {when:28} {(s.get('last_full_at') or '')[:10]}")
    return 0
