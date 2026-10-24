"""Trusted, frozen executable contracts. No calls to a model or real log service.

Each generated script runs in the evaluated copy. Keep this module outside it.
These checks are pragmatic contracts, not an adversarial security boundary.
"""
HEADER = r'''
import os,sys,json,tempfile,subprocess,re,inspect
from pathlib import Path
sys.path.insert(0,os.getcwd())
ENV=os.environ.copy(); ENV.pop('PYTHONPATH',None); ENV['PYTHONUTF8']='1'
TEXT='2026-10-24T01:00:00Z INFO alpha beta\n2026-10-24T01:00:01Z INFO alpha beta\n2026-10-24T01:00:02Z ERROR beta\n'
def jsonl(text):
    result=[]
    for line in text.splitlines():
        t,l,m=line.split(' ',2);result.append(json.dumps(dict(timestamp=t,level=l,message=m),ensure_ascii=False))
    return '\n'.join(result)+'\n'
def call(text,*args,fmt='text'):
    with tempfile.TemporaryDirectory() as directory:
        path=Path(directory)/'synthetic.log';path.write_text(text,encoding='utf-8')
        r=subprocess.run([sys.executable,'-m','log_inspector',str(path),'--input-format',fmt,*args],
            cwd=os.getcwd(),env=ENV,capture_output=True,text=True,encoding='utf-8',timeout=5)
    return r
def good(r):
    assert r.returncode==0,(r.returncode,r.stderr);return r
def rows(r):return [json.loads(line) for line in good(r).stdout.splitlines()]
'''

PROBES = {
    'dev-01': r'''
for text,fmt in ((TEXT,'text'),(jsonl(TEXT),'jsonl')):
    r=good(call(text,'--aggregate','--json',fmt=fmt));assert len(rows(r))==2
    assert re.search(r'匹配\s*3\s*条',r.stderr),r.stderr
    assert re.search(r'输出\s*2\s*组',r.stderr),r.stderr
    r=good(call(text,'--aggregate','--top','1',fmt=fmt))
    assert len(r.stdout.splitlines())==1 and re.search(r'输出\s*1\s*组',r.stderr)
    r=good(call(text,'--aggregate','--level','CRITICAL',fmt=fmt))
    assert r.stdout=='' and re.search(r'匹配\s*0\s*条',r.stderr) and re.search(r'输出\s*0\s*组',r.stderr)
r=good(call(TEXT));assert len(r.stdout.splitlines())==3 and '输出 3 条' in r.stderr
''',
    'dev-02': r'''
for text,fmt in ((TEXT,'text'),(jsonl(TEXT),'jsonl')):
    r=rows(call(text,'--contains','alpha','--contains','beta','--json',fmt=fmt));assert len(r)==2
    assert all(x['message']=='alpha beta' for x in r)
    assert rows(call(text,'--contains','ALPHA','--json',fmt=fmt))==[]
    assert rows(call(text,'--contains','alpha','--level','ERROR','--json',fmt=fmt))==[]
    groups=rows(call(text,'--contains','alpha','--aggregate','--json',fmt=fmt))
    assert len(groups)==1 and groups[0]['count']==2
    assert len(rows(call(text,'--json',fmt=fmt)))==3
r=rows(call('2026-10-24T01:00:00Z INFO literal .*\n2026-10-24T01:00:01Z INFO plain\n','--contains','.*','--json'))
assert len(r)==1 and r[0]['message']=='literal .*'
''',
    'dev-03': r'''
levels=('DEBUG','INFO','WARN','ERROR','FATAL')
text=''.join('2026-10-24T01:00:0%dZ %s x\n'%(i,l) for i,l in enumerate(levels))
for data,fmt in ((text,'text'),(jsonl(text),'jsonl')):
    r=rows(call(data,'--min-level','ERROR','--json',fmt=fmt));assert [x['level'] for x in r]==['ERROR','CRITICAL']
    r=rows(call(data,'--min-level','WARN','--json',fmt=fmt));assert [x['level'] for x in r]==['WARNING','ERROR','CRITICAL']
    r=rows(call(data,'--min-level','ERROR','--level','FATAL','--json',fmt=fmt));assert [x['level'] for x in r]==['CRITICAL']
    r=rows(call(data,'--min-level','FATAL','--aggregate','--json',fmt=fmt));assert len(r)==1 and r[0]['level']=='CRITICAL'
    assert rows(call(data,'--min-level','CRITICAL','--level','INFO','--json',fmt=fmt))==[]
assert call(TEXT,'--min-level','invalid').returncode==2
''',
    'dev-04': r'''
for text,fmt in ((TEXT,'text'),(jsonl(TEXT),'jsonl')):
    r=rows(call(text,'--limit','2','--json',fmt=fmt));assert len(r)==2 and [x['line'] for x in r]==[1,2]
    assert rows(call(text,'--limit','0','--json',fmt=fmt))==[]
    r=rows(call(text,'--limit','1','--level','ERROR','--json',fmt=fmt));assert len(r)==1 and r[0]['level']=='ERROR'
    assert len(rows(call(text,'--json',fmt=fmt)))==3
    assert call(text,'--limit','-1',fmt=fmt).returncode==2
    assert call(text,'--limit','1','--aggregate',fmt=fmt).returncode==2
r=good(call(TEXT+'broken\n\n','--limit','1'))
assert len(r.stdout.splitlines())==1 and '读取 5 行' in r.stderr and '格式错误 1' in r.stderr and '空白 1' in r.stderr
assert call(TEXT+'broken\n','--limit','0','--strict').returncode==2
assert '输出 3 条' in good(call(TEXT,'--aggregate')).stderr
''',
    'dev-05': r'''
from log_inspector import aggregation, __main__ as cli
from log_inspector.core import parse_line
assert cli.aggregate is aggregation.aggregate
assert aggregation.aggregate.__module__=='log_inspector.aggregation'
records=[parse_line('2026-10-24T10:00:00+08:00 INFO x',1),parse_line('2026-10-24T01:00:00Z INFO x',2),parse_line('2026-10-24T03:00:00Z ERROR y',3)]
groups=aggregation.aggregate(iter(records))
assert [key for item,key in groups]==[('INFO','x'),('ERROR','y')]
item,key=groups[0];assert item['count']==2 and item['earliest'].isoformat()=='2026-10-24T01:00:00+00:00'
assert item['latest'].isoformat()=='2026-10-24T10:00:00+08:00'
assert aggregation.aggregate([])==[]
assert len(rows(call(jsonl(TEXT),'--aggregate','--top','1','--json',fmt='jsonl')))==1
''',
    'dev-06': r'''
from log_inspector import core,filters
assert core.matches is filters.matches and filters.matches.__module__=='log_inspector.filters'
r=core.parse_line('2026-10-24T09:00:00+08:00 INFO x',7)
same=core.timestamp('2026-10-24T01:00:00Z')
assert filters.matches(r) and filters.matches(r,set())
assert filters.matches(r,{'INFO'},same,same)
assert not filters.matches(r,{'ERROR'})
assert not filters.matches(r,since=core.timestamp('2026-10-24T01:00:01Z'))
assert not filters.matches(r,until=core.timestamp('2026-10-24T00:59:59Z'))
assert len(rows(call(TEXT,'--level','ERROR','--json')))==1
''',
    'dev-07': r'''
for text,fmt in ((TEXT,'text'),(jsonl(TEXT),'jsonl')):
    r=good(call(text+'broken\n\n','--stats-json',fmt=fmt));stats=json.loads(r.stderr.splitlines()[-1])
    assert stats==dict(lines=5,matched=3,emitted=3,blank=1,malformed=1),stats
    assert '第 4 行' in r.stderr and len(r.stdout.splitlines())==3
    r=good(call(text,'--stats-json','--aggregate','--top','1','--json',fmt=fmt));stats=json.loads(r.stderr.splitlines()[-1])
    assert stats['matched']==3 and stats['emitted']==1 and len(rows(r))==1
r=good(call('','--stats-json'));assert json.loads(r.stderr)==dict(lines=0,matched=0,emitted=0,blank=0,malformed=0)
assert call('broken\n','--stats-json','--strict').returncode==2
normal=good(call(TEXT));assert '读取 3 行' in normal.stderr and not normal.stderr.strip().startswith('{')
''',
    'hold-01': r'''
for text,fmt in ((TEXT,'text'),(jsonl(TEXT),'jsonl')):
    r=rows(call(text,'--exclude','alpha','--json',fmt=fmt));assert len(r)==1 and r[0]['level']=='ERROR'
    assert rows(call(text,'--exclude','alpha','--exclude','beta','--json',fmt=fmt))==[]
    assert len(rows(call(text,'--exclude','ALPHA','--json',fmt=fmt)))==3
    r=rows(call(text,'--exclude','alpha','--aggregate','--json',fmt=fmt));assert len(r)==1 and r[0]['count']==1
    assert rows(call(text,'--exclude','beta','--level','ERROR','--json',fmt=fmt))==[]
assert good(call('','--exclude','x')).stdout==''
assert len(rows(call(TEXT,'--json')))==3
''',
    'hold-02': r'''
from log_inspector import core,records
from dataclasses import FrozenInstanceError
assert core.Record is records.Record and records.Record.__module__=='log_inspector.records'
r=core.parse_line('2026-10-24T09:00:00+08:00 INFO 请求 成功',7)
assert type(r) is records.Record
assert r.as_dict()==dict(timestamp='2026-10-24T09:00:00+08:00',level='INFO',message='请求 成功',line=7)
j=core.parse_json_line(json.dumps(dict(timestamp='2026-10-24T01:00:00Z',level='WARN',message='中文')),9)
assert type(j) is records.Record and j.line==9 and j.level=='WARNING'
try:r.message='changed'
except FrozenInstanceError:pass
else:raise AssertionError('Record must remain frozen')
assert len(rows(call(jsonl(TEXT),'--json',fmt='jsonl')))==3
''',
    'hold-03': r'''
for text,fmt in ((TEXT,'text'),(jsonl(TEXT),'jsonl')):
    r=good(call(text,'--count-only',fmt=fmt));assert r.stdout=='3\n' and '读取 3 行' in r.stderr
    assert good(call(text,'--count-only','--level','ERROR',fmt=fmt)).stdout=='1\n'
    for flags in (('--aggregate',),('--top','1'),('--json',)):
        assert call(text,'--count-only',*flags,fmt=fmt).returncode==2
assert good(call('','--count-only')).stdout=='0\n'
assert len(rows(call(TEXT,'--json')))==3
''',
}


def probe_source(task_id):
    """Freeze this text in the evaluation plan; do not reference a mutable file."""
    script = HEADER + PROBES[task_id] + "\nprint('TASK_CONTRACT_OK')\n"
    if len(script) > 8000:
        raise ValueError('Probe exceeds task-eval inline argument bound')
    compile(script, '<frozen-task-contract>', 'exec')
    return script
