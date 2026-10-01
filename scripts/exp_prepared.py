"""Experiment: customer documents as raw PDF vs "prepared" (per-page text + embedded images).

Usage: python scripts/exp_prepared.py [CLI-0001] [n_calls]
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import time
from pathlib import Path

from pypdf import PdfReader

sys.path.insert(0, str(Path(__file__).parent))
from azure.identity import get_bearer_token_provider  # noqa: E402
from common import CORPUS, credential  # noqa: E402
from openai import OpenAI  # noqa: E402

CID = sys.argv[1] if len(sys.argv) > 1 else "CLI-0001"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 3
MODEL = os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-5.6-terra")
client = OpenAI(
    base_url=os.environ["AZURE_OPENAI_ENDPOINT"] + "/openai/v1/",
    api_key=get_bearer_token_provider(credential(), "https://cognitiveservices.azure.com/.default"),
)
customer = next(c for c in json.loads((CORPUS / "customers.json").read_text()) if c["customer_id"] == CID)
QUESTIONS = [
    "Which teeth appear in the odontogram and with which treatment?",
    "Until when is my health card valid?",
    "What code does the authorization have and what is its status?",
]


def pdf_parts() -> list[dict]:
    parts = []
    for d in customer["docs"]:
        data = (CORPUS / d["relpath"]).read_bytes()
        parts.append({"type": "input_text", "text": f"=== DOCUMENT [{d['doc_id']}] {d['title']} ==="})
        parts.append({"type": "input_file", "filename": f"{d['doc_id']}.pdf", "file_data": "data:application/pdf;base64," + base64.b64encode(data).decode()})
    return parts


def prepared_parts() -> list[dict]:
    parts = []
    for d in customer["docs"]:
        reader = PdfReader(io.BytesIO((CORPUS / d["relpath"]).read_bytes()))
        lines = [f"=== DOCUMENT [{d['doc_id']}] {d['title']} ==="]
        for n, page in enumerate(reader.pages, 1):
            lines.append(f"--- [{d['doc_id']}] p. {n} ---\n{(page.extract_text() or '').strip()}")
            images = list(page.images)
            if images:
                parts.append({"type": "input_text", "text": "\n".join(lines)})
                lines = []
                for k, img in enumerate(images, 1):
                    mime = "image/png" if img.name.lower().endswith(".png") else "image/jpeg"
                    parts.append({"type": "input_text", "text": f"[Embedded image {k} in [{d['doc_id']}], p. {n}]"})
                    parts.append({"type": "input_image", "image_url": f"data:{mime};base64," + base64.b64encode(img.data).decode(), "detail": "high"})
        if lines:
            parts.append({"type": "input_text", "text": "\n".join(lines)})
    return parts


def run(label: str, parts: list[dict]) -> None:
    key = f"exp:{label}:{CID}"
    for i in range(N):
        for q in QUESTIONS:
            t0 = time.perf_counter()
            first = None
            text = ""
            usage = None
            stream = client.responses.create(
                model=MODEL,
                instructions="Reply briefly in the language of the question, citing [doc_id]. Also examine the images.",
                input=[{"role": "user", "content": parts}, {"role": "user", "content": q}],
                reasoning={"effort": "low"},
                prompt_cache_key=key,
                store=False,
                stream=True,
            )
            for ev in stream:
                if ev.type == "response.output_text.delta":
                    first = first or time.perf_counter() - t0
                    text += ev.delta
                elif ev.type == "response.completed":
                    usage = ev.response.usage
            cached = usage.input_tokens_details.cached_tokens if usage else 0
            print(f"{label:<8} it{i} ttft {first:5.2f}s total {time.perf_counter() - t0:5.2f}s in {usage.input_tokens:6d} cached {cached:6d} | {text.strip()[:110]!r}")


if __name__ == "__main__":
    run("prepared", prepared_parts())
    run("pdf", pdf_parts())
