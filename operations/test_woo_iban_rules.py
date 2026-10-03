import asyncio
import hashlib
import hmac
import json
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from operations import woo_iban_rules as m

IBAN='NL91ABNA0417164300'
ACCOUNT='11111111-1111-1111-1111-111111111111'

def body():
    return dict(transaction_id='987',administration_id=m.ADMINISTRATION,financial_account_id=m.BANK_ACCOUNT,
        order_id=137645,order_number='48794',order_date='2026-10-01',payment_date='2026-10-02',
        iban=IBAN,amount='135.00',currency='EUR',payment_method='bacs',order_status='completed',origin='plugin_completed')

def rule(**kw):
    return dict(ID='rule-1',Account=ACCOUNT,AccountBankAccount=IBAN,**kw)

def test_valid_evidence():
    assert m.validate(body())['iban']==IBAN
    assert m.iban('nl91 abna 0417 1643 00')==IBAN

@pytest.mark.parametrize('field,value', [('amount','0.00'),('amount','-1.00'),('currency','USD'),
    ('order_status','pending'),('payment_method','cod'),('administration_id','123'),
    ('financial_account_id','123'),('order_date','2026-09-30'),('payment_date','2026-09-30'),
    ('iban','NL00ABNA0417164300'),('iban',''),('order_id',True)])
def test_invalid_evidence(field,value):
    b=body();b[field]=value
    with pytest.raises(ValueError):m.validate(b)

def test_hmac(monkeypatch):
    key='a'*64;monkeypatch.setenv('WOO_IBAN_SHARED_SECRET',key)
    raw=json.dumps(body()).encode();stamp='1791028800'
    signature=hmac.new(key.encode(),stamp.encode()+b'.'+raw,hashlib.sha256).hexdigest()
    headers={'x-mbom-timestamp':stamp,'x-mbom-signature':signature}
    m.authenticate(headers,raw,now=int(stamp))
    for content,now in [(raw+b' ',int(stamp)),(raw,int(stamp)+301)]:
        with pytest.raises(HTTPException):m.authenticate(headers,content,now=now)

def test_existing_rule():
    assert m.existing_rule([rule()],IBAN,ACCOUNT)==('done','rule-1')
    assert m.existing_rule([rule(Words='48794')],IBAN,ACCOUNT)[0]=='conflict'
    assert m.existing_rule([rule(GLAccount='other')],IBAN,ACCOUNT)[0]=='conflict'
    assert m.existing_rule([rule()],IBAN,'other')[0]=='conflict'
    assert m.existing_rule([],IBAN,ACCOUNT)==('missing',None)

class DB:
    def __init__(self):self.calls=[]
    def execute(self,sql,args):self.calls.append((sql,args))

def api(responses):
    return type('API',(),{'account':AsyncMock(return_value=ACCOUNT),'rules':AsyncMock(side_effect=responses),'create':AsyncMock()})()

def test_create_and_verify():
    db=DB();a=api([[],[rule()]])
    asyncio.run(m.process(db,a,'e',body(),'pending'))
    a.create.assert_awaited_once_with(ACCOUNT,IBAN)
    assert "state='creating'" in db.calls[0][0]
    assert "state='done'" in db.calls[-1][0]

def test_idempotent_existing():
    db=DB();a=api([[rule()]])
    asyncio.run(m.process(db,a,'e',body(),'pending'))
    a.create.assert_not_awaited()
    assert "state='done'" in db.calls[-1][0]

def test_conflict_never_overwritten():
    db=DB();a=api([[rule(Words='other')]])
    asyncio.run(m.process(db,a,'e',body(),'pending'))
    a.create.assert_not_awaited()
    assert "state='conflict'" in db.calls[-1][0]

@pytest.mark.parametrize('previous',['creating','uncertain'])
def test_uncertain_not_blindly_retried(previous):
    db=DB();a=api([[]])
    asyncio.run(m.process(db,a,'e',body(),previous))
    a.create.assert_not_awaited()
    assert "state='uncertain'" in db.calls[-1][0]

def test_timeout_reconciled_by_readback():
    db=DB();a=api([[]]);a.create.side_effect=TimeoutError()
    asyncio.run(m.process(db,a,'e',body(),'pending'))
    assert "state='uncertain'" in db.calls[-1][0]
    a=api([[rule()]])
    asyncio.run(m.process(db,a,'e',body(),'uncertain'))
    a.create.assert_not_awaited();assert "state='done'" in db.calls[-1][0]

def test_old_creation_path_replaced(monkeypatch):
    from app import main
    monkeypatch.setenv('REPLACE_ORDER_RULES_WITH_IBAN','true')
    with pytest.raises(HTTPException) as exc: asyncio.run(main.create_order_rule('48794'))
    assert exc.value.status_code==410
