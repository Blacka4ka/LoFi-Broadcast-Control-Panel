import json
import os
import secrets
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(os.getenv("LOFI_BASE", "/home/lofi/app"))
load_dotenv(BASE / ".env")
sys.path.insert(0, str(BASE / "web"))

from db import init_db, setting, update_settings

STATUS_FILE = BASE / "data" / "worker-status.json"


def telegram_call(token, method, payload=None, timeout=35):
    data = urllib.parse.urlencode(payload or {}).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}", data=data
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def send_message(token, chat_id, text):
    keyboard = {
        "keyboard": [
            ["Статус", "Перезапустити"],
            ["Запустити", "Зупинити"],
        ],
        "resize_keyboard": True,
    }
    telegram_call(token, "sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": json.dumps(keyboard, ensure_ascii=False),
    })


def status_text():
    try:
        status = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "Worker не відповідає"
    labels = {
        "running": "Стрім працює",
        "stopped": "Стрім зупинено",
        "error": "Помилка стріму",
        "offline": "Worker офлайн",
    }
    text = labels.get(status.get("state"), status.get("state", "Невідомий стан"))
    if status.get("video"):
        text += f"\nВідео: {status['video']}"
    if status.get("message"):
        text += f"\n{status['message']}"
    return text


def handle_message(token, message, allowed_user):
    sender = message.get("from", {}).get("id")
    if str(sender) != str(allowed_user):
        return
    chat_id = message["chat"]["id"]
    text = message.get("text", "")
    if text in {"/start", "Статус"}:
        send_message(token, chat_id, status_text())
    elif text == "Запустити":
        update_settings({
            "desired_state": "running",
            "restart_nonce": secrets.token_hex(8),
        })
        send_message(token, chat_id, "Команду запуску прийнято")
    elif text == "Зупинити":
        update_settings({
            "desired_state": "stopped",
            "restart_nonce": secrets.token_hex(8),
        })
        send_message(token, chat_id, "Команду зупинки прийнято")
    elif text == "Перезапустити":
        update_settings({
            "desired_state": "running",
            "restart_nonce": secrets.token_hex(8),
        })
        send_message(token, chat_id, "Команду перезапуску прийнято")


def run():
    init_db()
    offset = 0
    active_token = None
    while True:
        token = setting("telegram_token", "")
        allowed_user = setting("telegram_user_id", "")
        enabled = setting("telegram_enabled", "0") == "1"
        if not enabled or not token or not allowed_user:
            active_token = None
            offset = 0
            time.sleep(5)
            continue
        if token != active_token:
            active_token = token
            offset = 0
        try:
            result = telegram_call(token, "getUpdates", {
                "offset": offset,
                "timeout": 25,
                "allowed_updates": json.dumps(["message"]),
            })
            for update in result.get("result", []):
                offset = update["update_id"] + 1
                if "message" in update:
                    handle_message(token, update["message"], allowed_user)
        except (OSError, ValueError, KeyError):
            time.sleep(5)


if __name__ == "__main__":
    run()
