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

    def set_states(self, **states):
        with open(self.events, "w", encoding="utf-8") as handle:
            for i, (task, state) in enumerate(states.items(), 1):
                handle.write(json.dumps({"seq": i, "task": task.replace("_", "-"), "to": state}) + "\n")

    def view(self):
        return wr.PlanView(self.root)

    def test_done_wakes_only_truly_unlocked_successor_owner(self):
        self.set_states(T1_1="done", T2_1="done")
        msg = bcast(1, "DONE", A, "T1-1")
        view = self.view()
        self.assertIsNone(wr.relevance(view, B, msg))  # T2-2 还在等 T3-1：未真正解锁
        self.assertIsNone(wr.relevance(view, C, msg))
        self.assertEqual(wr.relevance(view, COORD, msg), "协调人")
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


if __name__ == "__main__":
    unittest.main()
