from nonebot import get_driver


def _parse_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def roleplay_enabled() -> bool:
    config = get_driver().config
    return _parse_bool(getattr(config, "command_roleplay_enabled", True), True)


def roleplay_suffix() -> str:
    config = get_driver().config
    text = str(
        getattr(
            config,
            "command_roleplay_suffix",
            "主人，千夏已经处理好啦喵~",
        )
    ).strip()
    return text or "主人，千夏已经处理好啦喵~"


def roleplay_prefix() -> str:
    config = get_driver().config
    text = str(getattr(config, "command_roleplay_prefix", "")).strip()
    if text:
        return text
    # 兼容旧配置：未配置 prefix 时，沿用 suffix 的值做前置语
    legacy = roleplay_suffix()
    return legacy if legacy else "主、主人…千夏来汇报结果了喵："


def _select_lead_text(plain: str) -> str:
    if not plain:
        return roleplay_prefix()

    problem_keywords = ("失败", "错误", "异常", "超时", "不可用", "找不到", "无效")
    hint_keywords = ("用法", "请在", "请先", "仅支持", "未配置", "需要")
    success_keywords = ("已", "完成", "成功", "切换", "订阅", "退订", "生成完成", "触发")

    if any(k in plain for k in problem_keywords):
        return "主、主人…对不起喵，千夏这边遇到一点小问题："
    if any(k in plain for k in hint_keywords):
        return "主、主人…千夏给你一个小提示喵："
    if any(k in plain for k in success_keywords):
        return roleplay_prefix()
    return "主、主人…千夏整理好了，给你汇报喵："


def with_roleplay(text: str) -> str:
    plain = str(text or "").strip()
    if not roleplay_enabled():
        return plain
    lead = _select_lead_text(plain)
    if not plain:
        formatted = lead
    elif plain.startswith(lead):
        formatted = plain
    else:
        formatted = f"{lead}\n{plain}"

    # 上课提醒需要始终出现在命令结果最前面
    reminder = ""
    try:
        from src.utils.study import get_current_study_reminder

        reminder = get_current_study_reminder()
    except Exception:
        reminder = ""

    reminder = str(reminder or "").strip()
    if not reminder:
        return formatted
    if formatted.startswith(reminder):
        return formatted
    if not formatted:
        return reminder
    return f"{reminder}\n{formatted}"
