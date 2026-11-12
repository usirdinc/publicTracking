"""Single-use email password recovery with durable request limits."""
import hashlib
import hmac
import secrets
from datetime import timedelta
from app import config,db,mailer,security

MESSAGE='If that email has an account, a password reset link will be sent. Check your inbox and spam folder.'


def request_reset(cn,email,ip):
    email=email.strip().lower()
    key=lambda value:hmac.new(config.SECRET_KEY.encode(),value.encode(),hashlib.sha256).hexdigest()
    now=db.now()
    with db.tx(cn):
        cn.execute('DELETE FROM password_reset_requests WHERE created_at<?',(db.iso(now-timedelta(days=1)),))
        address_key,ip_key=key('email:'+email),key('ip:'+ip)
        hour=db.iso(now-timedelta(hours=1))
        if cn.execute('SELECT COUNT(*) FROM password_reset_requests WHERE email_key=? AND created_at>?',(address_key,hour)).fetchone()[0]>=2:return
        if cn.execute('SELECT COUNT(*) FROM password_reset_requests WHERE ip_key=? AND created_at>?',(ip_key,hour)).fetchone()[0]>=10:return
        cn.execute('INSERT INTO password_reset_requests(email_key,ip_key,created_at) VALUES(?,?,?)',(address_key,ip_key,db.iso(now)))
        user=cn.execute('SELECT id,email FROM users WHERE email=?',(email,)).fetchone()
        if not user:return
        token=secrets.token_urlsafe(32)
        cn.execute('INSERT INTO password_resets(user_id,token_hash,expires_at) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET token_hash=excluded.token_hash,expires_at=excluded.expires_at',
            (user['id'],hashlib.sha256(token.encode()).hexdigest(),db.iso(now+timedelta(minutes=30))))
    # Fragment stays out of HTTP requests, proxy logs, and referrers. The reset page
    # reads it locally and posts the token in the form body over HTTPS.
    url=config.BASE_URL+'/reset-password#token='+token
    return (user['email'],'Reset your USIRD Case Tracker password',
        'Use this link to choose a new password:\n\n'+url+'\n\nThis link expires in 30 minutes and can be used once. '
        'If you did not request this, ignore this email. Your password has not changed.')


def apply_reset(cn,token,password):
    if len(token)!=43 or not 10<=len(password)<=256:return False
    digest=hashlib.sha256(token.encode()).hexdigest()
    hashed=security.hash_password(password)
    with db.tx(cn):
        row=cn.execute('SELECT user_id FROM password_resets WHERE token_hash=? AND expires_at>?',(digest,db.iso(db.now()))).fetchone()
        if not row:return False
        cn.execute('UPDATE users SET pw_hash=?,auth_version=auth_version+1 WHERE id=?',(hashed,row['user_id']))
        cn.execute('DELETE FROM password_resets WHERE user_id=?',(row['user_id'],))
    return True
