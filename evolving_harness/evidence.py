"""Bounded local evidence, separate from model claims and improvement evaluation."""
from __future__ import annotations

import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

from .domain import digest, now
from .runtime import redact
from .store import Store

SUFFIXES = {'.py', '.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs', '.json', '.md', '.toml',
            '.yaml', '.yml', '.txt', '.html', '.css', '.sql', '.sh', '.ps1'}
SKIP_DIRS = {'node_modules', '__pycache__', 'dist', 'build', 'venv', 'release',
             'native-reports', 'native-sessions', 'pi-sessions'}


def snapshot(workspace: str | None) -> dict:
    if not workspace:
        return {'available': False, 'reason': 'No workspace recorded'}
    root = Path(workspace).resolve()
    if not root.is_dir():
        return {'available': False, 'reason': 'Workspace unavailable'}
    files, omitted = {}, []
    visited, total = 0, 0
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.') and d not in SKIP_DIRS
                         and not (Path(directory) / d).is_symlink()
                         and not getattr(Path(directory) / d, 'is_junction', lambda: False)())
        for name in sorted(names):
            visited += 1
            if visited > 2000:
                omitted.append('Traversal limit reached')
                dirs.clear()
                break
            path = Path(directory) / name
            if name.startswith('.') or path.suffix.lower() not in SUFFIXES:
                continue
            relative = path.relative_to(root).as_posix()
            if any(token in name.casefold() for token in ('credential', 'secret', 'password', 'auth.')):
                omitted.append(relative + ': sensitive filename')
                continue
            try:
                if path.is_symlink() or not path.resolve().is_relative_to(root):
                    omitted.append(relative + ': external link')
                    continue
                if len(files) >= 200 or path.stat().st_size > 65536 or total > 512000:
                    omitted.append(relative + ': size/file budget')
                    continue
                data = path.read_bytes()
                if len(data) > 65536:
                    omitted.append(relative + ': grew beyond budget')
                    continue
                text = data.decode('utf-8')
                total += len(data)
                files[relative] = {'sha256': hashlib.sha256(data).hexdigest(),
                                   'text': redact(text)[:4000], 'text_truncated': len(text) > 4000}
            except (OSError, UnicodeError) as exc:
                omitted.append(relative + ': ' + type(exc).__name__)
        if visited > 2000:
            break
    return {'available': True, 'workspace': str(root), 'at': now(), 'files': files,
            'manifest_hash': digest({key: value['sha256'] for key, value in files.items()}),
            'coverage': 'partial' if omitted else 'within_declared_text_scope', 'omitted': omitted[:40],
            'scope': 'Selected text/source extensions only; excludes hidden paths, dependencies and build output.'}


def changes(before: dict, after: dict) -> list[dict]:
    left, right = before.get('files', {}), after.get('files', {})
    result = []
    for path in sorted(left.keys() | right.keys()):
        a, b = left.get(path, {}), right.get(path, {})
        if a.get('sha256') == b.get('sha256'):
            continue
        diff = '\n'.join(difflib.unified_diff(a.get('text', '').splitlines(),
                    b.get('text', '').splitlines(), fromfile='before/' + path, tofile='after/' + path))[:8000]
        result.append({'path': path, 'before_hash': a.get('sha256'), 'after_hash': b.get('sha256'),
                       'diff': diff, 'diff_may_be_truncated': a.get('text_truncated', False)
                       or b.get('text_truncated', False) or len(diff) >= 8000})
    return result[:100]


class Evidence:
    def __init__(self, store: Store, owner: str, project: str):
        self.store, self.owner, self.project = store, owner, project

    def events(self, task: str) -> list[dict]:
        self.store.get_task(self.owner, self.project, task)
        return [e for e in self.store.events(self.owner, self.project) if e['task_id'] == task]

    def record(self, task: str, kind: str, data: dict) -> int:
        self.store.get_task(self.owner, self.project, task)
        return self.store.event(self.owner, self.project, kind, data, task)

    def workspace(self, task: str) -> str:
        contexts = [e for e in self.events(task) if e['kind'] == 'context_assembled']
        workspace = contexts[-1]['data'].get('workspace') if contexts else None
        if not workspace:
            raise ValueError('Task has no recorded workspace; start a new task')
        return workspace

    def capture(self, task: str, phase: str) -> int:
        try:
            workspace = self.workspace(task)
        except ValueError:
            workspace = None
        return self.record(task, 'code_snapshot', {'phase': phase, 'snapshot': snapshot(workspace)})

    def verify(self, task: str, timeout: float = 30) -> dict:
        if os.environ.get('EH_ALLOW_WRITE') != '1':
            raise ValueError('Running project tests requires restarting native with --allow-write')
        workspace = self.workspace(task)
        before = snapshot(workspace)
        command = [sys.executable, '-m', 'unittest', '-v']
        started, clock = now(), time.monotonic()
        # A temporary file avoids unbounded output pipes; retain only a bounded prefix.
        with tempfile.TemporaryFile() as output, tempfile.TemporaryDirectory(prefix='harness-bytecode-') as cache:
            environment = os.environ.copy()
            environment.update(PYTHONPYCACHEPREFIX=cache, PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1')
            process = subprocess.Popen(command, cwd=workspace, stdout=output, stderr=subprocess.STDOUT,
                                       shell=False, env=environment)
            timed_out = False
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.kill()
                process.wait()
            size = output.tell()
            output.seek(0)
            text = output.read(24000).decode('utf-8', errors='replace')
        after = snapshot(workspace)
        match = re.search(r'Ran (\d+) tests?\b', text)
        count = int(match.group(1)) if match else None
        stable = bool(before.get('manifest_hash')) and before.get('manifest_hash') == after.get('manifest_hash')
        status = ('timeout' if timed_out else 'no_tests_or_unrecognized_output' if count == 0 else
                  'failed' if process.returncode != 0 else
                  'no_tests_or_unrecognized_output' if not count else
                  'code_changed_during_test' if not stable else 'passed')
        data = {'source': 'harness-controlled-unittest', 'command': command, 'cwd': workspace,
                'started': started, 'duration_ms': round((time.monotonic() - clock) * 1000),
                'exit_code': process.returncode, 'timed_out': timed_out, 'output': redact(text),
                'output_truncated': size > 24000, 'test_count': count, 'status': status,
                'before_snapshot': before, 'after_snapshot': after,
                'snapshot_stable': stable, 'scope': 'Observed unittest execution, not independent capability evaluation.'}
        data['bytecode_policy'] = 'Fresh isolated cache prefix; do not read workspace .pyc or write bytecode.'
        seq = self.record(task, 'verification', data)
        return {'evidence_id': seq, **data}

    def bundle(self, task: str | None = None) -> dict:
        if not task:
            row = self.store.db.execute('SELECT id FROM tasks WHERE owner=? AND project=? '
                    'ORDER BY rowid DESC LIMIT 1', (self.owner, self.project)).fetchone()
            if row is None:
                raise ValueError('No tasks in this scope yet')
            task = row[0]
        stored = self.store.get_task(self.owner, self.project, task)
        events = self.events(task)
        snapshots = [e for e in events if e['kind'] == 'code_snapshot']
        verifications = [e for e in events if e['kind'] == 'verification']
        tools = [e for e in events if e['kind'] == 'native_tool']
        delta = changes(snapshots[0]['data']['snapshot'], snapshots[-1]['data']['snapshot']) if len(snapshots) >= 2 else []
        latest = verifications[-1] if verifications else None
        freshness = 'not_checked'
        if latest:
            current = snapshot(latest['data']['cwd'])
            freshness = ('unavailable' if not current.get('manifest_hash') else
                         'matches_recorded_text_scope' if current.get('manifest_hash') ==
                         latest['data']['after_snapshot'].get('manifest_hash') else 'stale')
        return {'task': stored, 'summary': {'tool_records': sum(e['data'].get('phase') != 'start' for e in tools),
                'tool_errors': sum(bool(e['data'].get('isError')) for e in tools),
                'verification_count': len(verifications),
                'latest_verification': latest['data']['status'] if latest else 'unverified',
                'verification_freshness': freshness, 'changed_files': [d['path'] for d in delta]},
                'changes': delta, 'events': [{'evidence_id': e['seq'], **e} for e in events],
                'limitations': ['Only bounded declared text/source scope is fingerprinted; not a full repository snapshot.',
                    'Tool output and model claims are untrusted data; shell success is not test success.',
                    'Tests execute project code with user permissions; this is not an OS sandbox.',
                    'Legacy task records lack arguments, snapshots or controlled verification; these cannot be reconstructed.']}
