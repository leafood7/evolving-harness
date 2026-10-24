"""Grader calibration only: correct and wrong patches in disposable copies.

Never run this against a user's working project. The caller must supply a
directory carrying the calibration marker, which the source snapshot lacks.
These patches are authored fixtures, not Agent solutions or evolution evidence.
"""
import ast
from pathlib import Path

MARKER = '.grader-calibration-copy'


def replace(text, old, new):
    if text.count(old) != 1:
        raise ValueError('Frozen source contract changed: ' + old[:70])
    return text.replace(old, new, 1)


def extract(text, name):
    node = next(n for n in ast.parse(text).body if getattr(n, 'name', None) == name)
    start = min([node.lineno] + [d.lineno for d in getattr(node, 'decorator_list', [])]) - 1
    lines = text.splitlines(keepends=True)
    return ''.join(lines[start:node.end_lineno])


def apply(root, task_id, *, wrong=False):
    root = Path(root)
    if not (root / MARKER).is_file():
        raise ValueError('Calibration patches require a disposable marked directory')
    main = root / 'log_inspector/__main__.py'; core = root / 'log_inspector/core.py'
    text = main.read_text(encoding='utf-8')
    parse = '    args=parser.parse_args(argv)'
    select = '                if matches(record,set(args.level or []),args.since,args.until):'
    summary = "        print(f'读取 {lines} 行；输出 {emitted} 条；空白 {blank} 行；格式错误 {bad} 行。',file=sys.stderr)"
    if task_id == 'dev-01':
        output = 'emitted' if wrong else '(len(groups) if args.aggregate else emitted)'
        text = replace(text, summary, "        print(f'读取 {lines} 行；匹配 {emitted} 条；输出 {" + output + "} {\"组\" if args.aggregate else \"条\"}；空白 {blank} 行；格式错误 {bad} 行。',file=sys.stderr)")
    elif task_id in {'dev-02', 'hold-01'}:
        flag = 'contains' if task_id == 'dev-02' else 'exclude'
        text = replace(text, parse, f"    parser.add_argument('--{flag}',action='append',default=[])\n" + parse)
        if flag == 'contains':
            predicate = ('any' if wrong else 'all') + '(value in record.message for value in args.contains)'
            # The correct all([]) preserves unfiltered behavior.
        else:
            predicate = 'not ' + ('all' if wrong else 'any') + '(value in record.message for value in args.exclude)'
            if wrong:
                predicate = '(not args.exclude or ' + predicate + ')'
        text = replace(text, select, '                if not (' + predicate + '):continue\n' + select)
    elif task_id == 'dev-03':
        text = replace(text, parse, "    parser.add_argument('--min-level',type=level)\n" + parse)
        op = '<=' if wrong else '<'
        text = replace(text, select,
            "                ranking={'DEBUG':0,'INFO':1,'WARNING':2,'ERROR':3,'CRITICAL':4}\n"
            f"                if args.min_level and ranking[record.level]{op}ranking[args.min_level]:continue\n" + select)
    elif task_id == 'dev-04':
        text = replace(text, parse, "    parser.add_argument('--limit',type=int)\n" + parse)
        text = replace(text, "    parse = parse_json_line", "    if args.limit is not None and (args.limit<0 or args.aggregate):parser.error('invalid --limit')\n    parse = parse_json_line")
        text = replace(text, '    collected=[]', '    collected=[]\n    written=0')
        limit = 'args.limit+1' if wrong else 'args.limit'
        text = replace(text, '                    else:\n                        print(json.dumps',
            f'                    else:\n                        if args.limit is not None and written>={limit}:continue\n                        written+=1\n                        print(json.dumps')
        text = replace(text, summary, summary.replace('{emitted}', '{written if args.limit is not None else emitted}'))
    elif task_id == 'dev-05':
        function = extract(text, 'aggregate')
        (root / 'log_inspector/aggregation.py').write_text(function, encoding='utf-8')
        imported = 'from .aggregation import aggregate\n'
        if wrong:
            imported = 'from .aggregation import aggregate as implementation\ndef aggregate(records):\n    return implementation(records)\n'
        text = replace(text, function, imported)
    elif task_id == 'dev-06':
        source = core.read_text(encoding='utf-8'); function = extract(source, 'matches')
        (root / 'log_inspector/filters.py').write_text(function, encoding='utf-8')
        imported = 'from .filters import matches\n'
        if wrong:
            imported = 'from .filters import matches as implementation\ndef matches(*args,**kwargs):\n    return implementation(*args,**kwargs)\n'
        core.write_text(replace(source, function, imported), encoding='utf-8')
    elif task_id == 'dev-07':
        text = replace(text, parse, "    parser.add_argument('--stats-json',action='store_true')\n" + parse)
        emitted = 'emitted' if wrong else '(len(groups) if args.aggregate else emitted)'
        json_summary = "        if args.stats_json:print(json.dumps(dict(lines=lines,matched=emitted,emitted=" + emitted + ",blank=blank,malformed=bad)),file=sys.stderr)\n        else:\n    " + summary
        text = replace(text, summary, json_summary)
    elif task_id == 'hold-02':
        source = core.read_text(encoding='utf-8'); record = extract(source, 'Record')
        if wrong: record = record.replace('frozen=True', 'frozen=False')
        (root / 'log_inspector/records.py').write_text('from dataclasses import dataclass\nfrom datetime import datetime\n' + record, encoding='utf-8')
        core.write_text(replace(source, extract(source, 'Record'), 'from .records import Record\n'), encoding='utf-8')
    elif task_id == 'hold-03':
        text = replace(text, parse, "    parser.add_argument('--count-only',action='store_true')\n" + parse)
        text = replace(text, '    parse = parse_json_line', "    if args.count_only and (args.aggregate or args.top is not None or args.json):parser.error('incompatible count mode')\n    parse = parse_json_line")
        text = replace(text, '                    else:\n                        print(json.dumps', '                    else:\n                        if args.count_only:continue\n                        print(json.dumps')
        count = 'emitted+1' if wrong else 'emitted'
        text = replace(text, '        if args.aggregate:\n            groups', f'        if args.count_only:print({count})\n        if args.aggregate:\n            groups')
    else:
        raise ValueError('Unknown calibration task')
    main.write_text(text, encoding='utf-8')
