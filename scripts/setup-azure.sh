#!/usr/bin/env bash
# One-time Azure setup for ghbackup:
#   storage account (Entra ID only, no keys) + containers + retention lock + lifecycle,
#   a user-assigned managed identity trusted ONLY by your backup repo's main branch (OIDC),
#   and the three GitHub repository variables the workflow needs.
#
# Requires: az (logged in: `az login`), optionally gh (logged in) to set repo variables.
# Usage:   ./scripts/setup-azure.sh -r <owner>/<backup-repo> [-g rg] [-l region] [-a account]
#                                   [-s sku] [-d retention_days] [-k keep_days] [-b branch] [--lock]
set -euo pipefail

RG="rg-github-backup"
LOCATION="westus3"
ACCOUNT=""
REPO=""
SKU="Standard_LRS"      # Standard_GRS/ZRS for extra redundancy (~2x cost)
RETENTION=30            # days a backup is write-protected (nobody can delete/overwrite it)
KEEP=365                # days before old backups are deleted automatically
BRANCH="main"
IDENTITY="id-github-backup"
LOCK=false

usage() { sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    -r) REPO="$2"; shift 2 ;;
    -g) RG="$2"; shift 2 ;;
    -l) LOCATION="$2"; shift 2 ;;
    -a) ACCOUNT="$2"; shift 2 ;;
    -s) SKU="$2"; shift 2 ;;
    -d) RETENTION="$2"; shift 2 ;;
    -k) KEEP="$2"; shift 2 ;;
    -b) BRANCH="$2"; shift 2 ;;
    --lock) LOCK=true; shift ;;
    -h|--help) usage ;;
    *) echo "unknown option $1"; usage ;;
  esac
done
[[ "$REPO" == */* ]] || { echo "error: -r <owner>/<repo> is required (the repo that runs the backup workflow)"; usage; }
(( KEEP > RETENTION )) || { echo "error: keep days (-k) must be greater than retention (-d)"; exit 1; }
command -v az >/dev/null || { echo "error: Azure CLI (az) not found"; exit 1; }

SUB=$(az account show --query id -o tsv)
TENANT=$(az account show --query tenantId -o tsv)
[[ -n "$ACCOUNT" ]] || ACCOUNT="ghbackup$(LC_ALL=C tr -dc 'a-z0-9' </dev/urandom | head -c 8)"
echo "Subscription $SUB · resource group $RG · account $ACCOUNT · repo $REPO"

echo "→ resource group and storage account"
az group create -n "$RG" -l "$LOCATION" -o none
az storage account create -n "$ACCOUNT" -g "$RG" -l "$LOCATION" --sku "$SKU" --kind StorageV2 \
  --access-tier Cool --allow-shared-key-access false --allow-blob-public-access false \
  --min-tls-version TLS1_2 --https-only true -o none
az storage account blob-service-properties update -n "$ACCOUNT" -g "$RG" \
  --enable-delete-retention true --delete-retention-days 14 \
  --enable-container-delete-retention true --container-delete-retention-days 14 -o none

echo "→ containers (created through Azure Resource Manager, so no storage keys are needed)"
az storage container-rm create --storage-account "$ACCOUNT" -g "$RG" -n github-backups -o none
az storage container-rm create --storage-account "$ACCOUNT" -g "$RG" -n backup-state -o none

if (( RETENTION > 0 )); then
  echo "→ write-once retention: $RETENTION days on github-backups"
  az storage container immutability-policy create --account-name "$ACCOUNT" -g "$RG" \
    --container-name github-backups --period "$RETENTION" --allow-protected-append-writes false -o none
  if $LOCK; then
    etag=$(az storage container immutability-policy show --account-name "$ACCOUNT" -g "$RG" \
      --container-name github-backups --query etag -o tsv)
    az storage container immutability-policy lock --account-name "$ACCOUNT" -g "$RG" \
      --container-name github-backups --if-match "$etag" -o none
    echo "  policy LOCKED (can be extended, never shortened or removed)"
  fi
fi

echo "→ lifecycle: Cold tier after 30 days, delete after $KEEP days"
policy=$(mktemp)
cat >"$policy" <<JSON
{"rules":[{"name":"tier-and-expire","enabled":true,"type":"Lifecycle","definition":{
  "filters":{"blobTypes":["blockBlob"],"prefixMatch":["github-backups/github/"]},
  "actions":{"baseBlob":{"tierToCold":{"daysAfterCreationGreaterThan":30},
                         "delete":{"daysAfterCreationGreaterThan":$KEEP}}}}}]}
JSON
az storage account management-policy create --account-name "$ACCOUNT" -g "$RG" --policy @"$policy" -o none
rm -f "$policy"

echo "→ managed identity trusted by GitHub Actions in $REPO (branch $BRANCH) only"
az identity create -n "$IDENTITY" -g "$RG" -l "$LOCATION" -o none
CLIENT_ID=$(az identity show -n "$IDENTITY" -g "$RG" --query clientId -o tsv)
PRINCIPAL_ID=$(az identity show -n "$IDENTITY" -g "$RG" --query principalId -o tsv)
FIC_NAME="github-$(echo "$REPO-$BRANCH" | tr '/' '-' | tr -cd 'A-Za-z0-9-_' | head -c 100)"
az identity federated-credential create --name "$FIC_NAME" --identity-name "$IDENTITY" -g "$RG" \
  --issuer https://token.actions.githubusercontent.com \
  --subject "repo:${REPO}:ref:refs/heads/${BRANCH}" \
  --audiences api://AzureADTokenExchange -o none

SCOPE=$(az storage account show -n "$ACCOUNT" -g "$RG" --query id -o tsv)
echo "→ role: Storage Blob Data Contributor for the identity"
for i in 1 2 3 4 5 6; do   # a new identity can take a minute to replicate
  if az role assignment create --assignee-object-id "$PRINCIPAL_ID" --assignee-principal-type ServicePrincipal \
       --role "Storage Blob Data Contributor" --scope "$SCOPE" -o none 2>/dev/null; then break; fi
  echo "  waiting for identity to replicate ($i)…"; sleep 15
done

ME=$(az ad signed-in-user show --query id -o tsv 2>/dev/null || true)
if [[ -n "$ME" ]]; then
  echo "→ role: Storage Blob Data Contributor for you (to list/restore from your machine)"
  az role assignment create --assignee-object-id "$ME" --assignee-principal-type User \
    --role "Storage Blob Data Contributor" --scope "$SCOPE" -o none || true
fi

if command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then
  echo "→ GitHub repository variables on $REPO"
  gh variable set AZURE_CLIENT_ID -R "$REPO" -b "$CLIENT_ID"
  gh variable set AZURE_TENANT_ID -R "$REPO" -b "$TENANT"
  gh variable set GHB_STORAGE_ACCOUNT -R "$REPO" -b "$ACCOUNT"
  VARS_SET=true
else
  VARS_SET=false
fi

cat <<EOF

✅ Azure is ready. Nothing secret was created: GitHub signs in with OIDC.

  AZURE_CLIENT_ID      = $CLIENT_ID
  AZURE_TENANT_ID      = $TENANT
  GHB_STORAGE_ACCOUNT  = $ACCOUNT
$($VARS_SET && echo "  (already set as repository variables on $REPO)" || echo "  → add these as repository *variables* (Settings → Secrets and variables → Actions → Variables)")

Next:
  1. Create a fine-grained PAT (read-only, All repositories): Contents, Issues, Pull requests, Metadata = Read
     then:  gh secret set GH_BACKUP_TOKEN -R $REPO
  2. Run it:  gh workflow run backup.yml -R $REPO -f full=true
  3. From your machine:  export GHB_STORAGE_ACCOUNT=$ACCOUNT && python -m ghbackup list
EOF
