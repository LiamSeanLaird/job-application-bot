"""The job record and the text normalisation used to compare jobs."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from html import unescape
from html.parser import HTMLParser

LEGAL_FORMS_RE = re.compile(r"\b(gmbh|mbh|ag|se|kg|ug|inc|ltd|llc|plc|bv|co)\b")
GENDER_TAG_RE = re.compile(r"[(\[]\s*(?:all genders|[mwfdxia*]\s*(?:[/|,]\s*[mwfdxia*]\s*)+)[^)\]]*[)\]]", re.IGNORECASE)
BARE_GENDER_TAG_RE = re.compile(r"(?:^|[;,\s])[mwfdx]\s*/\s*[mwfdx](?:\s*/\s*[mwfdx])*\b", re.IGNORECASE)
ZERO_WIDTH_RE = re.compile("[​‌‍⁠﻿]")
NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _ascii_lower(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char)).lower()


def normalize_company(name: str) -> str:
    return NON_ALNUM_RE.sub("", LEGAL_FORMS_RE.sub(" ", _ascii_lower(name).replace("&", " ")))


def normalize_title(title: str) -> str:
    visible = ZERO_WIDTH_RE.sub("", title)
    without_gender = BARE_GENDER_TAG_RE.sub(" ", GENDER_TAG_RE.sub(" ", visible))
    return NON_ALNUM_RE.sub(" ", _ascii_lower(without_gender)).strip()


def similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, left, right).ratio()


@dataclass
class Job:
    source: str
    native_id: str
    title: str
    company: str
    location: str
    url: str
    description: str = ""
    remote: bool = False
    posted_at: str = ""
    # Extra facts from the posting, such as salary, kept in case the posting is taken down.
    details: dict[str, str] = field(default_factory=dict)

    @property
    def job_id(self) -> str:
        return f"{self.source}:{self.native_id}"

    @property
    def dedupe_key(self) -> str:
        company = normalize_company(self.company)
        return f"{company}|{normalize_title(self.title)}" if company else self.job_id


class _TextExtractor(HTMLParser):
    SKIPPED = {"script", "style", "head"}
    BLOCKS = {"p", "br", "li", "div", "tr", "h1", "h2", "h3", "h4"}

    def __init__(self, keep_links: bool) -> None:
        super().__init__(convert_charrefs=True)
        self.keep_links = keep_links
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIPPED:
            self.skip_depth += 1
        elif tag in self.BLOCKS:
            self.parts.append("\n")
        elif tag == "a" and self.keep_links:
            href = dict(attrs).get("href")
            if href and href.startswith("http"):
                self.parts.append(f" [{href}] ")

    def handle_endtag(self, tag):
        if tag in self.SKIPPED and self.skip_depth:
            self.skip_depth -= 1

    def handle_data(self, data):
        if not self.skip_depth:
            self.parts.append(data)


def html_to_text(markup: str, keep_links: bool = False) -> str:
    parser = _TextExtractor(keep_links)
    parser.feed(unescape(markup) if "&lt;" in markup else markup)
    # close() flushes text the parser holds back after an "&", in case it starts an entity like "&amp;".
    # Without it, "FP&A" or a description ending in "R&D" loses that text.
    parser.close()
    lines = (re.sub(r"[ \t ]+", " ", line).strip() for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)
