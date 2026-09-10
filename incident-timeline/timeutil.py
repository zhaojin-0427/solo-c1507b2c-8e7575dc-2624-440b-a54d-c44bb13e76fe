"""时区解析与时间格式化工具。

所有时间在系统内部统一以 UTC 毫秒时间戳存储,
时区字符串仅影响展示与录入解析。
"""
import re
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

# 常见命名时区(固定偏移,分钟)
NAMED_TZ = {
    'UTC': 0, 'GMT': 0, 'Z': 0,
    'CST': 8 * 60, 'BEIJING': 8 * 60, 'PRC': 8 * 60, 'HKT': 8 * 60, 'SGT': 8 * 60,
    'JST': 9 * 60, 'KST': 9 * 60,
    'IST': 5 * 60 + 30,
    'CET': 60, 'CEST': 120, 'BST': 60, 'MSK': 180,
    'EST': -5 * 60, 'EDT': -4 * 60,
    'CST_US': -6 * 60, 'MST': -7 * 60, 'MDT': -6 * 60,
    'PST': -8 * 60, 'PDT': -7 * 60,
    'HST': -10 * 60, 'AKST': -9 * 60,
}

_OFFSET_RE = re.compile(r'^([+-])(\d{1,2}):?(\d{2})?$')


def tz_offset_minutes(tz_str):
    """时区字符串 -> 相对 UTC 的分钟偏移。支持 UTC/CST/+08:00/IANA 名。"""
    s = (tz_str or 'UTC').strip()
    if not s:
        return 0
    up = s.upper()
    if up in NAMED_TZ:
        return NAMED_TZ[up]
    m = _OFFSET_RE.match(s)
    if m:
        v = int(m.group(2)) * 60 + int(m.group(3) or 0)
        return v if m.group(1) == '+' else -v
    if ZoneInfo is not None:
        try:
            zi = ZoneInfo(s)
            off = zi.utcoffset(datetime.now())
            return int(off.total_seconds() // 60)
        except Exception:
            pass
    raise ValueError(f'无法识别的时区: {tz_str}')


def tzinfo_from(tz_str):
    return timezone(timedelta(minutes=tz_offset_minutes(tz_str)))


_TRAILING_TZ_RE = re.compile(r'\s+([+-]\d{2}:?\d{2}|Z)$')


def parse_time(s, default_tz='UTC'):
    """解析用户输入的时间字符串为 UTC 毫秒。

    支持: ISO 8601 (含空格分隔)、'YYYY-MM-DD HH:MM[:SS]'、尾部时区偏移。
    无时区信息时按 default_tz 解释。
    """
    if s is None:
        raise ValueError('时间为空')
    s = str(s).strip()
    if not s:
        raise ValueError('时间为空')
    # 提取尾部独立书写的时区偏移,如 "2026-09-10 14:03:22 +08:00"
    explicit_tz = None
    m = _TRAILING_TZ_RE.search(s)
    if m:
        explicit_tz = m.group(1)
        s = _TRAILING_TZ_RE.sub('', s).strip()
    s = s.replace('T', ' ').replace('t', ' ')
    dt = None
    for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M',
                '%Y-%m-%d', '%Y/%m/%d %H:%M:%S', '%Y/%m/%d %H:%M'):
        try:
            dt = datetime.strptime(s, fmt)
            break
        except ValueError:
            continue
    if dt is None:
        # 最后尝试 fromisoformat(兼容更多变体)
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            raise ValueError(f'无法解析时间: {s!r}')
    tz = explicit_tz or default_tz
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tzinfo_from(tz))
    else:
        # 时间串自带偏移(如 +08:00 / Z),以其为准
        off = dt.utcoffset() or timedelta(0)
        minutes = int(off.total_seconds() // 60)
        tz = _offset_str(minutes)
    return int(dt.timestamp() * 1000), tz


def _offset_str(minutes):
    sign = '+' if minutes >= 0 else '-'
    v = abs(minutes)
    return f'{sign}{v // 60:02d}:{v % 60:02d}'


def fmt_ts(ms, tz_str='UTC', with_seconds=True):
    """UTC 毫秒 -> 指定时区的可读字符串。"""
    if ms is None:
        return '—'
    off = tz_offset_minutes(tz_str)
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc) + timedelta(minutes=off)
    base = dt.strftime('%Y-%m-%d %H:%M:%S' if with_seconds else '%Y-%m-%d %H:%M')
    return f'{base} {_offset_str(off)}'


def fmt_ms(ms):
    """时长毫秒 -> 可读字符串。"""
    if ms is None:
        return '—'
    sign = '-' if ms < 0 else ''
    ms = abs(int(round(ms)))
    if ms < 1000:
        return f'{sign}{ms} 毫秒'
    sec = ms / 1000
    if sec < 60:
        return f'{sign}{sec:.1f} 秒'
    minutes = sec / 60
    if minutes < 60:
        return f'{sign}{minutes:.1f} 分钟'
    hours = minutes / 60
    if hours < 24:
        return f'{sign}{hours:.1f} 小时'
    return f'{sign}{hours / 24:.1f} 天'
