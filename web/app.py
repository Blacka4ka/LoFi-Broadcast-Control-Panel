import os
import re
import secrets
import smtplib
import sqlite3
import time
import urllib.parse
from email.message import EmailMessage
from functools import wraps
from pathlib import Path

import psutil
from dotenv import load_dotenv
from flask import (
    Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from db import connect, init_db, setting, update_settings
from runtime import live_stream_info

BASE = Path(os.getenv("LOFI_BASE", "/home/lofi/app"))
load_dotenv(BASE / ".env")

MUSIC_DIR = BASE / "music"
VIDEO_DIR = BASE / "video"
OVERLAY_DIR = BASE / "overlays"
STATUS_FILE = BASE / "data" / "worker-status.json"
ALLOWED_MUSIC = {".mp3", ".m4a", ".wav", ".flac", ".ogg"}
ALLOWED_VIDEO = {".mp4", ".mkv", ".mov", ".webm"}
ALLOWED_OVERLAY = {".mp4", ".mov", ".webm", ".gif"}

app = Flask(__name__)
app.secret_key = os.environ["FLASK_SECRET"]
app.config.update(
    MAX_CONTENT_LENGTH=int(os.getenv("MAX_UPLOAD_MB", "2048")) * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "1") == "1",
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=12 * 60 * 60,
)

for directory in (MUSIC_DIR, VIDEO_DIR, OVERLAY_DIR, BASE / "data"):
    directory.mkdir(parents=True, exist_ok=True)
init_db()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def verify_csrf():
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
        if not secrets.compare_digest(supplied or "", session.get("csrf", "")):
            abort(400, "Invalid CSRF token")


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; connect-src 'self'"
    )
    return response


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        with connect() as db:
            user = db.execute(
                "SELECT * FROM users WHERE email = ?", (request.form["email"].lower(),)
            ).fetchone()
        if user and check_password_hash(user["password_hash"], request.form["password"]):
            session.clear()
            session["user_id"] = user["id"]
            session["csrf"] = secrets.token_urlsafe(32)
            session.permanent = True
            return redirect(url_for("dashboard"))
        flash("Невірна пошта або пароль")
    return render_template("login.html")


@app.post("/logout")
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        with connect() as db:
            user = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
            if user:
                token = secrets.token_urlsafe(32)
                db.execute(
                    "INSERT INTO password_resets(user_id, token_hash, expires_at) VALUES(?,?,?)",
                    (user["id"], generate_password_hash(token), int(time.time()) + 3600),
                )
                db.commit()
                send_reset_email(email, token)
        flash("Якщо акаунт існує, лист для відновлення вже надіслано")
    return render_template("forgot.html")


def send_reset_email(recipient, token):
    if not os.getenv("SMTP_HOST"):
        app.logger.warning("SMTP is not configured; password reset email was not sent")
        return
    message = EmailMessage()
    message["Subject"] = "Відновлення доступу до LoFi Studio"
    message["From"] = os.environ["SMTP_FROM"]
    message["To"] = recipient
    message.set_content(f"{os.environ['PUBLIC_URL']}/reset-password/{token}")
    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.getenv("SMTP_PORT", "587"))) as smtp:
        smtp.starttls()
        smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        smtp.send_message(message)


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM password_resets WHERE used_at IS NULL AND expires_at > ?",
            (int(time.time()),),
        ).fetchall()
        reset = next((row for row in rows if check_password_hash(row["token_hash"], token)), None)
        if not reset:
            abort(404)
        if request.method == "POST":
            password = request.form["password"]
            if len(password) < 12:
                flash("Пароль повинен містити щонайменше 12 символів")
            else:
                db.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ?",
                    (generate_password_hash(password), reset["user_id"]),
                )
                db.execute(
                    "UPDATE password_resets SET used_at = ? WHERE id = ?",
                    (int(time.time()), reset["id"]),
                )
                db.commit()
                flash("Пароль оновлено")
                return redirect(url_for("login"))
    return render_template("reset.html", token=token)


@app.get("/")
@login_required
def dashboard():
    return render_template("dashboard.html")


@app.get("/api/state")
@login_required
def state():
    live = {
        "stream": worker_status(),
        "server": server_status(),
    }
    if request.args.get("live") == "1":
        return jsonify(live)
    with connect() as db:
        tracks = [dict(row) for row in db.execute(
            "SELECT id, filename, enabled, position FROM tracks ORDER BY position, filename"
        )]
        videos = [dict(row) for row in db.execute(
            "SELECT id, name, source_type, source, enabled FROM videos ORDER BY name"
        )]
        schedules = [dict(row) for row in db.execute(
            """SELECT schedules.id, schedules.name, start_time, end_time, timezone,
                      video_id, videos.name AS video_name
               FROM schedules JOIN videos ON videos.id = schedules.video_id
               WHERE schedules.enabled = 1 ORDER BY start_time"""
        )]
        text_overlays = [dict(row) for row in db.execute(
            "SELECT * FROM text_overlays ORDER BY id"
        )]
        media_overlays = [dict(row) for row in db.execute(
            "SELECT * FROM media_overlays ORDER BY id"
        )]
    return jsonify({
        **live,
        "settings": {
            "desired_state": setting("desired_state", "stopped"),
            "rtmp_configured": bool(setting("rtmp_url", "")),
            "video_bitrate": setting("video_bitrate", "3000k"),
            "playlist_shuffle": setting("playlist_shuffle", "0") == "1",
            "telegram_enabled": setting("telegram_enabled", "0") == "1",
            "telegram_configured": bool(setting("telegram_token", "")),
            "telegram_user_configured": bool(setting("telegram_user_id", "")),
        },
        "tracks": tracks,
        "videos": videos,
        "schedules": schedules,
        "text_overlays": text_overlays,
        "media_overlays": media_overlays,
    })


def worker_status():
    status = live_stream_info()
    if status.get("track"):
        with connect() as db:
            row = db.execute(
                "SELECT enabled FROM tracks WHERE filename = ?",
                (status["track"],),
            ).fetchone()
        status["track_enabled"] = bool(row["enabled"]) if row else None
    return status


def server_status():
    disk = psutil.disk_usage(str(BASE))
    return {
        "cpu": psutil.cpu_percent(),
        "memory": psutil.virtual_memory().percent,
        "disk": disk.percent,
        "load": list(os.getloadavg()) if hasattr(os, "getloadavg") else [],
        "uptime": int(time.time() - psutil.boot_time()),
    }


@app.post("/api/stream/<action>")
@login_required
def stream_action(action):
    if action not in {"start", "stop", "restart"}:
        abort(404)
    desired = "running" if action in {"start", "restart"} else "stopped"
    update_settings({"desired_state": desired, "restart_nonce": secrets.token_hex(8)})
    return jsonify(ok=True)


@app.post("/api/settings")
@login_required
def save_settings():
    payload = request.get_json()
    raw_rtmp = payload.get("rtmp_url", "").strip()
    bitrate = payload.get("video_bitrate", "3000k")
    if bitrate not in {"2500k", "3000k", "4500k", "6000k"}:
        return jsonify(error="Непідтримуваний бітрейт"), 400
    try:
        rtmp_url = normalize_rtmp_url(raw_rtmp)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    values = {}
    if bitrate != setting("video_bitrate", "3000k"):
        values["video_bitrate"] = bitrate
    if rtmp_url and rtmp_url != setting("rtmp_url", ""):
        values["rtmp_url"] = rtmp_url
    if values:
        values["config_nonce"] = secrets.token_hex(8)
        update_settings(values)
    return jsonify(ok=True, updated=bool(values), key_updated="rtmp_url" in values)


def normalize_rtmp_url(value):
    if not value:
        return ""
    if "://" not in value:
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,200}", value):
            raise ValueError("Ключ стріму містить недопустимі символи")
        return f"rtmps://a.rtmps.youtube.com/live2/{value}"
    if value.startswith("rtmp://a.rtmp.youtube.com/live2/"):
        value = value.replace(
            "rtmp://a.rtmp.youtube.com/live2/",
            "rtmps://a.rtmps.youtube.com/live2/",
            1,
        )
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"rtmp", "rtmps"} or not parsed.hostname:
        raise ValueError("Вкажіть ключ YouTube або повну RTMP / RTMPS адресу")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("RTMP-адреса має неправильний формат")
    if parsed.hostname in {"a.rtmp.youtube.com", "a.rtmps.youtube.com"}:
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 2 or parts[0] != "live2" or not parts[1]:
            raise ValueError("У YouTube RTMP-адресі відсутній ключ стріму")
    return value


@app.post("/api/upload/<kind>")
@login_required
def upload(kind):
    if kind not in {"music", "video", "overlay"}:
        abort(404)
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return jsonify(error="Файл не вибрано"), 400
    extension = Path(uploaded.filename).suffix.lower()
    allowed = {
        "music": ALLOWED_MUSIC, "video": ALLOWED_VIDEO, "overlay": ALLOWED_OVERLAY
    }[kind]
    if extension not in allowed:
        return jsonify(error="Формат файлу не підтримується"), 400
    directory = {"music": MUSIC_DIR, "video": VIDEO_DIR, "overlay": OVERLAY_DIR}[kind]
    filename = unique_filename(directory, secure_filename(uploaded.filename))
    uploaded.save(directory / filename)
    with connect() as db:
        if kind == "music":
            position = db.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM tracks").fetchone()[0]
            db.execute("INSERT INTO tracks(filename, position) VALUES(?, ?)", (filename, position))
        elif kind == "video":
            db.execute(
                "INSERT INTO videos(name, source_type, source) VALUES(?, 'file', ?)",
                (Path(filename).stem, filename),
            )
        else:
            db.execute(
                "INSERT INTO media_overlays(name, filename) VALUES(?, ?)",
                (Path(filename).stem, filename),
            )
        db.commit()
    update_settings({"config_nonce": secrets.token_hex(8)})
    return jsonify(ok=True, filename=filename)


@app.post("/api/media/scan")
@login_required
def scan_media():
    added_tracks = 0
    added_videos = 0

    with connect() as db:
        known_tracks = {
            row["filename"] for row in db.execute("SELECT filename FROM tracks")
        }
        known_videos = {
            row["source"] for row in db.execute(
                "SELECT source FROM videos WHERE source_type = 'file'"
            )
        }
        position = db.execute(
            "SELECT COALESCE(MAX(position), 0) FROM tracks"
        ).fetchone()[0]

        for path in sorted(MUSIC_DIR.iterdir(), key=lambda item: item.name.lower()):
            if (
                path.is_file()
                and path.suffix.lower() in ALLOWED_MUSIC
                and path.name not in known_tracks
            ):
                position += 1
                db.execute(
                    "INSERT INTO tracks(filename, position) VALUES(?, ?)",
                    (path.name, position),
                )
                known_tracks.add(path.name)
                added_tracks += 1

        for path in sorted(VIDEO_DIR.iterdir(), key=lambda item: item.name.lower()):
            if (
                path.is_file()
                and path.suffix.lower() in ALLOWED_VIDEO
                and path.name not in known_videos
            ):
                db.execute(
                    "INSERT INTO videos(name, source_type, source) VALUES(?, 'file', ?)",
                    (path.stem, path.name),
                )
                known_videos.add(path.name)
                added_videos += 1

        db.commit()

    if added_tracks or added_videos:
        update_settings({"config_nonce": secrets.token_hex(8)})
    return jsonify(ok=True, tracks=added_tracks, videos=added_videos)


def unique_filename(directory, filename):
    stem, suffix = Path(filename).stem, Path(filename).suffix
    candidate = filename
    index = 2
    while (directory / candidate).exists():
        candidate = f"{stem}-{index}{suffix}"
        index += 1
    return candidate


@app.post("/api/videos/remote")
@login_required
def add_remote_video():
    payload = request.get_json()
    source = payload.get("source", "").strip()
    if not source.startswith(("rtsp://", "rtsps://", "http://", "https://")):
        return jsonify(error="Дозволені RTSP(S) або HTTP(S) адреси"), 400
    with connect() as db:
        db.execute(
            "INSERT INTO videos(name, source_type, source) VALUES(?, 'remote', ?)",
            (payload.get("name", "Remote stream").strip(), source),
        )
        db.commit()
    update_settings({"config_nonce": secrets.token_hex(8)})
    return jsonify(ok=True)


@app.post("/api/tracks")
@login_required
def update_tracks():
    payload = request.get_json()
    with connect() as db:
        for position, item in enumerate(payload.get("tracks", []), start=1):
            db.execute(
                "UPDATE tracks SET enabled = ?, position = ? WHERE id = ?",
                (bool(item.get("enabled")), position, int(item["id"])),
            )
        db.commit()
    update_settings({"config_nonce": secrets.token_hex(8)})
    return jsonify(ok=True)


@app.post("/api/tracks/current/disable")
@login_required
def disable_current_track():
    filename = live_stream_info().get("track", "")
    if not filename:
        return jsonify(error="Не вдалося визначити поточний трек"), 409
    with connect() as db:
        cursor = db.execute(
            "UPDATE tracks SET enabled = 0 WHERE filename = ? AND enabled = 1",
            (filename,),
        )
        db.commit()
    if not cursor.rowcount:
        return jsonify(error="Цей трек уже вимкнений або не знайдений"), 409
    return jsonify(
        ok=True,
        filename=filename,
        message="Трек вимкнено. Поточний ефір не перезапускався.",
    )


@app.post("/api/tracks/<int:track_id>/enable")
@login_required
def enable_track(track_id):
    with connect() as db:
        cursor = db.execute(
            "UPDATE tracks SET enabled = 1 WHERE id = ?",
            (track_id,),
        )
        db.commit()
    if not cursor.rowcount:
        return jsonify(error="Трек не знайдено"), 404
    return jsonify(ok=True)


@app.post("/api/playlist/settings")
@login_required
def update_playlist_settings():
    payload = request.get_json()
    update_settings({
        "playlist_shuffle": "1" if payload.get("shuffle") else "0",
        "config_nonce": secrets.token_hex(8),
    })
    return jsonify(ok=True)


@app.post("/api/schedules")
@login_required
def add_schedule():
    payload = request.get_json()
    start, end = payload.get("start_time", ""), payload.get("end_time", "")
    if len(start) != 5 or len(end) != 5:
        return jsonify(error="Час має бути у форматі HH:MM"), 400
    with connect() as db:
        db.execute(
            """INSERT INTO schedules(name, video_id, start_time, end_time, timezone)
               VALUES(?,?,?,?,?)""",
            (
                payload.get("name", "Розклад").strip(),
                int(payload["video_id"]),
                start,
                end,
                payload.get("timezone", "Europe/Kyiv"),
            ),
        )
        db.commit()
    update_settings({"config_nonce": secrets.token_hex(8)})
    return jsonify(ok=True)


@app.delete("/api/schedules/<int:schedule_id>")
@login_required
def delete_schedule(schedule_id):
    with connect() as db:
        db.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
        db.commit()
    update_settings({"config_nonce": secrets.token_hex(8)})
    return jsonify(ok=True)


@app.post("/api/text-overlays")
@login_required
def add_text_overlay():
    payload = request.get_json()
    if payload.get("kind") not in {"text", "datetime", "location", "weather"}:
        return jsonify(error="Невідомий тип текстового шару"), 400
    if payload.get("position") not in {
        "top-left", "top-center", "top-right", "center",
        "bottom-left", "bottom-center", "bottom-right",
    }:
        return jsonify(error="Невірна позиція"), 400
    with connect() as db:
        db.execute(
            """INSERT INTO text_overlays
               (name, kind, content, position, offset_x, offset_y, font_size,
                font_color, font_family, timezone)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                payload.get("name", "Текст").strip()[:80],
                payload["kind"],
                payload.get("content", "").strip()[:300],
                payload["position"],
                int(payload.get("offset_x", 30)),
                int(payload.get("offset_y", 30)),
                max(12, min(120, int(payload.get("font_size", 36)))),
                payload.get("font_color", "white"),
                payload.get("font_family", "sans")
                if payload.get("font_family") in {"sans", "bold", "serif", "mono"}
                else "sans",
                payload.get("timezone", "Europe/Kyiv").strip(),
            ),
        )
        db.commit()
    update_settings({"config_nonce": secrets.token_hex(8)})
    return jsonify(ok=True)


@app.delete("/api/text-overlays/<int:overlay_id>")
@login_required
def delete_text_overlay(overlay_id):
    with connect() as db:
        db.execute("DELETE FROM text_overlays WHERE id = ?", (overlay_id,))
        db.commit()
    update_settings({"config_nonce": secrets.token_hex(8)})
    return jsonify(ok=True)


@app.post("/api/media-overlays/<int:overlay_id>")
@login_required
def update_media_overlay(overlay_id):
    payload = request.get_json()
    position = payload.get("position", "bottom-right")
    if position not in {
        "top-left", "top-center", "top-right", "center",
        "bottom-left", "bottom-center", "bottom-right",
    }:
        return jsonify(error="Невірна позиція"), 400
    with connect() as db:
        db.execute(
            """UPDATE media_overlays SET position = ?, offset_x = ?, offset_y = ?,
               width = ?, interval_minutes = ?, duration_seconds = ?,
               enabled = ? WHERE id = ?""",
            (
                position,
                int(payload.get("offset_x", 30)),
                int(payload.get("offset_y", 30)),
                max(80, min(1920, int(payload.get("width", 420)))),
                max(0, min(1440, int(payload.get("interval_minutes", 0)))),
                max(1, min(300, int(payload.get("duration_seconds", 10)))),
                bool(payload.get("enabled", True)),
                overlay_id,
            ),
        )
        db.commit()
    update_settings({"config_nonce": secrets.token_hex(8)})
    return jsonify(ok=True)


@app.delete("/api/media-overlays/<int:overlay_id>")
@login_required
def delete_media_overlay(overlay_id):
    with connect() as db:
        row = db.execute(
            "SELECT filename FROM media_overlays WHERE id = ?", (overlay_id,)
        ).fetchone()
        db.execute("DELETE FROM media_overlays WHERE id = ?", (overlay_id,))
        db.commit()
    if row:
        (OVERLAY_DIR / row["filename"]).unlink(missing_ok=True)
    update_settings({"config_nonce": secrets.token_hex(8)})
    return jsonify(ok=True)


@app.post("/api/telegram")
@login_required
def save_telegram():
    payload = request.get_json()
    values = {
        "telegram_enabled": "1" if payload.get("enabled") else "0",
        "telegram_nonce": secrets.token_hex(8),
    }
    if payload.get("token", "").strip():
        values["telegram_token"] = payload["token"].strip()
    if payload.get("user_id", "").strip():
        values["telegram_user_id"] = payload["user_id"].strip()
    update_settings(values)
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000)
