"""Create the AI Search indexes and index the general conditions (24 documents).

- idx-docs-general: general documents (conditions, coverage, copays, procedures).
- idx-docs-customer: empty index populated by the agent at runtime for customers
  with too much documentation to load fully into context ("index" route).
"""

from __future__ import annotations

import json
import sys

from common import CORPUS, GENERAL_INDEX, CUSTOMER_INDEX, embed, embeddings_client, index_schema, search_request


def ensure_index(name: str, customer: bool, recreate: bool) -> None:
    if recreate:
        try:
            search_request("DELETE", f"/indexes/{name}", ok=(204, 404))
        except RuntimeError:
            pass
    search_request("PUT", f"/indexes/{name}", index_schema(name, customer), ok=(200, 201, 204))
    print(f"index ready: {name}")


def main() -> None:
    recreate = "--recreate" in sys.argv
    ensure_index(GENERAL_INDEX, customer=False, recreate=recreate)
    ensure_index(CUSTOMER_INDEX, customer=True, recreate=recreate)

    docs = json.loads((CORPUS / "general" / "general_docs.json").read_text())
    chunks: list[dict] = []
    for doc in docs:
        pages = json.loads((CORPUS / "general" / doc["line"] / f"{doc['doc_id']}.json").read_text())["pages"]
        for page in pages:
            text = page["text"].strip()
            if not text:
                continue
            chunks.append(
                {
                    "id": f"{doc['doc_id']}-p{page['page']}",
                    "doc_id": doc["doc_id"],
                    "title": doc["title"],
                    "line": doc["line"],
                    "doc_type": doc["doc_type"],
                    "page": page["page"],
                    "chunk": 0,
                    "content": text,
                }
            )

    client = embeddings_client()
    vectors = embed(client, [f"{c['title']} · p. {c['page']}\n{c['content']}" for c in chunks])
    for c, v in zip(chunks, vectors, strict=True):
        c["content_vector"] = v
        c["@search.action"] = "mergeOrUpload"

    for i in range(0, len(chunks), 100):
        res = search_request("POST", f"/indexes/{GENERAL_INDEX}/docs/index", {"value": chunks[i : i + 100]})
        failed = [r for r in res.get("value", []) if not r.get("status")]
        if failed:
            raise RuntimeError(f"indexing failures: {failed[:3]}")
    print(f"OK: {len(chunks)} chunks indexed in {GENERAL_INDEX} ({len(docs)} documents)")


if __name__ == "__main__":
    sys.exit(main())
