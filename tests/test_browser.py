"""The real page in headless Chrome: it draws, updates live, reacts to keys and
controls, fits a phone, logs no errors, and shows hostile strings as plain text.

Skipped when Chrome is not installed (set HTOP_WEB_CHROME to point at one).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from aiohttp import web
from chrome import Browser, find_chrome
from conftest import free_port, proc_row, raw_snapshot, start_server, stop

from htop_web.app import HUB, Options, create_app
from htop_web.model import HostSpec, normalize_snapshot

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="needs Chrome or Chromium")

PROBE = """(() => {
  const cards = [...document.querySelectorAll('section.host')];
  const lines = (c) => [...c.querySelectorAll('.mline')].map((l) => l.textContent);
  return {
    cards: cards.length,
    names: cards.map((c) => c.querySelector('.name').textContent),
    status: cards.map((c) => c.querySelector('.status').textContent),
    meters: cards.map((c) => lines(c).length),
    rows: cards.map((c) => c.querySelectorAll('tbody tr').length),
    text: cards.map((c) => lines(c).join('\\n') + '\\n' + c.querySelector('tbody').textContent),
    sorted: (document.querySelector('th.sorted') || {}).textContent || '',
    foot: cards.map((c) => c.querySelector('.host-foot').textContent),
    conn: document.getElementById('conn').textContent,
    pageWidth: document.documentElement.scrollWidth,
    viewWidth: document.documentElement.clientWidth,
  };
})()"""

READY = """(() => {
  const cards = [...document.querySelectorAll('section.host')];
  return cards.length > 0 && cards.every((c) => c.querySelectorAll('.mline').length > 0
    && c.querySelectorAll('tbody tr').length > 0);
})()"""


def press(key: str) -> str:
    return f"document.dispatchEvent(new KeyboardEvent('keydown', {{key: {key!r}}}))"


@pytest.mark.parametrize("viewport", ["desktop", "mobile"])
async def test_demo_page(viewport, tmp_path):
    server, base = start_server("--demo", "--interval", "0.5")
    try:
        async with Browser() as browser:
            page = await browser.open(base + "/", viewport)
            await page.wait_for(READY)
            first = await page.eval(PROBE)
            assert first["names"] == ["gpu-node-01", "web-01", "db-01", "build-01"]
            assert first["status"] == ["live"] * 4 and first["conn"] == "live"
            assert first["rows"] == [15] * 4
            assert first["sorted"] == "CPU%▽"
            assert "Mem[" in first["text"][0] and "Tasks:" in first["text"][0]
            assert first["pageWidth"] <= first["viewWidth"], "the page scrolls sideways"

            await asyncio.sleep(1.5)
            second = await page.eval(PROBE)
            assert second["text"] != first["text"], "nothing changed after updates"

            await page.eval(press("M"))
            await page.wait_for("(document.querySelector('th.sorted') || {}).textContent"
                                " === 'MEM%▽'")
            await page.eval(press("I"))
            await page.wait_for("(document.querySelector('th.sorted') || {}).textContent"
                                " === 'MEM%△'")
            await page.eval("""(() => { const f = document.getElementById('filter');
                f.value = 'postgres'; f.dispatchEvent(new Event('input')); })()""")
            await page.wait_for("[...document.querySelectorAll('.host-foot')]"
                                ".every((f) => f.textContent.includes('matching'))")
            filtered = await page.eval(PROBE)
            assert filtered["rows"][2] > 0 and filtered["rows"][0] == 0
            await page.eval(press("Escape"))
            await page.wait_for("document.querySelectorAll('section.host')[0]"
                                ".querySelectorAll('tbody tr').length > 0")

            await page.screenshot(tmp_path / f"demo-{viewport}.png")
            assert page.problems() == []
            await page.close()
    finally:
        assert stop(server) == 0


async def test_this_machine(tmp_path):
    server, base = start_server("--interval", "0.5")
    try:
        async with Browser() as browser:
            page = await browser.open(base + "/", "desktop")
            await page.wait_for(READY)
            first = await page.eval(PROBE)
            assert first["cards"] == 1 and first["status"] == ["live"]
            assert first["names"][0] == Path("/proc/sys/kernel/hostname").read_text().strip()
            await asyncio.sleep(1.5)
            assert (await page.eval(PROBE))["text"] != first["text"]
            await page.screenshot(tmp_path / "local.png")
            assert page.problems() == []
            await page.close()
    finally:
        assert stop(server) == 0


async def test_hostile_strings_render_as_text(tmp_path):
    evil = "<img src=x onerror=alert(1)><script>alert(2)</script>\x1b[31m"
    raw = raw_snapshot(host="<b>pwn</b>", procs=[proc_row(1, user="<i>u</i>", cmd=evil, cpu=50),
                                                proc_row(2, cmd="normal")])
    app = create_app([HostSpec(key="<svg onload=alert(3)>", kind="ssh", host="x")], Options(),
                     worker_factory=lambda state: None)
    app[HUB].hosts[0].update(normalize_snapshot(raw))
    runner = web.AppRunner(app)
    await runner.setup()
    port = free_port()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    try:
        async with Browser() as browser:
            page = await browser.open(f"http://127.0.0.1:{port}/", "desktop")
            await page.wait_for(READY)
            found = await page.eval("""(() => ({
              injected: document.querySelectorAll('main img, main script, main b, main i,'
                + ' main svg').length,
              name: document.querySelector('.name').textContent,
              cmd: document.querySelector('td.cmd').textContent,
              user: document.querySelector('td.k-user').textContent,
            }))()""")
            assert found["injected"] == 0
            assert found["name"] == "<b>pwn</b>"
            assert found["cmd"] == "<img src=x onerror=alert(1)><script>alert(2)</script>?[31m"
            assert found["user"] == "<i>u</i>"
            await asyncio.sleep(0.5)
            assert page.problems() == []  # no alert dialogs, no errors
            await page.close()
    finally:
        await runner.cleanup()


FIT_PROBE = """(() => {
  const keys = document.getElementById('fkeys').getBoundingClientRect().top;
  const cards = [...document.querySelectorAll('section.host')].map((c) => {
    const box = c.getBoundingClientRect();
    const rows = [...c.querySelectorAll('tbody tr')];
    const last = rows.length ? rows[rows.length - 1].getBoundingClientRect().bottom : box.top;
    return {top: Math.round(box.top), bottom: box.bottom, rows: rows.length, lastRow: last};
  });
  return {
    fit: document.body.classList.contains('fit'),
    pageScrolls: document.documentElement.scrollHeight > window.innerHeight + 1,
    keysTop: keys, cards,
    tops: [...new Set(cards.map((c) => c.top))].length,
    font: getComputedStyle(document.querySelector('section.host')).fontSize,
  };
})()"""


def fits(probe: dict) -> bool:
    return (probe["fit"] and not probe["pageScrolls"]
            and all(c["bottom"] <= probe["keysTop"] + 1 for c in probe["cards"])
            and all(c["rows"] >= 1 and c["lastRow"] <= c["bottom"] + 1 for c in probe["cards"]))


async def test_fit_mode_and_columns(tmp_path):
    server, base = start_server("--demo", "--interval", "0.5")
    try:
        async with Browser() as browser:
            page = await browser.open(base + "/?fit=1", "desktop")
            await page.wait_for(READY)
            await asyncio.sleep(1.0)  # let the row count settle with the server
            probe = await page.eval(FIT_PROBE)
            assert fits(probe), probe
            assert probe["tops"] == 2  # four hosts in a 2 x 2 grid on a 1440 x 900 screen
            assert all(c["rows"] >= 3 for c in probe["cards"]), probe
            await page.screenshot(tmp_path / "fit-auto.png")

            await page.eval(press("C"))  # 1 column: four hosts stacked, still one screen
            await asyncio.sleep(1.0)
            probe = await page.eval(FIT_PROBE)
            assert fits(probe) and probe["tops"] == 4, probe

            await page.eval("""(() => { const s = document.getElementById('cols');
                s.value = '4'; s.dispatchEvent(new Event('change')); })()""")
            await asyncio.sleep(1.0)
            probe = await page.eval(FIT_PROBE)
            assert fits(probe) and probe["tops"] == 1, probe  # all four side by side
            await page.screenshot(tmp_path / "fit-row.png")

            await page.eval(press("F"))  # Fit off: the page scrolls again, 4 columns stay
            await page.wait_for("!document.body.classList.contains('fit')")
            await asyncio.sleep(0.5)
            probe = await page.eval(FIT_PROBE)
            assert probe["tops"] == 1 and probe["font"] == "13px"
            assert all(c["rows"] == 15 for c in probe["cards"]), probe
            assert page.problems() == []
            await page.close()
    finally:
        assert stop(server) == 0
