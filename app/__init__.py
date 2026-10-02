"""Temporary bounded GET-only probe of reconciliation linkage fields."""
import os
if os.getenv("JNP_READONLY_AUDIT") == "20261002-100100-probe-v2":
    import asyncio, json, re
    import xml.etree.ElementTree as ET
    import httpx
    from . import main as m
    A = "ec2af99c-809c-40e3-9057-8a31962ae1cf"
    def log(kind, data):
        print("JNP_AUDIT30_V2 " + kind + " " + json.dumps(data, ensure_ascii=True, default=str, separators=(",", ":")), flush=True)
    def clean(row):
        return {k:v for k,v in row.items() if k in "ID,EntryID,EntryNumber,LineNumber,Date,Description,AmountDC,AmountFC,AccountCode,GLAccountCode,OurRef,YourRef,TransactionID,TransactionEntryID,TransactionType,IsFullyPaid,Status,Source,EntryDate,EndDate,InvoiceNumber,PaymentReference,PaymentInformationID,CashflowTransactionBatchCode,TransactionAmountDC,TransactionAmountFC,InvoiceDate,Journal,EndToEndID,LastPaymentDate".split(",")}
    async def run():
        try:
            if m.DIVISION != 3977752 or m.ENABLE_ORDER_RULE_WRITES or m.ENABLE_DIRECT_MATCH_WRITES:
                raise ValueError("Audit safety gate failed")
            token = await m._access_token()
            async with httpx.AsyncClient(timeout=60) as c:
                async def read(path, params):
                    await asyncio.sleep(1.5)
                    r = await c.get(m.API_V1 + "/3977752/" + path, params=params, headers={"Authorization":"Bearer "+token,"Accept":"application/json"})
                    if r.status_code >= 400:
                        log("QUERY_ERROR", {"path":path,"status":r.status_code,"text":r.text[:300]})
                        return []
                    return m._extract_results(r.json())
                banks = await read("financialtransaction/BankEntryLines", {"$filter":"Account eq guid'"+A+"' and Date ge datetime'2026-09-03T00:00:00' and Date lt datetime'2026-10-03T00:00:00'", "$select":"ID,EntryID,EntryNumber,LineNumber,Date,Description,AmountDC,AccountCode,GLAccountCode,OurRef", "$orderby":"Date,ID","$top":"3"})
                log("BANK_SAMPLE", [clean(b) for b in banks])
                if banks:
                    b = banks[0]
                    tx = await read("financialtransaction/TransactionLines", {"$filter":"EntryID eq guid'"+str(b['EntryID'])+"' and LineNumber eq "+str(b['LineNumber']),"$top":"5"})
                    log("TRANSACTION_SAMPLE", [clean(t) for t in tx])
                    cf = await read("cashflow/Receivables", {"$filter":"Account eq guid'"+A+"' and EntryNumber eq "+str(b['EntryNumber']),"$top":"3"})
                    log("CASHFLOW_BY_ENTRYNUMBER",[clean(x) for x in cf])
                    cf2 = await read("cashflow/Receivables", {"$filter":"Account eq guid'"+A+"' and TransactionEntryID eq guid'"+str(b['EntryID'])+"'","$top":"3"})
                    log("CASHFLOW_BY_ENTRYGUID",[clean(x) for x in cf2])
                    if tx:
                        cf3 = await read("cashflow/Receivables", {"$filter":"TransactionID eq guid'"+str(tx[0]['ID'])+"'", "$top":"3"})
                        log("CASHFLOW_BY_TRANSACTIONID",[clean(x) for x in cf3])
                cf = await read("cashflow/Receivables",{"$filter":"Account eq guid'"+A+"'", "$top":"1"})
                log("CASHFLOW_SAMPLE",[clean(x) for x in cf])
                r = await c.get(m.BASE_URL + "/docs/XMLDownload.aspx", params={"Topic":"MatchSets","_Division_":"3977752","Params_AccountCode":"100100"},headers={"Authorization":"Bearer "+token,"Accept":"application/xml"})
                log("XML_META",{"status":r.status_code,"length":len(r.content),"content_type":r.headers.get('content-type')})
                if r.status_code == 200:
                    try:
                        root = ET.fromstring(r.content)
                        sets = root.findall('.//MatchSet')
                        log("XML_SAMPLE",{"root":root.tag,"count":len(sets),"examples":[ET.tostring(x,encoding='unicode') for x in sets[:2]],"messages":[ET.tostring(x,encoding='unicode') for x in root.findall('.//Message')[:2]]})
                        if sets:
                            with m._db_connect() as db:
                                db.execute("CREATE TABLE IF NOT EXISTS jnp_readonly_audit_cache (cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL, stored_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
                                db.execute("INSERT INTO jnp_readonly_audit_cache(cache_key,payload) VALUES(%s,%s) ON CONFLICT(cache_key) DO UPDATE SET payload=EXCLUDED.payload, stored_at=NOW()", ('20261002-matchsets-100100',r.text))
                    except ET.ParseError:
                        log('XML_PARSE_ERROR',{'first_text':r.text[:250]})
                log("COMPLETE",{"exact_financial_writes":0})
        except Exception as exc:
            log("ERROR",{"type":type(exc).__name__,"status":getattr(exc,'status_code',None),"detail":str(getattr(exc,'detail',str(exc)))[:300]})
    @m.app.on_event('startup')
    async def start_audit_probe():
        async def delayed():
            await asyncio.sleep(8)
            await run()
        m.app.state.audit_probe = asyncio.create_task(delayed())
