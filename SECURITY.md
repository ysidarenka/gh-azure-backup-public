# Security

Please report vulnerabilities privately via GitHub's *Report a vulnerability* (Security tab),
not in public issues.

Design notes: no Azure secrets are stored (GitHub OIDC → managed identity bound to one repo and
branch); the storage account rejects shared keys and SAS; backups are write-once for the
retention period; GitHub access for backups is read-only; tokens are passed to git via
environment config, never in URLs, process arguments, logs or the stored bundles.
