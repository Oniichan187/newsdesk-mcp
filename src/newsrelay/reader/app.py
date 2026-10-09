"""Public reader: daily briefings as speed reading (RSVP), spoken audio and the PDF.

Read-only: every request opens the database in read-only mode; files are served only from the
briefing directory and only under strict name patterns. The service listens on localhost; it is
published through Cloudflare (or inside the tailnet), never together with the MCP backend.
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
from datetime import date, timedelta
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from starlette.routing import Route

from ..audio.build import audio_dir, load_timing
from ..briefing.pdf import MONTHS, long_date
from ..briefing.store import day_pdfs, day_stories, days
from ..briefing.text import reading_words
from ..config import Config
from ..database import connect

_PDF_NAME = re.compile(r"^briefing-\d{4}-\d{2}-\d{2}-[a-z0-9-]{1,40}\.pdf$")
_MP3_NAME = re.compile(r"^(day|story-\d{2})\.mp3$")
PDFJS = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174"
PDFJS_SRI = "sha512-q+4liFwdPC/bNdhUpZx6aXDx/h77yEQtn4I1slHydcbZK34nLaR3cAeYSJshoxIOq3mjEf7xJE8YWIUHMn+oCQ=="
PDFJS_WORKER_SRI = (
    "sha512-BbrZ76UNZq5BhH7LL7pn9A4TKQpQeNCHOo65/akfelcIBbcVvYWOFQKPXIrykE3qZxYjmDX573oa4Ywsc7rpTw=="
)
_GOOGLE = "https://translate.google.com https://translate.googleapis.com https://translate-pa.googleapis.com"
HEADERS = {
    # Google Website Translator (widget) and pdf.js (cdnjs, SRI-pinned) are the only third parties.
    "Content-Security-Policy": (
        "default-src 'none'; "
        f"script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com https://www.gstatic.com {_GOOGLE}; "
        "style-src 'unsafe-inline' https://www.gstatic.com https://translate.googleapis.com; "
        f"img-src 'self' data: https://www.gstatic.com https://www.google.com {_GOOGLE}; "
        f"connect-src 'self' https://cdnjs.cloudflare.com {_GOOGLE}; "
        "font-src https://fonts.gstatic.com; "
        "frame-src https://translate.google.com https://translate.googleapis.com; "
        "worker-src blob:; media-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
FILE_HEADERS = {"X-Content-Type-Options": "nosniff", "Cache-Control": "public, max-age=3600"}


def _day(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _title(d: date) -> str:
    weekday, long = long_date(d)
    return f"{weekday}, {long}"


def _count(n: int) -> str:
    return f"{n} {'story' if n == 1 else 'stories'}"


def _audio(cfg: Config, d: date) -> dict[str, Any] | None:
    """Timing data with URLs for the reader, or None while the audio is not rendered yet."""
    t = load_timing(cfg, d)
    if not t or not (audio_dir(cfg, d) / "day.mp3").is_file():
        return None
    base = f"/day/{d.isoformat()}/audio/"
    return {
        "day": base + "day.mp3",
        "stories": [
            {
                "url": base + s["file"],
                "duration": s["duration"],
                "words": s["words"],
                "segments": s["segments"],
            }
            for s in t.get("stories", [])
        ],
    }


def _day_row(cfg: Config, d: date, n: int) -> str:
    weekday, _ = long_date(d)
    iso = d.isoformat()
    pdfs = "".join(f'<a class="ghost" href="/day/{iso}/pdf/{p.name}">PDF</a>' for p in day_pdfs(cfg, d))
    if not pdfs:
        pdfs = '<span class="ghost off" title="No PDF for this day">PDF</span>'
    if (audio_dir(cfg, d) / "day.mp3").is_file():
        mp3 = f'<a class="ghost" href="/day/{iso}/audio/day.mp3" download="briefing-{iso}.mp3">MP3</a>'
    else:
        mp3 = '<span class="ghost off" title="The spoken version is being prepared">MP3</span>'
    return (
        f'<li><div><div class="date">{weekday}, {d.day} {MONTHS[d.month - 1]}</div>'
        f'<div class="muted">{_count(n)}</div></div>'
        f'<div class="actions"><a class="primary" href="/day/{iso}">Read</a>{pdfs}{mp3}</div></li>'
    )


def _tree(cfg: Config, listing: list[tuple[date, int]]) -> str:
    """Year > month > ISO week > day, newest first; only the newest branch starts expanded."""
    years: dict[int, dict[int, dict[tuple[int, int], list[tuple[date, int]]]]] = {}
    for d, n in sorted(listing, reverse=True):
        iso = d.isocalendar()
        years.setdefault(d.year, {}).setdefault(d.month, {}).setdefault((iso.year, iso.week), []).append(
            (d, n)
        )
    out: list[str] = []
    first = True
    for year, months in years.items():
        y_total = sum(n for weeks in months.values() for days_ in weeks.values() for _, n in days_)
        out.append(
            f'<details class="y"{" open" if first else ""}><summary>{year}<span>{_count(y_total)}</span></summary>'
        )
        for month, weeks in months.items():
            m_total = sum(n for days_ in weeks.values() for _, n in days_)
            out.append(
                f'<details class="m"{" open" if first else ""}><summary>{MONTHS[month - 1]}'
                f"<span>{_count(m_total)}</span></summary>"
            )
            for (_, week), days_ in weeks.items():
                start = days_[0][0] - timedelta(days=days_[0][0].weekday())
                end = start + timedelta(days=6)
                span = f"{start.day} {MONTHS[start.month - 1][:3]} – {end.day} {MONTHS[end.month - 1][:3]}"
                out.append(
                    f'<details class="w"{" open" if first else ""}><summary>Week {week}'
                    f"<span>{span} · {_count(sum(n for _, n in days_))}</span></summary><ul>"
                )
                out.extend(_day_row(cfg, d, n) for d, n in days_)
                out.append("</ul></details>")
                first = False
            out.append("</details>")
        out.append("</details>")
    return "".join(out)


def create_reader_app(cfg: Config) -> Starlette:
    def db() -> sqlite3.Connection:
        return connect(cfg.db_path, readonly=True)

    async def index(_req: Request) -> Response:
        conn = db()
        try:
            listing = days(conn, cfg)
        finally:
            conn.close()
        body = _tree(cfg, listing) or '<p class="muted">No briefings yet.</p>'
        return HTMLResponse(_page(INDEX_HTML).replace("{{ITEMS}}", body), headers=HEADERS)

    async def day_page(req: Request) -> Response:
        d = _day(req.path_params["day"])
        if d is None:
            return PlainTextResponse("not found", status_code=404)
        conn = db()
        try:
            stories = day_stories(conn, cfg, d)
        finally:
            conn.close()
        data = [
            {
                "headline": s.headline,
                "meta": " · ".join(
                    x
                    for x in (s.category, s.region, f"Importance {s.importance}/10" if s.importance else "")
                    if x
                ),
                "words": reading_words(s, cfg.reader_country),
            }
            for s in stories
        ]
        pdfs = [f"/day/{d.isoformat()}/pdf/{p.name}" for p in day_pdfs(cfg, d)]
        audio = _audio(cfg, d)
        if audio and len(audio["stories"]) != len(data):
            audio = None  # stories changed since the audio was rendered; a new render is pending
        payload = json.dumps(
            {"day": d.isoformat(), "stories": data, "pdfs": pdfs, "audio": audio}, ensure_ascii=False
        )
        page = (
            _page(READER_HTML)
            .replace("{{TITLE}}", html.escape(_title(d)))
            .replace("{{DATA}}", payload.replace("</", "<\\/"))
        )
        return HTMLResponse(page, headers=HEADERS)

    async def pdf(req: Request) -> Response:
        d = _day(req.path_params["day"])
        name = req.path_params["name"]
        if d is None or not _PDF_NAME.match(name):
            return PlainTextResponse("not found", status_code=404)
        path = cfg.briefing_dir / d.isoformat() / name
        if not path.is_file():
            return PlainTextResponse("not found", status_code=404)
        return FileResponse(path, media_type="application/pdf", filename=name, headers=FILE_HEADERS)

    async def mp3(req: Request) -> Response:
        d = _day(req.path_params["day"])
        name = req.path_params["name"]
        if d is None or not _MP3_NAME.match(name):
            return PlainTextResponse("not found", status_code=404)
        path = audio_dir(cfg, d) / name
        if not path.is_file():
            return PlainTextResponse("not found", status_code=404)
        filename = (
            f"briefing-{d.isoformat()}.mp3" if name == "day.mp3" else f"briefing-{d.isoformat()}-{name}"
        )
        return FileResponse(
            path,
            media_type="audio/mpeg",
            filename=filename,
            content_disposition_type="inline",
            headers=FILE_HEADERS,
        )

    async def healthz(_req: Request) -> Response:
        return PlainTextResponse("ok")

    return Starlette(
        routes=[
            Route("/", index),
            Route("/day/{day}", day_page),
            Route("/day/{day}/pdf/{name}", pdf),
            Route("/day/{day}/audio/{name}", mp3),
            Route("/healthz", healthz),
        ]
    )


def _page(template: str) -> str:
    return template.replace("{{TRANSLATE}}", TRANSLATE_HTML)


_BASE_CSS = """
:root{--bg:#f7f6f2;--card:#fff;--ink:#1c1c20;--muted:#6f6f78;--accent:#224a76;--pivot:#c0392b;--line:#dcdce2}
@media (prefers-color-scheme:dark){:root{--bg:#141417;--card:#1d1d22;--ink:#ececf0;--muted:#9a9aa6;
--accent:#8fb3dd;--pivot:#ff7a6b;--line:#2e2e36}}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
body{top:0!important}.skiptranslate iframe,.goog-te-banner-frame{display:none!important}
a{color:var(--accent)}.muted{color:var(--muted)}
.wrap{max-width:760px;margin:0 auto;padding:20px 16px 40px}
.kicker{font-size:.72rem;letter-spacing:.14em;text-transform:uppercase;color:var(--accent);font-weight:600}
h1{font-family:"Iowan Old Style","Palatino Linotype",Georgia,serif;font-size:1.9rem;line-height:1.15;margin:.3rem 0 1.2rem}
.lang{display:flex;align-items:center;gap:8px;font-size:.8rem;color:var(--muted)}
.lang select{font:inherit;font-size:.82rem;padding:4px 6px;border:1px solid var(--line);border-radius:8px;
background:var(--card);color:var(--ink)}
.goog-logo-link,.goog-te-gadget>span{display:none!important}.goog-te-gadget{font-size:0!important;color:transparent!important}
"""

# Google Website Translator: translates the page text in the browser. The speed-reading stage is
# excluded (translate="no"): translating one flashing word at a time would make no sense.
TRANSLATE_HTML = """<div class="lang notranslate" translate="no"><span>Language</span><div id="gt"></div></div>
<script>function googleTranslateElementInit(){new google.translate.TranslateElement({pageLanguage:"en",autoDisplay:false},"gt");}</script>
<script src="https://translate.google.com/translate_a/element.js?cb=googleTranslateElementInit" async></script>"""

INDEX_HTML = (
    """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Daily Briefings</title><style>"""
    + _BASE_CSS
    + """
.head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap}
ul{list-style:none;padding:0;margin:0}li{display:flex;justify-content:space-between;align-items:center;gap:12px;
background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:10px}
details{margin:0 0 8px}summary{cursor:pointer;list-style:none;display:flex;justify-content:space-between;
align-items:baseline;gap:12px;padding:10px 4px;border-bottom:1px solid var(--line)}
summary::-webkit-details-marker{display:none}
summary:before{content:"";width:7px;height:7px;border-right:2px solid var(--muted);border-bottom:2px solid var(--muted);
transform:rotate(-45deg);margin-right:10px;transition:transform .15s;flex:none;align-self:center}
details[open]>summary:before{transform:rotate(45deg)}
summary span{margin-left:auto;font-size:.8rem;color:var(--muted);font-weight:400}
.y>summary{font-family:"Iowan Old Style","Palatino Linotype",Georgia,serif;font-size:1.5rem;font-weight:600}
.m{margin-left:6px}.m>summary{font-size:1.1rem;font-weight:600}
.w{margin-left:12px}.w>summary{font-size:.92rem;color:var(--muted);font-weight:600}
.w ul{margin:10px 0 4px}
.date{font-weight:600}.actions{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}
.actions a,.actions span{text-decoration:none;padding:8px 14px;border-radius:999px;font-weight:600;font-size:.9rem}
.primary{background:var(--accent);color:var(--bg)!important}.ghost{border:1px solid var(--line)}
.off{color:var(--muted);opacity:.55;cursor:default}
</style></head><body><main class="wrap"><div class="head"><div><div class="kicker">News Relay</div>
<h1>Daily Briefings</h1></div>{{TRANSLATE}}</div>
<ul>{{ITEMS}}</ul></main></body></html>"""
)

READER_HTML = (
    (
        """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{{TITLE}} · Speed reading</title><style>"""
        + _BASE_CSS
        + """
.top{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap}
.links{display:flex;flex-direction:column;align-items:flex-end;gap:8px}
.links nav a{text-decoration:none;font-size:.9rem;margin-left:12px}
.stories{display:flex;gap:8px;overflow-x:auto;padding:4px 0 12px;scrollbar-width:thin}
.stories button{flex:0 0 auto;max-width:240px;text-align:left;border:1px solid var(--line);background:var(--card);
color:var(--ink);border-radius:10px;padding:8px 10px;font:inherit;font-size:.82rem;cursor:pointer;line-height:1.3}
.stories button.on{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent) inset}
.stories b{color:var(--accent);margin-right:4px}
.stage{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px 14px 22px;
user-select:none;-webkit-user-select:none;cursor:pointer}
.meta{font-size:.8rem;color:var(--muted);min-height:1.2em}
.headline{font-family:"Iowan Old Style","Palatino Linotype",Georgia,serif;font-size:1.05rem;margin:.2rem 0 .8rem;min-height:1.4em}
.label{text-align:center;font-size:.7rem;letter-spacing:.16em;text-transform:uppercase;color:var(--muted);min-height:1.1em}
.guide{position:relative;margin:10px 0}
.guide:before,.guide:after{content:"";position:absolute;left:50%;width:2px;height:12px;background:var(--line);transform:translateX(-1px)}
.guide:before{top:-12px}.guide:after{bottom:-12px}
.word{display:grid;grid-template-columns:minmax(0,1fr) auto minmax(0,1fr);align-items:baseline;
font-family:"Iowan Old Style","Palatino Linotype",Georgia,serif;font-size:clamp(1.5rem,6vw,2.4rem);
line-height:1.25;padding:14px 0;white-space:pre}
.side{display:flex;align-items:baseline;overflow:hidden;min-width:0}
.side.l{justify-content:flex-end;-webkit-mask-image:linear-gradient(90deg,transparent,#000 55%);
mask-image:linear-gradient(90deg,transparent,#000 55%)}
.side.r{justify-content:flex-start;-webkit-mask-image:linear-gradient(270deg,transparent,#000 55%);
mask-image:linear-gradient(270deg,transparent,#000 55%)}
.piv{color:var(--pivot)}.pre,.post{flex:none}
.ctx{flex:none;font-size:.55em;color:var(--muted);white-space:pre}
.bar{height:4px;background:var(--line);border-radius:4px;margin:16px 0 6px;overflow:hidden}
.bar i{display:block;height:100%;width:0;background:var(--accent)}
.status{display:flex;justify-content:space-between;font-size:.8rem;color:var(--muted)}
.controls{display:flex;gap:10px;justify-content:center;margin:18px 0 10px}
.controls button{font:inherit;font-weight:600;border:1px solid var(--line);background:var(--card);color:var(--ink);
border-radius:999px;padding:10px 18px;min-width:64px;cursor:pointer}
.controls button.play{background:var(--accent);border-color:var(--accent);color:var(--bg);min-width:110px}
.speed{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px 14px}
.speed .row{display:flex;align-items:center;gap:12px}
.speed input[type=range]{flex:1;accent-color:var(--accent)}
.speed input[type=number]{width:84px;font:inherit;padding:6px 8px;border:1px solid var(--line);border-radius:8px;
background:var(--bg);color:var(--ink)}
.presets{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
.presets button{font:inherit;font-size:.8rem;border:1px solid var(--line);background:none;color:var(--muted);
border-radius:999px;padding:4px 10px;cursor:pointer}
.opts{display:flex;gap:16px;flex-wrap:wrap;margin-top:12px;font-size:.85rem;color:var(--muted)}
.opts .note{flex-basis:100%;font-size:.78rem;min-height:1em}
.hint{font-size:.78rem;color:var(--muted);margin-top:14px}
.doc{margin-top:28px}.doc h2{font-family:"Iowan Old Style","Palatino Linotype",Georgia,serif;font-size:1.25rem;margin:0 0 4px}
.pages canvas{display:block;width:100%;height:auto;margin:0 auto 14px;background:#fff;border-radius:6px;
box-shadow:0 1px 2px rgba(0,0,0,.08),0 4px 18px rgba(0,0,0,.06)}
</style></head><body><main class="wrap">
<div class="top"><div><div class="kicker">Speed reading</div><h1>{{TITLE}}</h1></div>
<div class="links"><nav><a href="/">All days</a><span id="files"></span></nav>{{TRANSLATE}}</div></div>
<div class="stories" id="stories"></div>
<section class="stage notranslate" id="stage" translate="no" aria-live="off">
<div class="meta" id="meta"></div><div class="headline" id="headline"></div>
<div class="label" id="label"></div>
<div class="guide"><div class="word" id="word"><span class="side l"><span class="ctx" id="ctxl"></span><span class="pre" id="pre"></span></span><span class="piv" id="piv"></span><span class="side r"><span class="post" id="post"></span><span class="ctx" id="ctxr"></span></span></div></div>
<div class="bar"><i id="bar"></i></div>
<div class="status"><span id="pos"></span><span id="left"></span></div>
</section>
<div class="controls"><button id="back" title="Back 10 words (Left)">Back</button>
<button id="play" class="play" title="Play / pause (Space)">Play</button>
<button id="fwd" title="Forward 10 words (Right)">Next</button></div>
<section class="speed"><div class="row"><label for="wpm">Words per minute</label>
<input type="range" id="wpm" min="100" max="2000" step="10">
<input type="number" id="wpmn" min="100" max="2000" step="10" aria-label="Words per minute"></div>
<div class="presets" id="presets"></div>
<div class="opts"><label><input type="checkbox" id="speak"> Read aloud</label>
<label><input type="checkbox" id="ctx" checked> Show neighbouring words</label>
<label><input type="checkbox" id="pause" checked> Pause longer at punctuation</label>
<span class="note" id="speaknote"></span></div></section>
<p class="hint">Space: play/pause · Left/Right: 10 words · Up/Down: speed ±25 · Tap the word to play or pause.</p>
<section class="doc" id="doc" hidden><h2>Read the PDF</h2><p class="muted" id="docnote">Loading…</p><div class="pages" id="pages"></div></section>
</main>
<script>
const DATA = {{DATA}};
const $ = id => document.getElementById(id);
const store = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } };
const tokens = []; const starts = [];
DATA.stories.forEach((s, si) => { starts.push(tokens.length); s.words.forEach(([label, w], wi) => tokens.push({si, label, w, first: wi === 0})); });
let i = Math.min(+(store("pos:" + DATA.day) || 0), Math.max(0, tokens.length - 1));
let wpm = Math.max(100, Math.min(2000, +(store("wpm") || 350)));
let timer = null, playing = false;

// ---- words -----------------------------------------------------------------------------------
function pivotIndex(w) { const n = w.replace(/[^\\p{L}\\p{N}]/gu, "").length; return n <= 1 ? 0 : n <= 5 ? 1 : n <= 9 ? 2 : n <= 13 ? 3 : 4; }
function splitWord(w) {
  let lead = 0; while (lead < w.length && !/[\\p{L}\\p{N}]/u.test(w[lead])) lead++;
  const p = Math.min(w.length - 1, lead + pivotIndex(w));
  return [w.slice(0, p), w[p] || "", w.slice(p + 1)];
}
function delay(t) {
  let d = 60000 / wpm;
  if ($("pause").checked) {
    if (/[.!?]["'”»)]*$/.test(t.w)) d *= 2.2; else if (/[,;:–—]["'”»)]*$/.test(t.w)) d *= 1.5;
    if (t.w.length > 9) d *= 1.25;
    if (t.first) d *= 2.5;
  }
  return d;
}
function context(at) {
  if (!$("ctx").checked) return ["", ""];
  const si = tokens[at].si, before = [], after = [];
  for (let k = at - 1; k >= 0 && before.length < 8 && tokens[k].si === si; k--) before.unshift(tokens[k].w);
  for (let k = at + 1; k < tokens.length && after.length < 8 && tokens[k].si === si; k++) after.push(tokens[k].w);
  return [before.length ? before.join(" ") + "  " : "", after.length ? "  " + after.join(" ") : ""];
}
function render() {
  if (!tokens.length) { $("headline").textContent = "No stories for this day."; return; }
  const t = tokens[i], s = DATA.stories[t.si];
  const [a, b, c] = splitWord(t.w);
  $("pre").textContent = a; $("piv").textContent = b; $("post").textContent = c;
  const [left, right] = context(i); $("ctxl").textContent = left; $("ctxr").textContent = right;
  $("label").textContent = t.label; $("meta").textContent = s.meta; $("headline").textContent = s.headline;
  $("bar").style.width = (100 * (i + 1) / tokens.length) + "%";
  $("pos").textContent = "Story " + (t.si + 1) + " of " + DATA.stories.length + " · word " + (i + 1) + " of " + tokens.length;
  const eff = speaking() ? effectiveWpm(t.si) : wpm;
  const mins = (tokens.length - i) / eff; $("left").textContent = mins < 1 ? Math.ceil(mins * 60) + " s left" : Math.ceil(mins) + " min left";
  document.querySelectorAll(".stories button").forEach((el, k) => el.classList.toggle("on", k === t.si));
  store("pos:" + DATA.day, i);
}

// ---- read aloud: the audio clock drives the words ----------------------------------------------
const AUDIO = DATA.audio; const audio = new Audio(); audio.preload = "auto"; audio.preservesPitch = true;
let audioStory = -1, raf = 0;
const MAX_RATE = 4, MIN_RATE = 0.5;
const plans = (AUDIO ? AUDIO.stories : []).map((sa, si) => {
  // per segment: cumulative character weights of its words, for positions inside a sentence
  const segs = sa.segments.map(([t0, t1, w0, w1]) => {
    const lens = []; let acc = 0;
    for (let k = w0; k < w1; k++) { acc += (DATA.stories[si].words[k] || ["", ""])[1].length + 1; lens.push(acc); }
    return {t0, t1, w0, w1, lens, total: acc || 1};
  });
  const spoken = segs.reduce((n, s) => n + (s.t1 - s.t0), 0) || 1;
  return {url: sa.url, segs, natural: sa.words / (spoken / 60)};
});
function speaking() { return $("speak").checked && plans.length > 0; }
function rateFor(si) { const p = plans[si]; return p ? Math.max(MIN_RATE, Math.min(MAX_RATE, wpm / p.natural)) : 1; }
function effectiveWpm(si) { const p = plans[si]; return p ? Math.round(p.natural * rateFor(si)) : wpm; }
function wordAt(si, t) {
  const p = plans[si]; let last = 0;
  for (const s of p.segs) {
    if (t < s.t0) return last;
    if (t < s.t1) { const target = (t - s.t0) / (s.t1 - s.t0) * s.total; let k = 0; while (k < s.lens.length - 1 && s.lens[k] < target) k++; return s.w0 + k; }
    last = Math.max(s.w0, s.w1 - 1);
  }
  return last;
}
function timeOf(si, w) {
  const p = plans[si];
  for (const s of p.segs) if (w >= s.w0 && w < s.w1) { const before = w > s.w0 ? s.lens[w - s.w0 - 1] : 0; return s.t0 + before / s.total * (s.t1 - s.t0); }
  const next = p.segs.find(s => s.w0 >= w); return next ? next.t0 : 0;
}
function speakNote() {
  if (!plans.length) { $("speaknote").textContent = AUDIO === null ? "The spoken version is being prepared on the server." : ""; return; }
  if (!$("speak").checked) { $("speaknote").textContent = ""; return; }
  const si = tokens.length ? tokens[i].si : 0, r = rateFor(si);
  $("speaknote").textContent = r >= MAX_RATE && wpm > effectiveWpm(si) ? "Voice speed is limited to " + MAX_RATE + "× (about " + effectiveWpm(si) + " words per minute)." : "";
}
function loop() {
  const si = audioStory; if (si < 0) return;
  const w = starts[si] + wordAt(si, audio.currentTime);
  if (w !== i) { i = w; render(); }
  raf = requestAnimationFrame(loop);
}
function startAudio() {
  const si = tokens[i].si;
  if (audioStory !== si) { audio.src = plans[si].url; audioStory = si; }
  const seek = () => { audio.currentTime = timeOf(si, i - starts[si]); audio.playbackRate = rateFor(si); audio.play().catch(() => stop()); };
  if (audio.readyState >= 1) seek(); else audio.addEventListener("loadedmetadata", seek, {once: true});
  cancelAnimationFrame(raf); raf = requestAnimationFrame(loop); speakNote();
}
audio.addEventListener("ended", () => {
  const next = audioStory + 1; cancelAnimationFrame(raf);
  if (playing && next < DATA.stories.length) { i = starts[next]; render(); startAudio(); } else stop();
});

// ---- transport ---------------------------------------------------------------------------------
function step() {
  if (i >= tokens.length - 1) { stop(); return; }
  i++; render(); timer = setTimeout(step, delay(tokens[i]));
}
function play() {
  if (!tokens.length) return; if (i >= tokens.length - 1) i = 0;
  playing = true; $("play").textContent = "Pause"; render();
  if (speaking()) startAudio(); else timer = setTimeout(step, delay(tokens[i]));
}
function stop() { playing = false; clearTimeout(timer); timer = null; cancelAnimationFrame(raf); audio.pause(); $("play").textContent = "Play"; }
function toggle() { playing ? stop() : play(); }
function jump(n) { const run = playing; stop(); i = Math.max(0, Math.min(tokens.length - 1, i + n)); render(); if (run) play(); }
function setWpm(v) {
  wpm = Math.max(100, Math.min(2000, Math.round(+v / 10) * 10 || 350));
  $("wpm").value = wpm; $("wpmn").value = wpm; store("wpm", wpm);
  if (speaking() && audioStory >= 0) audio.playbackRate = rateFor(audioStory);
  speakNote(); render();
}

DATA.stories.forEach((s, k) => { const b = document.createElement("button"); b.innerHTML = '<b class="notranslate" translate="no">' + String(k + 1).padStart(2, "0") + '</b>'; b.appendChild(document.createTextNode(s.headline)); b.onclick = () => { const run = playing; stop(); i = starts[k]; render(); if (run) play(); }; $("stories").appendChild(b); });
[200, 300, 400, 500, 700, 1000, 1500, 2000].forEach(v => { const b = document.createElement("button"); b.textContent = v; b.onclick = () => setWpm(v); $("presets").appendChild(b); });
const link = (href, text, file) => { const a = document.createElement("a"); a.href = href; a.textContent = text; if (file) a.download = file; $("files").appendChild(a); };
DATA.pdfs.forEach((u, k) => link(u, DATA.pdfs.length > 1 ? "PDF " + (k + 1) : "PDF"));
if (AUDIO) link(AUDIO.day, "MP3", "briefing-" + DATA.day + ".mp3");
$("speak").disabled = !plans.length; $("speak").checked = plans.length > 0 && store("speak") === "1";
$("speak").onchange = () => { store("speak", $("speak").checked ? "1" : "0"); const run = playing; stop(); speakNote(); if (run) play(); };
$("wpm").oninput = e => setWpm(e.target.value); $("wpmn").onchange = e => setWpm(e.target.value);
$("play").onclick = toggle; $("back").onclick = () => jump(-10); $("fwd").onclick = () => jump(10);
$("stage").onclick = toggle; $("ctx").onchange = render;
document.addEventListener("keydown", e => {
  if (e.target.tagName === "INPUT" && e.target.type === "number") return;
  if (e.code === "Space") { e.preventDefault(); toggle(); }
  else if (e.key === "ArrowLeft") jump(-10); else if (e.key === "ArrowRight") jump(10);
  else if (e.key === "ArrowUp") { e.preventDefault(); setWpm(wpm + 25); } else if (e.key === "ArrowDown") { e.preventDefault(); setWpm(wpm - 25); }
});
setWpm(wpm);

// ---- the PDF below, rendered with pdf.js (works on phones, where inline PDFs often do not) --------
async function loadPdf() {
  if (!DATA.pdfs.length) return;
  $("doc").hidden = false;
  try {
    await new Promise((ok, fail) => { const s = document.createElement("script"); s.src = "PDFJS/pdf.min.js"; s.integrity = "PDFJS_SRI"; s.crossOrigin = "anonymous"; s.onload = ok; s.onerror = fail; document.head.appendChild(s); });
    const src = await (await fetch("PDFJS/pdf.worker.min.js", {integrity: "PDFJS_WORKER_SRI"})).text();
    pdfjsLib.GlobalWorkerOptions.workerSrc = URL.createObjectURL(new Blob([src], {type: "text/javascript"}));
    const doc = await pdfjsLib.getDocument(DATA.pdfs[DATA.pdfs.length - 1]).promise;
    $("docnote").textContent = doc.numPages + " pages · " ;
    const a = document.createElement("a"); a.href = DATA.pdfs[DATA.pdfs.length - 1]; a.textContent = "open or download"; $("docnote").appendChild(a);
    const width = $("pages").clientWidth, ratio = Math.min(window.devicePixelRatio || 1, 2.5);
    for (let n = 1; n <= doc.numPages; n++) {
      const page = await doc.getPage(n), base = page.getViewport({scale: 1});
      const vp = page.getViewport({scale: width / base.width * ratio});
      const c = document.createElement("canvas"); c.width = vp.width; c.height = vp.height;
      $("pages").appendChild(c);
      await page.render({canvasContext: c.getContext("2d"), viewport: vp}).promise;
    }
  } catch (e) {
    $("docnote").textContent = "The PDF could not be shown here. ";
    const a = document.createElement("a"); a.href = DATA.pdfs[DATA.pdfs.length - 1]; a.textContent = "Open it directly"; $("docnote").appendChild(a);
  }
}
loadPdf();
</script></body></html>"""
    )
    .replace("PDFJS_WORKER_SRI", PDFJS_WORKER_SRI)
    .replace("PDFJS_SRI", PDFJS_SRI)
    .replace("PDFJS", PDFJS)
)
