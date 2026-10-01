"""Runtime customer documentation demo console.

The console acts as the insurance backend: it decides which customer is authenticated, opens the session
with that identity (never taking it from chat), and relays browser progress, answers, and measured turn
metrics.

Engines:
  hosted      Option 1 · hosted agent (MAF) deployed in Foundry — docs-runtime-agent
  filesearch  Option 2 · prompt agent + File Search with one vector store per session

Startup:
  set -a && . ./.env && set +a
  .venv/bin/python demo/server.py            # http://localhost:8780
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import threading
import time
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import chat  # noqa: E402
from common import CORPUS, credential  # noqa: E402
from sessions import AGENT_NAME, customers, delete_session, latest_version, new_session, project  # noqa: E402

STATIC = Path(__file__).parent / "static"
RESULTS = ROOT / "results"
ENGINES = ("hosted", "filesearch")
SHOWCASE_ORDER = ("general_plus_customer", "image_only", "cross_doc", "single_doc", "canary", "negative_other_customer")

log = logging.getLogger("docs.demo")


@dataclass
class DemoSession:
    key: str
    customer_id: str
    engine: str
    route: str | None
    created_at: float
    setup_s: float | None = None
    agent_session_id: str | None = None
    prev_response_id: str | None = None
    fs: Any = None
    turns: list[dict[str, Any]] = field(default_factory=list)
    progress: list[dict[str, Any]] = field(default_factory=list)
    server: dict[str, Any] = field(default_factory=dict)
    busy: bool = False
    turn_token: Any = None
    closed: bool = False

    def view(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "customer_id": self.customer_id,
            "engine": self.engine,
            "route": self.route,
            "setup_s": self.setup_s,
            "agent_session_id": self.agent_session_id,
            "vector_store_id": getattr(self.fs, "vector_store_id", None),
            "turns": self.turns,
            "progress": self.progress,
            "server": self.server,
            "busy": self.busy,
        }


SESSIONS: dict[str, DemoSession] = {}


# --------------------------------------------------------------------------------------------
# Synthetic customer data
# --------------------------------------------------------------------------------------------


@lru_cache(maxsize=1)
def ground_truth() -> list[dict[str, Any]]:
    return json.loads((CORPUS / "ground_truth.json").read_text())


def suggested_questions(cid: str, limit: int = 6) -> list[dict[str, str]]:
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    by_type: dict[str, list[dict[str, Any]]] = {}
    for q in ground_truth():
        if q["customer_id"] == cid:
            by_type.setdefault(q["type"], []).append(q)
    for kind in SHOWCASE_ORDER:
        for q in sorted(by_type.get(kind, []), key=lambda x: not x.get("showcase")):
            if q["question"] in seen:
                continue
            seen.add(q["question"])
            out.append({"type": kind, "question": q["question"]})
            break
    return out[:limit]


def customer_view(c: dict[str, Any]) -> dict[str, Any]:
    docs = [
        {
            "doc_id": d["doc_id"],
            "title": d["title"],
            "doc_type": d["doc_type"],
            "line": d.get("line"),
            "source": d.get("source"),
            "date": d.get("date"),
            "pages": d.get("pages", 0),
            "bytes": d.get("bytes", 0),
            "images": len(d.get("image_facts") or []),
        }
        for d in c["docs"]
    ]
    return {
        "customer_id": c["customer_id"],
        "full_name": c["full_name"],
        "kind": c.get("kind"),
        "products": [{"line": p["line"], "plan": p["plan"], "policy": p["policy"]} for p in c.get("products", [])],
        "docs": docs,
        "n_docs": len(docs),
        "pages": sum(d["pages"] for d in docs),
        "bytes": sum(d["bytes"] for d in docs),
        "images": sum(d["images"] for d in docs),
        "suggested": suggested_questions(c["customer_id"]),
    }


@lru_cache(maxsize=1)
def hosted_version() -> str:
    return latest_version(AGENT_NAME)


# --------------------------------------------------------------------------------------------
# Server-side metrics (hosted): files the agent writes in its sandbox
# --------------------------------------------------------------------------------------------


def read_agent_file(s: DemoSession, path: str) -> bytes | None:
    try:
        return b"".join(project().agents.download_session_file(agent_name=AGENT_NAME, session_id=s.agent_session_id, path=path))
    except Exception as exc:  # noqa: BLE001 - server metrics are informational
        log.info("missing %s for %s: %s", path, s.key, exc)
        return None


def server_metrics(s: DemoSession) -> dict[str, Any]:
    out: dict[str, Any] = {}
    init = read_agent_file(s, "docs-agent/init_metrics.json")
    if init:
        out["init"] = json.loads(init)
    turns = read_agent_file(s, "docs-agent/turns.jsonl")
    if turns:
        out["turns"] = [json.loads(line) for line in turns.decode().splitlines() if line.strip()]
    return out


def usage_numbers(usage: dict[str, Any] | None) -> dict[str, Any]:
    usage = usage or {}
    details = usage.get("input_tokens_details") or {}
    return {
        "input_tokens": usage.get("input_tokens"),
        "cached_tokens": details.get("cached_tokens"),
        "output_tokens": usage.get("output_tokens"),
    }


# --------------------------------------------------------------------------------------------
# Turns: hosted (Responses protocol proxy) and File Search (thread + queue)
# --------------------------------------------------------------------------------------------


def ev(kind: str, **payload: Any) -> dict[str, str]:
    return {"event": kind, "data": json.dumps(payload, ensure_ascii=False, default=str)}


async def hosted_turn(s: DemoSession, message: str) -> AsyncIterator[dict[str, str]]:
    url, headers = await asyncio.to_thread(chat.endpoint, AGENT_NAME)
    body: dict[str, Any] = {"input": message, "stream": True, "agent_session_id": s.agent_session_id}
    if s.prev_response_id:
        body["previous_response_id"] = s.prev_response_id
    t0 = time.perf_counter()
    ttft: float | None = None
    items: dict[str, dict[str, Any]] = {}
    turn: dict[str, Any] = {"message": message, "answer": "", "tools": [], "progress": [], "engine": s.engine}
    usage: dict[str, Any] | None = None
    error: Any = None
    async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=30)) as client:
        async with client.stream("POST", url, json=body, headers=headers) as resp:
            if resp.status_code >= 400:
                text = (await resp.aread()).decode(errors="replace")[:1500]
                yield ev("error", message=f"HTTP {resp.status_code}: {text}")
                return
            event_type = ""
            async for line in resp.aiter_lines():
                if line.startswith("event:"):
                    event_type = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                data = json.loads(line[5:].strip())
                etype = data.get("type", event_type)
                now = time.perf_counter() - t0
                if etype == "response.output_item.added":
                    item = data["item"]
                    items[item["id"]] = {"type": item.get("type"), "text": "", "emitted": 0}
                    if item.get("type") == "function_call":
                        turn["tools"].append(item.get("name"))
                        yield ev("tool", name=item.get("name"), phase="start", t=round(now, 2))
                elif etype == "response.output_text.delta":
                    it = items.setdefault(data.get("item_id"), {"type": "message", "text": "", "emitted": 0})
                    delta = data.get("delta", "")
                    progress = it["text"].startswith("▸") or (not it["text"] and delta.startswith("▸"))
                    it["text"] += delta
                    if progress:
                        lines = it["text"].split("\n")
                        for complete in lines[it["emitted"] : len(lines) - 1]:
                            if complete.strip():
                                entry = {"text": complete.strip(), "t": round(now, 2)}
                                turn["progress"].append(entry)
                                yield ev("progress", **entry)
                        it["emitted"] = len(lines) - 1
                    elif delta:
                        if ttft is None:
                            ttft = now
                        turn["answer"] += delta
                        yield ev("delta", text=delta)
                elif etype == "response.output_item.done":
                    item = data["item"]
                    if item.get("type") == "function_call":
                        yield ev("tool", name=item.get("name"), phase="done", arguments=(item.get("arguments") or "")[:400], t=round(now, 2))
                elif etype in ("response.completed", "response.failed", "response.incomplete"):
                    response = data.get("response", {})
                    s.prev_response_id = response.get("id") or s.prev_response_id
                    usage = response.get("usage")
                    error = response.get("error")
                elif etype == "error":
                    error = data
    turn.update(
        {
            "ttft_s": round(ttft, 2) if ttft is not None else None,
            "total_s": round(time.perf_counter() - t0, 2),
            **usage_numbers(usage),
            "error": error,
        }
    )
    s.turns.append(turn)
    if turn["progress"]:
        s.progress = turn["progress"]
    yield ev("done", **turn)
    s.server = await asyncio.to_thread(server_metrics, s)
    yield ev("server", **s.server)


async def filesearch_turn(s: DemoSession, message: str) -> AsyncIterator[dict[str, str]]:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
    t0 = time.perf_counter()
    turn: dict[str, Any] = {"message": message, "answer": "", "tools": [], "progress": [], "engine": s.engine, "retrieved": []}
    first_delta: list[float] = []

    def put(kind: str, **payload: Any) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, (kind, payload))

    def on_progress(line: str) -> None:
        put("progress", text=line, t=round(time.perf_counter() - t0, 2))

    def on_delta(delta: str) -> None:
        if not first_delta:
            first_delta.append(time.perf_counter() - t0)
        put("delta", text=delta)

    def on_tool(info: dict[str, Any]) -> None:
        put("tool", name=info["type"], phase=info["phase"], retrieved=info.get("retrieved"), t=round(time.perf_counter() - t0, 2))

    def work() -> None:
        try:
            if s.fs.vector_store_id is None:
                c = customers()[s.customer_id]
                on_progress(f"▸ Session authenticated by the backend · customer {s.customer_id} ({c['full_name']})")
                s.fs.open(on_progress=on_progress)
                turn["ingesta_s"] = round(time.perf_counter() - t0, 2)
            put("result", **s.fs.ask(message, on_delta=on_delta, on_tool=on_tool))
        except Exception as exc:  # noqa: BLE001 - shown in the console
            log.exception("File Search turn")
            put("error", message=str(exc))
        finally:
            put("end")

    threading.Thread(target=work, daemon=True).start()
    while True:
        kind, payload = await queue.get()
        if kind == "end":
            break
        if kind == "progress":
            turn["progress"].append(payload)
        elif kind == "delta":
            turn["answer"] += payload["text"]
        elif kind == "tool" and payload["phase"] == "start":
            turn["tools"].append(payload["name"])
        elif kind == "result":
            retrieved = Counter()
            for r in payload.get("retrieved") or []:
                name = r.get("filename") or "?"
                retrieved["-".join(name.split("-")[:2]) if name.startswith("CLI-") else name] += 1
            turn.update(
                {
                    "ttft_s": round(first_delta[0], 2) if first_delta else None,
                    "ttft_ask_s": payload.get("ttft_s"),
                    "total_s": round(time.perf_counter() - t0, 2),
                    "input_tokens": payload.get("input_tokens"),
                    "cached_tokens": payload.get("cached_tokens"),
                    "retrieved": [r.get("filename") for r in payload.get("retrieved") or []],
                    "retrieved_by_customer": dict(retrieved),
                    "error": payload.get("error"),
                }
            )
            continue
        elif kind == "error":
            turn["error"] = payload["message"]
        yield ev(kind, **payload)
    turn.setdefault("total_s", round(time.perf_counter() - t0, 2))
    s.turns.append(turn)
    if turn["progress"]:
        s.progress = turn["progress"]
        s.server = {"filesearch": {"timings_ms": s.fs.timings, "stats": s.fs.stats, "vector_store_id": s.fs.vector_store_id}}
        yield ev("server", **s.server)
    yield ev("done", **turn)


# --------------------------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------------------------


def _warm_up() -> None:
    try:
        credential().get_token("https://ai.azure.com/.default")
        hosted_version()
        log.info("credentials and agent version ready (v%s)", hosted_version())
    except Exception as exc:  # noqa: BLE001
        log.warning("warm-up incomplete: %s", exc)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    threading.Thread(target=_warm_up, daemon=True).start()
    yield
    for s in list(SESSIONS.values()):
        await asyncio.to_thread(close_session, s)


app = FastAPI(title="Runtime customer documentation", lifespan=lifespan)


class NewSession(BaseModel):
    customer_id: str
    engine: str = "hosted"
    route: str | None = None


class Turn(BaseModel):
    message: str


def _get(key: str) -> DemoSession:
    s = SESSIONS.get(key)
    if s is None or s.closed:
        raise HTTPException(404, "Session not found")
    return s


@app.get("/api/customers")
def api_customers() -> list[dict[str, Any]]:
    return [customer_view(c) for c in customers().values()]


@app.get("/api/sessions")
def api_sessions() -> list[dict[str, Any]]:
    return [s.view() for s in SESSIONS.values() if not s.closed]


@app.post("/api/sessions")
async def api_new_session(req: NewSession) -> dict[str, Any]:
    if req.customer_id not in customers():
        raise HTTPException(404, "Unknown customer")
    if req.engine not in ENGINES:
        raise HTTPException(400, f"Invalid engine: {req.engine}")
    route = req.route if req.route in ("cag", "index") and req.engine != "filesearch" else None
    s = DemoSession(key=uuid.uuid4().hex[:10], customer_id=req.customer_id, engine=req.engine, route=route, created_at=time.time())
    t0 = time.perf_counter()
    try:
        if req.engine == "filesearch":
            from filesearch_agent import FileSearchSession

            s.fs = FileSearchSession(req.customer_id)
            s.agent_session_id = s.fs.app_session
        else:
            version = await asyncio.to_thread(hosted_version)
            s.agent_session_id = await asyncio.to_thread(new_session, req.customer_id, AGENT_NAME, version, route)
    except Exception as exc:  # noqa: BLE001
        log.exception("session setup")
        raise HTTPException(502, f"Could not open the session: {exc}") from exc
    s.setup_s = round(time.perf_counter() - t0, 2)
    SESSIONS[s.key] = s
    return s.view()


@app.post("/api/sessions/{key}/turns")
async def api_turn(key: str, req: Turn) -> EventSourceResponse:
    s = _get(key)
    if s.busy:
        raise HTTPException(409, "The session is responding")
    message = req.message.strip()
    if not message:
        raise HTTPException(400, "Empty message")

    async def stream() -> AsyncIterator[dict[str, str]]:
        # The session is released as soon as the turn finishes ("done"), even if the later
        # metrics download is still running; the token prevents that tail from releasing a new turn.
        token = object()
        s.turn_token = token
        s.busy = True
        try:
            inner = filesearch_turn(s, message) if s.engine == "filesearch" else hosted_turn(s, message)
            async for item in inner:
                if item.get("event") == "done" and s.turn_token is token:
                    s.busy = False
                yield item
        except Exception as exc:  # noqa: BLE001
            log.exception("turn")
            yield ev("error", message=str(exc))
        finally:
            if s.turn_token is token:
                s.busy = False

    return EventSourceResponse(stream(), ping=15)


@app.get("/api/sessions/{key}/server")
async def api_server_metrics(key: str) -> dict[str, Any]:
    s = _get(key)
    if s.engine != "filesearch":
        s.server = await asyncio.to_thread(server_metrics, s)
    return s.server


def close_session(s: DemoSession) -> None:
    if s.closed:
        return
    s.closed = True
    try:
        if s.engine == "filesearch" and s.fs is not None:
            s.fs.close()
        elif s.agent_session_id:
            delete_session(s.agent_session_id, AGENT_NAME)
    except Exception as exc:  # noqa: BLE001 - best-effort cleanup
        log.warning("cleanup of %s: %s", s.key, exc)


@app.delete("/api/sessions/{key}")
async def api_close(key: str) -> dict[str, str]:
    s = _get(key)
    await asyncio.to_thread(close_session, s)
    SESSIONS.pop(key, None)
    return {"status": "closed"}


def isolation_findings(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Findings computed from measured rows and the filter experiment."""

    def scenario(prefix: str) -> list[dict[str, Any]]:
        return [r for r in rows if r["scenario"].startswith(prefix)]

    def foreign_chunks(r: dict[str, Any]) -> int:
        return sum(n for cid, n in (r.get("retrieved_by_customer") or {}).items() if cid != r["customer"])

    out: list[dict[str, str]] = []
    if hosted := scenario("H"):
        ok = sum(r["result"] == "OK" for r in hosted)
        out.append(
            {
                "tone": "ok" if ok == len(hosted) else "bad",
                "title": "Option 1 · hosted: no other-customer data",
                "body": f"{ok}/{len(hosted)} checks passed across CAG and index: own-data questions, another-customer questions, and "
                "instruction-injection attempts. Each session prepares documentation in its own sandbox and, on the index route, "
                "the agent code applies the session filter, not the model.",
            }
        )
    if per_session := scenario("F1"):
        ok = sum(r["result"] == "OK" for r in per_session)
        out.append(
            {
                "tone": "ok" if ok == len(per_session) else "bad",
                "title": "Option 2 · one vector store per session: isolated",
                "body": f"{ok}/{len(per_session)} checks passed; File Search retrieved only the customer's own files.",
            }
        )
    if wrong := scenario("F3"):
        n = sum(foreign_chunks(r) for r in wrong)
        out.append(
            {
                "tone": "bad",
                "title": "File Search uses whichever vector store it is given",
                "body": f"With another customer's vector store it retrieved {n} foreign chunks. The model did not show them, but that is not "
                "a control: the backend pins vector_store_id from the authenticated identity and never accepts it from the channel.",
            }
        )
    if shared := scenario("F4") + scenario("F5"):
        mixed = sum(1 for r in shared if foreign_chunks(r))
        body = f"{mixed}/{len(shared)} questions retrieved chunks from the other customer, including with the customer_id filter."
        exp_file = RESULTS / "filter-experiment.json"
        if exp_file.exists():
            exp = json.loads(exp_file.read_text())
            read = exp.get("attributes", {}).get("read", {})
            nulls, total = read.get("null", 0), sum(read.values())
            ignored = any((r.get("retrieved_by_customer") or {}) for r in exp.get("responses_api", {}).get("filter_nonexistent_value", []))
            body += (
                f" Measured cause: the project endpoint accepts per-file attributes but drops them ({nulls}/{total} "
                "read as null; files.update returns 404)"
                + (" and a filter with a nonexistent value still returns chunks: the filter is ignored without an error." if ignored else ".")
                + " Today a shared vector store cannot be partitioned by customer."
            )
        out.append({"tone": "bad", "title": "A shared vector store mixes customers", "body": body})
    return out


@app.get("/api/isolation")
def api_isolation() -> dict[str, Any]:
    files = sorted(RESULTS.glob("isolation-2*.json"))
    if not files:
        return {"file": None, "rows": [], "findings": []}
    rows = json.loads(files[-1].read_text())
    return {"file": files[-1].name, "rows": rows, "findings": isolation_findings(rows)}


@app.get("/api/benchmark")
def api_benchmark() -> list[dict[str, Any]]:
    out = []
    for file in sorted(RESULTS.glob("*.jsonl")):
        for line in file.read_text().splitlines():
            if not line.strip():
                continue
            data = json.loads(line)
            rows = data.get("rows", [])
            questions = [r for r in rows if r.get("qid") != "GREETING"]
            greeting = next((r for r in rows if r.get("qid") == "GREETING"), {})
            out.append(
                {
                    "file": file.name,
                    **data.get("summary", {}),
                    "greeting_ttft_s": greeting.get("ttft_s"),
                    "input_tokens_max": max((r.get("input_tokens") or 0 for r in rows), default=None),
                    "cached_ratio": round(
                        sum(r.get("cached_tokens") or 0 for r in questions) / max(1, sum(r.get("input_tokens") or 0 for r in questions)), 3
                    ),
                    "failed": [
                        {"qid": r["qid"], "type": r["type"], "question": r["question"]} for r in questions if not r.get("ok")
                    ],
                }
            )
    return out


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("azure").setLevel(logging.WARNING)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
