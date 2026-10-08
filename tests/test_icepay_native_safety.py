import ast
from pathlib import Path
import unittest
from types import SimpleNamespace

SOURCE=Path(__file__).parents[1]/"operations"/"icepay_automatic.py"
tree=ast.parse(SOURCE.read_text())
wanted={"automatic_blocked","require_native_allowed","seed"}
nodes=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in wanted]

class Conn:
    def __init__(self, blocked): self.blocked=blocked; self.queries=[]
    def execute(self,sql,*args): self.queries.append(sql); return self
    def fetchone(self): return (1,) if self.blocked else None
    def __enter__(self): return self
    def __exit__(self,*args): pass

class NativeSafetyTests(unittest.TestCase):
    def ns(self,conn):
        def require(ok,reason):
            if not ok: raise ValueError(reason)
        ns={"require":require,"initialize":lambda c:None,"DIVISION":3977752}
        exec(compile(ast.Module(body=nodes,type_ignores=[]),str(SOURCE),"exec"),ns)
        return ns
    def test_blocked_seed_stops_before_queue_or_clock(self):
        c=Conn(True);ns=self.ns(c)
        ns["seed"](c,SimpleNamespace(DIVISION=3977752))
        self.assertEqual(len(c.queries),1)
    def test_blocked_direct_execution_stops(self):
        c=Conn(True);ns=self.ns(c)
        with self.assertRaisesRegex(ValueError,"native_order_mismatch_requires_review"):
            ns["require_native_allowed"](SimpleNamespace(_db_connect=lambda:c))
    def test_clean_history_allows_guard(self):
        c=Conn(False);ns=self.ns(c)
        ns["require_native_allowed"](SimpleNamespace(_db_connect=lambda:c))
    def test_resolution_must_be_explicit(self):
        c=Conn(True);self.ns(c)["automatic_blocked"](c)
        self.assertIn("order_mismatch_confirmed",c.queries[0])
        self.assertIn("order_mismatch_resolved",c.queries[0])
    def test_direct_run_and_click_both_guarded(self):
        for name in ("run","automatic_click"):
            node=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name==name)
            calls=[n.func.id for n in ast.walk(node) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name)]
            self.assertIn("require_native_allowed",calls)

if __name__=="__main__":unittest.main()
