import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from portfolio_research.repository import ResearchRepository


class RecoveryTests(unittest.TestCase):
    def test_restart_preserves_other_live_workers_and_recovers_dead_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ResearchRepository(Path(directory) / "research.sqlite3")
            live, _ = repository.create_job("monthly", "live-worker", {})
            dead, _ = repository.create_job("monthly", "dead-worker", {})
            with repository.connect() as connection:
                connection.execute(
                    "UPDATE review_jobs SET owner_pid=123456 WHERE job_id=?", (dead,)
                )

            def probe(pid, signal):
                if pid == 123456:
                    raise ProcessLookupError

            with patch("portfolio_research.repository.os.kill", side_effect=probe):
                repository.recover_jobs()
            self.assertEqual(repository.job(live)["status"], "queued")
            self.assertEqual(repository.job(dead)["status"], "failed")
            with self.assertRaises(ValueError):
                repository.update_job(dead, "complete", output={})
