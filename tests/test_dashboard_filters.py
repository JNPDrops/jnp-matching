import json
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), 'Node unavailable')
class DashboardFilterTests(unittest.TestCase):
    def test_bankbook_scope_and_invoice_category_combine_without_crossing_divisions(self):
        script = Path('app/dashboard/index.html').read_text().split('<script>')[1].split('</script>')[0]
        script = script.replace('bootstrap();', '''globalThis.testUI={
          setup(rows){cases=rows;admins['1']={name:'One'};admins['2']={name:'Two'}},
          select(j,g){journal=j;group=g}, rows:filters, journals:journalsView};''')
        harness = '''const assert=require('node:assert/strict');
          const nodes=new Map();
          globalThis.document={getElementById(id){if(!nodes.has(id))nodes.set(id,{value:id==='admin'?'all':'',innerHTML:''});return nodes.get(id)},querySelectorAll(){return []}};
        ''' + script + '''
          testUI.setup([
            {id:'a',division:'1',journal:'26',journal_name:'Fibonatix',status:'open',category:'supplier_invoice_missing',question_group:'purchase_missing'},
            {id:'b',division:'2',journal:'26',journal_name:'Another bank',status:'open',category:'supplier_invoice_missing',question_group:'purchase_missing'},
            {id:'c',division:'1',journal:'26',status:'open',category:'invoice_missing',question_group:'sales_review'},
            {id:'d',division:'1',journal:null,status:'open',category:'unknown',question_group:'other'}
          ]);
          testUI.select('1:26','purchase_missing');
          assert.deepEqual(testUI.rows().map(r=>r.id),['a']);
          testUI.select('unknown','all');
          assert.deepEqual(testUI.rows().map(r=>r.id),['d']);
          testUI.journals();
          const html=nodes.get('screen').innerHTML;
          assert.ok(html.includes('Fibonatix'));assert.ok(html.includes('Another bank'));
          assert.ok(html.includes('Bankboek onbekend'));assert.ok(html.includes('Ontbrekende inkoopfacturen'));
        '''
        result = subprocess.run(['node','-e',harness],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)


if __name__=='__main__':
    unittest.main()
