"""Run final checks and save inspectable local results."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    results = []
    logs = []
    for argv in ([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
                 [sys.executable, 'scripts/validate_skills.py']):
        r = subprocess.run(argv, cwd=ROOT, capture_output=True, encoding='utf-8', errors='replace',
                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        output = r.stdout + r.stderr
        logs.append('$ ' + ' '.join(argv) + '\n' + output)
        results.append({'argv': argv, 'exit': r.returncode})
        print(output, end='')
    hashes = {}
    for part in ('gsp', 'scripts', 'skills', 'tests', 'hooks'):
        for file in (ROOT / part).rglob('*'):
            if file.is_file() and '__pycache__' not in file.parts and not file.name.endswith('.pyc'):
                hashes[file.relative_to(ROOT).as_posix()] = hashlib.sha256(file.read_bytes()).hexdigest()
    (ROOT / 'docs/verification-last.txt').write_text('\n'.join(logs), encoding='utf-8')
    (ROOT / 'docs/verification.json').write_text(json.dumps({'at_utc': datetime.now(timezone.utc).isoformat(),
        'checks': results, 'source_sha256': hashes, 'live_hosts': 'NOT_CHECKED',
        'paid_services': 'NOT_CHECKED'}, indent=2) + '\n', encoding='utf-8')
    return 1 if any(row['exit'] for row in results) else 0


if __name__ == '__main__':
    raise SystemExit(main())
