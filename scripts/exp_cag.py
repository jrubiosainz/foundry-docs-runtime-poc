"""Experiment: PDFs in context (inline vs file_id) + prompt cache with explicit breakpoint."""

from __future__ import annotations

import base64
import json
import sys
import time

from azure.ai.projects import AIProjectClient

from common import CORPUS, credential
import os

MODE = sys.argv[1] if len(sys.argv) > 1 else "inline"
CUSTOMER = sys.argv[2] if len(sys.argv) > 2 else "CLI-0001"
MODEL = os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-5.6-terra")

project = AIProjectClient(endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"], credential=credential())
client = project.get_openai_client()

cust = next(c for c in json.loads((CORPUS / "customers.json").read_text()) if c["customer_id"] == CUSTOMER)
parts: list[dict] = [{"type": "input_text", "text": f"CUSTOMER DOCUMENTS {CUSTOMER} ({len(cust['docs'])} documents)"}]
t0 = time.perf_counter()
file_ids = []
for d in cust["docs"]:
    data = (CORPUS / d["relpath"]).read_bytes()
    parts.append({"type": "input_text", "text": f"=== DOCUMENT {d['doc_id']} · {d['title']} · source {d['source']} ==="})
    if MODE == "inline":
        parts.append({"type": "input_file", "filename": d["filename"], "file_data": "data:application/pdf;base64," + base64.b64encode(data).decode()})
    else:
        f = client.files.create(file=(d["filename"], data, "application/pdf"), purpose="assistants")
        file_ids.append(f.id)
        parts.append({"type": "input_file", "file_id": f.id})
prep = time.perf_counter() - t0
parts.append({"type": "input_text", "text": "=== END DOCUMENTATION ===", "prompt_cache_breakpoint": {"mode": "explicit"}})
print(f"mode={MODE} preparation={prep:.2f}s docs={len(cust['docs'])}")

questions = [
    "What data appears in the dental-estimate odontogram? Cite the document.",
    "What is my internal case file code? Answer only the code.",
    "What authorization number appears in the authorization? And until when is the health card valid?",
]
for q in questions:
    t = time.perf_counter()
    first = None
    text = ""
    usage = None
    stream = client.responses.create(
        model=MODEL,
        instructions="You are an insurer customer-support assistant. Reply briefly in the language of the question, citing [doc_id].",
        input=[{"role": "user", "content": parts}, {"role": "user", "content": q}],
        prompt_cache_key=f"exp:{CUSTOMER}:{MODE}",
        store=False,
        stream=True,
        reasoning={"effort": "low"},
    )
    for ev in stream:
        if ev.type == "response.output_text.delta":
            if first is None:
                first = time.perf_counter() - t
            text += ev.delta
        elif ev.type == "response.completed":
            usage = ev.response.usage
    total = time.perf_counter() - t
    det = usage.input_tokens_details if usage else None
    print(f"\nQ: {q}\n→ {text.strip()[:300]}")
    print(
        f"  TTFT={first or 0:.2f}s total={total:.2f}s input={usage.input_tokens if usage else '?'} "
        f"cached={getattr(det, 'cached_tokens', None)} cache_write={getattr(det, 'cache_write_tokens', None)} out={usage.output_tokens if usage else '?'}"
    )

for fid in file_ids:
    try:
        client.files.delete(fid)
    except Exception:
        pass
