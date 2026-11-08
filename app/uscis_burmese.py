"""Burmese USCIS wording with an optional background queue; no network during rendering."""
import hashlib
import json
import re
from dataclasses import dataclass
from html import escape
from html.parser import HTMLParser
from pathlib import Path

from markupsafe import Markup
from app.uscis_display import description_html


def normalize(text):
    return re.sub(r"\s+", " ", text.replace("\u00a0", " ").replace("’", "'")).strip().casefold()


_data = json.loads((Path(__file__).parent / "locales" / "uscis-my.json").read_text(encoding="utf-8"))
PHRASES = {}
for english, burmese in _data["phrases"].items():
    key = normalize(english)
    if key in PHRASES and PHRASES[key] != burmese:
        raise ValueError("Conflicting USCIS phrase translations")
    PHRASES[key] = burmese

FIELDS = {
    "date": r"(?:[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})",
    "form": r"[A-Z]{1,3}-\d{2,4}[A-Z]?",
    "receipt": r"[A-Z]{3}\d{10}",
}
PATTERNS = []
for item in _data["patterns"]:
    parts = re.split(r"(\{\w+\})", item["english"])
    expression = "".join("(?P<" + part[1:-1] + ">" + FIELDS[part[1:-1]] + ")"
                         if part.startswith("{") else re.escape(part).replace(r"\ ", r"\s+")
                         for part in parts)
    PATTERNS.append((re.compile(expression, re.I), item["burmese"]))


def translate_unit(text):
    translated = PHRASES.get(normalize(text))
    if translated is not None:
        return translated, normalize(translated) != normalize(text), False
    for pattern, target in PATTERNS:
        match = pattern.fullmatch(text.strip())
        if match:
            return target.format_map(match.groupdict()), True, False
    # Unknown words are retained, never guessed or inferred from a status substring.
    return text, False, bool(re.search(r"[A-Za-z]", text))


@dataclass(frozen=True)
class SourceTranslation:
    original: Markup
    burmese: Markup
    translated_parts: int
    untranslated_parts: int
    pending_parts: int = 0
    machine_parts: int = 0
    key: str = ""

    @property
    def has_translation(self):
        return self.translated_parts > 0

    @property
    def complete(self):
        return self.untranslated_parts == 0


class ResponseTranslator(HTMLParser):
    """Translate only text inside markup already sanitized by description_html."""

    def __init__(self, automatic=False):
        super().__init__(convert_charrefs=True)
        self.automatic = automatic
        self.pending_parts = 0
        self.machine_parts = 0
        self.output = []
        self.translated_parts = 0
        self.untranslated_parts = 0

    def handle_starttag(self, tag, attrs):
        # The only attributes here are the sanitizer's trusted link attributes
        # and its fixed notice-heading class. No input HTML reaches this parser.
        self.output.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        self.output.append("</" + tag + ">")

    def handle_data(self, text):
        if not text.strip():
            self.output.append(escape(text))
            return
        prefix = text[:len(text) - len(text.lstrip())]
        suffix = text[len(text.rstrip()):]
        value = text.strip()
        translated, changed, missing = translate_unit(value)
        if missing:
            # Match whole known sentences when USCIS returns several in one node.
            sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z])", value)
            if len(sentences) > 1:
                self.output.append(escape(prefix))
                for index, sentence in enumerate(sentences):
                    if index:
                        self.output.append(" ")
                    self.emit(*translate_unit(sentence))
                self.output.append(escape(suffix))
                return
        self.output.append(escape(prefix))
        self.emit(translated, changed, missing)
        self.output.append(escape(suffix))

    def emit(self, text, changed, missing):
        if missing and self.automatic:
            from app.google_translation import lookup
            state, result = lookup(text)
            if state == "ready":
                text, changed, missing = result, True, False
                self.machine_parts += 1
            elif state == "pending":
                self.pending_parts += 1
        self.translated_parts += int(changed)
        self.untranslated_parts += int(missing)
        if missing:
            self.output.append('<span class="uscis-untranslated" lang="en">' + escape(text) + '</span>')
        else:
            self.output.append(escape(text))


def translate_source(value, automatic=False):
    original = description_html(value)
    parser = ResponseTranslator(automatic)
    parser.feed(str(original))
    parser.close()
    return SourceTranslation(original, Markup("".join(parser.output)),
                             parser.translated_parts, parser.untranslated_parts,
                             parser.pending_parts, parser.machine_parts, hashlib.sha256(str(original).encode()).hexdigest())
