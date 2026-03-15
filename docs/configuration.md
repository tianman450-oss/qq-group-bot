# Configuration Guide

本文整理了项目当前主要环境变量，便于你在不翻源码的情况下完成部署。

## 1. 基础服务

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST` | `0.0.0.0` | Bot 监听地址 |
| `PORT` | `8080` | Bot 监听端口，需与 OneBot 反向 WS 对齐 |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `SUPERUSERS` | `["123456789"]` | 超级用户列表，使用 QQ 号 |

## 2. 大模型配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `LLM_PROVIDER` | `openai` | 当前默认走 OpenAI 兼容接口 |
| `LLM_API_KEY` | 示例值 | OpenAI 兼容接口密钥 |
| `LLM_BASE_URL` | `https://api.openai.com/v1` | OpenAI 兼容接口地址 |
| `LLM_MODEL` | `gpt-4o-mini` | 当前聊天模型 |
| `GEMINI_API_KEY` | 空 | Gemini API Key |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Gemini 模型名 |
| `GEMINI_THINKING_BUDGET` | `0` | Gemini thinking budget |
| `LLM_TIMEOUT_SECONDS` | `30` | 单次请求超时 |
| `LLM_MEMORY_TURNS` | `8` | 会话上下文保留轮数 |
| `LLM_SESSION_EXPIRE_SECONDS` | `600` | 会话过期秒数 |
| `CHAT_COOLDOWN_SECONDS` | `2` | 聊天冷却时间 |

说明：

- 如果你使用本地 OpenAI 兼容服务，只需要修改 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`。
- 如果你想切换到 Gemini，请确认对应代码路径仍使用你当前配置的提供商。

## 3. 角色扮演

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `COMMAND_ROLEPLAY_ENABLED` | `true` | 是否让命令结果也走角色扮演风格 |
| `COMMAND_ROLEPLAY_PREFIX` | 已配置 | 命令回执前缀 |
| `COMMAND_ROLEPLAY_SUFFIX` | 已配置 | 命令回执后缀 |
| `LLM_SYSTEM_PROMPT` | 已配置 | 角色系统提示词 |

建议：

- 如果你只想保留功能型回复，可把 `COMMAND_ROLEPLAY_ENABLED` 改成 `false`。
- 修改人设后，建议至少手测聊天、识图、搜索、课表导入四类命令。

## 4. 群聊与访问控制

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CHAT_PREFIXES` | `!,/` | 命令前缀列表 |
| `GROUP_WHITELIST` | 空 | 仅允许这些群使用 |
| `GROUP_BLACKLIST` | 空 | 禁止这些群使用 |

## 5. 搜索与总结

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `WEB_SEARCH_ENABLED` | `true` | 是否启用联网搜索 |
| `WEB_SEARCH_TOPK` | `5` | 搜索结果数量 |
| `WEB_SEARCH_AUTO` | `true` | 是否自动触发搜索 |
| `WEB_SEARCH_TIMEOUT_SECONDS` | `15` | 搜索超时 |
| `SERPER_API_KEY` | 空 | 如果你使用 Serper，可填入 Key |
| `GOOGLE_NEWS_HL` | `zh-CN` | Google News 语言 |
| `GOOGLE_NEWS_GL` | `CN` | Google News 地区 |
| `GOOGLE_NEWS_CEID` | `CN:zh-Hans` | Google News 订阅参数 |
| `SUMMARY_BUFFER_LIMIT` | `200` | 总结缓存消息数 |
| `SUMMARY_DEFAULT_TAKE` | `80` | 默认总结条数 |

## 6. 长期记忆

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `USER_MEMORY_ENABLED` | `true` | 是否启用长期记忆 |
| `MEMORY_DB_PATH` | `data/bot_memory.db` | SQLite 数据库路径 |
| `MEMORY_UPDATE_EVERY` | `50` | 多少条消息后更新一次记忆 |
| `MEMORY_EXTRACT_LIMIT` | `40` | 记忆提取数量上限 |

## 7. ComfyUI 生图

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `COMFYUI_SERVER` | `127.0.0.1:8188` | ComfyUI 服务地址 |
| `COMFYUI_WORKFLOW` | `workflows/text2img_default.json` | 文生图工作流 |
| `COMFYUI_IMG2IMG_WORKFLOW` | `workflows/img2img_no_upscale.json` | 图生图工作流 |
| `COMFYUI_HTTP_TIMEOUT_SECONDS` | `30` | ComfyUI 请求超时 |
| `DRAW_TIMEOUT_SECONDS` | `200` | 单次生图总超时 |
| `DRAW_POLL_INTERVAL_SECONDS` | `2` | 轮询间隔 |
| `DRAW_COOLDOWN_SECONDS` | `5` | 生图冷却时间 |
| `DRAW_DAILY_LIMIT` | `20` | 每日生图上限 |
| `DRAW_USE_LLM_PROMPT` | `false` | 是否让 LLM 改写提示词 |
| `DRAW_WORKFLOW_UPSCALE_FACTOR` | `4` | 工作流内放大倍率参数 |
| `DRAW_POST_UPSCALE_SCALE_BY` | `0.25` | 放大后回缩比例 |
| `DRAW_USER_STYLE_TTL_SECONDS` | `1800` | 用户模型/LoRA 记忆时长 |
| `DRAW_LIST_ALL_LOCAL_STYLES` | `true` | 是否列出本地所有模型和 LoRA |
| `DRAW_STYLE_CATALOG_CACHE_SECONDS` | `60` | 模型目录缓存时间 |

说明：

- 公开仓库不会附带模型文件和 LoRA 文件。
- 如果你替换工作流，请同步检查节点名是否仍与代码读取逻辑一致。

## 8. 定时推送

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SCHEDULER_ENABLED` | `true` | 是否启用定时任务 |
| `NEWS_CRON` | `0 9 * * *` | 早报推送时间 |
| `FISH_CRON` | `30 12 * * *` | 摸鱼图推送时间 |
| `DAILY_FISH_PROMPT` | 已配置 | 摸鱼图提示词 |
| `NEWS_SOURCE` | `google_news_rss` | 早报来源 |
| `NEWS_SEARCH_QUERY` | `中国 今日 新闻` | 早报查询词 |
| `NEWS_SEARCH_TOPK` | `5` | 早报条目数 |
| `NEWS_RSS_URL` | 空 | 自定义 RSS 地址 |
| `NEWS_WITH_COVER` | `true` | 是否尝试带封面图 |

## 9. 课表与提醒

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `STUDY_SCHEDULE_ENABLED` | `true` | 是否启用课表功能 |
| `STUDY_REMINDER_ENABLED` | `true` | 是否启用上课提醒 |
| `STUDY_REMINDER_CACHE_SECONDS` | `90` | 提醒缓存 |
| `STUDY_IMPORT_TIMEOUT_SECONDS` | `20` | 导入超时 |
| `STUDY_IMPORT_MAX_CHARS` | `12000` | 导入文本长度上限 |
| `STUDY_TIMEZONE` | `Asia/Shanghai` | 课表时区 |
| `WAKEUP_SHARE_API` | `https://i.wakeup.fun/share_schedule/get` | WakeUp 口令接口 |
| `GROUP_COURSE_IMAGE_LIMIT` | `60` | 群友上课状态图最多渲染人数 |
| `GROUP_COURSE_QUERY_PARALLEL` | `12` | 群成员状态并发查询数 |
| `GROUP_AVATAR_TIMEOUT_SECONDS` | `8` | 群友头像抓取超时 |

## 10. 推荐最小配置

如果你只想先把机器人跑起来，至少修改这些值：

- `SUPERUSERS`
- `LLM_API_KEY`
- `LLM_BASE_URL`
- `LLM_MODEL`
- `COMFYUI_SERVER`

如果你还要用课表和推送，再继续确认：

- `WAKEUP_SHARE_API`
- `NEWS_SOURCE`
- `NEWS_RSS_URL` 或搜索类配置

## 11. 修改配置后的建议动作

每次改完 `.env` 后，建议至少做这两步：

```powershell
.\scripts\check.ps1
python bot.py
```

这样能更早发现语法错误、插件加载失败和路径配置问题。
