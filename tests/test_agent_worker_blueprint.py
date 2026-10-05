from pathlib import Path
import re
import unittest


BLUEPRINT = Path(__file__).parents[1] / "render" / "agent-workers.yaml"


class AgentWorkerBlueprintTests(unittest.TestCase):
    def setUp(self):
        self.text = BLUEPRINT.read_text()

    def test_only_real_workers_with_manual_deploy_and_shutdown_window(self):
        self.assertEqual(self.text.count("type: worker"), 6)
        self.assertNotRegex(self.text, r"(?m)^  - type: web$")
        self.assertEqual(self.text.count("autoDeployTrigger: 'off'"), 6)
        self.assertEqual(self.text.count("maxShutdownDelaySeconds: 300"), 6)
        self.assertNotIn("autoscaling", self.text.lower())

    def test_expected_roles_have_exactly_one_service_and_fibonatix_is_not_empty(self):
        roles = re.findall(r"startCommand: python -m app\.worker --role ([a-z-]+)", self.text)
        self.assertEqual(roles, ["routing", "woo-rules", "tax", "maintenance", "icepay", "reports"])
        self.assertNotIn("--role fibonatix", self.text)
        self.assertEqual(len(set(roles)), len(roles))

    def test_secrets_are_references_not_values(self):
        self.assertNotIn("sync: false", self.text)
        self.assertNotRegex(self.text, r"(?m)^\s+- key: .*(?:SECRET|PASSWORD|TOTP|API_KEY)\s*\n\s+value:")
        self.assertGreaterEqual(self.text.count("fromService:"), 40)
        self.assertNotIn("DATABASE_URL\n        value:", self.text)

    def test_browser_roles_receive_larger_plan_and_preinstall_browser(self):
        self.assertEqual(self.text.count("plan: 1c-2g"), 2)
        self.assertEqual(self.text.count("playwright install chromium --only-shell"), 2)
        self.assertEqual(self.text.count("plan: 0.5c-512mb"), 4)


if __name__ == "__main__":
    unittest.main()

class FirstRoutingBlueprintTests(unittest.TestCase):
    def test_first_phase_creates_only_one_real_manual_worker(self):
        text = (BLUEPRINT.parent / 'routing-worker.yaml').read_text()
        self.assertEqual(text.count('type: worker'),1)
        self.assertIn('startCommand: python -m app.worker --role routing',text)
        self.assertIn("autoDeployTrigger: 'off'",text)
        self.assertIn('maxShutdownDelaySeconds: 300',text)
        self.assertIn('numInstances: 1',text)
        self.assertIn('plan: 0.5c-512mb',text)
        self.assertNotIn('databases:',text)
        self.assertNotIn('type: web\n',text)
        self.assertIn('envVarKey: DATABASE_URL',text)
        self.assertIn('envVarKey: METORIK_API_KEY',text)
        self.assertNotIn('sync: false',text)
