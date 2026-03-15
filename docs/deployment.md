# Deployment Guide

This document covers practical deployment paths for the bot.

## 1. Local Windows Deployment

Recommended when:

- You run NapCat or Lagrange locally
- You run ComfyUI on the same machine
- You want the easiest setup path

Steps:

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
Copy-Item .env.example .env
python bot.py
```

Reverse WebSocket:

```text
ws://127.0.0.1:8080/onebot/v11/ws
```

## 2. Linux Server Deployment

Recommended when:

- You want the bot online all day
- NapCat/Lagrange and the bot live on a VPS or dedicated server
- ComfyUI is local to the server or exposed from another machine

Example setup:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python bot.py
```

## 3. Keep It Running

### screen

```bash
screen -S qq-group-bot
source .venv/bin/activate
python bot.py
```

Detach with:

```bash
Ctrl+A, then D
```

### tmux

```bash
tmux new -s qq-group-bot
source .venv/bin/activate
python bot.py
```

Detach with:

```bash
Ctrl+B, then D
```

## 4. Systemd Example

`/etc/systemd/system/qq-group-bot.service`

```ini
[Unit]
Description=QQ Group Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/qq-group-bot
ExecStart=/opt/qq-group-bot/.venv/bin/python /opt/qq-group-bot/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable qq-group-bot
sudo systemctl start qq-group-bot
sudo systemctl status qq-group-bot
```

## 5. Deployment Notes

- Do not commit `.env`
- Keep `data/` private
- Make sure your OneBot reverse WS points to the same host and port as the bot
- If ComfyUI runs remotely, update `COMFYUI_SERVER`
- If port `8080` is busy, the bot can auto-switch, but your OneBot client must follow the new port

## 6. What Is Not Included

This repository does not ship:

- QQ account credentials
- Model files
- LoRA files
- Production Docker images

If you later want containerized deployment, add it after your runtime path is stable.

