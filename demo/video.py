"""Demo tooling: drive the dashboard headless.

    python demo/video.py check     # deep links and tab layout -> demo/build/shots/
    python demo/video.py slides    # demo/slides.html -> demo/build/slides/<k>.png and slides.pdf
    python demo/video.py record    # dashboard scenes -> demo/build/clips/<scene>.mp4
    python demo/video.py build     # slides + clips + footage + voice -> demo/build/m-trace.mp4 and friends

Runs its own backend on 127.0.0.1 and a free port. The browser may only reach
the backend, the map tiles, Leaflet's CDN and Google Fonts; everything else
(the IBM web chat included) is aborted.
"""
import os
import re
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
        "Guessing a 6 every time is within one grade 56% of the time, because most roads are fair. "
        "But it can't tell a poor road from a good one. On drives it never saw, M-TRACE gets 67%, "
        "and it never called a good road poor. More rated miles to learn from, and calibration "
        "for each type of vehicle, will widen that gap.")),
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
    'accuracy': [],  # holds on the rated-vs-estimated grid the narration describes
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


FOOTAGE = {
    'box': ('The box', 'Film the box on the table while it replays a drive, with the Device view filling in on the laptop.'),
    'chat': ('Road Desk', 'Screen-record one question in the dashboard\'s web chat, from typing it to the full answer.'),
}
CARD = """<!doctype html><meta charset="utf-8">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Overpass:wght@400;800&display=swap">
<style>body{margin:0;width:1920px;height:1080px;background:#24272b;color:#eeede7;font-family:Overpass,sans-serif;
display:flex;flex-direction:column;justify-content:center;padding:0 160px;box-sizing:border-box;gap:28px}
p{margin:0}.k{color:#f47b20;font-size:30px;font-weight:800;letter-spacing:.08em;text-transform:uppercase}
.t{font-size:96px;font-weight:800;line-height:1}.d{font-size:40px;color:#a3a49f;max-width:1400px}
code{font-size:34px;color:#eeede7;background:#33373c;padding:8px 16px;border-radius:6px}</style>
<p class="k">Your footage goes here</p><p class="t">{title}</p><p class="d">{what}</p><p><code>demo/footage/{name}.mp4</code></p>"""
PAD = 0.3  # seconds of quiet before and after each line


def duration(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', str(path)],
                         capture_output=True, text=True, check=True).stdout
    return float(out)


def find(folder, name, exts):
    return next((f for e in exts if (f := Path(folder) / f'{name}{e}').exists()), None)


def tts(text, wav):
    """Offline Windows speech, a placeholder until a real take exists."""
    src = wav.with_suffix('.txt')
    if wav.exists() and src.exists() and src.read_text(encoding='utf-8') == text:
        return
    src.write_text(text, encoding='utf-8')
    ps = (f"Add-Type -AssemblyName System.Speech; $syn = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
          f"$syn.SetOutputToWaveFile('{wav}'); $syn.Speak([IO.File]::ReadAllText('{src}')); $syn.Dispose()")
    subprocess.run(['powershell', '-NoProfile', '-Command', ps], check=True)


def chunks(text, limit=60):
    """Caption-sized pieces: one per sentence, long sentences halved near the middle, at a comma if one is close."""
    def halve(s):
        if len(s) <= limit:
            return [s]
        words = s.split()
        cut = min(range(1, len(words)), key=lambda i: abs(len(' '.join(words[:i])) - len(s) / 2)
                  - (12 if words[i - 1].endswith((',', ':')) else 0))
        return halve(' '.join(words[:cut])) + halve(' '.join(words[cut:]))
    return [c for sent in re.split(r'(?<=[.?])\s+', text) for c in halve(sent)]


def stamp(t):
    return f'{int(t // 3600)}:{int(t % 3600 // 60):02}:{t % 60:05.2f}'


def build(voice='demo/voice', footage='demo/footage'):
    """The video from SCENES: demo/build/m-trace.mp4, m-trace-loop.mp4 (no sound), narration.md, table.html."""
    voice, footage = ROOT / voice, ROOT / footage
    for d in ('seg', 'tts', 'cards'):
        (BUILD / d).mkdir(parents=True, exist_ok=True)
    if not all((BUILD / 'slides' / f"{s['slide']}.png").exists() for s in SCENES if 'slide' in s):
        slides()
    missing = [s['id'] for s in SCENES if 'clip' in s and not (BUILD / 'clips' / f"{s['id']}.mp4").exists()]
    if missing:
        raise SystemExit(f'no clips for {missing}: run python demo/video.py record first')

    events, segs, t, notes = [], [], 0.0, []
    for i, s in enumerate(SCENES):
        take = find(voice, s['id'], ('.wav', '.m4a', '.mp3'))
        if not take:
            take = BUILD / 'tts' / f"{s['id']}.wav"
            tts(s['say'], take)
        # trim the silence a take starts and ends with, and level it
        clean = BUILD / 'seg' / f"{s['id']}-voice.wav"
        trim = 'silenceremove=start_periods=1:start_threshold=-45dB'
        ffmpeg('-i', take, '-af', f'{trim},areverse,{trim},areverse,loudnorm=I=-16:TP=-1.5', '-ar', 48000, '-ac', 2, clean)
        said = duration(clean)
        dur = max(s['secs'], said + 2 * PAD)
        ffmpeg('-i', clean, '-af', f'adelay={int(PAD * 1000)}:all=1,apad', '-t', f'{dur:.3f}', '-ar', 48000, '-ac', 2,
               BUILD / 'seg' / f"{s['id']}.wav")

        out = BUILD / 'seg' / f"{s['id']}.mp4"
        fit = 'scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:-1:-1:black,setsar=1'
        hold = f'tpad=stop_mode=clone:stop_duration={dur:.3f},fps=30,format=yuv420p'
        if 'slide' in s:
            src, vf, what = ['-loop', 1, '-i', BUILD / 'slides' / f"{s['slide']}.png"], f'{fit},fps=30,format=yuv420p', f"slide {s['slide']}"
        elif 'clip' in s:
            src, vf, what = ['-i', BUILD / 'clips' / f"{s['id']}.mp4"], hold, 'recorded clip'
        elif (film := find(footage, s['footage'], ('.mp4', '.mov', '.m4v', '.webm'))):
            src, vf, what = ['-i', film], f'{fit},{hold}', film.name
        else:
            card = BUILD / 'cards' / f"{s['footage']}.png"
            title, desc = FOOTAGE[s['footage']]
            with browser() as ctx:
                page = ctx.new_page()
                page.set_content(CARD.replace('{title}', title).replace('{what}', desc).replace('{name}', s['footage']))
                page.evaluate('document.fonts.ready')
                page.wait_for_load_state('networkidle')
                page.screenshot(path=card)
            src, vf, what = ['-loop', 1, '-i', card], f'{fit},fps=30,format=yuv420p', 'placeholder card'
        ffmpeg(*src, '-vf', vf, '-t', f'{dur:.3f}', '-an', '-c:v', 'libx264', '-crf', 18, '-r', 30, out)
        segs.append(s['id'])

        # captions, each piece on screen for its share of the spoken line
        style = 'Map' if 'clip' in s else 'Full'
        pieces, at = chunks(s['say']), t + PAD
        total = sum(len(c) for c in pieces)
        for c in pieces:
            end = at + said * len(c) / total
            events.append(f'Dialogue: 0,{stamp(at)},{stamp(end)},{style},,0,0,0,,{c}')
            at = end
        notes.append((i + 1, s, what, dur, take))
        print(f"{s['id']}: {dur:.1f} s, voice {'take' if take.parent == voice else 'TTS'} {said:.1f} s, {what}")
        t += dur

    (BUILD / 'seg' / 'video.txt').write_text(''.join(f"file '{n}.mp4'\n" for n in segs))
    (BUILD / 'seg' / 'audio.txt').write_text(''.join(f"file '{n}.wav'\n" for n in segs))
    # map scenes caption over the map, clear of the sidebar and the legend; slides along the very bottom,
    # under their source lines
    (BUILD / 'captions.ass').write_text("""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Full,Segoe UI Semibold,46,&H00FFFFFF,&H00FFFFFF,&H00000000,&H40000000,0,0,0,0,100,100,0,0,3,10,0,2,200,200,10,1
Style: Map,Segoe UI Semibold,46,&H00FFFFFF,&H00FFFFFF,&H00000000,&H40000000,0,0,0,0,100,100,0,0,3,14,0,2,1320,50,56,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
""" + '\n'.join(events) + '\n', encoding='utf-8')
    ffmpeg('-f', 'concat', '-i', 'video.txt', '-c', 'copy', 'video.mp4', cwd=BUILD / 'seg')
    ffmpeg('-f', 'concat', '-i', 'audio.txt', '-c', 'copy', 'audio.wav', cwd=BUILD / 'seg')
    # run from the build folder: a drive letter's colon breaks the subtitles filter's path
    ffmpeg('-i', 'seg/video.mp4', '-i', 'seg/audio.wav', '-vf', 'subtitles=captions.ass', '-c:v', 'libx264', '-crf', 20,
           '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '160k', '-shortest', '-movflags', '+faststart', 'm-trace.mp4', cwd=BUILD)
    ffmpeg('-i', 'm-trace.mp4', '-an', '-c:v', 'copy', 'm-trace-loop.mp4', cwd=BUILD)
    (BUILD / 'table.html').write_text("""<!doctype html><meta charset="utf-8"><title>M-TRACE</title>
<style>html,body{margin:0;height:100%;background:#000;overflow:hidden}video{width:100%;height:100%;object-fit:contain}</style>
<video src="m-trace-loop.mp4" autoplay muted loop playsinline></video>
<!-- press F11 for full screen -->
""")
    lines = ['# Narration', '',
             'Read each line in its own take and save it as `demo/voice/<scene>.wav` (or .m4a, .mp3). '
             'Silence at the start and end is trimmed. A scene stretches to fit a longer take. '
             f'Then run `python demo/video.py build`. Total now: {t:.0f} s.', '']
    for n, s, what, dur, take in notes:
        lines += [f"## {n}. {s['id']}: {what}", '',
                  f"Save as `demo/voice/{s['id']}.wav`. Now {dur:.1f} s ({'your take' if take.parent == voice else 'synthetic voice'}).",
                  '', f"> {s['say']}", '']
    (BUILD / 'narration.md').write_text('\n'.join(lines), encoding='utf-8')
    print(f'{t:.1f} s -> {BUILD / "m-trace.mp4"}, m-trace-loop.mp4, narration.md, table.html')


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('cmd', choices=['check', 'slides', 'record', 'build'])
    ap.add_argument('--voice', default='demo/voice', help='folder of takes, <scene>.wav/.m4a/.mp3 (build)')
    ap.add_argument('--footage', default='demo/footage', help='folder of box.mp4 and chat.mp4 (build)')
    a = ap.parse_args()
    if a.cmd == 'build':
        build(a.voice, a.footage)
    else:
        {'check': check, 'slides': slides, 'record': record}[a.cmd]()
