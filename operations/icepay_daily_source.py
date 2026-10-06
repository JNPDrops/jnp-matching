"""Read-only daily ICEPAY capture using a previously registered local day.

This is a source adapter, not an import adapter. A downloaded source does not
certify financial processing. Existing one-off source and import tasks remain
immutable and are compared by PSP identity before any later import.
"""
from datetime import date, datetime, time, timedelta, timezone
from operations.nightly_batches import DIVISION, ZONE, identity


def validate_window(day, saved):
    if type(day) is not date:
        raise ValueError('invalid_processing_date')
    expected = (datetime.combine(day,time.min,ZONE).astimezone(timezone.utc),
                datetime.combine(day+timedelta(days=1),time.min,ZONE).astimezone(timezone.utc))
    if not saved or tuple(saved) != expected:
        raise ValueError('registered_source_window_required')
    if datetime.now(timezone.utc) < expected[1]:
        raise ValueError('source_day_not_closed')


async def capture(app, day):
    from operations import icepay_source_window as source
    if app.DIVISION != DIVISION:
        raise ValueError('wrong_administration')
    job = identity(day,'icepay')+':source'
    with app._db_connect() as conn:
        validate_window(day,conn.execute('''SELECT window_start,window_end
            FROM jnp_nightly_runs WHERE division=%s AND processing_date=%s''',
            (DIVISION,day)).fetchone())
        # Commit before provider access. An interrupted attempt is not replayed.
        if not source.claim(conn,job):
            previous = conn.execute('''SELECT status,summary FROM
                icepay_transaction_tasks WHERE job=%s''',(job,)).fetchone()
            return {'job':job,'existing':True,'status':previous[0],'summary':previous[1]}
    status, artifacts, summary = await source.capture(day,day)
    with app._db_connect() as conn:
        source.finish(conn,job,status,artifacts,summary)
    return {'job':job,'existing':False,'status':status,'summary':summary}
