"""Exclusive, expiring assignments for attended EOIR/ICE browser shifts."""
import hashlib
import json
import secrets
from datetime import timedelta
from cryptography.fernet import InvalidToken
from app import config, db, security

LEASE_SECONDS = 15 * 60


def eligible(row):
    return bool(row and not row['phone_verification_required'] and
                (db.is_paid(row) or row['email'] in config.ADMIN_EMAILS))


def snapshot(cn, service, case_id, person=0, require_due=True):
    row=cn.execute('SELECT c.*,u.email,u.paid_until,u.phone_verification_required FROM cases c JOIN users u ON u.id=c.user_id WHERE c.id=?',(case_id,)).fetchone()
    if not eligible(row):return None
    numbers=list(dict.fromkeys(security.decrypt(row['a_numbers_enc']).split(';')))
    if service=='eoir':
        if not config.EOIR_ENABLED or not row['nationality'] or person!=0:return None
        next_at=row['eoir_next_at']
        country=row['nationality']
        version=[row['nationality'],row['a_numbers_enc'],row['eoir_next_at']]
    else:
        if not config.ICE_STAFF_ENABLED:return None
        check=cn.execute('SELECT * FROM ice_staff_checks WHERE case_id=? AND person=?',(case_id,person)).fetchone()
        if not check or not row['ice_birth_countries_enc'] or not 0<=person<len(numbers):return None
        country=json.loads(security.decrypt(row['ice_birth_countries_enc'])).get(numbers[person])
        if not country:return None
        next_at=check['next_at']
        version=[check['version'],row['a_numbers_enc'],country,next_at]
    if require_due and next_at and next_at>db.iso(db.now()):return None
    number=numbers[person].removeprefix('A')
    if len(number)==8:number='0'+number
    if not (len(number)==9 and number.isascii() and number.isdigit()):return None
    fingerprint=hashlib.sha256(json.dumps(version,sort_keys=True).encode()).hexdigest()
    return dict(service=service,case_id=case_id,person=person,number=number,country=country,version=fingerprint)


def claim(cn, worker, service='both'):
    with db.tx(cn):
        now=db.iso(db.now())
        cn.execute('DELETE FROM staff_lookup_leases WHERE expires_at<=?',(now,))
        if cn.execute('SELECT COUNT(*) FROM staff_lookup_leases').fetchone()[0]>=4:return None
        # Filter assigned items before LIMIT so busy workers cannot starve the remaining queue.
        candidates=[]
        admins=sorted(config.ADMIN_EMAILS)
        eligibility=" AND u.phone_verification_required=0 AND (u.paid_until>?" + (" OR u.email IN ("+",".join("?" for _ in admins)+")" if admins else "") + ") "
        parameters=[now,*admins,now]
        if service in ('both','eoir') and config.EOIR_ENABLED:
            candidates += [('eoir',r['id'],0,r['last']) for r in cn.execute("""SELECT c.id,c.eoir_checked_at AS last FROM cases c JOIN users u ON u.id=c.user_id
              WHERE c.nationality IS NOT NULL """+eligibility+""" AND (c.eoir_next_at IS NULL OR c.eoir_next_at<=?)
              AND NOT EXISTS(SELECT 1 FROM staff_lookup_leases l WHERE l.service='eoir' AND l.case_id=c.id)
              ORDER BY c.eoir_checked_at,c.id LIMIT 200""",parameters)]
        if service in ('both','ice') and config.ICE_STAFF_ENABLED:
            candidates += [('ice',r['case_id'],r['person'],r['last']) for r in cn.execute("""SELECT i.case_id,i.person,i.checked_at AS last FROM ice_staff_checks i JOIN cases c ON c.id=i.case_id JOIN users u ON u.id=c.user_id
              WHERE 1=1 """+eligibility+""" AND (i.next_at IS NULL OR i.next_at<=?) AND NOT EXISTS(SELECT 1 FROM staff_lookup_leases l
              WHERE l.service='ice' AND l.case_id=i.case_id AND l.person=i.person)
              ORDER BY i.checked_at,i.case_id,i.person LIMIT 200""",parameters)]
        for kind,cid,person,_ in sorted(candidates,key=lambda v:(v[3] or '',v[1],v[0],v[2])):
            try:job=snapshot(cn,kind,cid,person)
            except (InvalidToken,ValueError,TypeError,IndexError):continue
            if not job:continue
            token=secrets.token_urlsafe(32)
            cn.execute('INSERT INTO staff_lookup_leases(service,case_id,person,token_hash,worker,version,expires_at) VALUES(?,?,?,?,?,?,?)',
                (kind,cid,person,hashlib.sha256(token.encode()).hexdigest(),worker,job['version'],db.in_(seconds=LEASE_SECONDS)))
            job['lease_token']=token
            return job
    return None


def lease(cn, token):
    return cn.execute('SELECT * FROM staff_lookup_leases WHERE token_hash=? AND expires_at>?',
        (hashlib.sha256(token.encode()).hexdigest(),db.iso(db.now()))).fetchone()


def heartbeat(cn, token):
    with db.tx(cn):
        row=lease(cn,token)
        if not row:return False
        cn.execute('UPDATE staff_lookup_leases SET expires_at=? WHERE token_hash=?',(db.in_(seconds=LEASE_SECONDS),row['token_hash']))
        return True


def complete(cn, token, action, text='', source_url=''):
    from tracker.staff_results import parse_result
    with db.tx(cn):
        row=lease(cn,token)
        if not row:raise ValueError('Assignment expired or already completed.')
        try:current=snapshot(cn,row['service'],row['case_id'],row['person'])
        except (InvalidToken,ValueError,TypeError,IndexError):current=None
        if not current or current['version']!=row['version']:
            cn.execute('DELETE FROM staff_lookup_leases WHERE token_hash=?',(row['token_hash'],))
            return 'stale'
        result=None if action=='retry' else parse_result(row['service'],text,source_url,current['number'])
        if action!='retry' and result is None:raise ValueError('Official result is not unambiguous; defer or review it.')
        now=db.iso(db.now())
        # A failed attempt never replaces the previous official result or last-success timestamp.
        if row['service']=='ice':
            if action=='retry':
                cn.execute('UPDATE ice_staff_checks SET next_at=? WHERE case_id=? AND person=?',(db.in_(minutes=15),row['case_id'],row['person']))
            else:
                cn.execute('UPDATE ice_staff_checks SET result_enc=?,checked_at=?,next_at=? WHERE case_id=? AND person=?',
                    (security.encrypt(json.dumps(result)),now,db.in_(days=1),row['case_id'],row['person']))
        elif action=='retry':
            cn.execute('UPDATE cases SET eoir_next_at=? WHERE id=?',(db.in_(minutes=15),row['case_id']))
        else:
            cn.execute('UPDATE cases SET eoir_status=?,eoir_fields=?,eoir_checked_at=?,eoir_next_at=? WHERE id=?',
                (result['status'],json.dumps(result['fields'],sort_keys=True),now,db.in_(days=1),row['case_id']))
        cn.execute('DELETE FROM staff_lookup_leases WHERE token_hash=?',(row['token_hash'],))
        cn.execute('INSERT INTO staff_lookup_attempts(service,case_id,person,worker,outcome,finished_at) VALUES(?,?,?,?,?,?)',
            (row['service'],row['case_id'],row['person'],row['worker'],'deferred' if result is None else result['status'],now))
        return 'deferred' if result is None else 'saved'
