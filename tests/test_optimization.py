import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch,MagicMock

from qualitygate import launcher
from qualitygate.config import Config
from qualitygate.demo import seed
from qualitygate.engine import evaluate
from qualitygate.service import Service
from qualitygate.worker import Worker

SCRATCH=Path(__file__).resolve().parents[1]/".test-tmp"
SCRATCH.mkdir(exist_ok=True)


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=SCRATCH)
        self.s=Service(Config(Path(self.tmp.name)))
        self.demo=seed(self.s)
        self.job=self.s.enqueue({"dataset_id":self.demo["dirty_dataset_id"],"ruleset_id":self.demo["ruleset_id"]})
    def tearDown(self):self.tmp.cleanup()

    def test_database_polling_is_bounded_when_throttled(self):
        with patch.object(self.s.db,"connect",wraps=self.s.db.connect) as connect:
            Worker(self.s,checkpoint_interval=999).run_one()
            self.assertLessEqual(connect.call_count,10)
        self.assertEqual(self.s.job(self.job["id"])["summary"]["failed"],66)

    def test_final_checkpoint_catches_cancel_during_throttle_window(self):
        def cancel_after_first_checkpoint(rows,rules,references,maximum,checkpoint):
            self.assertFalse(checkpoint(0))
            self.s.cancel(self.job["id"])
            self.assertFalse(checkpoint(50))  # DB check intentionally throttled
            return evaluate(rows,rules,references,maximum,checkpoint)
        with patch("qualitygate.worker.evaluate",side_effect=cancel_after_first_checkpoint):
            Worker(self.s,checkpoint_interval=999).run_one()
        self.assertEqual(self.s.job(self.job["id"])["state"],"cancelled")
        self.assertEqual(self.s.issues(self.job["id"])["total"],0)

    def test_cancel_between_evaluation_and_commit_is_not_published(self):
        def cancel_after_result(*args):
            result=evaluate(*args)
            self.s.cancel(self.job["id"])
            return result
        with patch("qualitygate.worker.evaluate",side_effect=cancel_after_result):Worker(self.s).run_one()
        self.assertEqual(self.s.job(self.job["id"])["state"],"cancelled")
        self.assertIsNone(self.s.job(self.job["id"])["summary"])


class LauncherTests(unittest.TestCase):
    def test_health_rejects_other_services_or_stopped_worker(self):
        for body in (b'{"status":"ok"}',b'{"status":"ok","mode":"local","version":"1.0.0","worker_alive":false}',b'not json'):
            with patch.object(launcher.OPENER,"open",return_value=io.BytesIO(body)):
                self.assertFalse(launcher.healthy(8877))

    def test_success_opens_browser_only_after_health(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as td:
            child=MagicMock();child.poll.return_value=None;child.wait.return_value=0
            events=[]
            with patch.object(launcher,"DATA",Path(td)),patch.object(launcher,"available",return_value=True),patch.object(launcher.subprocess,"Popen",return_value=child),patch.object(launcher,"healthy",side_effect=lambda p:events.append("health") or True),patch.object(launcher.webbrowser,"open",side_effect=lambda url:events.append("browser")),patch("builtins.print"):
                self.assertEqual(launcher.main(),0)
            self.assertEqual(events,["health","browser"])

    def test_failed_start_never_opens_browser(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as td:
            child=MagicMock();child.poll.return_value=1
            with patch.object(launcher,"DATA",Path(td)),patch.object(launcher,"available",return_value=True),patch.object(launcher.subprocess,"Popen",return_value=child),patch.object(launcher.webbrowser,"open") as browser,patch("builtins.print"):
                self.assertEqual(launcher.main(),1)
                browser.assert_not_called()


if __name__=="__main__":unittest.main()
