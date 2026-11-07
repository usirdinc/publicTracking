"""Conservative display formatting for untrusted USCIS descriptions."""
import json
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
            self.output.append(escape(data).replace('\n', '<br>'))

    def result(self):
        while self.stack:
            self.output.append('</' + self.stack.pop() + '>')
        return Markup(''.join(self.output).strip().removeprefix('<br>'))


def description_html(value):
    parser = DescriptionParser()
    parser.feed(value or '')
    parser.close()
    return parser.result()


def case_history(encrypted):
    if not encrypted:
        return []
    from cryptography.fernet import InvalidToken
    try:
        result = json.loads(security.decrypt(encrypted))
        return result if isinstance(result, list) else []
    except (InvalidToken, ValueError, TypeError):
        return []
