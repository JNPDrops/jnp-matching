"""Strict matching policy and private read-only audit; no financial writes.

Individual financial identities remain in the existing private batch dossier.
"""
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import re

from fastapi import APIRouter, HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from operations import fibonatix_import as legacy

POLICY = 'same_source_order_required_20261004_2145_CEST'
ARTIFACT = 'order_match_audit'
legacy.ARTIFACTS.add(ARTIFACT)
router = APIRouter(prefix=legacy.router.prefix)
from operations.strict_order_matching import router as strict_router
router.include_router(strict_router)


def retired_action(path):
    prefix = legacy.router.prefix + '/'
    if not path.startswith(prefix):
        return False
    action = path[len(prefix):].rstrip('/')
    return action == 'automatic' or action.startswith('settle_')


class SourceOrderPolicyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.method == 'POST' and retired_action(request.url.path):
            try:
                legacy.authorize(request)
            except HTTPException as exc:
                return JSONResponse({'detail':exc.detail},status_code=exc.status_code)
            return JSONResponse({'detail':'same_source_order_required_legacy_action_retired',
                                 'policy':POLICY},status_code=409)
        return await call_next(request)


def candidates(lines, debtor_code):
    result=[]
    for line in lines:
        # Source receipts are positive bank lines; difference rows have no
        # source-order description. Refund offsets have no debtor relationship.
        if str(line.get('AccountCode') or '').strip()!=debtor_code or Decimal(str(line.get('AmountDC') or 0))<=0:
            continue
        match=re.search(r'\bOrder (TD\d+)\b',line.get('Description') or '')
        if match and line.get('YourRef')!=match[1]:
            result.append({'source_order':match[1],'allocated_reference':line.get('YourRef'),
                'bank_line_id':line['ID'],'entry_id':line['EntryID'],'entry_number':line['EntryNumber'],
                'amount':str(line['AmountDC']),'description':line['Description'],
                'status':'requires_actual_invoice_match_verification'})
    return result


async def audit():
    conn=legacy.database(); locked=False
    try:
        locked=conn.execute('SELECT pg_try_advisory_lock(%s)',(legacy.LOCK,)).fetchone()[0]
        if not locked:
            return
        legacy.update(running=True,action='audit_order_matches',last_error=None)
        lines=await legacy.ledger()
        open_items=await legacy.receivables()
        found=candidates(lines,legacy.application().COLLECTIVE_DEBTOR_CODE)
        legacy.artifact(ARTIFACT,{'policy':POLICY,'read_at':datetime.now(timezone.utc).isoformat(),
            'writes_executed':False,'candidates':found,'ledger':lines,'receivables':open_items,
            'limitation':'Verify the actual matched invoice before repairing a reference difference.'})
        legacy.update(phase='order_match_audit_complete',matching_policy=POLICY,
                      order_match_candidate_count=len(found),historical_matches_repaired=False)
    except asyncio.CancelledError:
        if locked: legacy.update(phase='order_match_audit_interrupted',last_error='worker_cancelled')
        raise
    except Exception as exc:
        if locked: legacy.update(phase='order_match_audit_failed',last_error=type(exc).__name__)
    finally:
        if locked:
            legacy.update(running=False)
            conn.execute('SELECT pg_advisory_unlock(%s)',(legacy.LOCK,))
        conn.close()


@router.get('/order-policy')
async def policy(request:Request):
    legacy.authorize(request)
    return {'policy':POLICY,'legacy_cross_order_actions_blocked':True,
            'order_repair_implemented':True,
            'historical_matches_repaired':legacy.state().get('historical_matches_repaired', False)}


@router.post('/audit_order_matches')
async def start_audit(request:Request):
    legacy.authorize(request)
    legacy.state()
    if any(not t.done() for t in legacy.TASKS):
        raise HTTPException(409,'job_already_running')
    task=asyncio.create_task(audit())
    legacy.TASKS.add(task)
    task.add_done_callback(legacy.TASKS.discard)
    return {'accepted':True,'read_only':True,'policy':POLICY}
