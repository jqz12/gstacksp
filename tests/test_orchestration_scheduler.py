"""Pure flat scheduler and usage aggregation tests."""
import unittest

from gsp.orchestration_scheduler import aggregate_usage, next_batch


def candidate(work, task, *, writes=(), depends=(), accepted=(), status="ready",
              role="worker", writes_value=None):
    return {
        "work_uid": work,
        "task_id": task,
        "status": status,
        "depends": list(depends),
        "accepted_task_ids": list(accepted),
        "contract_digest": "contract-" + work + "-" + task,
        "writes": list(writes) if writes_value is None else writes_value,
        "role": role,
        "model_alias": "group/deepseek-2",
        "effort": "max",
    }


class FlatSchedulerTests(unittest.TestCase):
    def test_two_main_works_are_fair_and_use_remaining_slots(self):
        result = next_batch(
            [
                candidate("work-a", "T01", writes=["services/a"]),
                candidate("work-a", "T02", writes=["services/b"]),
                candidate("work-b", "T01", writes=["services/c"]),
                candidate("work-b", "T02", writes=["services/d"]),
            ]
        )
        self.assertEqual(
            [(row["work_uid"], row["task_id"]) for row in result["batch"]],
            [("work-a", "T01"), ("work-b", "T01"), ("work-a", "T02")],
        )
        self.assertEqual(result["remaining_slots"], 0)
        self.assertFalse(result["persisted"])
        limited = next_batch(
            [
                candidate("work-a", "T01", writes=["a"]),
                candidate("work-b", "T01", writes=["b"]),
            ],
            occupied=2,
        )
        self.assertEqual(len(limited["batch"]), 1)
        self.assertEqual(limited["remaining_slots"], 0)

    def test_dependencies_and_nonready_statuses_are_not_redispatched(self):
        rows = [
            candidate("work-a", "T01", writes=["a"]),
            candidate(
                "work-a", "T02", writes=["b"], depends=["T01"], accepted=[]
            ),
            candidate(
                "work-b", "T01", writes=["c"], depends=["B00"],
                accepted=["B00"],
            ),
            candidate("work-c", "T01", writes=["d"], status="accepted"),
            candidate("work-c", "T02", writes=["e"], status="running"),
            candidate("work-c", "T03", writes=["f"], status="unknown"),
        ]
        result = next_batch(rows, limit=3)
        selected = {
            (row["work_uid"], row["task_id"]) for row in result["batch"]
        }
        self.assertIn(("work-a", "T01"), selected)
        self.assertIn(("work-b", "T01"), selected)
        self.assertNotIn(("work-a", "T02"), selected)
        self.assertFalse(
            selected.intersection(
                {("work-c", "T01"), ("work-c", "T02"), ("work-c", "T03")}
            )
        )
        reasons = {row["reason"] for row in result["skipped"]}
        self.assertIn("dependency-not-accepted", reasons)
        self.assertIn("status-accepted", reasons)
        self.assertIn("status-running", reasons)
        self.assertIn("status-unknown", reasons)

    def test_actual_path_overlap_and_unknown_writes_cannot_parallelize(self):
        overlap = next_batch(
            [
                candidate("work-a", "T01", writes=["services"]),
                candidate("work-b", "T01", writes=["services/api.py"]),
                candidate("work-c", "T01", writes=["docs/readme.md"]),
            ]
        )
        selected = [
            (row["work_uid"], row["task_id"]) for row in overlap["batch"]
        ]
        self.assertIn(("work-a", "T01"), selected)
        self.assertNotIn(("work-b", "T01"), selected)
        self.assertIn(("work-c", "T01"), selected)
        legacy = next_batch(
            [
                candidate("work-a", "T01", writes_value=[]),
                candidate("work-b", "T01", writes=["safe/path"]),
            ]
        )
        self.assertEqual(legacy["mode"], "legacy-sequential")
        self.assertEqual(len(legacy["batch"]), 1)
        self.assertEqual(legacy["batch"][0]["write_scope"], "unknown")

    def test_role_and_model_route_are_enforced(self):
        rows = [
            candidate("work-a", "T01", writes=["a"], role="helper"),
            candidate("work-b", "T01", writes=["b"]),
            candidate("work-c", "T01", writes=["c"]),
        ]
        rows[2]["model_alias"] = "gpt-6.1-sol"
        result = next_batch(rows)
        self.assertEqual(
            [(row["work_uid"], row["task_id"]) for row in result["batch"]],
            [("work-b", "T01")],
        )
        self.assertEqual(
            {row["reason"] for row in result["skipped"]},
            {"role-invalid", "model-route-invalid"},
        )


class UsageAggregationTests(unittest.TestCase):
    def event(self, response_id, input_tokens, output_tokens,
              cached=0, reasoning=0, **extra):
        return {
            "response_id": response_id,
            "scope": "flat",
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cached_input_tokens": cached,
                "reasoning_tokens": reasoning,
            },
            **extra,
        }

    def test_flat_usage_deduplicates_and_does_not_add_subsets(self):
        events = {
            "scope": "flat",
            "native_remaining_budget": 9,
            "events": [
                self.event("r1", 100, 20, 30, 5),
                self.event("r1", 100, 20, 30, 5),
                self.event("r2", 50, 10, 10, 2),
            ],
        }
        result = aggregate_usage(events)
        self.assertEqual(result["usage_scope"], "flat")
        self.assertEqual(result["unique_responses"], 2)
        self.assertEqual(result["input_tokens"], 150)
        self.assertEqual(result["output_tokens"], 30)
        self.assertEqual(result["cached_input_tokens"], 40)
        self.assertEqual(result["reasoning_tokens"], 7)
        self.assertEqual(result["total_tokens"], 180)
        self.assertEqual(result["remaining_budget"], 9)

    def test_same_id_changed_scope_and_parent_inclusion_are_unknown(self):
        changed = aggregate_usage(
            {
                "scope": "flat",
                "events": [
                    self.event("r1", 10, 2),
                    self.event("r1", 11, 2),
                ],
            }
        )
        self.assertEqual(changed["usage_scope"], "unknown")
        self.assertIsNone(changed["input_tokens"])
        self.assertIn("usage-same-id-changed", changed["warnings"])
        parent = aggregate_usage(
            {
                "events": [
                    self.event("parent", 100, 20, includes_descendants=True),
                    self.event("child", 40, 10),
                ]
            }
        )
        self.assertEqual(parent["usage_scope"], "unknown")
        self.assertIsNone(parent["total_tokens"])
        self.assertIn(
            "usage-parent-includes-descendants", parent["warnings"]
        )

    def test_unknown_scope_and_budget_are_not_reported_as_zero(self):
        unknown = aggregate_usage(
            [
                {
                    "response_id": "r1",
                    "usage": {"input_tokens": 10, "output_tokens": 2},
                }
            ]
        )
        self.assertEqual(unknown["usage_scope"], "unknown")
        self.assertIsNone(unknown["input_tokens"])
        self.assertIsNone(unknown["remaining_budget"])
        self.assertIn("usage-scope-unknown", unknown["warnings"])
        scoped_budget = aggregate_usage(
            {
                "scope": "flat",
                "remaining_budget": 4,
                "budget_scope": "native",
                "events": [self.event("r1", 1, 1)],
            }
        )
        self.assertEqual(scoped_budget["remaining_budget"], 4)
        unscoped_budget = aggregate_usage(
            {
                "scope": "flat",
                "remaining_budget": 4,
                "events": [self.event("r1", 1, 1)],
            }
        )
        self.assertIsNone(unscoped_budget["remaining_budget"])
        self.assertIn(
            "usage-budget-scope-unknown", unscoped_budget["warnings"]
        )


if __name__ == "__main__":
    unittest.main()
