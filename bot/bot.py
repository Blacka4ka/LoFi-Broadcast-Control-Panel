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

from db import connect, init_db, setting, update_settings
from runtime import live_stream_info


def telegram_call(token, method, payload=None, timeout=35):
    data = urllib.parse.urlencode(payload or {}).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}", data=data
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def send_message(token, chat_id, text):
    status = live_stream_info()
    running = status.get("state") in {"running", "reconnecting"}
    can_disable = running and current_track_enabled(status.get("track", ""))
    keyboard = {
        "keyboard": [
            ["Статус", "Перезапустити"] if running else ["Статус", "Запустити"],
            ["Вимкнути цей трек"] if can_disable else [],
            ["Зупинити"] if running else [],
        ],
        "resize_keyboard": True,
    }
    keyboard["keyboard"] = [row for row in keyboard["keyboard"] if row]
    telegram_call(token, "sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": json.dumps(keyboard, ensure_ascii=False),
    })


def status_text():
    status = live_stream_info()
    labels = {
        "running": "Стрім працює",
        "stopped": "Стрім зупинено",
        "error": "Помилка стріму",
        "offline": "Worker офлайн",
    }
    text = labels.get(status.get("state"), status.get("state", "Невідомий стан"))
    if status.get("video"):
        text += f"\nВідео: {status['video']}"
    if status.get("video_file") and status["video_file"] != status.get("video"):
        text += f"\nФайл: {status['video_file']}"
    if status.get("track"):
        text += f"\nЗараз грає: {status['track']}"
    if status.get("ffmpeg_uptime"):
        text += f"\nЕфір: {format_duration(status['ffmpeg_uptime'])}"
    if status.get("ffmpeg_cpu") is not None:
        text += (
            f"\nFFmpeg: CPU {status['ffmpeg_cpu']}% · "
            f"RAM {status.get('ffmpeg_memory', 0)}%"
        )
    if status.get("message"):
        text += f"\n{status['message']}"
    return text


def format_duration(seconds):
    hours, remainder = divmod(int(seconds), 3600)
    minutes = remainder // 60
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"{days} д {hours} год {minutes} хв"
    return f"{hours} год {minutes} хв" if hours else f"{minutes} хв"


def disable_current_track():
    filename = live_stream_info().get("track", "")
    if not filename:
        return "Не вдалося визначити поточний трек"
    with connect() as db:
        cursor = db.execute(
            "UPDATE tracks SET enabled = 0 WHERE filename = ? AND enabled = 1",
            (filename,),
        )
        db.commit()
    if not cursor.rowcount:
        return "Цей трек уже вимкнений або не знайдений"
    return (
        f"Вимкнено: {filename}\n"
        "Ефір не перезапускався. Зміна діятиме після наступного "
        "планового перепідключення."
    )


def current_track_enabled(filename):
    if not filename:
        return False
    with connect() as db:
        row = db.execute(
            "SELECT enabled FROM tracks WHERE filename = ?",
            (filename,),
        ).fetchone()
    return bool(row["enabled"]) if row else False


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
    elif text == "Вимкнути цей трек":
        send_message(token, chat_id, disable_current_track())


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
