"""Turn a day's stories into speech.

The spoken text is exactly the reader's word list (`reading_words`), cut into sentences. Every
sentence is synthesised separately, so its start and end time are known; the reader maps a word to
the audio clock by its position inside the sentence. Section labels ("Key facts", "Impact Austria")
are spoken as short cues of their own.

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


class Synth(Protocol):
    def __call__(self, text: str) -> Any: ...  # -> 1-D float32 numpy array at SAMPLE_RATE


@dataclass
class Segment:
    text: str
    w0: int | None = None  # first word index in the story's reading list (None = spoken label)
    w1: int | None = None  # exclusive
    t0: float = 0.0
    t1: float = 0.0


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
        samples = np.asarray(synth(seg.text), dtype=np.float32).reshape(-1)
        seg.t0, seg.t1 = t, t + len(samples) / SAMPLE_RATE
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

    def synth(text: str) -> Any:
        samples, sr = kokoro.create(text, voice=cfg.tts_voice, speed=cfg.tts_speed, lang=cfg.tts_lang)
        if sr != SAMPLE_RATE:
            raise RuntimeError(f"unexpected sample rate {sr}")
        return samples

    return synth


def audio_dir(cfg: Config, day: date) -> Path:
    return cfg.briefing_dir / day.isoformat() / "audio"


def load_timing(cfg: Config, day: date) -> dict[str, Any] | None:
    try:
        data = json.loads((audio_dir(cfg, day) / "timing.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def build_day(cfg: Config, conn: sqlite3.Connection, day: date, synth: Synth | None = None) -> bool:
    """Render the day's audio if missing or outdated. Returns True when something was written."""
    stories = day_stories(conn, cfg, day)
    if not stories:
        return False
    digest = content_hash(stories, cfg)
    old = load_timing(cfg, day)
    if old and old.get("content") == digest:
        return False
    synth = synth or kokoro_synth(cfg)
    import numpy as np

    weekday, long = long_date(day)
    target = audio_dir(cfg, day)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".audio-", dir=target.parent))
    try:
        intro = np.asarray(synth(f"Daily briefing for {weekday}, {long}."), dtype=np.float32)
        day_parts: list[Any] = [intro, _silence(PAUSE_STORY)]
        meta: list[dict[str, Any]] = []
        for n, st in enumerate(stories, 1):
            if n > 1:
                day_parts.append(_silence(PAUSE_STORY))
            day_parts.append(np.asarray(synth(f"Story {n}."), dtype=np.float32))
            day_parts.append(_silence(PAUSE_LABEL))
            sa = plan(st, cfg.reader_country)
            samples = render_story(sa, synth)
            name = f"story-{n:02d}.mp3"
            encode_mp3(samples, tmp / name)
            day_parts.append(samples)
            meta.append(
                {
                    "file": name,
                    "duration": round(len(samples) / SAMPLE_RATE, 3),
                    "words": sa.words,
                    "segments": [
                        [round(s.t0, 3), round(s.t1, 3), s.w0, s.w1] for s in sa.segments if s.w0 is not None
                    ],
                }
            )
            log(
                logger,
                logging.INFO,
                "story audio",
                day=day.isoformat(),
                story=n,
                seconds=meta[-1]["duration"],
            )
        whole = np.concatenate(day_parts)
        encode_mp3(whole, tmp / "day.mp3")
        timing = {
            "version": 1,
            "content": digest,
            "voice": cfg.tts_voice,
            "day": {"file": "day.mp3", "duration": round(len(whole) / SAMPLE_RATE, 3)},
            "stories": meta,
        }
        (tmp / "timing.json").write_text(json.dumps(timing), encoding="utf-8")
        if target.exists():
            shutil.rmtree(target)
        tmp.replace(target)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
    log(logger, logging.INFO, "day audio written", day=day.isoformat(), stories=len(stories))
    return True


def run(cfg: Config, conn: sqlite3.Connection, days_back: int = 2, only: date | None = None) -> int:
    """Render missing/outdated audio for the last `days_back` days (newest first)."""
    today = local_day(timeutil.now(), cfg.display_timezone)
    days = [only] if only else [today - timedelta(days=i) for i in range(days_back + 1)]
    synth: Synth | None = None
    done = 0
    for d in days:
        stories = day_stories(conn, cfg, d)
        if not stories:
            continue
        old = load_timing(cfg, d)
        if old and old.get("content") == content_hash(stories, cfg):
            continue
        synth = synth or kokoro_synth(cfg)
        done += int(build_day(cfg, conn, d, synth))
    return done
