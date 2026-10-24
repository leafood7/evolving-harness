"""Deterministic protocol demo with real subprocess checks, zero model calls."""
import argparse
import asyncio
import json
from pathlib import Path
import shutil
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from evolving_harness.bootstrap import bootstrap
from evolving_harness.components import Components
from evolving_harness.store import Store
from evolving_harness.task_eval import TaskEvaluation


class ScriptedActor:
    def __init__(self, command, cwd, timeout, observe): self.root = Path(cwd)
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def request(self, kind, **kwargs): return {'model': {'provider': 'fixture', 'id': 'scripted'}}
    async def prompt(self, prompt):
        if (self.root / 'app.py').exists():
            (self.root / 'app.py').write_text("def normalize(value):\n    value=value.strip().upper()\n    if value not in {'INFO','ERROR'}: raise ValueError(value)\n    return value\n", encoding='utf-8')
        else:
            (self.root / 'toolkit/parsers.py').write_text('def parse(value):\n    return int(value)\n', encoding='utf-8')
            (self.root / 'toolkit/core.py').write_text('from .parsers import parse\n', encoding='utf-8')
        return {'text': 'Deterministic scripted actor; not real model performance.', 'message': {}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    output = parser.parse_args().output.resolve()
    if output.exists(): raise SystemExit('Choose a new output directory; do not overwrite history')
    output.mkdir(parents=True)
    shutil.copytree(Path(__file__).parent, output / 'cases', ignore=shutil.ignore_patterns('__pycache__', 'offline_demo.py'))
    store = Store(output / 'state.sqlite3')
    try:
        bootstrap(store, 'offline', 'evaluation-demo')
        registry = Components(store, 'offline', 'evaluation-demo')
        skill = registry.propose('skill:verify', {'kind': 'skill', 'description': 'Offline fixture only',
            'triggers': ['test'], 'steps': ['Inspect changes and verify project imports when applicable.']},
            [], backend='offline-demo', reason='Protocol demonstration, not a validated improvement')
        manifest = output / 'cases/suite.json'
        config = json.loads(manifest.read_text(encoding='utf-8')); config['candidate'] = skill['id']
        manifest.write_text(json.dumps(config), encoding='utf-8')
        evaluation = TaskEvaluation(store, 'offline', 'evaluation-demo')
        preview = evaluation.prepare(output, 'cases/suite.json')
        with patch('evolving_harness.skill_compare.PiRpc', ScriptedActor), patch('evolving_harness.skill_compare.pi_command', return_value=['offline-fixture']):
            for pair in preview['evaluation_preview']['pairs']:
                state = pair['evaluation_pair_preview']
                asyncio.run(evaluation.run_case(state['suite_id'], state['id'], pair['packet_hash'], '', 'fixture', 'scripted'))
        report = evaluation.report()
        print(json.dumps({'backend': 'deterministic-offline-fixture', 'model_calls': 0,
                          'counts': report['evaluation_report']['counts'], 'report_html': report['report_html'],
                          'report_json': report['report_json']}, ensure_ascii=False, indent=2))
    finally: store.close()


if __name__ == '__main__': main()
