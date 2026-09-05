import json, sys, uuid, traceback
sys.path.insert(0, "tests")
import test_team
from nbchat.core.team import TeamAgent, TeamCoordinator, _WorkerPool
import nbchat.core.client as client_mod
import nbchat.core.team as T

plan = json.dumps([
    {"title": "read the db module", "objective": "Summarize db.py"},
    {"title": "check tests", "objective": "Run pytest and report"},
])
client = test_team._MockClient([plan, "All tasks completed."])
client_mod.get_client = lambda: client

orig_exec = T.TeamCoordinator._execute_task
def exec_wrap(worker, task, deadline, deps=None):
    print(f"EXEC {task.task_id} worker={type(worker).__name__} deadline={deadline}", file=sys.stderr)
    try:
        orig_exec(worker, task, deadline, deps)
        print(f"EXEC {task.task_id} -> {task.status} :: {task.summary!r}", file=sys.stderr)
    except BaseException as e:
        print(f"EXEC {task.task_id} RAISED {e!r}", file=sys.stderr)
        traceback.print_exc()
        raise
T.TeamCoordinator._execute_task = staticmethod(exec_wrap)

orig_exec_one = _WorkerPool._execute_one
def eo_wrap(self, tid):
    try:
        orig_exec_one(self, tid)
    except BaseException:
        print("EXEC_ONE RAISED:", file=sys.stderr)
        traceback.print_exc()
_WorkerPool._execute_one = eo_wrap

agent = TeamAgent()
agent.session_id = f"team:dbg-{uuid.uuid4().hex[:8]}"
coord = TeamCoordinator(agent, worker_factory=test_team._FastWorker)
res = coord.run("Do the two things in parallel.")
print("RESULT:", res["status"], "|", res["summary"])
for t in res["tasks"]:
    print(" TASK", t["task_id"], t["status"], repr(t["summary"]))
