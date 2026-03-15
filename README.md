# QQ Group Bot

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![License](https://img.shields.io/github/license/tianman450-oss/qq-group-bot)
![CI](https://img.shields.io/github/actions/workflow/status/tianman450-oss/qq-group-bot/ci.yml?branch=main)
![Release](https://img.shields.io/github/v/tag/tianman450-oss/qq-group-bot)

一个基于 NoneBot2 + OneBot V11 的 QQ 群机器人，集成了大模型对话、ComfyUI 生图、图生图、识图、联网搜索、群聊总结、WakeUp 课表同步、上课提醒和群友上课状态长图。

## Highlights

- `LLM 对话`：多轮上下文、角色扮演、长期记忆、自动识图
- `ComfyUI 生图`：文生图、图生图、模型切换、LoRA 切换、队列控制
- `群聊增强`：联网搜索、消息总结、帮助命令
- `课表能力`：WakeUp 口令导入、当前课程判断、上课提醒、群友上课状态长图
- `定时推送`：新闻早报、摸鱼图订阅

## Stack

- Python `3.10+`
- NoneBot2
- OneBot V11
- NapCatQQ 或 Lagrange
- ComfyUI
- OpenAI 兼容 API 或 Gemini API
- SQLite

## Screenshots

角色扮演对话

![角色扮演二选一](./docs/screenshots/chat-roleplay-choice.jpg)
![角色扮演称呼互动](./docs/screenshots/chat-roleplay-title.jpg)

识图与搜索

![识图示例](./docs/screenshots/vision-bird.jpg)
![搜索示例](./docs/screenshots/search-command.jpg)

课表与群友状态

![WakeUp 课表导入](./docs/screenshots/course-import-wakeup.jpg)
![群友上课状态图](./docs/screenshots/group-course-status.jpg)

帮助与生图

![帮助命令](./docs/screenshots/help-command.jpg)
![文生图示例](./docs/screenshots/text2img-command.jpg)

## Quick Start

```powershell
git clone https://github.com/tianman450-oss/qq-group-bot.git
cd qq-group-bot
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
Copy-Item .env.example .env
```

然后修改 `.env`：

- 配置 `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`
- 配置 `COMFYUI_SERVER`
- 确认工作流路径：
  - `COMFYUI_WORKFLOW=workflows/text2img_default.json`
  - `COMFYUI_IMG2IMG_WORKFLOW=workflows/img2img_no_upscale.json`

启动：

```powershell
python bot.py
```

NapCat/Lagrange 反向 WS：

```text
ws://127.0.0.1:8080/onebot/v11/ws
```

## Common Commands

- `@机器人 你好`
- `/帮助`
- `/画 提示词`
- `/图生图 提示词 --denoise 0.65`
- `/识图 这是什么`
- `/搜索 关键词`
- `/总结`
- `/导入课表 WakeUp口令`
- `/课表`
- `/上课状态`
- `/群友上什么课`

## Project Structure

```text
src/plugins/chat_plugin/       聊天、搜索、总结、识图
src/plugins/draw_plugin/       文生图、图生图、模型与 LoRA
src/plugins/course_plugin/     课表导入、上课提醒、群友上课状态图
src/plugins/scheduler_plugin/  早报与摸鱼图订阅
src/utils/                     角色扮演、课表存储等公共工具
workflows/                     ComfyUI API 工作流
docs/screenshots/              项目截图
```

## Included Workflows

- `workflows/text2img_default.json`
- `workflows/img2img_no_upscale.json`

注意：

- 仓库不会附带模型、LoRA、QQ 账号信息、API Key
- 文生图和图生图都依赖你本地或远程可用的 ComfyUI
- 图生图工作流需要带 `LoadImage` 节点

## Documentation

- [使用教程.md](./使用教程.md)
- [CHANGELOG.md](./CHANGELOG.md)
- [PRD.md](./PRD.md)
- [architecture.md](./architecture.md)
- [docs/deployment.md](./docs/deployment.md)
- [开源准备清单.md](./开源准备清单.md)

## Open Source Notes

- 请不要提交 `.env`、数据库、日志和用户数据
- Fork 后建议优先修改 `.env.example`
- 如果替换截图，尽量保持 `docs/screenshots/` 中现有文件名不变

## Contributing

欢迎提交 Issue 和 PR。提交前建议至少完成：

- `python -m py_compile bot.py src/utils/study.py src/plugins/chat_plugin/__init__.py src/plugins/course_plugin/__init__.py src/plugins/draw_plugin/__init__.py src/plugins/scheduler_plugin/__init__.py`
- `python -X utf8 -c "import nonebot; nonebot.init(); nonebot.load_plugins('src/plugins'); print('plugins_loaded')"`
- 确认 `.env`、数据库和真实密钥没有进入提交内容

更多说明见 [CONTRIBUTING.md](./CONTRIBUTING.md)。

## Community

- [CODE_OF_CONDUCT.md](./CODE_OF_CONDUCT.md)
- [SECURITY.md](./SECURITY.md)

## License

本项目使用 [MIT License](./LICENSE)。
