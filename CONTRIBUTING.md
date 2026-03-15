# Contributing Guide

感谢你愿意参与这个项目。

## Before You Start

- 请先阅读 `README.md` 和 `使用教程.md`
- 新功能建议先提 Issue，再开始改动
- Bug 修复可以直接提 PR，但请在描述里写清楚复现步骤和修复方式

## Local Development

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
Copy-Item .env.example .env
python bot.py
```

## Basic Checks

提交前至少运行：

```powershell
python -m py_compile bot.py src/utils/study.py src/plugins/chat_plugin/__init__.py src/plugins/course_plugin/__init__.py src/plugins/draw_plugin/__init__.py src/plugins/scheduler_plugin/__init__.py
python -X utf8 -c "import nonebot; nonebot.init(); nonebot.load_plugins('src/plugins'); print('plugins_loaded')"
```

## Contribution Rules

- 不要提交 `.env`、数据库、日志、缓存和真实 API Key
- 不要把本地绝对路径写进代码和文档
- 新增命令请同步更新 `README.md` 和 `使用教程.md`
- 如果改动配置项，请同步更新 `.env.example`
- 如果改动了工作流路径，请同步更新相关文档

## Pull Request Checklist

- 变更目的清晰
- 影响范围明确
- 文档已同步
- 本地已完成基础校验

