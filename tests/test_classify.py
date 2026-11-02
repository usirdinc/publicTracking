import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tracker.aaf import classify, anum_digits, first_token


class FakeLocator:
    def __init__(self, n): self.n = n
    def count(self): return self.n


class FakePage:
    def __init__(self, body, url="https://my.uscis.gov/x", form=0):
        self._body, self.url, self._form = body, url, form
    def inner_text(self, _): return self._body
    def locator(self, _): return FakeLocator(self._form)


def test_due_with_amount():
    assert classify(FakePage("Pay for your Annual Asylum Fee $100.00"))[:2] == ("DUE", "100.00")

def test_not_due():
    assert classify(FakePage("Payment is not due for this case."))[0] == "NOT_DUE"

def test_expired():
    assert classify(FakePage("The payment period for this case has expired"))[0] == "EXPIRED"

def test_block_page():
    assert classify(FakePage("Access Denied. Reference #18.abc"))[0] == "__BLOCKED__"

def test_form_still_showing_is_incomplete():
    assert classify(FakePage("Annual asylum fee questionnaire", form=1))[0] == "__INCOMPLETE__"

def test_helpers():
    assert anum_digits("A233-623-665") == "233623665"
    assert first_token("A1; A2") == "A1"
