"""Repeatable batch protocol demonstration; scripted actors, zero model calls."""
import argparse
import asyncio
import json
from pathlib import Path
import shutil
from unittest.mock import patch

from offline_demo import ScriptedActor, bootstrap, Components, Store
from evolving_harness.eval_batch import EvaluationBatch


class MeteredActor(ScriptedActor):
    def __init__(self, command, cwd, timeout, observe):
        super().__init__(command, cwd, timeout, observe); self.observe = observe

    async def prompt(self, prompt):
        result = await super().prompt(prompt)
        self.observe({'type': 'message_end', 'message': {'role': 'assistant',
            'usage': {'totalTokens': 10, 'cost': {'total': 0.001}}}})
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    output = parser.parse_args().output.resolve()
    if output.exists(): raise SystemExit('Use a new directory; history is never overwritten')
    output.mkdir(parents=True)
    source = Path(__file__).parent
    for name in ('normalization', 'imports'): shutil.copytree(source / name, output / name)
    store = Store(output / 'state.sqlite3')
    try:
        bootstrap(store, 'offline', 'batch-demo')
        skill = Components(store, 'offline', 'batch-demo').propose('skill:verify',
            {'kind': 'skill', 'description': 'Scripted fixture', 'triggers': ['test'],
             'steps': ['Run project tests and report evidence.']}, [], reason='Offline protocol demo only')
        config = json.loads((source / 'batch.example.json').read_text(encoding='utf-8-sig'))
        config.update(candidate=skill['id'], model={'provider': 'fixture', 'model': 'scripted'})
        manifest = output / 'batch.json'; manifest.write_text(json.dumps(config), encoding='utf-8')
        batch = EvaluationBatch(store, 'offline', 'batch-demo')
        with patch('evolving_harness.eval_batch.runtime', return_value={'offline_fixture': True}), \
             patch('evolving_harness.skill_compare.PiRpc', MeteredActor), \
             patch('evolving_harness.skill_compare.pi_command', return_value=['offline-fixture']):
            preview = batch.prepare(output, manifest.name, '')
            key = preview['batch_preview']['id']
            while True:
                result = asyncio.run(batch.run_next(key, preview['packet_hash'], '', 'fixture', 'scripted'))
                if result['continue_allowed']: preview = result; continue
                if result['batch_report']['state']['status'] == 'development_complete':
                    preview = batch.unlock(key, result['packet_hash']); continue
                break
        print(json.dumps({'backend': 'scripted-offline-fixture', 'real_model_calls': 0,
            'scripted_usage_is_not_real_spending': True, 'status': result['batch_report']['state']['status'],
            'summary': result['batch_report']['summary'], 'report_html': result['report_html'],
            'report_json': result['report_json']}, ensure_ascii=False, indent=2))
    finally: store.close()


if __name__ == '__main__': main()
