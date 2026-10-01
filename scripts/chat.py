"""Hosted-agent test client over the Responses protocol with SSE streaming.

Examples:
  python scripts/chat.py --customer CLI-0001 "Good morning"
  python scripts/chat.py --session <sid> --prev <response_id> "How much will the root canal cost me?"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

import httpx

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
from common import credential  # noqa: E402

AGENT_NAME = os.environ.get("HOSTED_AGENT_NAME", "docs-runtime-agent")


def endpoint(agent: str) -> tuple[str, dict[str, str]]:
    project = os.environ["FOUNDRY_PROJECT_ENDPOINT"].rstrip("/")
    token = credential().get_token("https://ai.azure.com/.default").token
    url = f"{project}/agents/{agent}/endpoint/protocols/openai/responses?api-version=v1"
    return url, {"Authorization": f"Bearer {token}"}


def stream_turn(message: str, session: str, prev: str | None, agent: str = AGENT_NAME, quiet: bool = False) -> dict[str, Any]:
    url, headers = endpoint(agent)
    body: dict[str, Any] = {"input": message, "stream": True, "agent_session_id": session}
    if prev:
        body["previous_response_id"] = prev
    t0 = time.perf_counter()
    first_token: float | None = None
    first_event: float | None = None
    items: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    result: dict[str, Any] = {"session": session}
    event_type = ""
    with httpx.Client(timeout=httpx.Timeout(600, connect=30)) as client:
        with client.stream("POST", url, json=body, headers=headers) as resp:
            if resp.status_code >= 400:
                resp.read()
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:2000]}")
            for line in resp.iter_lines():
                if line.startswith("event:"):
                    event_type = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                data = json.loads(line[5:].strip())
                etype = data.get("type", event_type)
                if first_event is None:
                    first_event = time.perf_counter() - t0
                if etype == "response.output_item.added":
                    item = data["item"]
                    items[item["id"]] = {"type": item.get("type"), "text": "", "name": item.get("name")}
                    order.append(item["id"])
                    if not quiet and item.get("type") == "function_call":
                        print(f"\n  [tool] {item.get('name')}", flush=True)
                elif etype == "response.output_text.delta":
                    iid = data.get("item_id")
                    if iid not in items:
                        items[iid] = {"type": "message", "text": ""}
                        order.append(iid)
                    delta = data.get("delta", "")
                    is_progress = items[iid]["text"].startswith("▸") or (not items[iid]["text"] and delta.startswith("▸"))
                    if first_token is None and delta and not is_progress:
                        first_token = time.perf_counter() - t0
                    items[iid]["text"] += delta
                    if not quiet:
                        print(delta, end="", flush=True)
                elif etype == "response.output_item.done":
                    item = data["item"]
                    if item.get("type") == "function_call" and not quiet:
                        print(f"  [args] {item.get('arguments', '')[:200]}", flush=True)
                elif etype in ("response.completed", "response.failed", "response.incomplete"):
                    response = data.get("response", {})
                    result["response_id"] = response.get("id")
                    result["status"] = response.get("status")
                    result["usage"] = response.get("usage")
                    result["error"] = response.get("error")
                elif etype == "error":
                    result["error"] = data
    total = time.perf_counter() - t0
    messages = [items[i]["text"] for i in order if items[i]["type"] == "message"]
    result.update(
        {
            "progress": next((m for m in messages if m.startswith("▸")), ""),
            "answer": "\n".join(m for m in messages if not m.startswith("▸")),
            "tools": [items[i]["name"] for i in order if items[i]["type"] == "function_call"],
            "first_event_s": round(first_event or 0, 2),
            "ttft_s": round(first_token, 2) if first_token else None,
            "total_s": round(total, 2),
        }
    )
    return result


def usage_summary(usage: dict[str, Any] | None) -> str:
    if not usage:
        return "no usage data"
    details = usage.get("input_tokens_details") or {}
    return (
        f"input {usage.get('input_tokens')} (cache {details.get('cached_tokens', 0)}) · "
        f"output {usage.get('output_tokens')}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("message")
    parser.add_argument("--session", help="existing agent_session_id")
    parser.add_argument("--customer", help="create a new session for this customer (for example CLI-0001)")
    parser.add_argument("--prev")
    parser.add_argument("--agent", default=AGENT_NAME)
    args = parser.parse_args()
    if not args.session:
        if not args.customer:
            parser.error("provide --session or --customer")
        from sessions import new_session

        args.session = new_session(args.customer, args.agent)
        print(f"[session {args.session} created for {args.customer}]")
    res = stream_turn(args.message, args.session, args.prev, args.agent)
    print("\n" + "-" * 80)
    print(f"response_id={res.get('response_id')} status={res.get('status')} tools={res['tools']}")
    print(f"first event {res['first_event_s']} s · first token {res['ttft_s']} s · total {res['total_s']} s · {usage_summary(res.get('usage'))}")
    if res.get("error"):
        print("ERROR:", json.dumps(res["error"], ensure_ascii=False)[:1500])


if __name__ == "__main__":
    main()
