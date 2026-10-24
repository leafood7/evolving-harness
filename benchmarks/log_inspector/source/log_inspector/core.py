import json
from dataclasses import dataclass
from datetime import datetime
import re

LEVELS = {'DEBUG','INFO','WARNING','ERROR','CRITICAL'}
ALIASES = {'WARN':'WARNING','FATAL':'CRITICAL'}
LINE = re.compile(r'^(\S+)\s+([A-Za-z]+)\s+(.+)$')


def timestamp(value: str) -> datetime:
    # Restrict the public format to an ISO timestamp rather than silently accepting a date.
    if 'T' not in value:
        raise ValueError('时间必须为含时区的 ISO 8601 时间，例如 2026-10-24T09:00:00+08:00')
    try:
        parsed = datetime.fromisoformat(value[:-1]+'+00:00' if value.endswith('Z') else value)
    except ValueError as exc:
        raise ValueError('无效的 ISO 8601 时间：'+value) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('时间缺少时区：'+value)
    return parsed


def level(value: str) -> str:
    normalized = ALIASES.get(value.upper(),value.upper())
    if normalized not in LEVELS:
        raise ValueError('未知日志级别：'+value)
    return normalized


@dataclass(frozen=True)
class Record:
    time: datetime
    level: str
    message: str
    line: int

    def as_dict(self):
        return {'timestamp':self.time.isoformat(),'level':self.level,'message':self.message,'line':self.line}


def parse_line(text: str, number: int) -> Record:
    match = LINE.fullmatch(text.rstrip('\r\n'))
    if not match:
        raise ValueError('格式应为：时间 级别 消息')
    return Record(timestamp(match[1]),level(match[2]),match[3],number)


def parse_json_line(text: str, number: int) -> Record:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError('JSON 解析失败：'+str(exc)) from exc
    if not isinstance(payload, dict):
        raise ValueError('JSON 行必须为对象，包含 timestamp、level、message')
    missing = [key for key in ('timestamp','level','message') if key not in payload]
    if missing:
        raise ValueError('缺少必需的字段：'+', '.join(missing))
    try:
        return Record(timestamp(str(payload['timestamp'])),level(str(payload['level'])),str(payload['message']),number)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc


def matches(record, levels=None, since=None, until=None):
    return ((not levels or record.level in levels) and (since is None or record.time>=since)
            and (until is None or record.time<=until))
