"""Passwordless Azure Blob access.

* In GitHub Actions: the job's OIDC token is exchanged for an Entra token of a
  user-assigned managed identity (federated credential). A fresh OIDC token is
  requested every time Azure needs one, so runs longer than an hour keep working.
  Nothing is stored in GitHub except the (non-secret) client and tenant IDs.
* On your machine: `az login` (or VS Code / azd / PowerShell sign-in) via DefaultAzureCredential.
* AZURE_STORAGE_CONNECTION_STRING is honoured only for local tests against Azurite.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import requests
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import BlobServiceClient, StandardBlobTier


def credential():
    if os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL") and os.environ.get("AZURE_CLIENT_ID"):
        from azure.identity import ClientAssertionCredential

        def github_oidc_token() -> str:
            r = requests.get(os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"],
                             params={"audience": "api://AzureADTokenExchange"},
                             headers={"Authorization": f"Bearer {os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN']}"},
                             timeout=30)
            r.raise_for_status()
            return r.json()["value"]

        return ClientAssertionCredential(os.environ["AZURE_TENANT_ID"], os.environ["AZURE_CLIENT_ID"], github_oidc_token)

    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential()


def service(account: str) -> BlobServiceClient:
    if os.environ.get("AZURE_STORAGE_CONNECTION_STRING"):
        return BlobServiceClient.from_connection_string(os.environ["AZURE_STORAGE_CONNECTION_STRING"])
    url = account if account.startswith("https://") else f"https://{account}.blob.core.windows.net"
    return BlobServiceClient(url, credential=credential())


class Store:
    def __init__(self, svc: BlobServiceClient, container: str, state_container: str, tier: str | None = "Cool"):
        self.c = svc.get_container_client(container)
        self.state = svc.get_container_client(state_container)
        self.tier = StandardBlobTier(tier) if tier else None

    # --- immutable backup objects: unique names, never overwritten
    def put_file(self, name: str, path: Path, sha256: str) -> None:
        try:
            with path.open("rb") as fh:
                self.c.upload_blob(name, fh, overwrite=False, standard_blob_tier=self.tier,
                                   metadata={"sha256": sha256}, max_concurrency=4)
        except ResourceExistsError:
            pass

    def put_json(self, name: str, obj) -> None:
        try:
            self.c.upload_blob(name, json.dumps(obj, indent=2, default=str).encode(), overwrite=False)
        except ResourceExistsError:
            pass

    def get_file(self, name: str, dest: Path) -> dict:
        dest.parent.mkdir(parents=True, exist_ok=True)
        bc = self.c.get_blob_client(name)
        with dest.open("wb") as fh:
            bc.download_blob(max_concurrency=4).readinto(fh)
        return bc.get_blob_properties().metadata or {}

    def get_json(self, name: str):
        return json.loads(self.c.download_blob(name).readall())

    def exists(self, name: str) -> bool:
        return self.c.get_blob_client(name).exists()

    def names(self, prefix: str) -> list[str]:
        return [b.name for b in self.c.list_blobs(name_starts_with=prefix)]

    # --- mutable per-repo change-tracking state
    def read_state(self, key: str) -> dict:
        try:
            return json.loads(self.state.download_blob(f"{key}.json").readall())
        except ResourceNotFoundError:
            return {}

    def write_state(self, key: str, obj: dict) -> None:
        self.state.upload_blob(f"{key}.json", json.dumps(obj, default=str), overwrite=True)

    def state_keys(self) -> list[str]:
        return [b.name[:-5] for b in self.state.list_blobs() if b.name.endswith(".json")]
