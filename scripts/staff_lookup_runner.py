"""Local staff runner for the authenticated ICE and EOIR queues.

Run on a staff workstation with Chrome installed. The workstation authenticates automatically. The operator accepts any official site prompts, and completes each CAPTCHA in the window.
"""
import argparse
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Error, sync_playwright

ICE_URL = "https://locator.ice.gov/odls/"
EOIR_URL = "https://acis.eoir.justice.gov/en/"
QUEUES = {"ice": "/staff/lookups", "eoir": "/admin/eoir"}
SITES = {"ice": ICE_URL, "eoir": EOIR_URL}


def current_host(page, url):
    return urlsplit(page.url).netloc == urlsplit(url).netloc and urlsplit(page.url).scheme == "https"


def prepare_ice(page, number, country):
    page.locator("#alienNumber").fill(number)
    select = page.locator("#alien_Search_Country")
    labels = select.locator("option").all_text_contents()
    wanted = {"myanmar": "burma"}.get(country.casefold(), country.casefold())
    matches = [label for label in labels if label.strip().casefold() == wanted]
    if len(matches) != 1:
        raise ValueError("Select the matching country of birth in the ICE form.")
    select.select_option(label=matches[0].strip())
    page.get_by_role("button", name="Search by A-Number", exact=True).click()


def prepare_eoir(page, number, nationality):
    boxes = page.locator('input[type="number"]:visible')
    # Staff must close any official notice themselves.
    boxes.first.wait_for(state="visible", timeout=120000)
    if boxes.count() != len(number):
        raise ValueError("The EOIR A-Number form has changed.")
    for index, digit in enumerate(number):
        boxes.nth(index).fill(digit)
    field = page.locator('input[role="combobox"]:visible, input.p-autocomplete-input:visible')
    field.fill(nationality)
    option = page.get_by_role("option", name=re.compile(rf"^{re.escape(nationality)}$", re.I))
    option.click(timeout=5000)
    page.get_by_role("button", name="Submit", exact=True).click()


def finish_eoir(page, queue, job):
    """Use the ordinary Submit button once if the verified form still awaits it."""
    from tracker.eoir import NO_CASE_TEXT, _captcha_token, extract_fields

    end = time.monotonic() + 180
    submit_again = True
    while time.monotonic() < end and current_host(page, EOIR_URL):
        if not job.count():
            return
        text = page.locator("body").inner_text(timeout=4000)
        outcome = NO_CASE_TEXT.lower() in text.lower()
        if "caseinformation" in urlsplit(page.url).path.lower():
            fields = extract_fields(text)
            outcome = any(key != "name" for key in fields)
        if outcome:
            area = job.locator('textarea[name="page_text"]')
            if area.count() and not area.input_value():
                area.fill(text[:24000])
            return
        button = page.get_by_role("button", name="Submit", exact=True)
        if submit_again and _captcha_token(page) and button.count() == 1 and button.is_visible() and button.is_enabled():
            # Give the official callback the first opportunity to finish.
            page.wait_for_timeout(1800)
            if current_host(page, EOIR_URL) and "caseinformation" not in urlsplit(page.url).path.lower():
                if not page.get_by_text(NO_CASE_TEXT, exact=True).count() and button.count() == 1 and button.is_visible() and button.is_enabled():
                    button.click(timeout=2000)
            submit_again = False
        queue.wait_for_timeout(800)


def run(browser, base_url, service):
    queue = browser.new_page()
    credential = Path.home() / "case-tracker-operator" / "lookup-token"
    try:
        if credential.stat().st_mode & 0o077:
            print("The Beacon lookup credential must be owner-readable only.")
            return
        token = credential.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        print("The Beacon lookup credential is missing or unreadable.")
        return
    try:
        session = queue.context.request.post(
            base_url + "/staff/lookups/session",
            headers={"Authorization": "Bearer " + token},
            max_redirects=0,
            timeout=15000,
        )
    except Error:
        print("Case Tracker could not be reached for machine sign-in.")
        return
    try:
        if session.status != 204:
            print(f"Case Tracker rejected the machine session (HTTP {session.status}).")
            return
    finally:
        session.dispose()
    queue.goto(base_url + QUEUES[service])
    while not queue.is_closed():
        if urlsplit(queue.url).path != QUEUES[service] or not current_host(queue, base_url):
            print("The requested queue is no longer open. Restart the runner to open a fresh machine session.")
            return
        row = queue.locator(f'article[data-lookup-service="{service}"]').first
        if not row.count():
            print("No due lookup is visible. Refresh the queue before the next shift.")
            return
        key = row.get_attribute("data-lookup-key")
        if not key or not re.fullmatch(r"\d+(?:-\d+)?", key):
            print("The queue item could not be identified.")
            return
        job = queue.locator(f'article[data-lookup-service="{service}"][data-lookup-key="{key}"]')
        number = (job.locator("[data-lookup-number]").inner_text()).strip().removeprefix("A")
        country = job.locator("[data-lookup-country]").inner_text().strip()
        if not re.fullmatch(r"[0-9]{9}", number) or not country:
            print("The queue item lacks valid lookup details. Review it in Case Tracker.")
            return
        official = queue.context.new_page()
        try:
            official.goto(SITES[service], wait_until="domcontentloaded")
            if not current_host(official, SITES[service]):
                print("The official site redirected to an unexpected host. Review the browser.")
                return
            print("Complete any prompts and the CAPTCHA yourself in the official tab.")
            if service == "ice":
                prepare_ice(official, number, country)
                # The ICE form controls the continuation after reCAPTCHA.
            else:
                prepare_eoir(official, number, country)
                finish_eoir(official, queue, job)
        except (Error, ValueError):
            print("The browser could not finish preparing this lookup. Continue manually or defer it.")
        print("Review the official result, then save or defer this item in the Case Tracker tab.")
        try:
            job.wait_for(state="detached", timeout=20 * 60 * 1000)
            queue.wait_for_load_state("domcontentloaded")
            if job.count():
                print("The same item is still due. Review it before starting another lookup.")
                return
        except Error:
            print("The item was not completed within 20 minutes. No new lookup was started.")
            return
        if not official.is_closed():
            official.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service", choices=QUEUES)
    args = parser.parse_args()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=False)
        try:
            run(browser, "https://track.usird.org", args.service)
        except (KeyboardInterrupt, EOFError):
            print("\nRunner stopped.")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
