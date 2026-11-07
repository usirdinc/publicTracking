"""Conservative display formatting for untrusted USCIS descriptions."""
import json
import re
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlsplit

from markupsafe import Markup

from app import security


class DescriptionParser(HTMLParser):
    tags = {'strong', 'b', 'em', 'i', 'ul', 'ol', 'li'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.output = []
        self.stack = []
        self.blocked = []

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'iframe', 'object', 'svg', 'math', 'template'}:
            self.blocked.append(tag)
            return
        if self.blocked:
            return
        if tag == 'br' or tag == 'p':
            self.output.append('<br>')
        elif tag in self.tags:
            self.output.append('<' + tag + '>')
            self.stack.append(tag)
        elif tag == 'a':
            url = dict(attrs).get('href') or ''
            try:
                parsed = urlsplit(url)
                host = (parsed.hostname or '').lower()
                allowed = (parsed.scheme.lower() == 'https' and
                           (host == 'uscis.gov' or host.endswith('.uscis.gov')) and
                           not parsed.username and not parsed.password and not any(ord(c)<33 for c in url))
            except ValueError:
                allowed = False
            if allowed:
                self.output.append('<a href="' + escape(url, quote=True) + '" target="_blank" rel="noopener noreferrer">')
                self.stack.append('a')

    def handle_endtag(self, tag):
        if self.blocked:
            if tag == self.blocked[-1]:
                self.blocked.pop()
            return
        if tag == 'p':
            self.output.append('<br><br>')
        elif tag in self.stack:
            while self.stack:
                current = self.stack.pop()
                self.output.append('</' + current + '>')
                if current == tag:
                    break

    def handle_data(self, data):
        if not self.blocked:
            # Only paired bold markers are recognized; HTML and URLs still pass
            # through the strict allowlist above.
            text = escape(data)
            text = re.sub(r'(?<!\*)\*\*(?=\S)([^\n]+?)(?<=\S)\*\*(?!\*)',
                          r'<strong>\1</strong>', text)
            self.output.append(text.replace('\r\n', '\n').replace('\n', '<br>'))

    def result(self):
        while self.stack:
            self.output.append('</' + self.stack.pop() + '>')
        return Markup(''.join(self.output).strip().removeprefix('<br>'))


class ParagraphLayout(HTMLParser):
    """Group sanitized inline text into balanced paragraphs without interpreting new HTML."""

    inline_tags = {'strong', 'b', 'em', 'i', 'a'}
    block_tags = {'ul', 'ol', 'li'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.output = []
        self.inline = []
        self.open_paragraph = False
        self.breaks = 0

    def close_paragraph(self):
        if self.open_paragraph:
            self.output.extend('</' + tag + '>' for tag, _ in reversed(self.inline))
            self.output.append('</p>')
            self.open_paragraph = False

    def paragraph(self):
        if self.breaks >= 2:
            self.close_paragraph()
        if not self.open_paragraph:
            self.output.append('<p>')
            self.output.extend(start for _, start in self.inline)
            self.open_paragraph = True
        elif self.breaks:
            self.output.append('<br>')
        self.breaks = 0

    def handle_starttag(self, tag, attrs):
        if tag == 'br':
            self.breaks += 1
        elif tag in self.block_tags:
            self.close_paragraph()
            self.breaks = 0
            self.output.append('<' + tag + '>')
        elif tag in self.inline_tags:
            self.paragraph()
            start = self.get_starttag_text()
            self.output.append(start)
            self.inline.append((tag, start))

    def handle_endtag(self, tag):
        if tag in self.block_tags:
            self.close_paragraph()
            self.breaks = 0
            self.output.append('</' + tag + '>')
        elif self.inline and self.inline[-1][0] == tag:
            if self.open_paragraph:
                self.output.append('</' + tag + '>')
            self.inline.pop()

    def handle_data(self, data):
        if not data.strip() and not self.open_paragraph:
            return
        self.paragraph()
        self.output.append(escape(data))

    def result(self):
        self.close_paragraph()
        result = ''.join(self.output)
        # A standalone emphasized opening line is the source's notice heading.
        return re.sub(r'^<p><strong>([^<]+)</strong></p>',
                      r'<p class="uscis-notice-heading"><strong>\1</strong></p>', result)


def description_html(value):
    parser = DescriptionParser()
    parser.feed(value or '')
    parser.close()
    layout = ParagraphLayout()
    layout.feed(str(parser.result()))
    layout.close()
    return Markup(layout.result())


def case_history(encrypted):
    if not encrypted:
        return []
    from cryptography.fernet import InvalidToken
    try:
        result = json.loads(security.decrypt(encrypted))
        return result if isinstance(result, list) else []
    except (InvalidToken, ValueError, TypeError):
        return []
