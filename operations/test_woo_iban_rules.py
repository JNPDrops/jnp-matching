import asyncio
from contextlib import nullcontext
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
    return type('API',(),{'account':AsyncMock(return_value=ACCOUNT),'rules':AsyncMock(side_effect=responses),'create':AsyncMock(),'create_words':AsyncMock()})()

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

REFERENCE='bosci_0123456789abcdef0123456789abcdef'
WORDS='bosci_0123456789abcdef0123456789abc'

def reference_body():
    b=body();b.pop('iban');b['bank_reference']=REFERENCE;return b

def words_rule(**kw):
    return dict(ID='words-rule-1',Account=ACCOUNT,Words=WORDS,**kw)

def test_reference_evidence_and_camt_truncation():
    assert m.validate(reference_body(),reference_mode=True)==reference_body()
    assert m.camt_words(REFERENCE)==WORDS
    assert len(WORDS)==35
    with pytest.raises(ValueError):m.validate(reference_body())
    with pytest.raises(ValueError):m.validate(body(),reference_mode=True)

@pytest.mark.parametrize('value',[WORDS,REFERENCE+'a','bacs',REFERENCE+' '+REFERENCE,'bosci_'+'g'*32,'',None])
def test_bad_reference_rejected(value):
    b=reference_body();b['bank_reference']=value
    with pytest.raises(ValueError):m.validate(b,reference_mode=True)

def test_reference_create_and_verify():
    db=DB();a=api([[],[words_rule()]])
    asyncio.run(m.process(db,a,'e',reference_body(),'pending'))
    a.create_words.assert_awaited_once_with(ACCOUNT,WORDS)
    a.create.assert_not_awaited()
    assert "state='done'" in db.calls[-1][0]

def test_reference_reuses_manual_test_rule():
    r=words_rule();r['Words']=' '+WORDS.upper()+' '
    db=DB();a=api([[r]])
    asyncio.run(m.process(db,a,'e',reference_body(),'pending'))
    a.create_words.assert_not_awaited();a.create.assert_not_awaited()
    assert "state='done'" in db.calls[-1][0]

@pytest.mark.parametrize('override',[{'Account':'other'},{'Words':WORDS+' extra'},
    {'AccountBankAccount':IBAN},{'GLAccount':'other'},{'VATCode':'1'}])
def test_reference_conflicts_not_overwritten(override):
    r={**words_rule(),**override};db=DB();a=api([[r]])
    asyncio.run(m.process(db,a,'e',reference_body(),'pending'))
    a.create_words.assert_not_awaited()
    assert "state='conflict'" in db.calls[-1][0]

@pytest.mark.parametrize('previous',['creating','uncertain'])
def test_reference_no_blind_retry(previous):
    db=DB();a=api([[]])
    asyncio.run(m.process(db,a,'e',reference_body(),previous))
    a.create_words.assert_not_awaited()
    assert "state='uncertain'" in db.calls[-1][0]

def test_reference_timeout_is_reconciled():
    db=DB();a=api([[]]);a.create_words.side_effect=TimeoutError()
    asyncio.run(m.process(db,a,'e',reference_body(),'pending'))
    assert "state='uncertain'" in db.calls[-1][0]
    a=api([[words_rule()]])
    asyncio.run(m.process(db,a,'e',reference_body(),'uncertain'))
    a.create_words.assert_not_awaited()
    assert "state='done'" in db.calls[-1][0]

def test_reference_payload_has_only_account_and_words():
    a=m.ExactAPI(None);a.request=AsyncMock(return_value={})
    asyncio.run(a.create_words(ACCOUNT,WORDS))
    a.request.assert_awaited_once_with('POST',f'{m.BASE}/api/v1/beta/{m.DIVISION}/cashflow/AllocationRule',payload={'Account':ACCOUNT,'Words':WORDS})

def test_reference_endpoint_idempotency(monkeypatch):
    from app import main
    from starlette.requests import Request
    import time
    class Store:
        def __init__(self):self.rows={};self.orders=set();self.result=None
        def transaction(self):return nullcontext()
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def execute(self,sql,args=None):
            if sql.startswith('INSERT'):
                event,order,raw,digest=args
                if event not in self.rows and order not in self.orders:
                    self.rows[event]=(digest,'pending',None,None,json.loads(raw));self.orders.add(order)
            elif sql.startswith('UPDATE jnp_woo_iban_events SET body='):
                raw,digest,event,old_digest=args
                if self.rows[event][0]==old_digest and self.rows[event][1]=='done':
                    self.rows[event]=(digest,'pending',None,None,json.loads(raw))
            elif sql.startswith('SELECT digest'):self.result=self.rows.get(args[0])
            return self
        def fetchone(self):return self.result
    db=Store();key='b'*64
    monkeypatch.setenv('WOO_IBAN_SHARED_SECRET',key)
    monkeypatch.setattr(main,'DATABASE_URL','test-db')
    monkeypatch.setattr(main,'_db_connect',lambda:db)
    def post(b,path=m.REFERENCE_PATH):
        raw=json.dumps(b).encode();stamp=str(int(time.time()))
        sig=hmac.new(key.encode(),stamp.encode()+b'.'+raw,hashlib.sha256).hexdigest()
        async def stream():return {'type':'http.request','body':raw,'more_body':False}
        request=Request({'type':'http','method':'POST','scheme':'https','server':('test',443),'path':path,
            'headers':[(b'x-mbom-timestamp',stamp.encode()),(b'x-mbom-signature',sig.encode())]},stream)
        try:return 200,asyncio.run(m.receive(request))
        except HTTPException as e:return e.status_code,e.detail
    assert post(reference_body())[0]==200
    assert post(reference_body())[1]['state']=='pending'
    assert len(db.rows)==1
    assert post({**reference_body(),'bank_reference':REFERENCE[:-1]+'4'})[0]==409
    assert post({**reference_body(),'transaction_id':'988'})[0]==409
    assert post(reference_body(),m.PATH)[0]==422
    assert post(body())[0]==422

    event=m.ADMINISTRATION+':'+body()['transaction_id']
    db.rows.clear();db.orders.clear()
    assert post(body(),m.PATH)[0]==200
    old=db.rows[event]
    db.rows[event]=(old[0],'uncertain',None,None,old[4])
    assert post(reference_body())[0]==409
    db.rows[event]=(old[0],'done',None,'iban-rule',old[4])
    assert post({**reference_body(),'amount':'999.00'})[0]==409
    assert post(reference_body())[1]['state']=='pending'
    assert db.rows[event][4]['bank_reference']==REFERENCE
    assert post(reference_body())[1]['state']=='pending'


def test_module_payment_cutoff_is_payment_date():
    b={**reference_body(),'order_date':'2026-09-30','origin':'plugin_completed'}
    assert m.validate(b,reference_mode=True)==b
    with pytest.raises(ValueError):m.validate({**b,'payment_date':'2026-09-30'},reference_mode=True)
    with pytest.raises(ValueError):m.validate({**b,'origin':'historical_paid_exact'},reference_mode=True)


@pytest.mark.parametrize('field',['transaction_id','administration_id','financial_account_id','order_id',
    'order_number','order_date','payment_date','amount','currency','payment_method'])
def test_legacy_upgrade_cannot_change_payment(field):
    assert m.can_upgrade_iban(body(),reference_body(),'done')
    changed={**reference_body(),field:'different'}
    assert not m.can_upgrade_iban(body(),changed,'done')


@pytest.mark.parametrize('state',['pending','creating','uncertain','conflict'])
def test_legacy_upgrade_requires_confirmed_write(state):
    assert not m.can_upgrade_iban(body(),reference_body(),state)


def test_status_is_read_only_and_reports_verified_rules(monkeypatch):
    from app import main
    from starlette.requests import Request
    class StatusDB:
        def transaction(self):return nullcontext()
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def execute(self,sql):
            assert sql.startswith('SELECT')
            self.rows=[('done',1)] if 'count(*)' in sql else [('event','48794',REFERENCE,'done','Confirmed','rule')]
            return self
        def fetchall(self):return self.rows
    monkeypatch.setattr(m,'authenticate',lambda headers,raw:None)
    monkeypatch.setattr(main,'DATABASE_URL','test-db')
    monkeypatch.setattr(main,'DIVISION',m.DIVISION)
    monkeypatch.setattr(main,'_db_connect',lambda:StatusDB())
    a=api([[words_rule(),{**words_rule(),'Account':'other'},{'ID':'iban','Account':ACCOUNT}]])
    monkeypatch.setattr(m,'ExactAPI',lambda app:a)
    request=Request({'type':'http','method':'POST','scheme':'https','server':('test',443),
        'path':'/api/woocommerce/allocation-rule/status','headers':[]})
    result=asyncio.run(m.check_connection(request))
    assert result['bosci_rule_count']==1 and result['queue_counts']=={'done':1}
    assert result['allocation_rule_version']==3
    a.create.assert_not_awaited();a.create_words.assert_not_awaited()
