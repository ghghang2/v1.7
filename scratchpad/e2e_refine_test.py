"""E2E smoke test for the refine hook wiring (not part of the suite)."""
import time
import nbchat.core.refine_hook as hook
import nbchat.core.db as db

db.init_db()

JSON = ('{"should_refine": true, "scope": "session", "reasoning": "ok", '
        '"edits": [{"op": "create", "content": "Always run tests before '
        'finishing", "rationale": "r"}]}')


class FakeLLM:
    def __call__(self, messages, **kw):
        class M:
            content = JSON
        class C:
            message = M()
        class R:
            choices = [C()]
        return R()


class FakeAgent:
    def __init__(self):
        self.session_id = "test-e2e"
        self.history = [("user", "fix the bug"), ("assistant", "done")]
        self._on_agent_message = lambda msg: print("VOICE:", msg)
        self.system_prompt = "sys"
        self.model_name = "m"
        self.server_url = "http://x"


class FakeRec:
    task_id = 1
    _finished = True
    status = "complete"
    request_text = "fix the bug"
    tool_calls_failed = 0
    num_tool_turns = 2


agent = FakeAgent()
rec = FakeRec()
hook._build_llm_call = lambda agent: FakeLLM()
sched = hook.on_task_finished(agent, rec)
print("scheduled:", sched)
time.sleep(0.5)
rows = db.load_lessons(session_id="test-e2e", include_global=True)
print("lessons:", [(r["id"], r["content"][:40]) for r in rows])
events = db.load_refine_events("test-e2e")
print("events:", [e["kind"] for e in events])
assert sched is True
assert any("Always run tests" in r["content"] for r in rows)
print("E2E OK")
