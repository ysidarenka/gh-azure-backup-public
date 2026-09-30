# ghbackup — back up your GitHub repos to Azure Blob Storage, passwordless

Every night a GitHub Actions workflow copies **all your repositories** — every branch, tag,
LFS file and wiki, plus issues, pull requests, comments and releases — into your own Azure
Storage account. Deleted a repo by accident? Get it back with one command:

```bash
python -m ghbackup restore my-repo            # recreates it: same name, branches, tags, settings
python -m ghbackup restore my-repo --with-issues
```

**No Azure password, key or secret is stored anywhere.** GitHub signs in to Azure with OpenID
Connect, and your storage account doesn't even accept keys.

---

## How authentication works

| Where it runs | Azure | GitHub |
|---|---|---|
| **GitHub Actions** (nightly) | OIDC → managed identity. Trusted only for *this repo's `main` branch*. Stored in GitHub: two IDs, which aren't secrets. | **One read-only token**, stored as a secret (see note below) |
| **Your laptop** (restore, list) | `az login`: nothing stored | `gh auth login`: nothing stored |

> **Why one GitHub token?** The automatic `GITHUB_TOKEN` in a workflow can only read the repo
> it runs in, and GitHub has no way to swap it for access to your other repos. So the backup
> needs either a **fine-grained personal access token** (read-only) or a **GitHub App** you
> own, whose tokens are short-lived and refresh automatically.

## Quick start (about 10 minutes)

**You need:** an Azure subscription, the [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli),
the [GitHub CLI](https://cli.github.com/), and Python 3.10+ on your machine.

1. **Make your own private copy** of this repository. Click *Use this template → Create a new
   repository → **Private***. It must be private: GitHub turns off scheduled workflows in
   public repos after 60 days without activity.

2. **Set up Azure.** This creates the storage account, the identity, the trust relationship
   and the repo variables:
   ```bash
   az login
   gh auth login
   git clone https://github.com/<you>/<your-backup-repo> && cd <your-backup-repo>
   ./scripts/setup-azure.sh -r <you>/<your-backup-repo> -l westus3
   ```
   The defaults are Standard_LRS storage, 30-day write protection and 365-day retention. See
   `-h` for options, or use `infra/main.bicep` instead.

3. **Give the workflow read access to your repos.** Create a
   [fine-grained token](https://github.com/settings/personal-access-tokens/new):
   - Repository access: **All repositories**
   - Permissions: **Contents**, **Issues** and **Pull requests** set to *Read-only* (Metadata is added automatically)

   Then store it:
   ```bash
   gh secret set GH_BACKUP_TOKEN -R <you>/<your-backup-repo>
   ```

4. **Run it once:**
   ```bash
   gh workflow run backup.yml -R <you>/<your-backup-repo> -f full=true
   ```
   After that it runs every night. Check any run's summary page for the results table.
   To back up just some repos, add `-f only=my-repo` (several: `-f only="repo-a,repo-b"`).

## Restore

Restoring from your laptop is the easiest way, and needs no stored credentials:

```bash
pip install -r requirements.txt
export GHB_STORAGE_ACCOUNT=<account printed by setup>

# (or `pip install .` once, then use `ghbackup …` instead of `python -m ghbackup …`)
python -m ghbackup list                                   # everything that's backed up
python -m ghbackup list my-repo                           # restore points for one repo
python -m ghbackup restore my-repo                        # latest backup → github.com/<you>/my-repo
python -m ghbackup restore my-repo --at 2026/09/01 --as my-repo-sept   # older version, new name
python -m ghbackup restore my-repo --with-issues          # also recreate issues + comments
python -m ghbackup restore my-repo --local ./out          # just the files: git clone ./out/repo.git
python -m ghbackup restore repo-a repo-b                  # several repos
python -m ghbackup restore --all                          # every backed-up repo that's missing on GitHub
```

- **What comes back:** all branches and tags, LFS objects, the default branch, topics,
  description and visibility (private stays private).
- **Wiki:** click *Wiki → Create the first page* in the new repo once, then run the restore
  again with `--into-existing`.
- **Issues** (with `--with-issues`) get new numbers, with the original number, author and
  date noted in each one.
- **Pull requests:** GitHub can't re-create them, but their commits are in the backup
  under `refs/pull/*`.
- **Safety:** it refuses to push into an existing non-empty repo unless you pass
  `--into-existing`. With `--all` or several repos, those are skipped instead, so
  `restore --all` safely brings back only what's missing. One failed repo doesn't stop the rest.

### Restore from GitHub Actions

Away from your laptop? Use the *Restore repositories* workflow (*Actions → Restore repositories →
Run workflow*). In **repo**, enter `my-repo`, `owner/my-repo`, several names separated by
commas, or `all`. The run's summary page lists each repo as restored, skipped or failed.

First add a `GH_RESTORE_TOKEN` secret. Use a
[fine-grained token](https://github.com/settings/personal-access-tokens/new) with
**All repositories** access and **Administration**, **Contents** and **Issues** set to
*Read and write*:

```bash
gh secret set GH_RESTORE_TOKEN -R <you>/<your-backup-repo>
gh workflow run restore.yml -R <you>/<your-backup-repo> -f repo=my-repo
gh workflow run restore.yml -R <you>/<your-backup-repo> -f repo=all -f with_issues=true
```

Delete the secret or let the token expire when you're done. It can create and write to all your
repos, so keep it only as long as you need it.

## What's backed up

| | How | When |
|---|---|---|
| Code: all branches, tags, PR refs | `git clone --mirror` → verified `git bundle` | when the repo was pushed to, plus a full copy every 30 days |
| Git LFS files | `git lfs fetch --all` | with code |
| Wiki | wiki bundle | with code |
| Settings (description, visibility, topics, default branch) | `repo.json` | with code |
| Issues, PRs, comments, review comments, releases | GitHub API → `metadata.json.gz` | changes since the last run, nightly |

Not included: Actions secrets (GitHub never reveals them), Discussions, Projects, Packages,
Actions logs and artifacts, and Gists.

Storage layout: `github/<owner>/<repo>/<yyyy>/<mm>/<dd>/<run>/…`. Each restore point is a
self-contained bundle. Every nightly run also writes a manifest to `_runs/` with the SHA-256
of every file, and `ghbackup verify` checks those hashes. A *Monthly restore test* workflow
restores a sample of repos and runs `git fsck` on them.

## Protection built in

- **No keys:** shared-key and SAS access are **disabled** on the storage account. Only
  Entra ID sign-ins work: the workflow's identity, and you.
- **Write-once retention:** once a backup is written, nobody (not the workflow, not a stolen
  token, not you) can delete or overwrite it for the retention period (default 30 days).
  Add `--lock` to the setup script to make that policy permanent.
- **Narrow trust:** the identity trusts GitHub tokens only from `repo:<you>/<your-backup-repo>:ref:refs/heads/main`.
  Forks, other branches and other repos can't use it.
- **Read-only GitHub access:** the backup token only reads. Restores use your own `gh`
  login, or a separate write token.
- **Automatic cleanup:** backups move to the Cold tier after 30 days and are deleted after
  the keep period (default 365). Every repo gets a full copy at least every 30 days, so a
  complete recent copy always exists.

## Settings

The CLI reads flags or environment variables. In Actions, set them as repository **variables**.

| Variable | Default | Meaning |
|---|---|---|
| `GHB_STORAGE_ACCOUNT` | *(required)* | storage account name |
| `AZURE_CLIENT_ID`, `AZURE_TENANT_ID` | *(set by setup)* | identity used by Actions (not secrets) |
| `GHB_AFFILIATION` | `owner` | also back up org/collaborator repos: `owner,organization_member,collaborator` |
| `GHB_EXCLUDE` | | comma-separated regexes on `owner/name` to skip |
| `GHB_INCLUDE_FORKS` | `false` | include forks |
| `GHB_FULL_EVERY_DAYS` | `30` | force a complete copy this often (keep below the retention/keep days) |
| `GHB_WORKERS` | `4` | repos processed in parallel |
| `GHB_TIER` | `Cool` | upload access tier |

A fine-grained token covers **one** owner: your account *or* one organization. For org repos,
create one token per org, or use a GitHub App (below).

**GitHub App instead of a token** (no expiry dates to manage):

1. Create an app under *Settings → Developer settings → GitHub Apps*, with no webhook and
   read-only Contents, Issues, Pull requests and Metadata.
2. Install it on your account with *All repositories*.
3. Set `GH_APP_ID` as a repository variable and `GH_APP_PRIVATE_KEY` as a secret, and
   remove `GH_BACKUP_TOKEN`.

The app's tokens last one hour; the tool refreshes them automatically.

## Cost

- **Storage:** about 50 repos (~2 GB of history) grow to roughly 10–30 GB a year at Cool/Cold
  prices, which is **well under $1/month**. Standard_GRS about doubles that.
- **Actions:** a nightly run takes a few minutes, well inside the free minutes of any GitHub plan.

## Development

```bash
pip install -r requirements.txt && npm install -g azurite
python tests/e2e_local.py     # fake GitHub API + Azurite: backup, delete, restore, verify
```

`scripts/setup-azure.sh` and `infra/main.bicep` create the same resources. Contributions welcome.

## License

MIT
