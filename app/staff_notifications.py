"""Durable delivery of verified EOIR/ICE change alerts through existing contacts."""
import hashlib
from contextlib import closing
from app import db,notifications,config


def drain_one():
    with closing(db.connect()) as cn:
        with db.tx(cn):
            cn.execute("UPDATE staff_notification_outbox SET state='pending' WHERE state='sending' AND next_at<=?",(db.iso(db.now()),))
            row=cn.execute("SELECT * FROM staff_notification_outbox WHERE state='pending' AND next_at<=? ORDER BY id LIMIT 1",(db.iso(db.now()),)).fetchone()
            if not row:return False
            case=cn.execute('SELECT * FROM cases WHERE id=?',(row['case_id'],)).fetchone()
            cn.execute("UPDATE staff_notification_outbox SET state='sending',next_at=? WHERE id=?",(db.in_(minutes=5),row['id']))
        subject='Your '+row['kind'].upper()+' tracking result changed'
        detail_path=f"/cases/{case['id']}/"+('court' if row['kind']=='eoir' else 'ice')
        body=('A verified '+row['kind'].upper()+' lookup result for your tracked case has changed.\n\n'
              'Sign in to review the latest information: '+config.BASE_URL+detail_path+'\n\n'
              'For official details, confirm with the court or agency. This is an information service, not legal advice.')
        event_id='staff-change-'+str(row['id'])
        notifications.send_update(case,subject,body,row['kind'],event_id=event_id,retry_failed=True)
        event=hashlib.sha256(f"{case['id']}|{row['kind']}|{event_id}|{subject}".encode()).hexdigest()
        failed=cn.execute("SELECT 1 FROM notification_deliveries WHERE event_key=? AND state='failed'",(event,)).fetchone()
        cn.execute('UPDATE staff_notification_outbox SET state=?,next_at=? WHERE id=?',
            ('pending' if failed else 'done',db.in_(minutes=5),row['id']))
    return True
