"""Conservative official-page result recognition. Errors and unknown pages are never no-match."""
import re
from urllib.parse import urlsplit
from tracker.eoir import NO_CASE_TEXT, extract_fields


def parse_result(service,text,url,number):
    parsed=urlsplit(url)
    expected='acis.eoir.justice.gov' if service=='eoir' else 'locator.ice.gov'
    if parsed.scheme!='https' or parsed.netloc!=expected or len(text)>40000:return None
    if re.search(r'captcha error|unable to validate|access denied|service unavailable|try again later',text,re.I):return None
    if service=='eoir':
        if NO_CASE_TEXT.casefold() in text.casefold():return {'status':'NO_CASE','fields':{}}
        if 'caseinformation' not in parsed.path.casefold():return None
        # Require the returned A-number and a substantive court field before saving.
        returned=re.search(r'A[- ]?Number\s*:?\s*([0-9 -]{8,14})',text,re.I)
        if not returned or re.sub(r'\D','',returned[1]).zfill(9)!=number:return None
        fields=extract_fields(text)
        fields.pop('name',None)
        if not fields:return None
        return {'status':'CASE_FOUND','fields':fields}
    # ICE result layouts must match explicit result markers. A normal search page or
    # the FAQ's general "not found" guidance cannot count as an actual no-match.
    if not re.search(r'(?:search[-_/]?results|detainee[-_/]?details)',parsed.fragment,re.I):return None
    lines=[line.strip() for line in text.splitlines() if line.strip()]
    if any(line.casefold() in ('no detainees found.','no detainees found','no matching detainees found.','no matching detainees found') for line in lines):
        return {'status':'no_match','facility':''}
    returned=re.search(r'A[- ]?Number\s*:?\s*([0-9 -]{8,14})',text,re.I)
    if not returned or re.sub(r'\D','',returned[1]).zfill(9)!=number:return None
    facility=re.search(r'(?:Current Detention Facility|Detention Facility|Facility Name)\s*:?\s*\n([^\n]{2,160})',text,re.I)
    if not facility:return None
    return {'status':'listed','facility':facility[1].strip()}
