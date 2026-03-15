import asyncio
import io
import json
import random
import re
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

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
from nonebot.params import CommandArg
from PIL import Image
from src.utils.roleplay import with_roleplay

config = get_driver().config

comfy_server = getattr(config, "comfyui_server", "127.0.0.1:8188")
text2img_workflow_path = Path(
    getattr(config, "comfyui_workflow", "workflows/text2img_default.json")
)
img2img_workflow_path = Path(
    getattr(config, "comfyui_img2img_workflow", "workflows/img2img_no_upscale.json")
)

comfy_http_timeout_seconds = float(getattr(config, "comfyui_http_timeout_seconds", 30))
draw_timeout_seconds = float(getattr(config, "draw_timeout_seconds", 200))
draw_poll_interval_seconds = float(getattr(config, "draw_poll_interval_seconds", 2))
draw_cooldown_seconds = float(getattr(config, "draw_cooldown_seconds", 5))
draw_daily_limit = int(getattr(config, "draw_daily_limit", 20))

workflow_upscale_factor = float(getattr(config, "draw_workflow_upscale_factor", 4))
default_post_upscale_scale = (
    1.0 / workflow_upscale_factor if workflow_upscale_factor > 0 else 1.0
)
post_upscale_scale_by = float(
    getattr(config, "draw_post_upscale_scale_by", default_post_upscale_scale)
)


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


group_whitelist = _parse_int_set(getattr(config, "group_whitelist", ""))
group_blacklist = _parse_int_set(getattr(config, "group_blacklist", ""))

LANDSCAPE_PATTERN = re.compile(r"(--landscape|--l\b|横图|横版)", re.IGNORECASE)
PORTRAIT_PATTERN = re.compile(r"(--portrait|--p\b|竖图|竖版)", re.IGNORECASE)
IMG2IMG_DENOISE_PATTERN = re.compile(
    r"(?:--denoise|--d|denoise|降噪)\s*[=:]?\s*([+-]?(?:\d+(?:\.\d+)?|\.\d+))",
    re.IGNORECASE,
)
IMG2IMG_DENOISE_FLAG_PATTERN = re.compile(
    r"(?:--denoise|--d|denoise|降噪)\b",
    re.IGNORECASE,
)

DEFAULT_PORTRAIT_SIZE = (1024, 1536)
DEFAULT_LANDSCAPE_SIZE = (1536, 1024)
DEFAULT_IMG2IMG_PORTRAIT_SIZE = (1536, 2048)
DEFAULT_IMG2IMG_LANDSCAPE_SIZE = (2048, 1536)
MAX_INPUT_SHORT_SIDE = 1536
MAX_INPUT_LONG_SIDE = 2048
DEFAULT_IMG2IMG_PROMPT = "masterpiece, best quality, detailed, high quality"
MODEL_FILE_EXTENSIONS = (".safetensors", ".ckpt", ".pt", ".pth", ".bin")
LORA_FILE_EXTENSIONS = (".safetensors", ".ckpt", ".pt", ".bin")
draw_list_all_local_styles = _parse_bool(
    getattr(config, "draw_list_all_local_styles", True),
    default=True,
)
style_catalog_cache_seconds = max(
    5,
    int(getattr(config, "draw_style_catalog_cache_seconds", 60)),
)
draw_user_style_ttl_seconds = max(
    60,
    int(getattr(config, "draw_user_style_ttl_seconds", 1800)),
)


@dataclass
class DrawTask:
    prompt_text: str
    orientation: Optional[str]
    image_url: Optional[str]
    workflow_path: Path
    task_type: str
    style_selection: Optional["RuntimeStyleSelection"]
    img2img_denoise: Optional[float]
    future: asyncio.Future


@dataclass
class WorkflowStyleOptions:
    models: List[str]
    loras: List[str]
    default_model: Optional[str]
    default_lora: Optional[str]


@dataclass
class RuntimeStyleSelection:
    model: Optional[str]
    lora: Optional[str]
    lora_enabled: bool


@dataclass
class UserStyleSelection:
    model: Optional[str]
    lora: Optional[str]
    lora_enabled: bool
    expires_at: float


draw_queue: "asyncio.Queue[DrawTask]" = asyncio.Queue()
queue_state_lock = asyncio.Lock()
worker_started = False
is_processing = False
draw_cooldown_ts: Dict[str, float] = {}
draw_daily_usage: Dict[str, Tuple[str, int]] = {}
workflow_capability_cache: Dict[str, bool] = {}
text2img_style_cache: Dict[str, Any] = {}
user_style_selection_map: Dict[str, UserStyleSelection] = {}

# 注册指令
draw_cmd = on_command("画", aliases={"draw"}, priority=5, block=True)
img2img_cmd = on_command("图生图", aliases={"img2img"}, priority=5, block=True)
upscale_removed_cmd = on_command("超分", aliases={"upscale"}, priority=5, block=True)
model_cmd = on_command("模型", aliases={"model", "setmodel", "切换模型"}, priority=5, block=True)
lora_cmd = on_command("lora", aliases={"LoRA", "setlora", "切换lora", "切换LoRA"}, priority=5, block=True)

LIST_TOKENS = {"list", "ls", "列表"}
LORA_ENABLE_TOKENS = {"on", "enable", "1", "开", "开启", "启用"}
LORA_DISABLE_TOKENS = {"off", "disable", "0", "关", "关闭", "禁用", "none", "不使用", "不用"}


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


def _is_group_allowed(event: MessageEvent) -> bool:
    if not isinstance(event, GroupMessageEvent):
        return True
    group_id = event.group_id
    if group_whitelist and group_id not in group_whitelist:
        return False
    if group_id in group_blacklist:
        return False
    return True


def _cooldown_key(event: MessageEvent) -> str:
    if isinstance(event, GroupMessageEvent):
        return f"group_{event.group_id}:user_{event.user_id}"
    return f"user_{event.user_id}"


def _cooldown_remaining(key: str) -> float:
    if draw_cooldown_seconds <= 0:
        return 0
    elapsed = time.time() - draw_cooldown_ts.get(key, 0)
    remaining = draw_cooldown_seconds - elapsed
    return max(0, remaining)


def _check_and_consume_daily_limit(user_id: str) -> Tuple[bool, int]:
    if draw_daily_limit <= 0:
        return True, -1
    today = date.today().isoformat()
    last_day, used_count = draw_daily_usage.get(user_id, (today, 0))
    if last_day != today:
        used_count = 0
    if used_count >= draw_daily_limit:
        return False, 0
    used_count += 1
    draw_daily_usage[user_id] = (today, used_count)
    return True, draw_daily_limit - used_count


def _parse_prompt_options(raw_text: str) -> Tuple[str, Optional[str]]:
    orientation: Optional[str] = None
    if LANDSCAPE_PATTERN.search(raw_text):
        orientation = "landscape"
        raw_text = LANDSCAPE_PATTERN.sub(" ", raw_text).strip()
    if PORTRAIT_PATTERN.search(raw_text):
        orientation = "portrait"
        raw_text = PORTRAIT_PATTERN.sub(" ", raw_text).strip()
    return " ".join(raw_text.split()), orientation


def _parse_img2img_options(
    raw_text: str,
) -> Tuple[str, Optional[str], Optional[float], Optional[str]]:
    denoise_value: Optional[float] = None
    denoise_error: Optional[str] = None
    denoise_match = IMG2IMG_DENOISE_PATTERN.search(raw_text)
    cleaned_text = raw_text

    if denoise_match:
        value_text = denoise_match.group(1).strip()
        cleaned_text = (
            f"{raw_text[:denoise_match.start()]} {raw_text[denoise_match.end():]}"
        ).strip()
        try:
            value = float(value_text)
            if 0 < value < 1:
                denoise_value = value
            else:
                denoise_error = (
                    "denoise 参数必须大于 0 且小于 1，"
                    "例如：`/图生图 赛博朋克 --denoise 0.7`。"
                )
        except ValueError:
            denoise_error = (
                "denoise 参数格式错误，"
                "例如：`/图生图 赛博朋克 --denoise 0.7`。"
            )
    elif IMG2IMG_DENOISE_FLAG_PATTERN.search(raw_text):
        denoise_error = (
            "检测到 denoise 标记，但未提供数值。"
            "请使用：`/图生图 赛博朋克 --denoise 0.7`。"
        )

    prompt_text, orientation = _parse_prompt_options(cleaned_text)
    return prompt_text, orientation, denoise_value, denoise_error


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
    for segment in message:
        if segment.type != "image":
            continue
        url = str(segment.data.get("url") or "").strip()
        if url.startswith("http://") or url.startswith("https://"):
            return url
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


def _load_workflow(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _workflow_supports_input_image(path: Path) -> bool:
    key = str(path.resolve())
    cached = workflow_capability_cache.get(key)
    if cached is not None:
        return cached
    if not path.exists():
        workflow_capability_cache[key] = False
        return False
    try:
        workflow = _load_workflow(path)
    except Exception:
        workflow_capability_cache[key] = False
        return False
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        if node.get("class_type") not in {"LoadImage", "LoadImageMask"}:
            continue
        inputs = node.get("inputs", {})
        if isinstance(inputs, dict) and "image" in inputs:
            workflow_capability_cache[key] = True
            return True
    workflow_capability_cache[key] = False
    return False


def _dedup_keep_order(items: List[str]) -> List[str]:
    seen: Set[str] = set()
    result: List[str] = []
    for item in items:
        value = str(item).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _extract_filenames_from_payload(payload: Any, exts: Tuple[str, ...]) -> List[str]:
    collected: List[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            text = value.strip()
            if text and text.lower().endswith(exts):
                collected.append(text)
            return
        if isinstance(value, dict):
            for nested in value.values():
                walk(nested)
            return
        if isinstance(value, (list, tuple, set)):
            for nested in value:
                walk(nested)

    walk(payload)
    return _dedup_keep_order(collected)


def _http_get_json(path: str) -> Optional[Any]:
    url = f"http://{comfy_server}{path}"
    try:
        with urllib.request.urlopen(url, timeout=comfy_http_timeout_seconds) as response:
            return json.loads(response.read())
    except Exception as e:
        logger.debug(f"ComfyUI {path} 不可用: {e}")
        return None


def _extract_catalog_from_object_info(object_info: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    models: List[str] = []
    loras: List[str] = []
    checkpoint_classes = {"CheckpointLoaderSimple", "CheckpointLoader", "CheckpointLoaderNF4"}

    for class_name, node_def in object_info.items():
        if not isinstance(node_def, dict):
            continue
        input_def = node_def.get("input", {})
        if not isinstance(input_def, dict):
            continue

        sections: List[Dict[str, Any]] = []
        for sec_name in ("required", "optional"):
            sec = input_def.get(sec_name, {})
            if isinstance(sec, dict):
                sections.append(sec)

        if class_name in checkpoint_classes:
            for section in sections:
                if "ckpt_name" in section:
                    models.extend(
                        _extract_filenames_from_payload(
                            section.get("ckpt_name"),
                            MODEL_FILE_EXTENSIONS,
                        )
                    )

        if "lora" in class_name.lower():
            for section in sections:
                for key, value in section.items():
                    if "lora_name" in str(key).lower():
                        loras.extend(
                            _extract_filenames_from_payload(
                                value,
                                LORA_FILE_EXTENSIONS,
                            )
                        )

    return _dedup_keep_order(models), _dedup_keep_order(loras)


def _fetch_comfyui_style_catalog() -> Tuple[List[str], List[str]]:
    models: List[str] = []
    loras: List[str] = []

    checkpoints_payload = _http_get_json("/models/checkpoints")
    if checkpoints_payload is not None:
        models.extend(_extract_filenames_from_payload(checkpoints_payload, MODEL_FILE_EXTENSIONS))

    loras_payload = _http_get_json("/models/loras")
    if loras_payload is not None:
        loras.extend(_extract_filenames_from_payload(loras_payload, LORA_FILE_EXTENSIONS))

    models_payload = _http_get_json("/models")
    if isinstance(models_payload, dict):
        if "checkpoints" in models_payload:
            models.extend(
                _extract_filenames_from_payload(models_payload.get("checkpoints"), MODEL_FILE_EXTENSIONS)
            )
        if "loras" in models_payload:
            loras.extend(_extract_filenames_from_payload(models_payload.get("loras"), LORA_FILE_EXTENSIONS))

    object_info = _http_get_json("/object_info")
    if isinstance(object_info, dict):
        obj_models, obj_loras = _extract_catalog_from_object_info(object_info)
        models.extend(obj_models)
        loras.extend(obj_loras)

    return _dedup_keep_order(models), _dedup_keep_order(loras)


def _get_comfyui_style_catalog(force_reload: bool = False) -> Tuple[List[str], List[str]]:
    now = time.time()
    cached_catalog = text2img_style_cache.get("catalog")
    cached_at = float(text2img_style_cache.get("catalog_ts", 0))
    if (
        not force_reload
        and isinstance(cached_catalog, dict)
        and now - cached_at <= style_catalog_cache_seconds
    ):
        return (
            list(cached_catalog.get("models") or []),
            list(cached_catalog.get("loras") or []),
        )

    fetched_models, fetched_loras = _fetch_comfyui_style_catalog()
    if fetched_models or fetched_loras:
        text2img_style_cache["catalog"] = {"models": fetched_models, "loras": fetched_loras}
        text2img_style_cache["catalog_ts"] = now
        return fetched_models, fetched_loras

    if isinstance(cached_catalog, dict):
        return (
            list(cached_catalog.get("models") or []),
            list(cached_catalog.get("loras") or []),
        )
    return [], []


def _parse_temp_lora_names(value: object) -> List[str]:
    text = str(value or "").strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except Exception:
        return []
    if not isinstance(payload, list):
        return []

    lora_names: List[str] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        for key in ("lora", "display_name", "name"):
            raw_name = str(item.get(key) or "").strip()
            if raw_name:
                lora_names.append(raw_name)
                break
    return lora_names


def _extract_text2img_style_options(workflow: Dict[str, Any]) -> WorkflowStyleOptions:
    model_names: List[str] = []
    lora_names: List[str] = []
    default_model: Optional[str] = None
    default_lora: Optional[str] = None

    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        class_type = str(node.get("class_type") or "")
        inputs = node.get("inputs", {})
        if not isinstance(inputs, dict):
            continue

        if class_type in {"CheckpointLoaderSimple", "CheckpointLoader", "CheckpointLoaderNF4"}:
            ckpt_name = str(inputs.get("ckpt_name") or "").strip()
            if ckpt_name:
                model_names.append(ckpt_name)
                if default_model is None:
                    default_model = ckpt_name

        if "lora_name" in inputs:
            lora_name = str(inputs.get("lora_name") or "").strip()
            if lora_name:
                lora_names.append(lora_name)
                if default_lora is None:
                    default_lora = lora_name

        if class_type == "WeiLinPromptUIOnlyLoraStack":
            parsed = _parse_temp_lora_names(inputs.get("temp_lora_str"))
            if parsed:
                lora_names.extend(parsed)
                if default_lora is None:
                    default_lora = parsed[0]

    model_names = _dedup_keep_order(model_names)
    lora_names = _dedup_keep_order(lora_names)
    if default_model and default_model not in model_names:
        default_model = model_names[0] if model_names else None
    if default_lora and default_lora not in lora_names:
        default_lora = lora_names[0] if lora_names else None

    return WorkflowStyleOptions(
        models=model_names,
        loras=lora_names,
        default_model=default_model,
        default_lora=default_lora,
    )


def _style_owner_key(event: MessageEvent) -> str:
    return str(event.user_id)


def _cleanup_expired_user_styles() -> None:
    now = time.time()
    expired_keys = [key for key, value in user_style_selection_map.items() if value.expires_at <= now]
    for key in expired_keys:
        user_style_selection_map.pop(key, None)


def _default_runtime_style(options: WorkflowStyleOptions) -> RuntimeStyleSelection:
    model = options.default_model or (options.models[0] if options.models else None)
    lora = options.default_lora or (options.loras[0] if options.loras else None)
    return RuntimeStyleSelection(model=model, lora=lora, lora_enabled=bool(lora))


def _sanitize_runtime_style(
    style: RuntimeStyleSelection,
    options: WorkflowStyleOptions,
) -> RuntimeStyleSelection:
    default_style = _default_runtime_style(options)

    if options.models:
        model = style.model if style.model in options.models else default_style.model
    else:
        model = None

    if options.loras:
        lora = style.lora if style.lora in options.loras else default_style.lora
        lora_enabled = bool(lora) and style.lora_enabled
    else:
        lora = None
        lora_enabled = False

    return RuntimeStyleSelection(model=model, lora=lora, lora_enabled=lora_enabled)


def _format_remaining_seconds(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}秒"
    if seconds < 3600:
        mins, sec = divmod(seconds, 60)
        return f"{mins}分{sec}秒"
    hours, rem = divmod(seconds, 3600)
    mins, sec = divmod(rem, 60)
    return f"{hours}小时{mins}分{sec}秒"


def _get_user_runtime_style(
    owner_key: str,
    options: WorkflowStyleOptions,
) -> Tuple[RuntimeStyleSelection, Optional[int]]:
    _cleanup_expired_user_styles()
    default_style = _default_runtime_style(options)
    stored = user_style_selection_map.get(owner_key)
    if stored is None:
        return default_style, None

    remaining = max(0, int(stored.expires_at - time.time()))
    runtime_style = _sanitize_runtime_style(
        RuntimeStyleSelection(
            model=stored.model,
            lora=stored.lora,
            lora_enabled=stored.lora_enabled,
        ),
        options,
    )

    # 如果工作流更新导致用户选择失效，则回写为修正后的值，保留原过期时间。
    if (
        runtime_style.model != stored.model
        or runtime_style.lora != stored.lora
        or runtime_style.lora_enabled != stored.lora_enabled
    ):
        user_style_selection_map[owner_key] = UserStyleSelection(
            model=runtime_style.model,
            lora=runtime_style.lora,
            lora_enabled=runtime_style.lora_enabled,
            expires_at=stored.expires_at,
        )

    return runtime_style, remaining


def _save_user_runtime_style(owner_key: str, style: RuntimeStyleSelection) -> UserStyleSelection:
    expires_at = time.time() + draw_user_style_ttl_seconds
    saved = UserStyleSelection(
        model=style.model,
        lora=style.lora,
        lora_enabled=style.lora_enabled,
        expires_at=expires_at,
    )
    user_style_selection_map[owner_key] = saved
    return saved


def _sync_text2img_style_state(options: WorkflowStyleOptions) -> None:
    _cleanup_expired_user_styles()
    for key, stored in list(user_style_selection_map.items()):
        runtime_style = _sanitize_runtime_style(
            RuntimeStyleSelection(
                model=stored.model,
                lora=stored.lora,
                lora_enabled=stored.lora_enabled,
            ),
            options,
        )
        if (
            runtime_style.model != stored.model
            or runtime_style.lora != stored.lora
            or runtime_style.lora_enabled != stored.lora_enabled
        ):
            user_style_selection_map[key] = UserStyleSelection(
                model=runtime_style.model,
                lora=runtime_style.lora,
                lora_enabled=runtime_style.lora_enabled,
                expires_at=stored.expires_at,
            )


def _get_text2img_style_options(force_reload: bool = False) -> WorkflowStyleOptions:
    empty = WorkflowStyleOptions(models=[], loras=[], default_model=None, default_lora=None)
    if not text2img_workflow_path.exists():
        return empty

    try:
        stat = text2img_workflow_path.stat()
        signature = (
            f"{text2img_workflow_path.resolve()}:{stat.st_mtime_ns}:{stat.st_size}"
        )
    except OSError:
        return empty

    cached_signature = text2img_style_cache.get("signature")
    cached_options = text2img_style_cache.get("options")
    if (
        not force_reload
        and cached_signature == signature
        and isinstance(cached_options, WorkflowStyleOptions)
    ):
        _sync_text2img_style_state(cached_options)
        return cached_options

    try:
        workflow = _load_workflow(text2img_workflow_path)
        options = _extract_text2img_style_options(workflow)
    except Exception as e:
        logger.warning(f"读取文生图工作流模型/LoRA信息失败: {e}")
        options = empty

    if draw_list_all_local_styles:
        local_models, local_loras = _get_comfyui_style_catalog(force_reload=force_reload)
        if local_models:
            options.models = _dedup_keep_order(options.models + local_models)
        if local_loras:
            options.loras = _dedup_keep_order(options.loras + local_loras)
        if options.default_model is None and options.models:
            options.default_model = options.models[0]
        if options.default_lora is None and options.loras:
            options.default_lora = options.loras[0]

    text2img_style_cache["signature"] = signature
    text2img_style_cache["options"] = options
    _sync_text2img_style_state(options)
    return options


def _normalize_choice(text: str) -> str:
    return " ".join(text.strip().split())


def _match_choice(text: str, choices: List[str]) -> Optional[str]:
    target = _normalize_choice(text)
    if not target:
        return None
    for choice in choices:
        if choice == target:
            return choice

    target_lower = target.lower()
    for choice in choices:
        if choice.lower() == target_lower:
            return choice

    contains_matches = [choice for choice in choices if target_lower in choice.lower()]
    if len(contains_matches) == 1:
        return contains_matches[0]
    return None


def _format_named_list(items: List[str]) -> str:
    if not items:
        return "（无）"
    return "\n".join(f"{idx}. {name}" for idx, name in enumerate(items, start=1))


def _build_model_status_text(
    options: WorkflowStyleOptions,
    current_style: RuntimeStyleSelection,
    remaining_seconds: Optional[int],
) -> str:
    current = current_style.model or "（未设置）"
    expire_text = (
        "当前使用默认模型。"
        if remaining_seconds is None
        else f"你的模型设置剩余 {_format_remaining_seconds(remaining_seconds)}，到期后恢复默认。"
    )
    return (
        f"当前模型：{current}\n"
        f"可选模型（共 {len(options.models)} 个）：\n"
        f"{_format_named_list(options.models)}\n"
        "用法：`/模型 模型名`，或 `/模型 列表`\n"
        f"{expire_text}"
    )


def _build_lora_status_text(
    options: WorkflowStyleOptions,
    current_style: RuntimeStyleSelection,
    remaining_seconds: Optional[int],
) -> str:
    if not options.loras:
        return "当前工作流里没有可切换的 LoRA 节点。"
    current = current_style.lora or "（未设置）"
    status = "开启" if current_style.lora_enabled else "关闭"
    expire_text = (
        "当前使用默认LoRA配置。"
        if remaining_seconds is None
        else f"你的LoRA设置剩余 {_format_remaining_seconds(remaining_seconds)}，到期后恢复默认。"
    )
    return (
        f"LoRA状态：{status}\n"
        f"当前LoRA：{current}\n"
        f"可选LoRA（共 {len(options.loras)} 个）：\n"
        f"{_format_named_list(options.loras)}\n"
        "用法：`/lora 名称`、`/lora 开启`、`/lora 关闭`、`/lora 列表`\n"
        f"{expire_text}"
    )


def _apply_selected_model(workflow: Dict[str, Any], model_name: Optional[str]) -> None:
    if not model_name:
        return
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        class_type = str(node.get("class_type") or "")
        if class_type not in {"CheckpointLoaderSimple", "CheckpointLoader", "CheckpointLoaderNF4"}:
            continue
        inputs = node.get("inputs", {})
        if isinstance(inputs, dict) and "ckpt_name" in inputs:
            inputs["ckpt_name"] = model_name


def _apply_selected_lora(
    workflow: Dict[str, Any],
    lora_name: Optional[str],
    lora_enabled: bool,
) -> None:
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        class_type = str(node.get("class_type") or "")
        if "lora" not in class_type.lower():
            continue
        inputs = node.get("inputs", {})
        if not isinstance(inputs, dict):
            continue
        if "lora_name" not in inputs:
            continue

        if lora_name:
            inputs["lora_name"] = lora_name

        if not lora_enabled:
            if "strength_model" in inputs:
                inputs["strength_model"] = 0
            if "strength_clip" in inputs:
                inputs["strength_clip"] = 0


def _apply_text2img_style_selection(
    workflow: Dict[str, Any],
    style_selection: Optional[RuntimeStyleSelection],
) -> None:
    options = _get_text2img_style_options()
    effective = _sanitize_runtime_style(
        style_selection or _default_runtime_style(options),
        options,
    )
    _apply_selected_model(workflow, effective.model)
    _apply_selected_lora(workflow, effective.lora, effective.lora_enabled)


def _apply_prompt(workflow: Dict[str, Any], prompt_text: str) -> None:
    if not prompt_text:
        return
    if "2" in workflow and isinstance(workflow["2"], dict):
        inputs = workflow["2"].get("inputs", {})
        if isinstance(inputs, dict):
            inputs["text"] = prompt_text
            return

    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs", {})
        if isinstance(inputs, dict) and "text" in inputs:
            inputs["text"] = prompt_text
            return


def _apply_size(workflow: Dict[str, Any], orientation: Optional[str], task_type: str) -> None:
    if task_type == "img2img":
        if orientation == "landscape":
            width, height = DEFAULT_IMG2IMG_LANDSCAPE_SIZE
        else:
            width, height = DEFAULT_IMG2IMG_PORTRAIT_SIZE
    else:
        if orientation == "landscape":
            width, height = DEFAULT_LANDSCAPE_SIZE
        else:
            width, height = DEFAULT_PORTRAIT_SIZE

    # 文生图：修改任意 EmptyLatentImage 的宽高，不依赖固定节点编号
    latent_updated = False
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        if node.get("class_type") != "EmptyLatentImage":
            continue
        inputs = node.get("inputs", {})
        if isinstance(inputs, dict) and "width" in inputs and "height" in inputs:
            inputs["width"] = width
            inputs["height"] = height
            latent_updated = True

    if task_type != "img2img" and latent_updated:
        return

    # 图生图：若工作流无固定宽高节点，则注入 ImageScale 节点来控制分辨率
    if task_type == "img2img":
        _inject_img2img_resize_node(workflow, width, height)


def _next_node_id(workflow: Dict[str, Any]) -> str:
    numeric_ids = []
    for key in workflow.keys():
        try:
            numeric_ids.append(int(str(key)))
        except Exception:
            continue
    return str(max(numeric_ids) + 1) if numeric_ids else "1000"


def _inject_img2img_resize_node(workflow: Dict[str, Any], width: int, height: int) -> None:
    updated = False
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        if node.get("class_type") != "ImageScale":
            continue
        inputs = node.get("inputs", {})
        if not isinstance(inputs, dict):
            continue
        if "width" in inputs and "height" in inputs and "image" in inputs:
            inputs["width"] = width
            inputs["height"] = height
            if "crop" in inputs:
                inputs["crop"] = "disabled"
            updated = True
    if updated:
        return

    target_vae_node_id = None
    target_pixels = None
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        if node.get("class_type") != "VAEEncode":
            continue
        inputs = node.get("inputs", {})
        pixels = inputs.get("pixels")
        if isinstance(inputs, dict) and isinstance(pixels, list) and len(pixels) >= 2:
            target_vae_node_id = str(node_id)
            target_pixels = pixels
            break
    if target_vae_node_id is None or target_pixels is None:
        return

    new_node_id = _next_node_id(workflow)
    workflow[new_node_id] = {
        "inputs": {
            "upscale_method": "lanczos",
            "width": width,
            "height": height,
            "crop": "disabled",
            "image": target_pixels,
        },
        "class_type": "ImageScale",
        "_meta": {"title": "Auto Resize For Img2Img"},
    }
    workflow[target_vae_node_id]["inputs"]["pixels"] = [new_node_id, 0]


def _apply_input_image(workflow: Dict[str, Any], image_name: str) -> None:
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        if node.get("class_type") not in {"LoadImage", "LoadImageMask"}:
            continue
        inputs = node.get("inputs", {})
        if isinstance(inputs, dict) and "image" in inputs:
            inputs["image"] = image_name
            return

    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs", {})
        if isinstance(inputs, dict) and "image" in inputs and isinstance(inputs["image"], str):
            inputs["image"] = image_name
            return

    raise RuntimeError("图生图工作流未找到可替换的 LoadImage 节点")


def _apply_post_upscale_scale(workflow: Dict[str, Any]) -> None:
    has_upscale_model = any(
        isinstance(node, dict) and node.get("class_type") == "ImageUpscaleWithModel"
        for node in workflow.values()
    )
    if not has_upscale_model:
        return

    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        if node.get("class_type") != "ImageScaleBy":
            continue
        inputs = node.get("inputs", {})
        if isinstance(inputs, dict) and "scale_by" in inputs:
            inputs["scale_by"] = post_upscale_scale_by
            return


def _randomize_seeds(workflow: Dict[str, Any]) -> None:
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        if node.get("class_type") != "KSampler":
            continue
        inputs = node.get("inputs", {})
        if isinstance(inputs, dict) and "seed" in inputs:
            inputs["seed"] = random.randint(1, 2**60)


def _apply_img2img_denoise(workflow: Dict[str, Any], denoise_value: float) -> bool:
    updated = False
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        if node.get("class_type") != "KSampler":
            continue
        inputs = node.get("inputs", {})
        if isinstance(inputs, dict) and "denoise" in inputs:
            inputs["denoise"] = denoise_value
            updated = True
    return updated


def queue_prompt(prompt_workflow: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    payload = {"prompt": prompt_workflow, "client_id": str(uuid.uuid4())}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"http://{comfy_server}/prompt",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=comfy_http_timeout_seconds) as response:
            return json.loads(response.read())
    except Exception as e:
        logger.error("ComfyUI /prompt error: %s", e)
        return None


def get_image(filename: str, subfolder: str, folder_type: str) -> bytes:
    data = {"filename": filename, "subfolder": subfolder, "type": folder_type}
    url_values = urllib.parse.urlencode(data)
    with urllib.request.urlopen(
        f"http://{comfy_server}/view?{url_values}", timeout=comfy_http_timeout_seconds
    ) as response:
        return response.read()


def get_history(prompt_id: str) -> Dict[str, Any]:
    try:
        with urllib.request.urlopen(
            f"http://{comfy_server}/history/{prompt_id}",
            timeout=comfy_http_timeout_seconds,
        ) as response:
            return json.loads(response.read())
    except Exception as e:
        logger.debug("ComfyUI /history error: %s", e)
        return {}


def _extract_image_info(outputs: Dict[str, Any]) -> Optional[Dict[str, str]]:
    node_34 = outputs.get("34")
    if isinstance(node_34, dict):
        node_images = node_34.get("images", [])
        if node_images:
            return node_images[0]

    for output in outputs.values():
        if not isinstance(output, dict):
            continue
        images = output.get("images", [])
        if images:
            return images[0]
    return None


def _validate_input_image_size(width: int, height: int) -> None:
    short_side = min(width, height)
    long_side = max(width, height)
    if short_side <= MAX_INPUT_SHORT_SIDE and long_side <= MAX_INPUT_LONG_SIDE:
        return
    raise ValueError(
        (
            f"输入图片尺寸 {width}x{height} 超出限制。"
            f"最大支持短边 {MAX_INPUT_SHORT_SIDE}、长边 {MAX_INPUT_LONG_SIDE}（即 1536x2048 范围内）。"
        )
    )


async def _upload_image_to_comfy(image_url: str) -> str:
    timeout = httpx.Timeout(comfy_http_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as http_client:
        image_resp = await http_client.get(image_url)
        image_resp.raise_for_status()
        try:
            with Image.open(io.BytesIO(image_resp.content)) as pil_img:
                width, height = pil_img.size
        except Exception as e:
            raise RuntimeError(f"无法识别输入图片格式: {e}") from e

        _validate_input_image_size(width, height)

        content_type = image_resp.headers.get("Content-Type", "application/octet-stream")
        suffix = ".jpg"
        if "png" in content_type.lower():
            suffix = ".png"
        filename = f"qq_{uuid.uuid4().hex}{suffix}"
        upload_url = f"http://{comfy_server}/upload/image"
        files = {"image": (filename, image_resp.content, content_type)}
        data = {"type": "input", "overwrite": "true"}
        upload_resp = await http_client.post(upload_url, files=files, data=data)
        upload_resp.raise_for_status()
        payload = upload_resp.json()
        image_name = str(payload.get("name") or payload.get("filename") or "").strip()
        if not image_name:
            raise RuntimeError(f"ComfyUI 上传响应异常: {payload}")
        return image_name


async def _generate_image(task: DrawTask) -> Tuple[bytes, float]:
    start_time = time.monotonic()
    if not task.workflow_path.exists():
        raise FileNotFoundError(f"找不到工作流文件: {task.workflow_path}")

    workflow = await asyncio.to_thread(_load_workflow, task.workflow_path)
    if task.task_type == "text2img":
        _apply_text2img_style_selection(workflow, task.style_selection)
    _apply_prompt(workflow, task.prompt_text)
    _apply_size(workflow, task.orientation, task.task_type)
    if task.task_type == "img2img" and task.img2img_denoise is not None:
        denoise_applied = _apply_img2img_denoise(workflow, task.img2img_denoise)
        if not denoise_applied:
            raise RuntimeError("当前图生图工作流未找到可调 denoise 的 KSampler 节点。")
    if task.image_url:
        comfy_name = await _upload_image_to_comfy(task.image_url)
        _apply_input_image(workflow, comfy_name)
    _apply_post_upscale_scale(workflow)
    _randomize_seeds(workflow)

    queue_result = await asyncio.to_thread(queue_prompt, workflow)
    if not queue_result or "prompt_id" not in queue_result:
        raise RuntimeError("提交 ComfyUI 任务失败，请检查服务是否可用")

    prompt_id = str(queue_result["prompt_id"])
    deadline = time.monotonic() + draw_timeout_seconds
    while time.monotonic() < deadline:
        await asyncio.sleep(draw_poll_interval_seconds)
        history = await asyncio.to_thread(get_history, prompt_id)
        task_result = history.get(prompt_id, {})
        outputs = task_result.get("outputs", {})
        if not isinstance(outputs, dict):
            continue

        image_info = _extract_image_info(outputs)
        if image_info:
            image_bytes = await asyncio.to_thread(
                get_image,
                image_info.get("filename", ""),
                image_info.get("subfolder", ""),
                image_info.get("type", "output"),
            )
            if not image_bytes:
                raise RuntimeError("获取图片结果为空")
            return image_bytes, time.monotonic() - start_time

    raise TimeoutError("ComfyUI 生图超时")


async def _ensure_worker_started() -> None:
    global worker_started
    async with queue_state_lock:
        if worker_started:
            return
        worker_started = True
    asyncio.create_task(_draw_worker(), name="comfyui-draw-worker")


async def _enqueue_task(task: DrawTask) -> int:
    # 返回排在前面的任务数量
    async with queue_state_lock:
        ahead_count = draw_queue.qsize() + (1 if is_processing else 0)
        await draw_queue.put(task)
        return ahead_count


async def _draw_worker() -> None:
    global is_processing
    while True:
        task = await draw_queue.get()
        async with queue_state_lock:
            is_processing = True
        try:
            result = await _generate_image(task)
            if not task.future.done():
                task.future.set_result(result)
        except Exception as e:
            if not task.future.done():
                task.future.set_exception(e)
        finally:
            async with queue_state_lock:
                is_processing = False
            draw_queue.task_done()


async def _submit_draw_task(
    event: MessageEvent,
    prompt_text: str,
    orientation: Optional[str],
    image_url: Optional[str],
    workflow_path: Path,
    img2img_denoise: Optional[float],
    matcher,
) -> None:
    if not _is_group_allowed(event):
        await matcher.finish(_build_command_reply_message(event, "本群未开通生图功能。"))

    if not workflow_path.exists():
        await matcher.finish(_build_command_reply_message(event, f"找不到工作流文件：{workflow_path}"))

    cooldown_key = _cooldown_key(event)
    cooldown_remain = _cooldown_remaining(cooldown_key)
    if cooldown_remain > 0:
        wait_seconds = int(cooldown_remain) + 1
        await matcher.finish(
            _build_command_reply_message(event, f"生图请求太快了，请 {wait_seconds} 秒后再试。")
        )

    allowed, remain_quota = _check_and_consume_daily_limit(str(event.user_id))
    if not allowed:
        await matcher.finish(_build_command_reply_message(event, "今日生图额度已用完，请明天再来。"))

    draw_cooldown_ts[cooldown_key] = time.time()
    await _ensure_worker_started()

    style_selection: Optional[RuntimeStyleSelection] = None
    if image_url is None:
        owner_key = _style_owner_key(event)
        style_options = _get_text2img_style_options()
        style_selection, _ = _get_user_runtime_style(owner_key, style_options)

    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    task = DrawTask(
        prompt_text=prompt_text,
        orientation=orientation,
        image_url=image_url,
        workflow_path=workflow_path,
        task_type="img2img" if image_url else "text2img",
        style_selection=style_selection,
        img2img_denoise=img2img_denoise,
        future=future,
    )
    ahead_count = await _enqueue_task(task)

    if ahead_count > 0:
        await matcher.send(
            _build_command_reply_message(event, f"已加入生图队列，当前前方 {ahead_count} 人，请稍候...")
        )
    else:
        await matcher.send(_build_command_reply_message(event, "正在为你生成图片，请稍候..."))

    try:
        image_data, elapsed = await future
    except ValueError as e:
        await matcher.finish(_build_command_reply_message(event, str(e)))
    except TimeoutError:
        await matcher.finish(_build_command_reply_message(event, "生图超时，请稍后重试。"))
    except Exception as e:
        logger.exception("Draw task failed: %s", e)
        await matcher.finish(
            _build_command_reply_message(event, "生图失败，请检查 ComfyUI 工作流与服务状态。")
        )

    await matcher.send(_build_image_message(event, image_data))
    finish_text = f"生成完成，耗时 {elapsed:.1f} 秒。"
    if remain_quota >= 0:
        finish_text += f" 今日剩余额度：{remain_quota} 次。"
    await matcher.finish(with_roleplay(finish_text))


async def generate_text2img_for_scheduler(
    prompt_text: str,
    orientation: Optional[str] = "landscape",
) -> Tuple[bytes, float]:
    await _ensure_worker_started()
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    task = DrawTask(
        prompt_text=prompt_text,
        orientation=orientation,
        image_url=None,
        workflow_path=text2img_workflow_path,
        task_type="text2img",
        style_selection=None,
        img2img_denoise=None,
        future=future,
    )
    await _enqueue_task(task)
    return await future


@draw_cmd.handle()
async def handle_draw(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    raw_prompt = args.extract_plain_text().strip()
    if not raw_prompt:
        await draw_cmd.finish(
            _build_command_reply_message(
                event,
                "请在指令后输入描述，例如：/画 星空少女（默认竖图1024x1536，横图可加“横图”）",
            )
        )
    prompt_text, orientation = _parse_prompt_options(raw_prompt)
    if not prompt_text:
        await draw_cmd.finish(_build_command_reply_message(event, "描述不能为空。"))

    await _submit_draw_task(
        event=event,
        prompt_text=prompt_text,
        orientation=orientation,
        image_url=None,
        workflow_path=text2img_workflow_path,
        img2img_denoise=None,
        matcher=draw_cmd,
    )


@model_cmd.handle()
async def handle_model_command(event: MessageEvent, args: Message = CommandArg()):
    options = _get_text2img_style_options(force_reload=True)
    if not options.models:
        await model_cmd.finish(
            _build_command_reply_message(event, "当前文生图工作流未发现可切换模型节点。")
        )

    owner_key = _style_owner_key(event)
    current_style, remaining_seconds = _get_user_runtime_style(owner_key, options)
    raw_arg = _normalize_choice(args.extract_plain_text())
    lowered = raw_arg.lower()
    if not raw_arg or raw_arg in LIST_TOKENS or lowered in LIST_TOKENS:
        await model_cmd.finish(
            _build_command_reply_message(
                event,
                _build_model_status_text(options, current_style, remaining_seconds),
            )
        )

    matched = _match_choice(raw_arg, options.models)
    if not matched:
        await model_cmd.finish(
            _build_command_reply_message(
                event,
                "未找到该模型，请发送 `/模型 列表` 查看可选项。",
            )
        )

    saved = _save_user_runtime_style(
        owner_key,
        RuntimeStyleSelection(
            model=matched,
            lora=current_style.lora,
            lora_enabled=current_style.lora_enabled,
        ),
    )
    ttl_tip = _format_remaining_seconds(max(0, int(saved.expires_at - time.time())))
    await model_cmd.finish(
        _build_command_reply_message(
            event,
            f"已为你切换文生图模型为：{matched}\n将在 {ttl_tip} 后恢复默认。",
        )
    )


@lora_cmd.handle()
async def handle_lora_command(event: MessageEvent, args: Message = CommandArg()):
    options = _get_text2img_style_options(force_reload=True)
    owner_key = _style_owner_key(event)
    current_style, remaining_seconds = _get_user_runtime_style(owner_key, options)
    raw_arg = _normalize_choice(args.extract_plain_text())
    lowered = raw_arg.lower()

    if not raw_arg or raw_arg in LIST_TOKENS or lowered in LIST_TOKENS:
        await lora_cmd.finish(
            _build_command_reply_message(
                event,
                _build_lora_status_text(options, current_style, remaining_seconds),
            )
        )

    if not options.loras:
        await lora_cmd.finish(_build_command_reply_message(event, "当前工作流没有可切换的 LoRA。"))

    if raw_arg in LORA_DISABLE_TOKENS or lowered in LORA_DISABLE_TOKENS:
        saved = _save_user_runtime_style(
            owner_key,
            RuntimeStyleSelection(
                model=current_style.model,
                lora=current_style.lora,
                lora_enabled=False,
            ),
        )
        ttl_tip = _format_remaining_seconds(max(0, int(saved.expires_at - time.time())))
        await lora_cmd.finish(
            _build_command_reply_message(event, f"已为你关闭 LoRA。\n将在 {ttl_tip} 后恢复默认。")
        )

    if raw_arg in LORA_ENABLE_TOKENS or lowered in LORA_ENABLE_TOKENS:
        chosen_lora = current_style.lora
        if chosen_lora not in options.loras:
            chosen_lora = options.default_lora or options.loras[0]
        saved = _save_user_runtime_style(
            owner_key,
            RuntimeStyleSelection(
                model=current_style.model,
                lora=chosen_lora,
                lora_enabled=True,
            ),
        )
        ttl_tip = _format_remaining_seconds(max(0, int(saved.expires_at - time.time())))
        await lora_cmd.finish(
            _build_command_reply_message(
                event,
                f"已为你开启 LoRA：{chosen_lora}\n将在 {ttl_tip} 后恢复默认。",
            )
        )

    matched = _match_choice(raw_arg, options.loras)
    if not matched:
        await lora_cmd.finish(
            _build_command_reply_message(event, "未找到该 LoRA，请发送 `/lora 列表` 查看可选项。")
        )

    saved = _save_user_runtime_style(
        owner_key,
        RuntimeStyleSelection(
            model=current_style.model,
            lora=matched,
            lora_enabled=True,
        ),
    )
    ttl_tip = _format_remaining_seconds(max(0, int(saved.expires_at - time.time())))
    await lora_cmd.finish(
        _build_command_reply_message(
            event,
            f"已为你切换 LoRA 为：{matched}\n将在 {ttl_tip} 后恢复默认。",
        )
    )


@img2img_cmd.handle()
async def handle_img2img(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    raw_prompt = args.extract_plain_text().strip()
    prompt_text, orientation, denoise_value, denoise_error = _parse_img2img_options(raw_prompt)
    if denoise_error:
        await img2img_cmd.finish(_build_command_reply_message(event, denoise_error))
    if not prompt_text:
        prompt_text = DEFAULT_IMG2IMG_PROMPT

    if not _workflow_supports_input_image(img2img_workflow_path):
        await img2img_cmd.finish(
            _build_command_reply_message(
                event,
                (
                    "当前图生图工作流不可用。请提供包含 LoadImage 节点的 ComfyUI API JSON，"
                    "并在 .env 设置 `COMFYUI_IMG2IMG_WORKFLOW=你的文件名.json`。"
                ),
            )
        )

    image_url = await _extract_image_url_from_context(bot, event, args)
    if not image_url:
        await img2img_cmd.finish(
            _build_command_reply_message(
                event,
                "请在指令消息里带图，或回复某张图再发 `/图生图 提示词`。",
            )
        )

    await _submit_draw_task(
        event=event,
        prompt_text=prompt_text,
        orientation=orientation,
        image_url=image_url,
        workflow_path=img2img_workflow_path,
        img2img_denoise=denoise_value,
        matcher=img2img_cmd,
    )


@upscale_removed_cmd.handle()
async def handle_upscale_removed(event: MessageEvent):
    await upscale_removed_cmd.finish(_build_command_reply_message(event, "超分功能已下线。"))


def _startup_workflow_diagnostics() -> None:
    if not text2img_workflow_path.exists():
        logger.warning(f"文生图工作流缺失：{text2img_workflow_path}（请在 .env 设置 COMFYUI_WORKFLOW）")
    else:
        style_options = _get_text2img_style_options(force_reload=True)
        if style_options.models:
            model_preview = " | ".join(style_options.models[:5])
            logger.info(
                f"文生图可选模型共 {len(style_options.models)} 个，示例：{model_preview}"
            )
        else:
            logger.warning("文生图工作流未识别到模型节点（CheckpointLoaderSimple）。")
        if style_options.loras:
            lora_preview = " | ".join(style_options.loras[:5])
            logger.info(
                f"文生图可选LoRA共 {len(style_options.loras)} 个，示例：{lora_preview}"
            )
        else:
            logger.info("文生图工作流未识别到可切换LoRA节点。")

    if not img2img_workflow_path.exists():
        logger.warning(
            f"图生图工作流缺失：{img2img_workflow_path}（请在 .env 设置 COMFYUI_IMG2IMG_WORKFLOW）"
        )
    elif not _workflow_supports_input_image(img2img_workflow_path):
        logger.warning(
            f"图生图工作流不含 LoadImage 节点：{img2img_workflow_path}（请更换为可接收输入图的 API JSON）"
        )
    else:
        logger.info(
            "图生图输出分辨率已设为默认竖图 "
            f"{DEFAULT_IMG2IMG_PORTRAIT_SIZE[0]}x{DEFAULT_IMG2IMG_PORTRAIT_SIZE[1]} / "
            f"横图 {DEFAULT_IMG2IMG_LANDSCAPE_SIZE[0]}x{DEFAULT_IMG2IMG_LANDSCAPE_SIZE[1]}"
        )


try:
    _startup_workflow_diagnostics()
except Exception as e:
    logger.warning(f"Startup workflow diagnostics failed: {e}")
