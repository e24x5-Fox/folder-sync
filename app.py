#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Конвейер копирования — веб-приложение (Flask) для копирования выбранных
файлов/папок в выбранное место с проверкой "стабильности" файла перед
копированием (файл не копируется, пока он ещё дозаписывается).

Каждое правило (папка/файл-источник -> назначение) синхронизируется в своём
собственном потоке, поэтому несколько папок копируются ОДНОВРЕМЕННО, а не
по очереди. У каждого правила можно задать свои собственные настройки
(интервал проверки стабильности и пропуск неизменных файлов), либо
использовать общие настройки.

Доступ к веб-интерфейсу и API защищён ключом безопасности (API-key).
Ключ создаётся автоматически при первом запуске и сохраняется в файл
secret.json рядом со скриптом (в репозиторий этот файл попадать не должен —
он уже добавлен в .gitignore). Ключ нужно ввести один раз в браузере,
дальше используется сессионная cookie.

Запуск:
    pip install -r requirements.txt
    python app.py
Затем откройте в браузере: http://127.0.0.1:5000
При первом запуске в консоли будет выведен ключ безопасности — он нужен
для входа в веб-интерфейс.

Переменные окружения:
    PORT              — порт веб-сервера (по умолчанию 5000)
    HOST              — адрес для прослушивания (по умолчанию 127.0.0.1)
    FOLDER_SYNC_KEY   — задать свой ключ безопасности вместо автогенерации

Состояние (правила копирования, настройки, флаг "запущено") сохраняется
в config.json рядом со скриптом — поэтому кнопка запуска "помнит" своё
состояние между перезапусками сервера.
"""

import os
import sys
import json
import time
import shutil
import string
import secrets
import hmac
import threading
import uuid
from pathlib import Path
from functools import wraps
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import (
    Flask, request, jsonify, render_template, session, redirect,
    url_for, make_response,
)

if getattr(sys, "frozen", False):
    # Собрано PyInstaller-ом: шаблоны/статика лежат в распакованном
    # временном каталоге (sys._MEIPASS), а config.json/secret.json должны
    # сохраняться РЯДОМ С САМИМ .exe, чтобы не терять состояние между запусками.
    BASE_DIR = Path(sys.executable).resolve().parent
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR))
else:
    BASE_DIR = Path(__file__).resolve().parent
    BUNDLE_DIR = BASE_DIR

CONFIG_PATH = BASE_DIR / "config.json"
SECRET_PATH = BASE_DIR / "secret.json"

app = Flask(
    __name__,
    template_folder=str(BUNDLE_DIR / "templates"),
    static_folder=str(BUNDLE_DIR / "static"),
)

state_lock = threading.RLock()
log_lock = threading.RLock()

DEFAULT_STATE = {
    "routes": [],          # [{id, name, type, source, destination, enabled,
                           #   interval, skip_unchanged, threads}]
                           # (interval/skip_unchanged/threads: null = "как в общих")
    "interval": 5,
    "skip_unchanged": True,
    "threads": 2,          # общее число потоков копирования на правило по умолчанию
    "running": False,
}

log_buffer = []            # [{"i": int, "t": epoch, "msg": str, "level": str}]
log_counter = 0

# route_id -> {"thread": Thread, "stop_event": Event}
workers = {}
workers_lock = threading.RLock()

CHUNK_SIZE = 4 * 1024 * 1024  # 4 МБ — размер порции для копирования с прогрессом

# --------------------------------------------------------------------------
# Прогресс синхронизации (для живого отображения в интерфейсе)
# --------------------------------------------------------------------------
# Правила синхронизируются параллельно (свой поток на каждое), а внутри
# правила ещё и несколько файлов могут копироваться одновременно (threads).
# Поэтому прогресс хранится НА ПРАВИЛО, а внутри — набор "active" копий,
# идущих прямо сейчас (может быть больше одной, если threads > 1).

progress_lock = threading.RLock()
progress = {}  # route_id -> {...}


def _route_progress(route_id):
    return progress.setdefault(route_id, {
        "phase": "idle",           # idle | scanning | copying
        "scanned_files": 0,
        "pass_total_files": 0,
        "pass_total_bytes": 0,
        "pass_done_files": 0,
        "pass_done_bytes": 0,
        "pass_skipped_files": 0,
        "active": {},               # src_path -> {size, done, speed, state}
        "queue": [],                 # предстоящие файлы этого прохода (превью)
    })


def reset_route_progress(route_id, phase="scanning"):
    with progress_lock:
        progress[route_id] = {
            "phase": phase,
            "scanned_files": 0,
            "pass_total_files": 0,
            "pass_total_bytes": 0,
            "pass_done_files": 0,
            "pass_done_bytes": 0,
            "pass_skipped_files": 0,
            "active": {},
            "queue": [],
        }


def set_route_phase(route_id, phase):
    with progress_lock:
        _route_progress(route_id)["phase"] = phase


def set_scanned(route_id, scanned):
    with progress_lock:
        _route_progress(route_id)["scanned_files"] = scanned


def set_pass_totals(route_id, total_files, total_bytes, skipped, queue):
    with progress_lock:
        p = _route_progress(route_id)
        p["phase"] = "copying" if total_files else "idle"
        p["pass_total_files"] = total_files
        p["pass_total_bytes"] = total_bytes
        p["pass_skipped_files"] = skipped
        p["queue"] = queue


def mark_active(route_id, src_path, size):
    with progress_lock:
        p = _route_progress(route_id)
        p["active"][src_path] = {"size": size, "done": 0, "speed": 0.0, "state": "waiting"}
        p["queue"] = [q for q in p["queue"] if q["source"] != src_path]


def set_active_state(route_id, src_path, state):
    with progress_lock:
        active = _route_progress(route_id)["active"]
        if src_path in active:
            active[src_path]["state"] = state


def update_active_progress(route_id, src_path, done, speed):
    with progress_lock:
        active = _route_progress(route_id)["active"]
        if src_path in active:
            active[src_path]["done"] = done
            active[src_path]["speed"] = speed


def clear_active(route_id, src_path):
    with progress_lock:
        _route_progress(route_id)["active"].pop(src_path, None)


def finish_task(route_id, result, size):
    with progress_lock:
        p = _route_progress(route_id)
        p["pass_done_files"] += 1
        if result == "copied":
            p["pass_done_bytes"] += size


def clear_route_progress(route_id):
    with progress_lock:
        progress.pop(route_id, None)


# --------------------------------------------------------------------------
# Безопасность: ключ доступа + сессии
# --------------------------------------------------------------------------

def _load_or_create_secret():
    """Загружает (или создаёт при первом запуске) ключ API и секрет сессии."""
    if SECRET_PATH.exists():
        try:
            with open(SECRET_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("api_key") and data.get("session_secret"):
                return data
        except Exception:
            pass

    api_key = os.environ.get("FOLDER_SYNC_KEY") or secrets.token_urlsafe(24)
    data = {"api_key": api_key, "session_secret": secrets.token_hex(32)}
    try:
        with open(SECRET_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        try:
            os.chmod(SECRET_PATH, 0o600)
        except Exception:
            pass
    except Exception as e:
        print(f"[!] Не удалось сохранить secret.json: {e}", file=sys.stderr)
    return data


_secret_data = _load_or_create_secret()
API_KEY = _secret_data["api_key"]
app.secret_key = _secret_data["session_secret"]
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

# защита от подбора ключа: IP -> [временные метки неудачных попыток]
_failed_attempts = {}
_failed_lock = threading.Lock()
MAX_ATTEMPTS = 6
LOCKOUT_SECONDS = 300
ATTEMPT_WINDOW = 300


def _client_ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()


def _is_locked_out(ip):
    with _failed_lock:
        attempts = [t for t in _failed_attempts.get(ip, []) if time.time() - t < ATTEMPT_WINDOW]
        _failed_attempts[ip] = attempts
        return len(attempts) >= MAX_ATTEMPTS


def _register_failed_attempt(ip):
    with _failed_lock:
        _failed_attempts.setdefault(ip, []).append(time.time())


def _check_key(candidate):
    if not candidate:
        return False
    return hmac.compare_digest(str(candidate), API_KEY)


def _request_is_authenticated():
    # 1) авторизованная сессия браузера
    if session.get("authenticated"):
        return True
    # 2) программный доступ по заголовку/параметру (для скриптов/curl)
    header_key = request.headers.get("X-API-Key")
    query_key = request.args.get("key")
    if _check_key(header_key) or _check_key(query_key):
        return True
    return False


PUBLIC_ENDPOINTS = {"login", "static"}


@app.before_request
def _enforce_auth():
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if _request_is_authenticated():
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "Требуется авторизация (ключ безопасности)"}), 401
    return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        ip = _client_ip()
        if _is_locked_out(ip):
            error = "Слишком много неверных попыток. Подождите несколько минут."
        else:
            candidate = (request.form.get("api_key") or "").strip()
            if _check_key(candidate):
                session.clear()
                session["authenticated"] = True
                session.permanent = True
                nxt = request.args.get("next") or "/"
                return redirect(nxt)
            _register_failed_attempt(ip)
            error = "Неверный ключ безопасности."
    return render_template("login.html", error=error)


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/security", methods=["GET"])
def api_security_info():
    # ключ показывается только уже авторизованной сессии браузера — не по
    # программному X-API-Key, чтобы обладание ключом не раскрывало его же.
    reveal = bool(session.get("authenticated"))
    return jsonify({
        "key_set": True,
        "api_key": API_KEY if reveal else None,
        "host": request.host,
    })


@app.route("/api/security/regenerate", methods=["POST"])
def api_security_regenerate():
    global API_KEY, _secret_data
    if not session.get("authenticated"):
        return jsonify({"error": "Доступно только из веб-интерфейса"}), 403
    new_key = secrets.token_urlsafe(24)
    _secret_data["api_key"] = new_key
    try:
        with open(SECRET_PATH, "w", encoding="utf-8") as f:
            json.dump(_secret_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        return jsonify({"error": f"Не удалось сохранить ключ: {e}"}), 500
    API_KEY = new_key
    add_log("Ключ безопасности пересоздан", "warn")
    return jsonify({"api_key": API_KEY})


# --------------------------------------------------------------------------
# Состояние / персистентность
# --------------------------------------------------------------------------

def path_contains(parent, child):
    """True, если child совпадает с parent или лежит внутри него."""
    try:
        parent_r = os.path.realpath(parent)
        child_r = os.path.realpath(child)
        if parent_r == child_r:
            return True
        return os.path.commonpath([parent_r, child_r]) == parent_r
    except (ValueError, OSError):
        return False


def default_route_stats():
    return {"files_copied": 0, "bytes_copied": 0}


def ensure_route_stats(route):
    stats = route.get("stats")
    if not isinstance(stats, dict):
        stats = default_route_stats()
        route["stats"] = stats
    stats.setdefault("files_copied", 0)
    stats.setdefault("bytes_copied", 0)
    return stats


def load_state():
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            merged = json.loads(json.dumps(DEFAULT_STATE))
            merged.update(data)
            for route in merged.get("routes", []):
                ensure_route_stats(route)
            return merged
        except Exception:
            pass
    return json.loads(json.dumps(DEFAULT_STATE))


def save_state():
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        add_log(f"Не удалось сохранить config.json: {e}", "error")


state = load_state()


def add_log(msg, level="info"):
    global log_counter
    with log_lock:
        log_counter += 1
        log_buffer.append({"i": log_counter, "t": time.time(), "msg": msg, "level": level})
        if len(log_buffer) > 2000:
            del log_buffer[: len(log_buffer) - 2000]


# --------------------------------------------------------------------------
# Логика копирования
# --------------------------------------------------------------------------

def file_signature(path):
    st = os.stat(path)
    return st.st_size, st.st_mtime


def wait_until_stable(path, interval, stop_evt):
    """Ждёт, пока файл перестанет меняться (по размеру и mtime).
    Проверяет stop_evt часто, чтобы быстро реагировать на остановку."""
    while True:
        if stop_evt.is_set():
            return False
        try:
            sig1 = file_signature(path)
        except (FileNotFoundError, OSError):
            add_log(f"Файл недоступен, пропускаю: {path}", "warn")
            return False

        waited = 0.0
        step = 0.2
        while waited < interval:
            if stop_evt.is_set():
                return False
            time.sleep(min(step, interval - waited))
            waited += step

        if stop_evt.is_set():
            return False
        try:
            sig2 = file_signature(path)
        except (FileNotFoundError, OSError):
            add_log(f"Файл недоступен, пропускаю: {path}", "warn")
            return False

        if sig1 == sig2:
            return True
        add_log(f"Файл ещё меняется, жду ещё {interval:g} с: {path}", "warn")


def is_up_to_date(src_path, dst_path):
    if not os.path.exists(dst_path):
        return False
    try:
        s_size, s_mtime = file_signature(src_path)
        d_size, d_mtime = file_signature(dst_path)
    except (FileNotFoundError, OSError):
        return False
    return s_size == d_size and d_mtime >= s_mtime - 1


def copy_one_file(src_path, dst_path, interval, skip_unchanged, stop_evt, route_id=None, size=0):
    if skip_unchanged and is_up_to_date(src_path, dst_path):
        return "skipped"

    if route_id is not None:
        mark_active(route_id, src_path, size)
    add_log(f"Проверяю: {src_path}")
    if not wait_until_stable(src_path, interval, stop_evt):
        if route_id is not None:
            clear_active(route_id, src_path)
        return "aborted"
    if stop_evt.is_set():
        if route_id is not None:
            clear_active(route_id, src_path)
        return "aborted"

    try:
        dst_dir = os.path.dirname(dst_path)
        if dst_dir:
            os.makedirs(dst_dir, exist_ok=True)

        if route_id is not None:
            set_active_state(route_id, src_path, "copying")

        start = time.monotonic()
        last_tick = start
        last_done = 0
        done = 0
        with open(src_path, "rb") as fsrc, open(dst_path, "wb") as fdst:
            while True:
                if stop_evt.is_set():
                    fdst.close()
                    try:
                        os.remove(dst_path)
                    except OSError:
                        pass
                    if route_id is not None:
                        clear_active(route_id, src_path)
                    return "aborted"
                chunk = fsrc.read(CHUNK_SIZE)
                if not chunk:
                    break
                fdst.write(chunk)
                done += len(chunk)
                now = time.monotonic()
                dt = now - last_tick
                if dt >= 0.2 and route_id is not None:
                    update_active_progress(route_id, src_path, done, (done - last_done) / dt)
                    last_tick = now
                    last_done = done
        shutil.copystat(src_path, dst_path)
        add_log(f"Скопировано → {dst_path}", "ok")
        return "copied"
    except Exception as e:
        add_log(f"Ошибка копирования {src_path}: {e}", "error")
        return "error"
    finally:
        if route_id is not None:
            clear_active(route_id, src_path)


def resolve_file_destination(src_file, dst):
    """Определяет итоговый путь для копии одиночного файла."""
    if os.path.isdir(dst) or dst.endswith(os.sep) or dst.endswith("/"):
        return os.path.join(dst, os.path.basename(src_file))
    return dst


def effective_settings(route):
    """Индивидуальные настройки правила, либо общие, если не заданы."""
    with state_lock:
        g_interval = float(state.get("interval", 5))
        g_skip = bool(state.get("skip_unchanged", True))
        g_threads = int(state.get("threads", 2) or 1)
    interval = route.get("interval")
    interval = g_interval if interval is None else float(interval)
    skip_unchanged = route.get("skip_unchanged")
    skip_unchanged = g_skip if skip_unchanged is None else bool(skip_unchanged)
    threads = route.get("threads")
    threads = g_threads if threads is None else int(threads)
    threads = max(1, min(32, threads))
    return interval, skip_unchanged, threads


def record_route_stats(route_id, files_count, bytes_count):
    """Накапливает статистику правила (сколько скопировано за всё время)
    и сохраняет её в config.json. Вызывается один раз по итогам прохода,
    а не на каждый файл — чтобы не долбить диск лишними записями."""
    with state_lock:
        for r in state["routes"]:
            if r["id"] == route_id:
                stats = ensure_route_stats(r)
                stats["files_copied"] += files_count
                stats["bytes_copied"] += bytes_count
                break
        save_state()


def _root_is_reachable(path):
    """Проверяет, что диск/устройство, на котором лежит путь, вообще
    подключено — чтобы не долбить каждые пару секунд отвалившееся
    устройство (это и вызывает лишние всплывающие окна проводника/
    автозапуска в Windows) и не сыпать одну и ту же ошибку на каждый файл."""
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    if not drive:
        return True
    try:
        return os.path.exists(drive + os.sep)
    except OSError:
        return False


def process_route(route, stop_evt):
    """Выполняет один проход синхронизации правила.
    Возвращает True при успешном проходе (даже если часть файлов
    пропущена/с ошибкой сети на конкретном файле) и False при
    "жёсткой" ошибке уровня всего правила (источник/назначение
    недоступны целиком) — это используется воркером для увеличения
    паузы перед следующей попыткой (backoff), чтобы не долбить
    отвалившееся устройство."""
    route_id = route.get("id")
    src = route["source"]
    dst = route["destination"]
    label = route.get("name") or src
    interval, skip_unchanged, threads = effective_settings(route)

    reset_route_progress(route_id, phase="scanning")

    if not _root_is_reachable(src):
        add_log(f"[{label}] диск/устройство источника недоступно: {src}", "error")
        set_route_phase(route_id, "idle")
        return False
    if not os.path.exists(src):
        add_log(f"[{label}] источник не найден: {src}", "error")
        set_route_phase(route_id, "idle")
        return False
    if not _root_is_reachable(dst):
        add_log(f"[{label}] диск/устройство назначения недоступно: {dst}", "error")
        set_route_phase(route_id, "idle")
        return False

    if os.path.isfile(src):
        try:
            size = os.path.getsize(src)
        except OSError:
            size = 0
        dst_path = resolve_file_destination(src, dst)
        if skip_unchanged and is_up_to_date(src, dst_path):
            set_pass_totals(route_id, total_files=0, total_bytes=0, skipped=1, queue=[])
            set_route_phase(route_id, "idle")
            return True
        set_pass_totals(route_id, total_files=1, total_bytes=size, skipped=0,
                        queue=[{"source": src, "destination": dst_path, "size": size}])
        status = copy_one_file(src, dst_path, interval, skip_unchanged, stop_evt, route_id, size)
        finish_task(route_id, status, size)
        if status == "copied":
            record_route_stats(route_id, 1, size)
        set_route_phase(route_id, "idle")
        return status != "error"

    # источник — папка: сканируем и решаем, что нужно скопировать, а что
    # уже актуально (skip_unchanged) — заодно считаем общий объём для
    # прогресс-бара и превью очереди
    tasks = []
    skipped = 0
    scanned = 0
    last_tick = time.monotonic()
    try:
        for root, dirs, files in os.walk(src):
            if stop_evt.is_set():
                set_route_phase(route_id, "idle")
                return True
            rel = os.path.relpath(root, src)
            dst_dir = dst if rel == "." else os.path.join(dst, rel)
            for fname in files:
                if stop_evt.is_set():
                    set_route_phase(route_id, "idle")
                    return True
                s_path = os.path.join(root, fname)
                d_path = os.path.join(dst_dir, fname)
                if skip_unchanged and is_up_to_date(s_path, d_path):
                    skipped += 1
                else:
                    try:
                        size = os.path.getsize(s_path)
                    except OSError:
                        size = 0
                    tasks.append((s_path, d_path, size))
                scanned += 1
                now = time.monotonic()
                if now - last_tick > 0.2:
                    set_scanned(route_id, scanned)
                    last_tick = now
    except OSError as e:
        add_log(f"[{label}] ошибка чтения источника: {e}", "error")
        set_route_phase(route_id, "idle")
        return False

    total_bytes = sum(t[2] for t in tasks)
    set_pass_totals(
        route_id, total_files=len(tasks), total_bytes=total_bytes, skipped=skipped,
        queue=[{"source": s, "destination": d, "size": sz} for s, d, sz in tasks],
    )

    if not tasks:
        if skipped:
            add_log(f"[{label}] проход завершён: всё уже актуально ({skipped})", "ok")
        set_route_phase(route_id, "idle")
        return True

    error_count = 0
    copied_count = 0
    copied_bytes = 0

    if threads <= 1 or len(tasks) <= 1:
        for s_path, d_path, size in tasks:
            if stop_evt.is_set():
                break
            result = copy_one_file(s_path, d_path, interval, skip_unchanged, stop_evt, route_id, size)
            finish_task(route_id, result, size)
            if result == "error":
                error_count += 1
            elif result == "copied":
                copied_count += 1
                copied_bytes += size
    else:
        # несколько файлов этого правила копируются одновременно —
        # количество потоков задаётся в настройках правила
        with ThreadPoolExecutor(max_workers=threads, thread_name_prefix=f"copy-{route_id}") as pool:
            futures = {
                pool.submit(copy_one_file, s_path, d_path, interval, skip_unchanged, stop_evt, route_id, size): (s_path, size)
                for s_path, d_path, size in tasks
            }
            for fut in as_completed(futures):
                s_path, size = futures[fut]
                try:
                    result = fut.result()
                except Exception as e:
                    add_log(f"[{label}] непредвиденная ошибка копирования {s_path}: {e}", "error")
                    result = "error"
                finish_task(route_id, result, size)
                if result == "error":
                    error_count += 1
                elif result == "copied":
                    copied_count += 1
                    copied_bytes += size

    if copied_count or skipped:
        msg = f"[{label}] проход завершён: скопировано {copied_count}, уже актуально {skipped}"
        if error_count:
            msg += f", ошибок {error_count}"
        add_log(msg, "warn" if error_count else "ok")

    if copied_count:
        record_route_stats(route_id, copied_count, copied_bytes)

    set_route_phase(route_id, "idle")

    # если ошибки почти на всех файлах — скорее всего, отвалилось само
    # устройство/диск, а не отдельные файлы; сигналим воркеру об этом
    if error_count and error_count >= len(tasks):
        return False
    return True


# --------------------------------------------------------------------------
# Воркеры: один поток на каждое правило -> папки синхронизируются ОДНОВРЕМЕННО
# --------------------------------------------------------------------------

def _find_route(route_id):
    with state_lock:
        for r in state["routes"]:
            if r["id"] == route_id:
                return dict(r)
    return None


BASE_PAUSE = 2.0     # обычная пауза между проходами правила, сек
MAX_BACKOFF_PAUSE = 60.0  # верхняя граница паузы при повторяющихся ошибках


def route_worker_loop(route_id, stop_evt):
    add_log(f"[{route_id}] поток синхронизации запущен", "ok")
    consecutive_failures = 0
    try:
        while not stop_evt.is_set():
            route = _find_route(route_id)
            if route is None or not route.get("enabled", True):
                break
            ok = process_route(route, stop_evt)

            if ok:
                if consecutive_failures:
                    add_log(f"[{route_id}] правило снова работает нормально", "ok")
                consecutive_failures = 0
                pause = BASE_PAUSE
            else:
                consecutive_failures += 1
                # экспоненциальный backoff: 2 -> 4 -> 8 -> 16 -> 32 -> 60...
                # чтобы не долбить отвалившийся диск/устройство каждые 2 сек
                # (именно это и вызывало лишние всплывающие окна проводника)
                pause = min(BASE_PAUSE * (2 ** consecutive_failures), MAX_BACKOFF_PAUSE)
                if consecutive_failures == 1 or pause >= MAX_BACKOFF_PAUSE:
                    add_log(
                        f"[{route_id}] источник/назначение недоступны, "
                        f"следующая попытка через {pause:g} с",
                        "warn",
                    )

            waited = 0.0
            while waited < pause and not stop_evt.is_set():
                time.sleep(0.2)
                waited += 0.2
    finally:
        clear_route_progress(route_id)
        add_log(f"[{route_id}] поток синхронизации остановлен", "warn")


def sync_workers_with_routes():
    """Запускает потоки для новых/включённых правил и останавливает для
    удалённых/выключенных. Вызывается при старте и при любом изменении
    списка правил, пока синхронизация включена."""
    with state_lock:
        wanted_ids = {r["id"] for r in state["routes"] if r.get("enabled", True)}

    with workers_lock:
        # остановить лишние
        for route_id in list(workers.keys()):
            if route_id not in wanted_ids:
                workers[route_id]["stop_event"].set()
                del workers[route_id]
        # запустить недостающие
        for route_id in wanted_ids:
            if route_id not in workers:
                stop_evt = threading.Event()
                t = threading.Thread(target=route_worker_loop, args=(route_id, stop_evt), daemon=True)
                workers[route_id] = {"thread": t, "stop_event": stop_evt}
                t.start()


def any_worker_running():
    with workers_lock:
        return any(w["thread"].is_alive() for w in workers.values())


def start_worker():
    sync_workers_with_routes()


def stop_worker():
    with workers_lock:
        for w in workers.values():
            w["stop_event"].set()
        workers.clear()
    add_log("Синхронизация остановлена", "warn")


# --------------------------------------------------------------------------
# Веб-страница
# --------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


# --------------------------------------------------------------------------
# API: состояние / логи
# --------------------------------------------------------------------------

@app.route("/api/state")
def api_state():
    with state_lock:
        return jsonify({
            "routes": state["routes"],
            "interval": state["interval"],
            "skip_unchanged": state["skip_unchanged"],
            "threads": state.get("threads", 2),
            "running": state.get("running", False) and any_worker_running(),
        })


@app.route("/api/logs")
def api_logs():
    since = int(request.args.get("since", 0))
    with log_lock:
        new_logs = [l for l in log_buffer if l["i"] > since]
        last = log_counter
    return jsonify({"logs": new_logs, "last": last})


@app.route("/api/progress")
def api_progress():
    with progress_lock:
        return jsonify({rid: dict(p, active=dict(p["active"])) for rid, p in progress.items()})


# --------------------------------------------------------------------------
# API: обзор файловой системы (для выбора путей из браузера)
# --------------------------------------------------------------------------

def list_roots():
    if os.name == "nt":
        roots = []
        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            if os.path.exists(drive):
                roots.append({"name": drive, "path": drive, "is_dir": True})
        return roots
    return [{"name": "/", "path": "/", "is_dir": True}]


@app.route("/api/browse")
def api_browse():
    raw_path = request.args.get("path", "")
    only_dirs = request.args.get("dirs_only", "0") == "1"

    if raw_path in ("", "__ROOT__"):
        return jsonify({
            "current": None,
            "parent": None,
            "entries": list_roots(),
            "home": str(Path.home()),
        })

    try:
        p = Path(raw_path).expanduser()
        if not p.exists() or not p.is_dir():
            return jsonify({"error": "Папка не найдена"}), 400

        entries = []
        try:
            children = sorted(
                p.iterdir(),
                key=lambda e: (not e.is_dir(), e.name.lower())
            )
        except PermissionError:
            return jsonify({"error": "Нет доступа к этой папке"}), 403

        for entry in children:
            try:
                if entry.name.startswith("."):
                    continue
                is_dir = entry.is_dir()
                if only_dirs and not is_dir:
                    continue
                entries.append({
                    "name": entry.name,
                    "path": str(entry),
                    "is_dir": is_dir,
                })
            except OSError:
                continue

        parent = str(p.parent) if str(p.parent) != str(p) else None
        return jsonify({
            "current": str(p),
            "parent": parent,
            "entries": entries,
            "home": str(Path.home()),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# --------------------------------------------------------------------------
# API: правила копирования (routes)
# --------------------------------------------------------------------------

def _parse_override_interval(data, out):
    if "interval" not in data:
        return
    val = data["interval"]
    if val is None or val == "":
        out["interval"] = None
    else:
        try:
            out["interval"] = max(0.0, float(val))
        except (TypeError, ValueError):
            pass


def _parse_override_skip(data, out):
    if "skip_unchanged" not in data:
        return
    val = data["skip_unchanged"]
    out["skip_unchanged"] = None if val is None else bool(val)


def _parse_override_threads(data, out):
    if "threads" not in data:
        return
    val = data["threads"]
    if val is None or val == "":
        out["threads"] = None
    else:
        try:
            out["threads"] = max(1, min(32, int(val)))
        except (TypeError, ValueError):
            pass


@app.route("/api/routes", methods=["POST"])
def api_add_route():
    data = request.get_json(force=True, silent=True) or {}
    source = (data.get("source") or "").strip()
    destination = (data.get("destination") or "").strip()
    name = (data.get("name") or "").strip()

    if not source or not destination:
        return jsonify({"error": "Укажите источник и назначение"}), 400
    if not os.path.exists(source):
        return jsonify({"error": "Указанный источник не найден на диске"}), 400
    if os.path.isdir(source) and (path_contains(source, destination) or path_contains(destination, source)):
        return jsonify({
            "error": "Назначение не должно быть вложено в источник (и наоборот) — "
                     "это вызовет бесконечное копирование папки саму в себя"
        }), 400

    route_type = "folder" if os.path.isdir(source) else "file"
    route = {
        "id": uuid.uuid4().hex[:8],
        "name": name or os.path.basename(source.rstrip("/\\")) or source,
        "type": route_type,
        "source": source,
        "destination": destination,
        "enabled": True,
        "interval": None,          # None = использовать общие настройки
        "skip_unchanged": None,    # None = использовать общие настройки
        "threads": None,           # None = использовать общие настройки
        "stats": default_route_stats(),
    }
    _parse_override_interval(data, route)
    _parse_override_skip(data, route)
    _parse_override_threads(data, route)

    with state_lock:
        state["routes"].append(route)
        save_state()
        running = state.get("running", False)
    add_log(f"Добавлено правило «{route['name']}»: {source} → {destination}")
    if running:
        sync_workers_with_routes()
    return jsonify(route)


@app.route("/api/routes/<route_id>", methods=["PUT"])
def api_edit_route(route_id):
    data = request.get_json(force=True, silent=True) or {}
    with state_lock:
        for r in state["routes"]:
            if r["id"] == route_id:
                new_source = data["source"].strip() if isinstance(data.get("source"), str) else r["source"]
                new_destination = (
                    data["destination"].strip() if isinstance(data.get("destination"), str) else r["destination"]
                )
                if "source" in data and not os.path.exists(new_source):
                    return jsonify({"error": "Указанный источник не найден на диске"}), 400
                if os.path.isdir(new_source) and (
                    path_contains(new_source, new_destination) or path_contains(new_destination, new_source)
                ):
                    return jsonify({
                        "error": "Назначение не должно быть вложено в источник (и наоборот) — "
                                 "это вызовет бесконечное копирование папки саму в себя"
                    }), 400

                for field in ("source", "destination", "name"):
                    if field in data and data[field] is not None:
                        r[field] = data[field].strip()
                if "enabled" in data:
                    r["enabled"] = bool(data["enabled"])
                if "source" in data:
                    r["type"] = "folder" if os.path.isdir(r["source"]) else "file"
                _parse_override_interval(data, r)
                _parse_override_skip(data, r)
                _parse_override_threads(data, r)
                save_state()
                running = state.get("running", False)
                result = dict(r)
                break
        else:
            return jsonify({"error": "Правило не найдено"}), 404
    if running:
        sync_workers_with_routes()
    return jsonify(result)


@app.route("/api/routes/<route_id>", methods=["DELETE"])
def api_delete_route(route_id):
    with state_lock:
        before = len(state["routes"])
        state["routes"] = [r for r in state["routes"] if r["id"] != route_id]
        save_state()
        removed = before != len(state["routes"])
        running = state.get("running", False)
    if removed:
        clear_route_progress(route_id)
        add_log("Правило удалено")
        if running:
            sync_workers_with_routes()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# API: настройки
# --------------------------------------------------------------------------

@app.route("/api/settings", methods=["POST"])
def api_settings():
    data = request.get_json(force=True, silent=True) or {}
    with state_lock:
        if "interval" in data:
            try:
                state["interval"] = max(0.0, float(data["interval"]))
            except (TypeError, ValueError):
                pass
        if "skip_unchanged" in data:
            state["skip_unchanged"] = bool(data["skip_unchanged"])
        if "threads" in data:
            try:
                state["threads"] = max(1, min(32, int(data["threads"])))
            except (TypeError, ValueError):
                pass
        save_state()
        return jsonify({
            "interval": state["interval"],
            "skip_unchanged": state["skip_unchanged"],
            "threads": state.get("threads", 2),
        })


# --------------------------------------------------------------------------
# API: старт / стоп
# --------------------------------------------------------------------------

@app.route("/api/start", methods=["POST"])
def api_start():
    with state_lock:
        if not state["routes"]:
            return jsonify({"error": "Добавьте хотя бы одно правило копирования"}), 400
        state["running"] = True
        save_state()
    start_worker()
    add_log("Синхронизация запущена (параллельно по каждому правилу)", "ok")
    return jsonify({"running": True})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    with state_lock:
        state["running"] = False
        save_state()
    stop_worker()
    return jsonify({"running": False})


# --------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 62)
    print(" Конвейер копирования")
    print("=" * 62)
    print(f" Ключ безопасности: {API_KEY}")
    print(" (нужен для входа в веб-интерфейс; хранится в secret.json)")
    print("=" * 62)

    if state.get("running"):
        add_log("Обнаружено сохранённое состояние: синхронизация была запущена — возобновляю...", "ok")
        start_worker()

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", 5000))
    if host not in ("127.0.0.1", "localhost"):
        print(f" [!] Внимание: сервер слушает {host} — доступ извне защищён ключом безопасности,")
        print("     но убедитесь, что порт не открыт наружу без необходимости (файрвол/роутер).")
    display_host = "127.0.0.1" if host == "0.0.0.0" else host
    url = f"http://{display_host}:{port}"
    print(f" Открой в браузере: {url}")

    if getattr(sys, "frozen", False):
        import webbrowser
        import threading as _threading
        _threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    app.run(host=host, port=port, debug=False)
