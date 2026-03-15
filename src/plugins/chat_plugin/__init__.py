import asyncio
import base64
import html
import io
import re
import sqlite3
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

import httpx
from nonebot import get_driver, on_command, on_message
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
)
from nonebot.log import logger
from nonebot.params import CommandArg
from openai import AsyncOpenAI
from PIL import Image
from src.utils.roleplay import with_roleplay
from src.utils.study import get_current_study_course

try:
    from duckduckgo_search import DDGS
except Exception:
    DDGS = None

config = get_driver().config


def _parse_csv(value: object) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).replace("，", ",")
    return [item.strip() for item in text.split(",") if item.strip()]


def _parse_int_set(value: object) -> Set[int]:
    result: Set[int] = set()
    for item in _parse_csv(value):
        try:
            result.add(int(item))
        except ValueError:
            logger.warning("Invalid group id in config: %s", item)
    return result


def _parse_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _normalize_prefixes(value: object) -> List[str]:
    prefixes = _parse_csv(value)
    if not prefixes:
        prefixes = ["!", "/"]
    return prefixes


api_key = getattr(config, "llm_api_key", "")
base_url = getattr(config, "llm_base_url", "https://api.deepseek.com/v1")
model_name = getattr(config, "llm_model", "deepseek-chat")
llm_provider = str(getattr(config, "llm_provider", "openai")).strip().lower()
gemini_api_key = str(getattr(config, "gemini_api_key", "") or api_key).strip()
gemini_model_name = str(getattr(config, "gemini_model", model_name or "gemini-2.5-flash")).strip()
gemini_thinking_budget = int(getattr(config, "gemini_thinking_budget", 0))
system_prompt = getattr(
    config,
    "llm_system_prompt",
    "你是一个有用的QQ群聊助手，回答要简短、口语化，不要带有违规或敏感词。",
)
memory_turns = int(getattr(config, "llm_memory_turns", 8))
session_expire_seconds = int(getattr(config, "llm_session_expire_seconds", 600))
llm_timeout_seconds = float(getattr(config, "llm_timeout_seconds", 30))
chat_cooldown_seconds = float(getattr(config, "chat_cooldown_seconds", 2))

web_search_enabled = _parse_bool(getattr(config, "web_search_enabled", True), True)
web_search_topk = int(getattr(config, "web_search_topk", 5))
web_search_timeout_seconds = float(getattr(config, "web_search_timeout_seconds", 15))
web_search_auto = _parse_bool(getattr(config, "web_search_auto", True), True)
google_news_hl = str(getattr(config, "google_news_hl", "zh-CN"))
google_news_gl = str(getattr(config, "google_news_gl", "CN"))
google_news_ceid = str(getattr(config, "google_news_ceid", "CN:zh-Hans"))
serper_api_key = str(getattr(config, "serper_api_key", "")).strip()

summary_buffer_limit = int(getattr(config, "summary_buffer_limit", 200))
summary_default_take = int(getattr(config, "summary_default_take", 80))

memory_db_path = Path(getattr(config, "memory_db_path", "data/bot_memory.db"))
user_memory_enabled = _parse_bool(getattr(config, "user_memory_enabled", True), True)
memory_update_every = int(getattr(config, "memory_update_every", 50))
memory_extract_limit = int(getattr(config, "memory_extract_limit", 40))

chat_prefixes = _normalize_prefixes(getattr(config, "chat_prefixes", "!,/"))
group_whitelist = _parse_int_set(getattr(config, "group_whitelist", ""))
group_blacklist = _parse_int_set(getattr(config, "group_blacklist", ""))
ignored_prefixed_commands = {
    "draw",
    "画",
    "图生图",
    "img2img",
    "超分",
    "总结",
    "summary",
    "搜索",
    "search",
    "联网",
    "订阅早报",
    "退订早报",
    "订阅摸鱼图",
    "退订摸鱼图",
    "帮助",
    "help",
    "list",
    "菜单",
    "命令",
    "模型",
    "lora",
    "识图",
    "看图",
    "vision",
    "导入课表",
    "课表导入",
    "上传课表",
    "课表",
    "查看课表",
    "我的课表",
    "清空课表",
    "删除课表",
    "重置课表",
    "上课状态",
    "现在上什么",
    "当前课程",
    "群友上什么课",
    "群友上课",
    "谁在上课",
    "群课表状态",
}

client = AsyncOpenAI(api_key=api_key, base_url=base_url) if (api_key and llm_provider != "gemini") else None

# session_id -> OpenAI messages
history: Dict[str, List[dict]] = {}
last_active: Dict[str, float] = {}
last_chat_ts: Dict[str, float] = {}
group_recent_messages: Dict[int, Deque[dict]] = {}
user_msg_counter: Dict[str, int] = {}
refreshing_users: Set[str] = set()

chat = on_message(priority=10, block=False)
summary_cmd = on_command("总结", aliases={"summary"}, priority=6, block=True)
search_cmd = on_command("搜索", aliases={"search", "联网"}, priority=6, block=True)
help_cmd = on_command("帮助", aliases={"help"}, priority=6, block=True)
list_cmd = on_command("list", aliases={"菜单", "命令"}, priority=6, block=True)
vision_cmd = on_command("识图", aliases={"看图", "vision"}, priority=6, block=True)
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def _build_help_text() -> str:
    return (
        "可用命令如下：\n"
        "\n"
        "聊天：\n"
        "- `@机器人 你好`\n"
        "- `!你好` 或 `/你好`\n"
        "\n"
        "文生图：\n"
        "- `/画 提示词` 或 `/draw prompt`\n"
        "- 横图可加：`横图` 或 `--landscape`\n"
        "\n"
        "图生图：\n"
        "- `/图生图 提示词`（消息里带图或回复图片）\n"
        "- 降噪：`--denoise 0.7`（必须 >0 且 <1）\n"
        "- 识图：`/识图 这是什么`（消息里带图或回复图片）\n"
        "- 自动识图：`@机器人` 的消息里只要有图片就会自动识别\n"
        "\n"
        "模型与LoRA：\n"
        "- `/模型 列表`、`/模型 模型名`\n"
        "- `/lora 列表`、`/lora 名称`\n"
        "- `/lora 开启`、`/lora 关闭`\n"
        "\n"
        "检索与总结：\n"
        "- `/搜索 关键词`\n"
        "- `/总结`\n"
        "\n"
        "订阅推送：\n"
        "- `/订阅早报`、`/退订早报`\n"
        "- `/订阅摸鱼图`、`/退订摸鱼图`\n"
        "- `/测试早报`、`/测试摸鱼图`\n"
        "\n"
        "课表与提醒：\n"
        "- `/导入课表 WakeUp口令`（推荐，支持32位口令）\n"
        "- `/导入课表` 仍支持文本/链接/图片/PDF 兜底导入\n"
        "- `/课表`、`/清空课表`\n"
        "- `/上课状态`\n"
        "- `/群友上什么课 [数量]`（生成群成员当前上课状态长图）\n"
        "\n"
        "帮助命令：`/帮助`、`/help`、`/list`"
    )


def _init_db() -> None:
    memory_db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(memory_db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_archive (
                user_id TEXT PRIMARY KEY,
                nickname TEXT,
                preferences TEXT,
                summary_memory TEXT,
                updated_at INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dialogue_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                user_id TEXT,
                group_id TEXT,
                role TEXT,
                content TEXT,
                created_at INTEGER
            )
            """
        )
        conn.commit()


def _db_get_user_summary(user_id: str) -> str:
    with sqlite3.connect(memory_db_path) as conn:
        row = conn.execute(
            "SELECT summary_memory FROM user_archive WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row or not row[0]:
            return ""
        return str(row[0]).strip()


def _db_upsert_user_archive(user_id: str, nickname: str) -> None:
    now_ts = int(time.time())
    with sqlite3.connect(memory_db_path) as conn:
        conn.execute(
            """
            INSERT INTO user_archive (user_id, nickname, preferences, summary_memory, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                nickname = excluded.nickname,
                updated_at = excluded.updated_at
            """,
            (user_id, nickname, "{}", "", now_ts),
        )
        conn.commit()


def _db_append_dialogue(
    session_id: str,
    user_id: str,
    group_id: str,
    role: str,
    content: str,
) -> None:
    now_ts = int(time.time())
    with sqlite3.connect(memory_db_path) as conn:
        conn.execute(
            """
            INSERT INTO dialogue_log (session_id, user_id, group_id, role, content, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (session_id, user_id, group_id, role, content, now_ts),
        )
        conn.commit()


def _db_recent_user_messages(user_id: str, limit: int) -> List[str]:
    with sqlite3.connect(memory_db_path) as conn:
        rows = conn.execute(
            """
            SELECT role, content FROM dialogue_log
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    lines: List[str] = []
    for role, content in reversed(rows):
        if not content:
            continue
        speaker = "用户" if role == "user" else "机器人"
        lines.append(f"{speaker}: {content}")
    return lines


def _db_update_user_summary(user_id: str, summary_text: str) -> None:
    with sqlite3.connect(memory_db_path) as conn:
        conn.execute(
            """
            UPDATE user_archive
            SET summary_memory = ?, updated_at = ?
            WHERE user_id = ?
            """,
            (summary_text, int(time.time()), user_id),
        )
        conn.commit()


def _build_reply_message(event: MessageEvent, text: str) -> Message:
    if isinstance(event, GroupMessageEvent):
        return MessageSegment.reply(event.message_id) + Message(text)
    return Message(text)


def _llm_available() -> bool:
    if llm_provider == "gemini":
        return bool(gemini_api_key)
    return client is not None


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


def _to_gemini_contents(messages: List[dict]) -> Tuple[str, List[dict]]:
    system_chunks: List[str] = []
    contents: List[dict] = []

    for msg in messages:
        role = str(msg.get("role", "user")).strip().lower()
        text = str(msg.get("content", "")).strip()
        if not text:
            continue
        if role == "system":
            system_chunks.append(text)
            continue
        gemini_role = "model" if role == "assistant" else "user"
        contents.append({"role": gemini_role, "parts": [{"text": text}]})

    if not contents:
        contents = [{"role": "user", "parts": [{"text": "请继续。"}]}]

    return "\n\n".join(system_chunks).strip(), contents


async def _gemini_generate_content(
    *,
    model: str,
    contents: List[dict],
    max_output_tokens: int,
    system_instruction_text: str = "",
) -> str:
    if not gemini_api_key:
        return ""
    payload: Dict[str, Any] = {"contents": contents}
    if system_instruction_text:
        payload["system_instruction"] = {"parts": [{"text": system_instruction_text}]}
    if max_output_tokens > 0:
        generation_config: Dict[str, Any] = {"maxOutputTokens": max_output_tokens}
        if "2.5" in model and gemini_thinking_budget >= 0:
            generation_config["thinkingConfig"] = {"thinkingBudget": gemini_thinking_budget}
        payload["generationConfig"] = generation_config

    headers = {"Content-Type": "application/json", "x-goog-api-key": gemini_api_key}
    timeout = httpx.Timeout(llm_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as http_client:
        response = await http_client.post(
            _gemini_endpoint(model),
            json=payload,
            headers=headers,
        )
        response.raise_for_status()
        data = response.json()
    return _extract_gemini_text(data)


async def _llm_chat(messages: List[dict], max_tokens: int) -> str:
    if llm_provider == "gemini":
        system_text, contents = _to_gemini_contents(messages)
        return await _gemini_generate_content(
            model=gemini_model_name or model_name,
            contents=contents,
            max_output_tokens=max_tokens,
            system_instruction_text=system_text,
        )

    if client is None:
        return ""
    response = await client.chat.completions.create(
        model=model_name,
        messages=messages,
        max_tokens=max_tokens,
    )
    return (response.choices[0].message.content or "").strip()


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


async def _extract_image_url_from_context(
    bot: Bot,
    event: MessageEvent,
    args: Message,
) -> Optional[str]:
    direct = await _extract_image_url_with_file_fallback(bot, args)
    if not direct:
        direct = await _extract_image_url_with_file_fallback(bot, event.message)
    if direct:
        return direct

    reply_obj = getattr(event, "reply", None)
    if reply_obj:
        reply_message = getattr(reply_obj, "message", None)
        if reply_message is not None:
            reply_message_obj = (
                reply_message if isinstance(reply_message, Message) else Message(reply_message)
            )
            reply_url = await _extract_image_url_with_file_fallback(bot, reply_message_obj)
            if reply_url:
                return reply_url
        reply_message_id = getattr(reply_obj, "message_id", None)
        if reply_message_id is not None:
            try:
                reply_msg = await bot.get_msg(message_id=int(reply_message_id))
                raw_message = reply_msg.get("message")
                if raw_message is not None:
                    message_obj = raw_message if isinstance(raw_message, Message) else Message(raw_message)
                    reply_url = await _extract_image_url_with_file_fallback(bot, message_obj)
                    if reply_url:
                        return reply_url
            except Exception as e:
                logger.debug("Resolve event.reply message failed: %s", e)

    reply_id = _extract_reply_message_id(event.message)
    if not reply_id:
        return None
    try:
        reply_msg = await bot.get_msg(message_id=reply_id)
        raw_message = reply_msg.get("message")
        if raw_message is None:
            return None
        message_obj = raw_message if isinstance(raw_message, Message) else Message(raw_message)
        return await _extract_image_url_with_file_fallback(bot, message_obj)
    except Exception as e:
        logger.warning("Resolve replied image failed: %s", e)
        return None


def _detect_image_mime(image_bytes: bytes, header_content_type: str) -> str:
    content_type = (header_content_type or "").split(";")[0].strip().lower()
    if content_type.startswith("image/"):
        return content_type

    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
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
    return mapping.get(fmt, "image/jpeg")


async def _gemini_vision_analyze(image_url: str, ask: str) -> str:
    prompt_text = ask.strip() or "请用中文简要描述这张图片，并指出关键信息。"

    if llm_provider != "gemini" and client is not None:
        response = await client.chat.completions.create(
            model=model_name,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你现在扮演“千夏”女仆猫娘，为主人识图。"
                        "请先准确识别图片事实，再用自然口语化女仆语气回答；"
                        "称呼对方为“主人”，可少量使用“喵~”和害羞停顿语气；"
                        "不要编造看不到的细节。"
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt_text},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                },
            ],
            max_tokens=800,
        )
        return (response.choices[0].message.content or "").strip()

    if not gemini_api_key:
        raise RuntimeError("未配置 Gemini API Key。请在 .env 设置 `GEMINI_API_KEY`。")

    timeout = httpx.Timeout(llm_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as http_client:
        image_resp = await http_client.get(image_url)
        image_resp.raise_for_status()
    image_bytes = image_resp.content
    if not image_bytes:
        raise RuntimeError("图片下载失败，未获取到有效内容。")

    mime_type = _detect_image_mime(image_bytes, image_resp.headers.get("Content-Type", ""))
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    contents = [
        {
            "role": "user",
            "parts": [
                {"text": prompt_text},
                {"inline_data": {"mime_type": mime_type, "data": image_b64}},
            ],
        }
    ]
    result = await _gemini_generate_content(
        model=gemini_model_name or model_name or "gemini-2.5-flash",
        contents=contents,
        max_output_tokens=800,
        system_instruction_text=(
            "你现在扮演“千夏”女仆猫娘，为主人识图。"
            "请先准确识别图片事实，再用自然口语化女仆语气回答；"
            "称呼对方为“主人”，可少量使用“喵~”和害羞停顿语气；"
            "不要编造看不到的细节。"
        ),
    )
    return result.strip()


def _event_nickname(event: MessageEvent) -> str:
    sender = getattr(event, "sender", None)
    if sender:
        card = getattr(sender, "card", "") or ""
        nick = getattr(sender, "nickname", "") or ""
        if card.strip():
            return card.strip()
        if nick.strip():
            return nick.strip()
    return str(event.user_id)


def _is_group_allowed(event: MessageEvent) -> bool:
    if not isinstance(event, GroupMessageEvent):
        return True
    group_id = event.group_id
    if group_whitelist and group_id not in group_whitelist:
        return False
    if group_id in group_blacklist:
        return False
    return True


def _extract_chat_query(event: MessageEvent) -> str:
    text = event.get_plaintext().strip()
    if not text:
        return ""

    if not isinstance(event, GroupMessageEvent):
        return text

    # to_me 触发（@机器人）
    if bool(getattr(event, "to_me", False)):
        return text

    # 前缀触发
    for prefix in chat_prefixes:
        if text.startswith(prefix):
            content = text[len(prefix) :].strip()
            if not content:
                return ""
            first_token = content.split(maxsplit=1)[0].lower()
            if first_token in ignored_prefixed_commands:
                return ""
            return content
    return ""


def _record_group_message(event: MessageEvent) -> None:
    if not isinstance(event, GroupMessageEvent):
        return
    parts: List[str] = []
    for segment in event.message:
        if segment.type == "text":
            text = str(segment.data.get("text", "")).strip()
            if text:
                parts.append(text)
        elif segment.type == "image":
            parts.append("[图片]")
    if not parts:
        return
    content = " ".join(parts).strip()
    if not content:
        return
    item = {
        "nickname": _event_nickname(event),
        "time": time.strftime("%H:%M", time.localtime()),
        "content": content,
    }
    buffer = group_recent_messages.get(event.group_id)
    if buffer is None:
        buffer = deque(maxlen=summary_buffer_limit)
        group_recent_messages[event.group_id] = buffer
    buffer.append(item)


def _session_id(event: MessageEvent) -> str:
    if isinstance(event, GroupMessageEvent):
        return f"group_{event.group_id}:user_{event.user_id}"
    return f"user_{event.user_id}"


def _cooldown_key(event: MessageEvent) -> str:
    if isinstance(event, GroupMessageEvent):
        return f"group_{event.group_id}:user_{event.user_id}"
    return f"user_{event.user_id}"


def _trim_history(messages: List[dict]) -> List[dict]:
    max_recent_messages = max(2, memory_turns * 2)
    if len(messages) <= max_recent_messages + 1:
        return messages
    return [messages[0]] + messages[-max_recent_messages:]


def _clear_expired_sessions(now_ts: float) -> None:
    if session_expire_seconds <= 0:
        return
    expired_sessions = [
        session_id
        for session_id, ts in last_active.items()
        if now_ts - ts > session_expire_seconds
    ]
    for session_id in expired_sessions:
        history.pop(session_id, None)
        last_active.pop(session_id, None)


def _is_summary_intent(text: str) -> bool:
    lower = text.lower()
    return (
        "今天聊了啥" in text
        or "总结一下" in text
        or lower.startswith("总结")
        or lower == "总结"
    )


def _should_auto_search(text: str) -> bool:
    if not web_search_auto:
        return False
    keywords = (
        "最新",
        "今天",
        "刚刚",
        "新闻",
        "实时",
        "价格",
        "汇率",
        "比分",
        "行情",
    )
    return any(word in text for word in keywords)


def _strip_html(raw: str) -> str:
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw, flags=re.IGNORECASE)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _is_news_query(text: str) -> bool:
    keywords = ("新闻", "早报", "今天", "最新", "实时", "快讯", "热点", "头条")
    return any(word in text for word in keywords)


def _normalize_result_url(href: str) -> str:
    href = html.unescape(href or "").strip()
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    if href.startswith("/"):
        href = "https://duckduckgo.com" + href
    try:
        parsed = urllib.parse.urlparse(href)
        if "duckduckgo.com" in parsed.netloc:
            q = urllib.parse.parse_qs(parsed.query)
            uddg = q.get("uddg", [])
            if uddg:
                return urllib.parse.unquote(uddg[0])
    except Exception:
        pass
    return href


def _dedupe_results(results: List[dict]) -> List[dict]:
    seen: Set[str] = set()
    out: List[dict] = []
    for item in results:
        href = str(item.get("href") or "").strip()
        key = href or str(item.get("title") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


async def _search_serper(query: str) -> List[dict]:
    if not serper_api_key:
        return []
    try:
        async with httpx.AsyncClient(timeout=web_search_timeout_seconds) as http_client:
            response = await http_client.post(
                "https://google.serper.dev/search",
                headers={
                    "X-API-KEY": serper_api_key,
                    "Content-Type": "application/json",
                },
                json={"q": query, "num": web_search_topk},
            )
            response.raise_for_status()
            payload = response.json()
        results: List[dict] = []
        for item in payload.get("organic", [])[:web_search_topk]:
            title = str(item.get("title", "")).strip()
            href = str(item.get("link", "")).strip()
            body = str(item.get("snippet", "")).strip()
            if title and href:
                results.append(
                    {"title": title, "href": href, "body": body, "source": "serper"}
                )
        return results
    except Exception as e:
        logger.warning("Serper search failed: %s", e)
        return []


async def _search_google_news_rss(query: str) -> List[dict]:
    q = urllib.parse.quote_plus(query)
    rss_url = (
        f"https://news.google.com/rss/search?q={q}"
        f"&hl={urllib.parse.quote_plus(google_news_hl)}"
        f"&gl={urllib.parse.quote_plus(google_news_gl)}"
        f"&ceid={urllib.parse.quote_plus(google_news_ceid)}"
    )
    try:
        async with httpx.AsyncClient(timeout=web_search_timeout_seconds) as http_client:
            response = await http_client.get(rss_url, headers=HTTP_HEADERS)
            response.raise_for_status()
        root = ET.fromstring(response.content)
        results: List[dict] = []
        for item in root.findall("./channel/item"):
            title = (item.findtext("title") or "").strip()
            href = (item.findtext("link") or "").strip()
            desc = _strip_html(item.findtext("description") or "")
            if title and href:
                results.append(
                    {
                        "title": title,
                        "href": href,
                        "body": desc,
                        "source": "google_news_rss",
                    }
                )
            if len(results) >= web_search_topk:
                break
        return results
    except Exception as e:
        logger.warning("Google News RSS search failed: %s", e)
        return []


async def _search_duckduckgo_lite(query: str) -> List[dict]:
    try:
        async with httpx.AsyncClient(timeout=web_search_timeout_seconds) as http_client:
            response = await http_client.get(
                "https://lite.duckduckgo.com/lite/",
                params={"q": query},
                headers=HTTP_HEADERS,
            )
            response.raise_for_status()
        text = response.text
        results: List[dict] = []
        for match in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', text, re.I | re.S):
            href = _normalize_result_url(match.group(1))
            title = _strip_html(match.group(2))
            if not title or not href or "duckduckgo.com" in href:
                continue
            results.append(
                {"title": title, "href": href, "body": "", "source": "duckduckgo_lite"}
            )
            if len(results) >= web_search_topk:
                break
        return results
    except Exception as e:
        logger.warning("DuckDuckGo lite search failed: %s", e)
        return []


async def _search_ddgs(query: str) -> List[dict]:
    if DDGS is None:
        return []

    def _search() -> List[dict]:
        with DDGS() as ddgs:
            rows = list(ddgs.text(query, max_results=web_search_topk))
        results: List[dict] = []
        for item in rows:
            results.append(
                {
                    "title": str(item.get("title", "")).strip(),
                    "href": str(item.get("href", "")).strip(),
                    "body": str(item.get("body", "")).strip(),
                    "source": "ddgs",
                }
            )
        return results

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_search),
            timeout=web_search_timeout_seconds,
        )
    except Exception as e:
        logger.warning("DDGS search failed: %s", e)
        return []


async def _search_web(query: str) -> List[dict]:
    if not web_search_enabled:
        return []
    collectors: List[dict] = []
    strategies = []
    if serper_api_key:
        strategies.append(_search_serper)
    if _is_news_query(query):
        strategies.extend([_search_google_news_rss, _search_duckduckgo_lite, _search_ddgs])
    else:
        strategies.extend([_search_duckduckgo_lite, _search_google_news_rss, _search_ddgs])

    for strategy in strategies:
        results = await strategy(query)
        if not results:
            continue
        collectors.extend(results)
        merged = _dedupe_results(collectors)
        if len(merged) >= web_search_topk:
            return merged[:web_search_topk]

    return _dedupe_results(collectors)[:web_search_topk]


def _format_search_results(results: List[dict], max_items: int = 5) -> str:
    if not results:
        return "未检索到有效结果。"
    lines: List[str] = []
    for idx, item in enumerate(results[:max_items], start=1):
        title = item.get("title") or "(无标题)"
        href = item.get("href") or "(无链接)"
        body = item.get("body") or ""
        source = item.get("source") or "unknown"
        lines.append(f"{idx}. {title}\n来源: {source}\n链接: {href}\n摘要: {body}")
    return "\n\n".join(lines)


async def _summarize_search(query: str, results: List[dict]) -> str:
    formatted = _format_search_results(results)
    if not _llm_available():
        return f"搜索关键词：{query}\n\n{formatted}"

    try:
        text = await asyncio.wait_for(
            _llm_chat(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是检索助手。请基于给定搜索结果回答问题，简短清晰，"
                            "优先使用有来源链接的结果，最后附上 1-3 个来源链接。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"用户问题：{query}\n\n搜索结果：\n{formatted}",
                    },
                ],
                max_tokens=700,
            ),
            timeout=llm_timeout_seconds + 5,
        )
        return text or f"搜索关键词：{query}\n\n{formatted}"
    except Exception as e:
        logger.warning("Search summarize fallback: %s", e)
        return f"搜索关键词：{query}\n\n{formatted}"


def _format_group_messages(group_id: int, take: int) -> str:
    items = list(group_recent_messages.get(group_id, []))
    if not items:
        return ""
    lines: List[str] = []
    for item in items[-take:]:
        lines.append(f"{item['nickname']}({item['time']}): {item['content']}")
    return "\n".join(lines)


async def _generate_group_summary(group_id: int, ask: str = "") -> str:
    content = _format_group_messages(group_id, summary_default_take)
    if not content:
        return "本群最近还没有可总结的消息。"

    if not _llm_available():
        lines = content.splitlines()
        return "最近聊天摘录：\n" + "\n".join(lines[-20:])

    prompt = ask.strip() or "请总结今天主要聊了什么，提炼 3-6 条关键点。"
    try:
        text = await asyncio.wait_for(
            _llm_chat(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是群聊纪要助手。请根据聊天记录产出简明摘要，"
                            "输出包含：主题、关键结论、待办。控制在 200 字以内。"
                        ),
                    },
                    {"role": "user", "content": f"指令：{prompt}\n\n聊天记录：\n{content}"},
                ],
                max_tokens=600,
            ),
            timeout=llm_timeout_seconds + 5,
        )
        return text or "总结失败，请稍后重试。"
    except Exception as e:
        logger.warning("Group summary failed: %s", e)
        return "总结失败，请稍后重试。"


async def _refresh_user_summary_if_needed(user_id: str) -> None:
    if not user_memory_enabled or not _llm_available():
        return
    if user_id in refreshing_users:
        return
    count = user_msg_counter.get(user_id, 0)
    if count <= 0 or count % max(1, memory_update_every) != 0:
        return

    refreshing_users.add(user_id)
    try:
        lines = await asyncio.to_thread(_db_recent_user_messages, user_id, memory_extract_limit)
        if not lines:
            return
        content = "\n".join(lines)
        summary_text = await asyncio.wait_for(
            _llm_chat(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是用户画像提炼器。请从对话中提炼该用户稳定偏好、"
                            "称呼习惯、禁忌点。输出简短中文段落，不超过 120 字。"
                        ),
                    },
                    {"role": "user", "content": content},
                ],
                max_tokens=220,
            ),
            timeout=llm_timeout_seconds + 5,
        )
        if summary_text:
            await asyncio.to_thread(_db_update_user_summary, user_id, summary_text)
    except Exception as e:
        logger.warning("Refresh user memory failed: %s", e)
    finally:
        refreshing_users.discard(user_id)


@summary_cmd.handle()
async def handle_summary(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    if not isinstance(event, GroupMessageEvent):
        await summary_cmd.finish(_build_reply_message(event, with_roleplay("`/总结` 仅支持在群聊中使用。")))
    text = args.extract_plain_text().strip()
    result = await _generate_group_summary(event.group_id, ask=text)
    await summary_cmd.finish(_build_reply_message(event, with_roleplay(result)))


@search_cmd.handle()
async def handle_search(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    query = args.extract_plain_text().strip()
    if not query:
        await search_cmd.finish(_build_reply_message(event, with_roleplay("用法：`/搜索 你的问题`")))
    results = await _search_web(query)
    if not results:
        hint = "没有检索到结果。可在 .env 配置 `SERPER_API_KEY` 提升稳定性。"
        await search_cmd.finish(_build_reply_message(event, with_roleplay(hint)))
    answer = await _summarize_search(query, results)
    await search_cmd.finish(_build_reply_message(event, with_roleplay(answer)))


@help_cmd.handle()
async def handle_help(event: MessageEvent):
    await help_cmd.finish(_build_reply_message(event, with_roleplay(_build_help_text())))


@list_cmd.handle()
async def handle_list(event: MessageEvent):
    await list_cmd.finish(_build_reply_message(event, with_roleplay(_build_help_text())))


@vision_cmd.handle()
async def handle_vision(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    if not _is_group_allowed(event):
        await vision_cmd.finish(_build_reply_message(event, with_roleplay("本群未开通此功能。")))

    ask = args.extract_plain_text().strip()
    image_url = await _extract_image_url_from_context(bot, event, args)
    if not image_url:
        await vision_cmd.finish(
            _build_reply_message(
                event,
                with_roleplay("请在指令消息里带图，或回复某张图再发 `/识图 你的问题`。"),
            )
        )

    try:
        answer = await _gemini_vision_analyze(image_url, ask)
    except httpx.HTTPStatusError as e:
        logger.warning("Vision http error: %s", e)
        await vision_cmd.finish(
            _build_reply_message(event, with_roleplay("识图请求失败，请检查 Gemini API 配置或额度。"))
        )
    except Exception as e:
        logger.warning("Vision analyze failed: %s", e)
        await vision_cmd.finish(_build_reply_message(event, with_roleplay(f"识图失败：{e}")))

    if not answer:
        answer = "我暂时没识别出有效结果，请换一张图或换个问法。"
    await vision_cmd.finish(_build_reply_message(event, with_roleplay(answer)))


@chat.handle()
async def handle_chat(bot: Bot, event: MessageEvent):
    _record_group_message(event)

    if not _is_group_allowed(event):
        return

    # 群聊里只要 @机器人 且消息上下文中有图（直接带图/回复图片），自动识图
    if isinstance(event, GroupMessageEvent) and bool(getattr(event, "to_me", False)):
        image_url = await _extract_image_url_from_context(bot, event, event.message)
        if image_url:
            ask_text = event.get_plaintext().strip()
            ask_text = re.sub(r"^(?:/)?(?:识图|看图|vision)\s*", "", ask_text, flags=re.I).strip()
            try:
                answer = await _gemini_vision_analyze(image_url, ask_text)
            except httpx.HTTPStatusError as e:
                logger.warning("Auto vision http error: %s", e)
                await chat.finish(
                    _build_reply_message(
                        event,
                        with_roleplay("识图请求失败，请检查 Gemini API 配置或额度。"),
                    )
                )
            except Exception as e:
                logger.warning("Auto vision failed: %s", e)
                await chat.finish(_build_reply_message(event, with_roleplay(f"识图失败：{e}")))

            if not answer:
                answer = "我暂时没识别出有效结果，请换一张图或换个问法。"
            await chat.finish(_build_reply_message(event, with_roleplay(answer)))

    user_query = _extract_chat_query(event)
    if not user_query:
        return

    if isinstance(event, GroupMessageEvent) and _is_summary_intent(user_query):
        result = await _generate_group_summary(event.group_id, ask=user_query)
        await chat.finish(_build_reply_message(event, with_roleplay(result)))

    if user_query.startswith("搜索 ") or user_query.startswith("联网 "):
        query = user_query.split(maxsplit=1)[1].strip()
        if query:
            results = await _search_web(query)
            if not results:
                await chat.finish(
                    _build_reply_message(
                        event,
                        with_roleplay("没有检索到结果。可在 .env 配置 `SERPER_API_KEY` 提升稳定性。"),
                    )
                )
            answer = await _summarize_search(query, results)
            await chat.finish(_build_reply_message(event, with_roleplay(answer)))

    if not _llm_available():
        await chat.finish(_build_reply_message(event, "未配置可用的大模型 API，暂时无法进行聊天。"))

    now_ts = time.time()
    _clear_expired_sessions(now_ts)

    cd_key = _cooldown_key(event)
    if chat_cooldown_seconds > 0:
        remaining = chat_cooldown_seconds - (now_ts - last_chat_ts.get(cd_key, 0))
        if remaining > 0:
            wait_seconds = int(remaining) + 1
            await chat.finish(
                _build_reply_message(event, f"消息太快了，请 {wait_seconds} 秒后再试。")
            )
    last_chat_ts[cd_key] = now_ts

    session_id = _session_id(event)
    user_id = str(event.user_id)
    group_id = str(event.group_id) if isinstance(event, GroupMessageEvent) else ""
    nickname = _event_nickname(event)

    if user_memory_enabled:
        await asyncio.to_thread(_db_upsert_user_archive, user_id, nickname)

    if session_id not in history:
        sys_prompt = system_prompt
        if user_memory_enabled:
            user_summary = await asyncio.to_thread(_db_get_user_summary, user_id)
            if user_summary:
                sys_prompt += f"\n\n[用户长期记忆]\n{user_summary}"
        history[session_id] = [{"role": "system", "content": sys_prompt}]

    history[session_id].append({"role": "user", "content": user_query})
    history[session_id] = _trim_history(history[session_id])
    last_active[session_id] = now_ts

    request_messages = list(history[session_id])
    current_course = get_current_study_course()
    if current_course:
        location_tip = f"，地点 {current_course.location}" if current_course.location else ""
        request_messages.append(
            {
                "role": "system",
                "content": (
                    f"该用户当前正在上《{current_course.course_name}》"
                    f"（{current_course.start_time}-{current_course.end_time}{location_tip}）。"
                    "请先非常自然地提醒主人收心听课，再继续回答他当前问题。"
                    "提醒要像日常对话，不要机械模板。"
                ),
            }
        )

    if _should_auto_search(user_query):
        results = await _search_web(user_query)
        if results:
            request_messages.append(
                {
                    "role": "system",
                    "content": "以下是联网搜索结果，请优先使用其中信息：\n"
                    + _format_search_results(results, max_items=min(5, web_search_topk)),
                }
            )

    try:
        reply = await asyncio.wait_for(
            _llm_chat(request_messages, max_tokens=800),
            timeout=llm_timeout_seconds + 5,
        )
        reply = (reply or "").strip()
        if not reply:
            reply = "我这边暂时没有组织出合适的回复，请换个问法试试。"

        history[session_id].append({"role": "assistant", "content": reply})
        history[session_id] = _trim_history(history[session_id])
        last_active[session_id] = time.time()

        if user_memory_enabled:
            await asyncio.to_thread(_db_append_dialogue, session_id, user_id, group_id, "user", user_query)
            await asyncio.to_thread(_db_append_dialogue, session_id, user_id, group_id, "assistant", reply)
            user_msg_counter[user_id] = user_msg_counter.get(user_id, 0) + 1
            asyncio.create_task(_refresh_user_summary_if_needed(user_id))

        await chat.send(_build_reply_message(event, reply))
        return
    except asyncio.TimeoutError:
        logger.warning("LLM timeout: session=%s", session_id)
        if history.get(session_id):
            history[session_id].pop()
        await chat.finish(_build_reply_message(event, "大模型响应超时了，请稍后再试。"))
    except Exception as e:
        logger.exception("LLM Error: %s", e)
        if history.get(session_id):
            history[session_id].pop()
        await chat.finish(_build_reply_message(event, "抱歉，大模型满载或 API 连接异常。"))


try:
    _init_db()
except Exception as e:
    logger.error("Init memory DB failed: %s", e)
