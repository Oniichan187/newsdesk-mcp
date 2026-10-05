"""Reading-friendly PDF of one briefing: phone-sized pages, one typeface, Bionic Reading body text.

Design rules: no emoji or decorative icons, one serif family (IBM Plex Serif, OFL), generous line
height, one quiet accent colour, every story on its own page, a linked table of contents and
clickable sources. The page format (108 x 192 mm) fills a phone screen without zooming and still
reads well as a page on tablets and desktops.
"""

from __future__ import annotations

from datetime import date
from importlib import resources
from typing import Any

from fpdf import FPDF
from fpdf.enums import XPos, YPos

from .text import StoryText, bionic_markdown

PAGE = (108.0, 192.0)
MARGIN_X = 9.0
MARGIN_TOP = 12.0
MARGIN_BOTTOM = 15.0

INK = (28, 28, 32)
MUTED = (112, 112, 120)
ACCENT = (34, 74, 118)
RULE = (214, 214, 220)
SCALE_OFF = (226, 226, 232)

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip


def long_date(d: date) -> tuple[str, str]:
    return WEEKDAYS[d.weekday()], f"{d.day} {MONTHS[d.month - 1]} {d.year}"


class _Doc(FPDF):
    def __init__(self, footer_label: str) -> None:
        super().__init__(unit="mm", format=PAGE)
        self.footer_label = footer_label
        self.set_margins(MARGIN_X, MARGIN_TOP, MARGIN_X)
        self.set_auto_page_break(True, margin=MARGIN_BOTTOM)
        fonts = resources.files("newsrelay.briefing") / "fonts"
        with resources.as_file(fonts) as d:
            self.add_font("Plex", "", str(d / "IBMPlexSerif-Regular.ttf"))
            self.add_font("Plex", "B", str(d / "IBMPlexSerif-SemiBold.ttf"))
            self.add_font("Plex", "I", str(d / "IBMPlexSerif-Italic.ttf"))
            self.add_font("PlexHead", "", str(d / "IBMPlexSerif-Bold.ttf"))
            self.add_font("PlexSemi", "", str(d / "IBMPlexSerif-SemiBold.ttf"))

    def multi_cell(self, *args: Any, **kwargs: Any) -> Any:
        """Ragged-right everywhere: justified text leaves large gaps on narrow pages."""
        kwargs.setdefault("align", "L")
        return super().multi_cell(*args, **kwargs)

    @property
    def width(self) -> float:
        return self.w - self.l_margin - self.r_margin

    def footer(self) -> None:
        self.set_y(-10)
        self.set_draw_color(*RULE)
        self.set_line_width(0.2)
        self.line(self.l_margin, self.get_y() - 1.5, self.w - self.r_margin, self.get_y() - 1.5)
        self.set_font("Plex", "", 7)
        self.set_text_color(*MUTED)
        self.cell(self.width / 2, 4, self.footer_label, align="L")
        self.cell(self.width / 2, 4, f"{self.page_no()} / {{nb}}", align="R")

    # -- building blocks -------------------------------------------------------------------------

    def kicker(self, text: str, color: tuple[int, int, int] = ACCENT, size: float = 6.6) -> None:
        self.set_font("PlexSemi", "", size)
        self.set_text_color(*color)
        self.set_char_spacing(0.9)
        self.multi_cell(0, 3.8, text.upper(), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_char_spacing(0)

    def rule(self, gap_before: float = 2.5, gap_after: float = 3.5) -> None:
        self.ln(gap_before)
        self.set_draw_color(*RULE)
        self.set_line_width(0.25)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(gap_after)

    def keep(self, height: float) -> None:
        """Start a new page if fewer than `height` mm remain (keeps labels with their text)."""
        if self.will_page_break(height):
            self.add_page()

    def body(self, text: str, indent: float = 0.0, size: float = 8.8) -> None:
        self.set_font("Plex", "", size)
        self.set_text_color(*INK)
        self.set_x(self.l_margin + indent)
        self.multi_cell(
            self.width - indent,
            4.5,
            bionic_markdown(text),
            markdown=True,
            align="L",
            new_x=XPos.LMARGIN,
            new_y=YPos.NEXT,
        )

    def bullet(self, text: str) -> None:
        y = self.get_y()
        self.set_font("Plex", "", 8.8)
        self.set_text_color(*ACCENT)
        self.set_xy(self.l_margin + 0.6, y)
        self.cell(3, 4.5, "•")
        self.set_y(y)
        self.body(text, indent=4.2)
        self.ln(0.8)

    def label(self, text: str) -> None:
        self.keep(16)
        self.ln(2.2)
        self.kicker(text, MUTED, 6.8)
        self.ln(0.8)

    def scale(self, value: int) -> None:
        """Ten small squares, `value` of them filled: the importance score at a glance."""
        size, gap = 2.2, 1.0
        x, y = self.l_margin, self.get_y() + 0.6
        for i in range(10):
            self.set_fill_color(*(ACCENT if i < value else SCALE_OFF))
            self.rect(x + i * (size + gap), y, size, size, style="F")
        self.set_xy(x + 10 * (size + gap) + 1.5, self.get_y())
        self.set_font("PlexSemi", "", 8)
        self.set_text_color(*ACCENT)
        self.cell(0, 3.8, f"{value} / 10", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(1.6)


def render_pdf(day: date, stories: list[StoryText], country: str, title: str = "Daily Briefing") -> bytes:
    """Two passes: the first finds each story's start page so the contents can link to it."""
    stories = sorted(stories, key=lambda s: -s.importance)
    _, pages = _build(day, stories, country, title, None)
    doc, _ = _build(day, stories, country, title, pages)
    return bytes(doc.output())


def _build(
    day: date, stories: list[StoryText], country: str, title: str, pages: list[int] | None
) -> tuple[_Doc, list[int]]:
    weekday, long = long_date(day)
    doc = _Doc(f"{title} · {day:%d.%m.%Y}")
    doc.set_title(f"{title} – {weekday}, {long}")
    doc.set_author("News Relay")
    doc.set_creator("newsrelay")
    doc.set_lang("en")
    links: list[int | None] = [doc.add_link(page=p) for p in pages] if pages else [None] * len(stories)
    starts: list[int] = []

    # -- cover / contents ----------------------------------------------------------------------
    doc.add_page()
    doc.ln(6)
    doc.kicker(title)
    doc.ln(2.5)
    doc.set_font("PlexHead", "", 19)
    doc.set_text_color(*INK)
    doc.multi_cell(0, 8, weekday, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    doc.set_font("Plex", "", 11.5)
    doc.set_text_color(*MUTED)
    doc.multi_cell(0, 6, long, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    doc.ln(3)
    doc.set_font("Plex", "", 7.6)
    n = len(stories)
    doc.multi_cell(
        0,
        4.4,
        f"{n} {'story' if n == 1 else 'stories'} that matter for {country}, ordered by importance.",
        new_x=XPos.LMARGIN,
        new_y=YPos.NEXT,
    )
    doc.rule(4, 5)
    doc.kicker("Contents", MUTED, 6.8)
    doc.ln(2)
    for i, st in enumerate(stories):
        doc.set_font("Plex", "", 8.6)
        lines = len(doc.multi_cell(doc.width - 21, 4.3, st.headline, dry_run=True, output="LINES"))
        doc.keep(lines * 4.3 + 4)
        y = doc.get_y()
        doc.set_font("PlexSemi", "", 8.4)
        doc.set_text_color(*ACCENT)
        doc.cell(7, 4.3, f"{i + 1:02d}", link=links[i] or "")
        doc.set_xy(doc.l_margin + 7, y)
        doc.set_font("Plex", "", 8.6)
        doc.set_text_color(*INK)
        doc.multi_cell(
            doc.width - 21, 4.3, st.headline, link=links[i] or "", new_x=XPos.RIGHT, new_y=YPos.TOP
        )
        doc.set_xy(doc.w - doc.r_margin - 13, y)
        doc.set_font("PlexSemi", "", 7.6)
        doc.set_text_color(*MUTED)
        doc.cell(13, 4.3, f"{st.importance}/10", align="R")
        doc.set_y(y + lines * 4.3 + 3)
    doc.ln(4)
    doc.set_font("Plex", "I", 7)
    doc.set_text_color(*MUTED)
    doc.multi_cell(
        0,
        4,
        "The first letters of each word are set in bold (Bionic Reading) to guide the eye.",
        new_x=XPos.LMARGIN,
        new_y=YPos.NEXT,
    )

    # -- stories --------------------------------------------------------------------------------
    for st in stories:
        doc.add_page()
        starts.append(doc.page_no())
        kick = " · ".join(x for x in ("Update" if st.kind == "UPDATE" else "Correction" if st.kind == "CORRECTION" else "", st.category, st.region) if x)  # fmt: skip
        doc.kicker(kick)
        doc.ln(1.5)
        doc.set_font("PlexHead", "", 12.5)
        doc.set_text_color(*INK)
        doc.multi_cell(0, 5.5, st.headline, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        doc.ln(1.5)
        doc.set_font("Plex", "", 7.2)
        doc.set_text_color(*MUTED)
        meta = [st.date, f"Importance {st.importance}/10"] + ([st.confidence] if st.confidence else [])
        doc.multi_cell(0, 3.6, "  ·  ".join(meta), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        doc.rule(2.5, 2)

        for sec in st.sections:
            if sec.label:
                doc.label(sec.label)
            for p in sec.paragraphs:
                doc.body(p)
                doc.ln(1.2)
            for b in sec.bullets:
                doc.bullet(b)

        doc.label("Importance")
        doc.scale(st.importance)
        doc.body(st.importance_reason)
        doc.label(f"Impact {country}")
        doc.body(st.impact_country)
        doc.label("Impact global")
        doc.body(st.impact_global)
        if st.outlook:
            doc.label("Outlook")
            for o in st.outlook:
                doc.keep(12)
                doc.set_font("PlexSemi", "", 8.4)
                doc.set_text_color(*ACCENT)
                doc.multi_cell(0, 4.3, o.likelihood, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                doc.body(o.event)
                doc.set_font("Plex", "I", 7.6)
                doc.set_text_color(*MUTED)
                doc.multi_cell(0, 3.9, f"Basis: {o.basis}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                doc.ln(1.8)
        doc.label("Sources")
        for name, url in st.sources:
            doc.set_font("PlexSemi", "", 8)
            doc.set_text_color(*ACCENT)
            doc.multi_cell(0, 4.2, name, link=url, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            doc.set_font("Plex", "", 6.6)
            doc.set_text_color(*MUTED)
            doc.multi_cell(0, 3.4, url, link=url, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            doc.ln(1.2)
    return doc, starts
