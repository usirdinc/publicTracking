"""Small, local translation catalog. Client identifiers and USCIS source text are never translated."""
import json
import re
from pathlib import Path
from jinja2 import pass_context

CATALOG = json.loads((Path(__file__).parent / "locales" / "my.json").read_text(encoding="utf-8"))
LANGUAGES = ("en", "my")

def language(request):
    return request.cookies.get("usird_language", "en") if request.cookies.get("usird_language") in LANGUAGES else "en"

def translate(text, locale="en", **values):
    source = str(text or "")
    # These existing server messages interpolate the configured limit.
    for pattern, key in [
        (r"You can track up to (\d+) cases\.", "You can track up to {count} cases."),
        (r"You can track up to (\d+) people across all cases\.", "You can track up to {count} people across all cases."),
        (r"You can track up to (\d+) people per account\.", "You can track up to {count} people per account."),
        (r"Fee due: \$(.+)", "Fee due: ${amount}"),
    ]:
        match = re.fullmatch(pattern, source)
        if match:
            values = {**values, ("amount" if "amount" in key else "count"): match[1]}
            source = key
            break
    result = CATALOG.get(source, source) if locale == "my" else source
    if values:
        try:
            return result.format_map(values)
        except (KeyError, ValueError):
            return source
    return result

@pass_context
def template_translate(context, text, **values):
    request = context.get("request")
    return translate(text, language(request) if request else "en", **values)
