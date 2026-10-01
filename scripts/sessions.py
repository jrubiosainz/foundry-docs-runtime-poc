"""Create and close customer sessions for the hosted agent.

The insurer backend knows which customer is authenticated: it creates the hosted-agent session
(own sandbox with persistent $HOME) and uploads `session.json`. The agent never takes the
customer identity from chat text.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache

from common import CORPUS, credential

AGENT_NAME = os.environ.get("HOSTED_AGENT_NAME", "docs-runtime-agent")


@lru_cache(maxsize=1)
def customers() -> dict[str, dict]:
    return {c["customer_id"]: c for c in json.loads((CORPUS / "customers.json").read_text())}


def session_payload(customer_id: str, route: str | None = None) -> bytes:
    c = customers()[customer_id]
    payload = {"customer_id": customer_id, "full_name": c["full_name"], "products": c["products"]}
    if route:
        payload["route"] = route  # tests/demo only: force the CAG or index route
    return json.dumps(payload, ensure_ascii=False).encode()


@lru_cache(maxsize=1)
def project():
    from azure.ai.projects import AIProjectClient

    return AIProjectClient(endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"], credential=credential())


def latest_version(agent_name: str = AGENT_NAME) -> str:
    return project().agents.get(agent_name=agent_name).versions.latest.version


def new_session(customer_id: str, agent_name: str = AGENT_NAME, version: str | None = None, route: str | None = None) -> str:
    from azure.ai.projects.models import VersionRefIndicator

    session = project().agents.create_session(
        agent_name=agent_name,
        version_indicator=VersionRefIndicator(agent_version=version or latest_version(agent_name)),
    )
    sid = session.agent_session_id
    project().agents.upload_session_file(
        agent_name=agent_name,
        session_id=sid,
        content=session_payload(customer_id, route),
        path="session.json",
        content_type="application/json",
    )
    return sid


def purge_session_chunks(session_id: str) -> int:
    """Delete one session's chunks from the customer index (index route). Idempotent."""
    from common import search_request

    sid = re.sub(r"[^A-Za-z0-9_-]", "_", session_id)[:128]  # same sanitization as the agent
    index = os.environ.get("CUSTOMER_INDEX_NAME", "idx-docs-customer")
    deleted = 0
    for _ in range(20):
        body = {"search": "*", "filter": f"session_id eq '{sid}'", "select": "id", "top": 1000}
        hits = search_request("POST", f"/indexes/{index}/docs/search", body).get("value", [])
        if hits:
            actions = [{"@search.action": "delete", "id": h["id"]} for h in hits]
            search_request("POST", f"/indexes/{index}/docs/index", {"value": actions}, ok=(200, 207))
            deleted += len(hits)
        if len(hits) < 1000:
            break
    return deleted


def delete_session(session_id: str, agent_name: str = AGENT_NAME) -> None:
    """Backend close: delete the agent session and its chunks from the customer index."""
    try:
        purge_session_chunks(session_id)
    finally:
        # Even if purge fails, the session is still deleted; the purge error propagates afterwards.
        project().agents.delete_session(agent_name=agent_name, session_id=session_id)
