"""Shared prototype utilities (Entra ID, AI Search REST, embeddings)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import requests
from azure.identity import (
    AzureCliCredential,
    AzureDeveloperCliCredential,
    ChainedTokenCredential,
    DefaultAzureCredential,
    get_bearer_token_provider,
)
from dotenv import load_dotenv
from openai import OpenAI

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

CORPUS = ROOT / "corpus" / "out"
SEARCH_API = "2024-07-01"
GENERAL_INDEX = os.environ.get("GENERAL_INDEX_NAME", "idx-docs-general")
CUSTOMER_INDEX = os.environ.get("CUSTOMER_INDEX_NAME", "idx-docs-customer")
DMS_CONTAINER = os.environ.get("DMS_CONTAINER", "docs-dms")
BANK_CONTAINER = os.environ.get("BANK_CONTAINER", "docs-bank")
EMBED_DIMS = 3072

_cred = None


class _CachedCredential:
    """Cache tokens in memory: AzureCliCredential launches `az` on every call."""

    def __init__(self, inner):
        self._inner = inner
        self._tokens: dict[tuple, object] = {}

    def get_token(self, *scopes, **kwargs):
        key = tuple(scopes)
        token = self._tokens.get(key)
        if token is None or token.expires_on - time.time() < 300:
            token = self._inner.get_token(*scopes, **kwargs)
            self._tokens[key] = token
        return token

    def get_token_info(self, *scopes, **kwargs):
        # azure-core >=1.31 uses get_token_info when available: without caching here, each new client would launch `az`.
        key = ("info", *scopes)
        token = self._tokens.get(key)
        if token is None or token.expires_on - time.time() < 300:
            token = self._inner.get_token_info(*scopes, **kwargs)
            self._tokens[key] = token
        return token


def credential():
    global _cred
    if _cred is None:
        _cred = _CachedCredential(
            ChainedTokenCredential(
                AzureCliCredential(process_timeout=180),
                AzureDeveloperCliCredential(process_timeout=180),
                DefaultAzureCredential(exclude_cli_credential=True, exclude_developer_cli_credential=True),
            )
        )
    return _cred


def search_headers() -> dict[str, str]:
    token = credential().get_token("https://search.azure.com/.default").token
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def search_request(method: str, path: str, body: dict | None = None, ok=(200, 201, 204)) -> dict:
    url = f"{os.environ['SEARCH_ENDPOINT']}{path}"
    sep = "&" if "?" in url else "?"
    url = f"{url}{sep}api-version={SEARCH_API}"
    for attempt in range(5):
        try:
            resp = requests.request(method, url, headers=search_headers(), json=body, timeout=120)
        except (requests.ConnectionError, requests.Timeout):
            # Connection cut by the service or network: the operations we use are idempotent.
            if attempt == 4:
                raise
            time.sleep(2 ** attempt)
            continue
        if resp.status_code in ok:
            return resp.json() if resp.content else {}
        if resp.status_code in (429, 503) and attempt < 4:
            time.sleep(2 ** attempt)
            continue
        raise RuntimeError(f"{method} {path} -> {resp.status_code}: {resp.text[:800]}")
    return {}


def embeddings_client() -> OpenAI:
    provider = get_bearer_token_provider(credential(), "https://cognitiveservices.azure.com/.default")
    return OpenAI(base_url=f"{os.environ['AZURE_OPENAI_ENDPOINT'].rstrip('/')}/openai/v1/", api_key=provider)


def embed(client: OpenAI, texts: list[str], batch: int = 64) -> list[list[float]]:
    out: list[list[float]] = []
    model = os.environ.get("EMBEDDING_DEPLOYMENT_NAME", "text-embedding-3-large")
    for i in range(0, len(texts), batch):
        chunk = texts[i : i + batch]
        for attempt in range(6):
            try:
                resp = client.embeddings.create(model=model, input=chunk)
                out.extend(d.embedding for d in resp.data)
                break
            except Exception as exc:  # noqa: BLE001 - simple retry for 429
                if attempt == 5:
                    raise
                wait = 2 ** attempt
                print(f"  embeddings retry {attempt + 1} in {wait}s: {exc}")
                time.sleep(wait)
    return out


def index_schema(name: str, customer: bool) -> dict:
    fields = [
        {"name": "id", "type": "Edm.String", "key": True, "filterable": True},
        {"name": "doc_id", "type": "Edm.String", "filterable": True, "facetable": True},
        {"name": "title", "type": "Edm.String", "searchable": True, "analyzer": "en.microsoft"},
        {"name": "line", "type": "Edm.String", "filterable": True, "facetable": True},
        {"name": "doc_type", "type": "Edm.String", "filterable": True, "facetable": True},
        {"name": "page", "type": "Edm.Int32", "filterable": True, "sortable": True},
        {"name": "chunk", "type": "Edm.Int32", "filterable": True},
        {"name": "content", "type": "Edm.String", "searchable": True, "analyzer": "en.microsoft"},
        {
            "name": "content_vector",
            "type": "Collection(Edm.Single)",
            "searchable": True,
            "retrievable": False,
            "stored": False,
            "dimensions": EMBED_DIMS,
            "vectorSearchProfile": "hnsw-profile",
        },
    ]
    if customer:
        fields += [
            {"name": "session_id", "type": "Edm.String", "filterable": True},
            {"name": "customer_id", "type": "Edm.String", "filterable": True, "facetable": True},
            {"name": "source", "type": "Edm.String", "filterable": True, "facetable": True},
            {"name": "date", "type": "Edm.String", "filterable": True, "sortable": True},
            {"name": "created_at", "type": "Edm.DateTimeOffset", "filterable": True, "sortable": True},
        ]
    return {
        "name": name,
        "fields": fields,
        "vectorSearch": {
            "algorithms": [
                {
                    "name": "hnsw",
                    "kind": "hnsw",
                    "hnswParameters": {"metric": "cosine", "m": 4, "efConstruction": 400, "efSearch": 500},
                }
            ],
            "profiles": [{"name": "hnsw-profile", "algorithm": "hnsw"}],
        },
        "semantic": {
            "defaultConfiguration": "default",
            "configurations": [
                {
                    "name": "default",
                    "prioritizedFields": {
                        "titleField": {"fieldName": "title"},
                        "prioritizedContentFields": [{"fieldName": "content"}],
                    },
                }
            ],
        },
    }
