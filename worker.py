import json
import os
import signal
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import re
import random
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE = Path(os.getenv("LOFI_BASE", "/home/lofi/app"))
load_dotenv(BASE / ".env")
import sys
sys.path.insert(0, str(BASE / "web"))
from db import connect, init_db, setting

STATUS_FILE = BASE / "data" / "worker-status.json"
MUSIC_DIR = BASE / "music"
VIDEO_DIR = BASE / "video"
OVERLAY_DIR = BASE / "overlays"
TEXT_OVERLAY_DIR = BASE / "data" / "overlay-text"
process = None
active_signature = None
started_at = None
weather_cache = {}
last_progress_at = None
last_output_time_ms = 0
WATCHDOG_GRACE_SECONDS = 45
WATCHDOG_STALL_SECONDS = 30


def write_status(state, message="", **extra):
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "state": state,
        "message": message,
        "updated_at": int(time.time()),
        "started_at": started_at,
        **extra,
    }
    temporary = STATUS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(STATUS_FILE)


def selected_video():
    with connect() as db:
        rows = db.execute(
            """SELECT schedules.*, videos.source_type, videos.source
               FROM schedules JOIN videos ON videos.id = schedules.video_id
               WHERE schedules.enabled = 1 AND videos.enabled = 1"""
        ).fetchall()
        for row in rows:
            now = datetime.now(ZoneInfo(row["timezone"])).strftime("%H:%M")
            start, end = row["start_time"], row["end_time"]
            matches = start <= now < end if start < end else now >= start or now < end
            if matches:
                return dict(row)
        fallback = db.execute(
            "SELECT source_type, source, name FROM videos WHERE enabled = 1 ORDER BY id LIMIT 1"
        ).fetchone()
    return dict(fallback) if fallback else None


def build_playlist():
    with connect() as db:
        tracks = db.execute(
            "SELECT filename FROM tracks WHERE enabled = 1 ORDER BY position, id"
        ).fetchall()
    tracks = list(tracks)
    if setting("playlist_shuffle", "0") == "1":
        random.SystemRandom().shuffle(tracks)
    playlist = tempfile.NamedTemporaryFile(
        mode="w", prefix="lofi-", suffix=".ffconcat", delete=False, encoding="utf-8"
    )
    playlist.write("ffconcat version 1.0\n")
    count = 0
    for row in tracks:
        path = (MUSIC_DIR / row["filename"]).resolve()
        if path.is_file() and MUSIC_DIR.resolve() in path.parents:
            escaped = str(path).replace("'", "'\\''")
            playlist.write(f"file '{escaped}'\n")
            count += 1
    playlist.close()
    if not count:
        Path(playlist.name).unlink(missing_ok=True)
        raise RuntimeError("У плейлисті немає доступних треків")
    return playlist.name


def active_overlays():
    with connect() as db:
        text = [dict(row) for row in db.execute(
            "SELECT * FROM text_overlays WHERE enabled = 1 ORDER BY id"
        )]
        media = [dict(row) for row in db.execute(
            "SELECT * FROM media_overlays WHERE enabled = 1 ORDER BY id"
        )]
    return text, media


def weather_text(location):
    cached = weather_cache.get(location)
    if cached and time.time() - cached["checked_at"] < 600:
        return cached["text"]
    text = cached["text"] if cached else f"{location}: погода недоступна"
    try:
        geocode_url = "https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode({
            "name": location, "count": 1, "language": "uk", "format": "json",
        })
        with urllib.request.urlopen(geocode_url, timeout=8) as response:
            place = json.load(response)["results"][0]
        forecast_url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
            "latitude": place["latitude"], "longitude": place["longitude"],
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
            "timezone": "auto",
        })
        with urllib.request.urlopen(forecast_url, timeout=8) as response:
            current = json.load(response)["current"]
        descriptions = {
            0: "ясно", 1: "переважно ясно", 2: "мінлива хмарність", 3: "хмарно",
            45: "туман", 48: "паморозь", 51: "мряка", 53: "мряка",
            55: "сильна мряка", 61: "дощ", 63: "дощ", 65: "сильний дощ",
            71: "сніг", 73: "сніг", 75: "сильний сніг", 80: "злива",
            81: "злива", 82: "сильна злива", 95: "гроза", 96: "гроза",
            99: "сильна гроза",
        }
        name = place.get("name", location)
        description = descriptions.get(current["weather_code"], "мінлива погода")
        text = (
            f"{name}: {round(current['temperature_2m'])}°C, {description}, "
            f"вітер {round(current['wind_speed_10m'])} км/год"
        )
    except (OSError, KeyError, IndexError, ValueError):
        pass
    weather_cache[location] = {"checked_at": time.time(), "text": text}
    return text


def refresh_text_overlays(overlays):
    TEXT_OVERLAY_DIR.mkdir(parents=True, exist_ok=True)
    for overlay in overlays:
        kind = overlay["kind"]
        if kind == "datetime":
            try:
                now = datetime.now(ZoneInfo(overlay["timezone"]))
            except Exception:
                now = datetime.now()
            text = now.strftime(overlay["content"] or "%d.%m.%Y  %H:%M")
        elif kind == "weather":
            text = weather_text(overlay["content"])
        else:
            text = overlay["content"]
        target = TEXT_OVERLAY_DIR / f"{overlay['id']}.txt"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(text.replace("\r", ""), encoding="utf-8")
        temporary.replace(target)


def position_expression(position, offset_x, offset_y, media=False):
    width = "overlay_w" if media else "text_w"
    height = "overlay_h" if media else "text_h"
    x = {
        "top-left": str(offset_x), "bottom-left": str(offset_x),
        "top-center": f"(main_w-{width})/2+{offset_x}",
        "bottom-center": f"(main_w-{width})/2+{offset_x}",
        "center": f"(main_w-{width})/2+{offset_x}",
        "top-right": f"main_w-{width}-{offset_x}",
        "bottom-right": f"main_w-{width}-{offset_x}",
    }.get(position, str(offset_x))
    y = {
        "top-left": str(offset_y), "top-center": str(offset_y),
        "top-right": str(offset_y),
        "center": f"(main_h-{height})/2+{offset_y}",
        "bottom-left": f"main_h-{height}-{offset_y}",
        "bottom-center": f"main_h-{height}-{offset_y}",
        "bottom-right": f"main_h-{height}-{offset_y}",
    }.get(position, str(offset_y))
    return x, y


def video_filters(text_overlays, media_overlays):
    # Rebuild timestamps from frame numbers so looping files cannot send
    # backward/discontinuous PTS values to the RTMP server.
    filters = ["[0:v]fps=30,settb=AVTB,setpts=N/(30*TB)[v0]"]
    current = "v0"
    for index, overlay in enumerate(media_overlays, start=2):
        scaled = f"media{index}"
        output = f"v{index - 1}"
        x, y = position_expression(
            overlay["position"], overlay["offset_x"], overlay["offset_y"], media=True
        )
        filters.append(
            f"[{index}:v]fps=30,scale={overlay['width']}:-1,"
            f"settb=AVTB,setpts=N/(30*TB)[{scaled}]"
        )
        filters.append(
            f"[{current}][{scaled}]overlay=x='{x}':y='{y}':eof_action=repeat[{output}]"
        )
        current = output
    for sequence, overlay in enumerate(text_overlays):
        output = f"text{sequence}"
        x, y = position_expression(
            overlay["position"], overlay["offset_x"], overlay["offset_y"]
        )
        color = overlay["font_color"] if re.fullmatch(r"[A-Za-z]+|#[0-9A-Fa-f]{6}", overlay["font_color"]) else "white"
        textfile = TEXT_OVERLAY_DIR / f"{overlay['id']}.txt"
        filters.append(
            f"[{current}]drawtext=textfile='{textfile}':reload=1:"
            f"fontcolor={color}:fontsize={overlay['font_size']}:"
            f"box=1:boxcolor=black@0.45:boxborderw=12:x='{x}':y='{y}'[{output}]"
        )
        current = output
    return ";".join(filters), current


def command(video, playlist, text_overlays, media_overlays):
    source = video["source"] if video["source_type"] == "remote" else str(VIDEO_DIR / video["source"])
    if source.startswith(("rtsp://", "rtsps://")):
        video_input = ["-rtsp_transport", "tcp", "-i", source]
    elif video["source_type"] == "remote":
        video_input = ["-i", source]
    else:
        video_input = ["-stream_loop", "-1", "-i", source]
    media_inputs = []
    valid_media = []
    for overlay in media_overlays:
        path = (OVERLAY_DIR / overlay["filename"]).resolve()
        if path.is_file() and OVERLAY_DIR.resolve() in path.parents:
            media_inputs.extend(["-stream_loop", "-1", "-i", str(path)])
            valid_media.append(overlay)
    filters, video_output = video_filters(text_overlays, valid_media)
    bitrate = setting("video_bitrate", "3000k")
    try:
        buffer_size = f"{int(bitrate.removesuffix('k')) * 2}k"
    except ValueError:
        buffer_size = "6000k"
    destination = setting("rtmp_url", "")
    if destination.startswith("rtmp://a.rtmp.youtube.com/live2/"):
        destination = destination.replace(
            "rtmp://a.rtmp.youtube.com/live2/",
            "rtmps://a.rtmps.youtube.com/live2/",
            1,
        )
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning",
        "-progress", "pipe:1", "-stats_period", "2", "-re",
        *video_input,
        "-re", "-stream_loop", "-1", "-f", "concat", "-safe", "0", "-i", playlist,
        *media_inputs,
        "-filter_complex", filters,
        "-map", f"[{video_output}]", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "veryfast",
        "-b:v", bitrate, "-maxrate", bitrate, "-bufsize", buffer_size,
        "-pix_fmt", "yuv420p", "-r", "30",
        "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
        "-af", (
            "loudnorm=I=-16:TP=-1.5:LRA=11,"
            "aresample=48000:async=1000:first_pts=0,asetpts=N/SR/TB"
        ),
        "-flvflags", "no_duration_filesize", "-f", "flv", destination,
    ]


def monitor_progress(stream):
    global last_progress_at, last_output_time_ms
    try:
        for raw_line in stream:
            key, separator, value = raw_line.strip().partition("=")
            if not separator:
                continue
            if key in {"out_time_ms", "out_time_us"}:
                try:
                    output_time = int(value)
                except ValueError:
                    continue
                if output_time > last_output_time_ms:
                    last_output_time_ms = output_time
                    last_progress_at = time.monotonic()
            elif key == "progress" and value == "continue":
                last_progress_at = time.monotonic()
    finally:
        stream.close()


def start_stream(command_args):
    global process, started_at, last_progress_at, last_output_time_ms
    last_progress_at = time.monotonic()
    last_output_time_ms = 0
    process = subprocess.Popen(
        command_args,
        stdout=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    threading.Thread(
        target=monitor_progress,
        args=(process.stdout,),
        daemon=True,
        name="ffmpeg-progress",
    ).start()
    started_at = int(time.time())


def stream_is_stalled():
    if not process or process.poll() is not None or last_progress_at is None:
        return False
    if not started_at or time.time() - started_at < WATCHDOG_GRACE_SECONDS:
        return False
    return time.monotonic() - last_progress_at > WATCHDOG_STALL_SECONDS


def stop_stream():
    global process, started_at, last_progress_at, last_output_time_ms
    if process and process.poll() is None:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
    process = None
    started_at = None
    last_progress_at = None
    last_output_time_ms = 0


def run():
    global process, active_signature, started_at
    init_db()
    playlist = None
    while True:
        try:
            desired = setting("desired_state", "stopped")
            video = selected_video()
            text_overlays, media_overlays = active_overlays()
            refresh_text_overlays(text_overlays)
            signature = (
                setting("config_nonce"), setting("restart_nonce"),
                video["source"] if video else None,
            )
            if desired != "running":
                stop_stream()
                write_status("stopped")
            elif not setting("rtmp_url"):
                stop_stream()
                write_status("error", "RTMP URL не налаштовано")
            elif video is None:
                stop_stream()
                write_status("error", "Не додано жодного відеоджерела")
            else:
                needs_start = process is None or process.poll() is not None
                if active_signature != signature:
                    stop_stream()
                    needs_start = True
                elif stream_is_stalled():
                    print(
                        "FFmpeg progress stalled; reconnecting RTMP session",
                        flush=True,
                    )
                    write_status(
                        "reconnecting",
                        "FFmpeg не передає нові кадри; RTMP-сесію буде оновлено",
                        video=video.get("name", video["source"]),
                        pid=process.pid,
                    )
                    stop_stream()
                    needs_start = True
                if needs_start:
                    if playlist:
                        Path(playlist).unlink(missing_ok=True)
                    playlist = build_playlist()
                    start_stream(
                        command(video, playlist, text_overlays, media_overlays)
                    )
                    active_signature = signature
                write_status(
                    "running" if process.poll() is None else "error",
                    video=video.get("name", video["source"]),
                    pid=process.pid,
                )
        except Exception as exc:
            stop_stream()
            write_status("error", str(exc))
        time.sleep(5)


if __name__ == "__main__":
    try:
        run()
    finally:
        stop_stream()
