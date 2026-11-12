import copy
import unittest

from gsp.acceptance_migration import migrate_state


class AcceptanceMigrationTests(unittest.TestCase):
    def test_legacy_tasks_migrate_without_fabricating_acceptance(self):
        legacy = {"id": "old", "status": "ready", "tasks": [{"id": "T1", "status": "done", "evidence": "receipt-1"}],
                  "evidence": ["receipt-1"]}
        migrated = migrate_state(legacy)
        self.assertEqual(legacy["tasks"][0].keys(), {"id", "status", "evidence"})
        self.assertEqual(migrated["acceptance_protocol"], "dual-track")
        self.assertEqual(migrated["tasks"][0]["implementation"]["status"], "implementation_done")
        self.assertEqual(migrated["tasks"][0]["acceptance_track"]["status"], "acceptance_pending")
        self.assertEqual(migrated["tasks"][0]["acceptance_cards"], [])

    def test_pending_legacy_task_stays_pending(self):
        migrated = migrate_state({"id": "old", "tasks": [{"id": "T1", "status": "pending"}]})
        self.assertEqual(migrated["tasks"][0]["implementation"]["status"], "implementation_pending")
        self.assertEqual(migrated["status"], "needs-verification")


if __name__ == "__main__":
    unittest.main()
