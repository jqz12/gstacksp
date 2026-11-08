"""Creator/worker/controller exit behavior using owned real fixture processes."""
import json
import os
from pathlib import Path
import signal
import subprocess
from process_helpers import run, popen
import sys
import time
import unittest

import test_driver
ROOT = test_driver.ROOT
from gsp.hosts import identity, is_alive
from gsp.runtime import read


class DriverProcessTests(unittest.TestCase):
    setUp = test_driver.DriverTests.setUp
    cli = test_driver.DriverTests.cli
    prepare = test_driver.DriverTests.prepare
    status = test_driver.DriverTests.status

    def wait_for(self, predicate, seconds=25):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(.1)
        logs = list((self.root/'.gsp/runs/work/driver').rglob('*.log')) + list((self.root/'.gsp/runs/work/driver/gates').glob('*.json'))
        self.fail('Timed out: '+json.dumps(self.raw())+'\n'+ '\n'.join(p.read_text(encoding='utf-8',errors='replace')[-2500:] for p in logs))

    def state_file(self):
        return self.root / '.gsp/runs/work/driver/state.json'

    def raw(self):
        return read(self.state_file())

    def kill_owned(self, owner):
        if owner and is_alive(owner):
            os.kill(owner['pid'], signal.SIGTERM if os.name == 'nt' else signal.SIGKILL)
            self.wait_for(lambda: not is_alive(owner), seconds=10)

    def cleanup_tree(self, owner):
        if owner and is_alive(owner) and os.name == 'nt':
            run(['taskkill','/PID',str(owner['pid']),'/T','/F'],capture_output=True,timeout=15)
        self.kill_owned(owner)

    def background(self):
        r = run([sys.executable, str(ROOT/'scripts/gsp.py'), '--project', str(self.root), 'drive-start', 'work'],
                           capture_output=True, text=True, encoding='utf-8', timeout=15)
        if r.returncode and 'DETACH_UNAVAILABLE' in r.stderr:
            self.skipTest('OS refused breakaway; foreground still available; background not certified')
        self.assertEqual(r.returncode, 0, r.stdout+r.stderr)
        result = json.loads(r.stdout)
        self.assertTrue(result['started'], result)
        owner = result['started']
        def cleanup():
            # Stop the dispatcher first so it cannot create a new child between
            # reading pending identity and terminating the controller.
            self.cleanup_tree(owner)
            pending = self.raw().get('pending')
            if pending: self.kill_owned(pending.get('worker'))
            self.kill_owned(self.raw().get('pending_check'))
            # Windows can retain inherited log handles briefly after process exit.
            time.sleep(.3)
        self.addCleanup(cleanup)
        return owner

    def active_worker(self):
        pending = self.raw().get('pending') or {}
        owner = pending.get('worker')
        return owner if owner and is_alive(owner) else None

    def active_check(self):
        owner=self.raw().get('pending_check')
        return owner if owner and is_alive(owner) else None

    def slow_check(self):
        # This test exercises a running check, not the latency of a plan job.
        (self.doc/'plan.md').write_text('# Check interruption fixture\nT1 implement add and test its behavior.\n',encoding='utf-8')
        self.cli('task-add','work','T1','--title','Add','--acceptance','Two integers are added')
        self.cli('seal-plan','work')
        self.checks.write_text(json.dumps([sys.executable,'-c','import time; time.sleep(20); print("Ran 1 test")']))
        self.prepare(timeout=30)

    def test_stop_remains_available_during_a_check(self):
        self.slow_check(); self.background()
        checker=self.wait_for(self.active_check)
        self.cli('drive-stop','work','--quote','SYNTHETIC TEST: stop active check')
        self.wait_for(lambda:self.raw()['status']=='waiting')
        self.assertEqual(self.raw()['reason'],'STOP_REQUESTED')
        self.assertFalse(is_alive(checker))
        state=self.cli('status','work')
        self.assertIsNotNone(state.get('inflight'))

    def test_dead_controller_preserves_active_check_identity(self):
        self.slow_check(); controller=self.background()
        checker=self.wait_for(self.active_check)
        self.kill_owned(controller)
        report=self.root/'recovery.md'; report.write_text('Fixture controller killed; check remains active.')
        self.assertIn('WORKER_ACTIVE',self.cli('drive-recover','work','--evidence',str(report),'--reason','Observed active check',ok=False))
        self.kill_owned(checker)
        report.write_text('Fixture controller and check confirmed exited; partial outputs inspected.')
        self.cli('drive-recover','work','--evidence',str(report),'--reason','Observed both exits')
        recovery=self.raw()['recoveries'][-1]
        self.assertIsNotNone(recovery['interrupted_check'])
        self.assertEqual(recovery['check_process'],checker)
        self.assertEqual(self.raw()['dispatches'],1)

    def test_background_continues_after_launcher_exits(self):
        self.prepare()
        self.background()  # This actual CLI creator process has already exited.
        self.wait_for(lambda: self.raw()['status'] in ('completed','waiting','blocked'), seconds=120)
        self.assertEqual(self.raw()['status'], 'completed', self.raw())
        self.assertEqual(self.cli('status','work')['status'], 'ready')

    def test_killed_worker_stops_without_redispatch(self):
        self.prepare('slow', timeout=12)
        owner = self.background()
        worker = self.wait_for(self.active_worker)
        self.kill_owned(worker)
        self.wait_for(lambda: self.raw()['status']=='waiting')
        self.assertEqual(self.raw()['reason'], 'EXECUTION_UNKNOWN')
        self.assertEqual(self.raw()['dispatches'], 1)
        self.wait_for(lambda: not is_alive(owner))

    def test_background_survives_creator_being_killed(self):
        self.prepare('slow', timeout=30)
        marker = self.root/'.gsp/creator-result.json'
        code = ('import json,time; from pathlib import Path; '
                'from gsp.driver import Driver; from gsp.runtime import Project; '
                f'r=Driver(Project(Path({str(self.root)!r})),"work").start(); '
                f'Path({str(marker)!r}).write_text(json.dumps(r)); time.sleep(30)')
        creator = popen([sys.executable,'-c',code], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        creator_owner = identity(creator.pid)
        def cleanup_creator():
            self.kill_owned(creator_owner); creator.wait(timeout=10)
        self.addCleanup(cleanup_creator)
        self.wait_for(marker.exists)
        result = json.loads(marker.read_text())
        self.assertTrue(result['started'], result)
        controller = result['started']
        def cleanup():
            self.cleanup_tree(controller)
            pending=self.raw().get('pending') or {}
            self.kill_owned(pending.get('worker'))
            self.kill_owned(self.raw().get('pending_check'))
            time.sleep(.3)
        self.addCleanup(cleanup)
        self.wait_for(self.active_worker)
        self.kill_owned(creator_owner)
        self.assertTrue(is_alive(controller))
        # Let the already running plan finish, then avoid delays in later jobs.
        (self.root/'.gsp/fixture-mode.txt').write_text('ok')
        self.wait_for(lambda:self.raw()['status'] in ('completed','waiting','blocked'),seconds=120)
        self.assertEqual(self.raw()['status'],'completed',self.raw())

    def test_dead_controller_cannot_replay_a_live_worker(self):
        self.prepare('slow', timeout=12)
        owner = self.background()
        worker = self.wait_for(self.active_worker)
        self.kill_owned(owner)
        report = self.root/'recovery.md'; report.write_text('Fixture controller killed; worker still active.')
        self.assertIn('WORKER_ACTIVE', self.cli('drive-recover','work','--evidence',str(report),'--reason','Fixture observation',ok=False))
        self.kill_owned(worker)
        report.write_text('Fixture controller and worker are now confirmed terminated; no remote actions.')
        self.cli('drive-recover','work','--evidence',str(report),'--reason','Fixture observed both exits')
        self.assertEqual(self.status()['dispatches'],1)
        (self.root/'.gsp/fixture-mode.txt').write_text('ok')
        self.cli('drive-run','work')
        self.assertEqual(self.status()['status'],'completed')

    def test_explicit_stop_terminates_host_and_requires_recovery(self):
        self.prepare('slow', timeout=12)
        self.background()
        self.wait_for(self.active_worker)
        self.cli('drive-stop','work','--quote','SYNTHETIC TEST: stop this fixture')
        self.wait_for(lambda:self.raw()['status']=='waiting')
        self.assertEqual(self.raw()['reason'],'STOP_REQUESTED')
        self.assertEqual(self.raw()['dispatches'],1)

    def test_two_controllers_do_not_share_a_project(self):
        self.prepare('slow', timeout=12)
        self.background(); self.wait_for(self.active_worker)
        self.assertIn('DRIVER_BUSY',self.cli('drive-run','work',ok=False))
        self.assertEqual(self.raw()['dispatches'],1)


if __name__ == '__main__':
    unittest.main()
