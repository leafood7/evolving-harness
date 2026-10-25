"""Offline scripted actors, real Python acceptance checks; no model credentials."""
import asyncio
import json
from pathlib import Path
import unittest
import time
from unittest.mock import patch

import test_task_eval as fixtures
from test_task_eval import Actor
from evolving_harness.eval_batch import EvaluationBatch, configuration, assert_batch_promotion_allowed
from evolving_harness.task_eval import TaskEvaluation, TaskPair
from evolving_harness.domain import digest


class MeteredActor(Actor):
    usage = {'totalTokens': 10, 'cost': {'total': 0.01}}
    async def prompt(self, prompt):
        result = await super().prompt(prompt)
        self.observe({'type': 'message_end', 'message': {'role': 'assistant', 'usage': self.usage}})
        return result


class BatchTests(unittest.TestCase):
    setUpBase = fixtures.EvaluationTests.setUp
    tearDown = fixtures.EvaluationTests.tearDown
    case = fixtures.EvaluationTests.case
    write_config = fixtures.EvaluationTests.write_config

    def setUp(self):
        self.setUpBase()
        self.runtime = patch('evolving_harness.eval_batch.runtime', return_value={'offline': 'fixture'})
        self.runtime.start(); self.addCleanup(self.runtime.stop)
        self.batch = EvaluationBatch(self.s, 'alice', 'p')
        self.batch_config = {'version': 1, 'name': '<batch>', 'baseline': self.baseline['id'],
            'candidate': self.candidate['id'], 'model': {'provider': 'fixture', 'model': 'fixture'}, 'repeats': 2,
            'limits': {'max_arm_runs': 20, 'max_seconds': 600, 'max_tokens': 1000,
                       'max_model_messages': 100, 'max_estimated_cost': 10},
            'development': [self.case('dev')], 'heldout': [self.case('sealed')]}
        self.batch_config['heldout'][0]['prompt'] += ' HELDOUT_SECRET'
        MeteredActor.usage = {'totalTokens': 10, 'cost': {'total': 0.01}}

    def prepare(self):
        (self.root / 'batch.json').write_text(json.dumps(self.batch_config), encoding='utf-8')
        return self.batch.prepare(self.root, 'batch.json', 'offline')

    def next(self, preview):
        state = preview.get('batch_preview') or preview['batch_report']['state']
        with patch('evolving_harness.skill_compare.PiRpc', MeteredActor), patch('evolving_harness.skill_compare.pi_command', return_value=['fixture']):
            return asyncio.run(self.batch.run_next(state['id'], preview['packet_hash'], 'offline', 'fixture', 'fixture'))

    def test_repeats_sealed_split_and_declared_interventions(self):
        p = self.prepare(); key = p['batch_preview']['id']
        self.assertNotIn('HELDOUT_SECRET', json.dumps(p))
        self.assertFalse((self.batch.directory(p['batch_preview']) / 'heldout').exists())
        copies = [v['workspace'] for x in p['batch_preview']['payloads'] for v in x['evaluation_pair_preview']['arms'].values()]
        self.assertEqual(len(copies), len(set(copies)))
        with self.assertRaises(ValueError): self.batch.unlock(key, p['packet_hash'])
        with self.assertRaises(ValueError): assert_batch_promotion_allowed(self.s, 'alice', 'p', self.candidate['id'])
        first = self.next(p)
        with self.assertRaises(ValueError): self.next(p)
        done = self.next(first)
        self.assertEqual(done['batch_report']['state']['status'], 'development_complete')
        self.assertNotIn('HELDOUT_SECRET', json.dumps(done))
        outer = json.dumps(self.s.events('alice', 'p'))
        self.assertNotIn('HELDOUT_SECRET', outer)
        held = self.batch.unlock(key, done['packet_hash'])
        self.assertIn('HELDOUT_SECRET', json.dumps(held))
        final = self.next(self.next(held)); report = final['batch_report']
        self.assertEqual(report['state']['status'], 'completed')
        self.assertEqual(report['state']['usage']['arm_runs'], 8)
        self.assertEqual(report['state']['usage']['tokens'], 80)
        self.assertEqual(report['recommendation'], 'keep_current')
        self.assertEqual(len(Actor.prompts), 8)
        entry = report['rows'][0]['id']
        report = self.batch.note(key, entry, 'candidate', 'correction', '<manual help>')['batch_report']
        self.assertEqual(report['summary']['development/candidate']['independent_passed'], 2)
        self.assertEqual(report['summary']['development/candidate']['passed_without_declared_help'], 1)
        document = Path(final['report_html']).read_text(encoding='utf-8')
        self.assertNotIn('<manual help>', document); self.assertIn('&lt;manual help&gt;', document)
        with self.assertRaises(ValueError): assert_batch_promotion_allowed(self.s, 'alice', 'p', self.candidate['id'])
        with self.assertRaises(ValueError): self.prepare()

    def test_run_limit_never_starts_a_partial_pair(self):
        self.batch_config['limits']['max_arm_runs'] = 3
        first = self.next(self.prepare()); last = self.next(first)
        self.assertEqual(last['batch_report']['state']['status'], 'stopped')
        self.assertEqual(last['batch_report']['state']['usage']['arm_runs'], 2)
        self.assertEqual(len(Actor.prompts), 2)
        with self.assertRaises(ValueError): assert_batch_promotion_allowed(self.s, 'alice', 'p', self.candidate['id'])

    def test_token_cost_message_and_missing_usage_stop_before_second_arm(self):
        for limit, value, usage, reason in [
            ('max_tokens', 5, {'totalTokens': 10, 'cost': {'total': 0.01}}, 'max_tokens'),
            ('max_estimated_cost', 0.001, {'totalTokens': 10, 'cost': {'total': 0.01}}, 'max_estimated_cost'),
            ('max_model_messages', 1, {'totalTokens': 10, 'cost': {'total': 0.01}}, 'max_model_messages'),
            ('max_tokens', 1000, {}, 'missing_usage')]:
            with self.subTest(limit=limit, reason=reason):
                self.batch_config['limits'] = {'max_arm_runs': 20, 'max_seconds': 600, 'max_tokens': 1000,
                                               'max_model_messages': 100, 'max_estimated_cost': 10}
                self.batch_config['limits'][limit] = value; MeteredActor.usage = usage
                result = self.next(self.prepare())['batch_report']
                self.assertEqual(result['state']['stop_reason'], reason)
                self.assertEqual(result['state']['usage']['arm_runs'], 1)
                self.assertEqual(result['recommendation'], 'insufficient_evidence')

    def test_model_runtime_plan_and_scope_are_frozen(self):
        p = self.prepare(); key = p['batch_preview']['id']
        with self.assertRaises(ValueError): asyncio.run(self.batch.run_next(key, p['packet_hash'], 'offline', 'other', 'fixture'))
        with patch('evolving_harness.eval_batch.runtime', return_value={'offline': 'changed'}):
            with self.assertRaises(ValueError): self.next(p)
        with self.assertRaises(ValueError): EvaluationBatch(self.s, 'bob', 'p').preview(key)
        plan = self.batch.directory(p['batch_preview']) / 'plan.json'
        plan.write_text('{}', encoding='utf-8')
        with self.assertRaises(ValueError): self.next(p)

    def test_managed_pair_cannot_bypass_controller_or_replay_busy_batch(self):
        p = self.prepare(); state = p['batch_preview']; child = self.batch.child(state, 'development')
        try:
            e = state['entries'][0]; pair = TaskPair(child, 'alice', 'p', e['pair_id']).preview()
            with self.assertRaises(ValueError):
                asyncio.run(TaskEvaluation(child, 'alice', 'p').run_case(e['suite_id'], e['pair_id'], pair['packet_hash'], '', 'fixture', 'fixture'))
        finally: child.close()
        state['status'] = 'running'; self.batch.save(state)
        with self.assertRaises(ValueError): self.next({'batch_preview': state, 'packet_hash': digest(state)})

    def test_validation_duplicate_tasks_and_no_sealed_help_in_outer_events(self):
        self.batch_config['repeats'] = True
        with self.assertRaises(ValueError): configuration(self.batch_config)
        self.batch_config['repeats'] = 2
        self.batch_config['heldout'][0]['prompt'] = self.batch_config['development'][0]['prompt']
        with self.assertRaises(ValueError): self.prepare()

    def test_explicit_null_token_limit_keeps_metering_without_stopping(self):
        self.batch_config['limits']['max_tokens'] = None
        self.batch_config['heldout'] = []
        MeteredActor.usage = {'totalTokens': 100000001, 'cost': {'total': 0.01}}
        result = self.next(self.next(self.prepare()))['batch_report']
        self.assertEqual(result['state']['status'], 'completed')
        self.assertEqual(result['state']['usage']['tokens'], 400000004)
        self.assertEqual(result['state']['usage']['arm_runs'], 4)

    def test_time_budget_and_crashed_lease_never_replay(self):
        from evolving_harness.eval_batch import Budget
        p = self.prepare(); state = p['batch_preview']; entry = state['entries'][0]
        control = Budget(self.batch, state, entry)
        control.started -= 601
        self.assertFalse(control.before_arm('with_skill'))
        self.assertEqual(control.stop_reason, 'time_limit')
        self.assertEqual(state['usage']['arm_runs'], 0)
        state.update(status='running', claimed_at=time.time()-331, running_entry=entry['id'])
        entry['status'] = 'running'; self.batch.save(state)
        report = self.batch.report(state['id'])
        self.assertEqual(report['batch_report']['state']['status'], 'stopped')
        self.assertEqual(report['batch_report']['state']['stop_reason'], 'expired_lease_no_retry')
        with self.assertRaises(ValueError): self.next(report)

    def test_per_arm_message_limit_continues_but_never_counts_as_completed_model(self):
        self.batch_config['heldout'] = []
        self.batch_config['limits'].update(max_model_messages=None, max_arm_model_messages=1)
        first = self.next(self.prepare())
        self.assertTrue(first['continue_allowed'])
        final = self.next(first)['batch_report']
        self.assertEqual(final['state']['usage']['arm_runs'], 4)
        self.assertEqual(final['state']['status'], 'completed')
        self.assertEqual(final['recommendation'], 'insufficient_evidence')
        for row in final['rows']:
            self.assertEqual(row['outcome'], 'inconclusive')
            for result in row['arms'].values():
                self.assertEqual(result['execution_limit'], 'arm_model_messages')
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['reference']['status'], 'passed')

    def test_local_message_counter_resets_and_global_limits_still_take_priority(self):
        self.batch_config['heldout'] = []
        self.batch_config['limits'].update(max_model_messages=None, max_arm_model_messages=2)
        final = self.next(self.next(self.prepare()))['batch_report']
        self.assertEqual(final['state']['usage']['model_messages'], 4)
        self.assertEqual(final['recommendation'], 'keep_current')
        self.batch_config['limits']['max_tokens'] = 1
        limited = self.next(self.prepare())['batch_report']
        self.assertEqual(limited['state']['stop_reason'], 'max_tokens')
        self.assertEqual(limited['state']['usage']['arm_runs'], 1)

    def test_timeout_is_labeled_as_execution_limit_with_incomplete_usage_stop(self):
        async def timeout(actor, prompt):
            raise TimeoutError('scripted timeout, not model quality')
        with patch.object(MeteredActor, 'prompt', timeout):
            report = self.next(self.prepare())['batch_report']
        self.assertEqual(report['state']['stop_reason'], 'incomplete_model_usage')
        result = next(iter(report['rows'][0]['arms'].values()))
        self.assertEqual(result['execution_limit'], 'arm_model_timeout')
        self.assertEqual(report['recommendation'], 'insufficient_evidence')

    def test_preview_distinguishes_aggregate_and_arm_budgets(self):
        self.batch_config['limits']['max_arm_model_messages'] = 50
        preview = self.prepare()['batch_preview']
        policy = preview['execution_policy']
        self.assertEqual(policy['planned_arms'], 8)
        self.assertEqual(policy['per_arm']['max_model_messages'], 50)
        self.assertEqual(policy['batch_limits']['max_model_messages'], 100)
        self.assertEqual(len(policy['warnings']), 2)
        for value in (True, 0, -1, '10'):
            self.batch_config['limits']['max_arm_model_messages'] = value
            with self.assertRaises(ValueError): configuration(self.batch_config)

    def test_regression_survives_later_repeats_and_blocks_promotion(self):
        self.batch_config['heldout'] = []
        first = self.prepare(); Actor.fail_candidate = True
        first = self.next(first); Actor.fail_candidate = False
        final = self.next(first)['batch_report']
        self.assertEqual(final['state']['status'], 'completed')
        self.assertEqual([r['outcome'] for r in final['rows']], ['worse_on_this_task', 'tie_on_this_task'])
        self.assertEqual(final['recommendation'], 'do_not_promote')
        with self.assertRaises(ValueError): assert_batch_promotion_allowed(self.s, 'alice', 'p', self.candidate['id'])


if __name__ == '__main__': unittest.main()
