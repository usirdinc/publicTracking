import re
from app.uscis_burmese import translate_source, _data
from app import db
from test_app import CLIENT_DETAILS, client, csrf, make_paid, signup


def test_asylum_description_and_ead_notice_are_translated_with_original_intact():
    source = '**Your Annual Asylum Fee is Due**<br><br>The next step in your application is adjudication. If we schedule you for an interview, you will receive an interview notice.<br><br>For information related to the 180-Day Asylum EAD Clock and the Form I-765, Application for Employment Authorization, please review: <a href="https://www.uscis.gov/notice.pdf">The 180-Day Asylum EAD Notice</a>.'
    result = translate_source(source)
    assert result.complete and result.has_translation
    assert 'စိစစ်ဆုံးဖြတ်ခြင်း' in str(result.burmese)
    assert 'ရက်ချိန်းသတ်မှတ်ပေးပါက' in str(result.burmese)
    assert 'Form I-765' in str(result.burmese) and '180' in str(result.burmese)
    assert 'href="https://www.uscis.gov/notice.pdf"' in str(result.burmese)
    assert 'The next step in your application is adjudication.' in str(result.original)
    assert '<strong>Your Annual Asylum Fee is Due</strong>' in str(result.original)


def test_history_sentences_from_the_reported_screen_are_covered():
    for message in [
        'Your application with USCIS is still pending.',
        'The next step in your application is an in-person interview. Once your interview is scheduled, you will receive an interview notice.',
        'We scheduled you or a dependent included on your application for a biometrics collection appointment.',
        'We received your application and issued you a receipt notice.',
    ]:
        result=translate_source(message)
        assert result.complete and result.has_translation, message
        assert message in str(result.original)
        assert message not in str(result.burmese)


def test_statuses_match_full_meaning_instead_of_approval_substrings():
    approved=translate_source('Case Was Approved')
    denied=translate_source('Case Was Denied')
    assert approved.complete and denied.complete
    assert 'အတည်ပြု' in str(approved.burmese)
    assert 'ငြင်းပယ်' in str(denied.burmese)
    unsupported=translate_source('Case Was Not Approved')
    assert not unsupported.has_translation and not unsupported.complete
    assert 'Case Was Not Approved' in str(unsupported.original)


def test_partial_and_unknown_text_are_preserved_and_marked():
    result=translate_source('Your application with USCIS is still pending. A newly introduced USCIS sentence.')
    assert result.has_translation and not result.complete
    assert 'ဆုံးဖြတ်ချက်' in str(result.burmese)
    assert '<span class="uscis-untranslated" lang="en">A newly introduced USCIS sentence.</span>' in str(result.burmese)
    unknown=translate_source('This message has no reviewed translation.')
    assert not unknown.has_translation and not unknown.complete
    assert 'This message has no reviewed translation.' in str(unknown.original)


def test_dates_forms_and_receipt_identifiers_are_preserved():
    value='On November 7, 2026, we received your Form I-589. Your Receipt Number is ZAR1234567890.'
    result=translate_source(value)
    assert result.complete and result.has_translation
    assert all(token in str(result.burmese) for token in ['November 7, 2026','I-589','ZAR1234567890'])
    assert str(result.original).endswith(value+'</p>')
    # Loose or malformed identifiers cannot accidentally satisfy a template.
    assert not translate_source('Your Receipt Number is unexpected.').complete


def test_translation_does_not_reintroduce_blocked_html_or_external_links():
    result=translate_source('Case Was Received<script>bad()</script><a href="javascript:bad()">unsafe</a><a href="https://evil.test/">external</a>')
    for output in [str(result.burmese),str(result.original)]:
        assert '<script' not in output and 'bad()' not in output
        assert 'href="javascript:' not in output and 'href="https://evil.test/' not in output
    escaped=translate_source('&lt;img src=x onerror=bad()&gt;')
    assert '<img' not in str(escaped.burmese) and '<img' not in str(escaped.original)


def test_burmese_view_renders_status_description_and_history_without_rewriting_database(client):
    import json
    from app import security
    signup(client);make_paid('a@example.com')
    client.post('/cases',data={**CLIENT_DETAILS,'a_numbers':'A123456789','receipt':'ZAR1234567890','csrf':csrf(client,'/app')})
    cn=db.connect()
    cn.execute("UPDATE cases SET uscis_status='Case Was Received',uscis_desc='Your application with USCIS is still pending.',uscis_checked_at='2026-11-07T12:00:00+00:00',uscis_history_enc=?",
               (security.encrypt(json.dumps([{'date':'2026-11-01','text':'We received your application and issued you a receipt notice.'}])),))
    before=dict(cn.execute('SELECT * FROM cases').fetchone())
    client.post('/language',data={'language':'my','csrf':csrf(client,'/app'),'next':'/app'})
    page=client.get('/app')
    assert page.status_code==200
    assert 'ဆုံးဖြတ်ချက် ချမှတ်ရန် စောင့်ဆိုင်းဆဲ' in page.text
    assert 'Receipt Notice) ထုတ်ပေးထားပါသည်' in page.text
    assert 'လျှောက်လွှာ လက်ခံရရှိပြီး' in page.text
    assert 'USIRD ၏ မြန်မာဘာသာပြန်' in page.text
    assert 'Your application with USCIS is still pending.' in page.text
    assert 'uscis-original-copy' in page.text and 'uscis-burmese-copy' in page.text
    assert dict(cn.execute('SELECT * FROM cases').fetchone())==before
    cn.close()


def test_local_message_templates_keep_their_parameters():
    fields=lambda s:set(re.findall(r'\{\w+\}',s))
    for item in _data['patterns']:
        assert fields(item['english'])==fields(item['burmese'])
