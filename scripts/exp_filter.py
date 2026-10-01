"""Experiment: can File Search partition a SHARED Foundry vector store by customer?

Against the project endpoint (.../api/projects/<p>/openai/v1), checks:
  1  Per-file attribute support: files.create(attributes), file_batches.create(attributes),
     file_batches.create(files=[...]), files.update(attributes), and later reads (retrieve).
  2  Direct vector_stores.search (with an Entra ID token).
  3  Filter effect in File Search (Responses API, tool_choice=required) over a store with files
     for CLI-0001 and CLI-0002: no filter, customer_id=CLI-0001 filter, and a filter with a
     nonexistent value (if this returns results, the filter is ignored).
  4  Shared prompt agent with a customer_id eq {{customer_id}} template filter (F5 test).

Usage: python scripts/exp_filter.py   -> results/filter-experiment.json
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from common import ROOT  # noqa: E402
from filesearch_agent import AGENT_NAME, INSTRUCTIONS, MODEL, fetch_customer_docs, openai_client  # noqa: E402
from sessions import customers, project  # noqa: E402

OWN, OTHER = "CLI-0001", "CLI-0002"
QUESTIONS = [
    "What coverage and deductible do I have in my home insurance?",
    "Summarize which policies and documents appear in my documentation.",
    "Do I have any pending or returned receipts?",
]


def by_customer(filenames: list[str]) -> dict[str, int]:
    return dict(Counter("-".join(n.split("-")[:2]) for n in filenames))


def err(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc)[:220]}"


def sanitized_endpoint(_: Any) -> str:
    return "https://<account>.services.ai.azure.com/api/projects/<project>/openai/v1/"


def responses_call(vs_id: str, question: str, flt: dict[str, Any] | None) -> dict[str, Any]:
    tool: dict[str, Any] = {"type": "file_search", "vector_store_ids": [vs_id], "max_num_results": 8}
    if flt:
        tool["filters"] = flt
    c = customers()[OWN]
    resp = openai_client().responses.create(
        model=MODEL,
        instructions=INSTRUCTIONS.replace("{{customer_name}}", c["full_name"]).replace("{{customer_id}}", OWN),
        input=question,
        tools=[tool],
        tool_choice="required",
        include=["file_search_call.results"],
        reasoning={"effort": "low"},
    )
    names = [r.filename for item in resp.output if item.type == "file_search_call" for r in (item.results or [])]
    return {"question": question, "calls": sum(item.type == "file_search_call" for item in resp.output), "retrieved_by_customer": by_customer(names)}


def agent_call(agent: str, vs_id: str, question: str) -> dict[str, Any]:
    oai = openai_client()
    c = customers()[OWN]
    products = "; ".join(f"{p['line']}: {p['plan']} (policy {p['policy']})" for p in c["products"])
    conv = oai.conversations.create().id
    try:
        resp = oai.responses.create(
            conversation=conv,
            input=question,
            include=["file_search_call.results"],
            extra_body={
                "agent_reference": {"name": agent, "type": "agent_reference"},
                "structured_inputs": {"vector_store_id": vs_id, "customer_id": OWN, "customer_name": c["full_name"], "products": products},
            },
        )
    finally:
        oai.conversations.delete(conv)
    names = [r.filename for item in resp.output if item.type == "file_search_call" for r in (item.results or [])]
    return {"question": question, "calls": sum(item.type == "file_search_call" for item in resp.output), "retrieved_by_customer": by_customer(names)}


def wait_completed(oai: Any, store_id: str, file_ids: list[str]) -> list[str]:
    for _ in range(120):
        st = [oai.vector_stores.files.retrieve(file_id=f, vector_store_id=store_id).status for f in file_ids]
        if all(s in ("completed", "failed", "cancelled") for s in st):
            return st
        time.sleep(1)
    return st


def main() -> None:
    oai = openai_client()
    out: dict[str, Any] = {"endpoint": sanitized_endpoint(oai.base_url), "questions": QUESTIONS}
    store = oai.vector_stores.create(name="docs-filter-experiment", expires_after={"anchor": "last_active_at", "days": 1})
    uploaded: list[str] = []
    try:
        per_customer: dict[str, list[str]] = {}
        for cid in (OWN, OTHER):
            docs, _ = fetch_customer_docs(cid)
            with ThreadPoolExecutor(8) as pool:
                per_customer[cid] = list(pool.map(lambda d: oai.files.create(file=(d[0], d[1], "application/pdf"), purpose="assistants").id, docs))
            uploaded += per_customer[cid]

        attempts: dict[str, str] = {}
        for cid, ids in per_customer.items():
            attrs = {"customer_id": cid}
            first, rest = ids[0], ids[1:]
            try:
                oai.vector_stores.files.create(vector_store_id=store.id, file_id=first, attributes=attrs)
                attempts["files.create(attributes)"] = "accepted"
            except Exception as exc:  # noqa: BLE001
                attempts["files.create(attributes)"] = err(exc)
            try:
                oai.vector_stores.file_batches.create_and_poll(vector_store_id=store.id, file_ids=rest, attributes=attrs, poll_interval_ms=500)
                attempts["file_batches.create(attributes)"] = "accepted"
            except Exception as exc:  # noqa: BLE001
                attempts["file_batches.create(attributes)"] = err(exc)
        all_ids = [f for ids in per_customer.values() for f in ids]
        out["file_status"] = dict(Counter(wait_completed(oai, store.id, all_ids)))
        try:
            oai.vector_stores.file_batches.create(vector_store_id=store.id, files=[{"file_id": all_ids[0], "attributes": {"customer_id": OWN}}])
            attempts["file_batches.create(files=[...])"] = "accepted"
        except Exception as exc:  # noqa: BLE001
            attempts["file_batches.create(files=[...])"] = err(exc)
        try:
            oai.vector_stores.files.update(file_id=all_ids[0], vector_store_id=store.id, attributes={"customer_id": OWN})
            attempts["files.update(attributes)"] = "accepted"
        except Exception as exc:  # noqa: BLE001
            attempts["files.update(attributes)"] = err(exc)
        read_back = [json.loads(oai.vector_stores.files.with_raw_response.retrieve(file_id=f, vector_store_id=store.id).text).get("attributes") for f in all_ids]
        out["attributes"] = {"attempts": attempts, "read": dict(Counter(json.dumps(a, sort_keys=True) for a in read_back))}
        print("1 attributes:", json.dumps(out["attributes"], ensure_ascii=False), flush=True)

        try:
            res = oai.vector_stores.search(vector_store_id=store.id, query=QUESTIONS[0], max_num_results=5)
            out["direct_search"] = {"results": len(res.data)}
        except Exception as exc:  # noqa: BLE001
            out["direct_search"] = {"error": err(exc)}
        print("2 direct search:", out["direct_search"], flush=True)

        variants = {
            "no_filter": None,
            "filter_CLI-0001": {"type": "eq", "key": "customer_id", "value": OWN},
            "filter_nonexistent_value": {"type": "eq", "key": "customer_id", "value": "DOES-NOT-EXIST"},
        }
        jobs = [(name, q) for name in variants for q in QUESTIONS]
        with ThreadPoolExecutor(len(jobs)) as pool:
            results = list(pool.map(lambda j: responses_call(store.id, j[1], variants[j[0]]), jobs))
        out["responses_api"] = {name: [r for (n, _), r in zip(jobs, results) if n == name] for name in variants}
        for name, rows in out["responses_api"].items():
            print(f"3 responses {name}:", [r["retrieved_by_customer"] for r in rows], flush=True)

        shared_agent = f"{AGENT_NAME}-shared"
        with ThreadPoolExecutor(len(QUESTIONS)) as pool:
            out["agent_filter_template"] = list(pool.map(lambda q: agent_call(shared_agent, store.id, q), QUESTIONS))
        print("4 agent {{customer_id}}:", [r["retrieved_by_customer"] for r in out["agent_filter_template"]], flush=True)
        try:
            details = project().agents.get(agent_name=shared_agent)
            tool = details.versions.latest.definition.tools[0]
            out["filter_saved_in_agent"] = str(tool.get("filters") if hasattr(tool, "get") else getattr(tool, "filters", None))
        except Exception as exc:  # noqa: BLE001
            out["filter_saved_in_agent"] = err(exc)
        print("  saved filter:", out["filter_saved_in_agent"], flush=True)
    finally:
        oai.vector_stores.delete(store.id)
        with ThreadPoolExecutor(8) as pool:
            list(pool.map(lambda fid: oai.files.delete(fid), uploaded))
    (ROOT / "results" / "filter-experiment.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print("-> results/filter-experiment.json")


if __name__ == "__main__":
    main()
