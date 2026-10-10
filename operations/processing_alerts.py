"""Durable failure notifications. No provider errors or financial payloads in mail.

The sender uses an existing SMTP account configured in the worker environment.
Delivery with an unknown outcome is retained for review, not blindly resent.
"""
from email.message import EmailMessage
from email.utils import parseaddr
import hashlib
import re
import smtplib
import ssl


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS jnp_processing_alerts (
        alert_key text PRIMARY KEY,batch_key text NOT NULL,stage text NOT NULL,
        reason text NOT NULL,state text NOT NULL DEFAULT 'pending',
        created_at timestamptz NOT NULL DEFAULT now(),sent_at timestamptz,
        CHECK(state IN ('pending','sending','sent','uncertain')))''')


def enqueue(conn,batch_key,stage,reason):
    if not re.fullmatch(r'jnp:3977752:batch:\d{4}-\d\d-\d\dT\d{4}',batch_key):
        raise ValueError('invalid_alert_batch')
    if any(not re.fullmatch(r'[a-z_][a-z_0-9-]{0,79}',x) for x in (stage,reason)):
        raise ValueError('alert_requires_safe_reason_code')
    key=hashlib.sha256(f'{batch_key}|{stage}|{reason}'.encode()).hexdigest()
    initialize(conn)
    conn.execute('''INSERT INTO jnp_processing_alerts(alert_key,batch_key,stage,reason)
        VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING''',(key,batch_key,stage,reason))
    return key


def config(environ):
    names=('JNP_SMTP_HOST','JNP_SMTP_USER','JNP_SMTP_PASSWORD',
           'JNP_ALERT_FROM','JNP_ALERT_TO')
    if any(not environ.get(k) for k in names):
        raise ValueError('notification_delivery_not_configured')
    cfg={k:environ[k] for k in names}
    for name in ('JNP_ALERT_FROM','JNP_ALERT_TO'):
        value=cfg[name]
        if '\r' in value or '\n' in value or parseaddr(value)[1]!=value or '@' not in value:
            raise ValueError('invalid_notification_address')
    cfg['port']=int(environ.get('JNP_SMTP_PORT','465'))
    if not 1<=cfg['port']<=65535:
        raise ValueError('invalid_smtp_port')
    return cfg


def message(cfg,key,batch,stage,reason):
    msg=EmailMessage()
    msg['From']=cfg['JNP_ALERT_FROM'];msg['To']=cfg['JNP_ALERT_TO']
    msg['Subject']='JNP-verwerking vraagt aandacht'
    msg['Message-ID']=f'<jnp-{key}@{cfg["JNP_ALERT_FROM"].split("@")[1]}>'
    msg.set_content(f'Verwerking: {batch}\nOnderdeel: {stage}\n'
                    f'Reden: {reason}\n\nDe verwerking is niet als volledig afgerond bevestigd. '
                    'Controleer de opgeslagen taakstatus voordat een boeking wordt herhaald.\n')
    return msg


def deliver_one(app,environ,smtp_factory=smtplib.SMTP_SSL):
    """One sender, TLS, bounded connection timeout, durable delivery intent."""
    cfg=config(environ)
    with app._db_connect() as conn:
        initialize(conn)
        if not conn.execute("SELECT pg_try_advisory_lock(hashtextextended('jnp:alert-sender',0))").fetchone()[0]:
            return 'busy'
        try:
            # A process ended after recording delivery intent: do not duplicate.
            conn.execute("UPDATE jnp_processing_alerts SET state='uncertain' WHERE state='sending'")
            conn.commit()
            row=conn.execute('''SELECT alert_key,batch_key,stage,reason FROM jnp_processing_alerts
                WHERE state='pending' ORDER BY created_at,alert_key LIMIT 1''').fetchone()
            if not row:return 'idle'
            # Connect/login may safely fail before any message submission.
            with smtp_factory(cfg['JNP_SMTP_HOST'],cfg['port'],timeout=30,
                              context=ssl.create_default_context()) as smtp:
                smtp.login(cfg['JNP_SMTP_USER'],cfg['JNP_SMTP_PASSWORD'])
                conn.execute("UPDATE jnp_processing_alerts SET state='sending' WHERE alert_key=%s",(row[0],))
                conn.commit()
                try:
                    rejected=smtp.send_message(message(cfg,*row))
                    if rejected:raise RuntimeError('notification_recipient_rejected')
                except Exception:
                    conn.execute("UPDATE jnp_processing_alerts SET state='uncertain' WHERE alert_key=%s",(row[0],))
                    conn.commit()
                    return 'uncertain'
                conn.execute("UPDATE jnp_processing_alerts SET state='sent',sent_at=now() WHERE alert_key=%s",(row[0],))
                conn.commit()
                return 'sent'
        finally:
            conn.execute("SELECT pg_advisory_unlock(hashtextextended('jnp:alert-sender',0))")
