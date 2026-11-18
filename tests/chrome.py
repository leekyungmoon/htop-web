"""Drive headless Chrome over the DevTools protocol, using only aiohttp.

The browser tests use this; it also takes the README screenshot:

    htop-web --demo &
    python tests/chrome.py http://127.0.0.1:48110/ screenshot.png
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import aiohttp

VIEWPORTS = {
    "desktop": {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False},
    "mobile": {"width": 390, "height": 844, "deviceScaleFactor": 2, "mobile": True},
}


def find_chrome() -> str | None:
    if os.environ.get("HTOP_WEB_CHROME"):
        return os.environ["HTOP_WEB_CHROME"]
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        path = shutil.which(name)
        if path:
            return path
    return None


class Page:
    """One tab: evaluate JavaScript, wait for conditions, take screenshots."""

    def __init__(self, browser: Browser, target: str, session: str):
        self.browser = browser
        self.target = target
        self.session = session

    async def send(self, method: str, params: dict | None = None) -> dict:
        return await self.browser.send(method, params, self.session)

    async def eval(self, expression: str):
        result = await self.send("Runtime.evaluate", {"expression": expression,
                                                      "returnByValue": True,
                                                      "awaitPromise": True})
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"])
        return result.get("result", {}).get("value")

    async def wait_for(self, expression: str, timeout: float = 20.0):
        deadline = time.monotonic() + timeout
        while True:
            value = await self.eval(expression)
            if value:
                return value
            if time.monotonic() > deadline:
                raise TimeoutError(f"timed out waiting for {expression[:80]!r}")
            await asyncio.sleep(0.2)

    async def screenshot(self, path: str | Path, full: bool = False) -> None:
        shot = await self.send("Page.captureScreenshot",
                               {"format": "png", "captureBeyondViewport": full})
        Path(path).write_bytes(base64.b64decode(shot["data"]))

    def problems(self) -> list:
        """Console errors and warnings, uncaught exceptions, failed loads, dialogs."""
        found = []
        for event in self.browser.events:
            if event.get("sessionId") != self.session:
                continue
            method, params = event.get("method"), event.get("params", {})
            if method == "Runtime.exceptionThrown":
                found.append(("exception", params["exceptionDetails"].get("text")))
            elif method == "Runtime.consoleAPICalled" and params.get("type") in ("error", "warning",
                                                                                  "assert"):
                found.append(("console", [a.get("value") for a in params.get("args", [])]))
            elif method == "Log.entryAdded" and params["entry"].get("level") in ("error", "warning"):
                found.append(("log", params["entry"].get("text")))
            elif method == "Page.javascriptDialogOpening":
                found.append(("dialog", params.get("message")))
        return found

    async def close(self) -> None:
        await self.browser.send("Target.closeTarget", {"targetId": self.target})


class Browser:
    """A headless Chrome with its own throwaway profile, stopped by PID on exit."""

    def __init__(self, executable: str | None = None):
        self.executable = executable or find_chrome()
        self.events: list[dict] = []
        self._pending: dict[int, asyncio.Future] = {}
        self._next = 0

    async def __aenter__(self) -> Browser:
        if not self.executable:
            raise RuntimeError("Chrome not found; set HTOP_WEB_CHROME")
        self.profile = tempfile.mkdtemp(prefix="htop-web-chrome-")
        args = [self.executable, "--headless=new", "--remote-debugging-port=0",
                f"--user-data-dir={self.profile}", "--no-first-run",
                "--no-default-browser-check", "--disable-gpu", "--disable-extensions",
                "--disable-background-networking", "--disable-sync", "--mute-audio"]
        if os.environ.get("CI") or os.geteuid() == 0:
            args.append("--no-sandbox")  # CI runners often cannot use Chrome's sandbox
        self.process = subprocess.Popen(args + ["about:blank"], stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
        port_file = Path(self.profile) / "DevToolsActivePort"
        deadline = time.monotonic() + 30
        while not port_file.exists() or len(port_file.read_text().split()) < 2:
            if time.monotonic() > deadline or self.process.poll() is not None:
                await self._stop()
                raise RuntimeError("Chrome did not start")
            await asyncio.sleep(0.1)
        port, path = port_file.read_text().split()[:2]
        self.http = aiohttp.ClientSession()
        self.ws = await self.http.ws_connect(f"ws://127.0.0.1:{port}{path}", max_msg_size=0)
        self.reader = asyncio.create_task(self._read())
        return self

    async def __aexit__(self, *exc) -> None:
        try:
            await asyncio.wait_for(self.send("Browser.close"), timeout=5)
        except Exception:
            pass
        self.reader.cancel()
        await self.ws.close()
        await self.http.close()
        await self._stop()

    async def _stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        shutil.rmtree(self.profile, ignore_errors=True)

    async def _read(self) -> None:
        async for msg in self.ws:
            data = json.loads(msg.data)
            future = self._pending.pop(data.get("id"), None)
            if future is not None:
                if not future.done():
                    future.set_result(data)
            else:
                self.events.append(data)

    async def send(self, method: str, params: dict | None = None,
                   session: str | None = None) -> dict:
        self._next += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[self._next] = future
        message = {"id": self._next, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        await self.ws.send_str(json.dumps(message))
        reply = await asyncio.wait_for(future, timeout=30)
        if "error" in reply:
            raise RuntimeError(f"{method}: {reply['error']}")
        return reply.get("result", {})

    async def open(self, url: str, viewport: str = "desktop") -> Page:
        target = (await self.send("Target.createTarget", {"url": "about:blank"}))["targetId"]
        session = (await self.send("Target.attachToTarget",
                                   {"targetId": target, "flatten": True}))["sessionId"]
        page = Page(self, target, session)
        for domain in ("Runtime", "Log", "Page"):
            await page.send(f"{domain}.enable")
        await page.send("Emulation.setDeviceMetricsOverride", VIEWPORTS[viewport])
        await page.send("Page.navigate", {"url": url})
        return page


async def _screenshot(url: str, path: str, viewport: str, wait: float) -> None:
    async with Browser() as browser:
        page = await browser.open(url, viewport)
        await page.wait_for("document.querySelectorAll('tbody tr').length > 0")
        await asyncio.sleep(wait)
        await page.screenshot(path)
        await page.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Screenshot an htop-web page.")
    parser.add_argument("url")
    parser.add_argument("path")
    parser.add_argument("--viewport", choices=sorted(VIEWPORTS), default="desktop")
    parser.add_argument("--wait", type=float, default=3.0, help="seconds to let it update")
    args = parser.parse_args()
    asyncio.run(_screenshot(args.url, args.path, args.viewport, args.wait))
