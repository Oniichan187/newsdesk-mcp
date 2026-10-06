"""RSVP speed reader: one page per day, 100–2000 words per minute, plus the day's PDFs.

Read-only: every request opens the database in read-only mode. The service listens on localhost and
is meant to be published only inside the tailnet (`tailscale serve`, never Funnel).
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
from datetime import date, timedelta

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from starlette.routing import Route

from ..briefing.pdf import MONTHS, long_date
from ..briefing.store import day_pdfs, day_stories, days
from ..briefing.text import reading_words
from ..config import Config
from ..database import connect

_PDF_NAME = re.compile(r"^briefing-\d{4}-\d{2}-\d{2}-[a-z0-9-]{1,40}\.pdf$")
HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


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


def _day_row(cfg: Config, d: date, n: int) -> str:
    weekday, _ = long_date(d)
    pdfs = "".join(
        f'<a class="ghost" href="/day/{d.isoformat()}/pdf/{p.name}">PDF</a>' for p in day_pdfs(cfg, d)
    )
    return (
        f'<li><div><div class="date">{weekday}, {d.day} {MONTHS[d.month - 1]}</div>'
        f'<div class="muted">{_count(n)}</div></div>'
        f'<div class="actions"><a class="primary" href="/day/{d.isoformat()}">Read</a>{pdfs}</div></li>'
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
        return HTMLResponse(INDEX_HTML.replace("{{ITEMS}}", body), headers=HEADERS)

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
        payload = json.dumps({"day": d.isoformat(), "stories": data, "pdfs": pdfs}, ensure_ascii=False)
        page = READER_HTML.replace("{{TITLE}}", html.escape(_title(d))).replace(
            "{{DATA}}", payload.replace("</", "<\\/")
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
        return FileResponse(path, media_type="application/pdf", filename=name, headers=HEADERS)

    async def healthz(_req: Request) -> Response:
        return PlainTextResponse("ok")

    return Starlette(
        routes=[
            Route("/", index),
            Route("/day/{day}", day_page),
            Route("/day/{day}/pdf/{name}", pdf),
            Route("/healthz", healthz),
        ]
    )


_BASE_CSS = """
:root{--bg:#f7f6f2;--card:#fff;--ink:#1c1c20;--muted:#6f6f78;--accent:#224a76;--pivot:#c0392b;--line:#dcdce2}
@media (prefers-color-scheme:dark){:root{--bg:#141417;--card:#1d1d22;--ink:#ececf0;--muted:#9a9aa6;
--accent:#8fb3dd;--pivot:#ff7a6b;--line:#2e2e36}}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
a{color:var(--accent)}.muted{color:var(--muted)}
.wrap{max-width:760px;margin:0 auto;padding:20px 16px 40px}
.kicker{font-size:.72rem;letter-spacing:.14em;text-transform:uppercase;color:var(--accent);font-weight:600}
h1{font-family:"Iowan Old Style","Palatino Linotype",Georgia,serif;font-size:1.9rem;line-height:1.15;margin:.3rem 0 1.2rem}
"""

INDEX_HTML = (
    """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Daily Briefings</title><style>"""
    + _BASE_CSS
    + """
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
.actions a{text-decoration:none;padding:8px 14px;border-radius:999px;font-weight:600;font-size:.9rem}
.primary{background:var(--accent);color:var(--bg)!important}.ghost{border:1px solid var(--line)}
</style></head><body><main class="wrap"><div class="kicker">News Relay</div><h1>Daily Briefings</h1>
<ul>{{ITEMS}}</ul></main></body></html>"""
)

READER_HTML = (
    """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{{TITLE}} · Speed reading</title><style>"""
    + _BASE_CSS
    + """
.top{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
.top a{text-decoration:none;font-size:.9rem}
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
.hint{font-size:.78rem;color:var(--muted);margin-top:14px}
.pdfs a{margin-left:10px}
</style></head><body><main class="wrap">
<div class="top"><div><div class="kicker">Speed reading</div><h1>{{TITLE}}</h1></div>
<div><a href="/">All days</a><span class="pdfs" id="pdfs"></span></div></div>
<div class="stories" id="stories"></div>
<section class="stage" id="stage" aria-live="off">
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
<div class="opts"><label><input type="checkbox" id="ctx" checked> Show neighbouring words</label>
<label><input type="checkbox" id="pause" checked> Pause longer at punctuation</label></div></section>
<p class="hint">Space: play/pause · Left/Right: 10 words · Up/Down: speed ±25 · Tap the word to play or pause.</p>
</main>
<script>
const DATA = {{DATA}};
const $ = id => document.getElementById(id);
const store = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } };
const tokens = []; const starts = [];
DATA.stories.forEach((s, si) => { starts.push(tokens.length); s.words.forEach(([label, w], wi) => tokens.push({si, label, w, first: wi === 0})); });
let i = Math.min(+(store("pos:" + DATA.day) || 0), Math.max(0, tokens.length - 1));
let wpm = Math.max(100, Math.min(2000, +(store("wpm") || 350)));
let timer = null;

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
  // Neighbouring words of the same story, faded in beside the main word (plain text only).
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
  const mins = (tokens.length - i) / wpm; $("left").textContent = mins < 1 ? Math.ceil(mins * 60) + " s left" : Math.ceil(mins) + " min left";
  document.querySelectorAll(".stories button").forEach((el, k) => el.classList.toggle("on", k === t.si));
  store("pos:" + DATA.day, i);
}
function step() {
  if (i >= tokens.length - 1) { stop(); return; }
  i++; render(); timer = setTimeout(step, delay(tokens[i]));
}
function play() { if (!tokens.length) return; if (i >= tokens.length - 1) i = 0; $("play").textContent = "Pause"; render(); timer = setTimeout(step, delay(tokens[i])); }
function stop() { clearTimeout(timer); timer = null; $("play").textContent = "Play"; }
function toggle() { timer ? stop() : play(); }
function jump(n) { const run = !!timer; stop(); i = Math.max(0, Math.min(tokens.length - 1, i + n)); render(); if (run) play(); }
function setWpm(v) { wpm = Math.max(100, Math.min(2000, Math.round(+v / 10) * 10 || 350)); $("wpm").value = wpm; $("wpmn").value = wpm; store("wpm", wpm); render(); }

DATA.stories.forEach((s, k) => { const b = document.createElement("button"); b.innerHTML = "<b>" + String(k + 1).padStart(2, "0") + "</b>"; b.appendChild(document.createTextNode(s.headline)); b.onclick = () => { const run = !!timer; stop(); i = starts[k]; render(); if (run) play(); }; $("stories").appendChild(b); });
[200, 300, 400, 500, 700, 1000, 1500, 2000].forEach(v => { const b = document.createElement("button"); b.textContent = v; b.onclick = () => setWpm(v); $("presets").appendChild(b); });
DATA.pdfs.forEach((u, k) => { const a = document.createElement("a"); a.href = u; a.textContent = DATA.pdfs.length > 1 ? "PDF " + (k + 1) : "PDF"; $("pdfs").appendChild(a); });
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
</script></body></html>"""
)
