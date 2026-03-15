import asyncio
import html
import re
import sqlite3
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Tuple

import httpx
from nonebot import get_bots, get_driver, on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageSegment
from nonebot.log import logger
from src.utils.roleplay import with_roleplay

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
except Exception:
    AsyncIOScheduler = None
    CronTrigger = None

try:
    from duckduckgo_search import DDGS
except Exception:
    DDGS = None

config = get_driver().config
driver = get_driver()


def _parse_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


memory_db_path = Path(getattr(config, "memory_db_path", "data/bot_memory.db"))
scheduler_enabled = _parse_bool(getattr(config, "scheduler_enabled", True), True)
news_cron = str(getattr(config, "news_cron", "0 9 * * *"))
fish_cron = str(getattr(config, "fish_cron", "30 12 * * *"))
daily_fish_prompt = str(
    getattr(config, "daily_fish_prompt", "cute cat sleeping on desk, anime style")
)
news_search_query = str(getattr(config, "news_search_query", "中国 今日 新闻"))
news_search_topk = int(getattr(config, "news_search_topk", 5))
news_source = str(getattr(config, "news_source", "google_news_rss")).strip().lower()
news_rss_url = str(getattr(config, "news_rss_url", "")).strip()
news_with_cover = _parse_bool(getattr(config, "news_with_cover", True), True)
google_news_hl = str(getattr(config, "google_news_hl", "zh-CN"))
google_news_gl = str(getattr(config, "google_news_gl", "CN"))
google_news_ceid = str(getattr(config, "google_news_ceid", "CN:zh-Hans"))

TOPIC_NEWS = "news"
TOPIC_FISH = "fish"

scheduler = AsyncIOScheduler(timezone="Asia/Shanghai") if AsyncIOScheduler else None

subscribe_news_cmd = on_command("订阅早报", priority=6, block=True)
unsubscribe_news_cmd = on_command("退订早报", priority=6, block=True)
subscribe_fish_cmd = on_command("订阅摸鱼图", priority=6, block=True)
unsubscribe_fish_cmd = on_command("退订摸鱼图", priority=6, block=True)
test_news_cmd = on_command("测试早报", priority=6, block=True)
test_fish_cmd = on_command("测试摸鱼图", priority=6, block=True)


def _init_db() -> None:
    memory_db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(memory_db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                topic TEXT NOT NULL,
                group_id INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY(topic, group_id)
            )
            """
        )
        conn.commit()


def _set_subscription(topic: str, group_id: int, subscribe: bool) -> None:
    with sqlite3.connect(memory_db_path) as conn:
        if subscribe:
            conn.execute(
                """
                INSERT INTO subscriptions (topic, group_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(topic, group_id) DO UPDATE SET
                    updated_at = excluded.updated_at
                """,
                (topic, group_id, int(time.time())),
            )
        else:
            conn.execute(
                "DELETE FROM subscriptions WHERE topic = ? AND group_id = ?",
                (topic, group_id),
            )
        conn.commit()


def _list_subscriptions(topic: str) -> List[int]:
    with sqlite3.connect(memory_db_path) as conn:
        rows = conn.execute(
            "SELECT group_id FROM subscriptions WHERE topic = ? ORDER BY group_id",
            (topic,),
        ).fetchall()
    return [int(row[0]) for row in rows]


def _fetch_news_lines() -> List[str]:
    lines, _ = _fetch_news_bundle_sync()
    return lines


def _strip_html(raw: str) -> str:
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw, flags=re.IGNORECASE)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _build_google_news_rss_url(query: str) -> str:
    q = urllib.parse.quote_plus(query)
    return (
        f"https://news.google.com/rss/search?q={q}"
        f"&hl={urllib.parse.quote_plus(google_news_hl)}"
        f"&gl={urllib.parse.quote_plus(google_news_gl)}"
        f"&ceid={urllib.parse.quote_plus(google_news_ceid)}"
    )


def _fetch_from_google_news_rss(url: str) -> List[str]:
    response = httpx.get(url, timeout=12.0)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    lines: List[str] = []
    for idx, item in enumerate(root.findall("./channel/item"), start=1):
        title = (item.findtext("title") or "").strip() or "(无标题)"
        href = (item.findtext("link") or "").strip() or "(无链接)"
        desc = _strip_html(item.findtext("description") or "")
        lines.append(f"{idx}. {title}\n{desc}\n{href}")
        if len(lines) >= news_search_topk:
            break
    return lines


def _fetch_from_ddgs(query: str) -> List[str]:
    if DDGS is None:
        return []
    with DDGS() as ddgs:
        results = list(ddgs.text(query, max_results=news_search_topk))
    lines: List[str] = []
    for idx, item in enumerate(results, start=1):
        title = str(item.get("title", "")).strip() or "(无标题)"
        href = str(item.get("href", "")).strip() or "(无链接)"
        body = str(item.get("body", "")).strip()
        lines.append(f"{idx}. {title}\n{body}\n{href}")
    return lines


def _fetch_news_bundle_sync() -> Tuple[List[str], str]:
    try:
        if news_source == "custom_rss":
            if not news_rss_url:
                return [], "custom_rss(未配置NEWS_RSS_URL)"
            return _fetch_from_google_news_rss(news_rss_url), "custom_rss"

        if news_source in {"google_news_rss", "google"}:
            rss_url = _build_google_news_rss_url(news_search_query)
            return _fetch_from_google_news_rss(rss_url), "google_news_rss"

        if news_source == "ddgs":
            return _fetch_from_ddgs(news_search_query), "ddgs"

        # auto fallback
        try:
            rss_url = _build_google_news_rss_url(news_search_query)
            lines = _fetch_from_google_news_rss(rss_url)
            if lines:
                return lines, "google_news_rss"
        except Exception:
            pass
        lines = _fetch_from_ddgs(news_search_query)
        return lines, "ddgs"
    except Exception as e:
        logger.warning("Fetch news failed: %s", e)
        return [], "none"


async def _fetch_news_cover_url() -> str:
    if not news_with_cover:
        return ""
    try:
        async with httpx.AsyncClient(timeout=10.0) as http_client:
            response = await http_client.get(
                "https://www.bing.com/HPImageArchive.aspx",
                params={"format": "js", "idx": 0, "n": 1, "mkt": "zh-CN"},
            )
            response.raise_for_status()
            payload = response.json()
        images = payload.get("images") or []
        if not images:
            return ""
        url = str(images[0].get("url") or "").strip()
        if not url:
            return ""
        if url.startswith("/"):
            return "https://www.bing.com" + url
        return url
    except Exception as e:
        logger.warning("Fetch news cover failed: %s", e)
        return ""


async def _broadcast_group_message(group_ids: List[int], message):
    if not group_ids:
        return
    bots = get_bots()
    if not bots:
        logger.warning("No connected bot, skip scheduled broadcast")
        return
    bot = next(iter(bots.values()))
    for group_id in group_ids:
        try:
            await bot.send_group_msg(group_id=group_id, message=message)
        except Exception as e:
            logger.warning("Send scheduled message failed: group=%s err=%s", group_id, e)


async def _push_daily_news():
    group_ids = await asyncio.to_thread(_list_subscriptions, TOPIC_NEWS)
    if not group_ids:
        return
    lines, source = await asyncio.to_thread(_fetch_news_bundle_sync)
    if lines:
        text = f"【每日早报】\n来源：{source}\n\n" + "\n\n".join(lines[:news_search_topk])
    else:
        text = "【每日早报】今天暂时抓取失败，稍后重试。"
    cover_url = await _fetch_news_cover_url()
    if cover_url:
        message = MessageSegment.image(cover_url) + "\n" + text
    else:
        message = text
    await _broadcast_group_message(group_ids, message)


async def _push_daily_fish():
    group_ids = await asyncio.to_thread(_list_subscriptions, TOPIC_FISH)
    if not group_ids:
        return
    try:
        from src.plugins.draw_plugin import generate_text2img_for_scheduler

        image_data, elapsed = await generate_text2img_for_scheduler(
            daily_fish_prompt, orientation="landscape"
        )
        msg = MessageSegment.image(image_data) + f"\n每日摸鱼图已送达（耗时 {elapsed:.1f} 秒）"
    except Exception as e:
        logger.warning("Generate daily fish image failed: %s", e)
        msg = "今日摸鱼图生成失败，请稍后重试。"
    await _broadcast_group_message(group_ids, msg)


def _register_jobs() -> None:
    if scheduler is None or CronTrigger is None:
        logger.warning("APScheduler not installed, scheduled jobs disabled")
        return
    if scheduler.get_job("daily-news") is None:
        scheduler.add_job(
            _push_daily_news,
            CronTrigger.from_crontab(news_cron),
            id="daily-news",
            replace_existing=True,
        )
    if scheduler.get_job("daily-fish") is None:
        scheduler.add_job(
            _push_daily_fish,
            CronTrigger.from_crontab(fish_cron),
            id="daily-fish",
            replace_existing=True,
        )


@driver.on_startup
async def _on_startup():
    try:
        _init_db()
    except Exception as e:
        logger.error("Init scheduler DB failed: %s", e)
        return
    if not scheduler_enabled:
        logger.info("Scheduler disabled by config")
        return
    if scheduler is None:
        logger.warning("Scheduler dependency missing: pip install APScheduler")
        return
    try:
        _register_jobs()
        if not scheduler.running:
            scheduler.start()
        logger.info("Scheduler started")
    except Exception as e:
        logger.error("Scheduler start failed: %s", e)


@driver.on_shutdown
async def _on_shutdown():
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)


@subscribe_news_cmd.handle()
async def handle_subscribe_news(event: GroupMessageEvent):
    await asyncio.to_thread(_set_subscription, TOPIC_NEWS, event.group_id, True)
    await subscribe_news_cmd.finish(with_roleplay("已订阅早报推送。"))


@unsubscribe_news_cmd.handle()
async def handle_unsubscribe_news(event: GroupMessageEvent):
    await asyncio.to_thread(_set_subscription, TOPIC_NEWS, event.group_id, False)
    await unsubscribe_news_cmd.finish(with_roleplay("已退订早报推送。"))


@subscribe_fish_cmd.handle()
async def handle_subscribe_fish(event: GroupMessageEvent):
    await asyncio.to_thread(_set_subscription, TOPIC_FISH, event.group_id, True)
    await subscribe_fish_cmd.finish(with_roleplay("已订阅每日摸鱼图。"))


@unsubscribe_fish_cmd.handle()
async def handle_unsubscribe_fish(event: GroupMessageEvent):
    await asyncio.to_thread(_set_subscription, TOPIC_FISH, event.group_id, False)
    await unsubscribe_fish_cmd.finish(with_roleplay("已退订每日摸鱼图。"))


@test_news_cmd.handle()
async def handle_test_news(event: GroupMessageEvent):
    await _push_daily_news()
    await test_news_cmd.finish(with_roleplay("已触发早报推送任务（若本群已订阅）。"))


@test_fish_cmd.handle()
async def handle_test_fish(event: GroupMessageEvent):
    await _push_daily_fish()
    await test_fish_cmd.finish(with_roleplay("已触发摸鱼图推送任务（若本群已订阅）。"))
