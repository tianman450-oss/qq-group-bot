from __future__ import annotations

import contextvars
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

from nonebot import get_driver
from nonebot.log import logger


@dataclass(slots=True)
class ScheduleEntry:
    user_id: str
    day_of_week: int
    start_time: str
    end_time: str
    course_name: str
    location: str = ""
    start_week: int = 1
    end_week: int = 30
    week_type: int = 0  # 0=all, 1=odd, 2=even


_current_study_reminder: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_study_reminder", default=""
)
_current_study_course: contextvars.ContextVar[Optional[ScheduleEntry]] = contextvars.ContextVar(
    "current_study_course", default=None
)


def _get_config():
    try:
        return get_driver().config
    except Exception:
        class _FallbackConfig:
            pass

        return _FallbackConfig()


def _parse_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def study_schedule_enabled() -> bool:
    config = _get_config()
    return _parse_bool(getattr(config, "study_schedule_enabled", True), True)


def study_reminder_enabled() -> bool:
    config = _get_config()
    return _parse_bool(getattr(config, "study_reminder_enabled", True), True)


def _db_path() -> Path:
    config = _get_config()
    value = str(getattr(config, "study_db_path", "data/bot_memory.db")).strip()
    return Path(value or "data/bot_memory.db")


def _tz() -> ZoneInfo:
    config = _get_config()
    tz_name = str(getattr(config, "study_timezone", "Asia/Shanghai")).strip() or "Asia/Shanghai"
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("Asia/Shanghai")


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _column_names(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row[1]) for row in rows}


def init_study_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                day_of_week INTEGER NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                course_name TEXT NOT NULL,
                location TEXT DEFAULT '',
                updated_at INTEGER NOT NULL DEFAULT 0,
                start_week INTEGER DEFAULT 1,
                end_week INTEGER DEFAULT 30,
                week_type INTEGER DEFAULT 0
            )
            """
        )
        columns = _column_names(conn, "user_schedules")
        if "updated_at" not in columns:
            conn.execute("ALTER TABLE user_schedules ADD COLUMN updated_at INTEGER NOT NULL DEFAULT 0")
        if "start_week" not in columns:
            conn.execute("ALTER TABLE user_schedules ADD COLUMN start_week INTEGER DEFAULT 1")
        if "end_week" not in columns:
            conn.execute("ALTER TABLE user_schedules ADD COLUMN end_week INTEGER DEFAULT 30")
        if "week_type" not in columns:
            conn.execute("ALTER TABLE user_schedules ADD COLUMN week_type INTEGER DEFAULT 0")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_schedule_meta (
                user_id TEXT PRIMARY KEY,
                term_start_date TEXT,
                updated_at INTEGER
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_schedules_user_id ON user_schedules(user_id)")
        conn.commit()


def normalize_hhmm(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("：", ":")
    parts = text.split(":")
    if len(parts) != 2:
        return ""
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except Exception:
        return ""
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return ""
    return f"{hour:02d}:{minute:02d}"


def _time_to_minutes(value: str) -> int:
    hour_str, minute_str = value.split(":")
    return int(hour_str) * 60 + int(minute_str)


def _parse_int(value: object, default: int) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default


def validate_schedule_entry(entry: ScheduleEntry) -> Optional[ScheduleEntry]:
    day = _parse_int(entry.day_of_week, 0)
    if day < 1 or day > 7:
        return None

    start = normalize_hhmm(entry.start_time)
    end = normalize_hhmm(entry.end_time)
    if not start or not end:
        return None
    if _time_to_minutes(start) >= _time_to_minutes(end):
        return None

    name = str(entry.course_name or "").strip()
    if not name:
        return None
    location = str(entry.location or "").strip()

    start_week = max(1, _parse_int(entry.start_week, 1))
    end_week = max(start_week, _parse_int(entry.end_week, start_week))
    week_type = _parse_int(entry.week_type, 0)
    if week_type not in {0, 1, 2}:
        week_type = 0

    return ScheduleEntry(
        user_id=str(entry.user_id),
        day_of_week=day,
        start_time=start,
        end_time=end,
        course_name=name,
        location=location,
        start_week=start_week,
        end_week=end_week,
        week_type=week_type,
    )


def _dedupe_entries(entries: List[ScheduleEntry]) -> List[ScheduleEntry]:
    seen = set()
    out: List[ScheduleEntry] = []
    for item in entries:
        key = (
            item.day_of_week,
            item.start_time,
            item.end_time,
            item.course_name,
            item.location,
            item.start_week,
            item.end_week,
            item.week_type,
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    out.sort(
        key=lambda x: (
            x.day_of_week,
            x.start_time,
            x.end_time,
            x.start_week,
            x.end_week,
            x.course_name,
        )
    )
    return out


def list_user_schedule(user_id: str) -> List[ScheduleEntry]:
    init_study_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT user_id, day_of_week, start_time, end_time, course_name, location,
                   COALESCE(start_week, 1) AS start_week,
                   COALESCE(end_week, 30) AS end_week,
                   COALESCE(week_type, 0) AS week_type
            FROM user_schedules
            WHERE user_id = ?
            ORDER BY day_of_week ASC, start_time ASC, end_time ASC, start_week ASC
            """,
            (str(user_id),),
        ).fetchall()
    out: List[ScheduleEntry] = []
    for row in rows:
        entry = validate_schedule_entry(
            ScheduleEntry(
                user_id=str(row["user_id"]),
                day_of_week=int(row["day_of_week"]),
                start_time=str(row["start_time"]),
                end_time=str(row["end_time"]),
                course_name=str(row["course_name"]),
                location=str(row["location"] or ""),
                start_week=int(row["start_week"] or 1),
                end_week=int(row["end_week"] or 30),
                week_type=int(row["week_type"] or 0),
            )
        )
        if entry:
            out.append(entry)
    return out


def has_user_schedule(user_id: str) -> bool:
    init_study_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM user_schedules WHERE user_id = ? LIMIT 1",
            (str(user_id),),
        ).fetchone()
    return bool(row)


def clear_user_schedule(user_id: str) -> int:
    init_study_db()
    with _connect() as conn:
        cur = conn.execute("DELETE FROM user_schedules WHERE user_id = ?", (str(user_id),))
        deleted = int(cur.rowcount or 0)
        conn.execute("DELETE FROM user_schedule_meta WHERE user_id = ?", (str(user_id),))
        conn.commit()
    return deleted


def _parse_date(value: str) -> Optional[date]:
    text = str(value or "").strip()
    if not text:
        return None
    for sep in (".", "/", "年", "月"):
        text = text.replace(sep, "-")
    text = text.replace("日", "")
    parts = [p for p in text.split("-") if p]
    if len(parts) != 3:
        return None
    try:
        return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except Exception:
        return None


def set_user_term_start_date(user_id: str, term_start_date: Optional[str]) -> None:
    init_study_db()
    normalized = ""
    if term_start_date:
        parsed = _parse_date(term_start_date)
        if parsed:
            normalized = parsed.isoformat()
    with _connect() as conn:
        if not normalized:
            conn.execute("DELETE FROM user_schedule_meta WHERE user_id = ?", (str(user_id),))
            conn.commit()
            return
        conn.execute(
            """
            INSERT INTO user_schedule_meta (user_id, term_start_date, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                term_start_date = excluded.term_start_date,
                updated_at = excluded.updated_at
            """,
            (str(user_id), normalized, int(time.time())),
        )
        conn.commit()


def get_user_term_start_date(user_id: str) -> Optional[date]:
    init_study_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT term_start_date FROM user_schedule_meta WHERE user_id = ?",
            (str(user_id),),
        ).fetchone()
    if not row:
        return None
    return _parse_date(str(row["term_start_date"] or ""))


def replace_user_schedule(
    user_id: str,
    entries: List[ScheduleEntry],
    term_start_date: Optional[str] = None,
) -> int:
    init_study_db()
    normalized_entries: List[ScheduleEntry] = []
    for item in entries:
        fixed = validate_schedule_entry(item)
        if not fixed:
            continue
        fixed.user_id = str(user_id)
        normalized_entries.append(fixed)
    normalized_entries = _dedupe_entries(normalized_entries)

    with _connect() as conn:
        conn.execute("DELETE FROM user_schedules WHERE user_id = ?", (str(user_id),))
        now_ts = int(time.time())
        for item in normalized_entries:
            conn.execute(
                """
                INSERT INTO user_schedules (
                    user_id, day_of_week, start_time, end_time, course_name, location,
                    updated_at, start_week, end_week, week_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(user_id),
                    int(item.day_of_week),
                    item.start_time,
                    item.end_time,
                    item.course_name,
                    item.location,
                    now_ts,
                    int(item.start_week),
                    int(item.end_week),
                    int(item.week_type),
                ),
            )
        conn.commit()

    if term_start_date is not None:
        set_user_term_start_date(str(user_id), term_start_date)
    return len(normalized_entries)


def weekday_label(day: int) -> str:
    mapping = {
        1: "周一",
        2: "周二",
        3: "周三",
        4: "周四",
        5: "周五",
        6: "周六",
        7: "周日",
    }
    return mapping.get(int(day), f"周{day}")


def _now(now: Optional[datetime] = None) -> datetime:
    if now is not None:
        if now.tzinfo is None:
            return now.replace(tzinfo=_tz())
        return now.astimezone(_tz())
    return datetime.now(tz=_tz())


def get_user_current_week(user_id: str, now: Optional[datetime] = None) -> Optional[int]:
    term_start = get_user_term_start_date(user_id)
    if term_start is None:
        return None
    local_now = _now(now)
    delta_days = (local_now.date() - term_start).days
    if delta_days < 0:
        return 0
    return delta_days // 7 + 1


def _week_matches(entry: ScheduleEntry, week_number: Optional[int]) -> bool:
    if week_number is None:
        return True
    if week_number <= 0:
        return False
    if week_number < int(entry.start_week) or week_number > int(entry.end_week):
        return False
    if int(entry.week_type) == 1:
        return week_number % 2 == 1
    if int(entry.week_type) == 2:
        return week_number % 2 == 0
    return True


def get_active_course(user_id: str, now: Optional[datetime] = None) -> Optional[ScheduleEntry]:
    entries = list_user_schedule(user_id)
    if not entries:
        return None

    local_now = _now(now)
    weekday = local_now.isoweekday()
    now_min = local_now.hour * 60 + local_now.minute
    week_number = get_user_current_week(user_id, now=local_now)

    active: Optional[ScheduleEntry] = None
    for item in entries:
        if int(item.day_of_week) != weekday:
            continue
        if not _week_matches(item, week_number):
            continue
        start_min = _time_to_minutes(item.start_time)
        end_min = _time_to_minutes(item.end_time)
        if start_min <= now_min < end_min:
            if active is None or _time_to_minutes(active.start_time) < start_min:
                active = item
    return active


def get_next_course(user_id: str, now: Optional[datetime] = None) -> Optional[Tuple[ScheduleEntry, int]]:
    entries = list_user_schedule(user_id)
    if not entries:
        return None

    local_now = _now(now)
    weekday_now = local_now.isoweekday()
    now_min = local_now.hour * 60 + local_now.minute
    week_number = get_user_current_week(user_id, now=local_now)

    max_offset = 14 if week_number is not None else 7
    for day_offset in range(0, max_offset):
        target_day = ((weekday_now + day_offset - 1) % 7) + 1
        candidate_week = week_number
        if week_number is not None:
            candidate_week = week_number + ((weekday_now - 1 + day_offset) // 7)
        day_candidates: List[ScheduleEntry] = []
        for item in entries:
            if int(item.day_of_week) != target_day:
                continue
            if not _week_matches(item, candidate_week):
                continue
            if day_offset == 0 and _time_to_minutes(item.start_time) <= now_min:
                continue
            day_candidates.append(item)
        if day_candidates:
            day_candidates.sort(key=lambda x: (_time_to_minutes(x.start_time), _time_to_minutes(x.end_time)))
            return day_candidates[0], day_offset
    return None


def set_current_study_context(reminder: str, course: Optional[ScheduleEntry]) -> None:
    _current_study_reminder.set(str(reminder or "").strip())
    _current_study_course.set(course)


def clear_current_study_context() -> None:
    _current_study_reminder.set("")
    _current_study_course.set(None)


def get_current_study_reminder() -> str:
    return str(_current_study_reminder.get() or "").strip()


def get_current_study_course() -> Optional[ScheduleEntry]:
    return _current_study_course.get()


try:
    init_study_db()
except Exception as e:
    logger.error("Init study db failed: %s", e)
