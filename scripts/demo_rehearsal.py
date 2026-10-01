"""Demo rehearsal: runs the README demo path against the console and checks the answers.

It uses the console API (demo/server.py), so it also tests the console. It opens and closes sessions
like the live demo, serially to stay away from the TPM limit, and grades each answer with the same
checker as the benchmark. At the end it verifies that nothing is left: no console sessions, no chunks
in the customer index, and no vector stores in the project.

Usage, with the console running and from the prototype root:
  .venv/bin/python scripts/demo_rehearsal.py                         # full path, about 4-5 minutes
  .venv/bin/python scripts/demo_rehearsal.py --quick                 # only warm up the hosted agent (CLI-0003), under 1 minute
  .venv/bin/python scripts/demo_rehearsal.py --scenarios volume,index
  .venv/bin/python scripts/demo_rehearsal.py --cleanup               # after the demo: close leftovers and verify cleanup
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from common import CORPUS, CUSTOMER_INDEX, search_request  # noqa: E402
from converse import GREETING, check  # noqa: E402

SCENARIOS = ["typical", "isolation", "volume", "index", "filesearch"]

# Demo-path questions not present in the ground truth. Odontogram facts only exist in the image.
SCRIPT = {
    "ODONTO": {
        "customer_id": "CLI-0001",
        "type": "image_only",
        "question": "Which teeth are marked in the odontogram in my estimate?",
        "expected": ["36: molar root canal", "46: zirconia crown"],
        "forbidden": [],
    },
}


def question(qid: str) -> dict[str, Any]:
    """Demo-path or ground-truth question, merging rows that share text (multiple images in one document)."""
    if qid in SCRIPT:
        return {"qid": qid, **SCRIPT[qid]}
    gt = json.loads((CORPUS / "ground_truth.json").read_text())
    base = next(q for q in gt if q["qid"] == qid)
    same = [q for q in gt if q["customer_id"] == base["customer_id"] and q["question"] == base["question"]]
    return {**base, "expected": [e for q in same for e in q["expected"]], "forbidden": [f for q in same for f in q["forbidden"]]}


def image_facts(customer_id: str) -> dict[str, list[str]]:
    """Image-only facts for the customer, grouped by document."""
    groups: dict[str, list[str]] = {}
    for f in json.loads((CORPUS / "image_facts.json").read_text()):
        if f["customer_id"] == customer_id:
            groups.setdefault(f["doc_id"], []).append(f["value"])
    return groups


def fmt(x: float | None) -> str:
    return "—" if x is None else f"{x:.1f} s"


class Rehearsal:
    def __init__(self, base: str) -> None:
        self.http = httpx.Client(base_url=base, timeout=httpx.Timeout(300, connect=10))
        self.keys: dict[str, str] = {}
        self.failures: list[str] = []

    def _mark(self, ok: bool, text: str) -> None:
        print(f"  {'✓' if ok else '✗'} {text}", flush=True)
        if not ok:
            self.failures.append(text)

    def open(self, alias: str, cid: str, engine: str, route: str = "") -> None:
        r = self.http.post("/api/sessions", json={"customer_id": cid, "engine": engine, "route": route})
        r.raise_for_status()
        s = r.json()
        self.keys[alias] = s["key"]
        route_name = {"": "automatic", "cag": "forced CAG", "index": "forced index"}[route]
        print(f"\n▶ {cid} · {engine} · route {route_name} · setup {fmt(s['setup_s'])}", flush=True)

    def turn(self, alias: str, message: str) -> dict[str, Any]:
        done: dict[str, Any] = {}
        progress: list[str] = []
        error = None
        with self.http.stream("POST", f"/api/sessions/{self.keys[alias]}/turns", json={"message": message}) as r:
            r.raise_for_status()
            event = None
            for line in r.iter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:") and event:
                    data = json.loads(line[5:].strip())
                    if event == "progress":
                        progress.append(data.get("text", ""))
                    elif event == "done":
                        done = data
                    elif event == "error":
                        error = data.get("message")
        return {**done, "progress_lines": progress, "error": error or done.get("error")}

    def greet(self, alias: str, markers: list[str]) -> None:
        t = self.turn(alias, GREETING)
        for line in t["progress_lines"]:
            print(f"      {line}")
        progress = " ".join(t["progress_lines"])
        missing = [m for m in markers if m not in progress]
        ok = not t["error"] and not missing and bool(t.get("answer"))
        detail = f"missing '{missing[0]}'" if missing else (t["error"] or "")
        self._mark(ok, f"'{GREETING}' · first token {fmt(t.get('ttft_s'))} · total {fmt(t.get('total_s'))} {detail}".rstrip())

    def ask(self, alias: str, qid: str, expect_hit: bool = True, any_image: bool = False) -> None:
        q = question(qid)
        t = self.turn(alias, q["question"])
        # The generic image question does not specify which image, so any customer image is valid.
        options = image_facts(q["customer_id"]) if any_image else {"": q["expected"]}
        read = next((doc for doc, exp in options.items() if not t["error"] and check(t.get("answer") or "", {**q, "expected": exp})), None)
        hit = read is not None
        cache = ""
        if t.get("input_tokens"):
            cache = f" · cache {100 * (t.get('cached_tokens') or 0) / t['input_tokens']:.0f}% of {t['input_tokens']:,} tk"
        note = "" if expect_hit else (" · does not resolve it, as expected" if not hit else " · it resolved it! Review the path")
        image = f" · image from {read}" if any_image and read else ""
        text = f"{qid} {q['question'][:60]} · first token {fmt(t.get('ttft_s'))} · total {fmt(t.get('total_s'))}{cache}{image}{note}"
        self._mark(hit == expect_hit, text)
        if hit != expect_hit or not expect_hit:
            print(f"      expected: {' or '.join(map(str, options.values())) if any_image else q['expected']}")
            print(f"      answer: {(t.get('answer') or t['error'] or '').strip().replace(chr(10), ' ')[:300]}")

    def close(self, alias: str) -> None:
        key = self.keys.pop(alias, None)
        if key:
            self.http.delete(f"/api/sessions/{key}").raise_for_status()

    def close_all(self) -> None:
        for alias in list(self.keys):
            try:
                self.close(alias)
            except Exception as exc:  # noqa: BLE001 - best-effort cleanup
                print(f"  ! could not close {alias}: {exc}")

    def close_console(self) -> None:
        """Close sessions still open in the console, such as live-demo leftovers."""
        for s in self.http.get("/api/sessions").json():
            self.http.delete(f"/api/sessions/{s['key']}").raise_for_status()
            print(f"  · closed {s['customer_id']} ({s['engine']})")

    def check_cleanup(self) -> None:
        print("\n▶ Cleanup")
        open_sessions = self.http.get("/api/sessions").json()
        self._mark(not open_sessions, f"open console sessions: {len(open_sessions)}")
        chunks = None
        for _ in range(10):
            chunks = search_request("POST", f"/indexes/{CUSTOMER_INDEX}/docs/search", {"search": "*", "count": True, "top": 0})["@odata.count"]
            if not chunks:
                break
            time.sleep(1)
        self._mark(chunks == 0, f"chunks in {CUSTOMER_INDEX}: {chunks}")
        from filesearch_agent import openai_client

        stores = list(openai_client().vector_stores.list())
        self._mark(not stores, f"vector stores in the project: {len(stores)}")


def run(rehearsal: Rehearsal, scenarios: list[str]) -> None:
    if "typical" in scenarios or "isolation" in scenarios:
        rehearsal.open("lucia", "CLI-0001", "hosted")
        rehearsal.greet("lucia", ["Session authenticated by the backend", "bank channel", "CAG route", "Customer context ready"])
        rehearsal.ask("lucia", "Q0005")  # general + customer: root canal, €110
        rehearsal.ask("lucia", "ODONTO")  # image: teeth 36 and 46 in the odontogram
        rehearsal.ask("lucia", "Q0003", any_image=True)  # image-fact suggestion: odontogram, stamp, or card
    if "isolation" in scenarios:
        rehearsal.open("javier", "CLI-0002", "hosted")
        rehearsal.greet("javier", ["CAG route"])
        rehearsal.ask("lucia", "Q0007")  # Lucia asks about Javier: refusal
        rehearsal.ask("javier", "Q0013")  # Javier's canary
    rehearsal.close_all()
    if "volume" in scenarios:
        rehearsal.open("workshop", "CLI-0099", "hosted")
        rehearsal.greet("workshop", ["58 docs", "17 docs", "CAG route"])
        rehearsal.ask("workshop", "Q0183")  # count: 10 loss adjuster reports
        rehearsal.ask("workshop", "Q0180")  # image: plate 3812-KLM
        rehearsal.close_all()
    if "index" in scenarios:
        rehearsal.open("workshop-index", "CLI-0099", "hosted", "index")
        rehearsal.greet("workshop-index", ["Index route", "chunks indexed", "images in context"])
        rehearsal.ask("workshop-index", "Q0180")
        rehearsal.close_all()
    if "filesearch" in scenarios:
        rehearsal.open("lucia-fs", "CLI-0001", "filesearch")
        rehearsal.greet("lucia-fs", ["Vector store", "files indexed", "Customer context ready"])
        rehearsal.ask("lucia-fs", "ODONTO", expect_hit=False)  # File Search cannot see images
        rehearsal.ask("lucia-fs", "Q0003", expect_hit=False, any_image=True)
        rehearsal.ask("lucia-fs", "Q0005")
        rehearsal.close_all()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="http://127.0.0.1:8780", help="console URL")
    ap.add_argument("--quick", action="store_true", help="only warm up the hosted agent with CLI-0003")
    ap.add_argument("--cleanup", action="store_true", help="only close open console sessions and verify nothing is left")
    ap.add_argument("--scenarios", default=",".join(SCENARIOS), help=f"subset of {','.join(SCENARIOS)}")
    args = ap.parse_args()
    scenarios = [x.strip() for x in args.scenarios.split(",") if x.strip()]
    unknown = sorted(set(scenarios) - set(SCENARIOS))
    if unknown:
        ap.error(f"unknown scenarios: {', '.join(unknown)}")

    rehearsal = Rehearsal(args.base)
    t0 = time.perf_counter()
    try:
        rehearsal.http.get("/api/sessions").raise_for_status()
    except httpx.HTTPError as exc:
        sys.exit(f"The console is not responding at {args.base}: {exc}")
    try:
        if args.cleanup:
            rehearsal.close_console()
        elif args.quick:
            rehearsal.open("warmup", "CLI-0003", "hosted")
            rehearsal.greet("warmup", ["CAG route"])
            rehearsal.ask("warmup", "Q0015")
        else:
            run(rehearsal, scenarios)
    finally:
        rehearsal.close_all()
        rehearsal.check_cleanup()
    print(f"\n{'All good' if not rehearsal.failures else f'{len(rehearsal.failures)} failure(s)'} · {fmt(time.perf_counter() - t0)}")
    sys.exit(1 if rehearsal.failures else 0)


if __name__ == "__main__":
    main()
