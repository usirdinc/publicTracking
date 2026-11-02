"""Browser launch for checks. Always an isolated browser: no operator profile,
no Google login, no cookies shared with the USIRD fleet."""
import os
from contextlib import contextmanager
from playwright.sync_api import sync_playwright

USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


@contextmanager
def browser_page(headful: bool = True):
    """Yield one page. headful=True needs a display (Xvfb :99 on the VPS)."""
    if headful and not os.environ.get("DISPLAY"):
        os.environ["DISPLAY"] = ":99"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headful,
                                    args=["--disable-dev-shm-usage"])
        ctx = browser.new_context(viewport={"width": 1280, "height": 900},
                                  user_agent=USER_AGENT)
        try:
            yield ctx.new_page()
        finally:
            ctx.close()
            browser.close()
