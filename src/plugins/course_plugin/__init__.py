from __future__ import annotations

import asyncio
import base64
import html
import io
import json
import mimetypes
import os
import random
import re
import time
import urllib.parse
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from nonebot import get_driver, on_command
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
)
from nonebot.log import logger
from nonebot.message import event_preprocessor
from nonebot.params import CommandArg
from openai import AsyncOpenAI
from PIL import Image, ImageDraw, ImageFont
from src.utils.roleplay import with_roleplay
from src.utils.study import (
    ScheduleEntry,
    clear_current_study_context,
    clear_user_schedule,
    get_active_course,
    get_user_current_week,
    get_user_term_start_date,
    get_next_course,
    get_current_study_course,
    has_user_schedule,
    init_study_db,
    list_user_schedule,
    normalize_hhmm,
    replace_user_schedule,
    set_current_study_context,
    study_reminder_enabled as _study_reminder_enabled,
    study_schedule_enabled as _study_schedule_enabled,
    validate_schedule_entry,
    weekday_label,
)

config = get_driver().config

study_schedule_enabled = _study_schedule_enabled()
study_reminder_enabled = _study_reminder_enabled()
study_import_max_chars = max(2000, int(getattr(config, "study_import_max_chars", 12000)))
study_import_timeout_seconds = float(getattr(config, "study_import_timeout_seconds", 20))
study_reminder_cache_seconds = max(
    10,
    int(getattr(config, "study_reminder_cache_seconds", 90)),
)
group_course_image_limit = max(1, int(getattr(config, "group_course_image_limit", 60)))
group_course_query_parallel = max(4, int(getattr(config, "group_course_query_parallel", 12)))
group_avatar_timeout_seconds = max(3.0, float(getattr(config, "group_avatar_timeout_seconds", 8.0)))

api_key = str(getattr(config, "llm_api_key", "")).strip()
base_url = str(getattr(config, "llm_base_url", "https://api.deepseek.com/v1")).strip()
model_name = str(getattr(config, "llm_model", "deepseek-chat")).strip()
llm_provider = str(getattr(config, "llm_provider", "openai")).strip().lower()
gemini_api_key = str(getattr(config, "gemini_api_key", "") or api_key).strip()
gemini_model_name = str(getattr(config, "gemini_model", model_name or "gemini-2.5-flash")).strip()
gemini_thinking_budget = int(getattr(config, "gemini_thinking_budget", 0))
llm_timeout_seconds = float(getattr(config, "llm_timeout_seconds", 30))
client = AsyncOpenAI(api_key=api_key, base_url=base_url) if (api_key and llm_provider != "gemini") else None

import_schedule_cmd = on_command(
    "导入课表",
    aliases={"课表导入", "上传课表", "import_schedule"},
    priority=5,
    block=True,
)
show_schedule_cmd = on_command(
    "课表",
    aliases={"查看课表", "我的课表", "schedule"},
    priority=5,
    block=True,
)
clear_schedule_cmd = on_command(
    "清空课表",
    aliases={"删除课表", "重置课表"},
    priority=5,
    block=True,
)
status_schedule_cmd = on_command(
    "上课状态",
    aliases={"现在上什么", "当前课程"},
    priority=5,
    block=True,
)
group_course_now_cmd = on_command(
    "群友上什么课",
    aliases={"群友上课", "谁在上课", "群课表状态", "group_course_now"},
    priority=5,
    block=True,
)

URL_PATTERN = re.compile(r"(https?://[^\s]+)", re.IGNORECASE)
FILE_URI_PATTERN = re.compile(r"(file://[^\s]+)", re.IGNORECASE)
WINDOWS_PATH_PATTERN = re.compile(r"([A-Za-z]:\\[^\r\n]+\.pdf)", re.IGNORECASE)
WAKEUP_TOKEN_PATTERN = re.compile(r"\b([0-9a-fA-F]{32})\b")
COURSE_BLOCK_PATTERN = re.compile(
    r"(?P<name>[^\n]{2,60}?)[○★]\s*[\r\n]+\((?P<start>\d{1,2})-(?P<end>\d{1,2})节\)"
    r"[\s\S]{0,260}?/场地:(?P<location>[^/\n]+)",
    re.IGNORECASE,
)
TIME_RANGE_PATTERN = re.compile(
    r"(?P<start>\d{1,2}[:：]\d{1,2})\s*(?:-|~|—|到)\s*(?P<end>\d{1,2}[:：]\d{1,2})"
)
DAY_MAPPING = {
    "1": 1,
    "2": 2,
    "3": 3,
    "4": 4,
    "5": 5,
    "6": 6,
    "7": 7,
    "周一": 1,
    "星期一": 1,
    "周二": 2,
    "星期二": 2,
    "周三": 3,
    "星期三": 3,
    "周四": 4,
    "星期四": 4,
    "周五": 5,
    "星期五": 5,
    "周六": 6,
    "星期六": 6,
    "周日": 7,
    "周天": 7,
    "星期日": 7,
    "星期天": 7,
}
SCHEDULE_EXAMPLE = """[
  {"day_of_week": 1, "start_time": "08:00", "end_time": "09:40", "course_name": "高等数学", "location": "A101"},
  {"day_of_week": 3, "start_time": "14:00", "end_time": "15:40", "course_name": "线性代数", "location": "B203"}
]"""
WAKEUP_SHARE_API = str(
    getattr(config, "wakeup_share_api", "https://i.wakeup.fun/share_schedule/get")
).strip()
GROUP_STUDY_QUOTES = [
    "一鸣惊人，百尺竿头，更进一步。",
    "书中自有黄金屋，书中自有颜如玉。",
    "事不三思终有悔，人能百忍总有得。",
    "人生自谁无死，留取丹心照汗青。",
    "勇者不惧，智者不惑。",
    "天行健，君子以自强不息。",
    "天高地迥，觉宇宙之无穷。",
    "志之所趋，无远弗届。",
    "志在千里，壮心不已。",
    "志当存高远，心有猛虎，细嗅蔷薇。",
    "星星之火，可以燎原。",
    "昨日种种，似水无痕；今日种种，似水有痕。",
    "立志欲坚不欲锐，成功在久不在速。",
    "奇利国家生死以，岂因祸福避趋之。",
    "行稳致远，方能大有作为。",
    "读书破万卷，胸中自有千秋笔。",
    "路漫漫其修远兮，吾将上下而求索。",
    "车到山前必有路，船到桥头自然直。",
    "风滴如晦，鸡鸣不已。",
]

reminder_cache: Dict[str, Tuple[float, str, str]] = {}
_pypdf_checked = False
_pypdf_available_cache = False
_pdfplumber_checked = False
_pdfplumber_available_cache = False


def _parse_csv(value: object) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).replace("，", ",")
    return [item.strip() for item in text.split(",") if item.strip()]


def _parse_int_set(value: object) -> set:
    out: set = set()
    for item in _parse_csv(value):
        try:
            out.add(int(item))
        except ValueError:
            logger.warning("Invalid group id in config: %s", item)
    return out


group_whitelist = _parse_int_set(getattr(config, "group_whitelist", ""))
group_blacklist = _parse_int_set(getattr(config, "group_blacklist", ""))


def _is_group_allowed(event: MessageEvent) -> bool:
    if not isinstance(event, GroupMessageEvent):
        return True
    group_id = event.group_id
    if group_whitelist and group_id not in group_whitelist:
        return False
    if group_id in group_blacklist:
        return False
    return True


def _build_reply_message(event: MessageEvent, text: str) -> Message:
    if isinstance(event, GroupMessageEvent):
        return MessageSegment.reply(event.message_id) + Message(text)
    return Message(text)


def _build_command_reply_message(event: MessageEvent, text: str) -> Message:
    return _build_reply_message(event, with_roleplay(text))


def _build_image_message(event: MessageEvent, image_data: bytes) -> Message:
    if isinstance(event, GroupMessageEvent):
        return MessageSegment.reply(event.message_id) + MessageSegment.image(image_data)
    return MessageSegment.image(image_data)


def _extract_json_block(text: str) -> str:
    content = str(text or "").strip()
    if not content:
        return ""
    fenced = re.search(r"```(?:json)?\s*([\s\S]+?)```", content, flags=re.IGNORECASE)
    if fenced:
        return fenced.group(1).strip()
    array_match = re.search(r"\[[\s\S]*\]", content)
    if array_match:
        return array_match.group(0).strip()
    obj_match = re.search(r"\{[\s\S]*\}", content)
    if obj_match:
        return obj_match.group(0).strip()
    return content


def _truncate_text(value: str, max_chars: int = study_import_max_chars) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars]


def _llm_available() -> bool:
    if llm_provider == "gemini":
        return bool(gemini_api_key)
    return client is not None


def _parse_day(value: Any) -> Optional[int]:
    text = str(value or "").strip()
    if not text:
        return None
    if text in DAY_MAPPING:
        return DAY_MAPPING[text]
    text = text.replace("礼拜", "星期")
    return DAY_MAPPING.get(text)


def _normalize_entry(item: Dict[str, Any], user_id: str) -> Optional[ScheduleEntry]:
    day = _parse_day(
        item.get("day_of_week")
        or item.get("day")
        or item.get("weekday")
        or item.get("week_day")
        or item.get("week")
    )
    if day is None:
        return None

    start = normalize_hhmm(
        str(
            item.get("start_time")
            or item.get("start")
            or item.get("begin")
            or item.get("begin_time")
            or ""
        )
    )
    end = normalize_hhmm(
        str(
            item.get("end_time")
            or item.get("end")
            or item.get("finish")
            or item.get("finish_time")
            or ""
        )
    )
    if (not start or not end) and (item.get("time") or item.get("time_range")):
        match = TIME_RANGE_PATTERN.search(str(item.get("time") or item.get("time_range")))
        if match:
            if not start:
                start = normalize_hhmm(match.group("start"))
            if not end:
                end = normalize_hhmm(match.group("end"))
    if not start or not end:
        return None

    course_name = str(
        item.get("course_name") or item.get("course") or item.get("name") or item.get("title") or ""
    ).strip()
    if not course_name:
        return None
    location = str(item.get("location") or item.get("room") or item.get("classroom") or "").strip()

    return validate_schedule_entry(
        ScheduleEntry(
            user_id=str(user_id),
            day_of_week=day,
            start_time=start,
            end_time=end,
            course_name=course_name,
            location=location,
        )
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
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    out.sort(key=lambda x: (x.day_of_week, x.start_time, x.end_time, x.course_name))
    return out


def _parse_entries_from_json(text: str, user_id: str) -> List[ScheduleEntry]:
    block = _extract_json_block(text)
    if not block:
        return []
    try:
        payload = json.loads(block)
    except Exception:
        return []

    rows: List[Any]
    if isinstance(payload, dict):
        if isinstance(payload.get("courses"), list):
            rows = payload.get("courses") or []
        elif isinstance(payload.get("data"), list):
            rows = payload.get("data") or []
        else:
            rows = [payload]
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []

    out: List[ScheduleEntry] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        entry = _normalize_entry(row, user_id=user_id)
        if entry:
            out.append(entry)
    return _dedupe_entries(out)


def _parse_entries_from_lines(text: str, user_id: str) -> List[ScheduleEntry]:
    out: List[ScheduleEntry] = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        day: Optional[int] = None
        rest = line
        for day_text in sorted(DAY_MAPPING.keys(), key=lambda x: len(x), reverse=True):
            if not rest.startswith(day_text):
                continue
            day = DAY_MAPPING[day_text]
            rest = rest[len(day_text) :].strip()
            break
        if day is None:
            continue

        match = TIME_RANGE_PATTERN.search(rest)
        if not match:
            continue
        start = normalize_hhmm(match.group("start"))
        end = normalize_hhmm(match.group("end"))
        if not start or not end:
            continue

        tail = rest[match.end() :].strip()
        if not tail:
            continue
        location = ""
        name = tail
        for sep in (" @ ", "@", "#", "|"):
            if sep not in tail:
                continue
            left, right = tail.split(sep, 1)
            name = left.strip()
            location = right.strip()
            break
        if not name:
            continue

        entry = validate_schedule_entry(
            ScheduleEntry(
                user_id=str(user_id),
                day_of_week=day,
                start_time=start,
                end_time=end,
                course_name=name,
                location=location,
            )
        )
        if entry:
            out.append(entry)
    return _dedupe_entries(out)


def _extract_wakeup_token(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    match = WAKEUP_TOKEN_PATTERN.search(value)
    if match:
        return match.group(1).lower()
    match = re.search(r"口令[为:：\s「『\"]*([0-9a-fA-F]{32})", value)
    if match:
        return match.group(1).lower()
    return ""


def _parse_week_type(value: Any) -> int:
    text = str(value or "").strip().lower()
    if text in {"1", "odd", "single", "单", "单周"}:
        return 1
    if text in {"2", "even", "double", "双", "双周"}:
        return 2
    return 0


def _extract_term_start_date(meta: Any) -> Optional[str]:
    if not isinstance(meta, dict):
        return None
    for key in ("startDate", "start_date", "termStartDate", "beginDate", "firstWeekDate"):
        value = str(meta.get(key) or "").strip()
        if not value:
            continue
        value = value.replace(".", "-").replace("/", "-")
        match = re.search(r"\d{4}-\d{1,2}-\d{1,2}", value)
        if match:
            return match.group(0)
    return None


def _parse_wakeup_parts(raw_data: Any) -> List[Any]:
    if isinstance(raw_data, list):
        return raw_data
    if isinstance(raw_data, dict):
        return [raw_data]
    if not isinstance(raw_data, str):
        return []
    parts: List[Any] = []
    for line in raw_data.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parts.append(json.loads(line))
        except Exception:
            continue
    return parts


def _build_wakeup_node_time_map(rows: Any) -> Dict[int, Tuple[str, str]]:
    output: Dict[int, Tuple[str, str]] = {}
    if not isinstance(rows, list):
        return output
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            node = int(row.get("node") or row.get("index") or row.get("no") or 0)
        except Exception:
            continue
        if node <= 0:
            continue
        start = normalize_hhmm(
            str(row.get("startTime") or row.get("start") or row.get("start_time") or "")
        )
        end = normalize_hhmm(
            str(row.get("endTime") or row.get("end") or row.get("end_time") or "")
        )
        if (not start or not end) and row.get("time"):
            match = TIME_RANGE_PATTERN.search(str(row.get("time")))
            if match:
                if not start:
                    start = normalize_hhmm(match.group("start"))
                if not end:
                    end = normalize_hhmm(match.group("end"))
        if not start or not end:
            continue
        output[node] = (start, end)
    return output


def _build_wakeup_course_name_map(rows: Any) -> Dict[int, str]:
    output: Dict[int, str] = {}
    if not isinstance(rows, list):
        return output
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("courseName") or row.get("name") or "").strip()
        if not name:
            continue
        for key in ("id", "courseId", "course_id"):
            try:
                course_id = int(row.get(key) or 0)
            except Exception:
                continue
            if course_id > 0:
                output[course_id] = name
                break
    return output


def _parse_wakeup_schedule(parts: List[Any], user_id: str) -> Tuple[List[ScheduleEntry], Optional[str]]:
    nodes_rows: Any = []
    meta_row: Any = {}
    course_rows: Any = []
    detail_rows: Any = []

    if len(parts) >= 5:
        nodes_rows = parts[1]
        meta_row = parts[2]
        course_rows = parts[3]
        detail_rows = parts[4]
    elif len(parts) == 1 and isinstance(parts[0], dict):
        payload = parts[0]
        nodes_rows = payload.get("nodes") or payload.get("tableNodes") or []
        meta_row = payload.get("meta") or payload.get("settings") or {}
        course_rows = payload.get("courses") or payload.get("courseList") or []
        detail_rows = payload.get("details") or payload.get("arranges") or payload.get("scheduleList") or []

    node_time_map = _build_wakeup_node_time_map(nodes_rows)
    course_name_map = _build_wakeup_course_name_map(course_rows)
    term_start_date = _extract_term_start_date(meta_row)

    output: List[ScheduleEntry] = []
    if not isinstance(detail_rows, list):
        return [], term_start_date

    for row in detail_rows:
        if not isinstance(row, dict):
            continue
        try:
            day = int(row.get("day") or row.get("weekday") or row.get("weekDay") or 0)
        except Exception:
            day = 0
        if day < 1 or day > 7:
            continue

        try:
            start_node = int(row.get("startNode") or row.get("start_section") or row.get("startNodeId") or 0)
        except Exception:
            start_node = 0
        step = 1
        try:
            step = int(row.get("step") or row.get("nodeCount") or row.get("durationNode") or 1)
        except Exception:
            step = 1
        if step <= 0:
            step = 1
        end_node = start_node + step - 1 if start_node > 0 else 0

        start_time = normalize_hhmm(
            str(row.get("startTime") or row.get("start_time") or row.get("beginTime") or "")
        )
        end_time = normalize_hhmm(
            str(row.get("endTime") or row.get("end_time") or row.get("finishTime") or "")
        )
        if not start_time and start_node in node_time_map:
            start_time = node_time_map[start_node][0]
        if not end_time and end_node in node_time_map:
            end_time = node_time_map[end_node][1]
        if not start_time and start_node in node_time_map:
            start_time = node_time_map[start_node][0]
        if not end_time and start_node in node_time_map:
            end_time = node_time_map[start_node][1]
        if not start_time or not end_time:
            continue

        course_name = str(row.get("courseName") or row.get("name") or "").strip()
        if not course_name:
            try:
                course_id = int(row.get("courseId") or row.get("course_id") or row.get("id") or 0)
            except Exception:
                course_id = 0
            course_name = course_name_map.get(course_id, "").strip()
        if not course_name:
            continue

        location = str(
            row.get("room")
            or row.get("location")
            or row.get("classroom")
            or row.get("classRoom")
            or ""
        ).strip()

        try:
            start_week = int(row.get("startWeek") or row.get("start_week") or 1)
        except Exception:
            start_week = 1
        try:
            end_week = int(row.get("endWeek") or row.get("end_week") or start_week)
        except Exception:
            end_week = start_week
        if end_week < start_week:
            end_week = start_week
        week_type = _parse_week_type(row.get("type") or row.get("weekType") or row.get("week_type"))

        entry = validate_schedule_entry(
            ScheduleEntry(
                user_id=str(user_id),
                day_of_week=day,
                start_time=start_time,
                end_time=end_time,
                course_name=course_name,
                location=location,
                start_week=start_week,
                end_week=end_week,
                week_type=week_type,
            )
        )
        if entry:
            output.append(entry)
    return _dedupe_entries(output), term_start_date


async def _fetch_wakeup_schedule(token: str, user_id: str) -> Tuple[List[ScheduleEntry], Optional[str]]:
    timeout = httpx.Timeout(study_import_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as http_client:
        response = await http_client.get(WAKEUP_SHARE_API, params={"key": token})
        response.raise_for_status()
        payload = response.json()

    status = int(payload.get("status") or 0)
    if status != 1:
        msg = str(payload.get("message") or payload.get("msg") or "WakeUp 口令无效或已失效").strip()
        raise RuntimeError(msg)

    parts = _parse_wakeup_parts(payload.get("data"))
    entries, term_start_date = _parse_wakeup_schedule(parts, user_id=user_id)
    if not entries:
        raise RuntimeError("WakeUp 数据已获取，但未解析出有效课程。")
    return entries, term_start_date


def _gemini_endpoint(model: str) -> str:
    model_id = model.strip()
    if not model_id.startswith("models/"):
        model_id = f"models/{model_id}"
    return f"https://generativelanguage.googleapis.com/v1beta/{model_id}:generateContent"


def _extract_gemini_text(payload: Dict[str, Any]) -> str:
    candidates = payload.get("candidates", [])
    if not isinstance(candidates, list):
        return ""
    for item in candidates:
        if not isinstance(item, dict):
            continue
        content = item.get("content", {})
        if not isinstance(content, dict):
            continue
        parts = content.get("parts", [])
        if not isinstance(parts, list):
            continue
        texts = [
            str(part.get("text", "")).strip()
            for part in parts
            if isinstance(part, dict) and str(part.get("text", "")).strip()
        ]
        if texts:
            return "\n".join(texts).strip()
    return ""


async def _gemini_generate(
    *,
    contents: List[dict],
    system_instruction: str,
    max_output_tokens: int,
) -> str:
    if not gemini_api_key:
        return ""
    payload: Dict[str, Any] = {"contents": contents}
    if system_instruction:
        payload["system_instruction"] = {"parts": [{"text": system_instruction}]}
    config_data: Dict[str, Any] = {"maxOutputTokens": max_output_tokens}
    if "2.5" in gemini_model_name and gemini_thinking_budget >= 0:
        config_data["thinkingConfig"] = {"thinkingBudget": gemini_thinking_budget}
    payload["generationConfig"] = config_data
    headers = {"Content-Type": "application/json", "x-goog-api-key": gemini_api_key}
    timeout = httpx.Timeout(llm_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as http_client:
        response = await http_client.post(
            _gemini_endpoint(gemini_model_name or model_name or "gemini-2.5-flash"),
            json=payload,
            headers=headers,
        )
        response.raise_for_status()
        return _extract_gemini_text(response.json())


async def _llm_parse_from_text(raw_text: str, user_id: str) -> List[ScheduleEntry]:
    if not _llm_available():
        return []
    prompt = (
        "请从下面内容提取课表，输出严格 JSON 数组。"
        "不要解释，不要 markdown。\n"
        "字段：day_of_week(1-7)、start_time(HH:MM)、end_time(HH:MM)、course_name、location。\n"
        "无法识别输出 []。\n"
        f"格式示例：\n{SCHEDULE_EXAMPLE}\n\n"
        f"待解析内容：\n{_truncate_text(raw_text)}"
    )
    try:
        if llm_provider == "gemini":
            text = await _gemini_generate(
                contents=[{"role": "user", "parts": [{"text": prompt}]}],
                system_instruction="你是课表解析器，只能输出 JSON。",
                max_output_tokens=1500,
            )
        else:
            if client is None:
                return []
            response = await client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": "你是课表解析器，只能输出 JSON。"},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=1500,
            )
            text = (response.choices[0].message.content or "").strip()
        parsed = _parse_entries_from_json(text, user_id=user_id)
        return _dedupe_entries(parsed)
    except Exception as e:
        logger.warning("LLM parse schedule from text failed: %s", e)
        return []


def _detect_mime(binary_data: bytes, header_content_type: str) -> str:
    content_type = (header_content_type or "").split(";")[0].strip().lower()
    if content_type:
        return content_type
    try:
        with Image.open(io.BytesIO(binary_data)) as img:
            fmt = str(img.format or "").strip().upper()
    except Exception:
        fmt = ""
    mapping = {
        "JPEG": "image/jpeg",
        "JPG": "image/jpeg",
        "PNG": "image/png",
        "WEBP": "image/webp",
        "GIF": "image/gif",
        "BMP": "image/bmp",
    }
    return mapping.get(fmt, "application/octet-stream")


async def _llm_parse_from_binary(binary_data: bytes, mime_type: str, user_id: str) -> List[ScheduleEntry]:
    if not gemini_api_key:
        return []
    prompt = (
        "请读取附件里的课表信息并输出 JSON 数组。"
        "不要解释，不要 markdown。\n"
        "字段：day_of_week(1-7)、start_time(HH:MM)、end_time(HH:MM)、course_name、location。\n"
        "无法识别输出 []。"
    )
    try:
        text = await _gemini_generate(
            contents=[
                {
                    "role": "user",
                    "parts": [
                        {"text": prompt},
                        {
                            "inline_data": {
                                "mime_type": mime_type,
                                "data": base64.b64encode(binary_data).decode("ascii"),
                            }
                        },
                    ],
                }
            ],
            system_instruction="你是课表解析器，只能输出 JSON。",
            max_output_tokens=1600,
        )
        parsed = _parse_entries_from_json(text, user_id=user_id)
        return _dedupe_entries(parsed)
    except Exception as e:
        logger.warning("LLM parse schedule from binary failed: %s", e)
        return []


def _pypdf_available() -> bool:
    global _pypdf_checked, _pypdf_available_cache
    if _pypdf_checked:
        return _pypdf_available_cache
    _pypdf_checked = True
    try:
        import pypdf  # noqa: F401

        _pypdf_available_cache = True
    except Exception:
        _pypdf_available_cache = False
    return _pypdf_available_cache


def _pdfplumber_available() -> bool:
    global _pdfplumber_checked, _pdfplumber_available_cache
    if _pdfplumber_checked:
        return _pdfplumber_available_cache
    _pdfplumber_checked = True
    try:
        import pdfplumber  # noqa: F401

        _pdfplumber_available_cache = True
    except Exception:
        _pdfplumber_available_cache = False
    return _pdfplumber_available_cache


def _extract_pdf_text(binary_data: bytes) -> str:
    if not _pypdf_available():
        return ""
    from pypdf import PdfReader  # type: ignore

    try:
        reader = PdfReader(io.BytesIO(binary_data))
    except Exception:
        return ""
    chunks: List[str] = []
    for page in reader.pages:
        try:
            text = str(page.extract_text() or "").strip()
            if text:
                chunks.append(text)
        except Exception:
            continue
    return _truncate_text("\n".join(chunks))


async def _download_url(url: str) -> Tuple[bytes, str, str]:
    candidate = str(url or "").strip()
    if candidate.startswith("file://"):
        parsed = urllib.parse.urlparse(candidate)
        path_value = urllib.parse.unquote(parsed.path or "")
        if os.name == "nt" and path_value.startswith("/") and len(path_value) > 2 and path_value[2] == ":":
            path_value = path_value[1:]
        if path_value and os.path.exists(path_value):
            content = Path(path_value).read_bytes()
            guessed_type, _ = mimetypes.guess_type(path_value)
            return content, str(guessed_type or "application/octet-stream"), candidate
    if candidate and os.path.exists(candidate):
        content = Path(candidate).read_bytes()
        guessed_type, _ = mimetypes.guess_type(candidate)
        return content, str(guessed_type or "application/octet-stream"), candidate

    timeout = httpx.Timeout(study_import_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as http_client:
        response = await http_client.get(
            candidate,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                )
            },
        )
        response.raise_for_status()
    content_type = str(response.headers.get("Content-Type") or "").strip().lower()
    return response.content, content_type, str(response.url)


def _strip_html(raw: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", raw, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text, flags=re.IGNORECASE)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _section_index_to_hhmm(index: int, is_start: bool) -> str:
    start_map = {
        1: "08:00",
        3: "10:00",
        5: "14:00",
        7: "16:00",
        9: "19:00",
        11: "20:40",
        13: "22:20",
    }
    end_map = {
        2: "09:40",
        4: "11:40",
        6: "15:40",
        8: "17:40",
        10: "20:30",
        12: "22:10",
        13: "23:00",
    }
    if is_start:
        return start_map.get(index, "08:00")
    return end_map.get(index, "09:40")


def _build_time_from_sections(start_idx: int, end_idx: int) -> Tuple[str, str]:
    start_text = _section_index_to_hhmm(start_idx, is_start=True)
    end_text = _section_index_to_hhmm(end_idx, is_start=False)
    return start_text, end_text


def _clean_course_name(value: str) -> str:
    name = str(value or "").strip()
    name = re.sub(r"^\d+\s*", "", name)
    name = re.sub(r"[○★●◆]+\s*$", "", name).strip()
    return name


def _extract_week_hint(body_text: str) -> str:
    compact = re.sub(r"\s+", "", str(body_text or ""))
    if not compact:
        return ""
    match = re.search(r"^([0-9,\-周单双()]+?)/校区", compact)
    if not match:
        return ""
    return match.group(1).strip()


def _extract_location(body_text: str) -> str:
    compact = str(body_text or "").replace("\n", "")
    match = re.search(r"/场地[:：]\s*([^/\n]+)", compact)
    if not match:
        return ""
    return str(match.group(1) or "").strip()


def _extract_course_chunks_from_cell(cell_text: str) -> List[Tuple[str, int, int, str, str]]:
    text = str(cell_text or "").replace("\r", "").strip()
    if not text:
        return []

    markers = list(re.finditer(r"\((\d{1,2})-(\d{1,2})节\)", text))
    if not markers:
        return []

    chunks: List[Tuple[str, int, int, str, str]] = []
    for idx, marker in enumerate(markers):
        start_pos = marker.start()
        end_pos = marker.end()
        next_start = markers[idx + 1].start() if idx + 1 < len(markers) else len(text)

        prefix = text[:start_pos]
        prefix_lines = [line.strip() for line in prefix.splitlines() if line.strip()]
        if not prefix_lines:
            continue
        course_name = _clean_course_name(prefix_lines[-1])
        if not course_name:
            continue

        try:
            section_start = int(marker.group(1))
            section_end = int(marker.group(2))
        except Exception:
            continue
        if section_end < section_start:
            continue

        body = text[end_pos:next_start]
        location = _extract_location(body)
        week_hint = _extract_week_hint(body)
        chunks.append((course_name, section_start, section_end, location, week_hint))

    # 某些单元格只有一个课程但被分页打断，使用整段兜底一次
    if not chunks:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if lines:
            name = _clean_course_name(lines[0])
            if name:
                chunks.append((name, 1, 2, _extract_location(text), _extract_week_hint(text)))
    return chunks


def _parse_entries_from_pdf_table(binary_data: bytes, user_id: str) -> List[ScheduleEntry]:
    if not _pdfplumber_available():
        return []

    import pdfplumber  # type: ignore

    output: List[ScheduleEntry] = []
    try:
        with pdfplumber.open(io.BytesIO(binary_data)) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables() or []
                for table in tables:
                    for row in table:
                        if not isinstance(row, list) or len(row) < 9:
                            continue
                        section_text = str(row[1] or "").strip()
                        if not section_text.isdigit():
                            continue
                        fallback_section = int(section_text)

                        for day_col in range(2, 9):
                            day_of_week = day_col - 1
                            cell_text = str(row[day_col] or "").strip()
                            if not cell_text:
                                continue
                            chunks = _extract_course_chunks_from_cell(cell_text)
                            if not chunks:
                                continue

                            for course_name, sec_start, sec_end, location, week_hint in chunks:
                                # 优先使用课程块里的节次，缺失时回退到当前行节次
                                start_idx = sec_start if sec_start > 0 else fallback_section
                                end_idx = sec_end if sec_end >= start_idx else fallback_section
                                start_time, end_time = _build_time_from_sections(start_idx, end_idx)
                                normalized_name = course_name
                                if week_hint and any(token in week_hint for token in ("单", "双")):
                                    normalized_name = f"{course_name} [{week_hint}]"

                                entry = validate_schedule_entry(
                                    ScheduleEntry(
                                        user_id=str(user_id),
                                        day_of_week=day_of_week,
                                        start_time=start_time,
                                        end_time=end_time,
                                        course_name=normalized_name,
                                        location=location,
                                    )
                                )
                                if entry:
                                    output.append(entry)
    except Exception as e:
        logger.warning("Parse PDF table by pdfplumber failed: %s", e)
        return []

    return _dedupe_entries(output)


def _parse_sparse_pdf_entries(text: str, user_id: str) -> List[ScheduleEntry]:
    matches = list(COURSE_BLOCK_PATTERN.finditer(text or ""))
    if not matches:
        return []

    grouped: Dict[Tuple[int, int], List[Tuple[int, str, str]]] = {}
    for idx, match in enumerate(matches):
        course_name = str(match.group("name") or "").strip()
        location = str(match.group("location") or "").strip()
        try:
            section_start = int(match.group("start"))
            section_end = int(match.group("end"))
        except Exception:
            continue
        if not course_name or section_end < section_start:
            continue
        key = (section_start, section_end)
        grouped.setdefault(key, []).append((idx, course_name, location))

    output: List[ScheduleEntry] = []
    for (section_start, section_end), rows in grouped.items():
        seen_course = set()
        day_of_week = 1
        start_time, end_time = _build_time_from_sections(section_start, section_end)
        for _, course_name, location in rows:
            dedupe_key = (course_name, location)
            if dedupe_key in seen_course:
                continue
            seen_course.add(dedupe_key)
            entry = validate_schedule_entry(
                ScheduleEntry(
                    user_id=str(user_id),
                    day_of_week=day_of_week,
                    start_time=start_time,
                    end_time=end_time,
                    course_name=course_name,
                    location=location,
                )
            )
            if entry:
                output.append(entry)
            day_of_week += 1
            if day_of_week > 7:
                break

    return _dedupe_entries(output)


async def _parse_schedule_from_url(url: str, user_id: str) -> List[ScheduleEntry]:
    binary_data, content_type, final_url = await _download_url(url)
    lower_url = final_url.lower()
    is_image = content_type.startswith("image/") or any(
        lower_url.endswith(suffix) for suffix in (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif")
    )
    is_pdf = (
        "pdf" in content_type
        or lower_url.endswith(".pdf")
        or binary_data.startswith(b"%PDF")
    )
    if is_image:
        mime = _detect_mime(binary_data, content_type)
        return await _llm_parse_from_binary(binary_data, mime, user_id=user_id)

    if is_pdf:
        logger.info(
            f"Detected PDF import source: url={final_url} "
            f"content_type={content_type or '(empty)'} size={len(binary_data)}"
        )

        table_entries = _parse_entries_from_pdf_table(binary_data, user_id=user_id)
        logger.info(f"PDF table parsed entries: {len(table_entries)}")
        if table_entries:
            return table_entries

        text = _extract_pdf_text(binary_data)
        logger.info(f"PDF text extraction length: {len(text)}")
        if text:
            entries = await _parse_schedule_from_text(text, user_id=user_id)
            logger.info(f"PDF parsed entries from extracted text: {len(entries)}")
            if entries:
                return entries
            heuristic_entries = _parse_sparse_pdf_entries(text, user_id=user_id)
            logger.info(f"PDF heuristic parsed entries: {len(heuristic_entries)}")
            if heuristic_entries:
                return heuristic_entries
        llm_entries = await _llm_parse_from_binary(binary_data, "application/pdf", user_id=user_id)
        logger.info(f"PDF parsed entries from binary multimodal: {len(llm_entries)}")
        if llm_entries:
            return llm_entries
        if not _pypdf_available():
            raise RuntimeError(
                "检测到你上传的是 PDF，但当前环境缺少 `pypdf` 依赖。"
                "请先执行 `pip install -r requirements.txt` 后再重试。"
            )
        return []

    decoded = ""
    for encoding in ("utf-8", "gb18030", "latin1"):
        try:
            decoded = binary_data.decode(encoding)
            break
        except Exception:
            continue
    if not decoded:
        return []
    cleaned = _strip_html(decoded)
    return await _parse_schedule_from_text(cleaned, user_id=user_id)


async def _parse_schedule_from_text(text: str, user_id: str) -> List[ScheduleEntry]:
    raw = _truncate_text(text)
    if not raw:
        return []
    entries = _parse_entries_from_json(raw, user_id=user_id)
    if entries:
        return entries
    entries = _parse_entries_from_lines(raw, user_id=user_id)
    if entries:
        return entries
    return await _llm_parse_from_text(raw, user_id=user_id)


def _extract_image_url(message: Message) -> Optional[str]:
    for segment in message:
        if segment.type != "image":
            continue
        url = str(segment.data.get("url") or "").strip()
        if url.startswith("http://") or url.startswith("https://"):
            return url
        file_value = str(segment.data.get("file") or "").strip()
        if file_value.startswith("http://") or file_value.startswith("https://"):
            return file_value
    return None


async def _extract_image_url_with_file_fallback(bot: Bot, message: Message) -> Optional[str]:
    direct = _extract_image_url(message)
    if direct:
        return direct
    for segment in message:
        if segment.type != "image":
            continue
        file_value = str(segment.data.get("file") or "").strip()
        if not file_value:
            continue
        try:
            image_info = await bot.get_image(file=file_value)
            recovered_url = str(image_info.get("url") or "").strip()
            if recovered_url.startswith("http://") or recovered_url.startswith("https://"):
                return recovered_url
        except Exception as e:
            logger.debug("Recover image url by file failed: %s", e)
    return None


def _extract_file_url(message: Message) -> Optional[str]:
    for segment in message:
        if segment.type != "file":
            continue
        url = str(segment.data.get("url") or "").strip()
        if url.startswith("http://") or url.startswith("https://"):
            return url
    return None


async def _resolve_group_file_url(bot: Bot, group_id: int, file_data: Dict[str, Any]) -> Optional[str]:
    file_id = str(
        file_data.get("file_id")
        or file_data.get("id")
        or file_data.get("fid")
        or ""
    ).strip()
    if not file_id:
        return None
    busid_raw = file_data.get("busid") or file_data.get("bus_id") or file_data.get("biz_id")
    busid_text = str(busid_raw).strip() if busid_raw is not None else ""

    call_payloads: List[Dict[str, Any]] = []
    if busid_text:
        try:
            call_payloads.append(
                {"group_id": int(group_id), "file_id": file_id, "busid": int(busid_text)}
            )
        except ValueError:
            call_payloads.append({"group_id": int(group_id), "file_id": file_id, "busid": busid_text})
    call_payloads.append({"group_id": int(group_id), "file_id": file_id})

    for payload in call_payloads:
        try:
            data = await bot.call_api("get_group_file_url", **payload)
            if not isinstance(data, dict):
                continue
            for key in ("url", "download_url", "file_url"):
                value = str(data.get(key) or "").strip()
                if value.startswith("http://") or value.startswith("https://"):
                    return value
        except Exception as e:
            logger.debug(f"get_group_file_url failed: payload={payload} err={e}")
            continue
    return None


async def _extract_file_url_with_fallback(
    bot: Bot,
    event: MessageEvent,
    message: Message,
) -> Optional[str]:
    direct = _extract_file_url(message)
    if direct:
        return direct
    if not isinstance(event, GroupMessageEvent):
        return None
    for segment in message:
        if segment.type != "file":
            continue
        resolved = await _resolve_group_file_url(bot, event.group_id, segment.data)
        if resolved:
            return resolved
    return None


def _extract_reply_message_id(message: Message) -> Optional[int]:
    for segment in message:
        if segment.type != "reply":
            continue
        reply_id = str(segment.data.get("id") or "").strip()
        if not reply_id:
            continue
        try:
            return int(reply_id)
        except ValueError:
            return None
    return None


async def _extract_media_from_context(
    bot: Bot,
    event: MessageEvent,
    args: Message,
) -> Tuple[Optional[str], Optional[str]]:
    image_url = await _extract_image_url_with_file_fallback(bot, args)
    if not image_url:
        image_url = await _extract_image_url_with_file_fallback(bot, event.message)
    file_url = await _extract_file_url_with_fallback(bot, event, args)
    if not file_url:
        file_url = await _extract_file_url_with_fallback(bot, event, event.message)
    if image_url or file_url:
        return image_url, file_url

    reply_obj = getattr(event, "reply", None)
    if reply_obj:
        reply_message = getattr(reply_obj, "message", None)
        if reply_message is not None:
            reply_message_obj = (
                reply_message if isinstance(reply_message, Message) else Message(reply_message)
            )
            image_url = await _extract_image_url_with_file_fallback(bot, reply_message_obj)
            file_url = await _extract_file_url_with_fallback(bot, event, reply_message_obj)
            if image_url or file_url:
                return image_url, file_url
        reply_message_id = getattr(reply_obj, "message_id", None)
        if reply_message_id is not None:
            try:
                reply_msg = await bot.get_msg(message_id=int(reply_message_id))
                raw_message = reply_msg.get("message")
                if raw_message is not None:
                    message_obj = raw_message if isinstance(raw_message, Message) else Message(raw_message)
                    image_url = await _extract_image_url_with_file_fallback(bot, message_obj)
                    file_url = await _extract_file_url_with_fallback(bot, event, message_obj)
                    if image_url or file_url:
                        return image_url, file_url
            except Exception as e:
                logger.debug("Resolve event.reply message failed: %s", e)

    reply_id = _extract_reply_message_id(event.message)
    if not reply_id:
        return None, None
    try:
        reply_msg = await bot.get_msg(message_id=reply_id)
        raw_message = reply_msg.get("message")
        if raw_message is None:
            return None, None
        message_obj = raw_message if isinstance(raw_message, Message) else Message(raw_message)
        image_url = await _extract_image_url_with_file_fallback(bot, message_obj)
        file_url = await _extract_file_url_with_fallback(bot, event, message_obj)
        return image_url, file_url
    except Exception as e:
        logger.debug("Resolve replied media failed: %s", e)
        pass

    raw_candidates = [
        str(getattr(event, "raw_message", "") or ""),
        str(event.message),
        str(args),
    ]
    for raw in raw_candidates:
        url = _first_url(raw)
        if url:
            return None, url
        file_uri = _first_file_uri(raw)
        if file_uri:
            return None, file_uri
    return None, None


def _message_plain_text(message: Message) -> str:
    try:
        return message.extract_plain_text().strip()
    except Exception:
        return str(message or "").strip()


async def _extract_wakeup_token_from_context(bot: Bot, event: MessageEvent, args: Message) -> str:
    candidates: List[str] = [
        _message_plain_text(args),
        _message_plain_text(event.message),
        str(getattr(event, "raw_message", "") or "").strip(),
    ]

    reply_obj = getattr(event, "reply", None)
    if reply_obj:
        reply_message = getattr(reply_obj, "message", None)
        if reply_message is not None:
            reply_message_obj = reply_message if isinstance(reply_message, Message) else Message(reply_message)
            candidates.append(_message_plain_text(reply_message_obj))
        reply_message_id = getattr(reply_obj, "message_id", None)
        if reply_message_id is not None:
            try:
                reply_msg = await bot.get_msg(message_id=int(reply_message_id))
                raw_message = reply_msg.get("message")
                if raw_message is not None:
                    message_obj = raw_message if isinstance(raw_message, Message) else Message(raw_message)
                    candidates.append(_message_plain_text(message_obj))
                    candidates.append(str(raw_message))
            except Exception as e:
                logger.debug("Resolve event.reply for wakeup token failed: %s", e)

    reply_id = _extract_reply_message_id(event.message)
    if reply_id:
        try:
            reply_msg = await bot.get_msg(message_id=reply_id)
            raw_message = reply_msg.get("message")
            if raw_message is not None:
                message_obj = raw_message if isinstance(raw_message, Message) else Message(raw_message)
                candidates.append(_message_plain_text(message_obj))
                candidates.append(str(raw_message))
        except Exception as e:
            logger.debug("Resolve reply segment for wakeup token failed: %s", e)

    for text in candidates:
        token = _extract_wakeup_token(text)
        if token:
            return token
    return ""


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for font_path in (
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
    ):
        try:
            return ImageFont.truetype(font_path, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> List[str]:
    content = str(text or "").strip()
    if not content:
        return []
    out: List[str] = []
    current = ""
    for ch in content:
        trial = current + ch
        bbox = draw.textbbox((0, 0), trial, font=font)
        if int(bbox[2] - bbox[0]) <= max_width:
            current = trial
            continue
        if current:
            out.append(current)
        current = ch
    if current:
        out.append(current)
    return out


def _time_to_minutes(value: str) -> int:
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def _render_schedule_image(entries: List[ScheduleEntry]) -> bytes:
    width, height = 1680, 1120
    margin_left, margin_right = 140, 60
    margin_top, margin_bottom = 140, 60
    day_width = (width - margin_left - margin_right) / 7.0
    min_start = min(_time_to_minutes(e.start_time) for e in entries) if entries else 8*60
    max_end = max(_time_to_minutes(e.end_time) for e in entries) if entries else 18*60
    axis_start = max(6 * 60, (min_start // 60) * 60 - 60)
    axis_end = min(23 * 60, ((max_end + 59) // 60) * 60 + 60)
    if axis_end - axis_start < 6 * 60:
        axis_end = min(23 * 60, axis_start + 6 * 60)
    minute_height = (height - margin_top - margin_bottom) / float(max(60, axis_end - axis_start))

    img = Image.new("RGBA", (width, height), "#F3F6F9")
    draw = ImageDraw.Draw(img)
    
    title_font = _load_font(48)
    day_font = _load_font(28)
    text_font = _load_font(24)
    small_font = _load_font(18)

    draw.rectangle((0, 0, width, margin_top - 20), fill="#FFFFFF")
    draw.line((0, margin_top - 20, width, margin_top - 20), fill="#E2E8F0", width=2)
    
    draw.text((40, 30), "📅 我的周课表", fill="#1E293B", font=title_font)

    for i in range(8):
        x = int(margin_left + i * day_width)
        draw.line((x, margin_top - 20, x, height - margin_bottom + 20), fill="#E2E8F0", width=1)
        if i < 7:
            label = weekday_label(i + 1)
            bbox = draw.textbbox((0, 0), label, font=day_font)
            tw = int(bbox[2] - bbox[0])
            draw.text((int(x + day_width / 2 - tw / 2), margin_top - 55), label, fill="#475569", font=day_font)

    for minute in range(axis_start, axis_end + 1, 60):
        y = int(margin_top + (minute - axis_start) * minute_height)
        draw.line((margin_left, y, width - margin_right, y), fill="#E2E8F0", width=1)
        draw.text((margin_left - 80, y - 12), f"{minute // 60:02d}:00", fill="#64748B", font=small_font)

    colors = [
        ("#FEF2F2", "#FCA5A5", "#991B1B"),
        ("#FFF7ED", "#FDBA74", "#9A3412"),
        ("#FEFCE8", "#FDE047", "#854D0E"),
        ("#F0FDF4", "#86EFAC", "#166534"),
        ("#F0F9FF", "#7DD3FC", "#075985"),
        ("#EEF2FF", "#A5B4FC", "#3730A3"),
        ("#FAF5FF", "#D8B4FE", "#6B21A8"),
        ("#FFF1F2", "#FDA4AF", "#9F1239"),
    ]
    
    for idx, item in enumerate(entries):
        start_min = _time_to_minutes(item.start_time)
        end_min = _time_to_minutes(item.end_time)
        x1 = int(margin_left + (item.day_of_week - 1) * day_width + 8)
        x2 = int(margin_left + item.day_of_week * day_width - 8)
        y1 = int(margin_top + (start_min - axis_start) * minute_height + 4)
        y2 = int(margin_top + (end_min - axis_start) * minute_height - 4)
        
        bg_color, border_color, text_color = colors[idx % len(colors)]
        
        draw.rounded_rectangle((x1+2, y1+4, x2+2, max(y1 + 40, y2)+4), radius=12, fill="#E2E8F0")
        draw.rounded_rectangle((x1, y1, x2, max(y1 + 40, y2)), radius=12, fill=bg_color, outline=border_color, width=2)
        
        name_lines = _wrap_text(draw, item.course_name, text_font, max(40, x2 - x1 - 20))
        subtitle = f"{item.start_time}-{item.end_time}" + (f" @{item.location}" if item.location else "")
        sub_lines = _wrap_text(draw, subtitle, small_font, max(40, x2 - x1 - 20))
        
        cursor_y = y1 + 12
        for line in name_lines[:3]:
            draw.text((x1 + 12, cursor_y), line, fill=text_color, font=text_font)
            cursor_y += 28
        cursor_y += 4
        for line in sub_lines[:2]:
            draw.text((x1 + 12, cursor_y), line, fill=text_color, font=small_font)
            cursor_y += 22

    output = io.BytesIO()
    img.convert("RGB").save(output, format="PNG")
    return output.getvalue()


def _stable_quote_for_user(user_id: str) -> str:
    digits = "".join(ch for ch in str(user_id) if ch.isdigit())
    seed = int(digits[-6:]) if digits else 0
    seed += int(time.time() // 86400)
    return GROUP_STUDY_QUOTES[seed % len(GROUP_STUDY_QUOTES)]


def _safe_member_name(member: Dict[str, Any]) -> str:
    card = str(member.get("card") or "").strip()
    if card:
        return card
    nickname = str(member.get("nickname") or "").strip()
    if nickname:
        return nickname
    return str(member.get("user_id") or "群友")


def _clip_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
) -> str:
    content = str(text or "").strip()
    if not content:
        return ""
    bbox = draw.textbbox((0, 0), content, font=font)
    if int(bbox[2] - bbox[0]) <= max_width:
        return content
    ellipsis = "..."
    current = ""
    for ch in content:
        trial = current + ch
        trial_bbox = draw.textbbox((0, 0), trial + ellipsis, font=font)
        if int(trial_bbox[2] - trial_bbox[0]) > max_width:
            break
        current = trial
    return (current + ellipsis) if current else ellipsis


def _fallback_avatar(name: str, size: int, user_id: str = "") -> Image.Image:
    digits = "".join(ch for ch in str(user_id) if ch.isdigit())
    base = int(digits[-5:]) if digits else sum(ord(ch) for ch in name)
    palette = [
        "#4f9cf9",
        "#00a8a8",
        "#59b368",
        "#f4a259",
        "#e76f51",
        "#9b5de5",
        "#3a86ff",
    ]
    bg_color = palette[base % len(palette)]
    img = Image.new("RGB", (size, size), bg_color)
    draw = ImageDraw.Draw(img)
    font = _load_font(max(14, int(size * 0.38)))
    first = (name[:1] or "友").upper()
    bbox = draw.textbbox((0, 0), first, font=font)
    tw = int(bbox[2] - bbox[0])
    th = int(bbox[3] - bbox[1])
    draw.text(
        ((size - tw) // 2, (size - th) // 2 - 2),
        first,
        fill="#ffffff",
        font=font,
    )
    return img


def _circle_avatar(image: Image.Image, size: int) -> Image.Image:
    avatar = image.convert("RGB").resize((size, size))
    mask = Image.new("L", (size, size), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.ellipse((0, 0, size - 1, size - 1), fill=255)
    output = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    output.paste(avatar, (0, 0), mask)
    return output


async def _download_avatar_bytes(
    http_client: httpx.AsyncClient,
    user_id: str,
) -> Optional[bytes]:
    avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=100"
    try:
        response = await http_client.get(avatar_url)
        response.raise_for_status()
        data = bytes(response.content)
        if len(data) < 20:
            return None
        return data
    except Exception:
        return None


async def _fetch_group_avatar_map(user_ids: List[str]) -> Dict[str, bytes]:
    unique_ids = [uid for uid in dict.fromkeys(user_ids) if uid]
    if not unique_ids:
        return {}
    timeout = httpx.Timeout(group_avatar_timeout_seconds)
    sem = asyncio.Semaphore(max(4, group_course_query_parallel))
    result: Dict[str, bytes] = {}
    async with httpx.AsyncClient(timeout=timeout) as http_client:
        async def _worker(uid: str) -> None:
            async with sem:
                data = await _download_avatar_bytes(http_client, uid)
                if data:
                    result[uid] = data

        await asyncio.gather(*[_worker(uid) for uid in unique_ids])
    return result


async def _build_group_course_rows(
    members: List[Dict[str, Any]],
    *,
    self_id: str,
) -> List[Dict[str, Any]]:
    sem = asyncio.Semaphore(group_course_query_parallel)
    rows: List[Dict[str, Any]] = []

    async def _worker(member: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        user_id = str(member.get("user_id") or "").strip()
        if not user_id or user_id == self_id:
            return None
        name = _safe_member_name(member)
        quote = _stable_quote_for_user(user_id)

        async with sem:
            active = await asyncio.to_thread(get_active_course, user_id)
            if active:
                location = f" @{active.location}" if active.location else ""
                return {
                    "user_id": user_id,
                    "name": name,
                    "status_key": "active",
                    "status_order": 0,
                    "status_label": "上课中",
                    "badge_color": "#2f8f61",
                    "today_label": "今日正在上课",
                    "course_line": f"{active.start_time}-{active.end_time} 《{active.course_name}》{location}",
                    "quote": quote,
                }

            next_item = await asyncio.to_thread(get_next_course, user_id)
            if next_item:
                next_course, day_offset = next_item
                location = f" @{next_course.location}" if next_course.location else ""
                if day_offset == 0:
                    return {
                        "user_id": user_id,
                        "name": name,
                        "status_key": "pending",
                        "status_order": 1,
                        "status_label": "待上课",
                        "badge_color": "#d98c2f",
                        "today_label": "今天稍后有课",
                        "course_line": f"{next_course.start_time} 开始《{next_course.course_name}》{location}",
                        "quote": quote,
                    }
                return {
                    "user_id": user_id,
                    "name": name,
                    "status_key": "today_none",
                    "status_order": 2,
                    "status_label": "今日无课",
                    "badge_color": "#8b8f9c",
                    "today_label": "今日无课程",
                    "course_line": f"{day_offset}天后 {next_course.start_time}《{next_course.course_name}》{location}",
                    "quote": quote,
                }

            has_schedule = await asyncio.to_thread(has_user_schedule, user_id)
            if has_schedule:
                return {
                    "user_id": user_id,
                    "name": name,
                    "status_key": "today_none",
                    "status_order": 2,
                    "status_label": "今日无课",
                    "badge_color": "#8b8f9c",
                    "today_label": "今日无课程",
                    "course_line": "本周暂无后续课程",
                    "quote": quote,
                }
            return {
                "user_id": user_id,
                "name": name,
                "status_key": "no_schedule",
                "status_order": 3,
                "status_label": "无课表",
                "badge_color": "#a0a5b3",
                "today_label": "未导入课表",
                "course_line": "可发送 /导入课表 录入",
                "quote": quote,
            }

    results = await asyncio.gather(*[_worker(member) for member in members])
    for item in results:
        if item:
            rows.append(item)
    rows.sort(key=lambda x: (int(x["status_order"]), str(x["name"]).lower(), str(x["user_id"])))
    return rows


def _render_group_course_image(
    rows: List[Dict[str, Any]],
    avatar_map: Dict[str, bytes],
    *,
    title: str = "群组成员课程状态",
) -> bytes:
    width = 1080
    header_height = 200
    row_height = 140
    footer_height = 80
    count = max(1, len(rows))
    height = header_height + count * row_height + footer_height

    img = Image.new("RGBA", (width, height), "#F8FAFC")
    draw = ImageDraw.Draw(img)

    title_font = _load_font(52)
    name_font = _load_font(36)
    badge_font = _load_font(24)
    meta_font = _load_font(28)
    quote_font = _load_font(24)
    footer_font = _load_font(20)

    draw.rectangle((0, 0, width, header_height), fill="#0F172A")
    draw.text((60, 65), title, fill="#F8FAFC", font=title_font)
    draw.text((60, 135), f"统计人数：{len(rows)} 人", fill="#94A3B8", font=meta_font)

    avatar_size = 84
    x_card = 40
    
    if not rows:
        draw.text((x_card, header_height + 40), "当前群内暂无成员可统计。", fill="#64748B", font=name_font)
        
    for idx, row in enumerate(rows):
        y = header_height + 20 + idx * row_height
        
        draw.rounded_rectangle((x_card + 4, y + 8, width - x_card + 4, y + row_height - 12), radius=16, fill="#E2E8F0")
        draw.rounded_rectangle((x_card, y, width - x_card, y + row_height - 20), radius=16, fill="#FFFFFF", outline="#E2E8F0", width=1)

        user_id = str(row.get("user_id") or "")
        avatar_data = avatar_map.get(user_id)
        if avatar_data:
            try:
                avatar_img = Image.open(io.BytesIO(avatar_data))
                avatar = _circle_avatar(avatar_img, avatar_size)
            except Exception:
                avatar = _circle_avatar(_fallback_avatar(str(row.get("name") or "群友"), avatar_size, user_id), avatar_size)
        else:
            avatar = _circle_avatar(_fallback_avatar(str(row.get("name") or "群友"), avatar_size, user_id), avatar_size)
        
        img.paste(avatar, (x_card + 24, y + 18), mask=avatar)

        x_name = x_card + 130
        name = _clip_text(draw, str(row.get("name") or "群友"), name_font, 260)
        draw.text((x_name, y + 20), name, fill="#0F172A", font=name_font)

        status_key = row.get("status_key")
        status_label = str(row.get("status_label") or "状态")
        
        if status_key == "active":
            badge_bg, badge_fg = "#DCFCE7", "#166534"
        elif status_key == "pending":
            badge_bg, badge_fg = "#FEF9C3", "#854D0E"
        elif status_key == "today_none":
            badge_bg, badge_fg = "#F1F5F9", "#475569"
        elif status_key == "no_schedule":
            badge_bg, badge_fg = "#F1F5F9", "#64748B"
        else:
            badge_bg, badge_fg = "#F3F4F6", "#374151"

        badge_w = int(draw.textbbox((0, 0), status_label, font=badge_font)[2] + 24)
        draw.rounded_rectangle((x_name, y + 70, x_name + badge_w, y + 100), radius=15, fill=badge_bg)
        draw.text((x_name + 12, y + 72), status_label, fill=badge_fg, font=badge_font)

        x_meta = x_card + 430
        
        course_line = _clip_text(draw, str(row.get("course_line") or ""), meta_font, width - x_meta - 40)
        draw.text((x_meta, y + 24), course_line, fill="#334155", font=meta_font)
        
        quote = _clip_text(draw, str(row.get("quote") or ""), quote_font, width - x_meta - 40)
        draw.text((x_meta, y + 72), quote, fill="#94A3B8", font=quote_font)

    footer_text = f"Generated by QQ Bot  •  {time.strftime('%Y-%m-%d %H:%M:%S')}  •  发送 /导入课表 录入您的课程"
    bbox = draw.textbbox((0, 0), footer_text, font=footer_font)
    tw = bbox[2] - bbox[0]
    draw.text(((width - tw) // 2, height - 50), footer_text, fill="#94A3B8", font=footer_font)

    output = io.BytesIO()
    img.convert("RGB").save(output, format="PNG")
    return output.getvalue()


def _first_url(text: str) -> str:
    match = URL_PATTERN.search(str(text or ""))
    if not match:
        return ""
    return match.group(1).strip()


def _first_file_uri(text: str) -> str:
    match = FILE_URI_PATTERN.search(str(text or ""))
    if not match:
        return ""
    return match.group(1).strip()


def _first_windows_path(text: str) -> str:
    match = WINDOWS_PATH_PATTERN.search(str(text or ""))
    if not match:
        return ""
    return match.group(1).strip()


def _build_schedule_summary(entries: List[ScheduleEntry]) -> str:
    grouped: Dict[int, List[ScheduleEntry]] = {idx: [] for idx in range(1, 8)}
    for item in entries:
        grouped[item.day_of_week].append(item)
    lines: List[str] = []
    for day in range(1, 8):
        rows = grouped[day]
        if not rows:
            continue
        lines.append(f"{weekday_label(day)}：")
        for row in rows:
            location = f" @ {row.location}" if row.location else ""
            week_hint = ""
            if row.start_week > 1 or row.end_week > 1 or row.week_type in {1, 2}:
                parity = ""
                if row.week_type == 1:
                    parity = " 单周"
                elif row.week_type == 2:
                    parity = " 双周"
                week_hint = f" [{row.start_week}-{row.end_week}周{parity}]"
            lines.append(f"- {row.start_time}-{row.end_time} {row.course_name}{location}{week_hint}")
    return "\n".join(lines) if lines else "当前没有课程。"


def _fallback_reminder(course: ScheduleEntry) -> str:
    return random.choice(
        [
            f"主、主人…你现在在上《{course.course_name}》喵，先认真听课，千夏等你下课。",
            f"主人…《{course.course_name}》正在进行中喵，先收心听讲好吗？",
            f"呜…现在是《{course.course_name}》时间喵，先把注意力放回课堂吧。",
        ]
    )


def _clean_reminder_text(text: str) -> str:
    plain = str(text or "").replace("\n", " ").strip().strip("'").strip('"')
    plain = re.sub(r"\s+", " ", plain)
    if len(plain) > 60:
        plain = plain[:60].rstrip("，,。.!！?？") + "…"
    return plain


async def _generate_reminder(course: ScheduleEntry, query_text: str) -> str:
    fallback = _fallback_reminder(course)
    if not _llm_available():
        return fallback
    prompt = (
        f"课程：{weekday_label(course.day_of_week)} {course.start_time}-{course.end_time}《{course.course_name}》"
        f"{('，地点：' + course.location) if course.location else ''}\n"
        f"用户刚刚说：{_truncate_text(query_text, max_chars=80) or '用户触发了机器人功能'}\n\n"
        "请输出一句自然劝学提醒，必须符合害羞女仆猫娘口吻，称呼“主人”，20-45字，只输出一句话。"
    )
    try:
        if llm_provider == "gemini":
            text = await _gemini_generate(
                contents=[{"role": "user", "parts": [{"text": prompt}]}],
                system_instruction="你是女仆猫娘千夏，只输出一句自然劝学提醒。",
                max_output_tokens=120,
            )
        else:
            if client is None:
                return fallback
            response = await client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": "你是女仆猫娘千夏，只输出一句自然劝学提醒。"},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=120,
            )
            text = (response.choices[0].message.content or "").strip()
        return _clean_reminder_text(text) or fallback
    except Exception as e:
        logger.warning("Generate study reminder failed: %s", e)
        return fallback


async def _get_cached_reminder(user_id: str, course: ScheduleEntry, query_text: str) -> str:
    now_ts = time.time()
    course_key = f"{course.day_of_week}:{course.start_time}:{course.end_time}:{course.course_name}:{course.location}"
    cached = reminder_cache.get(user_id)
    if cached:
        cached_ts, cached_course_key, cached_text = cached
        if cached_course_key == course_key and now_ts - cached_ts <= study_reminder_cache_seconds:
            return cached_text
    reminder = await _generate_reminder(course, query_text)
    reminder_cache[user_id] = (now_ts, course_key, reminder)
    return reminder


@event_preprocessor
async def _inject_study_context(bot: Bot, event: MessageEvent):
    clear_current_study_context()
    if not study_schedule_enabled or not study_reminder_enabled:
        return
    if not isinstance(event, MessageEvent):
        return
    if not _is_group_allowed(event):
        return
    if str(getattr(event, "user_id", "")) == str(getattr(bot, "self_id", "")):
        return
    course = await asyncio.to_thread(get_active_course, str(event.user_id))
    if not course:
        return
    reminder = await _get_cached_reminder(str(event.user_id), course, event.get_plaintext().strip())
    if reminder:
        set_current_study_context(reminder, course)


@import_schedule_cmd.handle()
async def handle_import_schedule(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    if not study_schedule_enabled:
        await import_schedule_cmd.finish(_build_command_reply_message(event, "课表功能当前未启用。"))
    if not _is_group_allowed(event):
        await import_schedule_cmd.finish(_build_command_reply_message(event, "本群未开通此功能。"))

    args_text = args.extract_plain_text().strip()
    image_url, file_url = await _extract_media_from_context(bot, event, args)
    wakeup_token = await _extract_wakeup_token_from_context(bot, event, args)
    if not args_text and not image_url and not file_url and not wakeup_token:
        await import_schedule_cmd.finish(
            _build_command_reply_message(
                event,
                (
                    "用法：`/导入课表 WakeUp口令`（推荐）。"
                    "也支持文本/JSON、链接、图片、PDF 作为兜底导入。"
                ),
            )
        )

    try:
        source_desc = "WakeUp 口令"
        term_start_date: Optional[str] = None
        if wakeup_token:
            entries, term_start_date = await _fetch_wakeup_schedule(
                wakeup_token,
                user_id=str(event.user_id),
            )
        elif image_url:
            source_desc = "图片"
            entries = await _parse_schedule_from_url(image_url, user_id=str(event.user_id))
            term_start_date = None
        elif file_url:
            source_desc = "文件"
            entries = await _parse_schedule_from_url(file_url, user_id=str(event.user_id))
            term_start_date = None
        else:
            url = _first_url(args_text)
            if url:
                source_desc = "链接"
                entries = await _parse_schedule_from_url(url, user_id=str(event.user_id))
                term_start_date = None
            else:
                local_pdf = _first_file_uri(args_text) or _first_windows_path(args_text)
                if local_pdf:
                    source_desc = "本地文件路径"
                    entries = await _parse_schedule_from_url(local_pdf, user_id=str(event.user_id))
                    term_start_date = None
                else:
                    source_desc = "文本"
                    entries = await _parse_schedule_from_text(args_text, user_id=str(event.user_id))
                    term_start_date = None
    except Exception as e:
        logger.warning("Import schedule failed: %s", e)
        await import_schedule_cmd.finish(_build_command_reply_message(event, f"课表导入失败：{e}"))

    if not entries:
        extra_tip = "\n可直接发送 WakeUp 分享口令（32位字母数字）重试。"
        await import_schedule_cmd.finish(
            _build_command_reply_message(
                event,
                "没有识别到有效课程。" + extra_tip,
            )
        )

    saved = await asyncio.to_thread(
        replace_user_schedule,
        str(event.user_id),
        entries,
        term_start_date,
    )
    if saved <= 0:
        await import_schedule_cmd.finish(
            _build_command_reply_message(event, "课表导入失败：解析结果为空。")
        )

    current_entries = await asyncio.to_thread(list_user_schedule, str(event.user_id))
    try:
        image_bytes = await asyncio.to_thread(_render_schedule_image, current_entries)
        await import_schedule_cmd.send(_build_image_message(event, image_bytes))
    except Exception as e:
        logger.warning("Render schedule image failed: %s", e)

    active_course = get_current_study_course() or await asyncio.to_thread(get_active_course, str(event.user_id))
    active_tip = ""
    if active_course:
        active_tip = (
            f"\n当前你正在上《{active_course.course_name}》"
            f"（{active_course.start_time}-{active_course.end_time}）。"
        )
    week_tip = ""
    current_week = await asyncio.to_thread(get_user_current_week, str(event.user_id))
    term_start = await asyncio.to_thread(get_user_term_start_date, str(event.user_id))
    if current_week is not None and current_week > 0:
        week_tip = f"\n当前第 {current_week} 周。"
        if isinstance(term_start, date):
            week_tip += f" 学期起始日：{term_start.isoformat()}。"
    elif isinstance(term_start, date):
        week_tip = f"\n学期起始日：{term_start.isoformat()}。"
    await import_schedule_cmd.finish(
        _build_command_reply_message(
            event,
            f"课表导入成功（来源：{source_desc}），共保存 {saved} 条课程。{week_tip}{active_tip}",
        )
    )


@show_schedule_cmd.handle()
async def handle_show_schedule(event: MessageEvent):
    if not study_schedule_enabled:
        await show_schedule_cmd.finish(_build_command_reply_message(event, "课表功能当前未启用。"))
    if not _is_group_allowed(event):
        await show_schedule_cmd.finish(_build_command_reply_message(event, "本群未开通此功能。"))
    entries = await asyncio.to_thread(list_user_schedule, str(event.user_id))
    if not entries:
        await show_schedule_cmd.finish(
            _build_command_reply_message(event, "你还没有课表，先用 `/导入课表` 导入。")
        )
    try:
        image_bytes = await asyncio.to_thread(_render_schedule_image, entries)
        await show_schedule_cmd.send(_build_image_message(event, image_bytes))
    except Exception as e:
        logger.warning("Render schedule image failed: %s", e)
    await show_schedule_cmd.finish(_build_command_reply_message(event, _build_schedule_summary(entries)))


@clear_schedule_cmd.handle()
async def handle_clear_schedule(event: MessageEvent):
    if not study_schedule_enabled:
        await clear_schedule_cmd.finish(_build_command_reply_message(event, "课表功能当前未启用。"))
    if not _is_group_allowed(event):
        await clear_schedule_cmd.finish(_build_command_reply_message(event, "本群未开通此功能。"))
    deleted = await asyncio.to_thread(clear_user_schedule, str(event.user_id))
    if deleted <= 0:
        await clear_schedule_cmd.finish(_build_command_reply_message(event, "你当前没有可清空的课表。"))
    await clear_schedule_cmd.finish(_build_command_reply_message(event, "已清空你的课表记录。"))


@status_schedule_cmd.handle()
async def handle_schedule_status(event: MessageEvent):
    if not study_schedule_enabled:
        await status_schedule_cmd.finish(_build_command_reply_message(event, "课表功能当前未启用。"))
    if not _is_group_allowed(event):
        await status_schedule_cmd.finish(_build_command_reply_message(event, "本群未开通此功能。"))
    current_week = await asyncio.to_thread(get_user_current_week, str(event.user_id))
    week_prefix = f"第 {current_week} 周，" if (current_week is not None and current_week > 0) else ""
    active = await asyncio.to_thread(get_active_course, str(event.user_id))
    if active:
        location = f" @ {active.location}" if active.location else ""
        await status_schedule_cmd.finish(
            _build_command_reply_message(
                event,
                f"{week_prefix}你现在正在上《{active.course_name}》"
                f"（{weekday_label(active.day_of_week)} {active.start_time}-{active.end_time}{location}）。",
            )
        )

    next_item = await asyncio.to_thread(get_next_course, str(event.user_id))
    if not next_item:
        await status_schedule_cmd.finish(
            _build_command_reply_message(event, "你当前不在上课，也没有后续课程（或尚未导入课表）。")
        )
    course, day_offset = next_item
    when = "今天稍后" if day_offset == 0 else f"{day_offset} 天后"
    location = f" @ {course.location}" if course.location else ""
    await status_schedule_cmd.finish(
        _build_command_reply_message(
            event,
            f"{week_prefix}你当前不在上课。下一节是 {when} 的《{course.course_name}》"
            f"（{weekday_label(course.day_of_week)} {course.start_time}-{course.end_time}{location}）。",
        )
    )


@group_course_now_cmd.handle()
async def handle_group_course_now(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    if not study_schedule_enabled:
        await group_course_now_cmd.finish(_build_command_reply_message(event, "课表功能当前未启用。"))
    if not isinstance(event, GroupMessageEvent):
        await group_course_now_cmd.finish(_build_command_reply_message(event, "该命令仅支持在群聊中使用。"))
    if not _is_group_allowed(event):
        await group_course_now_cmd.finish(_build_command_reply_message(event, "本群未开通此功能。"))

    arg_text = args.extract_plain_text().strip()
    render_limit = group_course_image_limit
    if arg_text:
        match = re.search(r"\d{1,3}", arg_text)
        if match:
            render_limit = max(1, min(group_course_image_limit, int(match.group(0))))

    try:
        raw_members = await bot.get_group_member_list(group_id=event.group_id)
    except Exception as e:
        logger.warning("Get group member list failed: %s", e)
        await group_course_now_cmd.finish(_build_command_reply_message(event, f"获取群成员失败：{e}"))

    members: List[Dict[str, Any]] = []
    if isinstance(raw_members, list):
        for item in raw_members:
            if isinstance(item, dict):
                members.append(item)
    if not members:
        await group_course_now_cmd.finish(_build_command_reply_message(event, "当前群成员列表为空，稍后再试。"))

    rows = await _build_group_course_rows(members, self_id=str(getattr(bot, "self_id", "")))
    if not rows:
        await group_course_now_cmd.finish(_build_command_reply_message(event, "当前没有可统计的群友数据。"))

    show_rows = rows[:render_limit]
    avatar_map = await _fetch_group_avatar_map([str(item.get("user_id") or "") for item in show_rows])

    title = "群友在上什么课？"
    try:
        group_info = await bot.get_group_info(group_id=event.group_id, no_cache=True)
        group_name = str(group_info.get("group_name") or "").strip()
        if group_name:
            title = f"{group_name} 在上什么课？"
    except Exception:
        pass

    try:
        image_bytes = await asyncio.to_thread(
            _render_group_course_image,
            show_rows,
            avatar_map,
            title=title,
        )
        await group_course_now_cmd.send(_build_image_message(event, image_bytes))
    except Exception as e:
        logger.warning("Render group course image failed: %s", e)
        await group_course_now_cmd.finish(_build_command_reply_message(event, f"生成课表图失败：{e}"))

    active_count = sum(1 for row in rows if row.get("status_key") == "active")
    pending_count = sum(1 for row in rows if row.get("status_key") == "pending")
    today_none_count = sum(1 for row in rows if row.get("status_key") == "today_none")
    no_schedule_count = sum(1 for row in rows if row.get("status_key") == "no_schedule")
    summary = (
        f"已统计 {len(rows)} 位群友：上课中 {active_count}，待上课 {pending_count}，"
        f"今日无课 {today_none_count}，无课表 {no_schedule_count}。"
    )
    if len(rows) > len(show_rows):
        summary += (
            f"\n图片仅展示前 {len(show_rows)} 位。"
            f"可用 `/群友上什么课 {group_course_image_limit}` 查看最大展示数。"
        )
    await group_course_now_cmd.finish(_build_command_reply_message(event, summary))


try:
    init_study_db()
except Exception as e:
    logger.error("Init study DB failed: %s", e)
