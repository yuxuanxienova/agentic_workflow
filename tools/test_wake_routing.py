#!/usr/bin/env python3
"""唤醒路由 / 条件唤醒 / 摘要的单元测试：python3 -m unittest agentic_workflow/tools/test_wake_routing.py"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bus_wake  # noqa: E402
import wake_routing as wr  # noqa: E402

PLAN = """# 测试计划

协调人：`claude-test-coord`

| 车道 | agent | 任务顺序 |
|---|---|---|
| A | `codex-a` | T1-1, T1-2 |
| B | `codex-b` | T2-1, T2-2 |
| C | `codex-c` | T3-1 |

| 后（等待方） | 先（被等任务） | 原因 |
|---|---|---|
| T2-2 | T1-1, T3-1 | 共享文件 |
"""

A, B, C, COORD = "codex-a", "codex-b", "codex-c", "claude-test-coord"


def bcast(seq, mtype, sender, task=None, body=""):
    return {"seq": seq, "from": sender, "to": "*", "type": mtype, "task_id": task, "body": body}


class WakeRoutingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        pdir = self.root / "plans" / "p"
        pdir.mkdir(parents=True)
        (pdir / "plan.json").write_text(json.dumps({"name": "p", "status": "active"}), encoding="utf-8")
        (pdir / "plan.md").write_text(PLAN, encoding="utf-8")
        self.events = pdir / "events.jsonl"
        self.events.touch()
        (self.root / "bus" / "inbox").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def set_states(self, auto: str = "", **states):
        with open(self.events, "w", encoding="utf-8") as handle:
            for i, (task, state) in enumerate(states.items(), 1):
                event = {"seq": i, "task": task.replace("_", "-"), "to": state}
                if auto and state == "done":
                    event["auto"] = auto
                handle.write(json.dumps(event) + "\n")

    def view(self):
        return wr.PlanView(self.root)

    def test_done_wakes_only_truly_unlocked_successor_owner(self):
        self.set_states(T1_1="done", T2_1="done")
        msg = bcast(1, "DONE", A, "T1-1")
        view = self.view()
        self.assertIsNone(wr.relevance(view, B, msg))  # T2-2 还在等 T3-1：未真正解锁
        self.assertIsNone(wr.relevance(view, C, msg))
        self.assertEqual(wr.relevance(view, COORD, msg), wr.DEFER)  # 协调人：进验收队列
        self.set_states(T1_1="done", T2_1="done", T3_1="done")
        self.assertIn("T2-2", wr.relevance(self.view(), B, bcast(2, "DONE", C, "T3-1")))

    def test_quiet_broadcasts_and_receipts_do_not_wake(self):
        view = self.view()
        for mtype in ("CLAIM", "RELEASE", "ACCEPT", "CONFLICT"):
            self.assertIsNone(wr.relevance(view, B, bcast(1, mtype, A, "T1-1")))
        self.assertIsNone(wr.relevance(view, B, {"seq": 2, "from": A, "to": B, "type": "ACCEPT"}))
        self.assertEqual(wr.relevance(view, B, {"seq": 3, "from": A, "to": B, "type": "HANDOFF"}), "点对点")

    def test_mention_and_interrupt(self):
        view = self.view()
        self.assertEqual(wr.relevance(view, B, bcast(1, "CLAIM", A, body=f"请 {B} 注意")), "广播点名你")
        self.assertEqual(wr.relevance(view, B, bcast(2, "INTERRUPT", COORD, body="全员停")), "全员广播")

    def test_finished_lane_only_gets_direct(self):
        self.set_states(T3_1="done")
        view = self.view()
        self.assertIsNone(wr.relevance(view, C, bcast(1, "INTERRUPT", COORD, body="全员停")))
        self.assertEqual(wr.relevance(view, C, {"seq": 2, "from": COORD, "to": C, "type": "PROPOSE"}), "点对点")

    def test_blocked_wakes_prerequisite_owner(self):
        msg = bcast(1, "BLOCKED", B, "T2-2")
        view = self.view()
        self.assertIn("T1-1", wr.relevance(view, A, msg))
        self.assertIsNone(wr.relevance(view, C, bcast(2, "BLOCKED", B, "T2-1")))

    def test_duplicate_conflict_wakes_once(self):
        seen: set = set()
        msg = {"seq": 1, "from": A, "to": B, "type": "CONFLICT", "task_id": "T1-1"}
        self.assertEqual(wr.relevance(self.view(), B, msg, seen), "点对点")
        self.assertIsNone(wr.relevance(self.view(), B, dict(msg, seq=2), seen))

    def test_condition_gates_broadcasts_until_met(self):
        cond = {"tasks": ["T1-1"], "state": "done"}
        self.assertTrue(wr.gated(cond, bcast(1, "PROPOSE", COORD, body="新规"), B))
        self.assertFalse(wr.gated(cond, bcast(2, "INTERRUPT", COORD), B))
        self.assertFalse(wr.gated(cond, {"seq": 3, "from": A, "to": B, "type": "PROPOSE"}, B))
        self.assertFalse(wr.condition_met(self.view(), cond))
        self.set_states(T1_1="done")
        self.assertTrue(wr.condition_met(self.view(), cond))

    def test_wake_text_inlines_digest_and_summarises_skipped(self):
        msgs = [bcast(1, "CLAIM", A, "T1-1"), bcast(2, "INTERRUPT", COORD, body="全员停手")]
        text = bus_wake.wake_text(self.root, B, msgs, self.view(), gate=None)
        self.assertIn("#2 INTERRUPT", text)
        self.assertIn("全员停手", text)
        self.assertIn("另有 1 条", text)
        self.assertIsNone(bus_wake.wake_text(self.root, B, msgs[:1], self.view(), gate=None))

    def test_plan_wake_fires_condition_and_clears_it(self):
        wr.save_conditions(self.root, {B: {"tasks": ["T1-1"], "state": "done"}})
        _, text = bus_wake.plan_wake(self.root, B, 0, False)
        self.assertIsNone(text)  # 条件未满足、也无消息：不叫
        self.set_states(T1_1="done")
        _, text = bus_wake.plan_wake(self.root, B, 0, False)
        self.assertIn("T1-1 已 done", text)
        self.assertEqual(wr.load_conditions(self.root), {})


    # ---- 1a / 1b / 1e：协调人验收队列 ----

    def test_coordinator_skips_auto_passed_done_and_defers_others(self):
        self.set_states(auto="pass", T1_1="done")
        view = self.view()
        self.assertIsNone(wr.relevance(view, COORD, bcast(1, "DONE", A, "T1-1")))
        self.set_states(auto="fail", T1_1="done")
        self.assertEqual(wr.relevance(self.view(), COORD, {"seq": 2, "from": A, "to": COORD, "type": "DONE",
                                                            "task_id": "T1-1"}), wr.DEFER)
        self.assertEqual(wr.relevance(self.view(), COORD, bcast(3, "BLOCKED", B, "T2-2")), "协调人")

    def test_accept_queue_batches_until_three_or_stale(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        done = [dict(bcast(i, "DONE", A, f"T{i}-1"), ts=now) for i in (1, 2)]
        self.assertIsNone(bus_wake.wake_text(self.root, COORD, done, self.view(), gate=None))
        third = dict(bcast(3, "DONE", B, "T3-1"), ts=now)
        text = bus_wake.wake_text(self.root, COORD, done + [third], self.view(), gate=None)
        self.assertIn("验收队列 3 条", text)
        self.assertIn("task_state.py packet", text)
        stale = dict(bcast(4, "DONE", A, "T1-2"), ts="2020-01-01T00:00:00+00:00")
        self.assertIn("验收队列 1 条", bus_wake.wake_text(self.root, COORD, [stale], self.view(), gate=None))

    # ---- 2b：车道收工退场 ----

    def test_finished_lane_is_retired_once(self):
        self.set_states(T3_1="done")
        sent = []
        orig_targets, orig_deliver = bus_wake.wake_targets, bus_wake.deliver
        bus_wake.wake_targets = lambda root: {C: ("codex", "t"), B: ("codex", "t2"), COORD: ("claude", "s")}
        bus_wake.deliver = lambda agent, target, text: sent.append(agent) or "woken"
        try:
            bus_wake.retire_finished(self.root)
            bus_wake.retire_finished(self.root)
        finally:
            bus_wake.wake_targets, bus_wake.deliver = orig_targets, orig_deliver
        self.assertEqual(sent, [C])


class AutoAcceptTest(unittest.TestCase):
    def setUp(self):
        import subprocess
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        run = lambda *a: subprocess.run(["git", "-C", str(self.repo), *a], check=True, capture_output=True)
        run("init", "-q")
        run("config", "user.email", "t@t"); run("config", "user.name", "t")
        (self.repo / "src").mkdir(); (self.repo / "tests").mkdir()
        (self.repo / "src" / "a.gd").write_text("a"); (self.repo / "tests" / "test_a.gd").write_text("t")
        run("add", "."); run("commit", "-qm", "ok")
        self.good = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "--short", "HEAD"],
                                   capture_output=True, text=True).stdout.strip()
        (self.repo / "src" / "b.gd").write_text("b"); (self.repo / "src" / "a.gd").write_text("a2")
        run("add", "."); run("commit", "-qm", "stray")
        self.stray = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "--short", "HEAD"],
                                    capture_output=True, text=True).stdout.strip()

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, note):
        import auto_accept
        from task_state import spec_hits_path
        return auto_accept.check(self.repo, note, ["src/a.gd"], spec_hits_path)[0]

    def test_pass_with_level_commit_and_exempt_tests(self):
        self.assertEqual(self.check(f"L0: {self.good} 测试全过"), [])

    def test_fail_reasons(self):
        self.assertTrue(any("等级" in r for r in self.check(f"{self.good} 做完了")))
        self.assertTrue(any("SHA" in r for r in self.check("L0: 见总线")))
        self.assertTrue(any("计划外" in r and "src/b.gd" in r for r in self.check(f"L0: {self.stray}")))


class LaneAnalysisTest(unittest.TestCase):
    def test_suggest_agents_and_critical_path(self):
        import lanes
        files = {"T1-1": ["x.gd"], "T1-2": ["y.gd"], "T2-1": ["x.gd"], "T3-1": ["z.gd"]}
        shared = lambda a, b: sorted(set(a) & set(b))
        lane_table = {"A": {"agent": A, "tasks": ["T1-1", "T1-2"], "note": ""},
                      "B": {"agent": B, "tasks": ["T2-1"], "note": ""},
                      "C": {"agent": C, "tasks": ["T3-1"], "note": ""}}
        sync = [{"after": "T2-1", "before": ["T1-1"], "reason": "x.gd"}]
        advice = lanes.suggest_agents(files, sync, shared)
        self.assertEqual(advice["profile"], [3, 1])  # T1-1/T1-2/T3-1 可并行，T2-1 等 x.gd
        crit = lanes.critical_path(lane_table, sync, files, shared)
        self.assertEqual(crit["depth"], 2)
        kinds = {e[2] for e in crit["edges"]}
        self.assertTrue(kinds <= {"同步点", "仅车道顺序"})


if __name__ == "__main__":
    unittest.main()
