import json
import os
import time
import urllib.parse
from pathlib import Path

import psutil


BASE = Path(os.getenv("LOFI_BASE", "/home/lofi/app"))
STATUS_FILE = BASE / "data" / "worker-status.json"
MUSIC_DIR = (BASE / "music").resolve()
VIDEO_DIR = (BASE / "video").resolve()


def live_stream_info():
    status = read_worker_status()
    pid = status.get("pid")
    if status.get("state") != "running" or not isinstance(pid, int):
        return status

    process = process_info(pid)
    if not process:
        return status

    video_file = first_input(process["arguments"])
    track_file = active_track(pid)
    return {
        **status,
        "video_file": display_source(video_file),
        "track": track_file.name if track_file else "",
        "ffmpeg_cpu": process["cpu"],
        "ffmpeg_memory": process["memory"],
        "ffmpeg_uptime": process["uptime"],
    }


def read_worker_status():
    try:
        return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "offline", "message": "Worker не відповідає"}


def process_info(pid):
    try:
        process = psutil.Process(pid)
        if process.name().lower() != "ffmpeg":
            return None
        elapsed = max(1.0, time.time() - process.create_time())
        cpu_times = process.cpu_times()
        cpu = round((cpu_times.user + cpu_times.system) / elapsed * 100, 1)
        return {
            "arguments": process.cmdline(),
            "cpu": cpu,
            "memory": round(process.memory_percent(), 1),
            "uptime": int(elapsed),
        }
    except (psutil.Error, OSError):
        return None


def first_input(arguments):
    for index, argument in enumerate(arguments[:-1]):
        if argument == "-i":
            return arguments[index + 1]
    return ""


def active_track(pid):
    fd_directory = Path(f"/proc/{pid}/fd")
    try:
        links = list(fd_directory.iterdir())
    except OSError:
        return None
    candidates = []
    for link in links:
        try:
            target = link.resolve(strict=True)
        except OSError:
            continue
        if target.is_file() and MUSIC_DIR in target.parents:
            candidates.append(target)
    if not candidates:
        return None
    try:
        return max(candidates, key=lambda path: path.stat().st_atime_ns)
    except OSError:
        return candidates[0]


def display_source(source):
    if not source:
        return ""
    if source.startswith(("rtsp://", "rtsps://", "http://", "https://")):
        parsed = urllib.parse.urlsplit(source)
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return urllib.parse.urlunsplit(
            (parsed.scheme, host, parsed.path, "", "")
        )
    try:
        path = Path(source).resolve()
        if VIDEO_DIR in path.parents:
            return path.name
    except OSError:
        pass
    return Path(source).name
