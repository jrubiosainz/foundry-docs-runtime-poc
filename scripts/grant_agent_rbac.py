"""Assign the minimum roles on prototype resources to hosted-agent identities.

Usage: python scripts/grant_agent_rbac.py [<principal_id> ...]
With no arguments, read instance_identity from `azd ai agent show docs-runtime-agent --output json`.
Idempotent: each assignment name is derived from (scope, principal, role).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from common import ROOT, credential  # noqa: E402

SUB = os.environ["AZURE_SUBSCRIPTION_ID"]
RG = os.environ["AZURE_RESOURCE_GROUP"]
RG_ID = f"/subscriptions/{SUB}/resourceGroups/{RG}"
ACCOUNT = f"{RG_ID}/providers/Microsoft.CognitiveServices/accounts/{os.environ['AZURE_AI_ACCOUNT_NAME']}"
STORAGE = f"{RG_ID}/providers/Microsoft.Storage/storageAccounts/{os.environ['STORAGE_ACCOUNT_NAME']}"
SEARCH = f"{RG_ID}/providers/Microsoft.Search/searchServices/{os.environ['SEARCH_SERVICE_NAME']}"

ROLES = [
    ("Foundry User", "53ca6127-db72-4b80-b1b0-d745d6d5456d", ACCOUNT),
    ("Cognitive Services OpenAI User", "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd", ACCOUNT),
    ("Storage Blob Data Reader", "2a2b9908-6ea1-4ae2-8e65-a410df84e7d1", STORAGE),
    ("Search Index Data Reader", "1407120a-92aa-4202-b7e9-c0e197c71c8f", SEARCH),
    ("Search Index Data Contributor", "8ebe5a00-799e-43f5-93ac-243d3dce84a7", SEARCH),
]


def agent_principals(agent: str = "docs-runtime-agent") -> list[str]:
    out = subprocess.run(["azd", "ai", "agent", "show", agent, "--output", "json"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    data = json.loads(out[out.find("{") :])
    # Only the instance identity supports role assignments (the blueprint does not).
    return [data["instance_identity"]["principal_id"]]


def main() -> None:
    principals = sys.argv[1:] or agent_principals()
    token = credential().get_token("https://management.azure.com/.default").token
    headers = {"Authorization": f"Bearer {token}"}
    errors = 0
    for principal in principals:
        for role_name, role_id, scope in ROLES:
            name = uuid.uuid5(uuid.NAMESPACE_URL, f"{scope}|{principal}|{role_id}")
            url = f"https://management.azure.com{scope}/providers/Microsoft.Authorization/roleAssignments/{name}?api-version=2022-04-01"
            body = {
                "properties": {
                    "roleDefinitionId": f"/subscriptions/{SUB}/providers/Microsoft.Authorization/roleDefinitions/{role_id}",
                    "principalId": principal,
                    "principalType": "ServicePrincipal",
                }
            }
            resp = requests.put(url, json=body, headers=headers, timeout=60)
            if resp.status_code in (200, 201):
                state = "assigned"
            elif resp.status_code == 409 and "RoleAssignmentExists" in resp.text:
                state = "already existed"
            else:
                state = f"ERROR {resp.status_code}: {resp.text[:300]}"
                errors += 1
            print(f"{principal[:8]}… · {role_name:<32} · {scope.rsplit('/', 1)[-1]:<22} · {state}")
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
