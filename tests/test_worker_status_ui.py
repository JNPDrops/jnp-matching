from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), 'Node unavailable')
class WorkerStatusTests(unittest.TestCase):
    def test_missing_expired_and_uncertain_states_cannot_look_healthy(self):
        script=Path('app/dashboard/index.html').read_text().split('<script>')[1].split('</script>')[0]
        script=script.replace('bootstrap();', "globalThis.testUI={status(data){admins['1']={name:'Test'};results['1']={agents:data};return workerPanel()}}");
        harness="""const assert=require('node:assert/strict');
        globalThis.document={getElementById(){return {value:'all'}},querySelectorAll(){return []}};
        """+script+"""
        assert.ok(testUI.status({available:false}).includes('niet beschikbaar'));
        assert.ok(testUI.status({available:true,roles:[]}).includes('geen afzonderlijk taakbezit'));
        const base={role:'routing',execution_location:'worker',desired_location:'worker',lease_live:true,unresolved_writes:0};
        assert.ok(testUI.status({available:true,roles:[base]}).includes('Actief taakbezit'));
        assert.ok(testUI.status({available:true,roles:[{...base,role:'woo-rules'}]}).includes('Woo-bankregels'));
        assert.ok(testUI.status({available:true,roles:[{...base,role:'tax'}]}).includes('Belastingregels'));
        const expired=testUI.status({available:true,roles:[{...base,lease_live:false}]});
        assert.ok(expired.includes('Geen actuele heartbeat'));assert.ok(!expired.includes('Actief taakbezit'));
        const uncertain=testUI.status({available:true,roles:[{...base,unresolved_writes:1}]});
        assert.ok(uncertain.includes('onzekere schrijfpoging'));assert.ok(!uncertain.includes('Actief taakbezit'));
        assert.ok(testUI.status({available:true,roles:[{...base,role:'reports'}]}).includes('Leesrapporten en probes'));
        const interrupted=testUI.status({available:true,roles:[{...base,jobs:{uncertain:1}}]});
        assert.ok(interrupted.includes('onderbroken opdracht'));assert.ok(!interrupted.includes('Actief taakbezit'));
        assert.ok(testUI.status({available:true,roles:[{...base,role:'<script>test</script>'}]}).includes('&lt;script&gt;'));
        """
        result=subprocess.run(['node','-e',harness],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
