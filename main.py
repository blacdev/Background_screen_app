from __future__ import annotations

import argparse
import faulthandler
import json
import ipaddress
import mimetypes
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.error import URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QPoint, QPropertyAnimation, QSize, QTime, QTimer, Qt, QUrl, Signal
from PySide6.QtCore import QFileSystemWatcher
from PySide6.QtGui import QAction, QCloseEvent, QFontMetrics, QGuiApplication, QIcon, QImageReader, QPixmap, QScreen
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtNetwork import QAbstractSocket, QLocalServer, QLocalSocket, QNetworkInterface
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QPushButton,
    QSplitter,
    QStyle,
    QSystemTrayIcon,
    QLineEdit,
    QSizePolicy,
    QTimeEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid


# Long-running local playback has been more stable when Qt is allowed to use
# its normal Windows video pipeline. We keep a switch to force software
# decoding for specific installations that need it, but do not force that path
# globally anymore because hard native crashes are worse than a rendering
# fallback.
if sys.platform == "win32":
    if os.getenv("BACKGROUND_SCREEN_FORCE_SOFTWARE_DECODE", "").strip().lower() in {"1", "true", "yes", "on"}:
        os.environ.setdefault("QT_FFMPEG_DECODING_HW_DEVICE_TYPES", ",")
        os.environ.setdefault("QT_DISABLE_HW_TEXTURES_CONVERSION", "1")


UK_TZ = ZoneInfo("Europe/London")
APP_NAME = "Background Screen Controller"
APP_INSTALL_CONTEXT_NAME = "install_context.json"
APP_DATA_ENV_VAR = "BACKGROUND_SCREEN_DATA_DIR"
CONTROLLER_INSTANCE_NAME = "BackgroundScreenControllerInstance"
LAN_SERVER_PORT = 8765
ENGINE_CONTROL_PORT = 8766
REMOTE_SCREEN_TIMEOUT_SECONDS = 15.0
REMOTE_POLL_INTERVAL_MS = 900
REMOTE_SYNC_LEAD_MS = 1100
REMOTE_HEARTBEAT_INTERVAL_MS = 2500
NETWORK_SNAPSHOT_CACHE_SECONDS = 8.0
VIRTUAL_INTERFACE_HINTS = (
    "wsl",
    "vethernet",
    "hyper-v",
    "virtualbox",
    "vmware",
    "docker",
    "loopback",
    "tailscale",
    "zerotier",
)

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_ROOT = app_root()
LEGACY_APP_DATA_DIR = APP_ROOT / "app_data"
INSTALL_CONTEXT_PATH = APP_ROOT / APP_INSTALL_CONTEXT_NAME


def load_install_context() -> dict[str, str]:
    if not INSTALL_CONTEXT_PATH.exists():
        return {}
    try:
        data = json.loads(INSTALL_CONTEXT_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return {str(key): str(value) for key, value in data.items() if isinstance(key, str) and isinstance(value, str)}


def windows_data_dir_for_scope(scope: str) -> Path:
    scope_name = scope.lower().strip()
    if scope_name == "machine":
        base = os.getenv("PROGRAMDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.getenv("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / APP_NAME


def resolve_app_data_dir() -> Path:
    env_override = os.getenv(APP_DATA_ENV_VAR, "").strip()
    if env_override:
        return Path(env_override).expanduser()

    context = load_install_context()
    configured_data_dir = context.get("data_dir", "").strip()
    if configured_data_dir:
        return Path(configured_data_dir).expanduser()

    configured_scope = context.get("scope", "").strip()
    if sys.platform == "win32" and configured_scope:
        return windows_data_dir_for_scope(configured_scope)

    if sys.platform == "win32" and getattr(sys, "frozen", False):
        return windows_data_dir_for_scope("user")

    return LEGACY_APP_DATA_DIR


APP_DATA_DIR = resolve_app_data_dir()
CONFIG_PATH = APP_DATA_DIR / "config.json"
ENGINE_STARTUP_STATUS_PATH = APP_DATA_DIR / "engine_startup_status.json"
ENGINE_STARTUP_LOG_PATH = APP_DATA_DIR / "engine_startup.log"
ENGINE_FAULT_LOG_HANDLE = None
SUPPORTED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".ogg"}
SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
SUPPORTED_MEDIA_EXTENSIONS = SUPPORTED_VIDEO_EXTENSIONS | SUPPORTED_IMAGE_EXTENSIONS
CONTROL_HIDE_DELAY_MS = 2500
CONTROL_FADE_DURATION_MS = 1000
TRANSITION_TO_BLACK_MS = 320
TRANSITION_FROM_BLACK_MS = 520
TRANSITION_METHODS = {
    "fade_black": "Fade Through Black",
    "cut": "Cut",
    "soft_fade": "Soft Fade",
}
DAY_OPTIONS = [
    ("mon", "Mon"),
    ("tue", "Tue"),
    ("wed", "Wed"),
    ("thu", "Thu"),
    ("fri", "Fri"),
    ("sat", "Sat"),
    ("sun", "Sun"),
]
DAY_KEYS = [key for key, _label in DAY_OPTIONS]
DAY_LABELS = dict(DAY_OPTIONS)
DAY_KEY_TO_INDEX = {key: index for index, key in enumerate(DAY_KEYS)}
DAY_INDEX_TO_KEY = {index: key for key, index in DAY_KEY_TO_INDEX.items()}


def normalize_day_key(value: str) -> str | None:
    cleaned = value.strip().lower()
    for key in DAY_KEYS:
        if cleaned == key or cleaned.startswith(key):
            return key
    return None


def normalized_day_keys(days: list[str] | tuple[str, ...] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for item in days or []:
        key = normalize_day_key(str(item))
        if key is None or key in seen:
            continue
        seen.add(key)
        normalized.append(key)
    return normalized if normalized else DAY_KEYS[:]


def format_day_selection(days: list[str] | tuple[str, ...] | None) -> str:
    normalized = normalized_day_keys(list(days or []))
    if normalized == DAY_KEYS:
        return "Every day"
    return ", ".join(DAY_LABELS[key] for key in normalized)


@dataclass
class ScheduleEntry:
    id: str
    title: str
    start_time: str
    end_time: str
    video_file: str
    video_label: str
    screen_ids: list[str]
    days: list[str]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScheduleEntry":
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex),
            title=str(data.get("title") or "Untitled schedule"),
            start_time=str(data.get("start_time") or "00:00"),
            end_time=str(data.get("end_time") or "00:00"),
            video_file=str(data.get("video_file") or ""),
            video_label=str(data.get("video_label") or data.get("video_file") or ""),
            screen_ids=[str(item) for item in data.get("screen_ids", []) if isinstance(item, str)],
            days=[key for item in data.get("days", []) if isinstance(item, str) if (key := normalize_day_key(item)) is not None],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "video_file": self.video_file,
            "video_label": self.video_label,
            "screen_ids": self.screen_ids,
            "days": self.days,
        }

    @property
    def range_label(self) -> str:
        return f"{self.start_time} - {self.end_time} UK"

    @property
    def days_label(self) -> str:
        return format_day_selection(self.days)

    @staticmethod
    def time_to_minutes(value: str) -> int:
        hours, minutes = [int(part) for part in value.split(":")]
        return (hours * 60) + minutes

    def selected_days(self) -> list[str]:
        return normalized_day_keys(self.days)

    def covers_minute(self, minute_of_day: int) -> bool:
        start = self.time_to_minutes(self.start_time)
        end = self.time_to_minutes(self.end_time)
        if start < end:
            return start <= minute_of_day < end
        return minute_of_day >= start or minute_of_day < end

    def covers_weekday_minute(self, weekday_index: int, minute_of_day: int) -> bool:
        if weekday_index not in DAY_INDEX_TO_KEY:
            return False
        start = self.time_to_minutes(self.start_time)
        end = self.time_to_minutes(self.end_time)
        current_day_key = DAY_INDEX_TO_KEY[weekday_index]
        if start < end:
            return current_day_key in self.selected_days() and start <= minute_of_day < end
        if minute_of_day >= start:
            return current_day_key in self.selected_days()
        previous_day_key = DAY_INDEX_TO_KEY[(weekday_index - 1) % 7]
        return previous_day_key in self.selected_days() and minute_of_day < end

    def split_ranges(self) -> list[tuple[int, int]]:
        start = self.time_to_minutes(self.start_time)
        end = self.time_to_minutes(self.end_time)
        if start == end:
            return []
        if start < end:
            return [(start, end)]
        return [(start, 1440), (0, end)]

    def weekly_segments(self) -> list[tuple[int, int, int]]:
        start = self.time_to_minutes(self.start_time)
        end = self.time_to_minutes(self.end_time)
        segments: list[tuple[int, int, int]] = []
        if start == end:
            return segments
        for day_key in self.selected_days():
            day_index = DAY_KEY_TO_INDEX[day_key]
            if start < end:
                segments.append((day_index, start, end))
                continue
            segments.append((day_index, start, 1440))
            segments.append(((day_index + 1) % 7, 0, end))
        return segments


def ensure_app_paths() -> None:
    migrate_legacy_app_data()
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)


def migrate_legacy_app_data() -> None:
    if LEGACY_APP_DATA_DIR == APP_DATA_DIR:
        return
    if not LEGACY_APP_DATA_DIR.exists() or not LEGACY_APP_DATA_DIR.is_dir():
        return
    if APP_DATA_DIR.exists() and any(APP_DATA_DIR.iterdir()):
        return
    APP_DATA_DIR.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(LEGACY_APP_DATA_DIR, APP_DATA_DIR, dirs_exist_ok=True)


def load_config() -> dict[str, Any]:
    ensure_app_paths()
    if not CONFIG_PATH.exists():
        return {
            "selected_monitor_ids": [],
            "video_directory": "",
            "screen_aliases": {},
            "transition_method": "fade_black",
            "run_at_startup": False,
            "schedules": [],
        }

    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {
            "selected_monitor_ids": [],
            "video_directory": "",
            "screen_aliases": {},
            "transition_method": "fade_black",
            "run_at_startup": False,
            "schedules": [],
        }

    selected = data.get("selected_monitor_ids")
    schedules = data.get("schedules")
    video_directory = data.get("video_directory")
    screen_aliases = data.get("screen_aliases")
    transition_method = data.get("transition_method")
    run_at_startup = data.get("run_at_startup")
    return {
        "selected_monitor_ids": selected if isinstance(selected, list) else [],
        "video_directory": str(video_directory) if isinstance(video_directory, str) else "",
        "screen_aliases": screen_aliases if isinstance(screen_aliases, dict) else {},
        "transition_method": transition_method if transition_method in TRANSITION_METHODS else "fade_black",
        "run_at_startup": bool(run_at_startup),
        "schedules": schedules if isinstance(schedules, list) else [],
    }


def save_config(
    selected_monitor_ids: list[str],
    video_directory: str,
    screen_aliases: dict[str, str],
    transition_method: str,
    run_at_startup: bool,
    schedules: list[ScheduleEntry],
) -> None:
    ensure_app_paths()
    payload = {
        "selected_monitor_ids": selected_monitor_ids,
        "video_directory": video_directory,
        "screen_aliases": screen_aliases,
        "transition_method": transition_method,
        "run_at_startup": run_at_startup,
        "schedules": [entry.to_dict() for entry in schedules],
    }
    CONFIG_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def write_engine_startup_status(state: str, message: str = "", details: str = "", pid: int | None = None) -> None:
    ensure_app_paths()
    payload = {
        "state": state,
        "message": message,
        "details": details,
        "pid": int(pid if pid is not None else os.getpid()),
        "updatedAt": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        ENGINE_STARTUP_STATUS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        pass


def read_engine_startup_status() -> dict[str, Any]:
    ensure_app_paths()
    if not ENGINE_STARTUP_STATUS_PATH.exists():
        return {}
    try:
        payload = json.loads(ENGINE_STARTUP_STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def reset_engine_startup_artifacts() -> None:
    ensure_app_paths()
    for path in (ENGINE_STARTUP_STATUS_PATH, ENGINE_STARTUP_LOG_PATH):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def append_engine_startup_log(message: str) -> None:
    ensure_app_paths()
    timestamp = datetime.now().isoformat(timespec="seconds")
    try:
        with ENGINE_STARTUP_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"[{timestamp}] {message.rstrip()}\n")
    except OSError:
        pass


def enable_engine_fault_logging() -> None:
    global ENGINE_FAULT_LOG_HANDLE
    ensure_app_paths()
    if ENGINE_FAULT_LOG_HANDLE is None or ENGINE_FAULT_LOG_HANDLE.closed:
        ENGINE_FAULT_LOG_HANDLE = ENGINE_STARTUP_LOG_PATH.open("a", encoding="utf-8")
    faulthandler.enable(file=ENGINE_FAULT_LOG_HANDLE, all_threads=True)


def screen_identifier(screen: QScreen) -> str:
    geometry = screen.geometry()
    return f"{screen.name()}|{geometry.x()}|{geometry.y()}|{geometry.width()}|{geometry.height()}"


def available_screens() -> list[QScreen]:
    return list(QGuiApplication.screens())


def find_screen_by_id(identifier: str) -> QScreen | None:
    for screen in available_screens():
        if screen_identifier(screen) == identifier:
            return screen
    return None


def current_uk_time() -> tuple[str, int, int]:
    now = datetime.now(UK_TZ)
    return now.strftime("%H:%M:%S"), (now.hour * 60) + now.minute, now.weekday()


def current_uk_datetime() -> datetime:
    return datetime.now(UK_TZ)


def active_schedule_for_minute(schedules: list[ScheduleEntry], weekday_index: int, minute_of_day: int) -> ScheduleEntry | None:
    for entry in sorted(schedules, key=lambda item: ScheduleEntry.time_to_minutes(item.start_time)):
        if entry.covers_weekday_minute(weekday_index, minute_of_day):
            return entry
    return None


def schedule_progress(entry: ScheduleEntry, now: datetime) -> float:
    current_minutes = (now.hour * 60) + now.minute + (now.second / 60.0)
    start = ScheduleEntry.time_to_minutes(entry.start_time)
    end = ScheduleEntry.time_to_minutes(entry.end_time)
    if start < end:
        total = max(end - start, 1)
        progressed = min(max(current_minutes - start, 0.0), float(total))
        return progressed / total
    total = max((1440 - start) + end, 1)
    if current_minutes >= start:
        progressed = current_minutes - start
    else:
        progressed = (1440 - start) + current_minutes
    progressed = min(max(progressed, 0.0), float(total))
    return progressed / total


def validate_schedule_candidate(candidate: ScheduleEntry, schedules: list[ScheduleEntry], ignore_id: str | None = None) -> None:
    if not candidate.title.strip():
        raise ValueError("Add a title for the schedule.")
    if candidate.start_time == candidate.end_time:
        raise ValueError("Start time and end time cannot be the same.")
    if not candidate.days:
        raise ValueError("Select at least one day for this schedule.")
    if not candidate.video_file:
        raise ValueError("Choose a media file for this schedule.")

    if Path(candidate.video_file).suffix.lower() not in SUPPORTED_MEDIA_EXTENSIONS:
        raise ValueError("Only supported video and image files can be scheduled.")

    for entry in schedules:
        if entry.id == ignore_id:
            continue
        if any(
            schedule_targets_overlap(candidate.screen_ids, entry.screen_ids)
            and candidate_day == entry_day
            and candidate_start < entry_end and entry_start < candidate_end
            for candidate_day, candidate_start, candidate_end in candidate.weekly_segments()
            for entry_day, entry_start, entry_end in entry.weekly_segments()
        ):
            raise ValueError(f'"{candidate.title}" overlaps with "{entry.title}" on at least one target screen.')


def schedule_targets_overlap(first: list[str], second: list[str]) -> bool:
    if not first or not second:
        return True
    return bool(set(first) & set(second))


def active_schedule_for_screen(schedules: list[ScheduleEntry], weekday_index: int, minute_of_day: int, screen_id: str) -> ScheduleEntry | None:
    for entry in sorted(schedules, key=lambda item: ScheduleEntry.time_to_minutes(item.start_time)):
        if entry.covers_weekday_minute(weekday_index, minute_of_day) and (not entry.screen_ids or screen_id in entry.screen_ids):
            return entry
    return None


def screen_label_from_id(screen_id: str, aliases: dict[str, str]) -> str:
    screen = find_screen_by_id(screen_id)
    if screen is not None:
        return screen_display_name(screen, aliases)
    alias = aliases.get(screen_id, "").strip()
    if alias:
        return alias
    return screen_id.split("|", 1)[0]


def format_screen_targets(screen_ids: list[str], aliases: dict[str, str]) -> str:
    if not screen_ids:
        return "All selected screens"
    return ", ".join(screen_label_from_id(screen_id, aliases) for screen_id in screen_ids)


def disconnected_screen_ids(screen_ids: list[str]) -> list[str]:
    return [screen_id for screen_id in screen_ids if find_screen_by_id(screen_id) is None]

def referenced_video_files(schedules: list[ScheduleEntry]) -> set[str]:
    return {entry.video_file for entry in schedules if entry.video_file}


def remote_screen_digest(state: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(state.get("screen_id") or ""),
        str(state.get("name") or ""),
        str(state.get("ip") or ""),
        int(state.get("width") or 0),
        int(state.get("height") or 0),
        bool(state.get("online")),
        bool(state.get("ready")),
        str(state.get("state") or ""),
        str(state.get("current_media") or ""),
        int(state.get("last_command_version") or 0),
    )


def scan_video_directory(directory: Path | None) -> list[Path]:
    if directory is None or not directory.exists() or not directory.is_dir():
        return []
    return sorted(
        [path for path in directory.rglob("*") if path.is_file() and path.suffix.lower() in SUPPORTED_MEDIA_EXTENSIONS],
        key=lambda path: (
            path.relative_to(directory).parent.as_posix().lower() if directory is not None else "",
            path.name.lower(),
        ),
    )


def media_relative_path(path: Path, directory: Path | None) -> str:
    if directory is None:
        return path.name
    return path.relative_to(directory).as_posix()


def media_category_label(path: Path, directory: Path | None) -> str:
    relative = Path(media_relative_path(path, directory))
    parent = relative.parent.as_posix()
    return parent if parent not in {"", "."} else "Root"


def is_video_file(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS


def is_image_file(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS


def load_pixmap_for_bounds(path: Path, bounds: QSize | None = None) -> QPixmap:
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    if bounds is not None and bounds.width() > 0 and bounds.height() > 0:
        original_size = reader.size()
        if original_size.isValid() and original_size.width() > 0 and original_size.height() > 0:
            scaled = original_size.scaled(bounds, Qt.AspectRatioMode.KeepAspectRatio)
            if scaled.width() > 0 and scaled.height() > 0:
                reader.setScaledSize(scaled)
    image = reader.read()
    if image.isNull():
        return QPixmap()
    return QPixmap.fromImage(image)


def screen_display_name(screen: QScreen, aliases: dict[str, str]) -> str:
    identifier = screen_identifier(screen)
    alias = aliases.get(identifier, "").strip()
    return alias or screen.name()


def windows_startup_script() -> Path:
    appdata = Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    return appdata / "background_screen_controller.vbs"


def legacy_windows_startup_script() -> Path:
    appdata = Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    return appdata / "background_screen_controller.cmd"


def cleanup_legacy_startup_script() -> None:
    if sys.platform != "win32":
        return
    legacy_windows_startup_script().unlink(missing_ok=True)


def preferred_windows_python_gui_executable() -> str:
    if getattr(sys, "frozen", False) or sys.platform != "win32":
        return str(Path(sys.executable).resolve())
    python_executable = Path(sys.executable).resolve()
    pythonw = python_executable.with_name("pythonw.exe")
    if pythonw.exists():
        return str(pythonw)
    return str(python_executable)


def windows_hidden_subprocess_kwargs() -> dict[str, Any]:
    if sys.platform != "win32":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    return {
        "startupinfo": startupinfo,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }


def startup_launch_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"'
    executable = preferred_windows_python_gui_executable() if sys.platform == "win32" else sys.executable
    return f'"{executable}" "{APP_ROOT / "main.py"}"'


def engine_launch_args() -> list[str]:
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve()), "--engine"]
    executable = preferred_windows_python_gui_executable() if sys.platform == "win32" else sys.executable
    return [executable, str(APP_ROOT / "main.py"), "--engine"]


def playback_worker_launch_args(screen_id: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve()), "--playback-worker", "--screen-id", screen_id]
    executable = preferred_windows_python_gui_executable() if sys.platform == "win32" else sys.executable
    return [executable, str(APP_ROOT / "main.py"), "--playback-worker", "--screen-id", screen_id]


def is_remote_screen_id(screen_id: str) -> bool:
    return screen_id.startswith("remote:")


def current_timestamp_ms() -> int:
    return int(time.time() * 1000)


class ControllerInstanceCoordinator(QObject):
    activation_requested = Signal()

    def __init__(self, server_name: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.server_name = server_name
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self._handle_new_connection)
        self._connections: list[QLocalSocket] = []

    def try_notify_existing(self, timeout_ms: int = 600) -> bool:
        socket = QLocalSocket(self)
        socket.connectToServer(self.server_name)
        if not socket.waitForConnected(timeout_ms):
            return False
        socket.write(b"show\n")
        socket.flush()
        socket.waitForBytesWritten(timeout_ms)
        socket.disconnectFromServer()
        return True

    def listen(self) -> bool:
        if self.server.listen(self.server_name):
            return True
        QLocalServer.removeServer(self.server_name)
        return self.server.listen(self.server_name)

    def _handle_new_connection(self) -> None:
        while self.server.hasPendingConnections():
            connection = self.server.nextPendingConnection()
            if connection is None:
                continue
            self._connections.append(connection)
            connection.readyRead.connect(lambda conn=connection: self._handle_ready_read(conn))
            connection.disconnected.connect(lambda conn=connection: self._drop_connection(conn))

    def _handle_ready_read(self, connection: QLocalSocket) -> None:
        payload = bytes(connection.readAll()).decode("utf-8", errors="ignore").strip().lower()
        if payload.startswith("show"):
            self.activation_requested.emit()
        connection.disconnectFromServer()

    def _drop_connection(self, connection: QLocalSocket) -> None:
        if connection in self._connections:
            self._connections.remove(connection)
        connection.deleteLater()


def is_wsl_runtime() -> bool:
    return sys.platform != "win32" and (
        "WSL_INTEROP" in os.environ
        or "WSL_DISTRO_NAME" in os.environ
        or "microsoft" in os.uname().release.lower()
    )


def interface_name_is_virtual(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in VIRTUAL_INTERFACE_HINTS)


def interface_label_priority(name: str) -> int:
    lowered = name.lower()
    if "ethernet" in lowered:
        return 0
    if "wi-fi" in lowered or "wifi" in lowered or "wireless" in lowered or "wlan" in lowered:
        return 1
    return 2


def is_private_ipv4(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_private
    except ValueError:
        return False


def find_lan_interface_details() -> list[tuple[str, str]]:
    details: list[tuple[str, str]] = []
    seen: set[str] = set()

    try:
        for interface in QNetworkInterface.allInterfaces():
            flags = interface.flags()
            if not (flags & QNetworkInterface.InterfaceFlag.IsUp):
                continue
            if not (flags & QNetworkInterface.InterfaceFlag.IsRunning):
                continue
            if flags & QNetworkInterface.InterfaceFlag.IsLoopBack:
                continue
            label = interface.humanReadableName() or interface.name() or "Network Interface"
            if interface_name_is_virtual(label) or interface_name_is_virtual(interface.name()):
                continue
            for entry in interface.addressEntries():
                ip = entry.ip()
                if ip.protocol() != QAbstractSocket.NetworkLayerProtocol.IPv4Protocol:
                    continue
                address = ip.toString().strip()
                if not address or address.startswith("127."):
                    continue
                if address in seen:
                    continue
                seen.add(address)
                details.append((address, label))
    except Exception:
        pass

    if not details:
        details.extend(find_socket_lan_interface_details(existing=set()))

    details.sort(key=lambda item: (interface_label_priority(item[1]), 0 if is_private_ipv4(item[0]) else 1, item[0]))
    return details


def find_windows_lan_interface_details(existing: set[str]) -> list[tuple[str, str]]:
    script = (
        "Get-NetIPConfiguration | "
        "Where-Object { $_.NetAdapter.Status -eq 'Up' -and $_.IPv4Address -ne $null } | "
        "ForEach-Object { "
        "$gateway = if ($_.IPv4DefaultGateway) { $_.IPv4DefaultGateway.NextHop } else { '' }; "
        "$alias = $_.InterfaceAlias; "
        "foreach ($addr in $_.IPv4Address) { Write-Output ($alias + '|' + $addr.IPAddress + '|' + $gateway) } "
        "}"
    )
    results: list[tuple[str, str, bool]] = []
    try:
        command = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            **windows_hidden_subprocess_kwargs(),
        )
        if completed.returncode != 0:
            return []
        for raw_line in completed.stdout.splitlines():
            line = raw_line.strip()
            if not line or "|" not in line:
                continue
            alias, address, gateway = (part.strip() for part in line.split("|", 2))
            if not address or address in existing or address.startswith("127."):
                continue
            if interface_name_is_virtual(alias):
                continue
            results.append((address, alias or "Network Interface", bool(gateway)))
            existing.add(address)
    except OSError:
        return []

    if any(has_gateway for _address, _alias, has_gateway in results):
        results = [item for item in results if item[2]]
    return [(address, alias) for address, alias, _has_gateway in results]


def find_socket_lan_interface_details(existing: set[str]) -> list[tuple[str, str]]:
    details: list[tuple[str, str]] = []
    hostnames = {socket.gethostname(), socket.getfqdn()}
    for host in hostnames:
        try:
            infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
        except OSError:
            continue
        for info in infos:
            address = str(info[4][0])
            if not address or address.startswith("127.") or address in existing:
                continue
            existing.add(address)
            details.append((address, "Host Network"))
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            address = sock.getsockname()[0]
            if address and not address.startswith("127.") and address not in existing:
                details.insert(0, (address, "Default Route"))
    except OSError:
        pass
    return details


def find_lan_ipv4_addresses() -> list[str]:
    return [address for address, _label in find_lan_interface_details()]


def preferred_lan_ip(addresses: list[str]) -> str:
    if not addresses:
        return ""
    for address in addresses:
        if is_private_ipv4(address):
            return address
    return addresses[0]


def local_network_hint_snapshot(port: int = LAN_SERVER_PORT) -> dict[str, Any]:
    details = find_lan_interface_details()
    lan_ips = [address for address, _label in details]
    preferred_ip = preferred_lan_ip(lan_ips)
    hostname = socket.gethostname() or "localhost"
    fqdn = hostname
    preferred_interface = next((label for address, label in details if address == preferred_ip), "")
    mdns_host = f"{hostname}.local" if hostname else ""
    warnings: list[str] = []
    if not lan_ips:
        warnings.append("No LAN IPv4 address was detected on this computer.")
    elif len(lan_ips) > 1:
        warnings.append("Multiple LAN addresses were detected. Make sure the TVs are using the same subnet as the preferred IP shown here.")
    if is_wsl_runtime():
        warnings.append("This app is running inside WSL. For direct LAN reachability, use the Windows build or Windows Python instead of the WSL runtime.")
    return {
        "hostname": hostname,
        "fqdn": fqdn,
        "mdnsHost": mdns_host,
        "port": port,
        "interfaces": [{"ip": address, "label": label} for address, label in details],
        "lan_ips": lan_ips,
        "preferred_ip": preferred_ip,
        "preferred_interface": preferred_interface,
        "preferred_url": f"http://{preferred_ip}:{port}/tv" if preferred_ip else "",
        "hostname_url": f"http://{hostname}:{port}/tv" if hostname else "",
        "mdns_url": f"http://{mdns_host}:{port}/tv" if mdns_host else "",
        "warnings": warnings,
    }


def format_request_error(error: Exception) -> str:
    text = str(error).strip()
    lowered = text.lower()
    if "timed out" in lowered:
        return "The engine took too long to respond. It may be blocked or restarting."
    if isinstance(error, URLError) and getattr(error, "reason", None):
        reason_text = str(error.reason).strip()
        if reason_text:
            return f"Engine request failed: {reason_text}"
    if text:
        return text
    return "The engine request failed."


def friendly_remote_name(remote_id: str, aliases: dict[str, str], state: dict[str, Any] | None = None) -> str:
    alias = aliases.get(remote_id, "").strip()
    if alias:
        return alias
    if state is not None:
        name = str(state.get("name") or "").strip()
        if name:
            return name
    return remote_id.removeprefix("remote:")[:8].upper()


@dataclass
class UnifiedScreenTarget:
    id: str
    label: str
    kind: str
    online: bool
    detail: str = ""
    warning: str = ""


class LanRemoteServer:
    def __init__(self, port: int = LAN_SERVER_PORT) -> None:
        self.port = port
        self.media_root: Path | None = None
        self._lock = threading.RLock()
        self._commands: dict[str, dict[str, Any]] = {}
        self._remote_screens: dict[str, dict[str, Any]] = {}
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._server_error = ""
        self._firewall_warning = ""
        self._firewall_configured = sys.platform != "win32"
        self._firewall_last_check = 0.0
        self._hostname = socket.gethostname() or "localhost"
        self._fqdn = self._hostname
        self._network_snapshot_cache: dict[str, Any] | None = None
        self._network_snapshot_cache_until = 0.0
        self._change_callback = None

    def start(self) -> None:
        if self._httpd is not None:
            return

        server_ref = self

        class _Server(ThreadingHTTPServer):
            allow_reuse_address = True

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                server_ref._handle_get(self)

            def do_POST(self) -> None:  # noqa: N802
                server_ref._handle_post(self)

            def log_message(self, format: str, *args) -> None:  # noqa: A003
                return

        try:
            self._httpd = _Server(("0.0.0.0", self.port), Handler)
        except OSError as error:
            self._server_error = str(error)
            self._httpd = None
            return

        self._thread = threading.Thread(target=self._httpd.serve_forever, name="lan-remote-server", daemon=True)
        self._thread.start()
        self.ensure_firewall_rule(force_retry=True)

    def stop(self) -> None:
        httpd = self._httpd
        self._httpd = None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None

    def set_media_root(self, media_root: Path | None) -> None:
        with self._lock:
            self.media_root = media_root

    def set_commands(self, commands: dict[str, dict[str, Any]]) -> None:
        with self._lock:
            self._commands = json.loads(json.dumps(commands))

    def set_change_callback(self, callback) -> None:
        self._change_callback = callback

    def _notify_change(self) -> None:
        callback = self._change_callback
        if callback is not None:
            try:
                callback()
            except Exception:
                return

    def _resolve_remote_screen_id(self, payload: dict[str, Any], client_ip: str) -> str:
        provided_id = str(payload.get("screenId") or "").strip()
        if provided_id.startswith("remote:"):
            return provided_id

        requested_name = str(payload.get("name") or "").strip().lower()
        requested_agent = str(payload.get("userAgent") or "").strip()
        ip_matches: list[str] = []
        for screen_id, state in self._remote_screens.items():
            state_name = str(state.get("name") or "").strip().lower()
            state_ip = str(state.get("ip") or "").strip()
            state_agent = str(state.get("user_agent") or "").strip()
            if state_ip == client_ip:
                ip_matches.append(screen_id)
            if requested_name and state_name == requested_name and state_ip == client_ip:
                return screen_id
            if state_ip == client_ip and requested_agent and state_agent == requested_agent:
                return screen_id
        if len(ip_matches) == 1:
            return ip_matches[0]
        identity_seed = requested_name or requested_agent or "remote-screen"
        stable_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{client_ip}|{identity_seed}")
        return f"remote:{stable_id.hex}"

    def register_or_refresh(self, payload: dict[str, Any], client_ip: str) -> dict[str, Any]:
        screen_id = self._resolve_remote_screen_id(payload, client_ip)
        name = str(payload.get("name") or "").strip() or f"Remote Screen {len(self._remote_screens) + 1}"
        state = self._remote_screens.get(screen_id, {})
        now = time.time()
        state.update(
            {
                "screen_id": screen_id,
                "name": name,
                "user_agent": str(payload.get("userAgent") or ""),
                "ip": client_ip,
                "width": int(payload.get("width") or 0),
                "height": int(payload.get("height") or 0),
                "online": True,
                "ready": False,
                "state": "connected",
                "current_media": "",
                "drift_ms": None,
                "last_seen": now,
                "last_command_version": int(payload.get("lastCommandVersion") or 0),
            }
        )
        self._remote_screens[screen_id] = state
        return {
            "screenId": screen_id,
            "name": name,
            "pollIntervalMs": REMOTE_POLL_INTERVAL_MS,
            "heartbeatIntervalMs": REMOTE_HEARTBEAT_INTERVAL_MS,
            "serverTimeMs": current_timestamp_ms(),
            "hostname": self._hostname,
        }

    def update_heartbeat(self, payload: dict[str, Any], client_ip: str) -> dict[str, Any]:
        screen_id = str(payload.get("screenId") or "").strip()
        if not screen_id.startswith("remote:"):
            return {"ok": False, "error": "invalid_screen_id"}
        now = time.time()
        state = self._remote_screens.get(screen_id, {"screen_id": screen_id, "name": screen_id.removeprefix("remote:")[:8].upper()})
        state.update(
            {
                "ip": client_ip,
                "online": True,
                "ready": bool(payload.get("ready")),
                "state": str(payload.get("state") or "connected"),
                "current_media": str(payload.get("currentMedia") or ""),
                "drift_ms": payload.get("driftMs"),
                "width": int(payload.get("width") or state.get("width") or 0),
                "height": int(payload.get("height") or state.get("height") or 0),
                "last_seen": now,
                "last_command_version": int(payload.get("lastCommandVersion") or state.get("last_command_version") or 0),
            }
        )
        self._remote_screens[screen_id] = state
        return {"ok": True, "serverTimeMs": current_timestamp_ms()}

    def remote_screens_snapshot(self) -> list[dict[str, Any]]:
        now = time.time()
        with self._lock:
            for state in self._remote_screens.values():
                state["online"] = (now - float(state.get("last_seen") or 0.0)) <= REMOTE_SCREEN_TIMEOUT_SECONDS
            return [dict(item) for item in sorted(self._remote_screens.values(), key=lambda row: str(row.get("name") or row.get("screen_id") or "").lower())]

    def command_for_screen(self, screen_id: str) -> dict[str, Any]:
        with self._lock:
            direct = self._commands.get(screen_id)
            if direct is not None:
                return dict(direct)
            online_screen_ids = [
                remote_id
                for remote_id, state in self._remote_screens.items()
                if bool(state.get("online"))
            ]
            active_commands = [
                dict(command)
                for command in self._commands.values()
                if str(command.get("type") or "") == "play" and is_remote_screen_id(str(command.get("screenId") or ""))
            ]
            if len(online_screen_ids) == 1 and online_screen_ids[0] == screen_id and len(active_commands) == 1:
                fallback = active_commands[0]
                fallback["screenId"] = screen_id
                return fallback
            return {"version": 0, "mode": "idle", "type": "clear", "message": ""}

    def invalidate_network_snapshot(self) -> None:
        with self._lock:
            self._network_snapshot_cache = None
            self._network_snapshot_cache_until = 0.0

    def network_snapshot(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            if self._network_snapshot_cache is not None and now < self._network_snapshot_cache_until:
                return dict(self._network_snapshot_cache)

        snapshot = local_network_hint_snapshot(self.port)
        warnings = list(snapshot.get("warnings") or [])
        if self._firewall_warning:
            warnings.append(self._firewall_warning)
        if self._server_error:
            warnings.insert(0, f"Server bind failed: {self._server_error}")
        snapshot["hostname"] = self._hostname
        snapshot["fqdn"] = self._fqdn
        snapshot["firewallConfigured"] = self._firewall_configured
        snapshot["hostname_url"] = f"http://{self._hostname}:{self.port}/tv" if self._hostname else ""
        snapshot["mdnsHost"] = f"{self._hostname}.local" if self._hostname else ""
        snapshot["mdns_url"] = f"http://{self._hostname}.local:{self.port}/tv" if self._hostname else ""
        snapshot["warnings"] = warnings
        with self._lock:
            self._network_snapshot_cache = dict(snapshot)
            self._network_snapshot_cache_until = now + NETWORK_SNAPSHOT_CACHE_SECONDS
        return snapshot

    def ensure_firewall_rule(self, force_retry: bool = False) -> bool:
        if sys.platform != "win32":
            self._firewall_configured = True
            self._firewall_warning = ""
            return True
        now = time.time()
        if not force_retry and now - self._firewall_last_check < 30.0:
            return self._firewall_configured
        self._firewall_last_check = now
        executable = Path(sys.executable if getattr(sys, "frozen", False) else sys.executable).resolve()
        rule_name = f"{APP_NAME} LAN Server"
        try:
            show_rule = subprocess.run(
                ["netsh", "advfirewall", "firewall", "show", "rule", f"name={rule_name}"],
                capture_output=True,
                text=True,
                check=False,
                **windows_hidden_subprocess_kwargs(),
            )
            if show_rule.returncode == 0 and "No rules match" not in (show_rule.stdout or ""):
                self._firewall_configured = True
                self._firewall_warning = ""
                self.invalidate_network_snapshot()
                return True
            add_rule = subprocess.run(
                [
                    "netsh",
                    "advfirewall",
                    "firewall",
                    "add",
                    "rule",
                    f"name={rule_name}",
                    "dir=in",
                    "action=allow",
                    "profile=private",
                    "protocol=TCP",
                    f"localport={self.port}",
                    f'program={executable}',
                    "enable=yes",
                ],
                capture_output=True,
                text=True,
                check=False,
                **windows_hidden_subprocess_kwargs(),
            )
            if add_rule.returncode != 0:
                self._firewall_configured = False
                self._firewall_warning = "Windows firewall rule could not be created automatically. Run the installer or the app as administrator."
            else:
                self._firewall_configured = True
                self._firewall_warning = ""
        except OSError:
            self._firewall_configured = False
            self._firewall_warning = "Windows firewall could not be configured automatically on this machine."
        self.invalidate_network_snapshot()
        return self._firewall_configured

    def _read_json(self, handler: BaseHTTPRequestHandler) -> dict[str, Any]:
        length = int(handler.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = handler.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_json(self, handler: BaseHTTPRequestHandler, payload: dict[str, Any], status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        try:
            handler.send_response(int(status))
            handler.send_header("Content-Type", "application/json; charset=utf-8")
            handler.send_header("Content-Length", str(len(body)))
            handler.send_header("Cache-Control", "no-store")
            handler.end_headers()
            handler.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            return

    def _write_text(self, handler: BaseHTTPRequestHandler, body: str, content_type: str = "text/html; charset=utf-8", status: int = HTTPStatus.OK) -> None:
        encoded = body.encode("utf-8")
        handler.send_response(int(status))
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(encoded)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(encoded)

    def _handle_get(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urlparse(handler.path)
        try:
            if parsed.path == "/":
                info = self.network_snapshot()
                body = (
                    "<html><head><title>Background Screen Controller</title></head>"
                    "<body style='font-family:sans-serif;background:#0f1720;color:#f5fbff;padding:32px;'>"
                    f"<h1>{escape(APP_NAME)}</h1>"
                    "<p>This controller is running on the local network.</p>"
                    f"<p>TV client: <a style='color:#84dcc6' href='/tv'>/tv</a></p>"
                    f"<p>Preferred URL: {escape(info.get('preferred_url') or 'Unavailable')}</p>"
                    f"<p>Hostname URL: {escape(info.get('hostname_url') or 'Unavailable')}</p>"
                    "</body></html>"
                )
                self._write_text(handler, body)
                return
            if parsed.path == "/tv":
                self._write_text(handler, self._remote_client_html())
                return
            if parsed.path == "/api/controller-info":
                self._write_json(
                    handler,
                    {
                        "network": self.network_snapshot(),
                        "remoteScreens": self.remote_screens_snapshot(),
                        "serverTimeMs": current_timestamp_ms(),
                    },
                )
                return
            if parsed.path == "/api/remote/poll":
                query = parse_qs(parsed.query)
                screen_id = str((query.get("screenId") or [""])[0])
                payload = {
                    "serverTimeMs": current_timestamp_ms(),
                    "command": self.command_for_screen(screen_id),
                }
                self._write_json(handler, payload)
                return
            if parsed.path == "/api/media-file":
                query = parse_qs(parsed.query)
                absolute_path = str((query.get("path") or [""])[0]).strip()
                self._serve_absolute_media(handler, absolute_path)
                return
            if parsed.path.startswith("/media/"):
                self._serve_media(handler, parsed.path.removeprefix("/media/"))
                return
            self._write_text(handler, "Not found", "text/plain; charset=utf-8", HTTPStatus.NOT_FOUND)
        except Exception as error:  # noqa: BLE001
            self._write_json(handler, {"ok": False, "error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_post(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urlparse(handler.path)
        payload = self._read_json(handler)
        client_ip = handler.client_address[0] if handler.client_address else ""
        if parsed.path == "/api/remote/register":
            with self._lock:
                response = self.register_or_refresh(payload, client_ip)
            self._notify_change()
            self._write_json(handler, response)
            return
        if parsed.path == "/api/remote/heartbeat":
            with self._lock:
                response = self.update_heartbeat(payload, client_ip)
            self._notify_change()
            self._write_json(handler, response)
            return
        self._write_text(handler, "Not found", "text/plain; charset=utf-8", HTTPStatus.NOT_FOUND)

    def _serve_media(self, handler: BaseHTTPRequestHandler, relative_path: str) -> None:
        with self._lock:
            media_root = self.media_root
        if media_root is None:
            self._write_text(handler, "No media folder configured", "text/plain; charset=utf-8", HTTPStatus.NOT_FOUND)
            return
        decoded = Path(unquote(relative_path))
        target = (media_root / decoded).resolve()
        try:
            target.relative_to(media_root.resolve())
        except ValueError:
            self._write_text(handler, "Forbidden", "text/plain; charset=utf-8", HTTPStatus.FORBIDDEN)
            return
        if not target.exists() or not target.is_file():
            self._write_text(handler, "Not found", "text/plain; charset=utf-8", HTTPStatus.NOT_FOUND)
            return
        self._serve_file(handler, target, cache_control="public, max-age=60")

    def _serve_absolute_media(self, handler: BaseHTTPRequestHandler, absolute_path: str) -> None:
        target = Path(unquote(absolute_path)).expanduser()
        if not target.exists() or not target.is_file() or target.suffix.lower() not in SUPPORTED_MEDIA_EXTENSIONS:
            self._write_text(handler, "Not found", "text/plain; charset=utf-8", HTTPStatus.NOT_FOUND)
            return
        self._serve_file(handler, target, cache_control="public, max-age=30")

    def _serve_file(self, handler: BaseHTTPRequestHandler, target: Path, cache_control: str) -> None:
        content_type, _encoding = mimetypes.guess_type(str(target))
        try:
            stat = target.stat()
        except OSError:
            self._write_text(handler, "Unable to read media", "text/plain; charset=utf-8", HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        total_size = int(stat.st_size)
        start = 0
        end = total_size - 1
        status = HTTPStatus.OK
        range_header = str(handler.headers.get("Range") or "").strip()
        if range_header.startswith("bytes=") and total_size > 0:
            requested = range_header.removeprefix("bytes=").split(",", 1)[0].strip()
            start_text, _separator, end_text = requested.partition("-")
            try:
                if start_text:
                    start = max(0, int(start_text))
                    end = min(total_size - 1, int(end_text)) if end_text else total_size - 1
                elif end_text:
                    suffix_length = max(0, int(end_text))
                    start = max(0, total_size - suffix_length)
                    end = total_size - 1
                else:
                    raise ValueError
                if start > end or start >= total_size:
                    raise ValueError
                status = HTTPStatus.PARTIAL_CONTENT
            except ValueError:
                handler.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                handler.send_header("Content-Range", f"bytes */{total_size}")
                handler.send_header("Accept-Ranges", "bytes")
                handler.end_headers()
                return

        content_length = max(0, end - start + 1)
        try:
            file_handle = target.open("rb")
        except OSError:
            self._write_text(handler, "Unable to read media", "text/plain; charset=utf-8", HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        handler.send_response(status)
        handler.send_header("Content-Type", content_type or "application/octet-stream")
        handler.send_header("Content-Length", str(content_length))
        handler.send_header("Cache-Control", cache_control)
        handler.send_header("Accept-Ranges", "bytes")
        if status == HTTPStatus.PARTIAL_CONTENT:
            handler.send_header("Content-Range", f"bytes {start}-{end}/{total_size}")
        handler.end_headers()

        remaining = content_length
        with file_handle:
            if start:
                file_handle.seek(start)
            while remaining > 0:
                chunk = file_handle.read(min(1024 * 64, remaining))
                if not chunk:
                    break
                handler.wfile.write(chunk)
                remaining -= len(chunk)

    def _remote_client_html(self) -> str:
        html = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Remote Screen</title>
  <style>
    html, body { margin: 0; width: 100%; height: 100%; overflow: hidden; background: #000; color: #eef6ff; font-family: Arial, sans-serif; }
    #stage, video, img { position: absolute; inset: 0; width: 100%; height: 100%; background: #000; }
    video, img { object-fit: contain; }
    #videoB, #imageLayer, #message, #overlay, #statusBar { transition: opacity 320ms ease; }
    #videoB, #imageLayer { opacity: 0; }
    #overlay { background: #000; opacity: 0; pointer-events: none; }
    #message { display: flex; align-items: center; justify-content: center; text-align: center; padding: 40px; color: rgba(240,246,255,0.82); }
    #statusBar { position: absolute; right: 18px; bottom: 18px; background: rgba(0,0,0,0.48); border: 1px solid rgba(255,255,255,0.14); border-radius: 999px; padding: 8px 14px; font-size: 14px; opacity: 0.9; }
  </style>
</head>
<body>
  <div id="stage">
    <video id="videoA" muted playsinline preload="metadata"></video>
    <video id="videoB" muted playsinline preload="metadata"></video>
    <img id="imageLayer" alt="" />
    <div id="message">Waiting for controller…</div>
    <div id="overlay"></div>
    <div id="statusBar">Connecting…</div>
  </div>
  <script>
    (() => {
      const POLL_MS = __POLL_MS__;
      const HEARTBEAT_MS = __HEARTBEAT_MS__;
      const storeKey = 'backgroundScreenRemote';
      const overlay = document.getElementById('overlay');
      const message = document.getElementById('message');
      const statusBar = document.getElementById('statusBar');
      const imageLayer = document.getElementById('imageLayer');
      let activeVideo = document.getElementById('videoA');
      let stagingVideo = document.getElementById('videoB');
      let remoteState = {};
      let currentVersion = 0;
      let activeMedia = '';
      let currentKind = '';
      let activeSourceUrl = '';
      let activeCommand = null;
      let serverOffset = 0;
      let pendingTimer = null;
      let lastVideoProgressAt = Date.now();
      let lastVideoPosition = 0;
      let videoRecoveryCount = 0;

      function loadState() {
        try { remoteState = JSON.parse(localStorage.getItem(storeKey) || '{}') || {}; } catch (_err) { remoteState = {}; }
      }
      function saveState() {
        localStorage.setItem(storeKey, JSON.stringify(remoteState));
      }
      function setStatus(text) {
        statusBar.textContent = text;
      }
      function nowServerMs() {
        return Date.now() + serverOffset;
      }
      async function postJson(url, payload) {
        const response = await fetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
          cache: 'no-store'
        });
        return response.json();
      }
      async function register() {
        if (!remoteState.name) {
          const given = window.prompt('Name this screen', remoteState.name || '');
          remoteState.name = (given || '').trim() || 'Remote Screen';
        }
        const payload = {
          screenId: remoteState.screenId || '',
          name: remoteState.name,
          userAgent: navigator.userAgent,
          width: window.innerWidth,
          height: window.innerHeight,
          lastCommandVersion: currentVersion
        };
        const result = await postJson('/api/remote/register', payload);
        remoteState.screenId = result.screenId;
        remoteState.name = result.name || remoteState.name;
        saveState();
        serverOffset = Number(result.serverTimeMs || Date.now()) - Date.now();
        setStatus(remoteState.name + ' connected');
      }
      function clearPending() {
        if (pendingTimer) {
          clearTimeout(pendingTimer);
          pendingTimer = null;
        }
      }
      function markVideoProgress() {
        lastVideoProgressAt = Date.now();
        lastVideoPosition = Number(activeVideo.currentTime || 0);
        videoRecoveryCount = 0;
      }
      function fadeToBlack(show) {
        overlay.style.opacity = show ? '1' : '0';
      }
      function stopVideos() {
        for (const video of [activeVideo, stagingVideo]) {
          video.pause();
          video.removeAttribute('src');
          video.load();
          video.style.opacity = '0';
        }
      }
      function swapVideos() {
        const prev = activeVideo;
        activeVideo = stagingVideo;
        stagingVideo = prev;
      }
      function applyClear(command) {
        clearPending();
        const run = () => {
          fadeToBlack(true);
          stopVideos();
          imageLayer.style.opacity = '0';
          imageLayer.removeAttribute('src');
          currentKind = '';
          activeMedia = '';
          activeSourceUrl = '';
          activeCommand = command;
          message.textContent = command.message || 'No media scheduled for the current UK time.';
          message.style.opacity = '1';
          setStatus((remoteState.name || 'Remote Screen') + ' idle');
        };
        const delay = Math.max(0, Number(command.playAtMs || 0) - nowServerMs());
        pendingTimer = setTimeout(run, delay);
      }
      function scheduleImage(command) {
        clearPending();
        const loader = new Image();
        loader.onload = () => {
          const run = () => {
            fadeToBlack(command.transition !== 'cut');
            stopVideos();
            imageLayer.src = command.mediaUrl;
            imageLayer.style.opacity = '1';
            currentKind = 'image';
            activeMedia = command.label || command.relativePath || '';
            activeSourceUrl = command.mediaUrl || '';
            activeCommand = command;
            message.style.opacity = '0';
            setStatus((remoteState.name || 'Remote Screen') + ' showing image');
            setTimeout(() => fadeToBlack(false), command.transition === 'cut' ? 20 : 220);
          };
          const delay = Math.max(0, Number(command.playAtMs || 0) - nowServerMs());
          pendingTimer = setTimeout(run, delay);
        };
        loader.onerror = () => applyClear({ message: 'Unable to load image.' });
        loader.src = command.mediaUrl;
      }
      function scheduleVideo(command) {
        clearPending();
        stagingVideo.pause();
        stagingVideo.src = command.mediaUrl;
        stagingVideo.currentTime = 0;
        stagingVideo.load();
        const ready = () => {
          stagingVideo.removeEventListener('canplay', ready);
          const run = async () => {
            fadeToBlack(command.transition !== 'cut');
            imageLayer.style.opacity = '0';
            imageLayer.removeAttribute('src');
            stagingVideo.style.opacity = '1';
            activeVideo.style.opacity = '0';
            swapVideos();
            try {
              if (command.paused) {
                activeVideo.pause();
              } else {
                await activeVideo.play();
              }
            } catch (_err) {}
            if (stagingVideo !== activeVideo) {
              stagingVideo.pause();
              stagingVideo.removeAttribute('src');
              stagingVideo.load();
            }
            currentKind = 'video';
            activeMedia = command.label || command.relativePath || '';
            activeSourceUrl = command.mediaUrl || '';
            activeCommand = command;
            lastVideoProgressAt = Date.now();
            lastVideoPosition = Number(activeVideo.currentTime || 0);
            videoRecoveryCount = 0;
            message.style.opacity = '0';
            setStatus((remoteState.name || 'Remote Screen') + (command.paused ? ' paused' : ' playing video'));
            setTimeout(() => fadeToBlack(false), command.transition === 'cut' ? 20 : 220);
          };
          const delay = Math.max(0, Number(command.playAtMs || 0) - nowServerMs());
          pendingTimer = setTimeout(run, delay);
        };
        stagingVideo.addEventListener('canplay', ready, { once: true });
      }
      async function applyCommand(command) {
        if (!command || Number(command.version || 0) === currentVersion) return;
        currentVersion = Number(command.version || 0);
        if (!command.mediaUrl || command.type === 'clear') {
          applyClear(command);
          return;
        }
        if (command.mediaKind === 'video' && currentKind === 'video' && activeSourceUrl === (command.mediaUrl || '')) {
          clearPending();
          if (command.paused) {
            activeVideo.pause();
            setStatus((remoteState.name || 'Remote Screen') + ' paused');
          } else {
            try { await activeVideo.play(); } catch (_err) {}
            setStatus((remoteState.name || 'Remote Screen') + ' playing video');
          }
          return;
        }
        if (command.mediaKind === 'image' && currentKind === 'image' && activeSourceUrl === (command.mediaUrl || '')) {
          clearPending();
          setStatus((remoteState.name || 'Remote Screen') + ' showing image');
          return;
        }
        if (command.mediaKind === 'image') {
          scheduleImage(command);
          return;
        }
        scheduleVideo(command);
      }
      async function recoverRemoteVideoPlayback() {
        if (currentKind !== 'video' || !activeCommand || activeCommand.paused) return;
        const now = Date.now();
        const currentPosition = Number(activeVideo.currentTime || 0);
        if (currentPosition > lastVideoPosition + 0.15) {
          lastVideoProgressAt = now;
          lastVideoPosition = currentPosition;
          videoRecoveryCount = 0;
          return;
        }
        if (now - lastVideoProgressAt < 6000) return;
        videoRecoveryCount += 1;
        setStatus((remoteState.name || 'Remote Screen') + ' recovering video…');
        try {
          if (videoRecoveryCount <= 2) {
            await activeVideo.play();
          } else {
            const replayCommand = Object.assign({}, activeCommand, { version: currentVersion + 1 });
            currentVersion = Number(currentVersion || 0);
            scheduleVideo(replayCommand);
          }
        } catch (_err) {
        }
        lastVideoProgressAt = now;
        lastVideoPosition = Number(activeVideo.currentTime || 0);
      }
      async function poll() {
        if (!remoteState.screenId) return;
        const response = await fetch('/api/remote/poll?screenId=' + encodeURIComponent(remoteState.screenId), { cache: 'no-store' });
        const data = await response.json();
        serverOffset = Number(data.serverTimeMs || Date.now()) - Date.now();
        await applyCommand(data.command || {});
      }
      async function heartbeat() {
        if (!remoteState.screenId) return;
        await postJson('/api/remote/heartbeat', {
          screenId: remoteState.screenId,
          ready: currentKind !== '',
          state: currentKind ? 'playing' : 'idle',
          currentMedia: activeMedia,
          driftMs: 0,
          width: window.innerWidth,
          height: window.innerHeight,
          lastCommandVersion: currentVersion
        });
      }
      for (const video of [document.getElementById('videoA'), document.getElementById('videoB')]) {
        video.loop = true;
        video.muted = true;
        video.playsInline = true;
        video.addEventListener('timeupdate', markVideoProgress);
        video.addEventListener('playing', markVideoProgress);
        video.addEventListener('seeked', markVideoProgress);
      }
      loadState();
      register()
        .then(() => {
          poll();
          heartbeat();
          setInterval(() => { poll().catch((_err) => setStatus('Waiting for controller…')); }, POLL_MS);
          setInterval(() => { heartbeat().catch(() => {}); }, HEARTBEAT_MS);
          setInterval(() => { recoverRemoteVideoPlayback().catch(() => {}); }, 2500);
        })
        .catch(() => {
          message.textContent = 'Unable to connect to the controller.';
          setStatus('Connection failed');
        });
    })();
  </script>
</body>
</html>
"""
        return html.replace("__POLL_MS__", str(REMOTE_POLL_INTERVAL_MS)).replace("__HEARTBEAT_MS__", str(REMOTE_HEARTBEAT_INTERVAL_MS))


class PlaybackWindow(QWidget):
    pause_requested = Signal()

    def __init__(self, screen: QScreen, title: str) -> None:
        super().__init__()
        self.screen_ref = screen
        self.screen_id = screen_identifier(screen)
        self.current_entry_id: str | None = None
        self.pending_entry: ScheduleEntry | None = None
        self.pending_entry_path: Path | None = None
        self.current_source_key: str | None = None
        self.current_source_signature: tuple[int, int] | None = None
        self.pending_source_key: str | None = None
        self.pending_source_signature: tuple[int, int] | None = None
        self.is_paused = False
        self.transition_method = "fade_black"
        self.controls_hide_timer = QTimer(self)
        self.controls_hide_timer.setInterval(CONTROL_HIDE_DELAY_MS)
        self.controls_hide_timer.setSingleShot(True)
        self.controls_hide_timer.timeout.connect(self.fade_controls_out)
        self.scheduled_action_timer = QTimer(self)
        self.scheduled_action_timer.setSingleShot(True)
        self.scheduled_action_timer.timeout.connect(self.run_scheduled_action)
        self.playback_watchdog_timer = QTimer(self)
        self.playback_watchdog_timer.setInterval(3000)
        self.playback_watchdog_timer.timeout.connect(self.check_playback_health)
        self.playback_watchdog_timer.start()
        self._scheduled_action = None
        self.transition_animation: QPropertyAnimation | None = None
        self.controls_animation: QPropertyAnimation | None = None
        self.transition_callback = None
        self._is_closing = False
        self.current_image_path: Path | None = None
        self.current_image_pixmap: QPixmap | None = None
        self.current_media_kind = ""
        self._ready_source_key: str | None = None
        self.current_video_path: Path | None = None
        self._last_player_position = -1
        self._last_progress_monotonic = time.monotonic()
        self._stall_recovery_count = 0

        self.setWindowTitle(title)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("background: #000;")
        self.setMouseTracking(True)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)

        self.player = QMediaPlayer(self)
        self.player.setLoops(QMediaPlayer.Loops.Infinite)
        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(0.0)
        self.player.setAudioOutput(self.audio_output)
        self.player.mediaStatusChanged.connect(self.on_media_status_changed)
        self.player.errorOccurred.connect(self.on_media_error)
        self.player.positionChanged.connect(self.on_position_changed)

        self.video_widget = QVideoWidget(self)
        self.player.setVideoOutput(self.video_widget)
        self.image_label = QLabel(self)
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setStyleSheet("background: #000;")
        self.image_label.hide()

        self.message_label = QLabel("No media scheduled for the current UK time.", self)
        self.message_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message_label.setWordWrap(True)
        self.message_label.setStyleSheet(
            "color: rgba(244,247,251,0.72); font-size: 20px; padding: 24px;"
        )

        self.black_overlay = QFrame(self)
        self.black_overlay.setStyleSheet("background: #000;")
        self.black_overlay_effect = QGraphicsOpacityEffect(self.black_overlay)
        self.black_overlay.setGraphicsEffect(self.black_overlay_effect)
        self.black_overlay_effect.setOpacity(0.0)

        self.controls_container = QWidget(self)
        self.controls_container.setStyleSheet(
            """
            QWidget {
                background: rgba(0, 0, 0, 0.46);
                border: 1px solid rgba(255,255,255,0.14);
                border-radius: 999px;
            }
            QPushButton {
                background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #84dcc6, stop:1 #5db7f0);
                color: #04111b;
                border: none;
                border-radius: 999px;
                padding: 10px 18px;
                font-weight: 600;
            }
            """
        )
        self.controls_effect = QGraphicsOpacityEffect(self.controls_container)
        self.controls_container.setGraphicsEffect(self.controls_effect)
        self.controls_effect.setOpacity(1.0)

        self.pause_button = QPushButton("Pause", self.controls_container)
        self.pause_button.clicked.connect(self.pause_requested.emit)

        controls_layout = QHBoxLayout(self.controls_container)
        controls_layout.setContentsMargins(12, 12, 12, 12)
        controls_layout.addWidget(self.pause_button)

        self.message_label.raise_()

    def show_on_screen(self) -> None:
        geometry = self.screen_ref.geometry()
        self.setGeometry(geometry)
        self.move(geometry.topLeft())
        self.showFullScreen()
        self.raise_()
        self.position_overlays()
        self.show_controls()
        self.hide_controls_later()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self.position_overlays()

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        self.show_controls()
        self.hide_controls_later()
        super().mouseMoveEvent(event)

    def position_overlays(self) -> None:
        self.video_widget.setGeometry(self.rect())
        self.image_label.setGeometry(self.rect())
        self.black_overlay.setGeometry(self.rect())
        self.message_label.setGeometry(self.rect())
        self.update_image_display()
        container_width = self.controls_container.sizeHint().width()
        container_height = self.controls_container.sizeHint().height()
        self.controls_container.setGeometry(
            self.width() - container_width - 24,
            self.height() - container_height - 24,
            container_width,
            container_height,
        )
        self.controls_container.raise_()

    def set_paused(self, paused: bool) -> None:
        if not self.is_runtime_alive():
            return
        self.is_paused = paused
        self.pause_button.setText("Play" if paused else "Pause")
        self.pause_button.setEnabled(self.current_media_kind == "video")
        if paused:
            self.player.pause()
        elif self.current_entry_id is not None:
            if self.current_media_kind == "video":
                self.player.play()

    def set_transition_method(self, method: str) -> None:
        self.transition_method = method if method in TRANSITION_METHODS else "fade_black"

    def show_controls(self) -> None:
        if not self.is_runtime_alive():
            return
        self.controls_container.show()
        self.animate_opacity(self.controls_effect, 1.0, 180)
        self.controls_hide_timer.stop()

    def hide_controls_later(self) -> None:
        self.controls_hide_timer.start()

    def fade_controls_out(self) -> None:
        if not self.is_runtime_alive():
            return
        self.animate_opacity(self.controls_effect, 0.0, CONTROL_FADE_DURATION_MS)

    def animate_opacity(self, effect: QGraphicsOpacityEffect, target: float, duration: int, on_finished=None) -> None:
        if not self.is_runtime_alive():
            return
        animation = QPropertyAnimation(effect, b"opacity", self)
        animation.setDuration(duration)
        animation.setStartValue(effect.opacity())
        animation.setEndValue(target)
        animation.setEasingCurve(QEasingCurve.Type.InOutQuad)
        if on_finished is not None:
            animation.finished.connect(on_finished)
        animation.start()
        if effect is self.black_overlay_effect:
            self.transition_animation = animation
        else:
            self.controls_animation = animation

    def fade_black_to(self, target: float, duration: int, callback=None) -> None:
        if not self.is_runtime_alive():
            return
        self.transition_callback = callback

        def done() -> None:
            if self.transition_callback is not None:
                callback_ref = self.transition_callback
                self.transition_callback = None
                callback_ref()

        self.animate_opacity(self.black_overlay_effect, target, duration, done if callback else None)

    def clear_playback(self, message: str, show_message: bool = True) -> None:
        if not self.is_runtime_alive():
            return
        self.current_entry_id = None
        self.current_source_key = None
        self.current_source_signature = None
        self.pending_entry = None
        self.pending_entry_path = None
        self.pending_source_key = None
        self.pending_source_signature = None
        self.current_image_path = None
        self.current_image_pixmap = None
        self.current_media_kind = ""
        self._ready_source_key = None
        self.current_video_path = None
        self._last_player_position = -1
        self._last_progress_monotonic = time.monotonic()
        self._stall_recovery_count = 0
        self.player.stop()
        self.video_widget.hide()
        self.image_label.hide()
        self.pause_button.setEnabled(False)
        self.message_label.setText(message)
        if show_message and message:
            self.message_label.show()
        else:
            self.message_label.hide()
        self.fade_black_to(1.0, TRANSITION_TO_BLACK_MS)

    def clear_playback_at(self, message: str, play_at_ms: int | None = None, show_message: bool = True) -> None:
        self.schedule_action(lambda: self.clear_playback(message, show_message=show_message), play_at_ms)

    def is_runtime_alive(self) -> bool:
        return not self._is_closing and isValid(self) and hasattr(self, "player") and isValid(self.player)

    def schedule_action(self, callback, play_at_ms: int | None = None) -> None:
        if play_at_ms is None or play_at_ms <= current_timestamp_ms() + 25:
            self.scheduled_action_timer.stop()
            self._scheduled_action = None
            callback()
            return
        self._scheduled_action = callback
        self.scheduled_action_timer.start(max(0, play_at_ms - current_timestamp_ms()))

    def run_scheduled_action(self) -> None:
        callback = self._scheduled_action
        self._scheduled_action = None
        if callback is not None:
            callback()

    def source_signature(self, path: Path) -> tuple[int, int] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return (int(stat.st_mtime_ns), int(stat.st_size))

    def switch_to_source(self, source_key: str, path: Path, display_label: str, entry_id: str | None, force_reload: bool = False) -> None:
        if not self.is_runtime_alive():
            return
        source_signature = self.source_signature(path)
        if source_signature is None or not path.exists():
            self.clear_playback(f"Missing media file:\n{display_label}")
            return

        if not force_reload and self.current_source_key == source_key and self.current_source_signature == source_signature:
            return

        self.pending_entry = None
        self.pending_entry_path = path
        self.pending_source_key = source_key
        self.pending_source_signature = source_signature
        self.current_entry_id = entry_id

        if self.current_source_key is None:
            if self.transition_method == "cut":
                self.black_overlay_effect.setOpacity(0.0)
                self.apply_pending_entry()
            else:
                target = 0.55 if self.transition_method == "soft_fade" else 1.0
                self.black_overlay_effect.setOpacity(target)
                self.apply_pending_entry()
            return

        if self.transition_method == "cut":
            self.black_overlay_effect.setOpacity(0.0)
            self.apply_pending_entry()
            return

        target = 0.55 if self.transition_method == "soft_fade" else 1.0
        duration = 220 if self.transition_method == "soft_fade" else TRANSITION_TO_BLACK_MS
        self.fade_black_to(target, duration, self.apply_pending_entry)

    def switch_to_source_at(
        self,
        source_key: str,
        path: Path,
        display_label: str,
        entry_id: str | None,
        play_at_ms: int | None = None,
        force_reload: bool = False,
    ) -> None:
        self.schedule_action(
            lambda: self.switch_to_source(source_key, path, display_label, entry_id, force_reload=force_reload),
            play_at_ms,
        )

    def switch_to_entry(self, entry: ScheduleEntry | None, video_directory: Path | None, force_reload: bool = False, play_at_ms: int | None = None) -> None:
        if not self.is_runtime_alive():
            return
        if entry is None:
            self.clear_playback_at("No media scheduled for the current UK time.", play_at_ms)
            return

        video_path = (video_directory / entry.video_file) if video_directory is not None else None
        if video_path is None or not video_path.exists():
            self.clear_playback_at(f"Missing media file:\n{entry.video_label or entry.video_file}", play_at_ms)
            return
        self.switch_to_source_at(
            f"schedule:{entry.id}",
            video_path,
            entry.video_label or entry.video_file,
            entry.id,
            play_at_ms=play_at_ms,
            force_reload=force_reload,
        )

    def switch_to_override(self, path: Path, label: str, force_reload: bool = False, play_at_ms: int | None = None) -> None:
        if not self.is_runtime_alive():
            return
        self.switch_to_source_at(
            f"override:{self.screen_id}:{path.resolve()}",
            path,
            label,
            f"override:{self.screen_id}",
            play_at_ms=play_at_ms,
            force_reload=force_reload,
        )

    def apply_pending_entry(self) -> None:
        if not self.is_runtime_alive():
            return
        if self.pending_entry_path is None or self.pending_source_key is None:
            return

        entry_path = self.pending_entry_path
        self.current_source_key = self.pending_source_key
        self.current_source_signature = self.pending_source_signature
        self.current_image_path = None
        self.current_image_pixmap = None
        self.current_media_kind = ""
        self._ready_source_key = None
        self.current_video_path = None
        self._last_player_position = -1
        self._last_progress_monotonic = time.monotonic()
        self._stall_recovery_count = 0
        self.pending_source_key = None
        self.pending_source_signature = None
        self.message_label.hide()
        if is_image_file(entry_path):
            pixmap = load_pixmap_for_bounds(entry_path, self.image_label.size())
            if pixmap.isNull():
                self.clear_playback(f"Unable to display image:\n{entry_path.name}")
                return
            self.player.stop()
            self.video_widget.hide()
            self.image_label.show()
            self.image_label.raise_()
            self.current_image_path = entry_path
            self.current_image_pixmap = pixmap
            self.current_media_kind = "image"
            self._ready_source_key = self.current_source_key
            self.current_video_path = None
            self.pause_button.setEnabled(False)
            self.update_image_display()
            if self.transition_method == "cut":
                self.black_overlay_effect.setOpacity(0.0)
            else:
                duration = 380 if self.transition_method == "soft_fade" else TRANSITION_FROM_BLACK_MS
                self.fade_black_to(0.0, duration)
        else:
            self.image_label.hide()
            self.video_widget.show()
            self.current_media_kind = "video"
            self.current_video_path = entry_path
            self.pause_button.setEnabled(True)
            self.player.stop()
            self.player.setSource(QUrl.fromLocalFile(str(entry_path)))
            self._last_player_position = -1
            self._last_progress_monotonic = time.monotonic()
            self._stall_recovery_count = 0
            if self.is_paused:
                self.player.pause()
            else:
                self.player.play()

    def update_image_display(self) -> None:
        if self.current_image_pixmap is None or not isValid(self.image_label):
            return
        target_size = self.image_label.size()
        if target_size.width() <= 0 or target_size.height() <= 0:
            return
        scaled = self.current_image_pixmap.scaled(
            target_size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.image_label.setPixmap(scaled)

    def on_media_status_changed(self, status: QMediaPlayer.MediaStatus) -> None:
        if not self.is_runtime_alive():
            return
        if self.current_media_kind != "video":
            return
        if status in {
            QMediaPlayer.MediaStatus.LoadedMedia,
            QMediaPlayer.MediaStatus.BufferedMedia,
        } and self.current_source_key is not None and self._ready_source_key != self.current_source_key:
            self._ready_source_key = self.current_source_key
            if not self.is_paused:
                self.player.play()
            if self.transition_method == "cut":
                self.black_overlay_effect.setOpacity(0.0)
            else:
                duration = 380 if self.transition_method == "soft_fade" else TRANSITION_FROM_BLACK_MS
                self.fade_black_to(0.0, duration)
        elif status == QMediaPlayer.MediaStatus.StalledMedia:
            append_engine_startup_log(f"Playback stalled on {self.screen_id}; attempting recovery.")
            self.recover_stalled_video()

    def on_media_error(self, error, error_string: str) -> None:
        if not self.is_runtime_alive():
            return
        if error == QMediaPlayer.Error.NoError:
            return
        append_engine_startup_log(
            f"Playback error on {self.screen_id}: {error_string or error}. "
            f"source={self.current_source_key or self.pending_source_key or ''}"
        )
        self.clear_playback(error_string or "Unable to play the selected media.")

    def on_position_changed(self, position_ms: int) -> None:
        if position_ms != self._last_player_position:
            self._last_player_position = position_ms
            self._last_progress_monotonic = time.monotonic()
            self._stall_recovery_count = 0

    def check_playback_health(self) -> None:
        if not self.is_runtime_alive():
            return
        if self.current_media_kind != "video" or self.is_paused or self.current_video_path is None:
            return
        if self.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            return
        if time.monotonic() - self._last_progress_monotonic < 6.0:
            return
        append_engine_startup_log(
            f"Detected frozen video on {self.screen_id}; "
            f"position={self._last_player_position} source={self.current_video_path}"
        )
        self.recover_stalled_video()

    def recover_stalled_video(self) -> None:
        if not self.is_runtime_alive() or self.current_media_kind != "video" or self.current_video_path is None:
            return
        self._stall_recovery_count += 1
        self._last_progress_monotonic = time.monotonic()
        try:
            if self._stall_recovery_count <= 2:
                self.player.play()
                return
            current_path = self.current_video_path
            self.player.stop()
            self.player.setSource(QUrl.fromLocalFile(str(current_path)))
            if not self.is_paused:
                self.player.play()
        except Exception as error:  # noqa: BLE001
            append_engine_startup_log(f"Failed to recover stalled video on {self.screen_id}: {error}")

    def closeEvent(self, event: QCloseEvent) -> None:  # type: ignore[override]
        self._is_closing = True
        self.controls_hide_timer.stop()
        self.scheduled_action_timer.stop()
        self.playback_watchdog_timer.stop()
        if hasattr(self, "player") and isValid(self.player):
            self.player.stop()
        super().closeEvent(event)


class PlaybackCoordinator(QWidget):
    status_changed = Signal(str, str)
    playback_state_changed = Signal(bool, int)
    commands_changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.manage_local_windows = True
        self.schedules: list[ScheduleEntry] = []
        self.windows: dict[str, PlaybackWindow] = {}
        self.selected_monitor_ids: list[str] = []
        self.video_directory: Path | None = None
        self.transition_method = "fade_black"
        self.is_paused = False
        self.playback_enabled = False
        self.current_entry_id: str | None = None
        self.quick_play_paths: dict[str, Path] = {}
        self.quick_play_labels: dict[str, str] = {}
        self.command_version = 0
        self.screen_commands: dict[str, dict[str, Any]] = {}
        self.last_assignment_keys: dict[str, str] = {}
        self.schedule_timer = QTimer(self)
        self.schedule_timer.setSingleShot(True)
        self.schedule_timer.timeout.connect(self.on_schedule_timer)
        self.sync_current_entry(force=True)

    def set_schedules(self, schedules: list[ScheduleEntry]) -> None:
        self.schedules = schedules[:]
        self.current_entry_id = None
        self.sync_current_entry(force=True)

    def set_selected_monitors(self, monitor_ids: list[str]) -> None:
        self.selected_monitor_ids = monitor_ids[:]
        self.sync_current_entry(force=True)

    def set_video_directory(self, video_directory: Path | None) -> None:
        self.video_directory = video_directory
        self.current_entry_id = None
        self.sync_current_entry(force=True)

    def set_transition_method(self, method: str) -> None:
        self.transition_method = method if method in TRANSITION_METHODS else "fade_black"
        if not self.manage_local_windows:
            return
        for screen_id, window in list(self.windows.items()):
            if not self.is_window_usable(screen_id, window):
                continue
            window.set_transition_method(self.transition_method)

    def launch_windows(self) -> None:
        self.playback_enabled = True
        self.sync_current_entry(force=True)

    def stop_windows(self) -> None:
        self.playback_enabled = False
        self.quick_play_paths = {}
        self.quick_play_labels = {}
        self.screen_commands = {}
        self.last_assignment_keys = {}
        self.current_entry_id = None
        self.schedule_timer.stop()
        windows = list(self.windows.items())
        self.windows = {}
        for _screen_id, window in windows:
            if isValid(window):
                window.close()
        clock_label, _minute_of_day, _weekday_index = current_uk_time()
        self.status_changed.emit(clock_label, "Playback stopped")
        self.playback_state_changed.emit(self.is_paused, 0)
        self.commands_changed.emit()

    def toggle_pause(self, checked: bool = False) -> None:
        self.is_paused = not self.is_paused
        if self.manage_local_windows:
            for screen_id, window in list(self.windows.items()):
                if not self.is_window_usable(screen_id, window):
                    continue
                window.set_paused(self.is_paused)
        self.last_assignment_keys = {}
        self.sync_current_entry(force=True)
        self.playback_state_changed.emit(self.is_paused, len(self.windows))

    def ensure_window_for_screen(self, screen_id: str) -> PlaybackWindow | None:
        if not self.manage_local_windows:
            return None
        existing = self.windows.get(screen_id)
        if existing is not None and self.is_window_usable(screen_id, existing):
            return existing
        screen = find_screen_by_id(screen_id)
        if screen is None:
            return None
        window = PlaybackWindow(screen, f"Background Screen {len(self.windows) + 1}")
        window.pause_requested.connect(self.toggle_pause)
        window.set_paused(self.is_paused)
        window.set_transition_method(self.transition_method)
        window.show_on_screen()
        self.register_window(window)
        self.playback_state_changed.emit(self.is_paused, len(self.windows))
        return window

    def apply_quick_play(self, target_screen_ids: list[str], path: Path) -> None:
        self.playback_enabled = True
        for screen_id in target_screen_ids:
            self.quick_play_paths[screen_id] = path
            self.quick_play_labels[screen_id] = path.name
            if self.manage_local_windows:
                window = self.ensure_window_for_screen(screen_id)
                if window is not None:
                    window.switch_to_override(path, path.name, force_reload=True)
        self.sync_current_entry(force=True)

    def clear_quick_play(self, target_screen_ids: list[str] | None = None) -> None:
        targets = list(self.quick_play_paths.keys()) if target_screen_ids is None else target_screen_ids
        for screen_id in targets:
            self.quick_play_paths.pop(screen_id, None)
            self.quick_play_labels.pop(screen_id, None)
            if self.manage_local_windows and not is_remote_screen_id(screen_id) and screen_id not in self.selected_monitor_ids and screen_id in self.windows:
                window = self.windows.pop(screen_id)
                if isValid(window):
                    window.close()
        self.playback_state_changed.emit(self.is_paused, len(self.windows))
        self.sync_current_entry(force=True)

    def all_active_screen_ids(self) -> list[str]:
        if not self.playback_enabled:
            return sorted(set(self.quick_play_paths.keys()))
        schedule_remote_targets = {
            screen_id
            for entry in self.schedules
            for screen_id in entry.screen_ids
            if is_remote_screen_id(screen_id)
        }
        return sorted(set(self.selected_monitor_ids) | set(self.quick_play_paths.keys()) | schedule_remote_targets)

    def has_launch_targets(self) -> bool:
        if self.selected_monitor_ids or self.quick_play_paths:
            return True
        return any(bool(entry.screen_ids) for entry in self.schedules)

    def assignment_for_screen(self, screen_id: str, weekday_index: int, minute_of_day: int) -> dict[str, Any]:
        override_path = self.quick_play_paths.get(screen_id)
        if override_path is not None:
            return {
                "key": f"override:{screen_id}:{override_path.resolve()}:{self.is_paused}",
                "type": "play",
                "mode": "quick_play",
                "path": override_path,
                "label": self.quick_play_labels.get(screen_id, override_path.name),
                "entry_id": f"override:{screen_id}",
                "message": "",
            }
        entry = active_schedule_for_screen(self.schedules, weekday_index, minute_of_day, screen_id)
        if entry is None:
            return {
                "key": f"clear:{screen_id}",
                "type": "clear",
                "mode": "idle",
                "path": None,
                "label": "",
                "entry_id": None,
                "message": "No media scheduled for the current UK time.",
            }
        media_path = (self.video_directory / entry.video_file) if self.video_directory is not None else None
        if media_path is None or not media_path.exists():
            return {
                "key": f"clear-missing:{screen_id}:{entry.video_file}",
                "type": "clear",
                "mode": "schedule",
                "path": None,
                "label": entry.video_label or entry.video_file,
                "entry_id": entry.id,
                "message": f"Missing media file:\n{entry.video_label or entry.video_file}",
            }
        return {
            "key": f"schedule:{entry.id}:{media_path.resolve()}:{self.is_paused}",
            "type": "play",
            "mode": "schedule",
            "path": media_path,
            "label": entry.video_label or entry.video_file,
            "relative_path": entry.video_file,
            "entry_id": entry.id,
            "message": "",
        }

    def build_screen_command(self, screen_id: str, assignment: dict[str, Any], play_at_ms: int, version: int) -> dict[str, Any]:
        media_path = assignment.get("path")
        media_kind = ""
        if isinstance(media_path, Path):
            if is_image_file(media_path):
                media_kind = "image"
            elif is_video_file(media_path):
                media_kind = "video"
        return {
            "screen_id": screen_id,
            "version": version,
            "type": assignment.get("type", "clear"),
            "mode": assignment.get("mode", "idle"),
            "entry_id": assignment.get("entry_id"),
            "message": assignment.get("message", ""),
            "label": assignment.get("label", ""),
            "path": str(media_path) if isinstance(media_path, Path) else "",
            "relative_path": assignment.get("relative_path", ""),
            "media_kind": media_kind,
            "play_at_ms": play_at_ms,
            "transition": self.transition_method,
            "paused": self.is_paused,
        }

    def sync_current_entry(self, force: bool = False) -> None:
        _, minute_of_day, weekday_index = current_uk_time()
        assignments = {
            screen_id: self.assignment_for_screen(screen_id, weekday_index, minute_of_day)
            for screen_id in self.all_active_screen_ids()
        }
        if self.manage_local_windows:
            for screen_id, assignment in assignments.items():
                if is_remote_screen_id(screen_id):
                    continue
                if assignment.get("type") != "play":
                    continue
                window = self.windows.get(screen_id)
                if window is None or not self.is_window_usable(screen_id, window):
                    created = self.ensure_window_for_screen(screen_id)
                    if created is not None:
                        created.set_transition_method(self.transition_method)
                        created.set_paused(self.is_paused)
        assignment_keys = {screen_id: str(assignment.get("key") or "") for screen_id, assignment in assignments.items()}
        changed_screen_ids = {screen_id for screen_id, key in assignment_keys.items() if self.last_assignment_keys.get(screen_id) != key}
        removed_screen_ids = set(self.last_assignment_keys) - set(assignment_keys)
        schedule_update = force or bool(changed_screen_ids) or bool(removed_screen_ids)
        play_at_ms = current_timestamp_ms() + REMOTE_SYNC_LEAD_MS if schedule_update else None
        first_entry_id: str | None = None
        if self.manage_local_windows:
            for screen_id, window in list(self.windows.items()):
                if not self.is_window_usable(screen_id, window):
                    continue
                assignment = assignments.get(screen_id)
                if assignment is None:
                    if not is_remote_screen_id(screen_id):
                        self.windows.pop(screen_id, None)
                        if isValid(window):
                            window.close()
                    continue
                if assignment.get("type") == "play" and assignment.get("mode") == "quick_play":
                    override_path = assignment.get("path")
                    if isinstance(override_path, Path):
                        window.switch_to_override(
                            override_path,
                            str(assignment.get("label") or override_path.name),
                            force_reload=force or screen_id in changed_screen_ids,
                            play_at_ms=play_at_ms,
                        )
                elif assignment.get("type") == "play":
                    entry = next((item for item in self.schedules if item.id == assignment.get("entry_id")), None)
                    if first_entry_id is None and entry is not None:
                        first_entry_id = entry.id
                    window.switch_to_entry(
                        entry,
                        self.video_directory,
                        force_reload=force or screen_id in changed_screen_ids,
                        play_at_ms=play_at_ms,
                    )
                else:
                    if screen_id not in self.selected_monitor_ids and not is_remote_screen_id(screen_id):
                        self.windows.pop(screen_id, None)
                        if isValid(window):
                            window.close()
                        continue
                    window.clear_playback_at(str(assignment.get("message") or "No media scheduled for the current UK time."), play_at_ms)
        for screen_id, assignment in assignments.items():
            if assignment.get("type") == "play" and assignment.get("mode") == "schedule" and first_entry_id is None:
                first_entry_id = str(assignment.get("entry_id") or "") or None
        if schedule_update:
            self.command_version += 1
            version = self.command_version
            for screen_id, assignment in assignments.items():
                self.screen_commands[screen_id] = self.build_screen_command(screen_id, assignment, play_at_ms or current_timestamp_ms(), version)
            for screen_id in removed_screen_ids:
                self.screen_commands.pop(screen_id, None)
            self.last_assignment_keys = assignment_keys
            self.commands_changed.emit()
        if force or first_entry_id != self.current_entry_id:
            self.current_entry_id = first_entry_id
        self.emit_status_for_current_time(weekday_index, minute_of_day)
        self.schedule_next_update()
        self.playback_state_changed.emit(self.is_paused, len(self.windows))

    def emit_status_for_current_time(self, weekday_index: int, minute_of_day: int) -> None:
        clock_label, _minute, _weekday = current_uk_time()
        if not self.playback_enabled and not self.quick_play_paths:
            self.status_changed.emit(clock_label, "Playback stopped")
            return
        active_entries = [
            entry
            for screen_id in self.selected_monitor_ids
            if (entry := active_schedule_for_screen(self.schedules, weekday_index, minute_of_day, screen_id)) is not None
        ]
        if not active_entries:
            status = "No active schedule"
        else:
            unique_labels = list(dict.fromkeys(f"{entry.title} • {entry.range_label}" for entry in active_entries))
            if len(unique_labels) == 1:
                status = unique_labels[0]
            else:
                status = f"{len(active_entries)} screens on scheduled playback"
        self.status_changed.emit(clock_label, status)

    def on_schedule_timer(self) -> None:
        self.sync_current_entry()

    def schedule_next_update(self) -> None:
        if self.schedule_timer.isActive():
            self.schedule_timer.stop()
        delay_ms = self.next_schedule_change_delay_ms()
        if delay_ms is not None:
            self.schedule_timer.start(delay_ms)

    def next_schedule_change_delay_ms(self) -> int | None:
        if not self.schedules:
            return None
        now = current_uk_datetime()
        next_change: datetime | None = None
        for entry in self.schedules:
            start_minutes = ScheduleEntry.time_to_minutes(entry.start_time)
            end_minutes = ScheduleEntry.time_to_minutes(entry.end_time)
            for day_offset in range(8):
                day_date = (now + timedelta(days=day_offset)).date()
                day_key = DAY_INDEX_TO_KEY[day_date.weekday()]
                if day_key not in entry.selected_days():
                    continue
                start_dt = datetime.combine(day_date, datetime.min.time(), tzinfo=UK_TZ) + timedelta(minutes=start_minutes)
                if start_dt > now and (next_change is None or start_dt < next_change):
                    next_change = start_dt
                end_date = day_date if start_minutes < end_minutes else day_date + timedelta(days=1)
                end_dt = datetime.combine(end_date, datetime.min.time(), tzinfo=UK_TZ) + timedelta(minutes=end_minutes)
                if end_dt > now and (next_change is None or end_dt < next_change):
                    next_change = end_dt
        if next_change is None:
            return None
        return max(int((next_change - now).total_seconds() * 1000) + 50, 250)

    def command_snapshots(self) -> dict[str, dict[str, Any]]:
        return {screen_id: dict(command) for screen_id, command in self.screen_commands.items()}

    def remote_command_snapshots(self) -> dict[str, dict[str, Any]]:
        return {screen_id: dict(command) for screen_id, command in self.screen_commands.items() if is_remote_screen_id(screen_id)}

    def register_window(self, window: PlaybackWindow) -> None:
        self.windows[window.screen_id] = window
        window.destroyed.connect(lambda *_args, screen_id=window.screen_id: self.on_window_destroyed(screen_id))

    def on_window_destroyed(self, screen_id: str) -> None:
        self.windows.pop(screen_id, None)
        self.quick_play_paths.pop(screen_id, None)
        self.quick_play_labels.pop(screen_id, None)
        self.playback_state_changed.emit(self.is_paused, len(self.windows))

    def is_window_usable(self, screen_id: str, window: PlaybackWindow) -> bool:
        if not isValid(window) or not window.is_runtime_alive():
            self.windows.pop(screen_id, None)
            self.quick_play_paths.pop(screen_id, None)
            self.quick_play_labels.pop(screen_id, None)
            return False
        return True


class LocalPlaybackWorkerController(QObject):
    def __init__(self, screen_id: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        screen = find_screen_by_id(screen_id)
        if screen is None:
            raise RuntimeError(f"Display {screen_id} is no longer available.")
        self.screen_id = screen_id
        self.window = PlaybackWindow(screen, "Background Screen")
        self.window.show_on_screen()
        self.current_version = 0
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(800)
        self.poll_timer.timeout.connect(self.poll_once)
        self.poll_timer.start()
        QTimer.singleShot(0, self.poll_once)

    def poll_once(self) -> None:
        try:
            request = Request(
                f"http://127.0.0.1:{LAN_SERVER_PORT}/api/remote/poll?screenId={quote(self.screen_id)}",
                headers={"Accept": "application/json"},
                method="GET",
            )
            with urlopen(request, timeout=1.5) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception:
            return
        command = payload.get("command")
        if not isinstance(command, dict):
            return
        version = int(command.get("version") or 0)
        if version == self.current_version:
            return
        self.current_version = version
        self.apply_command(command)

    def apply_command(self, command: dict[str, Any]) -> None:
        self.window.set_transition_method(str(command.get("transition") or "fade_black"))
        self.window.set_paused(bool(command.get("paused")))
        media_path = Path(str(command.get("path") or "")).expanduser() if command.get("path") else None
        media_label = str(command.get("label") or (media_path.name if isinstance(media_path, Path) else ""))
        play_at_ms = int(command.get("playAtMs") or 0) or None
        if str(command.get("type") or "clear") != "play" or media_path is None or not media_path.exists():
            message = str(command.get("message") or "")
            show_message = bool(message and "Missing media file" in message)
            self.window.clear_playback_at(message, play_at_ms, show_message=show_message)
            return
        self.window.switch_to_override(media_path, media_label or media_path.name, force_reload=True, play_at_ms=play_at_ms)


class ElidedLabel(QLabel):
    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full_text = text
        super().setText(text)

    def setText(self, text: str) -> None:  # type: ignore[override]
        self._full_text = text
        self._apply_elision()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._apply_elision()

    def _apply_elision(self) -> None:
        if self.width() <= 12:
            super().setText(self._full_text)
            return
        metrics = QFontMetrics(self.font())
        super().setText(metrics.elidedText(self._full_text, Qt.TextElideMode.ElideRight, self.width() - 6))


class MediaPickerDialog(QDialog):
    def __init__(
        self,
        media_files: list[Path],
        media_directory: Path | None,
        selected_file: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.media_files = media_files[:]
        self.media_directory = media_directory
        self.selected_file = selected_file
        self.selected_path = selected_file
        self.preview_pixmap: QPixmap | None = None
        self.player = QMediaPlayer(self)
        self.player.setLoops(QMediaPlayer.Loops.Infinite)
        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(0.0)
        self.player.setAudioOutput(self.audio_output)
        self.player.mediaStatusChanged.connect(self.on_media_status_changed)

        self.setWindowTitle("Choose Scheduled Media")
        self.resize(980, 620)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        header = QLabel("Choose media from the selected folder")
        header.setObjectName("sectionTitle")
        layout.addWidget(header)

        body = QHBoxLayout()
        body.setSpacing(14)
        layout.addLayout(body, 1)

        self.media_list = QListWidget()
        self.media_list.setMinimumWidth(340)
        self.media_list.currentItemChanged.connect(self.on_item_changed)
        body.addWidget(self.media_list, 0)

        preview_side = QFrame()
        preview_side.setObjectName("surface")
        preview_layout = QVBoxLayout(preview_side)
        preview_layout.setContentsMargins(14, 14, 14, 14)
        preview_layout.setSpacing(10)
        body.addWidget(preview_side, 1)

        preview_title = QLabel("Preview")
        preview_title.setObjectName("sectionTitle")
        preview_layout.addWidget(preview_title)

        self.preview_stack = QFrame()
        self.preview_stack.setStyleSheet("background: #0c1015; border-radius: 16px;")
        self.preview_stack.setMinimumHeight(320)
        self.preview_stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        preview_layout.addWidget(self.preview_stack, 1)

        self.video_preview = QVideoWidget(self.preview_stack)
        self.player.setVideoOutput(self.video_preview)
        self.image_preview = QLabel(self.preview_stack)
        self.image_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_preview.hide()
        self.preview_message = QLabel("Select a file to preview.")
        self.preview_message.setObjectName("mutedText")
        self.preview_message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview_layout.addWidget(self.preview_message)

        self.preview_name_label = ElidedLabel("No file selected")
        self.preview_name_label.setObjectName("videoCardTitle")
        self.preview_path_label = QLabel("")
        self.preview_path_label.setObjectName("mutedText")
        self.preview_path_label.setWordWrap(True)
        preview_layout.addWidget(self.preview_name_label)
        preview_layout.addWidget(self.preview_path_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        selected_row = -1
        current_category = None
        row_index = 0
        for media_path in self.media_files:
            relative_path = media_relative_path(media_path, self.media_directory)
            category = media_category_label(media_path, self.media_directory)
            if category != current_category:
                category_item = QListWidgetItem(f"[{category}]")
                category_item.setFlags(Qt.ItemFlag.NoItemFlags)
                self.media_list.addItem(category_item)
                row_index += 1
                current_category = category
            item = QListWidgetItem(media_path.name)
            item.setData(Qt.ItemDataRole.UserRole, relative_path)
            item.setToolTip(relative_path)
            self.media_list.addItem(item)
            if selected_row < 0:
                selected_row = row_index
            if relative_path == selected_file:
                selected_row = row_index
            row_index += 1

        if self.media_list.count() and selected_row >= 0:
            self.media_list.setCurrentRow(selected_row)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self.video_preview.setGeometry(self.preview_stack.rect())
        self.image_preview.setGeometry(self.preview_stack.rect())
        self.update_image_preview()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.player.stop()
        super().closeEvent(event)

    def accept(self) -> None:  # type: ignore[override]
        current = self.media_list.currentItem()
        if current is not None:
            selected = str(current.data(Qt.ItemDataRole.UserRole) or "")
            if selected:
                self.selected_path = selected
        self.player.stop()
        super().accept()

    def on_item_changed(self, current: QListWidgetItem | None, previous: QListWidgetItem | None) -> None:
        selected = "" if current is None else str(current.data(Qt.ItemDataRole.UserRole) or "")
        self.selected_path = selected
        if not selected or self.media_directory is None:
            self.player.stop()
            self.video_preview.hide()
            self.image_preview.hide()
            self.preview_pixmap = None
            self.preview_name_label.setText("No file selected")
            self.preview_path_label.setText("")
            self.preview_message.setText("Select a file to preview.")
            return

        media_path = self.media_directory / selected
        self.preview_name_label.setText(media_path.name)
        self.preview_path_label.setText(selected)
        if is_image_file(media_path):
            self.player.stop()
            self.video_preview.hide()
            self.image_preview.show()
            self.preview_message.setText("Image preview")
            self.preview_pixmap = load_pixmap_for_bounds(media_path, self.image_preview.size())
            self.update_image_preview()
            return

        self.image_preview.hide()
        self.preview_pixmap = None
        self.video_preview.show()
        self.preview_message.setText("Video preview")
        self.player.stop()
        self.player.setSource(QUrl.fromLocalFile(str(media_path)))
        self.player.play()

    def update_image_preview(self) -> None:
        pixmap = self.preview_pixmap
        if pixmap is None or pixmap.isNull():
            return
        scaled = pixmap.scaled(
            self.image_preview.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.image_preview.setPixmap(scaled)

    def on_media_status_changed(self, status: QMediaPlayer.MediaStatus) -> None:
        return


class TimePickerDialog(QDialog):
    def __init__(self, title: str, initial_time: QTime, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setModal(True)
        self.setObjectName("timePopup")
        self._selected_time = initial_time

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        title_label = QLabel(title)
        title_label.setObjectName("sectionDescription")
        layout.addWidget(title_label)

        self.time_preview = QLabel(initial_time.toString("HH:mm"))
        self.time_preview.setObjectName("timePopupPreview")
        self.time_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.time_preview)

        picker_row = QHBoxLayout()
        picker_row.setSpacing(10)

        self.hour_list = QListWidget()
        self.hour_list.setObjectName("timePickerList")
        self.hour_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.hour_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.hour_list.setSpacing(2)
        self.hour_list.setFixedWidth(74)
        self.hour_list.setFixedHeight(220)

        self.minute_list = QListWidget()
        self.minute_list.setObjectName("timePickerList")
        self.minute_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.minute_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.minute_list.setSpacing(2)
        self.minute_list.setFixedWidth(74)
        self.minute_list.setFixedHeight(220)

        for hour in range(24):
            self.hour_list.addItem(f"{hour:02d}")
        for minute in range(60):
            self.minute_list.addItem(f"{minute:02d}")

        self.hour_list.setCurrentRow(initial_time.hour())
        self.minute_list.setCurrentRow(initial_time.minute())
        self.hour_list.currentRowChanged.connect(self.on_time_part_changed)
        self.minute_list.currentRowChanged.connect(self.on_time_part_changed)

        hour_column = QVBoxLayout()
        hour_column.setSpacing(6)
        hour_header = QLabel("Hour")
        hour_header.setObjectName("mutedText")
        hour_header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hour_column.addWidget(hour_header)
        hour_column.addWidget(self.hour_list)

        minute_column = QVBoxLayout()
        minute_column.setSpacing(6)
        minute_header = QLabel("Minute")
        minute_header.setObjectName("mutedText")
        minute_header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        minute_column.addWidget(minute_header)
        minute_column.addWidget(self.minute_list)

        picker_row.addLayout(hour_column)
        picker_row.addLayout(minute_column)
        layout.addLayout(picker_row)

        hint_label = QLabel("Click outside to apply")
        hint_label.setObjectName("mutedText")
        hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint_label)

        self.setStyleSheet(
            """
            QDialog#timePopup {
                background: #ffffff;
                border: 1px solid rgba(23,48,67,0.14);
                border-radius: 16px;
            }
            QLabel#timePopupPreview {
                background: #eef7ff;
                border: 1px solid rgba(93,183,240,0.55);
                border-radius: 14px;
                padding: 8px 12px;
                color: #102b3d;
                font-size: 28px;
                font-weight: 700;
            }
            QListWidget#timePickerList {
                background: #ffffff;
                border: 1px solid rgba(23,48,67,0.14);
                border-radius: 14px;
                padding: 6px;
                outline: none;
                color: #102b3d;
                font-size: 18px;
                font-weight: 600;
            }
            QListWidget#timePickerList::item {
                min-height: 30px;
                padding: 3px 0;
                border-radius: 10px;
                text-align: center;
            }
            QListWidget#timePickerList::item:selected {
                background: rgba(93,183,240,0.22);
                border: 1px solid rgba(93,183,240,0.72);
                color: #102b3d;
            }
            """
        )
        self.resize(206, 338)
        QTimer.singleShot(0, self._prime_focus)

    def _prime_focus(self) -> None:
        self.hour_list.setFocus()
        self.hour_list.scrollToItem(self.hour_list.currentItem(), QListWidget.ScrollHint.PositionAtCenter)
        self.minute_list.scrollToItem(self.minute_list.currentItem(), QListWidget.ScrollHint.PositionAtCenter)

    def on_time_part_changed(self, _row: int = -1) -> None:
        hour = max(self.hour_list.currentRow(), 0)
        minute = max(self.minute_list.currentRow(), 0)
        self._selected_time = QTime(hour, minute)
        self.time_preview.setText(self._selected_time.toString("HH:mm"))

    def current_time(self) -> QTime:
        return self._selected_time


class QuickPlayTargetDialog(QDialog):
    def __init__(
        self,
        targets: list[UnifiedScreenTarget],
        selected_monitor_ids: list[str],
        media_name: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Choose Quick Play Screens")
        self.setModal(True)
        self.resize(360, 280)
        self.screen_checkboxes: dict[str, QCheckBox] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        title = QLabel("Choose where to play the dropped media")
        title.setObjectName("sectionTitle")
        body = QLabel(f"{media_name} will override scheduled playback on the chosen screen(s).")
        body.setObjectName("sectionDescription")
        body.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(body)

        list_holder = QWidget()
        list_layout = QVBoxLayout(list_holder)
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_layout.setSpacing(8)
        for target in targets:
            checkbox = QCheckBox(target.label if target.online else f"{target.label} (offline)")
            checkbox.setChecked(target.id in selected_monitor_ids if selected_monitor_ids else len(self.screen_checkboxes) == 0)
            checkbox.setToolTip(target.detail or target.label)
            self.screen_checkboxes[target.id] = checkbox
            list_layout.addWidget(checkbox)
        list_layout.addStretch(1)
        layout.addWidget(list_holder, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Start Quick Play")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_screen_ids(self) -> list[str]:
        return [screen_id for screen_id, checkbox in self.screen_checkboxes.items() if checkbox.isChecked()]


class VideoDropZone(QFrame):
    file_dropped = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setObjectName("surface")
        self.setMinimumHeight(108)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(8)
        title = QLabel("Quick Play")
        title.setObjectName("sectionTitle")
        body = QLabel("Drag and drop a video or picture here, then choose the screen or screens in the popup.")
        body.setObjectName("sectionDescription")
        body.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(body)

    def dragEnterEvent(self, event) -> None:  # type: ignore[override]
        urls = event.mimeData().urls()
        if urls and any(Path(url.toLocalFile()).suffix.lower() in SUPPORTED_MEDIA_EXTENSIONS for url in urls if url.isLocalFile()):
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event) -> None:  # type: ignore[override]
        for url in event.mimeData().urls():
            if not url.isLocalFile():
                continue
            local_path = Path(url.toLocalFile())
            if local_path.suffix.lower() in SUPPORTED_MEDIA_EXTENSIONS:
                self.file_dropped.emit(str(local_path))
                event.acceptProposedAction()
                return
        event.ignore()


class ScheduleSlotRow(QWidget):
    def __init__(
        self,
        title: str,
        subtitle: str,
        screens_tooltip: str,
        screen_icon: QPixmap,
        warning_icon: QPixmap | None = None,
        warning_tooltip: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setMinimumHeight(48)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(12)

        text_column = QVBoxLayout()
        text_column.setContentsMargins(0, 0, 0, 0)
        text_column.setSpacing(3)

        title_label = ElidedLabel(title)
        title_label.setStyleSheet("color: #112c40; font-size: 13px; font-weight: 700;")
        subtitle_label = ElidedLabel(subtitle)
        subtitle_label.setStyleSheet("color: rgba(23,48,67,0.78); font-size: 12px;")
        text_column.addWidget(title_label)
        text_column.addWidget(subtitle_label)

        layout.addLayout(text_column, 1)

        icon_row = QHBoxLayout()
        icon_row.setContentsMargins(0, 0, 0, 0)
        icon_row.setSpacing(8)

        screen_label = QLabel()
        screen_label.setPixmap(screen_icon)
        screen_label.setToolTip(screens_tooltip)
        icon_row.addWidget(screen_label, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        if warning_icon is not None:
            warning_label = QLabel()
            warning_label.setPixmap(warning_icon)
            warning_label.setToolTip(warning_tooltip)
            icon_row.addWidget(warning_label, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        layout.addLayout(icon_row, 0)

    def sizeHint(self) -> QSize:  # type: ignore[override]
        return QSize(0, 56)


class MainWindow(QMainWindow):
    def __init__(self, backend_client: ControllerApiClient) -> None:
        super().__init__()
        if backend_client is None:
            raise ValueError("MainWindow now requires a backend engine client.")
        self.backend_client = backend_client
        ensure_app_paths()
        config = load_config()
        self.schedules = [ScheduleEntry.from_dict(item) for item in config["schedules"]]
        self.selected_monitor_ids = [str(item) for item in config["selected_monitor_ids"]]
        self.video_directory: Path | None = Path(config["video_directory"]).expanduser() if config["video_directory"] else None
        if self.video_directory is not None and not self.video_directory.exists():
            self.video_directory = None
        self.screen_aliases: dict[str, str] = {str(key): str(value) for key, value in config["screen_aliases"].items()}
        self.transition_method = str(config["transition_method"])
        self.run_at_startup = bool(config["run_at_startup"])
        self.available_videos: list[Path] = []
        self.current_selected_video_file = self.schedules[0].video_file if self.schedules else ""
        self.current_edit_id: str | None = None
        self._hero_progress = 0.0
        self._allow_real_quit = False
        self._shutdown_in_progress = False
        self.tray_icon: QSystemTrayIcon | None = None
        self._layout_mode = ""
        self._last_layout_width = 0
        self.current_active_screen_count = 0
        self.remote_screens: dict[str, dict[str, Any]] = {}
        self.backend_network_snapshot: dict[str, Any] = {}
        self.backend_online = False
        self.backend_state_version = 0
        self.backend_listener = BackendStateListener(self.backend_client, self)
        self.backend_paused = False
        self.backend_playback_enabled = False
        self.backend_window_count = 0
        self.backend_library_scanning = False
        self.backend_library_error = ""

        self.folder_watcher = QFileSystemWatcher(self)
        self.folder_refresh_timer = QTimer(self)
        self.folder_refresh_timer.setSingleShot(True)
        self.folder_refresh_timer.setInterval(350)
        self.screen_refresh_timer = QTimer(self)
        self.screen_refresh_timer.setSingleShot(True)
        self.screen_refresh_timer.setInterval(250)
        self.ui_status_timer = QTimer(self)
        self.ui_status_timer.setInterval(1000)
        self.ui_status_timer.timeout.connect(self.refresh_live_ui_status)

        self.setWindowTitle("Background Screen Controller")
        self.resize(1180, 760)
        self.apply_styles()
        self.build_ui()
        self.setup_tray()
        self.backend_listener.snapshot_received.connect(self.apply_backend_snapshot)
        self.backend_listener.connection_changed.connect(self.on_backend_connection_changed)
        self.setup_screen_watchers()
        self.refresh_monitors()
        self.refresh_schedule_list()
        self.load_schedule_into_form(self.schedules[0].id if self.schedules else None)
        self.update_status_labels(*current_uk_time_status(self.schedules))
        self.update_playback_state_label(False, 0)
        self.pull_backend_state(initial=True)
        self.backend_listener.start()
        self.ui_status_timer.start()
        self.update_engine_menu_state()

    def apply_styles(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget#root {
                background: #dfe9f3;
                color: #173043;
            }
            QMenuBar {
                background: #f4f8fc;
                color: #173043;
                border-bottom: 1px solid rgba(23,48,67,0.12);
                padding: 4px;
            }
            QMenuBar::item {
                padding: 8px 12px;
                border-radius: 8px;
                background: transparent;
            }
            QMenuBar::item:selected {
                background: rgba(93,183,240,0.14);
            }
            QMenu {
                background: #f8fbfe;
                color: #173043;
                border: 1px solid rgba(23,48,67,0.12);
                padding: 8px;
            }
            QMenu::item {
                padding: 8px 18px;
                border-radius: 8px;
            }
            QMenu::item:selected {
                background: rgba(93,183,240,0.16);
            }
            QFrame#heroSurface,
            QFrame#surface,
            QFrame#videoCard,
            QFrame#footerBar {
                background: #f6fafd;
                border: 1px solid rgba(23,48,67,0.10);
                border-radius: 18px;
            }
            QFrame#heroSurface {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:1,
                    stop:0 #edf7ff,
                    stop:0.52 #d7edf9,
                    stop:1 #c3e2f4
                );
                border: 1px solid rgba(93,183,240,0.24);
            }
            QFrame#footerBar {
                background: #eef5fb;
                border-radius: 14px;
            }
            QLabel#eyebrow {
                color: #2f84b7;
                font-size: 10px;
                font-weight: 700;
                letter-spacing: 0.14em;
                text-transform: uppercase;
            }
            QLabel#heroTitle {
                font-size: 21px;
                font-weight: 700;
                color: #112c40;
            }
            QLabel#heroSubtitle {
                color: rgba(17,44,64,0.78);
                font-size: 12px;
            }
            QLabel#sectionTitle {
                font-size: 16px;
                font-weight: 650;
                color: #112c40;
            }
            QLabel#sectionDescription,
            QLabel#mutedText {
                color: rgba(23,48,67,0.78);
                font-size: 12px;
            }
            QListWidget {
                background: #ffffff;
                border: 1px solid rgba(23,48,67,0.10);
                border-radius: 14px;
                padding: 6px;
                outline: none;
            }
            QListWidget::item {
                background: #f6fbff;
                border: 1px solid rgba(23,48,67,0.05);
                border-radius: 12px;
                margin: 3px 0;
                padding: 0;
                color: #173043;
            }
            QListWidget::item:selected {
                background: #d9eefb;
                border: 1px solid rgba(47,132,183,0.42);
            }
            QListWidget::item:hover {
                background: #eef7fd;
            }
            QLabel#videoCardTitle {
                font-size: 14px;
                font-weight: 600;
                color: #112c40;
            }
            QLabel#videoCardLabel {
                color: rgba(23,48,67,0.72);
                font-size: 11px;
                font-weight: 700;
                letter-spacing: 0.08em;
                text-transform: uppercase;
            }
            QLabel#videoCardBody {
                color: rgba(17,44,64,0.86);
                font-size: 12px;
            }
            QLineEdit, QTimeEdit {
                min-height: 38px;
                background: #ffffff;
                border: 1px solid rgba(23,48,67,0.16);
                border-radius: 12px;
                padding: 0 12px;
                color: #173043;
                selection-background-color: rgba(93,183,240,0.28);
            }
            QLineEdit:focus, QTimeEdit:focus {
                border: 1px solid rgba(47,132,183,0.70);
                background: #fafdff;
            }
            QComboBox, QToolButton {
                min-height: 38px;
                background: #ffffff;
                border: 1px solid rgba(23,48,67,0.16);
                border-radius: 12px;
                padding: 0 12px;
                color: #173043;
            }
            QComboBox {
                padding-right: 28px;
            }
            QComboBox::drop-down, QToolButton::menu-indicator {
                border: none;
            }
            QComboBox::down-arrow {
                width: 12px;
                height: 12px;
            }
            QComboBox QAbstractItemView {
                background: #ffffff;
                color: #173043;
                border: 1px solid rgba(23,48,67,0.12);
                padding: 6px;
                selection-background-color: rgba(93,183,240,0.16);
            }
            QLabel {
                color: #173043;
            }
            QPushButton {
                min-height: 36px;
                padding: 0 14px;
                border-radius: 12px;
                border: 1px solid rgba(23,48,67,0.16);
                background: #edf5fb;
                color: #173043;
                font-weight: 700;
            }
            QPushButton:hover {
                background: #dcecf8;
            }
            QPushButton:pressed {
                background: #cfe3f2;
            }
            QPushButton[variant="primary"] {
                background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #84dcc6, stop:1 #5db7f0);
                color: #071119;
                border: none;
            }
            QPushButton[variant="primary"]:hover {
                background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #96e5d1, stop:1 #6cc4f5);
            }
            QPushButton[variant="danger"] {
                background: #ffe2e2;
                border: 1px solid #f0a5a5;
                color: #8a2424;
            }
            QPushButton[variant="ghost"] {
                background: #ffffff;
                border: 1px solid rgba(23,48,67,0.22);
                color: #102b3d;
            }
            QPushButton[variant="ghost"]:hover {
                background: #eef6fc;
                border: 1px solid rgba(47,132,183,0.34);
            }
            QPushButton[variant="danger"]:hover {
                background: #ffd4d4;
                border: 1px solid #e58c8c;
            }
            QPushButton:disabled {
                background: #eef2f5;
                border: 1px solid rgba(23,48,67,0.10);
                color: rgba(23,48,67,0.52);
            }
            QPushButton[variant="danger"]:disabled {
                background: #f7e6e6;
                border: 1px solid #e6c3c3;
                color: #b56d6d;
            }
            QCheckBox {
                color: #173043;
                spacing: 8px;
                font-weight: 600;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
                border-radius: 6px;
                border: 1px solid rgba(23,48,67,0.22);
                background: #ffffff;
            }
            QCheckBox::indicator:checked {
                background: #5db7f0;
                border: 1px solid rgba(47,132,183,0.8);
            }
            QLabel#messageBanner {
                border-radius: 12px;
                padding: 10px 12px;
                background: rgba(255,255,255,0.78);
                color: rgba(17,44,64,0.84);
            }
            QFrame#progressTrack {
                background: rgba(23,48,67,0.08);
                border: 1px solid rgba(23,48,67,0.08);
                border-radius: 999px;
            }
            QFrame#progressFill {
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 rgba(132,220,198,0.72), stop:1 rgba(93,183,240,0.62));
                border-radius: 999px;
            }
            QLabel#footerPrimary {
                color: #112c40;
                font-size: 13px;
                font-weight: 650;
            }
            QLabel#footerSecondary {
                color: rgba(23,48,67,0.72);
                font-size: 11px;
            }
            QSplitter::handle {
                background: transparent;
                width: 10px;
                height: 10px;
            }
            """
        )

    def build_ui(self) -> None:
        root = QWidget(self)
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(16, 14, 16, 16)
        outer.setSpacing(10)

        menu = self.menuBar().addMenu("Playback")
        launch_action = QAction("Start Playback", self)
        launch_action.triggered.connect(self.launch_selected_monitors)
        menu.addAction(launch_action)
        pause_action = QAction("Pause/Play", self)
        pause_action.triggered.connect(self.handle_toggle_pause)
        menu.addAction(pause_action)
        self.fullscreen_action = QAction("Controller Full Screen", self)
        self.fullscreen_action.setCheckable(True)
        self.fullscreen_action.triggered.connect(self.toggle_controller_fullscreen)
        menu.addAction(self.fullscreen_action)
        background_action = QAction("Run in Background", self)
        background_action.triggered.connect(self.send_to_background)
        menu.addAction(background_action)
        quit_action = QAction("Quit Completely", self)
        quit_action.triggered.connect(self.quit_from_tray)
        menu.addAction(quit_action)
        self.run_at_startup_action = QAction("Run at Windows Sign-in", self)
        self.run_at_startup_action.setCheckable(True)
        self.run_at_startup_action.setChecked(self.run_at_startup)
        self.run_at_startup_action.triggered.connect(self.toggle_run_at_startup)
        menu.addAction(self.run_at_startup_action)
        engine_menu = self.menuBar().addMenu("Engine")
        self.start_engine_action = QAction("Start Engine", self)
        self.start_engine_action.triggered.connect(self.start_backend_engine)
        engine_menu.addAction(self.start_engine_action)
        self.stop_engine_action = QAction("Stop Engine", self)
        self.stop_engine_action.triggered.connect(self.stop_backend_engine)
        engine_menu.addAction(self.stop_engine_action)
        self.retry_firewall_action = QAction("Retry LAN Firewall Setup", self)
        self.retry_firewall_action.triggered.connect(self.retry_lan_firewall_setup)
        engine_menu.addAction(self.retry_firewall_action)
        self.refresh_engine_action = QAction("Refresh Engine Status", self)
        self.refresh_engine_action.triggered.connect(lambda: self.pull_backend_state(initial=False))
        engine_menu.addAction(self.refresh_engine_action)
        screen_menu = self.menuBar().addMenu("Screen")
        self.screen_target_menu = screen_menu.addMenu("Target Playback Screens")
        self.rename_screens_menu = screen_menu.addMenu("Name Screens")
        self.remote_screens_menu = screen_menu.addMenu("Remote LAN Screens")
        refresh_screens_action = QAction("Refresh Screens", self)
        refresh_screens_action.triggered.connect(self.refresh_monitors)
        screen_menu.addAction(refresh_screens_action)
        library_menu = self.menuBar().addMenu("Library")
        set_folder_action = QAction("Set Media Folder", self)
        set_folder_action.triggered.connect(self.choose_video_folder)
        library_menu.addAction(set_folder_action)
        refresh_folder_action = QAction("Refresh Media List", self)
        refresh_folder_action.triggered.connect(self.refresh_video_library)
        library_menu.addAction(refresh_folder_action)

        hero = QFrame()
        hero.setObjectName("heroSurface")
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(18, 16, 18, 16)
        hero_layout.setSpacing(14)

        hero_copy = QVBoxLayout()
        hero_copy.setSpacing(4)
        eyebrow = QLabel("Active Slot")
        eyebrow.setObjectName("eyebrow")
        self.hero_title_label = ElidedLabel("No active slot")
        self.hero_title_label.setObjectName("heroTitle")
        self.hero_subtitle_label = ElidedLabel("Waiting for a matching UK-time schedule.")
        self.hero_subtitle_label.setObjectName("heroSubtitle")
        self.hero_progress_track = QFrame()
        self.hero_progress_track.setObjectName("progressTrack")
        self.hero_progress_track.setFixedHeight(10)
        self.hero_progress_fill = QFrame(self.hero_progress_track)
        self.hero_progress_fill.setObjectName("progressFill")
        hero_copy.addWidget(eyebrow)
        hero_copy.addWidget(self.hero_title_label)
        hero_copy.addWidget(self.hero_subtitle_label)
        hero_copy.addWidget(self.hero_progress_track)

        hero_actions = QHBoxLayout()
        hero_actions.setSpacing(8)
        self.launch_button = QPushButton("Start Playback")
        self.launch_button.setProperty("variant", "primary")
        self.launch_button.clicked.connect(self.launch_selected_monitors)
        self.pause_all_button = QPushButton("Pause / Play")
        self.pause_all_button.setProperty("variant", "ghost")
        self.pause_all_button.clicked.connect(self.handle_toggle_pause)
        self.stop_button = QPushButton("Stop Screens")
        self.stop_button.setProperty("variant", "danger")
        self.stop_button.clicked.connect(self.handle_stop_screens)
        for button in (self.launch_button, self.pause_all_button, self.stop_button):
            hero_actions.addWidget(button)

        hero_layout.addLayout(hero_copy, 1)
        hero_layout.addLayout(hero_actions, 0)
        outer.addWidget(hero)

        network_panel = QFrame()
        network_panel.setObjectName("surface")
        network_layout = QHBoxLayout(network_panel)
        network_layout.setContentsMargins(18, 14, 18, 14)
        network_layout.setSpacing(16)

        network_copy = QVBoxLayout()
        network_copy.setSpacing(3)
        network_title = QLabel("LAN Controller")
        network_title.setObjectName("sectionTitle")
        self.network_status_primary = ElidedLabel("Starting local server…")
        self.network_status_primary.setObjectName("videoCardTitle")
        self.network_status_secondary = ElidedLabel("Waiting for LAN details.")
        self.network_status_secondary.setObjectName("mutedText")
        self.network_url_label = ElidedLabel("")
        self.network_url_label.setObjectName("videoCardBody")
        network_copy.addWidget(network_title)
        network_copy.addWidget(self.network_status_primary)
        network_copy.addWidget(self.network_status_secondary)
        network_copy.addWidget(self.network_url_label)
        network_layout.addLayout(network_copy, 1)

        remote_meta = QVBoxLayout()
        remote_meta.setSpacing(3)
        remote_title = QLabel("Remote Screens")
        remote_title.setObjectName("sectionTitle")
        self.remote_screen_summary_label = ElidedLabel("0 connected")
        self.remote_screen_summary_label.setObjectName("videoCardTitle")
        self.remote_screen_detail_label = ElidedLabel("Open the TV URL on Whale OS screens to register them.")
        self.remote_screen_detail_label.setObjectName("mutedText")
        remote_meta.addWidget(remote_title)
        remote_meta.addWidget(self.remote_screen_summary_label)
        remote_meta.addWidget(self.remote_screen_detail_label)
        network_layout.addLayout(remote_meta, 0)

        outer.addWidget(network_panel)

        schedule_panel = QFrame()
        schedule_panel.setObjectName("surface")
        schedule_layout = QVBoxLayout(schedule_panel)
        schedule_layout.setContentsMargins(20, 20, 20, 20)
        schedule_layout.setSpacing(14)

        quick_play_row = QHBoxLayout()
        quick_play_row.setSpacing(6)
        quick_play_title = QLabel("Quick Play")
        quick_play_title.setObjectName("sectionTitle")
        self.quick_play_browse_button = QPushButton("Browse")
        self.quick_play_browse_button.setProperty("variant", "ghost")
        self.quick_play_browse_button.clicked.connect(self.browse_quick_play_media)
        self.quick_play_browse_button.setMinimumWidth(92)
        self.clear_quick_play_button = QPushButton("Stop")
        self.clear_quick_play_button.setProperty("variant", "ghost")
        self.clear_quick_play_button.clicked.connect(self.clear_quick_play_target)
        self.clear_quick_play_button.setMinimumWidth(84)
        quick_play_row.addWidget(quick_play_title)
        quick_play_row.addWidget(self.quick_play_browse_button)
        quick_play_row.addWidget(self.clear_quick_play_button)
        quick_play_row.addStretch(1)
        schedule_layout.addLayout(quick_play_row)
        self.quick_play_drop_zone = VideoDropZone()
        self.quick_play_drop_zone.file_dropped.connect(self.handle_quick_play_drop)
        schedule_layout.addWidget(self.quick_play_drop_zone)

        schedule_title = QLabel("Schedule Builder")
        schedule_title.setObjectName("sectionTitle")
        schedule_layout.addWidget(schedule_title)

        self.content_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.content_splitter.setChildrenCollapsible(False)
        self.content_splitter.setHandleWidth(12)
        schedule_layout.addWidget(self.content_splitter, 1)

        list_side = QFrame()
        list_side.setObjectName("surface")
        list_layout = QVBoxLayout(list_side)
        list_layout.setContentsMargins(14, 14, 14, 14)
        list_layout.setSpacing(10)
        schedule_list_title = QLabel("Saved Time Slots")
        schedule_list_title.setObjectName("sectionTitle")
        schedule_list_copy = QLabel("Select a slot to edit it, or create a new one.")
        schedule_list_copy.setObjectName("sectionDescription")
        schedule_list_copy.setWordWrap(True)
        list_layout.addWidget(schedule_list_title)
        list_layout.addWidget(schedule_list_copy)
        self.schedule_list = QListWidget()
        self.schedule_list.currentItemChanged.connect(self.on_schedule_selected)
        self.schedule_list.setSpacing(4)
        list_layout.addWidget(self.schedule_list, 1)

        list_buttons = QHBoxLayout()
        self.new_schedule_button = QPushButton("New")
        self.new_schedule_button.setProperty("variant", "primary")
        self.new_schedule_button.clicked.connect(lambda: self.load_schedule_into_form(None))
        self.delete_schedule_button = QPushButton("Delete")
        self.delete_schedule_button.setProperty("variant", "danger")
        self.delete_schedule_button.clicked.connect(self.delete_selected_schedule)
        list_buttons.addWidget(self.new_schedule_button)
        list_buttons.addWidget(self.delete_schedule_button)
        list_layout.addLayout(list_buttons)

        form_side = QFrame()
        form_side.setObjectName("surface")
        form_layout = QVBoxLayout(form_side)
        form_layout.setContentsMargins(14, 14, 14, 14)
        form_layout.setSpacing(12)

        editor_title = QLabel("Schedule Details")
        editor_title.setObjectName("sectionTitle")
        editor_copy = QLabel("Schedules now point to video or image files inside the chosen library folder.")
        editor_copy.setObjectName("sectionDescription")
        editor_copy.setWordWrap(True)
        form_layout.addWidget(editor_title)
        form_layout.addWidget(editor_copy)

        form_grid = QFormLayout()
        form_grid.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form_grid.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        form_grid.setHorizontalSpacing(12)
        form_grid.setVerticalSpacing(10)

        self.title_input = QLineEdit()
        self.title_input.setPlaceholderText("Evening loop")
        self.start_time_input = QTimeEdit()
        self.start_time_input.setDisplayFormat("HH:mm")
        self.start_time_input.setTime(QTime.fromString("00:00", "HH:mm"))
        self.end_time_input = QTimeEdit()
        self.end_time_input.setDisplayFormat("HH:mm")
        self.end_time_input.setTime(QTime.fromString("01:00", "HH:mm"))
        self.start_time_button = QPushButton("00:00")
        self.start_time_button.setProperty("variant", "ghost")
        self.start_time_button.setMinimumWidth(96)
        self.start_time_button.clicked.connect(lambda: self.open_time_picker("start"))
        self.end_time_button = QPushButton("01:00")
        self.end_time_button.setProperty("variant", "ghost")
        self.end_time_button.setMinimumWidth(96)
        self.end_time_button.clicked.connect(lambda: self.open_time_picker("end"))
        self.day_checkboxes: dict[str, QCheckBox] = {}
        self.current_form_screen_ids: list[str] = []
        self.schedule_screen_button = QToolButton()
        self.schedule_screen_button.setText("Target Screens")
        self.schedule_screen_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.schedule_screen_menu = QMenu(self)
        self.schedule_screen_button.setMenu(self.schedule_screen_menu)
        self.schedule_screen_summary_label = ElidedLabel("All selected screens")
        self.schedule_screen_summary_label.setObjectName("mutedText")
        days_field = QWidget()
        days_field.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        days_field.setMinimumHeight(36)
        days_layout = QHBoxLayout(days_field)
        days_layout.setContentsMargins(0, 0, 0, 0)
        days_layout.setSpacing(10)
        for day_key, day_label in DAY_OPTIONS:
            checkbox = QCheckBox(day_label)
            checkbox.setChecked(True)
            checkbox.setMinimumHeight(24)
            self.day_checkboxes[day_key] = checkbox
            days_layout.addWidget(checkbox)
        days_layout.addStretch(1)
        time_field = QWidget()
        time_layout = QHBoxLayout(time_field)
        time_layout.setContentsMargins(0, 0, 0, 0)
        time_layout.setSpacing(8)
        start_label = QLabel("Start")
        start_label.setObjectName("mutedText")
        end_label = QLabel("End")
        end_label.setObjectName("mutedText")
        time_layout.addWidget(start_label)
        time_layout.addWidget(self.start_time_button)
        time_layout.addSpacing(10)
        time_layout.addWidget(end_label)
        time_layout.addWidget(self.end_time_button)
        time_layout.addStretch(1)

        action_field = QWidget()
        action_layout = QHBoxLayout(action_field)
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.setSpacing(8)
        self.choose_media_button = QPushButton("Choose Media")
        self.choose_media_button.setProperty("variant", "ghost")
        self.choose_media_button.clicked.connect(self.open_media_picker)
        self.transition_selector = QComboBox()
        for key, label in TRANSITION_METHODS.items():
            self.transition_selector.addItem(label, key)
        transition_index = self.transition_selector.findData(self.transition_method)
        self.transition_selector.setCurrentIndex(transition_index if transition_index >= 0 else 0)
        self.transition_selector.currentIndexChanged.connect(self.on_transition_changed)
        self.transition_selector.setMaximumWidth(190)
        action_layout.addWidget(self.schedule_screen_button)
        action_layout.addWidget(self.choose_media_button)
        action_layout.addWidget(self.transition_selector)
        action_layout.addStretch(1)

        form_grid.addRow("Title", self.title_input)
        form_grid.addRow("Time", time_field)
        form_grid.addRow("Days", days_field)
        form_grid.addRow("Actions", action_field)
        for row in range(form_grid.rowCount()):
            item = form_grid.itemAt(row, QFormLayout.ItemRole.LabelRole)
            if item and item.widget():
                item.widget().setObjectName("mutedText")

        form_layout.addLayout(form_grid)

        save_buttons = QHBoxLayout()
        self.save_schedule_button = QPushButton("Save Schedule")
        self.save_schedule_button.setProperty("variant", "primary")
        self.save_schedule_button.clicked.connect(self.save_schedule)
        save_buttons.addWidget(self.save_schedule_button)
        save_buttons.addStretch(1)
        form_layout.addLayout(save_buttons)

        self.form_message_label = QLabel("")
        self.form_message_label.setObjectName("messageBanner")
        self.form_message_label.setWordWrap(True)
        form_layout.addWidget(self.form_message_label)
        form_layout.addStretch(1)

        self.content_splitter.addWidget(list_side)
        self.content_splitter.addWidget(form_side)
        self.content_splitter.setStretchFactor(0, 0)
        self.content_splitter.setStretchFactor(1, 1)
        list_side.setMinimumWidth(280)
        form_side.setMinimumWidth(620)

        outer.addWidget(schedule_panel, 1)
        self.content_splitter.setSizes([320, 760])

        footer = QFrame()
        footer.setObjectName("footerBar")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(14, 10, 14, 10)
        footer_layout.setSpacing(14)
        self.footer_time_primary = QLabel("--:--:--")
        self.footer_time_primary.setObjectName("footerPrimary")
        self.footer_time_secondary = QLabel("UK time • Europe/London")
        self.footer_time_secondary.setObjectName("footerSecondary")
        time_group = QVBoxLayout()
        time_group.setSpacing(1)
        time_group.addWidget(self.footer_time_primary)
        time_group.addWidget(self.footer_time_secondary)
        footer_total_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon).pixmap(16, 16)
        footer_global_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton).pixmap(16, 16)
        footer_active_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay).pixmap(16, 16)
        self.footer_total_icon = QLabel()
        self.footer_total_icon.setPixmap(footer_total_icon)
        self.footer_total_primary = QLabel("0 available")
        self.footer_total_primary.setObjectName("footerPrimary")
        self.footer_total_secondary = QLabel("Screens available")
        self.footer_total_secondary.setObjectName("footerSecondary")
        total_text = QVBoxLayout()
        total_text.setSpacing(1)
        total_text.addWidget(self.footer_total_primary)
        total_text.addWidget(self.footer_total_secondary)
        total_group = QHBoxLayout()
        total_group.setSpacing(8)
        total_group.addWidget(self.footer_total_icon, 0, Qt.AlignmentFlag.AlignTop)
        total_group.addLayout(total_text)

        self.footer_global_icon = QLabel()
        self.footer_global_icon.setPixmap(footer_global_icon)
        self.footer_global_primary = QLabel("0 global")
        self.footer_global_primary.setObjectName("footerPrimary")
        self.footer_global_secondary = QLabel("Default playback targets")
        self.footer_global_secondary.setObjectName("footerSecondary")
        global_text = QVBoxLayout()
        global_text.setSpacing(1)
        global_text.addWidget(self.footer_global_primary)
        global_text.addWidget(self.footer_global_secondary)
        global_group = QHBoxLayout()
        global_group.setSpacing(8)
        global_group.addWidget(self.footer_global_icon, 0, Qt.AlignmentFlag.AlignTop)
        global_group.addLayout(global_text)

        self.footer_active_icon = QLabel()
        self.footer_active_icon.setPixmap(footer_active_icon)
        self.footer_active_primary = QLabel("0 active")
        self.footer_active_primary.setObjectName("footerPrimary")
        self.footer_active_secondary = QLabel("Screens playing now")
        self.footer_active_secondary.setObjectName("footerSecondary")
        active_text = QVBoxLayout()
        active_text.setSpacing(1)
        active_text.addWidget(self.footer_active_primary)
        active_text.addWidget(self.footer_active_secondary)
        active_group = QHBoxLayout()
        active_group.setSpacing(8)
        active_group.addWidget(self.footer_active_icon, 0, Qt.AlignmentFlag.AlignTop)
        active_group.addLayout(active_text)
        footer_layout.addLayout(time_group)
        footer_layout.addStretch(1)
        footer_layout.addLayout(total_group)
        footer_layout.addSpacing(18)
        footer_layout.addLayout(global_group)
        footer_layout.addSpacing(18)
        footer_layout.addLayout(active_group)
        outer.addWidget(footer)
        self.refresh_responsive_layout()

    def section_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet("font-size: 18px; font-weight: 600;")
        return label

    def surface_panel(self, title: str, description: str) -> QFrame:
        panel = QFrame()
        panel.setObjectName("surface")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)
        title_label = QLabel(title)
        title_label.setObjectName("sectionTitle")
        description_label = QLabel(description)
        description_label.setObjectName("sectionDescription")
        description_label.setWordWrap(True)
        layout.addWidget(title_label)
        layout.addWidget(description_label)
        return panel

    def setup_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray_icon = None
            return
        self.tray_icon = QSystemTrayIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon), self)
        tray_menu = QMenu(self)
        show_action = tray_menu.addAction("Show Controller")
        show_action.triggered.connect(self.show_from_tray)
        background_action = tray_menu.addAction("Run in Background")
        background_action.triggered.connect(self.send_to_background)
        pause_action = tray_menu.addAction("Pause / Play")
        pause_action.triggered.connect(self.handle_toggle_pause)
        stop_action = tray_menu.addAction("Stop Screens")
        stop_action.triggered.connect(self.handle_stop_screens)
        tray_menu.addSeparator()
        quit_action = tray_menu.addAction("Quit Completely")
        quit_action.triggered.connect(self.quit_from_tray)
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(lambda reason: self.show_from_tray() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray_icon.show()

    def send_to_background(self) -> None:
        if self._allow_real_quit:
            return
        if self.tray_icon is None or not self.tray_icon.isVisible():
            self.showMinimized()
            return
        self.hide()
        self.tray_icon.showMessage(
            "Background Screen Controller",
            "The controller is still running in the background.",
            QSystemTrayIcon.MessageIcon.Information,
            2000,
        )

    def show_from_tray(self) -> None:
        self.showNormal()
        if self.fullscreen_action.isChecked():
            self.showFullScreen()
        self.raise_()
        self.activateWindow()

    def toggle_controller_fullscreen(self, checked: bool = False) -> None:
        if checked:
            self.showFullScreen()
        else:
            self.showMaximized()
        self.schedule_layout_refresh()

    def call_backend_action(self, action: str, payload: dict[str, Any] | None = None, *, show_errors: bool = True) -> dict[str, Any] | None:
        try:
            response = self.backend_client.action(action, payload or {})
        except Exception as error:  # noqa: BLE001
            if show_errors:
                self.set_form_message(format_request_error(error), "error")
            return None
        snapshot = response.get("snapshot")
        if isinstance(snapshot, dict):
            self.apply_backend_snapshot(snapshot)
        if not response.get("ok", True) and show_errors:
            self.set_form_message(str(response.get("error") or "Action failed."), "error")
            return None
        return response

    def update_engine_menu_state(self) -> None:
        if not hasattr(self, "start_engine_action"):
            return
        self.start_engine_action.setEnabled(not self.backend_online)
        self.stop_engine_action.setEnabled(self.backend_online)
        if hasattr(self, "retry_firewall_action"):
            self.retry_firewall_action.setEnabled(self.backend_online)
        self.refresh_engine_action.setEnabled(True)

    def set_backend_offline_ui(self, detail: str = "") -> None:
        self.backend_online = False
        self.backend_playback_enabled = False
        self.backend_window_count = 0
        self.backend_paused = False
        self.update_playback_state_label(False, 0)
        self.refresh_monitors()
        self.network_status_primary.setText("Background engine offline")
        self.network_status_secondary.setText(
            detail or "Local screens can still be detected, but playback control is unavailable until the engine responds."
        )
        self.network_url_label.setText("Use Engine > Start Engine to bring the background engine online.")
        self.network_url_label.setToolTip(self.network_url_label.text())
        self.remote_screen_summary_label.setText("Engine offline")
        self.remote_screen_detail_label.setText("Connected-device details appear here while the engine is running.")
        self.remote_screen_detail_label.setToolTip(self.remote_screen_detail_label.text())
        self.update_engine_menu_state()

    def pull_backend_state(self, initial: bool = False) -> None:
        try:
            snapshot = self.backend_client.get_state()
        except Exception as error:  # noqa: BLE001
            self.set_backend_offline_ui(
                "Local screens can still be detected, but playback control is unavailable until the engine responds."
            )
            if initial:
                self.set_form_message(format_request_error(error), "error")
            return
        self.backend_online = True
        self.apply_backend_snapshot(snapshot)
        self.update_engine_menu_state()

    def on_backend_connection_changed(self, online: bool, detail: str = "") -> None:
        if online:
            if not self.backend_online:
                self.pull_backend_state(initial=False)
            return
        if self.backend_online:
            self.set_backend_offline_ui(
                detail or "Local screens can still be detected, but playback control is unavailable until the engine responds."
            )

    def refresh_live_ui_status(self) -> None:
        if not self.backend_online:
            return
        clock_label, minute_of_day, weekday_index = current_uk_time()
        if not self.backend_playback_enabled:
            active_label = "Playback stopped"
        else:
            active_entry = active_schedule_for_minute(self.schedules, weekday_index, minute_of_day)
            active_label = "No active schedule" if active_entry is None else f"{active_entry.title} • {active_entry.range_label} • {active_entry.video_label or active_entry.video_file}"
        self.update_status_labels(clock_label, active_label)

    def start_backend_engine(self, checked: bool = False) -> None:
        if self.backend_online and self.backend_client.ping():
            self.set_form_message("The background engine is already running.", "success")
            self.update_engine_menu_state()
            return
        try:
            ensure_backend_running(self.backend_client)
        except Exception as error:  # noqa: BLE001
            message = str(error) or "The controller could not start the background engine."
            self.set_backend_offline_ui(message)
            self.set_form_message(message, "error")
            return
        self.pull_backend_state(initial=True)
        self.backend_listener.start()
        self.set_form_message("Background engine started.", "success")

    def stop_backend_engine(self, checked: bool = False) -> None:
        stopped = False
        if self.backend_online:
            try:
                self.backend_client.action("shutdown_engine", {})
                stopped = True
            except Exception:
                stopped = False
        if not stopped:
            stopped = stop_engine_from_lock()
        if stopped:
            self.set_backend_offline_ui()
            self.set_form_message("Background engine stopped.", "success")
        else:
            self.set_form_message("Background engine was not running or could not be stopped.", "error")

    def retry_lan_firewall_setup(self, checked: bool = False) -> None:
        response = self.call_backend_action("retry_firewall_setup")
        if response is None:
            return
        network = self.backend_network_snapshot
        warnings = [str(item) for item in network.get("warnings") or []]
        firewall_configured = bool(network.get("firewallConfigured"))
        if firewall_configured and not warnings:
            self.set_form_message("LAN firewall access is configured.", "success")
            return
        firewall_warnings = [item for item in warnings if "firewall" in item.lower()]
        if firewall_warnings:
            self.set_form_message(firewall_warnings[0], "error")
        else:
            self.set_form_message("LAN firewall check completed. Review the LAN panel warnings if screens still cannot connect.", "success")

    def handle_toggle_pause(self, checked: bool = False) -> None:
        self.call_backend_action("toggle_pause")

    def handle_stop_screens(self, checked: bool = False) -> None:
        self.call_backend_action("stop_screens")

    def quit_from_tray(self) -> None:
        self.prepare_for_exit(stop_engine=True)
        self._allow_real_quit = True
        self._shutdown_in_progress = True
        self.close()
        app = QApplication.instance()
        if app is not None:
            app.exit(0)

    def prepare_for_exit(self, stop_engine: bool) -> None:
        if self._shutdown_in_progress:
            return
        self._shutdown_in_progress = True
        self.backend_listener.stop()
        self.ui_status_timer.stop()
        self.folder_refresh_timer.stop()
        watched_paths = self.folder_watcher.directories()
        if watched_paths:
            self.folder_watcher.removePaths(watched_paths)
        if stop_engine:
            stopped = False
            try:
                if self.backend_client.ping():
                    self.backend_client.action("shutdown_engine", {}, timeout=1.5)
                    stopped = True
            except Exception:
                stopped = False
            if not stopped:
                stop_engine_from_lock()
        if self.tray_icon is not None:
            self.tray_icon.hide()
            self.tray_icon.deleteLater()
            self.tray_icon = None

    def toggle_run_at_startup(self, checked: bool = False) -> None:
        response = self.call_backend_action("set_run_at_startup", {"value": self.run_at_startup_action.isChecked()})
        if response is None:
            self.run_at_startup_action.blockSignals(True)
            self.run_at_startup_action.setChecked(self.run_at_startup)
            self.run_at_startup_action.blockSignals(False)

    def setup_screen_watchers(self) -> None:
        app = QGuiApplication.instance()
        if app is None:
            return
        app.screenAdded.connect(self.on_screen_topology_changed)
        app.screenRemoved.connect(self.on_screen_topology_changed)

    def on_screen_topology_changed(self, *_args) -> None:
        self.screen_refresh_timer.start()

    def unified_screen_targets(self, include_offline: bool = True) -> list[UnifiedScreenTarget]:
        targets: list[UnifiedScreenTarget] = []
        for screen in available_screens():
            identifier = screen_identifier(screen)
            geometry = screen.geometry()
            targets.append(
                UnifiedScreenTarget(
                    id=identifier,
                    label=screen_display_name(screen, self.screen_aliases),
                    kind="local",
                    online=True,
                    detail=f"HDMI / local display • {geometry.width()}x{geometry.height()}",
                )
            )
        for screen_id, state in sorted(self.remote_screens.items(), key=lambda item: friendly_remote_name(item[0], self.screen_aliases, item[1]).lower()):
            online = bool(state.get("online"))
            if not include_offline and not online:
                continue
            name = friendly_remote_name(screen_id, self.screen_aliases, state)
            size_text = ""
            if int(state.get("width") or 0) and int(state.get("height") or 0):
                size_text = f" • {int(state.get('width') or 0)}x{int(state.get('height') or 0)}"
            detail = f"LAN screen • {'Online' if online else 'Offline'}"
            if state.get("ip"):
                detail += f" • {state['ip']}"
            detail += size_text
            warning = ""
            if not online:
                warning = "This LAN screen has disconnected."
            targets.append(
                UnifiedScreenTarget(
                    id=screen_id,
                    label=name,
                    kind="remote",
                    online=online,
                    detail=detail,
                    warning=warning,
                )
            )
        return targets

    def update_network_summary(self) -> None:
        snapshot = self.backend_network_snapshot
        preferred = snapshot.get("preferred_url") or ""
        hostname_url = snapshot.get("hostname_url") or ""
        mdns_url = snapshot.get("mdns_url") or ""
        warnings = snapshot.get("warnings") or []
        firewall_configured = bool(snapshot.get("firewallConfigured"))
        preferred_interface = snapshot.get("preferred_interface") or ""
        interface_lines = [
            f"{item.get('label') or 'Network Interface'}: {item.get('ip') or ''}"
            for item in snapshot.get("interfaces") or []
            if item.get("ip")
        ]
        connected = sum(1 for item in self.remote_screens.values() if item.get("online"))
        total = len(self.remote_screens)
        if self.backend_online and preferred:
            self.network_status_primary.setText("Background engine online")
            self.network_status_secondary.setText(
                f"TV URL ready on port {snapshot.get('port')} • IP: {snapshot.get('preferred_ip') or 'Unavailable'}"
                + (f" • Interface: {preferred_interface}" if preferred_interface else "")
                + f" • Hostname: {snapshot.get('hostname') or 'Unavailable'}"
            )
            extra_urls = []
            if hostname_url:
                extra_urls.append(f"Host: {hostname_url}")
            if mdns_url:
                extra_urls.append(f"mDNS: {mdns_url}")
            url_suffix = " • ".join(extra_urls)
            self.network_url_label.setText(
                f"TV URL: {preferred}" + (f" • {url_suffix}" if url_suffix else "")
            )
        elif self.backend_online:
            self.network_status_primary.setText("Background engine online")
            detail = "LAN URL not ready. No reachable LAN IPv4 address was detected."
            if not firewall_configured:
                detail += " Firewall access still needs approval."
            self.network_status_secondary.setText(detail)
            fallback_urls = [item for item in [hostname_url, mdns_url] if item]
            if fallback_urls:
                self.network_url_label.setText("Try: " + " • ".join(fallback_urls))
            else:
                self.network_url_label.setText("Connect this PC to the same LAN as the TVs and allow local firewall access.")
        else:
            self.network_status_primary.setText("Background engine offline")
            self.network_status_secondary.setText("Start the engine to enable LAN screen connections.")
            self.network_url_label.setText("Use Engine > Start Engine to bring the background engine online.")
        if warnings:
            tip_lines = [str(item) for item in warnings]
            if interface_lines:
                tip_lines.extend(["", "Detected interfaces:"])
                tip_lines.extend(interface_lines)
            self.network_url_label.setToolTip("\n".join(tip_lines))
        else:
            tip_lines = [self.network_url_label.text()]
            if interface_lines:
                tip_lines.extend(["", "Detected interfaces:"])
                tip_lines.extend(interface_lines)
            self.network_url_label.setToolTip("\n".join(tip_lines))
        if self.backend_online:
            self.remote_screen_summary_label.setText(f"{connected} connected • {total} known")
        else:
            self.remote_screen_summary_label.setText("Engine offline")
        if self.backend_online and total:
            names = [
                f"{friendly_remote_name(screen_id, self.screen_aliases, state)} ({'online' if state.get('online') else 'offline'})"
                for screen_id, state in self.remote_screens.items()
            ]
            self.remote_screen_detail_label.setText("Registered LAN TVs are available alongside local HDMI screens.")
            self.remote_screen_detail_label.setToolTip("\n".join(names))
        elif self.backend_online:
            self.remote_screen_detail_label.setText("Open the TV URL on Whale OS screens to register them.")
            self.remote_screen_detail_label.setToolTip(self.network_url_label.text())

    def refresh_monitors(self, *_args) -> None:
        self.screen_target_menu.clear()
        self.rename_screens_menu.clear()
        self.remote_screens_menu.clear()
        self.schedule_screen_menu.clear()
        apply_all_action = self.schedule_screen_menu.addAction("Apply to All Selected Screens")
        apply_all_action.setCheckable(True)
        apply_all_action.setChecked(not self.current_form_screen_ids)
        apply_all_action.triggered.connect(lambda checked=False: self.set_schedule_screen_targets([]))
        self.schedule_screen_menu.addSeparator()
        for index, target in enumerate(self.unified_screen_targets(include_offline=True), start=1):
            label = target.label
            if target.kind == "local":
                local_screen = find_screen_by_id(target.id)
                if local_screen is not None:
                    geometry = local_screen.geometry()
                    label = f"{target.label} ({geometry.width()}x{geometry.height()})"
            else:
                label = f"{target.label} (LAN{' • offline' if not target.online else ''})"
            action = self.screen_target_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(target.id in self.selected_monitor_ids)
            action.setToolTip(target.warning or target.detail)
            action.triggered.connect(lambda checked=False, screen_id=target.id: self.toggle_screen_selection(screen_id))
            rename_action = self.rename_screens_menu.addAction(f"Rename Screen {index} - {target.label}")
            rename_action.triggered.connect(lambda checked=False, screen_id=target.id: self.rename_screen_alias(screen_id))
            schedule_action = self.schedule_screen_menu.addAction(label)
            schedule_action.setCheckable(True)
            schedule_action.setChecked(target.id in self.current_form_screen_ids)
            schedule_action.setToolTip(target.warning or target.detail)
            schedule_action.triggered.connect(lambda checked=False, screen_id=target.id: self.toggle_schedule_screen_selection(screen_id))
            if target.kind == "remote":
                remote_action = self.remote_screens_menu.addAction(label)
                remote_action.setEnabled(False)
                remote_action.setToolTip(target.warning or target.detail)
        self.update_monitor_summary()
        self.update_schedule_screen_summary()
        self.refresh_schedule_list()

    def toggle_screen_selection(self, screen_id: str) -> None:
        self.call_backend_action("toggle_screen_selection", {"screen_id": screen_id})

    def set_schedule_screen_targets(self, screen_ids: list[str]) -> None:
        self.current_form_screen_ids = screen_ids[:]
        self.refresh_monitors()

    def toggle_schedule_screen_selection(self, screen_id: str) -> None:
        if screen_id in self.current_form_screen_ids:
            self.current_form_screen_ids.remove(screen_id)
        else:
            self.current_form_screen_ids.append(screen_id)
        self.refresh_monitors()

    def rename_screen_alias(self, screen_id: str) -> None:
        screen = find_screen_by_id(screen_id)
        default_name = screen.name() if screen is not None else friendly_remote_name(screen_id, self.screen_aliases, self.remote_screens.get(screen_id))
        current_alias = self.screen_aliases.get(screen_id, "")
        new_name, accepted = QInputDialog.getText(
            self,
            "Name Screen",
            f"Name for {default_name}",
            text=current_alias,
        )
        if not accepted:
            return
        cleaned = new_name.strip()
        self.call_backend_action("rename_screen_alias", {"screen_id": screen_id, "alias": cleaned})

    def on_transition_changed(self, *_args) -> None:
        self.transition_method = str(self.transition_selector.currentData())
        self.call_backend_action("set_transition_method", {"transition_method": self.transition_method})

    def choose_quick_play_targets(self, media_path: Path) -> list[str] | None:
        targets = self.unified_screen_targets(include_offline=False)
        if not targets:
            self.set_form_message("No screens are available for quick play.", "error")
            return None
        dialog = QuickPlayTargetDialog(targets, self.selected_monitor_ids, media_path.name, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        target_ids = dialog.selected_screen_ids()
        if not target_ids:
            self.set_form_message("Choose at least one screen for quick play.", "error")
            return None
        return target_ids

    def handle_quick_play_drop(self, file_path: str) -> None:
        path = Path(file_path)
        if path.suffix.lower() not in SUPPORTED_MEDIA_EXTENSIONS:
            self.set_form_message("Dropped file is not a supported media file.", "error")
            return
        target_ids = self.choose_quick_play_targets(path)
        if not target_ids:
            return
        response = self.call_backend_action("quick_play", {"path": str(path), "target_screen_ids": target_ids})
        if response is not None:
            self.set_form_message(f"Quick play started with {path.name}.", "success")

    def browse_quick_play_media(self, *_args) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Choose Quick Play Media",
            str(Path.home()),
            "Media Files (*.mp4 *.mov *.m4v *.webm *.ogg *.jpg *.jpeg *.png *.webp *.bmp)",
        )
        if not selected:
            return
        self.handle_quick_play_drop(selected)

    def clear_quick_play_target(self) -> None:
        response = self.call_backend_action("clear_quick_play")
        if response is not None:
            self.set_form_message("Returned all quick-play screens to scheduled playback.", "success")

    def refresh_schedule_list(self) -> None:
        selected_id = self.current_edit_id
        self.schedule_list.blockSignals(True)
        self.schedule_list.clear()
        warning_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxCritical).pixmap(16, 16)
        screen_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon).pixmap(16, 16)
        for entry in sorted(self.schedules, key=lambda item: ScheduleEntry.time_to_minutes(item.start_time)):
            item = QListWidgetItem()
            missing_screen_ids = []
            for screen_id in entry.screen_ids:
                if is_remote_screen_id(screen_id):
                    remote_state = self.remote_screens.get(screen_id)
                    if remote_state is None or not remote_state.get("online"):
                        missing_screen_ids.append(screen_id)
                elif find_screen_by_id(screen_id) is None:
                    missing_screen_ids.append(screen_id)
            if not entry.screen_ids:
                screens_tooltip = "Assigned screens\nAll selected global playback screens."
            else:
                assigned_labels = [
                    friendly_remote_name(screen_id, self.screen_aliases, self.remote_screens.get(screen_id))
                    if is_remote_screen_id(screen_id)
                    else screen_label_from_id(screen_id, self.screen_aliases)
                    for screen_id in entry.screen_ids
                ]
                screens_tooltip = "Assigned screens\n" + "\n".join(assigned_labels)
            if missing_screen_ids:
                missing_labels = [
                    friendly_remote_name(screen_id, self.screen_aliases, self.remote_screens.get(screen_id))
                    if is_remote_screen_id(screen_id)
                    else screen_label_from_id(screen_id, self.screen_aliases)
                    for screen_id in missing_screen_ids
                ]
                warning_tooltip = "One or more attached screens are disconnected.\n\n" + "\n".join(missing_labels)
                row_widget = ScheduleSlotRow(
                    entry.title,
                    f"{entry.days_label} • {entry.range_label}",
                    screens_tooltip,
                    screen_icon,
                    warning_icon,
                    warning_tooltip,
                    self.schedule_list,
                )
                item.setToolTip(warning_tooltip)
            else:
                row_widget = ScheduleSlotRow(
                    entry.title,
                    f"{entry.days_label} • {entry.range_label}",
                    screens_tooltip,
                    screen_icon,
                    None,
                    "",
                    self.schedule_list,
                )
                item.setToolTip(screens_tooltip)
            item.setData(Qt.ItemDataRole.UserRole, entry.id)
            self.schedule_list.addItem(item)
            item.setSizeHint(QSize(0, 58))
            self.schedule_list.setItemWidget(item, row_widget)
            if entry.id == selected_id:
                self.schedule_list.setCurrentItem(item)
        self.schedule_list.blockSignals(False)
        self.update_library_summary()

    def load_schedule_into_form(self, schedule_id: str | None) -> None:
        self.current_edit_id = schedule_id
        entry = next((item for item in self.schedules if item.id == schedule_id), None)
        if entry is None:
            self.title_input.setText("")
            self.set_time_button_values(QTime.fromString("00:00", "HH:mm"), QTime.fromString("01:00", "HH:mm"))
            for checkbox in self.day_checkboxes.values():
                checkbox.setChecked(True)
            self.current_form_screen_ids = []
            self.current_selected_video_file = ""
            self.update_media_source_summary()
            self.update_schedule_screen_summary()
            self.refresh_monitors()
            self.delete_schedule_button.setEnabled(False)
            return

        self.title_input.setText(entry.title)
        self.set_time_button_values(QTime.fromString(entry.start_time, "HH:mm"), QTime.fromString(entry.end_time, "HH:mm"))
        selected_days = set(entry.selected_days())
        for day_key, checkbox in self.day_checkboxes.items():
            checkbox.setChecked(day_key in selected_days)
        self.current_form_screen_ids = entry.screen_ids[:]
        self.current_selected_video_file = entry.video_file
        self.update_schedule_screen_summary()
        self.refresh_monitors()
        self.update_media_source_summary()
        self.delete_schedule_button.setEnabled(True)
        self.select_schedule_item_by_id(schedule_id)

    def select_schedule_item_by_id(self, schedule_id: str | None) -> None:
        if schedule_id is None:
            self.schedule_list.clearSelection()
            return
        for row in range(self.schedule_list.count()):
            item = self.schedule_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == schedule_id:
                self.schedule_list.setCurrentItem(item)
                break

    def set_form_message(self, text: str, kind: str = "") -> None:
        styles = {
            "error": (
                "#8e1f1f",
                "#ffe4e4",
                "#ef9a9a",
            ),
            "success": (
                "#0f5132",
                "#dff7ec",
                "#8fd3b4",
            ),
            "": (
                "#173043",
                "rgba(255,255,255,0.76)",
                "rgba(23,48,67,0.12)",
            ),
        }
        color, background, border = styles.get(kind, styles[""])
        self.form_message_label.setStyleSheet(
            f"color: {color}; background: {background}; border: 1px solid {border};"
            " border-radius: 14px; padding: 12px 14px;"
        )
        self.form_message_label.setText(text)

    def choose_video_folder(self, *_args) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "Choose Media Folder",
            str(self.video_directory or Path.home()),
        )
        if not selected:
            return
        response = self.call_backend_action("set_media_folder", {"path": selected})
        if response is not None:
            self.set_form_message(f"Media folder set to {selected}.", "success")

    def refresh_video_library(self, *_args) -> None:
        self.call_backend_action("refresh_media", show_errors=False)

    def update_media_source_summary(self) -> None:
        file_name = self.current_selected_video_file
        if file_name:
            self.choose_media_button.setText("Change Media")
            self.choose_media_button.setToolTip(f"Selected media: {file_name}")
        else:
            entry = next((item for item in self.schedules if item.id == self.current_edit_id), None)
            if entry is not None and entry.video_file:
                self.choose_media_button.setText("Change Media")
                self.choose_media_button.setToolTip(f"Current media: {entry.video_label or entry.video_file}")
            else:
                self.choose_media_button.setText("Choose Media")
                self.choose_media_button.setToolTip("Choose a media file from the selected folder.")
        self.update_selection_summary()

    def open_media_picker(self) -> None:
        if self.video_directory is None:
            self.set_form_message("Set the media folder first from the Library menu.", "error")
            return
        if self.backend_library_scanning and not self.available_videos:
            self.set_form_message("Media library is still indexing. Try again in a moment.", "error")
            return
        if not self.available_videos:
            message = "No supported media files were found in the selected folder."
            if self.backend_library_error:
                message = f"Media indexing failed: {self.backend_library_error}"
            self.set_form_message(message, "error")
            return
        initial_selection = self.current_selected_video_file
        if not initial_selection:
            entry = next((item for item in self.schedules if item.id == self.current_edit_id), None)
            initial_selection = entry.video_file if entry is not None else ""
        dialog = MediaPickerDialog(self.available_videos, self.video_directory, initial_selection, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if dialog.selected_path:
            self.current_selected_video_file = dialog.selected_path
            self.update_media_source_summary()
            self.set_form_message(f"Media selected: {dialog.selected_path}", "success")

    def set_time_button_values(self, start_time: QTime, end_time: QTime) -> None:
        self.start_time_input.setTime(start_time)
        self.end_time_input.setTime(end_time)
        self.start_time_button.setText(start_time.toString("HH:mm"))
        self.end_time_button.setText(end_time.toString("HH:mm"))

    def open_time_picker(self, field: str) -> None:
        current_time = self.start_time_input.time() if field == "start" else self.end_time_input.time()
        anchor_button = self.start_time_button if field == "start" else self.end_time_button
        dialog = TimePickerDialog("Choose Time", current_time, self)
        popup_origin = anchor_button.mapToGlobal(anchor_button.rect().bottomLeft())
        dialog.move(popup_origin + QPoint(0, 6))
        dialog.exec()
        chosen_time = dialog.current_time()
        if field == "start":
            self.set_time_button_values(chosen_time, self.end_time_input.time())
        else:
            self.set_time_button_values(self.start_time_input.time(), chosen_time)

    def apply_video_directory_watch(self) -> None:
        existing_paths = self.folder_watcher.directories()
        if existing_paths:
            self.folder_watcher.removePaths(existing_paths)
        if self.video_directory is not None and self.video_directory.exists():
            self.folder_watcher.addPath(str(self.video_directory))

    def on_video_directory_changed(self, path: str) -> None:
        self.folder_refresh_timer.start()

    def on_schedule_selected(self, current: QListWidgetItem | None, previous: QListWidgetItem | None) -> None:
        if current is None:
            return
        self.load_schedule_into_form(str(current.data(Qt.ItemDataRole.UserRole)))

    def launch_selected_monitors(self, checked: bool = False, silent: bool = False) -> None:
        response = self.call_backend_action("launch_selected")
        if response is not None and not silent:
            self.set_form_message("Playback started.", "success")

    def build_candidate_from_form(self) -> ScheduleEntry:
        existing = next((item for item in self.schedules if item.id == self.current_edit_id), None)
        schedule_id = self.current_edit_id or uuid.uuid4().hex
        title = self.title_input.text().strip()
        fallback_title = title
        selected_video_file = self.current_selected_video_file or (existing.video_file if existing is not None else "")
        if not fallback_title and selected_video_file:
            fallback_title = Path(selected_video_file).stem
        elif not fallback_title and existing is not None:
            fallback_title = existing.title
        elif not fallback_title:
            fallback_title = "Untitled schedule"

        video_file = selected_video_file
        video_label = selected_video_file if selected_video_file else ""

        return ScheduleEntry(
            id=schedule_id,
            title=fallback_title,
            start_time=self.start_time_input.time().toString("HH:mm"),
            end_time=self.end_time_input.time().toString("HH:mm"),
            video_file=video_file,
            video_label=video_label,
            screen_ids=self.current_form_screen_ids[:],
            days=[day_key for day_key, checkbox in self.day_checkboxes.items() if checkbox.isChecked()],
        )

    def save_schedule(self) -> None:
        try:
            candidate = self.build_candidate_from_form()
            response = self.call_backend_action("save_schedule", {"schedule": candidate.to_dict()})
            if response is not None:
                self.current_edit_id = candidate.id
                self.set_form_message(
                    f"{candidate.title} is now mapped to {candidate.video_label or candidate.video_file}.",
                    "success",
                )
        except Exception as error:  # noqa: BLE001
            self.set_form_message(str(error), "error")

    def delete_selected_schedule(self) -> None:
        if self.current_edit_id is None:
            return
        current_id = self.current_edit_id
        response = self.call_backend_action("delete_schedule", {"schedule_id": current_id})
        if response is not None:
            self.current_edit_id = None
            self.set_form_message("Schedule deleted.", "success")

    def apply_backend_snapshot(self, snapshot: dict[str, Any]) -> None:
        should_reload_form = False
        self.backend_online = True
        self.backend_state_version = int(snapshot.get("stateVersion") or self.backend_state_version)
        self.backend_listener.seed_state_version(self.backend_state_version)
        schedules_payload = snapshot.get("schedules") or []
        self.schedules = [ScheduleEntry.from_dict(item) for item in schedules_payload if isinstance(item, dict)]
        self.selected_monitor_ids = [str(item) for item in snapshot.get("selectedMonitorIds") or []]
        video_directory = str(snapshot.get("videoDirectory") or "").strip()
        self.video_directory = Path(video_directory).expanduser() if video_directory else None
        if self.video_directory is not None and not self.video_directory.exists():
            self.video_directory = None
        self.screen_aliases = {str(key): str(value) for key, value in (snapshot.get("screenAliases") or {}).items()}
        self.transition_method = str(snapshot.get("transitionMethod") or "fade_black")
        self.run_at_startup = bool(snapshot.get("runAtStartup"))
        available_media = snapshot.get("availableMedia") or []
        if self.video_directory is not None:
            self.available_videos = [self.video_directory / str(item.get("relativePath") or "") for item in available_media if item.get("relativePath")]
        else:
            self.available_videos = []
        self.remote_screens = {
            str(item.get("screen_id")): item
            for item in snapshot.get("remoteScreens") or []
            if isinstance(item, dict) and item.get("screen_id")
        }
        self.backend_network_snapshot = dict(snapshot.get("network") or {})
        library = snapshot.get("library") or {}
        self.backend_library_scanning = bool(library.get("scanning"))
        self.backend_library_error = str(library.get("error") or "")

        playback = snapshot.get("playback") or {}
        self.backend_playback_enabled = bool(playback.get("enabled"))
        self.backend_paused = bool(playback.get("paused"))
        self.backend_window_count = int(playback.get("localWindowCount") or 0)

        self.run_at_startup_action.blockSignals(True)
        self.run_at_startup_action.setChecked(self.run_at_startup)
        self.run_at_startup_action.blockSignals(False)

        transition_index = self.transition_selector.findData(self.transition_method)
        if transition_index >= 0 and transition_index != self.transition_selector.currentIndex():
            self.transition_selector.blockSignals(True)
            self.transition_selector.setCurrentIndex(transition_index)
            self.transition_selector.blockSignals(False)

        existing_ids = {entry.id for entry in self.schedules}
        if self.current_edit_id and self.current_edit_id not in existing_ids:
            self.current_edit_id = None
            should_reload_form = True
        if self.current_edit_id is None and self.schedules:
            self.current_edit_id = self.schedules[0].id
            should_reload_form = True

        self.refresh_monitors()
        if should_reload_form:
            self.load_schedule_into_form(self.current_edit_id)
        status = snapshot.get("status") or {}
        self.update_status_labels(str(status.get("clockLabel") or "--:--:--"), str(status.get("activeLabel") or "No active schedule"))
        self.update_playback_state_label(self.backend_paused, self.backend_window_count)
        self.update_network_summary()

    def update_status_labels(self, clock_label: str, active_label: str) -> None:
        if not hasattr(self, "footer_time_primary"):
            return
        self.footer_time_primary.setText(clock_label)
        if active_label == "Playback stopped":
            self.hero_title_label.setText("Playback stopped")
            self.hero_subtitle_label.setText("Screens remain idle until you launch playback again.")
            self.update_hero_progress(0.0)
            return
        now = current_uk_datetime()
        weekday_index = now.weekday()
        minute_of_day = (now.hour * 60) + now.minute
        active_entry = next(
            (
                entry
                for screen_id in self.selected_monitor_ids
                if (entry := active_schedule_for_screen(self.schedules, weekday_index, minute_of_day, screen_id)) is not None
            ),
            active_schedule_for_minute(self.schedules, weekday_index, minute_of_day),
        )
        if active_entry is None:
            self.hero_title_label.setText(active_label if active_label != "No active schedule" else "No active slot")
            self.hero_subtitle_label.setText("Waiting for a matching UK-time schedule.")
            self.update_hero_progress(0.0)
        else:
            self.hero_title_label.setText(f"{active_entry.title} • {active_entry.range_label} • {active_entry.video_label or active_entry.video_file}")
            self.hero_subtitle_label.setText(
                f"Transition: {TRANSITION_METHODS.get(self.transition_method, 'Fade Through Black')} • "
                f"Media folder: {self.video_directory if self.video_directory is not None else 'Not set'}"
            )
            self.update_hero_progress(schedule_progress(active_entry, now))

    def update_playback_state_label(self, paused: bool, window_count: int) -> None:
        if not hasattr(self, "footer_active_primary"):
            return
        remote_active = sum(
            1
            for state in self.remote_screens.values()
            if state.get("online") and str(state.get("state") or "") in {"playing", "connected"} and str(state.get("current_media") or "")
        )
        total_active = window_count + remote_active
        self.current_active_screen_count = total_active
        state = "paused" if paused else "active"
        self.footer_active_primary.setText(f"{total_active} {state}")
        if total_active:
            self.footer_active_secondary.setText(
                "Outputs are paused." if paused else "Screens displaying now"
            )
        else:
            self.footer_active_secondary.setText("No active playback")
        if total_active == 0:
            active_summary = "0 screen(s) are currently active."
        elif paused:
            active_summary = f"{total_active} screen(s) are currently paused."
        else:
            active_summary = f"{total_active} screen(s) are currently displaying media."
        active_tooltip = (
            "Active screens\n"
            f"{active_summary}\n"
            f"Includes {window_count} local window(s) and {remote_active} LAN screen(s)."
        )
        for widget in (
            getattr(self, "footer_active_icon", None),
            getattr(self, "footer_active_primary", None),
            getattr(self, "footer_active_secondary", None),
        ):
            if widget is not None:
                widget.setToolTip(active_tooltip)

    def update_monitor_summary(self) -> None:
        total = len(available_screens()) + len(self.remote_screens)
        selected = len(self.selected_monitor_ids)
        if hasattr(self, "footer_total_primary"):
            self.footer_total_primary.setText(f"{total} available")
            self.footer_total_secondary.setText("Local + LAN screens")
        if hasattr(self, "footer_global_primary"):
            self.footer_global_primary.setText(f"{selected} global")
            self.footer_global_secondary.setText("Default playback targets")
        total_tooltip = (
            f"Available screens\n"
            f"{len(available_screens())} local screen(s) detected on this computer.\n"
            f"{len(self.remote_screens)} LAN screen(s) known to the controller."
        )
        for widget in (
            getattr(self, "footer_total_icon", None),
            getattr(self, "footer_total_primary", None),
            getattr(self, "footer_total_secondary", None),
        ):
            if widget is not None:
                widget.setToolTip(total_tooltip)
        if total == 0:
            global_tooltip = "Global playback screens\nNo screens were detected."
        elif selected == 0:
            global_tooltip = (
                "Global playback screens\n"
                f"0 of {total} screen(s) are selected as default playback targets.\n"
                "Schedules can still target LAN screens directly."
            )
        else:
            selected_names = [
                (
                    friendly_remote_name(screen_id, self.screen_aliases, self.remote_screens.get(screen_id))
                    if is_remote_screen_id(screen_id)
                    else screen_display_name(screen, self.screen_aliases)
                )
                for screen_id in self.selected_monitor_ids
                if is_remote_screen_id(screen_id) or (screen := find_screen_by_id(screen_id)) is not None
            ]
            global_tooltip = (
                "Global playback screens\n"
                f"{selected} of {total} screen(s) are selected as default playback targets.\n"
                "Schedules can still target LAN screens directly.\n\n"
                + "\n".join(selected_names)
            )
        for widget in (
            getattr(self, "footer_global_icon", None),
            getattr(self, "footer_global_primary", None),
            getattr(self, "footer_global_secondary", None),
        ):
            if widget is not None:
                widget.setToolTip(global_tooltip)
        if hasattr(self, "footer_active_primary"):
            self.footer_active_primary.setText(f"{self.current_active_screen_count} active")
            if self.current_active_screen_count == 0:
                self.footer_active_secondary.setText("No active playback")
            elif self.backend_paused:
                self.footer_active_secondary.setText("Windows are paused")
            else:
                self.footer_active_secondary.setText("Screens displaying now")
        return

    def update_schedule_screen_summary(self) -> None:
        if not hasattr(self, "schedule_screen_summary_label"):
            return
        target_label = format_screen_targets(self.current_form_screen_ids, self.screen_aliases)
        self.schedule_screen_summary_label.setText(target_label)
        if hasattr(self, "schedule_screen_button"):
            self.schedule_screen_button.setText(
                "Target Screens"
                if not self.current_form_screen_ids
                else f"Target Screens ({len(self.current_form_screen_ids)})"
            )
        self.update_selection_summary()

    def update_library_summary(self) -> None:
        if self.video_directory is None:
            self.choose_media_button.setEnabled(True)
            existing_tooltip = self.choose_media_button.toolTip().strip()
            if existing_tooltip in {"", "Choose a media file from the selected folder."}:
                self.choose_media_button.setToolTip("No media folder selected. Use Library > Set Media Folder.")
            return
        unique_files = sorted(referenced_video_files(self.schedules))
        available_count = len(self.available_videos)
        category_count = len({media_category_label(path, self.video_directory) for path in self.available_videos})
        existing_tooltip = self.choose_media_button.toolTip().strip()
        if self.backend_library_scanning:
            self.choose_media_button.setToolTip(
                f"Indexing media library... {available_count} file(s) discovered so far."
            )
            return
        if self.backend_library_error:
            self.choose_media_button.setToolTip(f"Media indexing error: {self.backend_library_error}")
            return
        if not self.current_selected_video_file and "Current media:" not in existing_tooltip and "Selected media:" not in existing_tooltip:
            self.choose_media_button.setToolTip(
                f"{available_count} media file(s) across {category_count} folder group(s) • {len(unique_files)} linked."
            )

    def update_selection_summary(self) -> None:
        if not hasattr(self, "schedule_screen_button"):
            return
        target_label = format_screen_targets(self.current_form_screen_ids, self.screen_aliases)
        transition_label = TRANSITION_METHODS.get(self.transition_method, "Fade Through Black")
        self.schedule_screen_button.setToolTip(f"Target screens: {target_label}")
        self.transition_selector.setToolTip(f"Transition: {transition_label}")

    def closeEvent(self, event: QCloseEvent) -> None:  # type: ignore[override]
        self.prepare_for_exit(stop_engine=True)
        self._allow_real_quit = True
        event.accept()
        super().closeEvent(event)

    def changeEvent(self, event: QEvent) -> None:  # type: ignore[override]
        super().changeEvent(event)
        if hasattr(self, "fullscreen_action"):
            self.fullscreen_action.blockSignals(True)
            self.fullscreen_action.setChecked(self.isFullScreen())
            self.fullscreen_action.blockSignals(False)
        if (
            event.type() == QEvent.Type.WindowStateChange
            and self.isMinimized()
            and not self._allow_real_quit
            and self.tray_icon is not None
            and self.tray_icon.isVisible()
        ):
            QTimer.singleShot(0, self.send_to_background)
        if event.type() == QEvent.Type.WindowStateChange:
            self.schedule_layout_refresh()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self.update_hero_progress(self._hero_progress)
        self.schedule_layout_refresh()

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        self.schedule_layout_refresh()

    def update_hero_progress(self, progress: float) -> None:
        self._hero_progress = max(0.0, min(progress, 1.0))
        track_width = self.hero_progress_track.width()
        fill_width = max(10 if self._hero_progress > 0 else 0, int(track_width * self._hero_progress))
        self.hero_progress_fill.setGeometry(0, 0, fill_width, self.hero_progress_track.height())

    def schedule_layout_refresh(self) -> None:
        for delay in (0, 40, 120, 260):
            QTimer.singleShot(delay, self.refresh_responsive_layout)

    def refresh_responsive_layout(self) -> None:
        if not hasattr(self, "content_splitter"):
            return
        screen = self.windowHandle().screen() if self.windowHandle() is not None else self.screen()
        screen_width = 0
        if screen is not None:
            screen_width = screen.availableGeometry().width()
        width = max(self.width(), 1)
        if self.isFullScreen() or self.isMaximized():
            width = max(width, screen_width)
        if self.isFullScreen() or width >= 1600:
            mode = "wide"
            left_width = min(max(300, int(width * 0.24)), 420)
        elif width >= 1200:
            mode = "standard"
            left_width = min(max(300, int(width * 0.27)), 400)
        else:
            mode = "compact"
            left_width = min(max(280, int(width * 0.32)), 360)
        right_width = max(620, width - left_width - 96)
        should_update = (
            mode != self._layout_mode
            or abs(width - self._last_layout_width) > 24
        )
        if should_update:
            self.content_splitter.setSizes([left_width, right_width])
            self._layout_mode = mode
            self._last_layout_width = width
        self.content_splitter.updateGeometry()
        self.content_splitter.update()
        self.schedule_screen_summary_label.updateGeometry()
        self.schedule_screen_button.updateGeometry()
        self.choose_media_button.updateGeometry()
        self.transition_selector.updateGeometry()
        if self.centralWidget() is not None and self.centralWidget().layout() is not None:
            self.centralWidget().layout().activate()


def current_uk_time_status(schedules: list[ScheduleEntry]) -> tuple[str, str]:
    clock_label, minute_of_day, weekday_index = current_uk_time()
    entry = active_schedule_for_minute(schedules, weekday_index, minute_of_day)
    if entry is None:
        return clock_label, "No active schedule"
    return clock_label, f"{entry.title} • {entry.range_label} • {entry.video_label or entry.video_file}"


def acquire_engine_instance_lock() -> Any | None:
    ensure_app_paths()
    lock_path = APP_DATA_DIR / "engine.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")
    try:
        if sys.platform == "win32":
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()).encode("utf-8"))
        handle.flush()
        return handle
    except OSError:
        handle.close()
        return None


def release_engine_instance_lock(handle: Any | None) -> None:
    if handle is None:
        return
    try:
        if sys.platform == "win32":
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        handle.close()
    except OSError:
        pass


def engine_lock_path() -> Path:
    ensure_app_paths()
    return APP_DATA_DIR / "engine.lock"


def read_engine_lock_pid() -> int | None:
    lock_path = engine_lock_path()
    if not lock_path.exists():
        return None
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def is_process_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                check=False,
                **windows_hidden_subprocess_kwargs(),
            )
            if result.returncode != 0:
                return False
            output = result.stdout.strip()
            return bool(output) and "No tasks are running" not in output
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def remove_stale_engine_lock() -> bool:
    pid = read_engine_lock_pid()
    if pid is None or is_process_running(pid):
        return False
    try:
        engine_lock_path().unlink(missing_ok=True)
    except OSError:
        return False
    return True


def discover_engine_process_ids() -> list[int]:
    if sys.platform != "win32":
        return []
    script_path = str((APP_ROOT / "main.py").resolve())
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "$script = [regex]::Escape('" + script_path.replace("'", "''") + "'); "
                    "Get-CimInstance Win32_Process | "
                    "Where-Object { $_.Name -match 'pythonw?\\.exe' -and $_.CommandLine -match $script -and $_.CommandLine -match '--engine' } | "
                    "Select-Object -ExpandProperty ProcessId"
                ),
            ],
            capture_output=True,
            text=True,
            check=False,
            **windows_hidden_subprocess_kwargs(),
        )
    except OSError:
        return []
    if result.returncode != 0:
        return []
    pids: list[int] = []
    for line in (result.stdout or "").splitlines():
        cleaned = line.strip()
        if not cleaned:
            continue
        try:
            pids.append(int(cleaned))
        except ValueError:
            continue
    return pids


def known_engine_process_ids() -> list[int]:
    candidates: list[int] = []
    pid_from_lock = read_engine_lock_pid()
    if pid_from_lock is not None:
        candidates.append(pid_from_lock)
    candidates.extend(discover_engine_process_ids())
    unique: list[int] = []
    for pid in candidates:
        if pid > 0 and pid not in unique:
            unique.append(pid)
    return unique


def stop_known_engine_processes() -> bool:
    stopped_any = False
    for pid in known_engine_process_ids():
        if terminate_engine_process(pid):
            stopped_any = True
    if stopped_any:
        time.sleep(0.6)
        try:
            engine_lock_path().unlink(missing_ok=True)
        except OSError:
            pass
    return stopped_any


def terminate_engine_process(pid: int) -> bool:
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                check=False,
                **windows_hidden_subprocess_kwargs(),
            )
            return result.returncode == 0
        os.kill(pid, 15)
        return True
    except OSError:
        return False


def stop_engine_from_lock() -> bool:
    pid = read_engine_lock_pid()
    stopped = False
    if pid is not None:
        stopped = terminate_engine_process(pid)
    if not stopped:
        stopped = stop_known_engine_processes()
    if stopped:
        try:
            engine_lock_path().unlink(missing_ok=True)
        except OSError:
            pass
    return stopped


class ControllerApiClient:
    def __init__(self, port: int = ENGINE_CONTROL_PORT) -> None:
        self.base_url = f"http://127.0.0.1:{port}"

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None, timeout: float = 5.0) -> dict[str, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(f"{self.base_url}{path}", data=data, method=method, headers=headers)
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
        result = json.loads(raw) if raw else {}
        return result if isinstance(result, dict) else {}

    def ping(self) -> bool:
        try:
            response = self._request("GET", "/api/ping", timeout=0.75)
        except Exception:  # noqa: BLE001
            return False
        return bool(response.get("ok"))

    def get_state(self) -> dict[str, Any]:
        return self._request("GET", "/api/state")

    def wait_for_state_update(self, since: int, timeout: float = 30.0) -> dict[str, Any]:
        timeout_ms = max(1000, int(timeout * 1000))
        return self._request("GET", f"/api/subscribe?since={int(since)}&timeout_ms={timeout_ms}", timeout=timeout + 5.0)

    def action(self, action: str, payload: dict[str, Any] | None = None, timeout: float = 5.0) -> dict[str, Any]:
        return self._request("POST", "/api/action", {"action": action, "payload": payload or {}}, timeout=timeout)


class BackendStateListener(QObject):
    snapshot_received = Signal(dict)
    connection_changed = Signal(bool, str)

    def __init__(self, client: ControllerApiClient, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.client = client
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._cursor_lock = threading.Lock()
        self._last_version = 0
        self._online = False
        self._consecutive_failures = 0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="backend-state-listener", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None

    def seed_state_version(self, version: int) -> None:
        with self._cursor_lock:
            self._last_version = max(0, int(version))

    def _emit_connection(self, online: bool, detail: str = "") -> None:
        if self._online == online and not detail:
            return
        self._online = online
        self.connection_changed.emit(online, detail)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            with self._cursor_lock:
                since = self._last_version
            try:
                response = self.client.wait_for_state_update(since, timeout=20.0)
                self._consecutive_failures = 0
                version = int(response.get("stateVersion") or since)
                with self._cursor_lock:
                    self._last_version = max(self._last_version, version)
                snapshot = response.get("snapshot")
                if response.get("updated") and isinstance(snapshot, dict):
                    self.snapshot_received.emit(snapshot)
                self._emit_connection(True, "")
            except Exception as error:  # noqa: BLE001
                self._consecutive_failures += 1
                if self.client.ping():
                    self._emit_connection(True, "")
                    self._stop_event.wait(0.2)
                    continue
                if self._consecutive_failures >= 2:
                    self._emit_connection(False, format_request_error(error))
                self._stop_event.wait(1.0)


class EngineControlServer:
    def __init__(self, engine: "ControllerEngine", port: int = ENGINE_CONTROL_PORT) -> None:
        self.engine = engine
        self.port = port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.server_error = ""

    def start(self) -> None:
        if self._httpd is not None:
            return

        server_ref = self

        class _Server(ThreadingHTTPServer):
            allow_reuse_address = True

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                server_ref._handle_get(self)

            def do_POST(self) -> None:  # noqa: N802
                server_ref._handle_post(self)

            def log_message(self, format: str, *args) -> None:  # noqa: A003
                return

        try:
            self._httpd = _Server(("127.0.0.1", self.port), Handler)
        except OSError as error:
            self.server_error = str(error)
            self._httpd = None
            raise RuntimeError(f"Engine control server failed to bind to 127.0.0.1:{self.port}: {error}") from error
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="controller-engine-api", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        httpd = self._httpd
        self._httpd = None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None

    def _write_json(self, handler: BaseHTTPRequestHandler, payload: dict[str, Any], status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        handler.send_response(int(status))
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(body)

    def _read_json(self, handler: BaseHTTPRequestHandler) -> dict[str, Any]:
        length = int(handler.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = handler.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _handle_get(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urlparse(handler.path)
        try:
            if parsed.path == "/api/ping":
                self._write_json(handler, {"ok": True, "server": "engine"})
                return
            if parsed.path == "/api/state":
                self._write_json(handler, self.engine.run_on_engine_thread(self.engine.snapshot))
                return
            if parsed.path == "/api/subscribe":
                params = parse_qs(parsed.query)
                try:
                    since = max(0, int(str((params.get("since") or ["0"])[0])))
                except ValueError:
                    since = 0
                try:
                    timeout_ms = int(str((params.get("timeout_ms") or ["20000"])[0]))
                except ValueError:
                    timeout_ms = 20000
                timeout_seconds = min(max(timeout_ms / 1000.0, 1.0), 30.0)
                updated, version = self.engine.wait_for_state_update(since, timeout_seconds)
                payload: dict[str, Any] = {
                    "ok": True,
                    "updated": updated,
                    "stateVersion": version,
                }
                if updated:
                    payload["snapshot"] = self.engine.run_on_engine_thread(self.engine.snapshot)
                self._write_json(handler, payload)
                return
            self._write_json(handler, {"ok": False, "error": "not_found"}, HTTPStatus.NOT_FOUND)
        except Exception as error:  # noqa: BLE001
            append_engine_startup_log(f"Engine control GET failure on {parsed.path}: {error}")
            self._write_json(handler, {"ok": False, "error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_post(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urlparse(handler.path)
        if parsed.path != "/api/action":
            self._write_json(handler, {"ok": False, "error": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        request = self._read_json(handler)
        action = str(request.get("action") or "").strip()
        payload = request.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        try:
            result = self.engine.run_on_engine_thread(lambda: self.engine.handle_action(action, payload))
        except Exception as error:  # noqa: BLE001
            result = {
                "ok": False,
                "error": str(error),
                "snapshot": self.engine.run_on_engine_thread(self.engine.snapshot),
            }
        self._write_json(handler, result)


class ControllerEngine(QObject):
    media_scan_completed = Signal(object, int, str)
    remote_server_changed = Signal()
    invoke_requested = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._state_condition = threading.Condition()
        self._state_version = 0
        self._media_scan_lock = threading.Lock()
        self._media_scan_generation = 0
        self._media_scan_target_dir: Path | None = None
        self._media_scan_active = False
        self.media_scan_in_progress = False
        self.media_scan_error = ""
        config = load_config()
        self.schedules = [ScheduleEntry.from_dict(item) for item in config["schedules"]]
        self.selected_monitor_ids = [str(item) for item in config["selected_monitor_ids"]]
        self.video_directory: Path | None = Path(config["video_directory"]).expanduser() if config["video_directory"] else None
        if self.video_directory is not None and not self.video_directory.exists():
            self.video_directory = None
        self.screen_aliases = {str(key): str(value) for key, value in config["screen_aliases"].items()}
        self.transition_method = str(config["transition_method"])
        self.run_at_startup = bool(config["run_at_startup"])
        self.available_videos: list[Path] = []
        self.remote_screens: dict[str, dict[str, Any]] = {}
        self.status_clock_label, self.status_active_label = current_uk_time_status(self.schedules)
        self.local_window_count = 0
        self.local_playback_workers: dict[str, subprocess.Popen] = {}
        self.playback_paused = False
        self.media_scan_completed.connect(self.on_media_scan_completed)
        self.remote_server_changed.connect(self.refresh_remote_screens)
        self.invoke_requested.connect(self._run_invocation)
        self.folder_watcher = QFileSystemWatcher(self)
        self.folder_watcher.directoryChanged.connect(self.on_video_directory_changed)
        self.folder_refresh_timer = QTimer(self)
        self.folder_refresh_timer.setSingleShot(True)
        self.folder_refresh_timer.setInterval(350)
        self.folder_refresh_timer.timeout.connect(self.refresh_video_library)
        self.remote_refresh_timer = QTimer(self)
        self.remote_refresh_timer.setInterval(1500)
        self.remote_refresh_timer.timeout.connect(self.refresh_remote_screens)
        self.firewall_check_timer = QTimer(self)
        self.firewall_check_timer.setInterval(60000)
        self.firewall_check_timer.timeout.connect(self.ensure_firewall_access)
        self.coordinator = PlaybackCoordinator()
        self.coordinator.manage_local_windows = False
        self.coordinator.status_changed.connect(self.on_status_changed)
        self.coordinator.playback_state_changed.connect(self.on_playback_state_changed)
        self.coordinator.commands_changed.connect(self.sync_playback_outputs)
        self.coordinator.set_schedules(self.schedules)
        self.coordinator.set_selected_monitors(self.selected_monitor_ids)
        self.coordinator.set_video_directory(self.video_directory)
        self.coordinator.set_transition_method(self.transition_method)
        self.remote_server = LanRemoteServer()
        self.remote_server.set_change_callback(self.remote_server_changed.emit)
        self.remote_server.set_media_root(self.video_directory)
        self.remote_server.start()
        self.control_server = EngineControlServer(self)
        self.control_server.start()
        self.apply_video_directory_watch()
        self.refresh_video_library()
        self.sync_playback_outputs()
        self.refresh_remote_screens()
        self.ensure_firewall_access(force_retry=False)
        self.sync_startup_registration()
        self.remote_refresh_timer.start()
        self.firewall_check_timer.start()

    def on_status_changed(self, clock_label: str, active_label: str) -> None:
        self.status_clock_label = clock_label
        self.status_active_label = active_label

    def on_playback_state_changed(self, paused: bool, window_count: int) -> None:
        self.playback_paused = paused
        self.local_window_count = len(self.local_playback_workers) if not self.coordinator.manage_local_windows else window_count
        self.notify_state_changed()

    def _run_invocation(self, callback) -> None:
        callback()

    def run_on_engine_thread(self, callback):
        if threading.current_thread() is threading.main_thread():
            return callback()
        completed = threading.Event()
        outcome: dict[str, Any] = {}

        def invoke() -> None:
            try:
                outcome["result"] = callback()
            except Exception as error:  # noqa: BLE001
                outcome["error"] = error
            finally:
                completed.set()

        self.invoke_requested.emit(invoke)
        if not completed.wait(10.0):
            raise TimeoutError("The engine UI thread did not process the request in time.")
        if "error" in outcome:
            raise outcome["error"]
        return outcome.get("result")

    def notify_state_changed(self) -> int:
        with self._state_condition:
            self._state_version += 1
            version = self._state_version
            self._state_condition.notify_all()
        return version

    def wait_for_state_update(self, since: int, timeout: float) -> tuple[bool, int]:
        deadline = time.monotonic() + max(timeout, 0.0)
        with self._state_condition:
            if self._state_version != since:
                return True, self._state_version
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False, self._state_version
                self._state_condition.wait(remaining)
                if self._state_version != since:
                    return True, self._state_version

    def should_register_startup(self) -> bool:
        return self.run_at_startup or bool(self.schedules)

    def sync_startup_registration(self) -> None:
        if sys.platform != "win32":
            return
        startup_file = windows_startup_script()
        legacy_file = legacy_windows_startup_script()
        legacy_file.unlink(missing_ok=True)
        if self.should_register_startup():
            startup_file.parent.mkdir(parents=True, exist_ok=True)
            command = startup_launch_command().replace('"', '""')
            script = (
                'Set shell = CreateObject("WScript.Shell")\n'
                f'shell.CurrentDirectory = "{str(APP_ROOT).replace(chr(92), chr(92) * 2)}"\n'
                f'shell.Run "{command}", 0\n'
            )
            startup_file.write_text(script, encoding="utf-8")
        elif startup_file.exists():
            startup_file.unlink(missing_ok=True)

    def persist_state(self) -> None:
        save_config(
            self.selected_monitor_ids,
            str(self.video_directory) if self.video_directory is not None else "",
            self.screen_aliases,
            self.transition_method,
            self.run_at_startup,
            self.schedules,
        )

    def apply_video_directory_watch(self) -> None:
        existing_paths = self.folder_watcher.directories()
        if existing_paths:
            self.folder_watcher.removePaths(existing_paths)
        if self.video_directory is not None and self.video_directory.exists():
            self.folder_watcher.addPath(str(self.video_directory))

    def on_video_directory_changed(self, path: str) -> None:
        self.folder_refresh_timer.start()

    def refresh_video_library(self, *_args) -> None:
        self.apply_video_directory_watch()
        self.coordinator.set_video_directory(self.video_directory)
        self.start_media_scan()

    def start_media_scan(self) -> None:
        directory = self.video_directory
        with self._media_scan_lock:
            self._media_scan_generation += 1
            generation = self._media_scan_generation
            self._media_scan_target_dir = directory
        if directory is None or not directory.exists() or not directory.is_dir():
            self.available_videos = []
            self.media_scan_in_progress = False
            self.media_scan_error = ""
            self.sync_playback_outputs()
            self.notify_state_changed()
            return
        should_start_worker = False
        with self._media_scan_lock:
            if not self.media_scan_in_progress:
                self.media_scan_in_progress = True
                self.media_scan_error = ""
                should_start_worker = True
            elif not self._media_scan_active:
                should_start_worker = True
        self.notify_state_changed()
        if should_start_worker:
            self._launch_media_scan_worker(generation, directory)

    def _launch_media_scan_worker(self, generation: int, directory: Path) -> None:
        with self._media_scan_lock:
            self._media_scan_active = True

        def worker() -> None:
            error_message = ""
            try:
                results = scan_video_directory(directory)
            except Exception as error:  # noqa: BLE001
                results = []
                error_message = str(error)
            self.media_scan_completed.emit(results, generation, error_message)

        threading.Thread(
            target=worker,
            name=f"media-library-scan-{generation}",
            daemon=True,
        ).start()

    def on_media_scan_completed(self, results: object, generation: int, error_message: str) -> None:
        next_generation = 0
        next_directory: Path | None = None
        apply_results = False
        with self._media_scan_lock:
            self._media_scan_active = False
            current_generation = self._media_scan_generation
            next_directory = self._media_scan_target_dir
            if generation == current_generation:
                apply_results = True
                self.media_scan_in_progress = False
            elif current_generation > generation and next_directory is not None:
                next_generation = current_generation
        if apply_results:
            self.available_videos = list(results) if isinstance(results, list) else []
            self.media_scan_error = error_message
            self.sync_playback_outputs()
            self.notify_state_changed()
            return
        if next_generation > 0 and next_directory is not None:
            self._launch_media_scan_worker(next_generation, next_directory)

    def refresh_remote_screens(self) -> None:
        previous_snapshot = {
            screen_id: remote_screen_digest(state)
            for screen_id, state in self.remote_screens.items()
        }
        self.remote_screens = {item["screen_id"]: item for item in self.remote_server.remote_screens_snapshot()}
        for screen_id, state in self.remote_screens.items():
            if screen_id not in self.screen_aliases and str(state.get("name") or "").strip():
                self.screen_aliases[screen_id] = str(state.get("name")).strip()
        self.sync_remote_server_commands()
        current_snapshot = {
            screen_id: remote_screen_digest(state)
            for screen_id, state in self.remote_screens.items()
        }
        if current_snapshot != previous_snapshot:
            self.notify_state_changed()

    def ensure_firewall_access(self, force_retry: bool = False) -> None:
        before_warning = self.remote_server.network_snapshot().get("warnings") or []
        before_configured = bool(self.remote_server.network_snapshot().get("firewallConfigured"))
        configured = self.remote_server.ensure_firewall_rule(force_retry=force_retry)
        after_snapshot = self.remote_server.network_snapshot()
        after_warning = after_snapshot.get("warnings") or []
        after_configured = bool(after_snapshot.get("firewallConfigured"))
        if configured != before_configured or after_warning != before_warning or after_configured != before_configured:
            self.notify_state_changed()

    def unified_screen_targets(self, include_offline: bool = True) -> list[UnifiedScreenTarget]:
        targets: list[UnifiedScreenTarget] = []
        for screen in available_screens():
            identifier = screen_identifier(screen)
            geometry = screen.geometry()
            targets.append(
                UnifiedScreenTarget(
                    id=identifier,
                    label=screen_display_name(screen, self.screen_aliases),
                    kind="local",
                    online=True,
                    detail=f"HDMI / local display • {geometry.width()}x{geometry.height()}",
                )
            )
        for screen_id, state in sorted(self.remote_screens.items(), key=lambda item: friendly_remote_name(item[0], self.screen_aliases, item[1]).lower()):
            online = bool(state.get("online"))
            if not include_offline and not online:
                continue
            detail = f"LAN screen • {'Online' if online else 'Offline'}"
            if state.get("ip"):
                detail += f" • {state['ip']}"
            warning = "" if online else "This LAN screen has disconnected."
            targets.append(
                UnifiedScreenTarget(
                    id=screen_id,
                    label=friendly_remote_name(screen_id, self.screen_aliases, state),
                    kind="remote",
                    online=online,
                    detail=detail,
                    warning=warning,
                )
            )
        return targets

    def build_remote_server_commands(self) -> dict[str, dict[str, Any]]:
        commands: dict[str, dict[str, Any]] = {}
        for screen_id, command in self.coordinator.command_snapshots().items():
            media_path = Path(str(command.get("path") or "")) if command.get("path") else None
            media_url = ""
            if media_path is not None:
                relative_path = str(command.get("relative_path") or "")
                if relative_path and self.video_directory is not None and media_path.exists():
                    media_url = f"/media/{quote(relative_path, safe='/')}"
                elif media_path.exists():
                    media_url = f"/api/media-file?path={quote(str(media_path))}"
            commands[screen_id] = {
                "screenId": screen_id,
                "version": int(command.get("version") or 0),
                "type": str(command.get("type") or "clear"),
                "mode": str(command.get("mode") or "idle"),
                "entryId": command.get("entry_id"),
                "message": str(command.get("message") or ""),
                "label": str(command.get("label") or ""),
                "path": str(command.get("path") or ""),
                "relativePath": str(command.get("relative_path") or ""),
                "mediaKind": str(command.get("media_kind") or ""),
                "mediaUrl": media_url,
                "playAtMs": int(command.get("play_at_ms") or 0),
                "transition": str(command.get("transition") or "fade_black"),
                "paused": bool(command.get("paused")),
            }
        return commands

    def sync_remote_server_commands(self) -> None:
        self.remote_server.set_media_root(self.video_directory)
        self.remote_server.set_commands(self.build_remote_server_commands())

    def desired_local_worker_screen_ids(self) -> list[str]:
        return sorted(
            screen_id
            for screen_id in self.coordinator.all_active_screen_ids()
            if not is_remote_screen_id(screen_id)
        )

    def spawn_local_playback_worker(self, screen_id: str) -> None:
        if screen_id in self.local_playback_workers:
            process = self.local_playback_workers.get(screen_id)
            if process is not None and process.poll() is None:
                return
        try:
            process = subprocess.Popen(
                playback_worker_launch_args(screen_id),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **windows_hidden_subprocess_kwargs(),
            )
        except OSError as error:
            append_engine_startup_log(f"Failed to launch local playback worker for {screen_id}: {error}")
            return
        self.local_playback_workers[screen_id] = process

    def stop_local_playback_worker(self, screen_id: str) -> None:
        process = self.local_playback_workers.pop(screen_id, None)
        if process is None:
            return
        if process.poll() is not None:
            return
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    check=False,
                    **windows_hidden_subprocess_kwargs(),
                )
            else:
                process.terminate()
        except OSError:
            pass

    def sync_local_playback_workers(self) -> None:
        desired_ids = set(self.desired_local_worker_screen_ids())
        for screen_id in list(self.local_playback_workers):
            process = self.local_playback_workers.get(screen_id)
            if process is not None and process.poll() is not None:
                self.local_playback_workers.pop(screen_id, None)
        for screen_id in list(self.local_playback_workers):
            if screen_id not in desired_ids:
                self.stop_local_playback_worker(screen_id)
        for screen_id in sorted(desired_ids):
            self.spawn_local_playback_worker(screen_id)
        self.local_window_count = len(self.local_playback_workers)

    def sync_playback_outputs(self) -> None:
        self.sync_remote_server_commands()
        self.sync_local_playback_workers()
        self.notify_state_changed()

    def available_media_payload(self) -> list[dict[str, Any]]:
        return [
            {
                "relativePath": media_relative_path(path, self.video_directory),
                "name": path.name,
                "category": media_category_label(path, self.video_directory),
                "isVideo": is_video_file(path),
                "isImage": is_image_file(path),
            }
            for path in self.available_videos
        ]

    def current_status_payload(self) -> dict[str, Any]:
        now = current_uk_datetime()
        weekday_index = now.weekday()
        minute_of_day = (now.hour * 60) + now.minute
        active_entry = next(
            (
                entry
                for screen_id in self.selected_monitor_ids
                if (entry := active_schedule_for_screen(self.schedules, weekday_index, minute_of_day, screen_id)) is not None
            ),
            active_schedule_for_minute(self.schedules, weekday_index, minute_of_day),
        )
        if not self.coordinator.playback_enabled and not self.coordinator.quick_play_paths:
            return {
                "clockLabel": self.status_clock_label,
                "activeLabel": "Playback stopped",
                "heroTitle": "Playback stopped",
                "heroSubtitle": "Screens remain idle until playback is launched again.",
                "heroProgress": 0.0,
            }
        if active_entry is None:
            return {
                "clockLabel": self.status_clock_label,
                "activeLabel": self.status_active_label,
                "heroTitle": "No active slot",
                "heroSubtitle": "Waiting for a matching UK-time schedule.",
                "heroProgress": 0.0,
            }
        return {
            "clockLabel": self.status_clock_label,
            "activeLabel": self.status_active_label,
            "heroTitle": f"{active_entry.title} • {active_entry.range_label} • {active_entry.video_label or active_entry.video_file}",
            "heroSubtitle": f"Transition: {TRANSITION_METHODS.get(self.transition_method, 'Fade Through Black')} • Media folder: {self.video_directory if self.video_directory is not None else 'Not set'}",
            "heroProgress": schedule_progress(active_entry, now),
        }

    def snapshot(self) -> dict[str, Any]:
        remote_active = sum(
            1
            for state in self.remote_screens.values()
            if state.get("online") and str(state.get("state") or "") in {"playing", "connected"} and str(state.get("current_media") or "")
        )
        return {
            "stateVersion": self._state_version,
            "selectedMonitorIds": self.selected_monitor_ids,
            "videoDirectory": str(self.video_directory) if self.video_directory is not None else "",
            "screenAliases": self.screen_aliases,
            "transitionMethod": self.transition_method,
            "runAtStartup": self.run_at_startup,
            "schedules": [entry.to_dict() for entry in self.schedules],
            "availableMedia": self.available_media_payload(),
            "library": {
                "scanning": self.media_scan_in_progress,
                "error": self.media_scan_error,
                "indexedCount": len(self.available_videos),
            },
            "remoteScreens": self.remote_server.remote_screens_snapshot(),
            "network": self.remote_server.network_snapshot(),
            "screens": [
                {
                    "id": target.id,
                    "label": target.label,
                    "kind": target.kind,
                    "online": target.online,
                    "detail": target.detail,
                    "warning": target.warning,
                }
                for target in self.unified_screen_targets(include_offline=True)
            ],
            "status": self.current_status_payload(),
            "playback": {
                "enabled": self.coordinator.playback_enabled,
                "paused": self.playback_paused,
                "localWindowCount": self.local_window_count,
                "remoteActiveCount": remote_active,
                "activeScreenCount": self.local_window_count + remote_active,
            },
        }

    def handle_action(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if action == "toggle_screen_selection":
            screen_id = str(payload.get("screen_id") or "")
            if screen_id:
                if screen_id in self.selected_monitor_ids:
                    self.selected_monitor_ids.remove(screen_id)
                else:
                    self.selected_monitor_ids.append(screen_id)
                self.coordinator.set_selected_monitors(self.selected_monitor_ids)
                self.persist_state()
        elif action == "rename_screen_alias":
            screen_id = str(payload.get("screen_id") or "")
            alias = str(payload.get("alias") or "").strip()
            if alias:
                self.screen_aliases[screen_id] = alias
            else:
                self.screen_aliases.pop(screen_id, None)
            self.persist_state()
        elif action == "set_transition_method":
            transition_method = str(payload.get("transition_method") or "")
            if transition_method in TRANSITION_METHODS:
                self.transition_method = transition_method
                self.coordinator.set_transition_method(self.transition_method)
                self.sync_playback_outputs()
                self.persist_state()
        elif action == "quick_play":
            path = Path(str(payload.get("path") or "")).expanduser()
            target_ids = [str(item) for item in payload.get("target_screen_ids") or []]
            if not target_ids:
                raise ValueError("Choose at least one screen for quick play.")
            self.coordinator.apply_quick_play(target_ids, path)
            self.sync_playback_outputs()
        elif action == "clear_quick_play":
            self.coordinator.clear_quick_play(None)
            self.sync_playback_outputs()
        elif action == "set_media_folder":
            selected = str(payload.get("path") or "").strip()
            if not selected:
                raise ValueError("Choose a media folder.")
            self.video_directory = Path(selected).expanduser()
            self.refresh_video_library()
            self.persist_state()
        elif action == "refresh_media":
            self.refresh_video_library()
        elif action == "launch_selected":
            self.coordinator.set_schedules(self.schedules)
            self.coordinator.set_selected_monitors(self.selected_monitor_ids)
            if not self.coordinator.has_launch_targets():
                raise ValueError("Choose at least one target screen in Global Screens or within a schedule before launching playback.")
            self.coordinator.launch_windows()
            self.sync_playback_outputs()
        elif action == "toggle_pause":
            self.coordinator.toggle_pause()
        elif action == "stop_screens":
            self.coordinator.stop_windows()
            self.sync_playback_outputs()
        elif action == "retry_firewall_setup":
            self.ensure_firewall_access(force_retry=True)
        elif action == "set_run_at_startup":
            self.run_at_startup = bool(payload.get("value"))
            self.sync_startup_registration()
            self.persist_state()
        elif action == "save_schedule":
            raw_schedule = payload.get("schedule")
            if not isinstance(raw_schedule, dict):
                raise ValueError("Invalid schedule payload.")
            candidate = ScheduleEntry.from_dict(raw_schedule)
            if self.video_directory is None:
                raise ValueError("Set the media folder first from the Library menu.")
            candidate_path = self.video_directory / candidate.video_file if candidate.video_file else None
            if candidate_path is None or not candidate_path.exists():
                raise ValueError("Choose a media file from the selected folder.")
            validate_schedule_candidate(candidate, self.schedules, candidate.id if any(item.id == candidate.id for item in self.schedules) else None)
            replaced = False
            for index, entry in enumerate(self.schedules):
                if entry.id == candidate.id:
                    self.schedules[index] = candidate
                    replaced = True
                    break
            if not replaced:
                self.schedules.append(candidate)
            self.schedules.sort(key=lambda item: ScheduleEntry.time_to_minutes(item.start_time))
            self.persist_state()
            self.sync_startup_registration()
            self.coordinator.set_schedules(self.schedules)
        elif action == "delete_schedule":
            schedule_id = str(payload.get("schedule_id") or "")
            self.schedules = [item for item in self.schedules if item.id != schedule_id]
            self.persist_state()
            self.sync_startup_registration()
            self.coordinator.set_schedules(self.schedules)
        elif action == "shutdown_engine":
            QTimer.singleShot(0, self.shutdown)
        else:
            raise ValueError(f"Unsupported action: {action}")
        self.notify_state_changed()
        return {"ok": True, "snapshot": self.snapshot()}

    def shutdown(self) -> None:
        self.folder_refresh_timer.stop()
        self.remote_refresh_timer.stop()
        self.firewall_check_timer.stop()
        watched_paths = self.folder_watcher.directories()
        if watched_paths:
            self.folder_watcher.removePaths(watched_paths)
        self.control_server.stop()
        self.remote_server.stop()
        for screen_id in list(self.local_playback_workers):
            self.stop_local_playback_worker(screen_id)
        self.coordinator.stop_windows()
        app = QApplication.instance()
        if app is not None:
            app.quit()


def run_playback_worker(screen_id: str) -> int:
    ensure_app_paths()
    app = QApplication(sys.argv)
    app.setApplicationName(f"{APP_NAME} Playback Worker")
    app.setQuitOnLastWindowClosed(True)
    try:
        _worker = LocalPlaybackWorkerController(screen_id)
    except Exception as error:  # noqa: BLE001
        append_engine_startup_log(f"Local playback worker failed for {screen_id}: {error}")
        return 1
    return app.exec()


def ensure_backend_running(client: ControllerApiClient) -> None:
    if client.ping():
        return
    remove_stale_engine_lock()
    existing_engine_pids = [pid for pid in known_engine_process_ids() if is_process_running(pid)]
    if existing_engine_pids:
        status = read_engine_startup_status()
        status_message = str(status.get("message") or "").strip()
        stop_known_engine_processes()
        if client.ping():
            return
        if status_message:
            append_engine_startup_log(f"Restarting unresponsive engine instance(s): {existing_engine_pids}. Last status: {status_message}")
        else:
            append_engine_startup_log(f"Restarting unresponsive engine instance(s): {existing_engine_pids}")
    reset_engine_startup_artifacts()
    creationflags = 0
    if sys.platform == "win32":
        creationflags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    subprocess.Popen(
        engine_launch_args(),
        cwd=str(APP_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=creationflags,
        start_new_session=sys.platform != "win32",
    )
    deadline = time.time() + 10.0
    while time.time() < deadline:
        if client.ping():
            return
        status = read_engine_startup_status()
        status_message = str(status.get("message") or "").strip()
        status_details = str(status.get("details") or "").strip()
        status_pid = int(status.get("pid") or 0) if str(status.get("pid") or "").strip() else 0
        if str(status.get("state") or "").strip().lower() == "failed":
            detail_suffix = f" See {ENGINE_STARTUP_LOG_PATH} for details." if ENGINE_STARTUP_LOG_PATH.exists() else ""
            raise RuntimeError(status_message or f"The background engine failed during startup.{detail_suffix}")
        if status_pid > 0 and not is_process_running(status_pid):
            if status_message:
                extra = f"\n\n{status_details}" if status_details else ""
                raise RuntimeError(f"{status_message}{extra}")
            log_suffix = f" See {ENGINE_STARTUP_LOG_PATH} for details." if ENGINE_STARTUP_LOG_PATH.exists() else ""
            raise RuntimeError(f"The background engine exited during startup.{log_suffix}")
        time.sleep(0.25)
    status = read_engine_startup_status()
    status_message = str(status.get("message") or "").strip()
    if status_message:
        raise RuntimeError(status_message)
    raise RuntimeError(
        f"The background engine did not respond on port {ENGINE_CONTROL_PORT} within 10 seconds. "
        f"See {ENGINE_STARTUP_LOG_PATH} for details."
    )


def run_engine() -> int:
    ensure_app_paths()
    enable_engine_fault_logging()
    decode_mode = "software-forced" if os.getenv("QT_FFMPEG_DECODING_HW_DEVICE_TYPES", "").strip() == "," else "qt-default"
    append_engine_startup_log(f"Engine launch decode mode: {decode_mode}")
    cleanup_legacy_startup_script()
    write_engine_startup_status("starting", "Launching background engine.", pid=os.getpid())
    engine_lock = acquire_engine_instance_lock()
    if engine_lock is None:
        existing_pid = read_engine_lock_pid()
        if existing_pid is not None and is_process_running(existing_pid):
            write_engine_startup_status(
                "failed",
                f"Another engine instance is already running with PID {existing_pid}.",
                pid=os.getpid(),
            )
            return 0
        stale_lock_removed = remove_stale_engine_lock()
        message = "A stale engine lock blocked startup." if stale_lock_removed else "Unable to acquire the engine instance lock."
        write_engine_startup_status("failed", message, pid=os.getpid())
        append_engine_startup_log(message)
        raise RuntimeError(message)
    try:
        app = QApplication(sys.argv)
        app.setApplicationName(f"{APP_NAME} Engine")
        app.setQuitOnLastWindowClosed(False)
        _engine = ControllerEngine()
        write_engine_startup_status("ready", "Background engine ready.", pid=os.getpid())
        return app.exec()
    except Exception as error:  # noqa: BLE001
        details = traceback.format_exc()
        write_engine_startup_status("failed", str(error) or "Engine startup failed.", details, pid=os.getpid())
        append_engine_startup_log(details)
        raise
    finally:
        release_engine_instance_lock(engine_lock)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--engine", action="store_true")
    parser.add_argument("--stop-engine", action="store_true")
    parser.add_argument("--playback-worker", action="store_true")
    parser.add_argument("--screen-id", default="")
    parsed, _unknown = parser.parse_known_args(args)
    if parsed.stop_engine:
        return 0 if stop_engine_from_lock() else 1
    if parsed.engine:
        return run_engine()
    if parsed.playback_worker:
        screen_id = str(parsed.screen_id or "").strip()
        if not screen_id:
            return 1
        return run_playback_worker(screen_id)

    ensure_app_paths()
    cleanup_legacy_startup_script()
    app = QApplication(sys.argv)
    app.setApplicationName("Background Screen Controller")
    app.setWindowIcon(app.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))
    app.setQuitOnLastWindowClosed(False)
    instance_coordinator = ControllerInstanceCoordinator(CONTROLLER_INSTANCE_NAME)
    if instance_coordinator.try_notify_existing():
        return 0
    if not instance_coordinator.listen():
        return 1
    client = ControllerApiClient()
    startup_error = ""
    try:
        ensure_backend_running(client)
    except Exception as error:  # noqa: BLE001
        startup_error = str(error)
    window = MainWindow(backend_client=client)
    instance_coordinator.activation_requested.connect(window.show_from_tray)
    window._instance_coordinator = instance_coordinator
    if startup_error:
        window.set_backend_offline_ui(startup_error)
        window.set_form_message(startup_error, "error")
    window.showMaximized()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
