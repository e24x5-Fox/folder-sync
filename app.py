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
                           #   interval, skip_unchanged}]  (interval/skip_unchanged: null = "как в общих")
    "interval": 5,
    "skip_unchanged": True,
    "running": False,
}

log_buffer = []            # [{"i": int, "t": epoch, "msg": str, "level": str}]
log_counter = 0

# route_id -> {"thread": Thread, "stop_event": Event}
workers = {}
workers_lock = threading.RLock()


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

def load_state():
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            merged = json.loads(json.dumps(DEFAULT_STATE))
            merged.update(data)
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


def copy_one_file(src_path, dst_path, interval, skip_unchanged, stop_evt):
    if skip_unchanged and is_up_to_date(src_path, dst_path):
        return "skipped"
    add_log(f"Проверяю: {src_path}")
    if not wait_until_stable(src_path, interval, stop_evt):
        return "aborted"
    try:
        dst_dir = os.path.dirname(dst_path)
        if dst_dir:
            os.makedirs(dst_dir, exist_ok=True)
        shutil.copy2(src_path, dst_path)
        add_log(f"Скопировано → {dst_path}", "ok")
        return "copied"
    except Exception as e:
        add_log(f"Ошибка копирования {src_path}: {e}", "error")
        return "error"


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
    interval = route.get("interval")
    interval = g_interval if interval is None else float(interval)
    skip_unchanged = route.get("skip_unchanged")
    skip_unchanged = g_skip if skip_unchanged is None else bool(skip_unchanged)
    return interval, skip_unchanged


def process_route(route, stop_evt):
    src = route["source"]
    dst = route["destination"]
    label = route.get("name") or src
    interval, skip_unchanged = effective_settings(route)

    if not os.path.exists(src):
        add_log(f"[{label}] источник не найден: {src}", "error")
        return

    if os.path.isfile(src):
        dst_path = resolve_file_destination(src, dst)
        copy_one_file(src, dst_path, interval, skip_unchanged, stop_evt)
        return

    # источник — папка: копируем рекурсивно, сохраняя относительную структуру
    for root, dirs, files in os.walk(src):
        if stop_evt.is_set():
            return
        rel = os.path.relpath(root, src)
        dst_dir = dst if rel == "." else os.path.join(dst, rel)
        for fname in files:
            if stop_evt.is_set():
                return
            copy_one_file(
                os.path.join(root, fname),
                os.path.join(dst_dir, fname),
                interval, skip_unchanged, stop_evt,
            )


# --------------------------------------------------------------------------
# Воркеры: один поток на каждое правило -> папки синхронизируются ОДНОВРЕМЕННО
# --------------------------------------------------------------------------

def _find_route(route_id):
    with state_lock:
        for r in state["routes"]:
            if r["id"] == route_id:
                return dict(r)
    return None


def route_worker_loop(route_id, stop_evt):
    add_log(f"[{route_id}] поток синхронизации запущен", "ok")
    try:
        while not stop_evt.is_set():
            route = _find_route(route_id)
            if route is None or not route.get("enabled", True):
                break
            process_route(route, stop_evt)

            waited = 0.0
            while waited < 2.0 and not stop_evt.is_set():
                time.sleep(0.2)
                waited += 0.2
    finally:
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
            "running": state.get("running", False) and any_worker_running(),
        })


@app.route("/api/logs")
def api_logs():
    since = int(request.args.get("since", 0))
    with log_lock:
        new_logs = [l for l in log_buffer if l["i"] > since]
        last = log_counter
    return jsonify({"logs": new_logs, "last": last})


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
    }
    _parse_override_interval(data, route)
    _parse_override_skip(data, route)

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
                for field in ("source", "destination", "name"):
                    if field in data and data[field] is not None:
                        r[field] = data[field].strip()
                if "enabled" in data:
                    r["enabled"] = bool(data["enabled"])
                if "source" in data:
                    r["type"] = "folder" if os.path.isdir(r["source"]) else "file"
                _parse_override_interval(data, r)
                _parse_override_skip(data, r)
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
        save_state()
        return jsonify({"interval": state["interval"], "skip_unchanged": state["skip_unchanged"]})


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
