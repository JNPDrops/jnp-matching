"""Read-only source navigation discovery. Never imports or matches in Exact."""
import asyncio
import json
import os
import re
from urllib.parse import urlsplit
from operations import paragon_login_probe as login

JOB='paragon-source-discovery-20261003-05-v4'
PREDECESSOR='paragon-source-discovery-20261003-05-v3'


def safe_links(links):
    result=[]
    for item in links:
        p=urlsplit(item.get('href',''))
        label=item.get('label','').strip()
        if p.scheme!='https' or p.netloc!='paragon.online' or p.query or p.fragment:
            continue
        if re.fullmatch(r'(transactions?|reports?|payments?|exports?)',label,re.I):
            result.append({'label':label,'path':p.path})
    return result


async def observe(page, data):
    links=await page.locator('a[href]').evaluate_all('(es)=>es.map(e=>({label:(e.innerText||"").trim(),href:e.href}))')
    data['navigation']=safe_links(links)
    choices=[r for r in data['navigation'] if re.fullmatch('transactions?',r['label'],re.I)]
    if len(choices)!=1:
        data['state']='transaction_navigation_not_unique'; return
    data['stage']='transaction_page'
    # Navigate only to the unique same-origin read link observed above.
    await page.goto(login.ORIGIN+choices[0]['path'],wait_until='domcontentloaded')
    p=urlsplit(page.url)
    if p.scheme!='https' or p.netloc!='paragon.online':
        raise ValueError('unexpected_origin')
    await page.wait_for_function("() => [...document.querySelectorAll('input,select,button')].some(e => e.offsetWidth || e.offsetHeight)",timeout=20000)
    # Public form metadata only: no input values, cookies, tokens, row contents.
    data['path']=p.path
    data['controls']=await page.locator('input,select,button').evaluate_all('''es=>es.filter(e=>e.offsetWidth||e.offsetHeight).map(e=>({
        tag:e.tagName.toLowerCase(),type:e.getAttribute('type')||'',name:e.getAttribute('name')||'',
        id:e.id||'',label:e.tagName==='BUTTON'?(e.innerText||'').trim().slice(0,100):'',
        placeholder:e.getAttribute('placeholder')||''})).filter(e=>e.type!=='hidden'&&e.type!=='password').slice(0,80)''')
    data['headers']=await page.locator('thead th').all_inner_texts()
    data['state']='source_form_observed'


def main():
    import psycopg
    with psycopg.connect(os.environ['DATABASE_URL']) as c:
        claimed=c.execute('''INSERT INTO paragon_login_probes(probe_id,result)
            VALUES(%s,%s::jsonb) ON CONFLICT DO NOTHING RETURNING probe_id''',
            (JOB,json.dumps({'status':'started','financial_writes':False}))).fetchone()
    if not claimed:
        print(json.dumps({'job':JOB,'status':'already_claimed'})); return
    data={'predecessor':PREDECESSOR}
    async def observed(page):
        try:
            await observe(page,data)
        except Exception as exc:
            data['state']='source_observation_failed'
            data['error_type']=type(exc).__name__
            data['path']=urlsplit(page.url).path
    async def run():
        return await login.worker(observe=observed)
    result=asyncio.run(asyncio.wait_for(run(),timeout=240))
    data['login_status']=result['status']; data['login_stage']=result['stage']
    data['financial_writes']=False
    with psycopg.connect(os.environ['DATABASE_URL']) as c:
        c.execute('UPDATE paragon_login_probes SET result=%s::jsonb WHERE probe_id=%s',(json.dumps(data),JOB))
    print(json.dumps({'job':JOB,**data},sort_keys=True))


if __name__=='__main__': main()
