"""HTML -> PDF with headless Microsoft Edge through Chrome DevTools Protocol (no extra dependencies).

Usage:
    from pdf_renderer import render_many
    asyncio.run(render_many([(html_path, pdf_path), ...], concurrency=6))
"""
from __future__ import annotations

import asyncio
import base64
import itertools
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import aiohttp

EDGE = os.environ.get("EDGE_BIN", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")
PORT = int(os.environ.get("EDGE_CDP_PORT", "9333"))


class _Tab:
    def __init__(self, ws: aiohttp.ClientWebSocketResponse):
        self.ws = ws
        self.ids = itertools.count(1)
        self.pending: dict[int, asyncio.Future] = {}
        self.events: asyncio.Queue = asyncio.Queue()
        self.reader = asyncio.create_task(self._read())

    async def _read(self):
        async for msg in self.ws:
            data = json.loads(msg.data)
            if "id" in data and data["id"] in self.pending:
                self.pending.pop(data["id"]).set_result(data)
            elif "method" in data:
                await self.events.put(data)

    async def call(self, method: str, **params):
        i = next(self.ids)
        fut = asyncio.get_running_loop().create_future()
        self.pending[i] = fut
        await self.ws.send_str(json.dumps({"id": i, "method": method, "params": params}))
        res = await asyncio.wait_for(fut, 120)
        if "error" in res:
            raise RuntimeError(f"{method}: {res['error']}")
        return res.get("result", {})

    async def wait_event(self, name: str, timeout: float = 60):
        end = time.monotonic() + timeout
        while True:
            ev = await asyncio.wait_for(self.events.get(), max(0.1, end - time.monotonic()))
            if ev["method"] == name:
                return ev


async def _open_tab(session: aiohttp.ClientSession) -> _Tab:
    async with session.put(f"http://127.0.0.1:{PORT}/json/new?about:blank") as r:
        info = await r.json()
    ws = await session.ws_connect(info["webSocketDebuggerUrl"], max_msg_size=0)
    tab = _Tab(ws)
    await tab.call("Page.enable")
    return tab


async def _render_one(tab: _Tab, html: Path, pdf: Path):
    while not tab.events.empty():
        tab.events.get_nowait()
    await tab.call("Page.navigate", url=html.resolve().as_uri())
    await tab.wait_event("Page.loadEventFired")
    res = await tab.call(
        "Page.printToPDF",
        printBackground=True,
        preferCSSPageSize=True,
        displayHeaderFooter=False,
        marginTop=0, marginBottom=0, marginLeft=0, marginRight=0,
    )
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(base64.b64decode(res["data"]))


async def render_many(jobs: list[tuple[Path, Path]], concurrency: int = 6) -> None:
    profile = tempfile.mkdtemp(prefix="edge-cdp-")
    proc = subprocess.Popen(
        [EDGE, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
         f"--remote-debugging-port={PORT}", f"--user-data-dir={profile}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        async with aiohttp.ClientSession() as session:
            for _ in range(100):
                try:
                    async with session.get(f"http://127.0.0.1:{PORT}/json/version") as r:
                        if r.status == 200:
                            break
                except aiohttp.ClientError:
                    pass
                await asyncio.sleep(0.2)
            queue: asyncio.Queue = asyncio.Queue()
            for job in jobs:
                queue.put_nowait(job)

            async def worker():
                tab = await _open_tab(session)
                while not queue.empty():
                    html, pdf = queue.get_nowait()
                    await _render_one(tab, Path(html), Path(pdf))
                await tab.ws.close()

            await asyncio.gather(*(worker() for _ in range(max(1, min(concurrency, len(jobs))))))
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    import sys

    asyncio.run(render_many([(Path(sys.argv[1]), Path(sys.argv[2]))]))
    print("ok", sys.argv[2])
