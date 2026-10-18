"""Turn a day's stories into speech.

The spoken text is exactly the reader's word list (`reading_words`), cut into sentences. Every
sentence is synthesised separately, so its start and end time are known. Kokoro also reports when
each phoneme is spoken; grouped into words, that gives every word its own start time (timing version
2). Where that grouping does not line up with the text, the reader falls back to the word's position
inside the sentence. Section labels ("Key facts", "Impact Austria") are spoken as short cues of their
own.

Output per day (in briefing_dir/<day>/audio/): story-NN.mp3, day.mp3 and timing.json. timing.json
carries a hash of the spoken content and the voice, so a day is only re-rendered when it changed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Protocol

from .. import timeutil
from ..briefing.pdf import long_date
from ..briefing.store import day_stories, local_day
from ..briefing.text import StoryText, reading_words
from ..config import Config
from ..logutil import log

logger = logging.getLogger("newsrelay.audio")

SAMPLE_RATE = 24000
PAUSE_SENTENCE = 0.28
PAUSE_LABEL = 0.45
PAUSE_STORY = 1.2
MAX_SEGMENT_WORDS = 38
_END = re.compile(r"[.!?:;][\"'”’)\]]*$")
_SPOKEN = re.compile(r"\w")
TIMING_VERSION = 2


@dataclass
class Spoken:
    samples: Any
    word_starts: list[float] | None = None  # seconds into `samples`, one per space-separated word


class Synth(Protocol):
    # -> 1-D float32 numpy array at SAMPLE_RATE, or Spoken with per-word start times
    def __call__(self, text: str) -> Any: ...


def _samples(out: Any) -> Any:
    import numpy as np

    return np.asarray(out.samples if isinstance(out, Spoken) else out, dtype=np.float32).reshape(-1)


def word_starts(phonemes: list[tuple[str, float]], counts: list[int]) -> list[float] | None:
    """Start time of each text word from (phoneme, start) pairs.

    `counts` = how many spoken phoneme words each text word becomes ("1,600" -> 3, "—" -> 0).
    Returns None when the phoneme words do not add up to the text, so nothing is guessed.
    """
    groups: list[float] = []
    current = ""
    start = 0.0
    for ph, t in [*phonemes, (" ", 0.0)]:
        if ph == " ":
            if _SPOKEN.search(current):
                groups.append(start)
            current = ""
            continue
        if not current:
            start = t
        current += ph
    if len(groups) != sum(counts) or not groups:
        return None
    out: list[float | None] = []
    k = 0
    for c in counts:
        out.append(groups[k] if c else None)
        k += c
    nxt = groups[-1]
    for i in range(len(out) - 1, -1, -1):  # silent words ("—") take the next word's start
        if out[i] is None:
            out[i] = nxt
        nxt = out[i]  # type: ignore[assignment]
    starts = [float(x) for x in out]  # type: ignore[arg-type]
    for i in range(1, len(starts)):
        starts[i] = max(starts[i], starts[i - 1])
    return starts


@dataclass
class Segment:
    text: str
    w0: int | None = None  # first word index in the story's reading list (None = spoken label)
    w1: int | None = None  # exclusive
    t0: float = 0.0
    t1: float = 0.0
    starts: list[float] | None = None  # per word, seconds from the start of the story audio


@dataclass
class StoryAudio:
    segments: list[Segment] = field(default_factory=list)
    words: int = 0


def plan(st: StoryText, country: str) -> StoryAudio:
    """Sentence segments over the story's reading words, with spoken section cues."""
    words = reading_words(st, country)
    out = StoryAudio(words=len(words))
    label: str | None = None
    start = 0
    for i, (lab, w) in enumerate(words):
        if lab != label:
            if start < i:
                out.segments.append(Segment(" ".join(x for _, x in words[start:i]), start, i))
            start = i
            if label is not None and lab != "Headline":
                out.segments.append(Segment(f"{lab}."))
            label = lab
        last = i + 1 == len(words) or words[i + 1][0] != lab
        if _END.search(w) or i + 1 - start >= MAX_SEGMENT_WORDS or last:
            out.segments.append(Segment(" ".join(x for _, x in words[start : i + 1]), start, i + 1))
            start = i + 1
    return out


def content_hash(stories: list[StoryText], cfg: Config) -> str:
    payload = [
        [cfg.tts_voice, cfg.tts_speed, cfg.tts_lang],
        *[[w for _, w in reading_words(s, cfg.reader_country)] for s in stories],
    ]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()[:16]


def _silence(seconds: float) -> Any:
    import numpy as np

    return np.zeros(int(SAMPLE_RATE * seconds), dtype=np.float32)


def render_story(st: StoryAudio, synth: Synth) -> Any:
    """Synthesise all segments; fills in t0/t1 (seconds from the start of the story audio)."""
    import numpy as np

    parts: list[Any] = []
    t = 0.0
    for seg in st.segments:
        out = synth(seg.text)
        samples = _samples(out)
        seg.t0, seg.t1 = t, t + len(samples) / SAMPLE_RATE
        rel = out.word_starts if isinstance(out, Spoken) else None
        if seg.w0 is not None and seg.w1 is not None and rel and len(rel) == seg.w1 - seg.w0:
            seg.starts = [t + min(x, seg.t1 - t) for x in rel]
        pause = _silence(PAUSE_LABEL if seg.w0 is None else PAUSE_SENTENCE)
        parts += [samples, pause]
        t = seg.t1 + len(pause) / SAMPLE_RATE
    return np.concatenate(parts) if parts else _silence(0.1)


def encode_mp3(samples: Any, path: Path, bitrate: int = 96) -> None:
    import lameenc
    import numpy as np

    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    enc = lameenc.Encoder()
    enc.set_bit_rate(bitrate)
    enc.set_in_sample_rate(SAMPLE_RATE)
    enc.set_channels(1)
    enc.set_quality(2)
    path.write_bytes(enc.encode(pcm) + enc.flush())


def kokoro_synth(cfg: Config) -> Synth:
    """Kokoro (82M) via onnxruntime; one core is left free for the relay."""
    import onnxruntime as ort
    from kokoro_onnx import Kokoro

    model = cfg.tts_model_dir / "kokoro-v1.0.onnx"
    voices = cfg.tts_model_dir / "voices-v1.0.bin"
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = max(1, cfg.tts_threads)
    session = ort.InferenceSession(str(model), sess_options=opts, providers=["CPUExecutionProvider"])
    kokoro = Kokoro.from_session(session, str(voices))

    def spoken_words(word: str) -> int:
        return sum(1 for p in kokoro.tokenizer.phonemize(word, cfg.tts_lang).split(" ") if _SPOKEN.search(p))

    def synth(text: str) -> Any:
        samples, sr, timed = kokoro.create_timed(
            text, voice=cfg.tts_voice, speed=cfg.tts_speed, lang=cfg.tts_lang
        )
        if sr != SAMPLE_RATE:
            raise RuntimeError(f"unexpected sample rate {sr}")
        starts = None
        if timed:
            starts = word_starts(
                [(x.phoneme, x.start) for x in timed], [spoken_words(w) for w in text.split(" ")]
            )
        return Spoken(samples, starts)

    return synth


def audio_dir(cfg: Config, day: date, lang: str = "en") -> Path:
    return cfg.briefing_dir / day.isoformat() / ("audio" if lang == "en" else f"audio-{lang}")


def load_timing(cfg: Config, day: date, lang: str = "en") -> dict[str, Any] | None:
    try:
        data = json.loads((audio_dir(cfg, day, lang) / "timing.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _segment_json(s: Segment) -> list[Any]:
    row: list[Any] = [round(s.t0, 3), round(s.t1, 3), s.w0, s.w1]
    if s.starts:
        row.append([round(x, 3) for x in s.starts])
    return row


def _current(old: dict[str, Any] | None, digest: str) -> bool:
    """Audio exists for this content and already has per-word timings."""
    return bool(old) and old.get("content") == digest and old.get("version", 1) >= TIMING_VERSION  # type: ignore[union-attr]


def write_day(
    target: Path,
    plans: list[StoryAudio],
    synth: Synth,
    intro: str,
    cue: Callable[[int], str],
    extra: dict[str, Any],
    day: date,
) -> None:
    """Synthesise the stories' plans into story-NN.mp3, day.mp3 and timing.json (atomically)."""
    import numpy as np

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".audio-", dir=target.parent))
    try:
        day_parts: list[Any] = [_samples(synth(intro)), _silence(PAUSE_STORY)]
        meta: list[dict[str, Any]] = []
        for n, sa in enumerate(plans, 1):
            if n > 1:
                day_parts.append(_silence(PAUSE_STORY))
            day_parts.append(_samples(synth(cue(n))))
            day_parts.append(_silence(PAUSE_LABEL))
            samples = render_story(sa, synth)
            name = f"story-{n:02d}.mp3"
            encode_mp3(samples, tmp / name)
            day_parts.append(samples)
            meta.append(
                {
                    "file": name,
                    "duration": round(len(samples) / SAMPLE_RATE, 3),
                    "words": sa.words,
                    "segments": [_segment_json(s) for s in sa.segments if s.w0 is not None],
                }
            )
            log(
                logger,
                logging.INFO,
                "story audio",
                day=day.isoformat(),
                dir=target.name,
                story=n,
                seconds=meta[-1]["duration"],
            )
        whole = np.concatenate(day_parts)
        encode_mp3(whole, tmp / "day.mp3")
        timing = {
            "version": TIMING_VERSION,
            **extra,
            "day": {"file": "day.mp3", "duration": round(len(whole) / SAMPLE_RATE, 3)},
            "stories": meta,
        }
        (tmp / "timing.json").write_text(json.dumps(timing, ensure_ascii=False), encoding="utf-8")
        if target.exists():
            shutil.rmtree(target)
        tmp.replace(target)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)


def build_day(cfg: Config, conn: sqlite3.Connection, day: date, synth: Synth | None = None) -> bool:
    """Render the day's audio if missing or outdated. Returns True when something was written."""
    stories = day_stories(conn, cfg, day)
    if not stories:
        return False
    digest = content_hash(stories, cfg)
    if _current(load_timing(cfg, day), digest):
        return False
    synth = synth or kokoro_synth(cfg)
    weekday, long = long_date(day)
    write_day(
        audio_dir(cfg, day),
        [plan(st, cfg.reader_country) for st in stories],
        synth,
        f"Daily briefing for {weekday}, {long}.",
        lambda n: f"Story {n}.",
        {"content": digest, "voice": cfg.tts_voice},
        day,
    )
    log(logger, logging.INFO, "day audio written", day=day.isoformat(), stories=len(stories))
    return True


def build_day_de(
    cfg: Config,
    conn: sqlite3.Connection,
    day: date,
    translate: Callable[[list[str]], list[str]],
    synth: Synth,
) -> bool:
    """German text + voice for the reader; written next to the English audio as audio-de/."""
    from .german import content_hash_de, german_day, long_date_de

    stories = day_stories(conn, cfg, day)
    if not stories:
        return False
    digest = content_hash_de(content_hash(stories, cfg), cfg)
    if _current(load_timing(cfg, day, "de"), digest):
        return False
    texts, plans = german_day(stories, cfg, translate)
    weekday, long = long_date_de(day)
    write_day(
        audio_dir(cfg, day, "de"),
        plans,
        synth,
        f"Tagesbriefing für {weekday}, den {long}.",
        lambda n: f"Meldung {n}.",
        {"content": digest, "voice": cfg.tts_german_voice, "text": texts},
        day,
    )
    log(logger, logging.INFO, "day audio written", day=day.isoformat(), stories=len(stories), lang="de")
    return True


def run(cfg: Config, conn: sqlite3.Connection, days_back: int = 2, only: date | None = None) -> int:
    """Render missing/outdated audio for the last `days_back` days (newest first): English for all
    days first, then the German edition (if its models are installed)."""
    from . import german

    today = local_day(timeutil.now(), cfg.display_timezone)
    days = [only] if only else [today - timedelta(days=i) for i in range(days_back + 1)]
    synth: Synth | None = None
    done = 0
    todo_de: list[date] = []
    for d in days:
        stories = day_stories(conn, cfg, d)
        if not stories:
            continue
        digest = content_hash(stories, cfg)
        if not _current(load_timing(cfg, d), digest):
            synth = synth or kokoro_synth(cfg)
            done += int(build_day(cfg, conn, d, synth))
        if german.available(cfg) and not _current(
            load_timing(cfg, d, "de"), german.content_hash_de(digest, cfg)
        ):
            todo_de.append(d)
    if todo_de:
        synth = None  # free the English voice before loading the German one
        translate = german.argos_translator(cfg)
        synth_de = german.piper_synth(cfg)
        for d in todo_de:
            done += int(build_day_de(cfg, conn, d, translate, synth_de))
    return done
