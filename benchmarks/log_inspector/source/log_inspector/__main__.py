import argparse
import codecs
import json
from pathlib import Path
import sys

from .core import timestamp,level,parse_line,parse_json_line,matches


def aggregate(records):
    """Group Records by (level, message), preserving first-seen order."""
    groups = {}
    order = []
    for record in records:
        key = (record.level, record.message)
        if key not in groups:
            groups[key] = {'count': 0, 'earliest': record.time, 'latest': record.time}
            order.append(key)
        entry = groups[key]
        entry['count'] += 1
        if record.time < entry['earliest']:
            entry['earliest'] = record.time
        if record.time > entry['latest']:
            entry['latest'] = record.time
    return [(groups[key], key) for key in order]


def main(argv=None):
    parser=argparse.ArgumentParser(description='本地日志筛选；时间必须含时区，结果按原文件顺序输出。')
    parser.add_argument('file',type=Path,help='日志文件（默认 UTF-8，允许 BOM）')
    parser.add_argument('--level',action='append',type=level,help='筛选级别，可重复；WARN/FATAL 自动归一化')
    parser.add_argument('--since',type=timestamp,help='包含起始时间')
    parser.add_argument('--until',type=timestamp,help='包含结束时间')
    parser.add_argument('--json',action='store_true',help='输出 JSON Lines，一条日志一行')
    parser.add_argument('--strict',action='store_true',help='遇到首条格式错误立即退出（此前结果可能已输出）')
    parser.add_argument('--encoding',default='utf-8-sig',help='输入编码，例如 utf-8-sig 或 gb18030')
    parser.add_argument('--input-format',choices=('text','jsonl'),default='text',help='输入格式：text 或 jsonl（默认 text）')
    parser.add_argument('--aggregate',action='store_true',help='按“级别＋消息”聚合，输出次数与最早/最晚时间')
    parser.add_argument('--top',type=int,help='仅聚合模式使用，输出次数最多的前 N 组')
    args=parser.parse_args(argv)
    if not args.aggregate and args.top is not None:parser.error('--top 仅在 --aggregate 模式下使用')
    if args.top is not None and args.top<=0:parser.error('--top 必须为正整数')
    parse = parse_json_line if args.input_format=='jsonl' else parse_line
    if args.since and args.until and args.since>args.until:parser.error('--since 不能晚于 --until')
    try:codecs.lookup(args.encoding)
    except LookupError:parser.error('未知输入编码：'+args.encoding)
    lines=emitted=blank=bad=0
    collected=[]
    try:
        with args.file.open('r',encoding=args.encoding) as source:
            for lines,text in enumerate(source,1):
                if not text.strip():blank+=1;continue
                try:record=parse(text,lines)
                except ValueError as exc:
                    bad+=1
                    # Bound diagnostic volume; totals still include every malformed line.
                    if bad<=10:print(f'第 {lines} 行格式错误：{exc}',file=sys.stderr)
                    if args.strict:return 2
                    continue
                if matches(record,set(args.level or []),args.since,args.until):
                    emitted+=1
                    if args.aggregate:
                        collected.append(record)
                    else:
                        print(json.dumps(record.as_dict(),ensure_ascii=False) if args.json else
                              f'{record.time.isoformat()} {record.level} {record.message}')
        if args.aggregate:
            groups = aggregate(collected)
            if args.top is not None:
                groups.sort(key=lambda item: (-item[0]['count'], item[1][0], item[1][1]))
                groups = groups[:args.top]
            for entry, key in groups:
                level_name, message = key
                earliest = entry['earliest'].isoformat()
                latest = entry['latest'].isoformat()
                if args.json:
                    print(json.dumps({'level': level_name, 'message': message, 'count': entry['count'],
                                      'earliest': earliest, 'latest': latest}, ensure_ascii=False))
                else:
                    print(f'{entry["count"]} {level_name} {earliest} {latest} {message}')
        if bad>10:print(f'另有 {bad-10} 条格式错误未逐条显示。',file=sys.stderr)
        print(f'读取 {lines} 行；输出 {emitted} 条；空白 {blank} 行；格式错误 {bad} 行。',file=sys.stderr)
        return 0
    except (OSError,UnicodeError) as exc:
        print(f'读取/输出失败：{exc}',file=sys.stderr)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
