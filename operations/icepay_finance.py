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
    result={'account_verified':False,'views':{}}
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
            keep=[i for i,h in enumerate(headers) if re.search(r'statement|transfer|date|amount|period|status|balance|currency|created',h,re.I)]
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
    return summary


if __name__=='__main__' and sys.argv[1:]==['--worker']:
    print(json.dumps(asyncio.run(worker()),sort_keys=True))
