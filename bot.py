import os
import socket
from pathlib import Path

import nonebot
from nonebot.adapters.onebot.v11 import Adapter as ONEBOT_V11Adapter


def _read_env_value(key: str, default: str) -> str:
    env_path = Path(".env")
    if not env_path.exists():
        return os.getenv(key, default)

    value = os.getenv(key)
    if value is not None:
        return value

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, raw = line.split("=", 1)
        if name.strip().upper() != key.upper():
            continue
        raw = raw.split("#", 1)[0].strip()
        if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
            raw = raw[1:-1]
        return raw
    return default


def _is_port_available(host: str, port: int) -> bool:
    test_host = "0.0.0.0" if host in {"0.0.0.0", "::"} else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((test_host, port))
            return True
        except OSError:
            return False


def _find_available_port(host: str, start_port: int, tries: int = 20) -> int:
    for port in range(start_port, start_port + tries):
        if _is_port_available(host, port):
            return port
    return start_port


host = _read_env_value("HOST", "0.0.0.0")
port_text = _read_env_value("PORT", "8080")
try:
    requested_port = int(port_text)
except ValueError:
    requested_port = 8080

selected_port = _find_available_port(host, requested_port)
if selected_port != requested_port:
    print(
        f"[WARN] 端口 {requested_port} 被占用，已自动切换到 {selected_port}。"
        " 请把 NapCat/Lagrange 的反向 WS 端口改为新端口。"
    )
    os.environ["PORT"] = str(selected_port)

# 初始化 nonebot（此时 PORT 已处理完）
nonebot.init()

# 注册网络和设配器
app = nonebot.get_asgi()
driver = nonebot.get_driver()
driver.register_adapter(ONEBOT_V11Adapter)

# 加载插件
nonebot.load_plugins("src/plugins")

if __name__ == "__main__":
    nonebot.run()
