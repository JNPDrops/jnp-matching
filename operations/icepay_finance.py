"""Bounded read of account statements and transfers; never submits an action."""
import asyncio
import json
import os
import re
import sys

from operations import icepay_browser as b
from operations.icepay_fetch_probe import environments


async def worker():
    from playwright.async_api import async_playwright
    from operations.icepay_transactions import open_account_page, table_evidence, failure_location
    result={'account_verified':False,'views':{},'details':{}}
    try:
        credentials=b.Credentials.from_env(os.environ)
        async with async_playwright() as playwright:
            base,_=environments(os.environ)
            browser=await playwright.chromium.launch(headless=True,env=base)
            try:
                context=await browser.new_context(service_workers='block',timezone_id='Europe/Amsterdam')
                await b.protect_requests(context)
                page=await context.new_page()
                page.set_default_timeout(15000)
                login=await b.authenticate(page,credentials)
                if not login['account_verified']:
                    return result
                result['account_verified']=True
                for label in ('Statements','Transfers'):
                    if await b.unique_account_link(page,label) is None:
                        result['views'][label]={'available':False}
                        continue
                    await open_account_page(page,label)
                    await page.wait_for_load_state('networkidle',timeout=20000)
                    for select in await b.visible(page.locator('select')):
                        options=[s.strip() for s in await select.locator('option').all_inner_texts()]
                        if options==['25','50','100']:
                            await select.select_option(label='100')
                            await page.wait_for_load_state('networkidle',timeout=20000)
                            await asyncio.sleep(1)
                            break
                    result['views'][label]={'available':True,'tables':await table_evidence(page),
                        'controls':await b.inspect_controls(page)}
                for identifier in ('S11930739','S11930206'):
                    await open_account_page(page,'Statements')
                    await page.wait_for_load_state('networkidle',timeout=20000)
                    targets=await b.visible(page.get_by_text(identifier,exact=True))
                    if len(targets)!=1:
                        result['details'][identifier]={'available':False}
                        continue
                    before=page.url
                    await targets[0].click()
                    await page.wait_for_load_state('networkidle',timeout=20000)
                    await b.wait_verified_account(page)
                    body=await page.locator('body').inner_text()
                    result['details'][identifier]={'available':page.url!=before,'text':body[:30000],
                        'controls':await b.inspect_controls(page),'tables':await table_evidence(page)}
            finally:
                await browser.close()
    except Exception as error:
        result['failure']=failure_location(error)
    return result


def summarize(result):
    summary={'account_verified':result.get('account_verified') is True,'views':{}}
    for name,view in result.get('views',{}).items():
        if name not in {'Statements','Transfers'}:
            raise ValueError('unexpected_view')
        tables=[]
        for table in view.get('tables',[]):
            headers=table['headers']
            keep=[i for i,h in enumerate(headers) if re.search(r'statement|transfer|date|amount|period|status|balance|currency|created|operation',h,re.I)]
            recent=[]
            for row in table['rows'][:8]:
                if len(row['cells'])!=len(headers):
                    continue
                values={headers[i]:re.sub(r'\b[A-Z]{2}\d{2}[A-Z0-9 ]{10,34}\b','[bank account]',row['cells'][i])[:160] for i in keep}
                recent.append(values)
            tables.append({'headers':headers,'rows':len(table['rows']),'recent':recent})
        summary['views'][name]={'available':view.get('available') is True,'tables':tables}
    if 'failure' in result:
        summary['failure']=result['failure']
    summary['details']={}
    for identifier,detail in result.get('details',{}).items():
        if identifier not in {'S11930739','S11930206'}:
            raise ValueError('unexpected_statement')
        lines=[s.strip() for s in detail.get('text','').splitlines() if s.strip()]
        financial=[]
        for i,line in enumerate(lines):
            if re.search(r'total|transfer|invoice|holdback|period|balance|turnover|refund|cost|statement|date',line,re.I) and len(line)<180:
                clean=re.sub(r'\b[A-Z]{2}\d{2}[A-Z0-9 ]{10,34}\b','[bank account]',line)
                if not re.search(r'password|secret|token|@|https?://',clean,re.I):
                    financial.append(clean)
                    if i+1<len(lines) and re.fullmatch(r'[\d.,€+\-\s/():]+|[ST]\d+|[A-Za-z]{3} \d{1,2}, 2026(?: \d\d:\d\d:\d\d)?',lines[i+1]):
                        financial.append(lines[i+1])
        summary['details'][identifier]={'available':detail.get('available') is True,'financial_lines':financial[:80]}
    return summary


if __name__=='__main__' and sys.argv[1:]==['--worker']:
    print(json.dumps(asyncio.run(worker()),sort_keys=True))
