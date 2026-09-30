"""ghbackup — back up your GitHub repositories to Azure Blob Storage, passwordless.

  python -m ghbackup backup                       # all repos you own (incremental)
  python -m ghbackup list [owner/repo]            # what is backed up / restore points
  python -m ghbackup restore my-repo              # recreate a deleted repo from the latest backup
  python -m ghbackup restore my-repo --at 2026/09/01 --as my-repo-old --with-issues
  python -m ghbackup restore my-repo --local ./out   # just give me the files
  python -m ghbackup restore repo-a repo-b        # several repos
  python -m ghbackup restore --all                # every backed-up repo that's missing on GitHub
  python -m ghbackup verify [--sample 10 | --all] # prove backups restore cleanly

Settings come from flags or environment variables (GHB_*), so the same command works
in GitHub Actions and on your laptop.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile


def _env(name: str, default=None):
    return os.environ.get(name) or default


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ghbackup", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account", default=_env("GHB_STORAGE_ACCOUNT"), help="storage account name or blob URL")
    ap.add_argument("--container", default=_env("GHB_CONTAINER", "github-backups"))
    ap.add_argument("--state-container", default=_env("GHB_STATE_CONTAINER", "backup-state"))
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backup", help="back up repositories")
    b.add_argument("--full", action="store_true", help="force a complete copy of every repo")
    b.add_argument("--full-every", type=int, default=int(_env("GHB_FULL_EVERY_DAYS", 30)),
                   help="days between forced complete copies (keep below your retention)")
    b.add_argument("--affiliation", default=_env("GHB_AFFILIATION", "owner"),
                   help="owner | owner,organization_member | owner,collaborator,organization_member")
    b.add_argument("--only", nargs="*", default=[], help="only these repos (name or owner/name)")
    b.add_argument("--exclude", nargs="*", default=[p for p in _env("GHB_EXCLUDE", "").split(",") if p],
                   help="regexes on owner/name to skip")
    b.add_argument("--include-forks", action="store_true", default=_env("GHB_INCLUDE_FORKS") == "true")
    b.add_argument("--no-lfs", action="store_true")
    b.add_argument("--no-wiki", action="store_true")
    b.add_argument("--no-metadata", action="store_true", help="skip issues/PRs/comments/releases")
    b.add_argument("--workers", type=int, default=int(_env("GHB_WORKERS", 4)))
    b.add_argument("--tier", default=_env("GHB_TIER", "Cool"), help="upload access tier: Hot | Cool | Cold | none")
    b.add_argument("--workdir", default=_env("RUNNER_TEMP", tempfile.gettempdir()))

    ls = sub.add_parser("list", help="list backed-up repos, or restore points of one repo")
    ls.add_argument("repo", nargs="?")

    r = sub.add_parser("restore", help="restore one, several or all repositories")
    r.add_argument("repo", nargs="*", help="repo name(s) or owner/repo as backed up")
    r.add_argument("--all", action="store_true",
                   help="restore every backed-up repo; ones that still exist and aren't empty are skipped")
    r.add_argument("--at", help="restore point: yyyy/mm/dd, yyyy/mm or run id (default: latest)")
    r.add_argument("--as", dest="as_name", help="new repository name (default: original name)")
    r.add_argument("--to", help="target owner/org (default: original owner)")
    r.add_argument("--local", help="only download and rebuild the repo in this folder")
    r.add_argument("--with-issues", action="store_true", help="recreate issues and comments too")
    r.add_argument("--into-existing", action="store_true", help="push into an existing non-empty repo")

    v = sub.add_parser("verify", help="restore-test backups without touching GitHub")
    v.add_argument("--sample", type=int, default=10)
    v.add_argument("--all", action="store_true")

    opts = ap.parse_args(argv)
    if opts.cmd == "restore":
        if bool(opts.repo) == opts.all:
            ap.error("restore: give repo name(s) or --all, not both")
        if opts.as_name and (opts.all or len(opts.repo) > 1):
            ap.error("restore: --as works with a single repo only")
    logging.basicConfig(level=logging.DEBUG if opts.verbose else logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("azure").setLevel(logging.WARNING)
    if not opts.account and not os.environ.get("AZURE_STORAGE_CONNECTION_STRING"):
        ap.error("set --account or GHB_STORAGE_ACCOUNT")

    from .azstore import Store, service

    tier = None if opts.cmd != "backup" or str(opts.tier).lower() == "none" else opts.tier
    store = Store(service(opts.account or ""), opts.container, opts.state_container, tier)

    if opts.cmd == "backup":
        from .backup import run
        from .github import GitHub, auth_from_env

        return run(opts, GitHub(auth_from_env(_env("GHB_OWNER"))), store)
    if opts.cmd == "list":
        from .verify import list_backups

        return list_backups(opts, store)
    if opts.cmd == "verify":
        from .verify import verify

        return verify(opts, store)
    if opts.cmd == "restore":
        from .github import GitHub, auth_from_env
        from .restore import restore

        gh = None if opts.local else GitHub(auth_from_env(token_env="GH_RESTORE_TOKEN"))
        return restore(opts, store, gh)
    return 2


if __name__ == "__main__":
    sys.exit(main())
