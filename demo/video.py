"""Demo tooling: drive the dashboard headless.

    python demo/video.py check     # deep links and tab layout -> demo/build/shots/
    python demo/video.py slides    # demo/slides.html -> demo/build/slides/<k>.png and slides.pdf
    python demo/video.py record    # dashboard scenes -> demo/build/clips/<scene>.mp4

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

# The video, scene by scene. A scene shows a slide (its number in slides.html), a clip recorded from the
# dashboard, or footage you film (demo/footage/<name>.mp4). `secs` is the least it lasts; the voice can stretch it.
DRIVE = 'drive_20261003_170553'  # 23 miles, 118 hits
SCENES = [
    dict(id='hook', slide=2, secs=15, say=(
        "Michigan law says a road agency is presumed to know about a defect once it's been readily apparent "
        "for 30 days. But most local roads are never rated, and potholes mostly get reported by residents. "
        "Fleet vehicles already drive every road, every week. M-TRACE makes those trips measure the road.")),
    dict(id='box', footage='box', secs=15, say=(
        "This is the whole sensor: an ESP32, a motion sensor, GPS and an SD card, about $100 in parts. "
        "It finds roughness and pothole hits on the board, keeps them on the card, and uploads whenever it has WiFi.")),
    dict(id='replay', clip=f'#drives/{DRIVE}', secs=14, say=(
        "Here's a real drive, replayed. Every 50 meters gets a roughness score and an estimated PASER grade, "
        "matched to the road segment it was on. The white dots are severe hits.")),
    dict(id='repairs', clip='#repairs', secs=20, say=(
        "Over 179 miles and 11 drives, a hit only counts as a pothole once it shows up on two or more passes: "
        "158 so far, hardest first. Each one has a 30-day clock from the first hit, "
        "and one click gives the crew a work order.")),
    dict(id='timelines', clip='#repairs', secs=15, say=(
        "One week can't show a repair, so these are real potholes with simulated passes afterward, "
        "run through the same rules. One gets patched, and the patch fails two weeks later. "
        "Another passes the 30-day mark untouched.")),
    dict(id='forecast', clip='#forecast', secs=18, say=(
        "Potholes are the symptom. Sealing a fair road before it turns poor costs far less than rebuilding it. "
        "Tested on SEMCOG's own rating history, the forecast's riskiest tenth of fair and good miles "
        "were 51% poor four years later, against 21% overall.")),
    dict(id='desk', footage='chat', secs=15, say=(
        "Road Desk runs on watsonx Orchestrate. It answers only through tools that call M-TRACE's API "
        "on IBM Code Engine, so every number it says comes from the data, not the model.")),
    dict(id='accuracy', clip='#accuracy', secs=18, say=(
        "On drives it never saw, the estimated grade lands within one grade of SEMCOG's rating 67% of the time, "
        "against 56% for always guessing a 6. IBM Granite TSPulse and a 15-feature model didn't beat "
        "the simple line, and I'd rather show that than hide it.")),
    dict(id='michigan', slide=9, secs=14, say=(
        "Michigan's 83 county road agencies maintain over 90,000 miles of road. The boxes ride on trucks "
        "they already run, get assembled in Michigan, and are calibrated by Michigan's trained PASER raters.")),
    dict(id='close', slide=1, secs=5, say="M-TRACE: every fleet vehicle becomes a road inspector."),
]
SLACK = 8  # seconds recorded past `secs`, so a slower voice still has picture under it


def work_order(page):
    # the work order replaces this page, so the shot stays continuous
    # (something also calls window.open(null) here; only the work order's blob URL counts)
    page.evaluate("""window.open = u => String(u).startsWith('blob:') && fetch(u).then(r => r.text()).then(t => {
      document.open(); document.write(t); document.close(); })""")
    page.click('.leaflet-popup-content a', no_wait_after=True)
    page.wait_for_selector('text=Work order WO-175')


# What happens in each recorded scene: (seconds after the view settles, action)
ACTIONS = {
    'replay': [(1, lambda p: p.click('#play'))],
    'repairs': [(4, lambda p: p.click('#r-now-body .row[data-id="175"]')), (15, work_order)],
    'timelines': [(1.5, lambda p: p.click('#r-next')), (6.5, lambda p: p.click('#r-next-body .row[data-id="175"]'))],
    'forecast': [(5, lambda p: p.click('#forecast .row'))],
    'accuracy': [(7, lambda p: p.evaluate("document.getElementById('accuracy').scrollTo({ top: 420, behavior: 'smooth' })"))],
}


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


def guard(c):
    c.route('**/*', lambda r: r.continue_() if allowed(r.request.url) else r.abort())
    return c


@contextmanager
def browser(**ctx):
    with sync_playwright() as p:
        b = p.chromium.launch()
        c = guard(b.new_context(viewport=HD, **ctx))
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


def ffmpeg(*args, cwd=None):
    subprocess.run(['ffmpeg', '-v', 'error', '-y', *map(str, args)], check=True, cwd=cwd)


def record():
    """Each dashboard scene to demo/build/clips/<id>.mp4, from the moment its view has settled.

    The dashboard runs at 150% zoom (1280x720 at device scale 1.5) so its text reads on a video. Playwright's
    own recorder captures CSS pixels only, so frames are screenshots, about 10 a second, timed as taken.
    """
    out = BUILD / 'clips'
    out.mkdir(parents=True, exist_ok=True)
    with backend() as url, sync_playwright() as p:
        b = p.chromium.launch()
        for s in SCENES:
            if 'clip' not in s:
                continue
            ctx = guard(b.new_context(viewport={'width': 1280, 'height': 720}, device_scale_factor=1.5))
            page = ctx.new_page()
            open_at(page, url + s['clip'])
            todo, frames = list(ACTIONS[s['id']]), []
            t0 = time.monotonic()
            while (t := time.monotonic() - t0) < s['secs'] + SLACK:
                frames.append((t, page.screenshot(type='jpeg', quality=92)))
                while todo and todo[0][0] <= t:
                    todo.pop(0)[1](page)
            ctx.close()
            raw = BUILD / 'raw' / s['id']
            raw.mkdir(parents=True, exist_ok=True)
            lines = []
            for i, (t, jpg) in enumerate(frames):
                (raw / f'{i:05d}.jpg').write_bytes(jpg)
                end = frames[i + 1][0] if i + 1 < len(frames) else s['secs'] + SLACK
                lines += [f"file '{i:05d}.jpg'", f'duration {end - t:.4f}']
            lines.append(f"file '{len(frames) - 1:05d}.jpg'")  # the concat demuxer drops the last duration otherwise
            (raw / 'frames.txt').write_text('\n'.join(lines) + '\n')
            ffmpeg('-f', 'concat', '-i', 'frames.txt', '-vf', 'fps=30,format=yuv420p', '-c:v', 'libx264', '-crf', 18,
                   (out / f"{s['id']}.mp4").resolve(), cwd=raw)
            print(f"{s['id']}: {len(frames)} frames over {s['secs'] + SLACK} s")
        b.close()


if __name__ == '__main__':
    cmds = {'check': check, 'slides': slides, 'record': record}
    if len(sys.argv) != 2 or sys.argv[1] not in cmds:
        raise SystemExit(__doc__)
    cmds[sys.argv[1]]()
