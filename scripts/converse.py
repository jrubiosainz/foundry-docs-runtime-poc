"""Scripted conversation against the agent with automatic checks against the ground truth.

Each conversation starts with "Good morning" (which triggers document retrieval) and then
continues with the customer's questions from `corpus/out/ground_truth.json`.

Usage:
  python scripts/converse.py CLI-0001
  python scripts/converse.py CLI-0099 --engine hosted --route index --types single_doc,image_only
  python scripts/converse.py CLI-0002 --engine filesearch --out results/filesearch.jsonl
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from common import CORPUS  # noqa: E402

GREETING = "Good morning"
_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
_MONTH = "(" + "|".join(_MONTHS + ["jan", "feb", "mar", "apr", "jun", "jul", "aug", "sept", "sep", "oct", "nov", "dec"]) + r")\.?"


def _month(name: str) -> int:
    return next(i for i, m in enumerate(_MONTHS, 1) if m.startswith(name[:3]))


def _dates(text: str) -> str:
    """English dates as in the documents: '16 September 2026' / 'September 16, 2026' -> 16/09/2026, 'May 2028' -> 05/2028."""
    text = re.sub(rf"\b(\d{{1,2}})(?:st|nd|rd|th)? (?:of )?{_MONTH},? (\d{{4}})\b", lambda m: f"{int(m[1]):02d}/{_month(m[2]):02d}/{m[3]}", text)
    text = re.sub(rf"\b{_MONTH} (\d{{1,2}})(?:st|nd|rd|th)?,? (\d{{4}})\b", lambda m: f"{int(m[2]):02d}/{_month(m[1]):02d}/{m[3]}", text)
    return re.sub(rf"\b{_MONTH},? (\d{{4}})\b", lambda m: f"{_month(m[1]):02d}/{m[2]}", text)


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = _dates(text)
    text = re.sub(r"(?<=\d)[.,\s](?=\d{3}\b)", "", text)  # 1,250 / 1.250 -> 1250
    return re.sub(r"[*_`]", "", text)


def _terms(expected: str) -> list[str]:
    terms = [t for t in re.findall(r"[\w-]+", _norm(expected)) if len(t) >= 3 or t.isdigit()]
    return [t[:-1] if len(t) > 4 and t.endswith("s") and not t[-2].isdigit() else t for t in terms]  # claims -> claim


def _has(term: str, norm: str) -> bool:
    """Numbers match whole numbers only ('20' is not found in '2026'); other terms match as substrings."""
    if term.isdigit():
        return re.search(rf"(?<!\d){term}(?!\d)", norm) is not None
    return term in norm


def check(answer: str, question: dict[str, Any]) -> bool:
    norm = _norm(answer)
    if any(_norm(f) in norm for f in question.get("forbidden", [])):
        return False
    if question["type"] == "negative_other_customer":
        return bool(
            re.search(
                r"\b(cannot|can ?not|can['’]?t|unable|not able|not allowed|not permitted|won['’]?t)\b|"
                r"\b(can|may) only (provide|share|help|discuss|give|access)\b|"
                r"\bother (customers?|clients?|person|people|individuals?)\b",
                norm,
            )
        )
    return all(all(_has(t, norm) for t in _terms(e)) for e in question["expected"])


def script_for(customer_id: str, types: set[str] | None, limit: int | None) -> list[dict[str, Any]]:
    """Customer questions, merging rows that share text (for example, multiple images)."""
    merged: dict[str, dict[str, Any]] = {}
    for q in json.loads((CORPUS / "ground_truth.json").read_text()):
        if q["customer_id"] != customer_id or (types and q["type"] not in types):
            continue
        entry = merged.setdefault(q["question"], {**q, "expected": [], "forbidden": [], "qids": []})
        entry["expected"] += q["expected"]
        entry["forbidden"] += q["forbidden"]
        entry["qids"].append(q["qid"])
    items = sorted(merged.values(), key=lambda q: (not q.get("showcase"), q["qids"][0]))
    return items[:limit] if limit else items


class AgentEngine:
    """Hosted agent (option 1) over the Responses protocol."""

    def __init__(self, route: str | None) -> None:
        self.route = route
        self.session: str | None = None
        self.prev: str | None = None

    def start(self, customer_id: str) -> str:
        from sessions import new_session

        self.session = new_session(customer_id, route=self.route)
        return self.session

    def turn(self, message: str) -> dict[str, Any]:
        from chat import stream_turn

        res = stream_turn(message, self.session, self.prev, quiet=True)
        self.prev = res.get("response_id") or self.prev
        usage = res.get("usage") or {}
        return {
            "answer": res.get("answer", ""),
            "progress": res.get("progress", ""),
            "tools": res.get("tools", []),
            "ttft_s": res.get("ttft_s"),
            "total_s": res.get("total_s"),
            "input_tokens": usage.get("input_tokens"),
            "cached_tokens": (usage.get("input_tokens_details") or {}).get("cached_tokens"),
            "error": res.get("error"),
        }

    def close(self) -> None:
        if not self.session:
            return
        from sessions import delete_session

        try:
            delete_session(self.session)
        except Exception as exc:  # noqa: BLE001 - best-effort cleanup
            print(f"warning: could not close session {self.session}: {exc}", file=sys.stderr)


def make_engine(name: str, route: str | None) -> Any:
    if name == "hosted":
        return AgentEngine(route=route)
    if name == "filesearch":
        from filesearch_agent import FileSearchEngine

        return FileSearchEngine()
    raise SystemExit(f"unknown engine: {name}")


def run(
    customer_id: str,
    engine_name: str,
    route: str | None,
    types: set[str] | None,
    limit: int | None,
    out: Path | None,
    quiet: bool = False,
    pause: float = 0,
) -> dict[str, Any]:
    engine = make_engine(engine_name, route)
    t0 = time.perf_counter()
    session = engine.start(customer_id)
    setup_s = round(time.perf_counter() - t0, 2)
    rows: list[dict[str, Any]] = []
    try:
        greet = engine.turn(GREETING)
        rows.append({"qid": "GREETING", "type": "greeting", "question": GREETING, "ok": bool(greet["answer"]) and not greet.get("error"), **greet})
        if not quiet:
            print(f"[{engine_name}] {customer_id} session {session} (setup {setup_s} s)")
            if greet.get("progress"):
                print(greet["progress"].rstrip())
            print(f"GREETING ttft {greet['ttft_s']} s total {greet['total_s']} s | {greet['answer'][:160]!r}")
        for q in script_for(customer_id, types, limit):
            time.sleep(pause)
            res = engine.turn(q["question"])
            ok = check(res["answer"], q) and not res.get("error")
            rows.append({"qid": ",".join(q["qids"]), "type": q["type"], "question": q["question"], "expected": q["expected"], "ok": ok, **res})
            if not quiet:
                flag = "OK " if ok else "KO "
                tools = ",".join(res["tools"]) or "-"
                print(
                    f"{flag}{q['qids'][0]:<6}{q['type']:<24} ttft {res['ttft_s'] or 0:5.2f}s total {res['total_s'] or 0:5.2f}s "
                    f"in {res['input_tokens'] or 0:6} cache {res['cached_tokens'] or 0:6} tools {tools:<28} | {res['answer'].strip()[:110]!r}"
                )
    finally:
        engine.close()
    questions = [r for r in rows if r["type"] != "greeting"]
    ttfts = [r["ttft_s"] for r in questions if r.get("ttft_s")]
    summary = {
        "customer_id": customer_id,
        "engine": engine_name,
        "route": route or "auto",
        "session": session,
        "setup_s": setup_s,
        "pause_s": pause,
        "greeting_total_s": rows[0].get("total_s"),
        "questions": len(questions),
        "ok": sum(r["ok"] for r in questions),
        "ttft_median_s": round(statistics.median(ttfts), 2) if ttfts else None,
        "ttft_p90_s": round(sorted(ttfts)[int(0.9 * (len(ttfts) - 1))], 2) if ttfts else None,
    }
    if not quiet:
        print(f"== {summary['ok']}/{summary['questions']} correct · median TTFT {summary['ttft_median_s']} s · p90 {summary['ttft_p90_s']} s")
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, default=str) + "\n")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("customer_id")
    parser.add_argument("--engine", choices=["hosted", "filesearch"], default="hosted")
    parser.add_argument("--route", choices=["cag", "index"])
    parser.add_argument("--types", help="comma-separated question types")
    parser.add_argument("--max", type=int)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--min-ok", type=int, help="exit with code 1 if there are fewer correct answers (smoke test)")
    parser.add_argument(
        "--pause",
        type=float,
        default=0,
        help="seconds to wait before each question; keeps large CAG conversations under the deployment TPM limit",
    )
    args = parser.parse_args()
    types = set(args.types.split(",")) if args.types else None
    summary = run(args.customer_id, args.engine, args.route, types, args.max, args.out, pause=args.pause)
    if args.min_ok is not None and summary["ok"] < args.min_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
