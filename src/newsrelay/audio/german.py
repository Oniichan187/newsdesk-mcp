"""German edition of a day for the reader: offline translation and a German voice.

Translation: the Argos Translate en->de model (CTranslate2 + SentencePiece, run directly, without
the argostranslate/stanza/torch stack). Each English sentence segment of the spoken plan is
translated on its own, so German words keep their section labels and sentence boundaries.
Voice: Piper (Thorsten), with the model patched to report phoneme durations, so the reader gets a
start time per German word just like for English.

Output per day: briefing_dir/<day>/audio-de/ with story-NN.mp3, day.mp3 and timing.json, which
also carries the German text ("text": headline + reading words per story).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import date
from typing import Any

from ..briefing.text import StoryText, reading_words
from ..config import Config
from .build import _SPOKEN, SAMPLE_RATE, Segment, Spoken, StoryAudio, Synth, plan, word_starts

Translate = Callable[[list[str]], list[str]]

LABELS = {
    "Headline": "Überschrift",
    "What happened": "Was passiert ist",
    "Key facts": "Wichtige Fakten",
    "Confirmed / unclear": "Bestätigt / unklar",
    "Since the last update": "Seit dem letzten Update",
    "Why it matters": "Warum es wichtig ist",
    "Correction": "Korrektur",
    "Importance": "Wichtigkeit",
    "Impact global": "Auswirkung global",
    "Outlook": "Ausblick",
    "Text": "Text",
}
COUNTRIES = {"Austria": "Österreich", "Germany": "Deutschland", "Switzerland": "Schweiz"}
WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
MONTHS = [
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]  # fmt: skip
TRANSLATION_MODEL = "translate-en_de-1_3"


def long_date_de(d: date) -> tuple[str, str]:
    return WEEKDAYS[d.weekday()], f"{d.day}. {MONTHS[d.month - 1]} {d.year}"


def label_de(label: str, country: str, translate: Translate) -> str:
    if label == f"Impact {country}":
        return f"Auswirkung {COUNTRIES.get(country) or translate([country])[0]}"
    return LABELS.get(label) or translate([label])[0]


def german_story(
    st: StoryText, country: str, translate: Translate
) -> tuple[str, list[list[str]], StoryAudio]:
    """German headline, reading words ([label, word]) and spoken plan, sentence by sentence."""
    en = plan(st, country)
    words_en = reading_words(st, country)
    texts = [s.text for s in en.segments if s.w0 is not None]
    it = iter(translate(texts))
    out = StoryAudio()
    words: list[list[str]] = []
    headline: list[str] = []
    for s in en.segments:
        if s.w0 is None:  # spoken section cue, "Key facts."
            out.segments.append(Segment(f"{label_de(s.text.rstrip('.'), country, translate)}."))
            continue
        label = words_en[s.w0][0]
        de = next(it).split()
        if not de:
            continue
        start = len(words)
        words.extend([label_de(label, country, translate), w] for w in de)
        out.segments.append(Segment(" ".join(de), start, len(words)))
        if label == "Headline":
            headline += de
    out.words = len(words)
    return " ".join(headline), words, out


def content_hash_de(english_digest: str, cfg: Config) -> str:
    key = json.dumps([english_digest, cfg.tts_german_voice, TRANSLATION_MODEL])
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def available(cfg: Config) -> bool:
    d = cfg.tts_model_dir
    return (
        cfg.tts_german
        and (d / TRANSLATION_MODEL / "model" / "model.bin").is_file()
        and (d / f"{cfg.tts_german_voice}.onnx").is_file()
    )


def argos_translator(cfg: Config) -> Translate:
    import ctranslate2
    import sentencepiece as spm

    root = cfg.tts_model_dir / TRANSLATION_MODEL
    model = ctranslate2.Translator(
        str(root / "model"), device="cpu", inter_threads=1, intra_threads=max(1, cfg.tts_threads)
    )
    sp = spm.SentencePieceProcessor(model_file=str(root / "sentencepiece.model"))
    cache: dict[str, str] = {}

    def translate(texts: list[str]) -> list[str]:
        todo = [t for t in dict.fromkeys(texts) if t not in cache]
        for i in range(0, len(todo), 16):
            batch = todo[i : i + 16]
            res = model.translate_batch(
                [sp.encode(t, out_type=str) for t in batch], beam_size=4, max_decoding_length=384
            )
            for t, r in zip(batch, res, strict=True):
                cache[t] = sp.decode(r.hypotheses[0]).strip()
        return [cache[t] for t in texts]

    return translate


def _resample(x: Any, src: int, dst: int) -> Any:
    import numpy as np

    if src == dst or len(x) == 0:
        return x
    n = round(len(x) * dst / src)
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


def piper_synth(cfg: Config) -> Synth:
    import numpy as np
    from piper import PiperVoice, SynthesisConfig

    voice = PiperVoice.load(str(cfg.tts_model_dir / f"{cfg.tts_german_voice}.onnx"))
    sr = voice.config.sample_rate
    syn = SynthesisConfig()

    def spoken_words(word: str) -> int:
        return sum(1 for sent in voice.phonemize(word) for p in "".join(sent).split(" ") if _SPOKEN.search(p))

    def synth(text: str) -> Any:
        parts: list[Any] = []
        phonemes: list[tuple[str, float]] = []
        offset = 0
        for chunk in voice.synthesize(text, syn, include_alignments=True):
            pcm = np.frombuffer(chunk.audio_int16_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            t = offset
            for a in chunk.phoneme_alignments or []:
                ph = a.phoneme
                phonemes.append((" " if ph in ("^", "$", "_") else ph, t / sr))
                t += int(a.num_samples)
            phonemes.append((" ", offset / sr))
            parts.append(pcm)
            offset += len(pcm)
        samples = _resample(np.concatenate(parts) if parts else np.zeros(1, np.float32), sr, SAMPLE_RATE)
        starts = word_starts(phonemes, [spoken_words(w) for w in text.split(" ")]) if phonemes else None
        return Spoken(samples, starts)

    return synth


def german_day(
    stories: list[StoryText], cfg: Config, translate: Translate
) -> tuple[list[dict[str, Any]], list[StoryAudio]]:
    texts: list[dict[str, Any]] = []
    plans: list[StoryAudio] = []
    for st in stories:
        headline, words, sa = german_story(st, cfg.reader_country, translate)
        texts.append({"headline": headline, "words": words})
        plans.append(sa)
    return texts, plans
