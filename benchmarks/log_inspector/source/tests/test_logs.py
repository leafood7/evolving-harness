import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from log_inspector.core import parse_line,parse_json_line,timestamp,matches


class ParseTests(unittest.TestCase):
    def test_message_and_alias_preserved(self):
        r=parse_line('2026-10-24T09:00:00+08:00 WARN 请求 失败\n',7)
        self.assertEqual((r.level,r.message,r.line),('WARNING','请求 失败',7))

    def test_offset_comparison_and_inclusive_boundaries(self):
        r=parse_line('2026-10-24T09:00:00+08:00 INFO ok',1)
        same=timestamp('2026-10-24T01:00:00Z')
        self.assertTrue(matches(r,since=same,until=same))
        self.assertFalse(matches(r,since=timestamp('2026-10-24T01:00:01Z')))

    def test_bad_date_missing_timezone_unknown_level_rejected(self):
        for text in ['2026-02-30T10:00:00Z INFO bad','2026-10-24T09:00:00 INFO bad',
                     '2026-10-24T09:00:00Z UNKNOWN bad','traceback continuation']:
            with self.assertRaises(ValueError):parse_line(text,1)


class JsonParseTests(unittest.TestCase):
    def test_json_line_alias_and_fields(self):
        r=parse_json_line('{"timestamp":"2026-10-24T09:00:00+08:00","level":"WARN","message":"请求 失败"}',7)
        self.assertEqual((r.level,r.message,r.line),('WARNING','请求 失败',7))

    def test_json_missing_timezone_rejected(self):
        with self.assertRaises(ValueError):
            parse_json_line('{"timestamp":"2026-10-24T09:00:00","level":"INFO","message":"x"}',1)

    def test_json_bad_structure_rejected(self):
        for text in ['not json','[1,2,3]',
                     '{"timestamp":"2026-10-24T09:00:00Z","level":"INFO"}',
                     '{"timestamp":"2026-10-24T09:00:00Z","level":"UNKNOWN","message":"x"}']:
            with self.assertRaises(ValueError):parse_json_line(text,1)


class CliTests(unittest.TestCase):
    def invoke(self,text,*args,encoding='utf-8',raw=None):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'input.log'
            if raw is not None:p.write_bytes(raw)
            else:p.write_text(text,encoding=encoding)
            return subprocess.run([sys.executable,'-m','log_inspector',str(p),*args],
                capture_output=True,text=True,encoding='utf-8',env=self.env(),timeout=10)

    def env(self):
        import os
        return os.environ|{'PYTHONUTF8':'1'}

    def test_level_filter_and_json_lines(self):
        result=self.invoke('2026-10-24T01:00:00Z INFO ok\n2026-10-24T01:00:01Z ERROR 坏了\n',
                           '--level','error','--json')
        self.assertEqual(result.returncode,0)
        records=[json.loads(x) for x in result.stdout.splitlines()]
        self.assertEqual(len(records),1);self.assertEqual(records[0]['line'],2)
        self.assertEqual(records[0]['message'],'坏了')

    def test_repeated_levels_and_time_filter(self):
        text='2026-10-24T01:00:00Z INFO first\n2026-10-24T01:00:01Z WARN middle\n2026-10-24T01:00:02Z ERROR last\n'
        r=self.invoke(text,'--level','WARN','--level','ERROR','--until','2026-10-24T01:00:01Z')
        self.assertEqual(r.returncode,0);self.assertIn('WARNING middle',r.stdout);self.assertNotIn('last',r.stdout)

    def test_malformed_lines_reported_while_valid_lines_survive(self):
        r=self.invoke('broken\n\n2026-10-24T01:00:00Z INFO good\n')
        self.assertEqual(r.returncode,0);self.assertIn('good',r.stdout)
        self.assertIn('第 1 行',r.stderr);self.assertIn('格式错误 1',r.stderr)

    def test_strict_mode_fails_and_does_not_read_later_lines(self):
        r=self.invoke('broken\n2026-10-24T01:00:00Z INFO good\n','--strict')
        self.assertEqual(r.returncode,2);self.assertEqual(r.stdout,'')

    def test_empty_no_match_and_source_order(self):
        self.assertEqual(self.invoke('').returncode,0)
        r=self.invoke('2026-10-24T02:00:00Z INFO later\n2026-10-24T01:00:00Z INFO earlier\n','--json')
        self.assertEqual([json.loads(x)['message'] for x in r.stdout.splitlines()],['later','earlier'])
        r=self.invoke('2026-10-24T02:00:00Z INFO ok\n','--level','ERROR')
        self.assertEqual(r.stdout,'');self.assertEqual(r.returncode,0)

    def test_invalid_filters_and_reverse_window_fail(self):
        for args in [('--level','bad'),('--since','2026-10-24T01:00:00'),
                     ('--since','2026-10-25T01:00:00Z','--until','2026-10-24T01:00:00Z')]:
            self.assertEqual(self.invoke('',*args).returncode,2)

    def test_bom_and_explicit_encoding(self):
        text='2026-10-24T01:00:00Z INFO 中文\n'
        self.assertEqual(self.invoke(text,encoding='utf-8-sig').returncode,0)
        r=self.invoke(text,'--encoding','gb18030',encoding='gb18030')
        self.assertEqual(r.returncode,0);self.assertIn('中文',r.stdout)

    def test_jsonl_input_filter_and_output(self):
        text=('{"timestamp":"2026-10-24T01:00:00Z","level":"INFO","message":"ok"}\n'
              '{"timestamp":"2026-10-24T01:00:01Z","level":"ERROR","message":"坏了"}\n')
        r=self.invoke(text,'--input-format','jsonl','--level','error','--json')
        self.assertEqual(r.returncode,0)
        records=[json.loads(x) for x in r.stdout.splitlines()]
        self.assertEqual(len(records),1);self.assertEqual(records[0]['line'],2)
        self.assertEqual(records[0]['message'],'坏了')

    def test_jsonl_malformed_survives_and_real_line_number(self):
        text=('{"timestamp":"2026-10-24T01:00:00Z","level":"INFO","message":"a"}\n'
              'broken json\n'
              '\n'
              '{"timestamp":"2026-10-24T01:00:02Z","level":"WARN","message":"c"}\n')
        r=self.invoke(text,'--input-format','jsonl')
        self.assertEqual(r.returncode,0)
        self.assertIn('a',r.stdout);self.assertIn('WARNING c',r.stdout)
        self.assertIn('第 2 行',r.stderr);self.assertIn('格式错误 1',r.stderr)

    def test_jsonl_strict_mode_real_line(self):
        text=('{"timestamp":"2026-10-24T01:00:00Z","level":"INFO","message":"ok"}\n'
              'bad\n')
        r=self.invoke(text,'--input-format','jsonl','--strict')
        self.assertEqual(r.returncode,2)
        self.assertIn('第 2 行',r.stderr)

    def test_jsonl_missing_timezone_reported_as_error(self):
        text='{"timestamp":"2026-10-24T01:00:00","level":"INFO","message":"x"}\n'
        r=self.invoke(text,'--input-format','jsonl')
        self.assertEqual(r.returncode,0);self.assertEqual(r.stdout,'')
        self.assertIn('第 1 行',r.stderr);self.assertIn('格式错误 1',r.stderr)

    def test_default_format_stays_text(self):
        text='{"timestamp":"2026-10-24T01:00:00Z","level":"INFO","message":"x"}\n'
        r=self.invoke(text)
        self.assertEqual(r.returncode,0);self.assertEqual(r.stdout,'')
        self.assertIn('第 1 行',r.stderr)

    def test_invalid_input_format_rejected(self):
        self.assertEqual(self.invoke('','--input-format','yaml').returncode,2)

    def test_bad_encoding_diagnostic_limit_and_missing_file(self):
        self.assertEqual(self.invoke('',raw=b'\xff\xfe').returncode,2)
        self.assertEqual(self.invoke('','--encoding','not-a-codec').returncode,2)
        r=self.invoke('bad\n'*15)
        self.assertEqual(r.stderr.count('行格式错误'),10);self.assertIn('另有 5',r.stderr)
        r=subprocess.run([sys.executable,'-m','log_inspector','does-not-exist.log'],capture_output=True,timeout=10)
        self.assertEqual(r.returncode,2)

    def test_aggregate_text_groups_by_level_and_message(self):
        text=('2026-10-24T01:00:00Z INFO ok\n'
              '2026-10-24T02:00:00Z INFO ok\n'
              '2026-10-24T03:00:00Z ERROR bad\n')
        r=self.invoke(text,'--aggregate')
        self.assertEqual(r.returncode,0)
        lines=r.stdout.splitlines()
        self.assertEqual(len(lines),2)
        self.assertIn('2 INFO 2026-10-24T01:00:00+00:00 2026-10-24T02:00:00+00:00 ok',lines[0])
        self.assertIn('1 ERROR 2026-10-24T03:00:00+00:00 2026-10-24T03:00:00+00:00 bad',lines[1])

    def test_aggregate_json_output_fields(self):
        text=('2026-10-24T01:00:00Z INFO ok\n'
              '2026-10-24T02:00:00Z INFO ok\n')
        r=self.invoke(text,'--aggregate','--json')
        self.assertEqual(r.returncode,0)
        rows=[json.loads(x) for x in r.stdout.splitlines()]
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['count'],2)
        self.assertEqual(rows[0]['level'],'INFO')
        self.assertEqual(rows[0]['message'],'ok')
        self.assertEqual(rows[0]['earliest'],'2026-10-24T01:00:00+00:00')
        self.assertEqual(rows[0]['latest'],'2026-10-24T02:00:00+00:00')

    def test_aggregate_applies_level_and_time_filters(self):
        text=('2026-10-24T01:00:00Z INFO ok\n'
              '2026-10-24T02:00:00Z ERROR bad\n'
              '2026-10-24T03:00:00Z ERROR bad\n')
        r=self.invoke(text,'--aggregate','--level','ERROR','--until','2026-10-24T02:00:00Z')
        self.assertEqual(r.returncode,0)
        lines=r.stdout.splitlines()
        self.assertEqual(len(lines),1)
        self.assertIn('1 ERROR',lines[0])
        self.assertIn('bad',lines[0])

    def test_aggregate_top_sorts_by_count_desc(self):
        text=('2026-10-24T01:00:00Z INFO a\n'
              '2026-10-24T02:00:00Z ERROR b\n'
              '2026-10-24T03:00:00Z ERROR b\n'
              '2026-10-24T04:00:00Z WARNING c\n')
        r=self.invoke(text,'--aggregate','--top','2')
        self.assertEqual(r.returncode,0)
        lines=r.stdout.splitlines()
        self.assertEqual(len(lines),2)
        self.assertIn('2 ERROR',lines[0])
        self.assertIn('b',lines[0])
        self.assertIn('1 INFO',lines[1])

    def test_aggregate_top_tie_break_stable(self):
        text=('2026-10-24T01:00:00Z ERROR b\n'
              '2026-10-24T02:00:00Z INFO a\n'
              '2026-10-24T03:00:00Z WARNING c\n')
        r=self.invoke(text,'--aggregate','--top','3','--json')
        self.assertEqual(r.returncode,0)
        rows=[json.loads(x) for x in r.stdout.splitlines()]
        pairs=[(row['level'],row['message']) for row in rows]
        self.assertEqual(pairs,[('ERROR','b'),('INFO','a'),('WARNING','c')])

    def test_aggregate_jsonl_input(self):
        text=('{"timestamp":"2026-10-24T01:00:00Z","level":"INFO","message":"ok"}\n'
              '{"timestamp":"2026-10-24T02:00:00Z","level":"INFO","message":"ok"}\n')
        r=self.invoke(text,'--input-format','jsonl','--aggregate','--json')
        self.assertEqual(r.returncode,0)
        rows=[json.loads(x) for x in r.stdout.splitlines()]
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['count'],2);self.assertEqual(rows[0]['message'],'ok')

    def test_top_requires_aggregate(self):
        r=self.invoke('','--top','5')
        self.assertEqual(r.returncode,2)

    def test_top_must_be_positive(self):
        r=self.invoke('','--aggregate','--top','0')
        self.assertEqual(r.returncode,2)
