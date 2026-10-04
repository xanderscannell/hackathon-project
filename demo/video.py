"""Demo tooling: drive the dashboard headless.

    python demo/video.py check     # deep links and tab layout -> demo/build/shots/
    python demo/video.py slides    # demo/slides.html -> demo/build/slides/<k>.png and slides.pdf

Runs its own backend on 127.0.0.1 and a free port. The browser may only reach
the backend, the map tiles, Leaflet's CDN and Google Fonts; everything else
(the IBM web chat included) is aborted.
"""
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / 'demo' / 'build'
ALLOW = {'127.0.0.1', 'tile.openstreetmap.org', 'cdnjs.cloudflare.com', 'fonts.googleapis.com', 'fonts.gstatic.com'}
VIEWS = ['repairs', 'drives', 'device', 'forecast', 'brief', 'accuracy']
HD = {'width': 1920, 'height': 1080}


@contextmanager
def backend():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    env = {**os.environ, 'HOST': '127.0.0.1', 'PORT': str(port)}
    proc = subprocess.Popen([sys.executable, 'backend.py'], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                socket.create_connection(('127.0.0.1', port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise SystemExit('backend did not start')
        yield f'http://127.0.0.1:{port}/'
    finally:
        proc.terminate()
        proc.wait()


def allowed(url):
    u = urlparse(url)
    return u.scheme in ('file', 'data', 'blob') or u.hostname in ALLOW


@contextmanager
def browser(**ctx):
    with sync_playwright() as p:
        b = p.chromium.launch()
        c = b.new_context(viewport=HD, **ctx)
        c.route('**/*', lambda r: r.continue_() if allowed(r.request.url) else r.abort())
        try:
            yield c
        finally:
            c.close()
            b.close()


def open_at(page, url):
    """Load the dashboard fresh at a URL and wait for its data, fonts and map tiles."""
    page.goto('about:blank')
    page.goto(url)
    page.wait_for_function("document.getElementById('asof').textContent.includes('drives')")
    settle(page)


def settle(page, timeout=15000):
    page.evaluate('document.fonts.ready')
    try:
        page.wait_for_function("![...document.querySelectorAll('.leaflet-tile')].some(t => !t.classList.contains('leaflet-tile-loaded'))",
                               timeout=timeout)
    except Exception:
        print('  (map tiles still loading)')
    page.wait_for_timeout(300)


def check():
    fails = []

    def expect(ok, what):
        print(('ok   ' if ok else 'FAIL ') + what)
        if not ok:
            fails.append(what)

    shots = BUILD / 'shots'
    shots.mkdir(parents=True, exist_ok=True)
    tabs_fit = """() => {
      const side = document.getElementById('side').getBoundingClientRect(), nav = document.querySelector('nav');
      return nav.scrollWidth <= nav.clientWidth &&
        [...nav.querySelectorAll('button:not([hidden])')].every(b => b.getBoundingClientRect().right <= side.right);
    }"""
    with backend() as url, browser() as ctx:
        page = ctx.new_page()
        for v in VIEWS:
            open_at(page, f'{url}#{v}')
            expect(page.is_visible(f'#{v}') and page.get_attribute(f'#t-{v}', 'aria-selected') == 'true', f'#{v} opens its view')
            page.screenshot(path=shots / f'{v}.png')

        drive = 'drive_20261003_165628'
        open_at(page, f'{url}#drives/{drive}')
        expect(page.eval_on_selector('#drive', 'e => e.value') == drive, f'#drives/{drive} selects the drive')

        open_at(page, f'{url}#drives/{drive}/play')
        miles = lambda: float(page.inner_text('#stats dd'))  # noqa: E731
        page.wait_for_timeout(1000)
        a = miles()
        page.wait_for_timeout(3000)
        b = miles()
        expect(b > a, f'#drives/{drive}/play replays ({a} -> {b} miles)')
        page.screenshot(path=shots / 'drives-play.png')

        open_at(page, f'{url}#repairs/175')
        expect('Wayne' in (page.text_content('.leaflet-popup-content') or ''), '#repairs/175 opens the Wayne Rd popup')
        page.screenshot(path=shots / 'repairs-175.png')

        open_at(page, f'{url}#nonsense')
        expect(page.is_visible('#repairs'), '#nonsense falls back to Repairs')

        page.click('#t-forecast')
        page.wait_for_timeout(300)
        expect(page.evaluate('location.hash') == '#forecast' and page.is_visible('#forecast'), 'clicking Forecast sets #forecast')
        page.go_back()
        page.wait_for_timeout(300)
        expect(page.is_visible('#repairs'), 'back returns to Repairs')

        for w, h in [(1920, 1080), (1366, 768), (390, 844)]:
            page.set_viewport_size({'width': w, 'height': h})
            open_at(page, f'{url}#accuracy')
            expect(page.evaluate(tabs_fit), f'all tabs fit the sidebar at {w}x{h}')
            page.screenshot(path=shots / f'tabs-{w}x{h}.png')

    print(f'{len(fails)} failed' if fails else 'check ok')
    sys.exit(1 if fails else 0)


def slides():
    """Each slide to demo/build/slides/<k>.png, the deck to demo/build/slides.pdf."""
    out = BUILD / 'slides'
    out.mkdir(parents=True, exist_ok=True)
    deck = (ROOT / 'demo' / 'slides.html').as_uri()
    fails = []
    with browser() as ctx:
        page = ctx.new_page()
        page.goto(deck)
        n = page.locator('.slide').count()
        for k in range(1, n + 1):
            page.goto('about:blank')
            page.goto(f'{deck}?n={k}')
            page.evaluate('document.fonts.ready')
            page.wait_for_load_state('networkidle')
            over = page.evaluate("""() => { const s = document.querySelector('.slide.on');
              return s.scrollHeight > s.clientHeight || s.scrollWidth > s.clientWidth; }""")
            if over:
                fails.append(k)
            page.screenshot(path=out / f'{k}.png')
        page.goto(deck)
        page.evaluate('document.fonts.ready')
        page.wait_for_load_state('networkidle')
        page.pdf(path=BUILD / 'slides.pdf', width='1920px', height='1080px', print_background=True)
    print(f'{n} slides -> {out}, {BUILD / "slides.pdf"}')
    if fails:
        raise SystemExit(f'content overflows on slide {fails}')


if __name__ == '__main__':
    cmds = {'check': check, 'slides': slides}
    if len(sys.argv) != 2 or sys.argv[1] not in cmds:
        raise SystemExit(__doc__)
    cmds[sys.argv[1]]()
