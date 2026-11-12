"""Host argv and owned-process operations. No implicit shell or permission bypass."""
import ctypes
import os
from pathlib import Path
import shutil
import signal
import subprocess

from .runtime import WorkflowError


def process_token(pid):
    """Birth identity, not PID alone. Unknown ownership is treated conservatively."""
    if not pid:
        return None
    if os.name == 'nt':
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        handle = kernel.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return 'unknown' if ctypes.get_last_error() == 5 else None
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
                return None
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return 'unknown'
            return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    stat = Path(f'/proc/{pid}/stat')
    if stat.exists():
        try:
            fields = stat.read_text().rsplit(')', 1)[1].split()
            return None if fields[0] == 'Z' else fields[19]
        except (OSError, IndexError):
            return 'unknown'
    try:
        os.kill(int(pid), 0)
        return 'unknown'
    except ProcessLookupError:
        return None
    except PermissionError:
        return 'unknown'


def is_alive(identity):
    if not identity or not identity.get('pid'):
        return False
    current = process_token(identity['pid'])
    return current is not None and (current == 'unknown' or identity.get('token') in (None, 'unknown', current))


def identity(pid):
    return {'pid': pid, 'token': process_token(pid)}


def terminate_owned(process):
    """Only the Popen child just created by this controller; never enumerate targets."""
    if process.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True, timeout=15,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill(); process.wait(timeout=10)


def child_options(detached=False):
    if os.name == 'nt':
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        if detached:
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS | subprocess.CREATE_BREAKAWAY_FROM_JOB
        return {'creationflags': flags}
    return {'start_new_session': True}


def codex_argv(job, executable=None):
    executable = executable or shutil.which('codex')
    if not executable:
        raise WorkflowError('CODEX_MISSING install or select the existing Codex executable')
    return [str(executable), '-a', 'never', '-c', 'features.multi_agent=false', 'exec',
            '--sandbox', 'read-only' if job['phase'] in ('plan', 'review') else 'workspace-write',
            '--skip-git-repo-check', '--json', '--output-schema', job['schema'],
            '--output-last-message', job['result'], '-']


def workflow_skills():
    source = Path(__file__).resolve().parents[1] / 'skills'
    return source if (source/'gsp-workflow/SKILL.md').is_file() else Path(__file__).resolve().parents[3]


def prompt(job):
    skill = {'plan': 'gsp-writing-plans', 'review': 'gsp-review'}.get(job['phase'], 'gsp-test-driven-development')
    return ('You are one worker of an explicitly authorized GstackSP unattended controller. '
            f'Read text as UTF-8. First read {workflow_skills()}/gsp-workflow/references/sp-adapter.md and the COMPLETE '
            f'{workflow_skills()}/{skill}/SKILL.md including relevant preserved resources. '
            'For failures also read gsp-systematic-debugging; do not replace methods with summaries. '
            'This worker uses single-agent execution; provider-fresh review is not an independent-provider claim. '
            'Read the approved design, plan, relevant code, and task context in the job below. '
            'Also read the project root TODOS.md (or existing TODO.md) for relevant long-term context; '
            'it is the human-readable source, not an instruction to implement every backlog item. '
            'For plan: return complete SP-style plan text plus tasks with matching IDs; do not modify files. '
            'Read artifact-fidelity.md in gsp-workflow/references and perform the plan content self-review. '
            'Include all required headings, task interfaces, steps, Run/Expected and Self-Review. '
            'Address job.plan_findings from any rejected attempt. The controller, not this read-only worker, '
            'registers tasks, runs structural checks and seals the plan; do not execute those bookkeeping commands. '
            'For execute/fix-task: implement ONLY the named task and meaningful tests. '
            'For fix-review: address the supplied blocking findings and tests. '
            'For review: read actual code and tests against design; do not modify files. '
            'The controller owns approvals, task state, checks, review registration, gates and delivery. '
            'Do not call GSP bookkeeping or edit .gsp control/workflow files; do not change design or authorized scope. '
            'Scope was already explicitly chosen in workflow state; do not ask it again. '
            'Choose justified routine technical recommendations yourself. Only genuine design, authorization or dependency blockers need input. '
            'No subagents, remote pushes, commits, publishing, messages, paid API services or global configuration changes. '
            'Never claim an unrun test passed. Return only the JSON schema result with the exact job_id.\n\n' +
            __import__('json').dumps(job, ensure_ascii=False))
