"""Upload the synthetic corpus to Blob Storage, simulating the two document sources.

- docs-dms/customer/<customer>/<doc>.pdf  -> insurer document management system
- docs-bank/customer/<customer>/<doc>.pdf -> documents received from the bank channel
- docs-dms/general/<line>/<doc>.pdf       -> general conditions (also indexed in AI Search)

Blob metadata (doc_id, title, type, line, date, pages) lets the agent list a customer's documents without downloading more than needed.
"""

from __future__ import annotations

import json
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import BlobServiceClient, ContentSettings

from common import BANK_CONTAINER, CORPUS, DMS_CONTAINER, credential
import os


def main() -> None:
    bsc = BlobServiceClient(os.environ["STORAGE_ACCOUNT_URL"], credential=credential())
    for name in (DMS_CONTAINER, BANK_CONTAINER):
        try:
            bsc.create_container(name)
            print(f"container created: {name}")
        except ResourceExistsError:
            print(f"container exists: {name}")

    jobs: list[tuple[str, str, object, dict]] = []
    customers = json.loads((CORPUS / "customers.json").read_text())
    for cust in customers:
        for doc in cust["docs"]:
            container = BANK_CONTAINER if doc["source"] == "bank" else DMS_CONTAINER
            meta = {
                "doc_id": doc["doc_id"],
                "customer_id": cust["customer_id"],
                "title": urllib.parse.quote(doc["title"]),
                "doc_type": doc["doc_type"],
                "line": doc.get("line") or "",
                "source": doc["source"],
                "date": doc.get("date") or "",
                "pages": str(doc.get("pages") or 0),
            }
            jobs.append((container, f"customer/{cust['customer_id']}/{doc['filename']}", CORPUS / doc["relpath"], meta))

    for doc in json.loads((CORPUS / "general" / "general_docs.json").read_text()):
        meta = {
            "doc_id": doc["doc_id"],
            "title": urllib.parse.quote(doc["title"]),
            "doc_type": doc["doc_type"],
            "line": doc["line"],
            "source": "dms",
            "pages": str(doc.get("pages") or 0),
        }
        jobs.append((DMS_CONTAINER, f"general/{doc['line']}/{doc['filename']}", CORPUS / doc["relpath"], meta))

    def upload(job):
        container, blob_name, path, meta = job
        data = path.read_bytes()
        bsc.get_blob_client(container, blob_name).upload_blob(
            data,
            overwrite=True,
            metadata=meta,
            content_settings=ContentSettings(content_type="application/pdf"),
        )
        return len(data)

    total = 0
    with ThreadPoolExecutor(max_workers=16) as pool:
        for i, size in enumerate(pool.map(upload, jobs), 1):
            total += size
            if i % 50 == 0 or i == len(jobs):
                print(f"  {i}/{len(jobs)} blobs ({total / 1e6:.1f} MB)")
    print(f"OK: {len(jobs)} blobs uploaded, {total / 1e6:.1f} MB")


if __name__ == "__main__":
    sys.exit(main())
