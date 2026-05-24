from __future__ import annotations

import argparse
import ctypes
import faulthandler
import ipaddress
import json
import mimetypes
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
import zipfile
from datetime import datetime
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.error import URLError
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from browser_screen_bindings import (
    BROWSER_BINDING_KEY,
    browser_binding_remote_id,
    browser_target_status_detail,
    configured_browser_screen_ids,
    resolve_browser_commands,
    validate_unique_browser_bindings,
)
from media_library import (
    SUPPORTED_MEDIA_EXTENSIONS,
    build_media_library_index_from_paths,
    is_image_file,
    media_category_label,
    media_relative_path,
    scan_video_directory,
)
from models import (
    DAY_OPTIONS,
    ScheduleEntry,
    ScheduleMediaAssignment,
    UnifiedScreenTarget,
)
from playback_assignment import PlaybackAssignmentService
from playback_protocol import COMMAND_PROTOCOL_VERSION, PlaybackCommand
from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QFileSystemWatcher,
    QObject,
    QPoint,
    QPropertyAnimation,
    QSize,
    Qt,
    QTime,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QFontMetrics,
    QGuiApplication,
    QIcon,
    QImageReader,
    QPixmap,
    QScreen,
)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtNetwork import (
    QAbstractSocket,
    QLocalServer,
    QLocalSocket,
    QNetworkInterface,
)
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
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStyle,
    QSystemTrayIcon,
    QTextBrowser,
    QTextEdit,
    QTimeEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from scheduling import (
    active_schedule_for_minute,
    active_schedule_for_screen,
    referenced_video_files,
    schedule_assignment_for_screen,
    schedule_progress,
    validate_schedule_candidate,
    validate_schedule_media_assignments,
)
from screen_groups import (
    DEFAULT_GROUP_ID,
    DEFAULT_GROUP_NAME,
    ScreenGroup,
    combined_screen_groups,
    expand_target_ids,
    is_group_target_id,
    normalize_schedule_target_ids,
    parse_screen_groups,
    target_label_map,
    validate_screen_groups,
)
from screen_protocols import (
    CAPABILITY_LABELS,
    SCREEN_CAPABILITIES,
    SCREEN_TRANSPORTS,
    TRANSPORT_LABELS,
    ConfiguredScreen,
    parse_configured_screens,
    supported_capabilities,
    validate_configured_screens,
)
from screen_registry import ScreenRegistry
from shiboken6 import isValid
from ui_styles import apply_dialog_theme

# Long-running local playback has been more stable when Qt is allowed to use
# its normal Windows video pipeline. We keep a switch to force software
# decoding for specific installations that need it, but do not force that path
# globally anymore because hard native crashes are worse than a rendering
# fallback.
if sys.platform == "win32":
    if os.getenv("BACKGROUND_SCREEN_FORCE_SOFTWARE_DECODE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
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
    return {
        str(key): str(value)
        for key, value in data.items()
        if isinstance(key, str) and isinstance(value, str)
    }


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
ENGINE_DIAGNOSTICS_LOG_PATH = APP_DATA_DIR / "engine_diagnostics.jsonl"
ENGINE_FAULT_LOG_HANDLE = None

CONFIG_SCHEMA_VERSION = 3
CONTROL_HIDE_DELAY_MS = 2500
CONTROL_FADE_DURATION_MS = 1000
TRANSITION_TO_BLACK_MS = 320
TRANSITION_FROM_BLACK_MS = 520
TRANSITION_METHODS = {
    "fade_black": "Fade Through Black",
    "cut": "Cut",
    "soft_fade": "Soft Fade",
}


def _default_config_payload() -> dict[str, Any]:
    return {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "configured_screens": [],
        "screen_groups": [],
        "selected_monitor_ids": [],
        "enabled_screen_ids": [],
        "video_directory": "",
        "screen_aliases": {},
        "transition_method": "fade_black",
        "run_at_startup": False,
        "lan_pairing_required": False,
        "lan_allow_unpaired_clients": False,
        "lan_paired_client_ids": [],
        "schedules": [],
    }


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f"{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(text)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
            temp_path = Path(handle.name)
        temp_path.replace(path)
    finally:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


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


def _migrate_config_to_v1(payload: dict[str, Any]) -> dict[str, Any]:
    migrated = dict(payload)
    defaults = _default_config_payload()
    for key, default_value in defaults.items():
        migrated.setdefault(key, default_value)
    migrated["schema_version"] = 1
    return migrated


def _migrate_config_v1_to_v2(payload: dict[str, Any]) -> dict[str, Any]:
    migrated = dict(payload)
    configured_screens = migrated.get("configured_screens")
    if not isinstance(configured_screens, list):
        configured_screens = []
        migrated["configured_screens"] = configured_screens
    # Formal registry snapshot key for forward-compatible persistence/modeling.
    migrated.setdefault("screen_registry", configured_screens)
    migrated["schema_version"] = 2
    return migrated


def _migrate_config_v2_to_v3(payload: dict[str, Any]) -> dict[str, Any]:
    migrated = dict(payload)
    migrated.setdefault("lan_pairing_required", False)
    migrated.setdefault("lan_allow_unpaired_clients", False)
    migrated.setdefault("lan_paired_client_ids", [])
    migrated["schema_version"] = 3
    return migrated


CONFIG_MIGRATIONS: dict[int, Any] = {
    1: _migrate_config_v1_to_v2,
    2: _migrate_config_v2_to_v3,
}


def migrate_config(raw_data: object) -> dict[str, Any]:
    if not isinstance(raw_data, dict):
        return _default_config_payload()

    migrated = dict(raw_data)
    schema_version = migrated.get("schema_version")
    if not isinstance(schema_version, int) or schema_version <= 0:
        migrated = _migrate_config_to_v1(migrated)
        schema_version = 1

    # Do not mutate unknown future schemas; preserve payload as-is.
    if schema_version > CONFIG_SCHEMA_VERSION:
        defaults = _default_config_payload()
        for key, default_value in defaults.items():
            migrated.setdefault(key, default_value)
        return migrated

    while schema_version < CONFIG_SCHEMA_VERSION:
        step = CONFIG_MIGRATIONS.get(schema_version)
        if step is None:
            break
        migrated = step(migrated)
        next_version = migrated.get("schema_version")
        if not isinstance(next_version, int) or next_version <= schema_version:
            break
        schema_version = next_version

    defaults = _default_config_payload()
    for key, default_value in defaults.items():
        migrated.setdefault(key, default_value)
    migrated["schema_version"] = max(
        1,
        int(migrated.get("schema_version") or CONFIG_SCHEMA_VERSION),
    )
    return migrated


def load_config() -> dict[str, Any]:
    ensure_app_paths()
    if not CONFIG_PATH.exists():
        return _default_config_payload()

    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _default_config_payload()

    migrated = migrate_config(data)
    schema_version = migrated.get("schema_version")
    selected = migrated.get("selected_monitor_ids")
    enabled = migrated.get("enabled_screen_ids")
    configured_screens = parse_configured_screens(migrated.get("configured_screens"))
    screen_groups = parse_screen_groups(migrated.get("screen_groups"))
    schedules = migrated.get("schedules")
    video_directory = migrated.get("video_directory")
    screen_aliases = migrated.get("screen_aliases")
    transition_method = migrated.get("transition_method")
    run_at_startup = migrated.get("run_at_startup")
    lan_pairing_required = migrated.get("lan_pairing_required")
    lan_allow_unpaired_clients = migrated.get("lan_allow_unpaired_clients")
    lan_paired_client_ids = migrated.get("lan_paired_client_ids")
    return {
        "schema_version": schema_version
        if isinstance(schema_version, int) and schema_version > 0
        else CONFIG_SCHEMA_VERSION,
        "configured_screens": configured_screens,
        "screen_groups": screen_groups,
        "selected_monitor_ids": selected if isinstance(selected, list) else [],
        "enabled_screen_ids": enabled if isinstance(enabled, list) else [],
        "video_directory": str(video_directory)
        if isinstance(video_directory, str)
        else "",
        "screen_aliases": screen_aliases if isinstance(screen_aliases, dict) else {},
        "transition_method": transition_method
        if transition_method in TRANSITION_METHODS
        else "fade_black",
        "run_at_startup": bool(run_at_startup),
        "lan_pairing_required": bool(lan_pairing_required),
        "lan_allow_unpaired_clients": bool(lan_allow_unpaired_clients),
        "lan_paired_client_ids": [
            str(item).strip()
            for item in (
                lan_paired_client_ids if isinstance(lan_paired_client_ids, list) else []
            )
            if str(item).strip()
        ],
        "schedules": schedules if isinstance(schedules, list) else [],
    }


def save_config(
    configured_screens: list[ConfiguredScreen],
    screen_groups: list[ScreenGroup],
    selected_monitor_ids: list[str],
    enabled_screen_ids: list[str],
    video_directory: str,
    screen_aliases: dict[str, str],
    transition_method: str,
    run_at_startup: bool,
    lan_pairing_required: bool,
    lan_allow_unpaired_clients: bool,
    lan_paired_client_ids: list[str],
    schedules: list[ScheduleEntry],
) -> None:
    ensure_app_paths()
    payload = {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "configured_screens": [screen.to_dict() for screen in configured_screens],
        "screen_groups": [group.to_dict() for group in screen_groups],
        "selected_monitor_ids": selected_monitor_ids,
        "enabled_screen_ids": enabled_screen_ids,
        "video_directory": video_directory,
        "screen_aliases": screen_aliases,
        "transition_method": transition_method,
        "run_at_startup": run_at_startup,
        "lan_pairing_required": bool(lan_pairing_required),
        "lan_allow_unpaired_clients": bool(lan_allow_unpaired_clients),
        "lan_paired_client_ids": [
            str(item).strip() for item in lan_paired_client_ids if str(item).strip()
        ],
        "schedules": [entry.to_dict() for entry in schedules],
    }
    _atomic_write_text(CONFIG_PATH, json.dumps(payload, indent=2))


def write_engine_startup_status(
    state: str, message: str = "", details: str = "", pid: int | None = None
) -> None:
    ensure_app_paths()
    payload = {
        "state": state,
        "message": message,
        "details": details,
        "pid": int(pid if pid is not None else os.getpid()),
        "updatedAt": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        _atomic_write_text(ENGINE_STARTUP_STATUS_PATH, json.dumps(payload, indent=2))
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
    for path in (
        ENGINE_STARTUP_STATUS_PATH,
        ENGINE_STARTUP_LOG_PATH,
        ENGINE_DIAGNOSTICS_LOG_PATH,
    ):
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


def append_engine_diagnostics_event(
    event: str,
    *,
    details: str = "",
    severity: str = "info",
    context: dict[str, Any] | None = None,
) -> None:
    ensure_app_paths()
    payload = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "event": str(event or "unknown"),
        "severity": str(severity or "info"),
        "details": str(details or ""),
        "context": context or {},
    }
    try:
        with ENGINE_DIAGNOSTICS_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, separators=(",", ":")) + "\n")
    except OSError:
        return


def export_engine_diagnostics_bundle(
    *,
    destination: Path | None = None,
    snapshot: dict[str, Any] | None = None,
) -> Path:
    ensure_app_paths()
    if destination is None:
        export_dir = APP_DATA_DIR / "diagnostics_exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        destination = export_dir / f"engine_diagnostics_{timestamp}.zip"
    else:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)

    diag_snapshot = snapshot if isinstance(snapshot, dict) else {}
    metadata = {
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "appDataDir": str(APP_DATA_DIR),
        "files": {
            "startupStatus": str(ENGINE_STARTUP_STATUS_PATH),
            "startupLog": str(ENGINE_STARTUP_LOG_PATH),
            "diagnosticsLog": str(ENGINE_DIAGNOSTICS_LOG_PATH),
        },
    }

    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "metadata.json",
            json.dumps(metadata, indent=2, ensure_ascii=False),
        )
        archive.writestr(
            "snapshot.json",
            json.dumps(diag_snapshot, indent=2, ensure_ascii=False),
        )
        for source, arc_name in (
            (ENGINE_STARTUP_STATUS_PATH, "engine_startup_status.json"),
            (ENGINE_STARTUP_LOG_PATH, "engine_startup.log"),
            (ENGINE_DIAGNOSTICS_LOG_PATH, "engine_diagnostics.jsonl"),
        ):
            if source.exists() and source.is_file():
                archive.write(source, arc_name)

    return destination


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


def screen_label_from_id(
    screen_id: str,
    aliases: dict[str, str],
    screen_groups: list[ScreenGroup] | None = None,
) -> str:
    if is_group_target_id(screen_id):
        group_labels = target_label_map(screen_groups or [])
        return group_labels.get(screen_id, screen_id)
    screen = find_screen_by_id(screen_id)
    if screen is not None:
        return screen_display_name(screen, aliases)
    alias = aliases.get(screen_id, "").strip()
    if alias:
        return alias
    return screen_id.split("|", 1)[0]


def format_screen_targets(
    screen_ids: list[str],
    aliases: dict[str, str],
    screen_groups: list[ScreenGroup] | None = None,
) -> str:
    if not screen_ids:
        return "No targets selected"
    return ", ".join(
        screen_label_from_id(screen_id, aliases, screen_groups)
        for screen_id in screen_ids
    )


def disconnected_screen_ids(
    screen_ids: list[str], screen_groups: list[ScreenGroup] | None = None
) -> list[str]:
    group_ids = target_label_map(screen_groups or [])
    disconnected: list[str] = []
    for screen_id in screen_ids:
        if screen_id in group_ids:
            continue
        if find_screen_by_id(screen_id) is None:
            disconnected.append(screen_id)
    return disconnected


def format_engine_diagnostics_summary(
    performance: dict[str, Any] | None,
    *,
    scanning: bool = False,
    library_error: str = "",
) -> tuple[str, str, str]:
    metrics = performance or {}
    indexed = max(0, int(metrics.get("indexedFileCount") or 0))
    scan_ms = max(0, int(metrics.get("mediaScanDurationMs") or 0))
    snapshot_bytes = max(0, int(metrics.get("snapshotSizeBytes") or 0))
    worker_count = max(0, int(metrics.get("localPlaybackWorkerCount") or 0))
    generated_at = str(metrics.get("lastSnapshotGeneratedAt") or "").strip()

    if library_error:
        secondary = f"Library error • last scan {scan_ms} ms"
    elif scanning:
        secondary = f"Library indexing • {indexed} file(s) found so far"
    else:
        secondary = (
            f"Library {indexed} file(s) • last scan {scan_ms} ms • "
            f"snapshot {snapshot_bytes} bytes"
        )

    tooltip_lines = [
        "Engine diagnostics summary",
        f"Indexed media files: {indexed}",
        f"Last media scan duration: {scan_ms} ms",
        f"Last engine snapshot size: {snapshot_bytes} bytes",
        f"Local playback workers: {worker_count}",
    ]
    if generated_at:
        tooltip_lines.append(f"Last snapshot generated: {generated_at}")
    if scanning:
        tooltip_lines.append("Library state: indexing in progress")
    if library_error:
        tooltip_lines.append(f"Library error: {library_error}")

    primary = f"{worker_count} worker{'s' if worker_count != 1 else ''}"
    return primary, secondary, "\n".join(tooltip_lines)


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


def load_pixmap_for_bounds(path: Path, bounds: QSize | None = None) -> QPixmap:
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    if bounds is not None and bounds.width() > 0 and bounds.height() > 0:
        original_size = reader.size()
        if (
            original_size.isValid()
            and original_size.width() > 0
            and original_size.height() > 0
        ):
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


def playback_window_uses_tool_mode(platform: str | None = None) -> bool:
    return (platform or sys.platform).lower().strip() == "win32"


def playback_window_flags_for_platform(
    platform: str | None = None,
) -> Qt.WindowType:
    flags = (
        Qt.WindowType.Window
        | Qt.WindowType.FramelessWindowHint
        | Qt.WindowType.WindowStaysOnTopHint
    )
    if playback_window_uses_tool_mode(platform):
        flags |= Qt.WindowType.Tool
    return flags


def apply_windows_playback_surface_mode(widget: QWidget) -> bool:
    if sys.platform != "win32":
        return False
    try:
        hwnd = int(widget.winId())
        if hwnd <= 0:
            return False
        user32 = ctypes.windll.user32
        get_window_long = getattr(user32, "GetWindowLongPtrW", None)
        set_window_long = getattr(user32, "SetWindowLongPtrW", None)
        if get_window_long is None or set_window_long is None:
            get_window_long = user32.GetWindowLongW
            set_window_long = user32.SetWindowLongW
        gwl_exstyle = -20
        ws_ex_appwindow = 0x00040000
        ws_ex_toolwindow = 0x00000080
        swp_nosize = 0x0001
        swp_nomove = 0x0002
        swp_nozorder = 0x0004
        swp_noactivate = 0x0010
        swp_framechanged = 0x0020
        ex_style = int(get_window_long(hwnd, gwl_exstyle) or 0)
        desired_style = (ex_style | ws_ex_toolwindow) & ~ws_ex_appwindow
        if desired_style != ex_style:
            set_window_long(hwnd, gwl_exstyle, desired_style)
            user32.SetWindowPos(
                hwnd,
                0,
                0,
                0,
                0,
                0,
                swp_nosize
                | swp_nomove
                | swp_nozorder
                | swp_noactivate
                | swp_framechanged,
            )
        return True
    except Exception:
        return False


def windows_startup_script() -> Path:
    appdata = (
        Path.home()
        / "AppData"
        / "Roaming"
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
    )
    return appdata / "background_screen_controller.vbs"


def legacy_windows_startup_script() -> Path:
    appdata = (
        Path.home()
        / "AppData"
        / "Roaming"
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
    )
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
    executable = (
        preferred_windows_python_gui_executable()
        if sys.platform == "win32"
        else sys.executable
    )
    return f'"{executable}" "{APP_ROOT / "main.py"}"'


def engine_launch_args() -> list[str]:
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve()), "--engine"]
    executable = (
        preferred_windows_python_gui_executable()
        if sys.platform == "win32"
        else sys.executable
    )
    return [executable, str(APP_ROOT / "main.py"), "--engine"]


def controller_launch_args() -> list[str]:
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve())]
    executable = (
        preferred_windows_python_gui_executable()
        if sys.platform == "win32"
        else sys.executable
    )
    return [executable, str(APP_ROOT / "main.py")]


def playback_worker_launch_args(screen_id: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [
            str(Path(sys.executable).resolve()),
            "--playback-worker",
            "--screen-id",
            screen_id,
        ]
    executable = (
        preferred_windows_python_gui_executable()
        if sys.platform == "win32"
        else sys.executable
    )
    return [
        executable,
        str(APP_ROOT / "main.py"),
        "--playback-worker",
        "--screen-id",
        screen_id,
    ]


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
            connection.readyRead.connect(
                lambda conn=connection: self._handle_ready_read(conn)
            )
            connection.disconnected.connect(
                lambda conn=connection: self._drop_connection(conn)
            )

    def _handle_ready_read(self, connection: QLocalSocket) -> None:
        payload = (
            bytes(connection.readAll()).decode("utf-8", errors="ignore").strip().lower()
        )
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
    if (
        "wi-fi" in lowered
        or "wifi" in lowered
        or "wireless" in lowered
        or "wlan" in lowered
    ):
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
            label = (
                interface.humanReadableName() or interface.name() or "Network Interface"
            )
            if interface_name_is_virtual(label) or interface_name_is_virtual(
                interface.name()
            ):
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

    details.sort(
        key=lambda item: (
            interface_label_priority(item[1]),
            0 if is_private_ipv4(item[0]) else 1,
            item[0],
        )
    )
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
        command = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ]
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
    preferred_interface = next(
        (label for address, label in details if address == preferred_ip), ""
    )
    mdns_host = f"{hostname}.local" if hostname else ""
    warnings: list[str] = []
    if not lan_ips:
        warnings.append("No LAN IPv4 address was detected on this computer.")
    elif len(lan_ips) > 1:
        warnings.append(
            "Multiple LAN addresses were detected. Make sure the TVs are using the same subnet as the preferred IP shown here."
        )
    if is_wsl_runtime():
        warnings.append(
            "This app is running inside WSL. For direct LAN reachability, use the Windows build or Windows Python instead of the WSL runtime."
        )
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


def friendly_remote_name(
    remote_id: str, aliases: dict[str, str], state: dict[str, Any] | None = None
) -> str:
    alias = aliases.get(remote_id, "").strip()
    if alias:
        return alias
    if state is not None:
        name = str(state.get("name") or "").strip()
        if name:
            return name
    return remote_id.removeprefix("remote:")[:8].upper()


def configured_screen_summary(screen: ConfiguredScreen) -> str:
    capability_labels = [
        CAPABILITY_LABELS.get(item, item) for item in screen.capabilities
    ]
    capability_text = (
        ", ".join(capability_labels) if capability_labels else "No capabilities"
    )
    return f"{TRANSPORT_LABELS.get(screen.transport, screen.transport)} • {capability_text}"


def configured_screen_name_map(
    configured_screens: list[ConfiguredScreen],
) -> dict[str, str]:
    return {
        screen.id: screen.name for screen in configured_screens if screen.name.strip()
    }


class LanRemoteServer:
    def __init__(self, port: int = LAN_SERVER_PORT) -> None:
        self.port = port
        self.media_root: Path | None = None
        self._lock = threading.RLock()
        self._commands: dict[str, PlaybackCommand] = {}
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
        self._pairing_required = False
        self._allow_unpaired_clients = False
        self._paired_client_ids: set[str] = set()
        self._pending_clients: dict[str, dict[str, Any]] = {}

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

        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="lan-remote-server", daemon=True
        )
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
            normalized: dict[str, PlaybackCommand] = {}
            for screen_id, payload in commands.items():
                normalized[screen_id] = PlaybackCommand.from_payload(
                    payload, default_screen_id=screen_id
                )
            self._commands = normalized

    def purge_remote_screens(self, include_online: bool = False) -> list[str]:
        removed_ids: list[str] = []
        now = time.time()
        with self._lock:
            for screen_id, state in list(self._remote_screens.items()):
                is_online = (
                    now - float(state.get("last_seen") or 0.0)
                ) <= REMOTE_SCREEN_TIMEOUT_SECONDS
                if include_online or not is_online:
                    removed_ids.append(screen_id)
                    self._remote_screens.pop(screen_id, None)
                    self._commands.pop(screen_id, None)
        if removed_ids:
            self._notify_change()
        return removed_ids

    def merge_remote_screens(self, source_id: str, target_id: str) -> bool:
        source = str(source_id or "").strip()
        target = str(target_id or "").strip()
        if not source.startswith("remote:") or not target.startswith("remote:"):
            return False
        if source == target:
            return False
        merged = False
        with self._lock:
            source_state = self._remote_screens.get(source)
            target_state = self._remote_screens.get(target)
            if source_state is None or target_state is None:
                return False

            source_seen = float(source_state.get("last_seen") or 0.0)
            target_seen = float(target_state.get("last_seen") or 0.0)
            if source_seen > target_seen:
                target_state["last_seen"] = source_seen
                target_state["ip"] = str(
                    source_state.get("ip") or target_state.get("ip") or ""
                )
                target_state["width"] = int(
                    source_state.get("width") or target_state.get("width") or 0
                )
                target_state["height"] = int(
                    source_state.get("height") or target_state.get("height") or 0
                )
                target_state["user_agent"] = str(
                    source_state.get("user_agent")
                    or target_state.get("user_agent")
                    or ""
                )
                target_state["ready"] = bool(source_state.get("ready"))
                target_state["state"] = str(
                    source_state.get("state")
                    or target_state.get("state")
                    or "connected"
                )
                target_state["current_media"] = str(
                    source_state.get("current_media")
                    or target_state.get("current_media")
                    or ""
                )
                target_state["drift_ms"] = source_state.get("drift_ms")
            if (
                not str(target_state.get("client_id") or "").strip()
                and str(source_state.get("client_id") or "").strip()
            ):
                target_state["client_id"] = str(source_state.get("client_id") or "")
            target_state["last_command_version"] = max(
                int(source_state.get("last_command_version") or 0),
                int(target_state.get("last_command_version") or 0),
            )
            self._remote_screens[target] = target_state
            self._remote_screens.pop(source, None)

            source_command = self._commands.pop(source, None)
            if source_command is not None and target not in self._commands:
                if isinstance(source_command, PlaybackCommand):
                    source_command.screen_id = target
                    self._commands[target] = source_command
                elif isinstance(source_command, dict):
                    parsed = PlaybackCommand.from_payload(
                        source_command, default_screen_id=target
                    )
                    parsed.screen_id = target
                    self._commands[target] = parsed
            merged = True
        if merged:
            self._notify_change()
        return merged

    def set_change_callback(self, callback) -> None:
        self._change_callback = callback

    def configure_pairing(
        self,
        required: bool,
        allow_unpaired_clients: bool,
        paired_client_ids: list[str] | None = None,
    ) -> None:
        with self._lock:
            self._pairing_required = bool(required)
            self._allow_unpaired_clients = bool(allow_unpaired_clients)
            self._paired_client_ids = {
                str(item).strip()
                for item in (paired_client_ids or [])
                if str(item).strip()
            }
            if not self._pairing_required:
                self._pending_clients = {}
            self.invalidate_network_snapshot()
        self._notify_change()

    def pending_clients_snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(item)
                for item in sorted(
                    self._pending_clients.values(),
                    key=lambda row: str(row.get("last_seen") or ""),
                    reverse=True,
                )
            ]

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
        requested_client_id = str(payload.get("clientId") or "").strip()

        # Most stable match: persisted browser client id (independent of IP).
        if requested_client_id:
            for screen_id, state in self._remote_screens.items():
                state_client_id = str(state.get("client_id") or "").strip()
                if state_client_id and state_client_id == requested_client_id:
                    return screen_id

        ip_matches: list[str] = []
        for screen_id, state in self._remote_screens.items():
            state_name = str(state.get("name") or "").strip().lower()
            state_ip = str(state.get("ip") or "").strip()
            state_agent = str(state.get("user_agent") or "").strip()
            if state_ip == client_ip:
                ip_matches.append(screen_id)
            if (
                state_ip == client_ip
                and requested_agent
                and state_agent
                and state_agent == requested_agent
            ):
                return screen_id
            if (
                requested_name
                and state_name == requested_name
                and state_ip == client_ip
            ):
                return screen_id
        if len(ip_matches) == 1:
            return ip_matches[0]

        # New screen: derive stable id from client id when available.
        identity_seed = (
            requested_client_id or requested_agent or requested_name or "remote-screen"
        )
        stable_id = uuid.uuid5(uuid.NAMESPACE_URL, identity_seed)
        return f"remote:{stable_id.hex}"

    def register_or_refresh(
        self, payload: dict[str, Any], client_ip: str
    ) -> dict[str, Any]:
        screen_id = self._resolve_remote_screen_id(payload, client_ip)
        state = self._remote_screens.get(screen_id, {})
        requested_name = str(payload.get("name") or "").strip()
        client_id = str(payload.get("clientId") or "").strip()
        if requested_name:
            name = requested_name
        else:
            name = (
                str(state.get("name") or "").strip()
                or f"Remote {screen_id.removeprefix('remote:')[:8].upper()}"
            )
        now = time.time()
        state.update(
            {
                "screen_id": screen_id,
                "name": name,
                "user_agent": str(payload.get("userAgent") or ""),
                "client_id": client_id,
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
                "paired": (
                    (not self._pairing_required)
                    or (not client_id)
                    or (client_id in self._paired_client_ids)
                ),
            }
        )
        self._remote_screens[screen_id] = state
        if (
            self._pairing_required
            and client_id
            and client_id not in self._paired_client_ids
        ):
            self._pending_clients[client_id] = {
                "client_id": client_id,
                "name": name,
                "screen_id": screen_id,
                "ip": client_ip,
                "user_agent": str(payload.get("userAgent") or ""),
                "last_seen": now,
            }
        elif client_id:
            self._pending_clients.pop(client_id, None)
        self.invalidate_network_snapshot()
        return {
            "screenId": screen_id,
            "name": name,
            "pollIntervalMs": REMOTE_POLL_INTERVAL_MS,
            "heartbeatIntervalMs": REMOTE_HEARTBEAT_INTERVAL_MS,
            "serverTimeMs": current_timestamp_ms(),
            "hostname": self._hostname,
            "pairingRequired": self._pairing_required,
            "paired": bool(state.get("paired", True)),
        }

    def update_heartbeat(
        self, payload: dict[str, Any], client_ip: str
    ) -> dict[str, Any]:
        screen_id = str(payload.get("screenId") or "").strip()
        client_id = str(payload.get("clientId") or "").strip()
        if not screen_id.startswith("remote:"):
            return {"ok": False, "error": "invalid_screen_id"}
        now = time.time()
        state = self._remote_screens.get(
            screen_id,
            {
                "screen_id": screen_id,
                "name": screen_id.removeprefix("remote:")[:8].upper(),
            },
        )
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
                "last_command_version": int(
                    payload.get("lastCommandVersion")
                    or state.get("last_command_version")
                    or 0
                ),
                "client_id": client_id or str(state.get("client_id") or ""),
                "paired": (
                    (not self._pairing_required)
                    or (
                        (client_id or str(state.get("client_id") or ""))
                        in self._paired_client_ids
                    )
                ),
            }
        )
        self._remote_screens[screen_id] = state
        effective_client_id = str(state.get("client_id") or "").strip()
        if (
            self._pairing_required
            and effective_client_id
            and effective_client_id not in self._paired_client_ids
        ):
            self._pending_clients[effective_client_id] = {
                "client_id": effective_client_id,
                "name": str(state.get("name") or screen_id),
                "screen_id": screen_id,
                "ip": client_ip,
                "user_agent": str(state.get("user_agent") or ""),
                "last_seen": now,
            }
        elif effective_client_id:
            self._pending_clients.pop(effective_client_id, None)
        self.invalidate_network_snapshot()
        return {"ok": True, "serverTimeMs": current_timestamp_ms()}

    def remote_screens_snapshot(self) -> list[dict[str, Any]]:
        now = time.time()
        with self._lock:
            for state in self._remote_screens.values():
                state["online"] = (
                    now - float(state.get("last_seen") or 0.0)
                ) <= REMOTE_SCREEN_TIMEOUT_SECONDS
            return [
                dict(item)
                for item in sorted(
                    self._remote_screens.values(),
                    key=lambda row: str(
                        row.get("name") or row.get("screen_id") or ""
                    ).lower(),
                )
            ]

    def command_for_screen(self, screen_id: str) -> dict[str, Any]:
        with self._lock:
            state = self._remote_screens.get(screen_id) or {}
            paired = bool(state.get("paired", True))
            if (
                self._pairing_required
                and not self._allow_unpaired_clients
                and not paired
            ):
                return PlaybackCommand.clear(
                    screen_id,
                    message="Pair this screen in the controller before playback can start.",
                ).to_remote_dict()
            direct = self._commands.get(screen_id)
            if direct is not None:
                return direct.to_remote_dict()
            online_screen_ids = [
                remote_id
                for remote_id, state in self._remote_screens.items()
                if bool(state.get("online"))
            ]
            active_commands = [
                command
                for command in self._commands.values()
                if command.command_type == "play"
                and is_remote_screen_id(command.screen_id)
            ]
            if (
                len(online_screen_ids) == 1
                and online_screen_ids[0] == screen_id
                and len(active_commands) == 1
            ):
                fallback = PlaybackCommand.from_payload(
                    active_commands[0].to_engine_dict(),
                    default_screen_id=screen_id,
                )
                fallback.screen_id = screen_id
                return fallback.to_remote_dict()
            return PlaybackCommand.clear(screen_id).to_remote_dict()

    def invalidate_network_snapshot(self) -> None:
        with self._lock:
            self._network_snapshot_cache = None
            self._network_snapshot_cache_until = 0.0

    def network_snapshot(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            if (
                self._network_snapshot_cache is not None
                and now < self._network_snapshot_cache_until
            ):
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
        snapshot["hostname_url"] = (
            f"http://{self._hostname}:{self.port}/tv" if self._hostname else ""
        )
        snapshot["mdnsHost"] = f"{self._hostname}.local" if self._hostname else ""
        snapshot["mdns_url"] = (
            f"http://{self._hostname}.local:{self.port}/tv" if self._hostname else ""
        )
        snapshot["warnings"] = warnings
        snapshot["pairingRequired"] = bool(self._pairing_required)
        snapshot["allowUnpairedClients"] = bool(self._allow_unpaired_clients)
        snapshot["pendingClientCount"] = len(self._pending_clients)
        snapshot["pendingClients"] = self.pending_clients_snapshot()
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
        executable = Path(
            sys.executable if getattr(sys, "frozen", False) else sys.executable
        ).resolve()
        rule_name = f"{APP_NAME} LAN Server"
        try:
            show_rule = subprocess.run(
                [
                    "netsh",
                    "advfirewall",
                    "firewall",
                    "show",
                    "rule",
                    f"name={rule_name}",
                ],
                capture_output=True,
                text=True,
                check=False,
                **windows_hidden_subprocess_kwargs(),
            )
            if show_rule.returncode == 0 and "No rules match" not in (
                show_rule.stdout or ""
            ):
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
                    f"program={executable}",
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

    def _write_json(
        self,
        handler: BaseHTTPRequestHandler,
        payload: dict[str, Any],
        status: int = HTTPStatus.OK,
    ) -> None:
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

    def _write_text(
        self,
        handler: BaseHTTPRequestHandler,
        body: str,
        content_type: str = "text/html; charset=utf-8",
        status: int = HTTPStatus.OK,
    ) -> None:
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
            self._write_text(
                handler, "Not found", "text/plain; charset=utf-8", HTTPStatus.NOT_FOUND
            )
        except Exception as error:  # noqa: BLE001
            self._write_json(
                handler,
                {"ok": False, "error": str(error)},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

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
        self._write_text(
            handler, "Not found", "text/plain; charset=utf-8", HTTPStatus.NOT_FOUND
        )

    def _serve_media(self, handler: BaseHTTPRequestHandler, relative_path: str) -> None:
        with self._lock:
            media_root = self.media_root
        if media_root is None:
            self._write_text(
                handler,
                "No media folder configured",
                "text/plain; charset=utf-8",
                HTTPStatus.NOT_FOUND,
            )
            return
        decoded = Path(unquote(relative_path))
        target = (media_root / decoded).resolve()
        try:
            target.relative_to(media_root.resolve())
        except ValueError:
            self._write_text(
                handler, "Forbidden", "text/plain; charset=utf-8", HTTPStatus.FORBIDDEN
            )
            return
        if not target.exists() or not target.is_file():
            self._write_text(
                handler, "Not found", "text/plain; charset=utf-8", HTTPStatus.NOT_FOUND
            )
            return
        self._serve_file(handler, target, cache_control="public, max-age=60")

    def _serve_absolute_media(
        self, handler: BaseHTTPRequestHandler, absolute_path: str
    ) -> None:
        target = Path(unquote(absolute_path)).expanduser()
        if (
            not target.exists()
            or not target.is_file()
            or target.suffix.lower() not in SUPPORTED_MEDIA_EXTENSIONS
        ):
            self._write_text(
                handler, "Not found", "text/plain; charset=utf-8", HTTPStatus.NOT_FOUND
            )
            return
        self._serve_file(handler, target, cache_control="public, max-age=30")

    def _serve_file(
        self, handler: BaseHTTPRequestHandler, target: Path, cache_control: str
    ) -> None:
        content_type, _encoding = mimetypes.guess_type(str(target))
        try:
            stat = target.stat()
        except OSError:
            self._write_text(
                handler,
                "Unable to read media",
                "text/plain; charset=utf-8",
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
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
                    end = (
                        min(total_size - 1, int(end_text))
                        if end_text
                        else total_size - 1
                    )
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
            self._write_text(
                handler,
                "Unable to read media",
                "text/plain; charset=utf-8",
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
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
      const COMMAND_PROTOCOL_VERSION = __COMMAND_PROTOCOL_VERSION__;
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
      let imageCycleTimer = null;
      let imageCycleUrls = [];
      let imageCycleIndex = 0;
      let lastVideoProgressAt = Date.now();
      let lastVideoPosition = 0;
      let videoRecoveryCount = 0;

      function makeClientId() {
        try {
          if (window.crypto && typeof window.crypto.randomUUID === 'function') {
            return window.crypto.randomUUID();
          }
        } catch (_err) {}
        return 'client-' + Math.random().toString(36).slice(2) + '-' + Date.now().toString(36);
      }
      function loadState() {
        try { remoteState = JSON.parse(localStorage.getItem(storeKey) || '{}') || {}; } catch (_err) { remoteState = {}; }
        if (!remoteState.clientId || typeof remoteState.clientId !== 'string') {
          remoteState.clientId = makeClientId();
          saveState();
        }
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
          clientId: remoteState.clientId || '',
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
      function clearImageCycle() {
        if (imageCycleTimer) {
          clearInterval(imageCycleTimer);
          imageCycleTimer = null;
        }
        imageCycleUrls = [];
        imageCycleIndex = 0;
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
        clearImageCycle();
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
        clearImageCycle();
        const mediaUrls = Array.isArray(command.mediaUrls) && command.mediaUrls.length ? command.mediaUrls : [command.mediaUrl];
        const loader = new Image();
        loader.onload = () => {
          const run = () => {
            fadeToBlack(command.transition !== 'cut');
            stopVideos();
            imageLayer.src = command.mediaUrl;
            imageLayer.style.opacity = '1';
            currentKind = 'image';
            activeMedia = command.label || command.relativePath || '';
            activeSourceUrl = mediaUrls[0] || '';
            activeCommand = command;
            message.style.opacity = '0';
            setStatus((remoteState.name || 'Remote Screen') + ' showing image');
            setTimeout(() => fadeToBlack(false), command.transition === 'cut' ? 20 : 220);
            if (command.cycle && mediaUrls.length > 1) {
              imageCycleUrls = mediaUrls.slice();
              imageCycleIndex = 0;
              const intervalMs = Math.max(2000, Number(command.cycleIntervalSeconds || 10) * 1000);
              imageCycleTimer = setInterval(() => {
                if (!imageCycleUrls.length || currentKind !== 'image') return;
                imageCycleIndex = (imageCycleIndex + 1) % imageCycleUrls.length;
                imageLayer.src = imageCycleUrls[imageCycleIndex];
                activeSourceUrl = imageLayer.src || imageCycleUrls[imageCycleIndex];
              }, intervalMs);
            }
          };
          const delay = Math.max(0, Number(command.playAtMs || 0) - nowServerMs());
          pendingTimer = setTimeout(run, delay);
        };
        loader.onerror = () => applyClear({ message: 'Unable to load image.' });
        loader.src = mediaUrls[0] || command.mediaUrl;
      }
      function scheduleVideo(command) {
        clearPending();
        clearImageCycle();
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
        if (!command) return;
        const protocolVersion = Number(command.protocolVersion || 1);
        if (protocolVersion > COMMAND_PROTOCOL_VERSION) {
          applyClear({ message: 'Client update required for this command protocol.' });
          setStatus((remoteState.name || 'Remote Screen') + ' protocol mismatch');
          return;
        }
        if (Number(command.version || 0) === currentVersion) return;
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
        return (
            html.replace("__POLL_MS__", str(REMOTE_POLL_INTERVAL_MS))
            .replace("__HEARTBEAT_MS__", str(REMOTE_HEARTBEAT_INTERVAL_MS))
            .replace("__COMMAND_PROTOCOL_VERSION__", str(COMMAND_PROTOCOL_VERSION))
        )


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
        self.image_cycle_paths: list[Path] = []
        self.image_cycle_index = 0
        self.image_cycle_timer = QTimer(self)
        self.image_cycle_timer.setSingleShot(False)
        self.image_cycle_timer.timeout.connect(self.advance_image_cycle)

        self.setWindowTitle(title)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("background: #000;")
        self.setMouseTracking(True)
        self.setWindowFlags(playback_window_flags_for_platform())

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
            "color: #e9f4ff; font-size: 20px; padding: 18px 22px;"
            " background: rgba(23,48,67,0.44); border: 1px solid rgba(132,220,198,0.35);"
            " border-radius: 18px;"
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
                background: rgba(23, 48, 67, 0.42);
                border: 1px solid rgba(132,220,198,0.38);
                border-radius: 999px;
            }
            QPushButton {
                background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #84dcc6, stop:1 #5db7f0);
                color: #0f2740;
                border: 1px solid rgba(255,255,255,0.28);
                border-radius: 999px;
                padding: 10px 18px;
                font-weight: 700;
            }
            QPushButton:pressed {
                background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #70ccb5, stop:1 #4ea8df);
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

    def apply_surface_window_mode(self) -> None:
        apply_windows_playback_surface_mode(self)

    def show_on_screen(self) -> None:
        geometry = self.screen_ref.geometry()
        self.setGeometry(geometry)
        self.move(geometry.topLeft())
        self.showFullScreen()
        self.apply_surface_window_mode()
        QTimer.singleShot(0, self.apply_surface_window_mode)
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

    def should_run_playback_watchdog(self) -> bool:
        return (
            self.current_media_kind == "video"
            and not self.is_paused
            and self.current_video_path is not None
        )

    def sync_playback_watchdog_timer(self) -> None:
        if not hasattr(self, "playback_watchdog_timer"):
            return
        if self.should_run_playback_watchdog():
            if not self.playback_watchdog_timer.isActive():
                self.playback_watchdog_timer.start()
            return
        self.playback_watchdog_timer.stop()

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
        self.sync_playback_watchdog_timer()

    def set_transition_method(self, method: str) -> None:
        self.transition_method = (
            method if method in TRANSITION_METHODS else "fade_black"
        )

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

    def animate_opacity(
        self,
        effect: QGraphicsOpacityEffect,
        target: float,
        duration: int,
        on_finished=None,
    ) -> None:
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

        self.animate_opacity(
            self.black_overlay_effect, target, duration, done if callback else None
        )

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
        self.image_cycle_timer.stop()
        self.image_cycle_paths = []
        self.image_cycle_index = 0
        self.playback_watchdog_timer.stop()
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

    def clear_playback_at(
        self, message: str, play_at_ms: int | None = None, show_message: bool = True
    ) -> None:
        self.schedule_action(
            lambda: self.clear_playback(message, show_message=show_message), play_at_ms
        )

    def is_runtime_alive(self) -> bool:
        return (
            not self._is_closing
            and isValid(self)
            and hasattr(self, "player")
            and isValid(self.player)
        )

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

    def switch_to_source(
        self,
        source_key: str,
        path: Path,
        display_label: str,
        entry_id: str | None,
        force_reload: bool = False,
    ) -> None:
        if not self.is_runtime_alive():
            return
        source_signature = self.source_signature(path)
        if source_signature is None or not path.exists():
            self.clear_playback(f"Missing media file:\n{display_label}")
            return

        if (
            not force_reload
            and self.current_source_key == source_key
            and self.current_source_signature == source_signature
        ):
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
        duration = (
            220 if self.transition_method == "soft_fade" else TRANSITION_TO_BLACK_MS
        )
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
            lambda: self.switch_to_source(
                source_key, path, display_label, entry_id, force_reload=force_reload
            ),
            play_at_ms,
        )

    def switch_to_entry(
        self,
        entry: ScheduleEntry | None,
        video_directory: Path | None,
        force_reload: bool = False,
        play_at_ms: int | None = None,
    ) -> None:
        if not self.is_runtime_alive():
            return
        if entry is None:
            self.clear_playback_at(
                "No media scheduled for the current UK time.", play_at_ms
            )
            return

        video_path = (
            (video_directory / entry.video_file)
            if video_directory is not None
            else None
        )
        if video_path is None or not video_path.exists():
            self.clear_playback_at(
                f"Missing media file:\n{entry.video_label or entry.video_file}",
                play_at_ms,
            )
            return
        self.switch_to_source_at(
            f"schedule:{entry.id}",
            video_path,
            entry.video_label or entry.video_file,
            entry.id,
            play_at_ms=play_at_ms,
            force_reload=force_reload,
        )

    def switch_to_override(
        self,
        path: Path,
        label: str,
        force_reload: bool = False,
        play_at_ms: int | None = None,
    ) -> None:
        if not self.is_runtime_alive():
            return
        self.image_cycle_timer.stop()
        self.image_cycle_paths = []
        self.image_cycle_index = 0
        self.switch_to_source_at(
            f"override:{self.screen_id}:{path.resolve()}",
            path,
            label,
            f"override:{self.screen_id}",
            play_at_ms=play_at_ms,
            force_reload=force_reload,
        )

    def switch_to_image_cycle(
        self,
        paths: list[Path],
        label: str,
        interval_seconds: int,
        play_at_ms: int | None = None,
    ) -> None:
        if not self.is_runtime_alive() or len(paths) < 2:
            return

        def apply_cycle() -> None:
            valid_paths = [
                path for path in paths if path.exists() and is_image_file(path)
            ]
            if len(valid_paths) < 2:
                return
            self.image_cycle_paths = valid_paths
            self.image_cycle_index = 0
            self.switch_to_source(
                f"cycle:{self.screen_id}:{'|'.join(str(path.resolve()) for path in valid_paths)}:0",
                valid_paths[0],
                label or valid_paths[0].name,
                self.current_entry_id,
                force_reload=True,
            )
            self.image_cycle_timer.start(max(2000, int(interval_seconds) * 1000))

        self.schedule_action(apply_cycle, play_at_ms)

    def advance_image_cycle(self) -> None:
        if not self.is_runtime_alive() or len(self.image_cycle_paths) < 2:
            self.image_cycle_timer.stop()
            return
        self.image_cycle_index = (self.image_cycle_index + 1) % len(
            self.image_cycle_paths
        )
        current_path = self.image_cycle_paths[self.image_cycle_index]
        self.switch_to_source(
            f"cycle:{self.screen_id}:{'|'.join(str(path.resolve()) for path in self.image_cycle_paths)}:{self.image_cycle_index}",
            current_path,
            current_path.name,
            self.current_entry_id,
            force_reload=True,
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
            self.sync_playback_watchdog_timer()
            self.update_image_display()
            if self.transition_method == "cut":
                self.black_overlay_effect.setOpacity(0.0)
            else:
                duration = (
                    380
                    if self.transition_method == "soft_fade"
                    else TRANSITION_FROM_BLACK_MS
                )
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
            self.sync_playback_watchdog_timer()
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
        if (
            status
            in {
                QMediaPlayer.MediaStatus.LoadedMedia,
                QMediaPlayer.MediaStatus.BufferedMedia,
            }
            and self.current_source_key is not None
            and self._ready_source_key != self.current_source_key
        ):
            self._ready_source_key = self.current_source_key
            if not self.is_paused:
                self.player.play()
            if self.transition_method == "cut":
                self.black_overlay_effect.setOpacity(0.0)
            else:
                duration = (
                    380
                    if self.transition_method == "soft_fade"
                    else TRANSITION_FROM_BLACK_MS
                )
                self.fade_black_to(0.0, duration)
        elif status == QMediaPlayer.MediaStatus.StalledMedia:
            append_engine_startup_log(
                f"Playback stalled on {self.screen_id}; attempting recovery."
            )
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
        if (
            self.current_media_kind != "video"
            or self.is_paused
            or self.current_video_path is None
        ):
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
        if (
            not self.is_runtime_alive()
            or self.current_media_kind != "video"
            or self.current_video_path is None
        ):
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
            append_engine_startup_log(
                f"Failed to recover stalled video on {self.screen_id}: {error}"
            )

    def closeEvent(self, event: QCloseEvent) -> None:  # type: ignore[override]
        self._is_closing = True
        self.controls_hide_timer.stop()
        self.scheduled_action_timer.stop()
        self.playback_watchdog_timer.stop()
        self.image_cycle_timer.stop()
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
        self.virtual_screen_ids: set[str] = set()
        self.screen_groups: list[ScreenGroup] = []
        self.command_version = 0
        self.screen_commands: dict[str, PlaybackCommand] = {}
        self.assignment_service = PlaybackAssignmentService()
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
        self.transition_method = (
            method if method in TRANSITION_METHODS else "fade_black"
        )
        if self.manage_local_windows:
            for screen_id, window in list(self.windows.items()):
                if not self.is_window_usable(screen_id, window):
                    continue
                window.set_transition_method(self.transition_method)
        # Transition method is part of command semantics. Force command refresh/version bump
        # so LAN clients and local worker processes apply the new transition immediately.
        self.last_assignment_keys = {}
        self.sync_current_entry(force=True)

    def set_virtual_screen_ids(self, screen_ids: set[str]) -> None:
        self.virtual_screen_ids = set(screen_ids)
        self.sync_current_entry(force=True)

    def set_screen_groups(self, groups: list[ScreenGroup]) -> None:
        self.screen_groups = [
            ScreenGroup.from_dict(group.to_dict()) for group in groups
        ]
        self.sync_current_entry(force=True)

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
        targets = (
            list(self.quick_play_paths.keys())
            if target_screen_ids is None
            else target_screen_ids
        )
        for screen_id in targets:
            self.quick_play_paths.pop(screen_id, None)
            self.quick_play_labels.pop(screen_id, None)
            if (
                self.manage_local_windows
                and screen_id not in self.virtual_screen_ids
                and not is_remote_screen_id(screen_id)
                and screen_id not in self.selected_monitor_ids
                and screen_id in self.windows
            ):
                window = self.windows.pop(screen_id)
                if isValid(window):
                    window.close()
        self.playback_state_changed.emit(self.is_paused, len(self.windows))
        self.sync_current_entry(force=True)

    def all_active_screen_ids(self) -> list[str]:
        if not self.playback_enabled:
            return sorted(set(self.quick_play_paths.keys()))
        schedule_targets = {
            screen_id
            for entry in self.schedules
            for screen_id in expand_target_ids(entry.screen_ids, self.screen_groups)
        }
        return sorted(
            set(self.selected_monitor_ids)
            | set(self.quick_play_paths.keys())
            | schedule_targets
        )

    def has_launch_targets(self) -> bool:
        if self.selected_monitor_ids or self.quick_play_paths:
            return True
        return any(bool(entry.screen_ids) for entry in self.schedules)

    def assignment_for_screen(
        self, screen_id: str, weekday_index: int, minute_of_day: int
    ) -> dict[str, Any]:
        return self.assignment_service.assignment_for_screen(
            screen_id=screen_id,
            weekday_index=weekday_index,
            minute_of_day=minute_of_day,
            quick_play_paths=self.quick_play_paths,
            quick_play_labels=self.quick_play_labels,
            is_paused=self.is_paused,
            schedules=self.schedules,
            screen_groups=self.screen_groups,
            video_directory=self.video_directory,
        )

    def build_screen_command(
        self, screen_id: str, assignment: dict[str, Any], play_at_ms: int, version: int
    ) -> PlaybackCommand:
        return self.assignment_service.build_screen_command(
            screen_id=screen_id,
            assignment=assignment,
            play_at_ms=play_at_ms,
            version=version,
            transition_method=self.transition_method,
            is_paused=self.is_paused,
        )

    def sync_current_entry(self, force: bool = False) -> None:
        _, minute_of_day, weekday_index = current_uk_time()
        assignments = {
            screen_id: self.assignment_for_screen(
                screen_id, weekday_index, minute_of_day
            )
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
        assignment_keys = {
            screen_id: str(assignment.get("key") or "")
            for screen_id, assignment in assignments.items()
        }
        changed_screen_ids = {
            screen_id
            for screen_id, key in assignment_keys.items()
            if self.last_assignment_keys.get(screen_id) != key
        }
        removed_screen_ids = set(self.last_assignment_keys) - set(assignment_keys)
        schedule_update = force or bool(changed_screen_ids) or bool(removed_screen_ids)
        play_at_ms = (
            current_timestamp_ms() + REMOTE_SYNC_LEAD_MS if schedule_update else None
        )
        first_entry_id: str | None = None
        if self.manage_local_windows:
            for screen_id, window in list(self.windows.items()):
                if not self.is_window_usable(screen_id, window):
                    continue
                assignment = assignments.get(screen_id)
                if assignment is None:
                    if (
                        not is_remote_screen_id(screen_id)
                        and screen_id not in self.virtual_screen_ids
                    ):
                        self.windows.pop(screen_id, None)
                        if isValid(window):
                            window.close()
                        continue
                if (
                    assignment.get("type") == "play"
                    and assignment.get("mode") == "quick_play"
                ):
                    override_path = assignment.get("path")
                    if isinstance(override_path, Path):
                        window.switch_to_override(
                            override_path,
                            str(assignment.get("label") or override_path.name),
                            force_reload=force or screen_id in changed_screen_ids,
                            play_at_ms=play_at_ms,
                        )
                elif assignment.get("type") == "play":
                    entry = next(
                        (
                            item
                            for item in self.schedules
                            if item.id == assignment.get("entry_id")
                        ),
                        None,
                    )
                    if first_entry_id is None and entry is not None:
                        first_entry_id = entry.id
                    if (
                        bool(assignment.get("cycle"))
                        and len(assignment.get("paths") or []) > 1
                    ):
                        cycle_paths = [
                            path
                            for path in assignment.get("paths") or []
                            if isinstance(path, Path)
                        ]
                        if len(cycle_paths) > 1:
                            window.switch_to_image_cycle(
                                cycle_paths,
                                str(assignment.get("label") or cycle_paths[0].name),
                                int(assignment.get("cycle_interval_seconds") or 10),
                                play_at_ms=play_at_ms,
                            )
                            continue
                    window.switch_to_entry(
                        entry,
                        self.video_directory,
                        force_reload=force or screen_id in changed_screen_ids,
                        play_at_ms=play_at_ms,
                    )
                else:
                    if (
                        screen_id not in self.selected_monitor_ids
                        and not is_remote_screen_id(screen_id)
                        and screen_id not in self.virtual_screen_ids
                    ):
                        self.windows.pop(screen_id, None)
                        if isValid(window):
                            window.close()
                        continue
                    window.clear_playback_at(
                        str(
                            assignment.get("message")
                            or "No media scheduled for the current UK time."
                        ),
                        play_at_ms,
                    )
        for screen_id, assignment in assignments.items():
            if (
                assignment.get("type") == "play"
                and assignment.get("mode") == "schedule"
                and first_entry_id is None
            ):
                first_entry_id = str(assignment.get("entry_id") or "") or None
        if schedule_update:
            self.command_version += 1
            version = self.command_version
            for screen_id, assignment in assignments.items():
                self.screen_commands[screen_id] = self.build_screen_command(
                    screen_id, assignment, play_at_ms or current_timestamp_ms(), version
                )
            for screen_id in removed_screen_ids:
                self.screen_commands.pop(screen_id, None)
            self.last_assignment_keys = assignment_keys
            self.commands_changed.emit()
        if force or first_entry_id != self.current_entry_id:
            self.current_entry_id = first_entry_id
        self.emit_status_for_current_time(weekday_index, minute_of_day)
        self.schedule_next_update()
        self.playback_state_changed.emit(self.is_paused, len(self.windows))

    def emit_status_for_current_time(
        self, weekday_index: int, minute_of_day: int
    ) -> None:
        clock_label, _minute, _weekday = current_uk_time()
        if not self.playback_enabled and not self.quick_play_paths:
            self.status_changed.emit(clock_label, "Playback stopped")
            return
        active_entries = [
            entry
            for screen_id in self.selected_monitor_ids
            if (
                entry := active_schedule_for_screen(
                    self.schedules,
                    weekday_index,
                    minute_of_day,
                    screen_id,
                    self.screen_groups,
                )
            )
            is not None
        ]
        if not active_entries:
            status = "No active schedule"
        else:
            unique_labels = list(
                dict.fromkeys(
                    f"{entry.title} • {entry.range_label}" for entry in active_entries
                )
            )
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
        return self.assignment_service.next_schedule_change_delay_ms(
            schedules=self.schedules,
            now=current_uk_datetime(),
            tz=UK_TZ,
        )

    def command_snapshots(self) -> dict[str, dict[str, Any]]:
        return {
            screen_id: command.to_engine_dict()
            for screen_id, command in self.screen_commands.items()
        }

    def remote_command_snapshots(self) -> dict[str, dict[str, Any]]:
        return {
            screen_id: command.to_engine_dict()
            for screen_id, command in self.screen_commands.items()
            if is_remote_screen_id(screen_id)
        }

    def register_window(self, window: PlaybackWindow) -> None:
        self.windows[window.screen_id] = window
        window.destroyed.connect(
            lambda *_args, screen_id=window.screen_id: self.on_window_destroyed(
                screen_id
            )
        )

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
    command_payload_received = Signal(object)

    def __init__(self, screen_id: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        screen = find_screen_by_id(screen_id)
        if screen is None:
            raise RuntimeError(f"Display {screen_id} is no longer available.")
        self.screen_id = screen_id
        self.window = PlaybackWindow(screen, "Background Screen")
        self.window.show_on_screen()
        self.current_version = 0
        self.current_command_type = "clear"
        self.current_command_paused = False
        self.command_payload_received.connect(self.on_command_payload_received)
        self._stop_event = threading.Event()
        self._subscription_thread: threading.Thread | None = None
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.stop)
        self.start_subscription_loop()

    def start_subscription_loop(self) -> None:
        if (
            self._subscription_thread is not None
            and self._subscription_thread.is_alive()
        ):
            return
        self._stop_event.clear()
        self._subscription_thread = threading.Thread(
            target=self._run_subscription_loop,
            name=f"local-worker-subscription-{self.screen_id}",
            daemon=True,
        )
        self._subscription_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if (
            self._subscription_thread is not None
            and self._subscription_thread.is_alive()
        ):
            self._subscription_thread.join(timeout=1.0)
        self._subscription_thread = None

    def _run_subscription_loop(self) -> None:
        client = ControllerApiClient()
        while not self._stop_event.is_set():
            try:
                payload = client.wait_for_local_worker_command(
                    self.screen_id,
                    self.current_version,
                    timeout=25.0,
                )
            except Exception:
                if self._stop_event.wait(0.75):
                    return
                continue

            command_payload = payload.get("command")
            if not isinstance(command_payload, dict):
                continue
            if not payload.get("updated"):
                continue
            self.command_payload_received.emit(command_payload)

    def on_command_payload_received(self, command_payload: object) -> None:
        if not isinstance(command_payload, dict):
            return
        command = PlaybackCommand.from_payload(
            command_payload, default_screen_id=self.screen_id
        )
        if command.protocol_version > COMMAND_PROTOCOL_VERSION:
            append_engine_diagnostics_event(
                "local_worker_protocol_mismatch",
                severity="warning",
                details="Unsupported command protocol version for local worker.",
                context={
                    "screenId": self.screen_id,
                    "commandProtocolVersion": command.protocol_version,
                    "supportedProtocolVersion": COMMAND_PROTOCOL_VERSION,
                },
            )
            command = PlaybackCommand.clear(
                self.screen_id,
                version=max(1, int(command.version)),
                message="Unsupported command protocol version.",
            )

        version = int(command.version or 0)
        if version == self.current_version:
            return

        self.current_version = version
        self.current_command_type = command.command_type
        self.current_command_paused = bool(command.paused)
        self.apply_command(command)

    def apply_command(self, command: PlaybackCommand) -> None:
        self.window.set_transition_method(str(command.transition or "fade_black"))
        self.window.set_paused(bool(command.paused))
        media_path = Path(command.path).expanduser() if command.path else None
        media_paths = [Path(item).expanduser() for item in command.paths if item]
        media_label = command.label or (
            media_path.name if isinstance(media_path, Path) else ""
        )
        play_at_ms = int(command.play_at_ms or 0) or None

        if command.command_type != "play":
            message = str(command.message or "")
            show_message = bool(message and "Missing media file" in message)
            self.window.clear_playback_at(
                message, play_at_ms, show_message=show_message
            )
            return

        if bool(command.cycle) and len(media_paths) > 1:
            valid_paths = [path for path in media_paths if path.exists()]
            if len(valid_paths) > 1:
                self.window.switch_to_image_cycle(
                    valid_paths,
                    media_label or valid_paths[0].name,
                    int(command.cycle_interval_seconds or 10),
                    play_at_ms=play_at_ms,
                )
                return

        if media_path is None or not media_path.exists():
            message = str(command.message or "")
            show_message = bool(message and "Missing media file" in message)
            self.window.clear_playback_at(
                message, play_at_ms, show_message=show_message
            )
            return

        self.window.switch_to_override(
            media_path,
            media_label or media_path.name,
            force_reload=True,
            play_at_ms=play_at_ms,
        )


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
        super().setText(
            metrics.elidedText(
                self._full_text, Qt.TextElideMode.ElideRight, self.width() - 6
            )
        )


class MediaPickerDialog(QDialog):
    def __init__(
        self,
        media_files: list[Path],
        media_directory: Path | None,
        selected_file: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        apply_dialog_theme(self)
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
        self.preview_stack.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
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

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
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

    def on_item_changed(
        self, current: QListWidgetItem | None, previous: QListWidgetItem | None
    ) -> None:
        selected = (
            "" if current is None else str(current.data(Qt.ItemDataRole.UserRole) or "")
        )
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
            self.preview_pixmap = load_pixmap_for_bounds(
                media_path, self.image_preview.size()
            )
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
    def __init__(
        self, title: str, initial_time: QTime, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        apply_dialog_theme(self)
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
        self.hour_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.hour_list.setSpacing(2)
        self.hour_list.setFixedWidth(74)
        self.hour_list.setFixedHeight(220)

        self.minute_list = QListWidget()
        self.minute_list.setObjectName("timePickerList")
        self.minute_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.minute_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
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

        apply_dialog_theme(
            self,
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
            """,
        )
        self.resize(206, 338)
        QTimer.singleShot(0, self._prime_focus)

    def _prime_focus(self) -> None:
        self.hour_list.setFocus()
        self.hour_list.scrollToItem(
            self.hour_list.currentItem(), QListWidget.ScrollHint.PositionAtCenter
        )
        self.minute_list.scrollToItem(
            self.minute_list.currentItem(), QListWidget.ScrollHint.PositionAtCenter
        )

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
        apply_dialog_theme(self)
        self.setWindowTitle("Choose Quick Play Screens")
        self.setModal(True)
        self.resize(360, 280)
        self.screen_checkboxes: dict[str, QCheckBox] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        title = QLabel("Choose where to play the dropped media")
        title.setObjectName("sectionTitle")
        body = QLabel(
            f"{media_name} will override scheduled playback on the chosen screen(s)."
        )
        body.setObjectName("sectionDescription")
        body.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(body)

        list_holder = QWidget()
        list_layout = QVBoxLayout(list_holder)
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_layout.setSpacing(8)
        for target in targets:
            checkbox = QCheckBox(
                target.label if target.online else f"{target.label} (offline)"
            )
            checkbox.setChecked(
                target.id in selected_monitor_ids
                if selected_monitor_ids
                else len(self.screen_checkboxes) == 0
            )
            checkbox.setToolTip(target.detail or target.label)
            self.screen_checkboxes[target.id] = checkbox
            list_layout.addWidget(checkbox)
        list_layout.addStretch(1)
        layout.addWidget(list_holder, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Start Quick Play")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_screen_ids(self) -> list[str]:
        return [
            screen_id
            for screen_id, checkbox in self.screen_checkboxes.items()
            if checkbox.isChecked()
        ]


class ConfiguredScreenEditorDialog(QDialog):
    def __init__(
        self,
        screen: ConfiguredScreen | None = None,
        remote_screens: dict[str, dict[str, Any]] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        apply_dialog_theme(self)
        self.screen = screen
        self.remote_screens = {
            str(key): dict(value) for key, value in (remote_screens or {}).items()
        }
        self.setWindowTitle("Configured Screen")
        self.setModal(True)
        self.resize(520, 420)
        self.capability_checks: dict[str, QCheckBox] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        title = QLabel("Define a screen and the protocol it must use")
        title.setObjectName("sectionTitle")
        body = QLabel(
            "This phase stores the screen contract now. Later phases will bind each protocol to real playback and discovery."
        )
        body.setObjectName("sectionDescription")
        body.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(body)

        form = QFormLayout()
        form.setSpacing(12)
        self.name_edit = QLineEdit(self)
        form.addRow("Name", self.name_edit)
        self.transport_selector = QComboBox(self)
        for transport in SCREEN_TRANSPORTS:
            self.transport_selector.addItem(
                TRANSPORT_LABELS.get(transport, transport), transport
            )
        self.transport_selector.currentIndexChanged.connect(self.on_transport_changed)
        form.addRow("Protocol", self.transport_selector)
        self.binding_edit = QLineEdit(self)
        self.binding_edit.setPlaceholderText(
            "Optional device id, receiver id, or protocol-specific binding"
        )
        form.addRow("Binding", self.binding_edit)
        layout.addLayout(form)

        self.binding_selector = QComboBox(self)
        self.binding_selector.currentIndexChanged.connect(self.on_binding_changed)
        layout.addWidget(self.binding_selector)

        capability_title = QLabel("Capabilities")
        capability_title.setObjectName("sectionDescription")
        layout.addWidget(capability_title)
        capability_holder = QWidget(self)
        capability_layout = QGridLayout(capability_holder)
        capability_layout.setContentsMargins(0, 0, 0, 0)
        capability_layout.setHorizontalSpacing(12)
        capability_layout.setVerticalSpacing(8)
        for index, capability in enumerate(SCREEN_CAPABILITIES):
            checkbox = QCheckBox(
                CAPABILITY_LABELS.get(capability, capability), capability_holder
            )
            self.capability_checks[capability] = checkbox
            capability_layout.addWidget(checkbox, index // 2, index % 2)
        layout.addWidget(capability_holder)

        self.message_label = QLabel("", self)
        self.message_label.setObjectName("messageBanner")
        self.message_label.hide()
        layout.addWidget(self.message_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if screen is not None:
            self.name_edit.setText(screen.name)
            initial_binding = browser_binding_remote_id(screen) or screen.binding.get(
                "value", ""
            )
            self.binding_edit.setText(initial_binding)
            selected_index = self.transport_selector.findData(screen.transport)
            if selected_index >= 0:
                self.transport_selector.setCurrentIndex(selected_index)
            for capability in screen.capabilities:
                checkbox = self.capability_checks.get(capability)
                if checkbox is not None:
                    checkbox.setChecked(True)
        else:
            self.transport_selector.setCurrentIndex(0)
        self.on_transport_changed()

    def on_transport_changed(self) -> None:
        transport = str(self.transport_selector.currentData() or "")
        supported = set(supported_capabilities(transport))
        should_seed_defaults = self.screen is None and not any(
            item.isChecked() for item in self.capability_checks.values()
        )
        for capability, checkbox in self.capability_checks.items():
            allowed = capability in supported
            checkbox.setEnabled(allowed)
            if not allowed:
                checkbox.setChecked(False)
            elif should_seed_defaults:
                checkbox.setChecked(True)
        self.refresh_transport_binding_selector()

    def refresh_transport_binding_selector(self) -> None:
        transport = str(self.transport_selector.currentData() or "")
        self.binding_selector.setVisible(transport == "browser")
        if transport != "browser":
            return
        self.binding_selector.blockSignals(True)
        self.binding_selector.clear()
        self.binding_selector.addItem(
            "Bind manually or choose a connected browser receiver", ""
        )
        for screen_id, state in sorted(
            self.remote_screens.items(),
            key=lambda item: friendly_remote_name(item[0], {}, item[1]).lower(),
        ):
            online_text = "online" if state.get("online") else "offline"
            label = f"{friendly_remote_name(screen_id, {}, state)} ({online_text})"
            self.binding_selector.addItem(label, screen_id)
        current_binding = self.binding_edit.text().strip()
        if current_binding:
            index = self.binding_selector.findData(current_binding)
            if index >= 0:
                self.binding_selector.setCurrentIndex(index)
        self.binding_selector.blockSignals(False)

    def on_binding_changed(self) -> None:
        binding_value = str(self.binding_selector.currentData() or "")
        if binding_value:
            self.binding_edit.setText(binding_value)

    def selected_capabilities(self) -> list[str]:
        return [
            capability
            for capability, checkbox in self.capability_checks.items()
            if checkbox.isChecked()
        ]

    def build_screen(self) -> ConfiguredScreen:
        binding_value = self.binding_edit.text().strip()
        binding: dict[str, str] = {}
        transport = str(self.transport_selector.currentData() or "")
        if binding_value:
            if transport == "browser":
                binding = {BROWSER_BINDING_KEY: binding_value}
            else:
                binding = {"value": binding_value}
        screen = ConfiguredScreen(
            id=self.screen.id if self.screen is not None else uuid.uuid4().hex,
            name=self.name_edit.text().strip(),
            transport=transport,
            capabilities=self.selected_capabilities(),
            binding=binding,
        )
        screen.validate()
        return screen

    def accept(self) -> None:  # type: ignore[override]
        try:
            self.build_screen()
        except ValueError as error:
            self.message_label.setText(str(error))
            self.message_label.show()
            return
        self.message_label.hide()
        super().accept()


class ConfiguredScreenManagerDialog(QDialog):
    def __init__(
        self,
        screens: list[ConfiguredScreen],
        remote_screens: dict[str, dict[str, Any]] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        apply_dialog_theme(self)
        self._screens = [
            ConfiguredScreen.from_dict(screen.to_dict()) for screen in screens
        ]
        self.remote_screens = {
            str(key): dict(value) for key, value in (remote_screens or {}).items()
        }
        self.setWindowTitle("Configured Screens")
        self.setModal(True)
        self.resize(720, 440)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        title = QLabel("Configured Screens")
        title.setObjectName("sectionTitle")
        body = QLabel(
            "Each screen keeps an explicit protocol and capability contract. Unsupported combinations are blocked before they can be saved."
        )
        body.setObjectName("sectionDescription")
        body.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(body)

        self.screen_list = QListWidget(self)
        self.screen_list.itemDoubleClicked.connect(
            lambda _item: self.edit_selected_screen()
        )
        layout.addWidget(self.screen_list, 1)

        button_row = QHBoxLayout()
        self.add_button = QPushButton("Add", self)
        self.add_button.clicked.connect(self.add_screen)
        self.edit_button = QPushButton("Edit", self)
        self.edit_button.clicked.connect(self.edit_selected_screen)
        self.delete_button = QPushButton("Delete", self)
        self.delete_button.setProperty("variant", "danger")
        self.delete_button.clicked.connect(self.delete_selected_screen)
        button_row.addWidget(self.add_button)
        button_row.addWidget(self.edit_button)
        button_row.addWidget(self.delete_button)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Save,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.refresh_list()

    def refresh_list(self) -> None:
        self.screen_list.clear()
        for screen in self._screens:
            item = QListWidgetItem(screen.name)
            item.setData(Qt.ItemDataRole.UserRole, screen.id)
            item.setToolTip(configured_screen_summary(screen))
            item.setText(f"{screen.name}\n{configured_screen_summary(screen)}")
            self.screen_list.addItem(item)
        if self.screen_list.count():
            self.screen_list.setCurrentRow(0)

    def selected_index(self) -> int:
        return self.screen_list.currentRow()

    def add_screen(self) -> None:
        dialog = ConfiguredScreenEditorDialog(
            remote_screens=self.remote_screens,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._screens.append(dialog.build_screen())
        self.refresh_list()

    def edit_selected_screen(self) -> None:
        index = self.selected_index()
        if index < 0 or index >= len(self._screens):
            return
        dialog = ConfiguredScreenEditorDialog(
            self._screens[index], self.remote_screens, self
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._screens[index] = dialog.build_screen()
        self.refresh_list()
        self.screen_list.setCurrentRow(index)

    def delete_selected_screen(self) -> None:
        index = self.selected_index()
        if index < 0 or index >= len(self._screens):
            return
        del self._screens[index]
        self.refresh_list()
        if self.screen_list.count():
            self.screen_list.setCurrentRow(min(index, self.screen_list.count() - 1))

    def configured_screens(self) -> list[ConfiguredScreen]:
        return [
            ConfiguredScreen.from_dict(screen.to_dict()) for screen in self._screens
        ]


class ScreenGroupEditorDialog(QDialog):
    def __init__(
        self,
        targets: list[UnifiedScreenTarget],
        group: ScreenGroup | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        apply_dialog_theme(self)
        self.group = group
        self.targets = [
            target for target in targets if not is_group_target_id(target.id)
        ]
        self.checkboxes: dict[str, QCheckBox] = {}
        self.setWindowTitle("Screen Group")
        self.setModal(True)
        self.resize(420, 420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        self.name_edit = QLineEdit(self)
        layout.addWidget(QLabel("Group Name"))
        layout.addWidget(self.name_edit)
        layout.addWidget(QLabel("Screens in this group"))
        holder = QWidget(self)
        holder_layout = QVBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        holder_layout.setSpacing(8)
        selected_ids = set(group.screen_ids if group is not None else [])
        for target in self.targets:
            checkbox = QCheckBox(target.label, holder)
            checkbox.setChecked(target.id in selected_ids)
            checkbox.setToolTip(target.detail or target.label)
            self.checkboxes[target.id] = checkbox
            holder_layout.addWidget(checkbox)
        holder_layout.addStretch(1)
        layout.addWidget(holder, 1)
        self.message_label = QLabel("", self)
        self.message_label.setObjectName("messageBanner")
        self.message_label.hide()
        layout.addWidget(self.message_label)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        if group is not None:
            self.name_edit.setText(group.name)

    def build_group(self) -> ScreenGroup:
        group = ScreenGroup(
            id=self.group.id if self.group is not None else f"group:{uuid.uuid4().hex}",
            name=self.name_edit.text().strip(),
            screen_ids=[
                screen_id
                for screen_id, checkbox in self.checkboxes.items()
                if checkbox.isChecked()
            ],
        )
        group.validate()
        return group

    def accept(self) -> None:  # type: ignore[override]
        try:
            group = self.build_group()
        except ValueError as error:
            self.message_label.setText(str(error))
            self.message_label.show()
            return
        if not group.screen_ids:
            self.message_label.setText("Choose at least one screen for the group.")
            self.message_label.show()
            return
        self.message_label.hide()
        super().accept()


class ScreenGroupManagerDialog(QDialog):
    def __init__(
        self,
        groups: list[ScreenGroup],
        targets: list[UnifiedScreenTarget],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        apply_dialog_theme(self)
        self._groups = [ScreenGroup.from_dict(group.to_dict()) for group in groups]
        self.targets = targets[:]
        self.setWindowTitle("Screen Groups")
        self.setModal(True)
        self.resize(640, 420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        title = QLabel("Screen Groups")
        title.setObjectName("sectionTitle")
        body = QLabel(
            "Use groups to target a set of screens from schedule slots without selecting each screen one by one."
        )
        body.setObjectName("sectionDescription")
        body.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(body)
        self.group_list = QListWidget(self)
        self.group_list.itemDoubleClicked.connect(
            lambda _item: self.edit_selected_group()
        )
        layout.addWidget(self.group_list, 1)
        buttons_row = QHBoxLayout()
        add_button = QPushButton("Add", self)
        add_button.clicked.connect(self.add_group)
        edit_button = QPushButton("Edit", self)
        edit_button.clicked.connect(self.edit_selected_group)
        delete_button = QPushButton("Delete", self)
        delete_button.setProperty("variant", "danger")
        delete_button.clicked.connect(self.delete_selected_group)
        buttons_row.addWidget(add_button)
        buttons_row.addWidget(edit_button)
        buttons_row.addWidget(delete_button)
        buttons_row.addStretch(1)
        layout.addLayout(buttons_row)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Save,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.refresh_list()

    def refresh_list(self) -> None:
        self.group_list.clear()
        for group in self._groups:
            item = QListWidgetItem(f"{group.name}\n{len(group.screen_ids)} screen(s)")
            item.setData(Qt.ItemDataRole.UserRole, group.id)
            item.setToolTip(", ".join(group.screen_ids))
            self.group_list.addItem(item)
        if self.group_list.count():
            self.group_list.setCurrentRow(0)

    def selected_index(self) -> int:
        return self.group_list.currentRow()

    def add_group(self) -> None:
        dialog = ScreenGroupEditorDialog(self.targets, None, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._groups.append(dialog.build_group())
        self.refresh_list()

    def edit_selected_group(self) -> None:
        index = self.selected_index()
        if index < 0 or index >= len(self._groups):
            return
        dialog = ScreenGroupEditorDialog(self.targets, self._groups[index], self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._groups[index] = dialog.build_group()
        self.refresh_list()
        self.group_list.setCurrentRow(index)

    def delete_selected_group(self) -> None:
        index = self.selected_index()
        if index < 0 or index >= len(self._groups):
            return
        del self._groups[index]
        self.refresh_list()

    def screen_groups(self) -> list[ScreenGroup]:
        return [ScreenGroup.from_dict(group.to_dict()) for group in self._groups]


class ManageScreensWorkspaceDialog(QDialog):
    def __init__(
        self,
        targets: list[UnifiedScreenTarget],
        selected_monitor_ids: list[str],
        enabled_screen_ids: list[str],
        screen_aliases: dict[str, str],
        merge_handler: Callable[[str, str], tuple[bool, str]] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        apply_dialog_theme(self)
        self.setWindowTitle("Manage Screens")
        self.setModal(True)
        self.resize(920, 560)
        self.all_targets = targets[:]
        self.targets: list[UnifiedScreenTarget] = []
        self.selected_monitor_ids = set(selected_monitor_ids)
        self.enabled_screen_ids = set(enabled_screen_ids)
        self.screen_aliases = dict(screen_aliases)
        self._active_row = -1
        self._updating_fields = False
        self.merge_handler = merge_handler

        apply_dialog_theme(
            self,
            """
            QLabel#manageScreensFieldLabel { color: #112c40; font-size: 13px; font-weight: 650; }
            QLabel#manageScreensDetail { color: rgba(23,48,67,0.90); font-size: 12px; }
            """,
        )

        layout = QVBoxLayout(self)
        header = QLabel("Configure local HDMI and LAN screens in one place")
        header.setObjectName("sectionDescription")
        layout.addWidget(header)

        body = QHBoxLayout()

        list_column = QVBoxLayout()
        search_row = QHBoxLayout()
        self.search_input = QLineEdit(self)
        self.search_input.setPlaceholderText("Search screens by name, ID, or details")
        self.filter_selector = QComboBox(self)
        self.filter_selector.addItem("All", "all")
        self.filter_selector.addItem("Local", "local")
        self.filter_selector.addItem("Remote", "remote")
        self.filter_selector.addItem("Online", "online")
        self.filter_selector.addItem("Offline", "offline")
        self.filter_selector.addItem("Enabled", "enabled")
        self.filter_selector.addItem("Default Group", "default")
        search_row.addWidget(self.search_input, 1)
        search_row.addWidget(self.filter_selector, 0)
        list_column.addLayout(search_row)

        bulk_row = QHBoxLayout()
        self.enable_visible_button = QPushButton("Enable Visible")
        self.disable_visible_button = QPushButton("Disable Visible")
        self.select_visible_button = QPushButton("Default Visible")
        self.unselect_visible_button = QPushButton("Clear Visible")
        bulk_row.addWidget(self.enable_visible_button)
        bulk_row.addWidget(self.disable_visible_button)
        bulk_row.addWidget(self.select_visible_button)
        bulk_row.addWidget(self.unselect_visible_button)
        list_column.addLayout(bulk_row)

        self.screen_list = QListWidget(self)
        self.screen_list.setMinimumWidth(360)
        self.screen_list.currentRowChanged.connect(self.on_screen_selected)
        list_column.addWidget(self.screen_list, 1)

        body.addLayout(list_column, 1)

        detail = QVBoxLayout()
        self.name_input = QLineEdit(self)
        self.enabled_checkbox = QCheckBox(
            "Enable this screen for scheduling (even if offline)", self
        )
        self.default_checkbox = QCheckBox("Include in default playback group", self)
        self.detail_label = QLabel("Select a screen")
        self.detail_label.setObjectName("manageScreensDetail")
        self.detail_label.setWordWrap(True)
        screen_name_label = QLabel("Screen Name")
        screen_name_label.setObjectName("manageScreensFieldLabel")
        detail.addWidget(screen_name_label)
        detail.addWidget(self.name_input)
        detail.addWidget(self.enabled_checkbox)
        detail.addWidget(self.default_checkbox)
        detail.addWidget(self.detail_label)

        merge_label = QLabel("Merge remembered duplicate LAN screens")
        merge_label.setObjectName("manageScreensFieldLabel")
        self.merge_source_selector = QComboBox(self)
        self.merge_target_selector = QComboBox(self)
        self.merge_apply_button = QPushButton("Merge into Target")
        self.merge_feedback_label = QLabel("")
        self.merge_feedback_label.setObjectName("manageScreensDetail")
        self.merge_feedback_label.setWordWrap(True)
        detail.addWidget(merge_label)
        detail.addWidget(self.merge_source_selector)
        detail.addWidget(self.merge_target_selector)
        detail.addWidget(self.merge_apply_button)
        detail.addWidget(self.merge_feedback_label)

        detail.addStretch(1)
        body.addLayout(detail, 1)
        layout.addLayout(body, 1)

        controls = QHBoxLayout()
        self.manage_groups_button = QPushButton("Manage Groups...")
        self.manage_configured_button = QPushButton("Configured Screens...")
        self.refresh_button = QPushButton("Refresh")
        self.merge_duplicates_button = QPushButton("Merge Duplicates...")
        self.purge_offline_button = QPushButton("Purge Offline")
        self.purge_all_button = QPushButton("Purge All")
        self.apply_button = QPushButton("Apply")
        self.apply_button.setProperty("variant", "primary")
        controls.addWidget(self.manage_groups_button)
        controls.addWidget(self.manage_configured_button)
        controls.addWidget(self.refresh_button)
        controls.addWidget(self.merge_duplicates_button)
        controls.addWidget(self.purge_offline_button)
        controls.addWidget(self.purge_all_button)
        controls.addStretch(1)
        controls.addWidget(self.apply_button)
        layout.addLayout(controls)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=self)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(
            self.accept
        )
        layout.addWidget(buttons)

        self.apply_button.clicked.connect(self.apply_current)
        self.name_input.editingFinished.connect(self.apply_current)
        self.enabled_checkbox.toggled.connect(lambda _checked: self.apply_current())
        self.default_checkbox.toggled.connect(lambda _checked: self.apply_current())
        self.search_input.textChanged.connect(lambda _value: self.populate_list())
        self.filter_selector.currentIndexChanged.connect(
            lambda _idx: self.populate_list()
        )
        self.enable_visible_button.clicked.connect(
            lambda *_args: self._bulk_set_visible(enabled=True)
        )
        self.disable_visible_button.clicked.connect(
            lambda *_args: self._bulk_set_visible(enabled=False)
        )
        self.select_visible_button.clicked.connect(
            lambda *_args: self._bulk_set_visible(default_selected=True)
        )
        self.unselect_visible_button.clicked.connect(
            lambda *_args: self._bulk_set_visible(default_selected=False)
        )
        self.merge_source_selector.currentIndexChanged.connect(
            lambda _idx: self._sync_merge_targets()
        )
        self.merge_apply_button.clicked.connect(self._apply_merge_from_workspace)
        self.populate_list()
        self._refresh_merge_controls()

    def replace_targets(self, targets: list[UnifiedScreenTarget]) -> None:
        self.apply_current()
        self.all_targets = targets[:]
        self.populate_list()
        self._refresh_merge_controls()

    def sync_state(
        self,
        *,
        selected_monitor_ids: list[str],
        enabled_screen_ids: list[str],
        screen_aliases: dict[str, str],
    ) -> None:
        self.selected_monitor_ids = set(selected_monitor_ids)
        self.enabled_screen_ids = set(enabled_screen_ids)
        self.screen_aliases = dict(screen_aliases)
        self.populate_list()
        self._refresh_merge_controls()

    def _filter_key(self) -> str:
        return str(self.filter_selector.currentData() or "all")

    def _target_matches_filters(
        self, target: UnifiedScreenTarget, query: str, key: str
    ) -> bool:
        if key == "local" and target.kind != "local":
            return False
        if key == "remote" and target.kind != "remote":
            return False
        if key == "online" and not target.online:
            return False
        if key == "offline" and target.online:
            return False
        if key == "enabled" and target.id not in self.enabled_screen_ids:
            return False
        if key == "default" and target.id not in self.selected_monitor_ids:
            return False
        if not query:
            return True
        haystack = " ".join(
            [
                target.id,
                target.label,
                self.screen_aliases.get(target.id, ""),
                target.detail,
                target.warning,
            ]
        ).lower()
        return query in haystack

    def populate_list(self) -> None:
        self._apply_row(self._active_row)
        selected_screen_id = ""
        current_row = self.screen_list.currentRow()
        if 0 <= current_row < len(self.targets):
            selected_screen_id = self.targets[current_row].id

        query = self.search_input.text().strip().lower()
        filter_key = self._filter_key()
        self.targets = [
            target
            for target in sorted(
                self.all_targets,
                key=lambda t: (0 if t.online else 1, t.kind, t.label.lower()),
            )
            if self._target_matches_filters(target, query, filter_key)
        ]

        self.screen_list.blockSignals(True)
        self.screen_list.clear()
        for target in self.targets:
            source_icon = "🖥️" if target.kind == "local" else "🌐"
            state = "online" if target.online else "offline"
            enabled = "enabled" if target.id in self.enabled_screen_ids else "disabled"
            default = "default" if target.id in self.selected_monitor_ids else ""
            suffix = ", ".join(part for part in [enabled, default] if part)
            item = QListWidgetItem(
                f"{source_icon} {target.label} ({state}{' • ' + suffix if suffix else ''})"
            )
            item.setData(Qt.ItemDataRole.UserRole, target.id)
            self.screen_list.addItem(item)
        self.screen_list.blockSignals(False)

        if self.screen_list.count() <= 0:
            self._active_row = -1
            self.detail_label.setText("No screens match the current filters.")
            return

        next_row = 0
        if selected_screen_id:
            for idx, target in enumerate(self.targets):
                if target.id == selected_screen_id:
                    next_row = idx
                    break
        self.screen_list.setCurrentRow(next_row)

    def on_screen_selected(self, row: int) -> None:
        self._apply_row(self._active_row)
        self._active_row = row
        if row < 0 or row >= len(self.targets):
            return
        target = self.targets[row]
        self._updating_fields = True
        try:
            self.name_input.setText(self.screen_aliases.get(target.id, target.label))
            self.enabled_checkbox.setChecked(target.id in self.enabled_screen_ids)
            self.default_checkbox.setChecked(target.id in self.selected_monitor_ids)
            self.detail_label.setText(
                f"{target.detail}\n\n{target.warning or ''}".strip()
            )
        finally:
            self._updating_fields = False

    def _apply_row(self, row: int) -> None:
        if self._updating_fields:
            return
        if row < 0 or row >= len(self.targets):
            return
        target = self.targets[row]
        name = self.name_input.text().strip()
        if name:
            self.screen_aliases[target.id] = name
        else:
            self.screen_aliases.pop(target.id, None)
        if self.enabled_checkbox.isChecked():
            self.enabled_screen_ids.add(target.id)
        else:
            self.enabled_screen_ids.discard(target.id)
        if self.default_checkbox.isChecked():
            self.selected_monitor_ids.add(target.id)
        else:
            self.selected_monitor_ids.discard(target.id)

    def _bulk_set_visible(
        self,
        *,
        enabled: bool | None = None,
        default_selected: bool | None = None,
    ) -> None:
        self.apply_current()
        for target in self.targets:
            if enabled is not None:
                if enabled:
                    self.enabled_screen_ids.add(target.id)
                else:
                    self.enabled_screen_ids.discard(target.id)
            if default_selected is not None:
                if default_selected:
                    self.selected_monitor_ids.add(target.id)
                else:
                    self.selected_monitor_ids.discard(target.id)
        self.populate_list()

    def _merge_candidate_ids(self) -> list[str]:
        return sorted(
            target.id
            for target in self.all_targets
            if target.kind == "remote" and is_remote_screen_id(target.id)
        )

    def _refresh_merge_controls(self) -> None:
        candidates = self._merge_candidate_ids()
        self.merge_source_selector.blockSignals(True)
        self.merge_source_selector.clear()
        for screen_id in candidates:
            label = self.screen_aliases.get(screen_id) or screen_id
            self.merge_source_selector.addItem(f"{label} ({screen_id})", screen_id)
        self.merge_source_selector.blockSignals(False)
        self._sync_merge_targets()

    def _sync_merge_targets(self) -> None:
        source_id = str(self.merge_source_selector.currentData() or "")
        candidates = [
            screen_id
            for screen_id in self._merge_candidate_ids()
            if screen_id != source_id
        ]
        self.merge_target_selector.clear()
        for screen_id in candidates:
            label = self.screen_aliases.get(screen_id) or screen_id
            self.merge_target_selector.addItem(f"{label} ({screen_id})", screen_id)
        enabled = bool(source_id and candidates and self.merge_handler is not None)
        self.merge_source_selector.setEnabled(bool(self._merge_candidate_ids()))
        self.merge_target_selector.setEnabled(enabled)
        self.merge_apply_button.setEnabled(enabled)
        if not enabled:
            if len(self._merge_candidate_ids()) < 2:
                self.merge_feedback_label.setText(
                    "Need at least two remembered remote screens to merge."
                )
            elif self.merge_handler is None:
                self.merge_feedback_label.setText("Merge action is unavailable.")

    def _apply_merge_from_workspace(self) -> None:
        source_id = str(self.merge_source_selector.currentData() or "").strip()
        target_id = str(self.merge_target_selector.currentData() or "").strip()
        if not source_id or not target_id or source_id == target_id:
            self.merge_feedback_label.setText(
                "Choose distinct source and target screens."
            )
            return
        if self.merge_handler is None:
            self.merge_feedback_label.setText("Merge action is unavailable.")
            return
        merged, message = self.merge_handler(source_id, target_id)
        self.merge_feedback_label.setText(message)
        if not merged:
            return
        self.selected_monitor_ids.discard(source_id)
        self.enabled_screen_ids.discard(source_id)
        self.screen_aliases.pop(source_id, None)
        self.all_targets = [
            target for target in self.all_targets if target.id != source_id
        ]
        self.populate_list()
        self._refresh_merge_controls()

    def apply_current(self) -> None:
        self._apply_row(self.screen_list.currentRow())
        self._refresh_merge_controls()

    def accept(self) -> None:  # type: ignore[override]
        self.apply_current()
        super().accept()


def markdown_sections(markdown_text: str) -> list[tuple[str, str]]:
    lines = markdown_text.splitlines()
    sections: list[tuple[str, list[str]]] = []
    current_title = "Overview"
    current_lines: list[str] = []
    for line in lines:
        if line.startswith("## "):
            if current_lines:
                sections.append((current_title, current_lines[:]))
            current_title = line[3:].strip()
            current_lines = []
            continue
        if line.startswith("# "):
            continue
        current_lines.append(line)
    if current_lines:
        sections.append((current_title, current_lines))
    return [
        (title, "\n".join(content).strip())
        for title, content in sections
        if title.strip()
    ]


def markdown_to_help_html(markdown_text: str) -> str:
    def esc(value: str) -> str:
        return escape(value)

    html: list[str] = [
        "<div style='font-family:Segoe UI,Arial,sans-serif; font-size:14px; line-height:1.5;'>"
    ]
    in_ul = False
    in_ol = False
    in_code = False
    for raw in markdown_text.splitlines():
        line = raw.rstrip("\n")
        stripped = line.strip()

        if stripped.startswith("```"):
            if in_code:
                html.append("</pre>")
                in_code = False
            else:
                html.append(
                    "<pre style='background:#0f1720;color:#e6f0ff;padding:10px;border-radius:8px;overflow:auto;'>"
                )
                in_code = True
            continue

        if in_code:
            html.append(esc(line))
            continue

        if stripped.startswith("### "):
            if in_ul:
                html.append("</ul>")
                in_ul = False
            if in_ol:
                html.append("</ol>")
                in_ol = False
            html.append(
                f"<h3 style='margin:14px 0 6px 0;'>{esc(stripped[4:].strip())}</h3>"
            )
            continue

        if stripped.startswith("- "):
            if in_ol:
                html.append("</ol>")
                in_ol = False
            if not in_ul:
                html.append("<ul style='margin:6px 0 10px 18px;'>")
                in_ul = True
            html.append(f"<li>{esc(stripped[2:].strip())}</li>")
            continue

        first_dot = stripped.find(". ")
        if first_dot > 0 and stripped[:first_dot].isdigit():
            if in_ul:
                html.append("</ul>")
                in_ul = False
            if not in_ol:
                html.append("<ol style='margin:6px 0 10px 18px;'>")
                in_ol = True
            html.append(f"<li>{esc(stripped[first_dot + 2 :].strip())}</li>")
            continue

        if in_ul:
            html.append("</ul>")
            in_ul = False
        if in_ol:
            html.append("</ol>")
            in_ol = False

        if not stripped:
            html.append("<div style='height:6px;'></div>")
        else:
            html.append(f"<p style='margin:4px 0;'>{esc(stripped)}</p>")

    if in_ul:
        html.append("</ul>")
    if in_ol:
        html.append("</ol>")
    if in_code:
        html.append("</pre>")
    html.append("</div>")
    return "\n".join(html)


class HelpCenterDialog(QDialog):
    def __init__(self, markdown_text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        apply_dialog_theme(self)
        self.setWindowTitle("Help Center")
        self.setModal(True)
        self.resize(980, 700)

        self.sections = markdown_sections(markdown_text)
        self.filtered_indices = list(range(len(self.sections)))

        layout = QVBoxLayout(self)
        top = QLabel(
            "Open a topic and follow the steps from start to finish. Use search when you need one specific workflow."
        )
        top.setObjectName("sectionDescription")
        layout.addWidget(top)

        split = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(split, 1)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search help topics...")
        self.search_input.textChanged.connect(self.filter_sections)
        self.section_list = QListWidget()
        self.section_list.currentRowChanged.connect(self.on_section_selected)
        left_layout.addWidget(self.search_input)
        left_layout.addWidget(self.section_list, 1)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        self.section_title = QLabel("Choose a guide")
        self.section_title.setObjectName("sectionTitle")
        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(True)
        right_layout.addWidget(self.section_title)
        right_layout.addWidget(self.browser, 1)

        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([280, 680])

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=self)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(
            self.accept
        )
        layout.addWidget(buttons)

        self.refresh_list()

    def refresh_list(self) -> None:
        self.section_list.blockSignals(True)
        self.section_list.clear()
        for index in self.filtered_indices:
            title, _content = self.sections[index]
            self.section_list.addItem(title)
        self.section_list.blockSignals(False)
        if self.section_list.count() > 0:
            self.section_list.setCurrentRow(0)

    def filter_sections(self, value: str) -> None:
        query = value.strip().lower()
        if not query:
            self.filtered_indices = list(range(len(self.sections)))
        else:
            self.filtered_indices = [
                idx
                for idx, (title, content) in enumerate(self.sections)
                if query in title.lower() or query in content.lower()
            ]
        self.refresh_list()

    def on_section_selected(self, row: int) -> None:
        if row < 0 or row >= len(self.filtered_indices):
            self.section_title.setText("Choose a guide")
            self.browser.setHtml("")
            return
        section_index = self.filtered_indices[row]
        title, content = self.sections[section_index]
        self.section_title.setText(title)
        self.browser.setHtml(markdown_to_help_html(content))


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
        body = QLabel(
            "Drag and drop a video or picture here, then choose the screen or screens in the popup."
        )
        body.setObjectName("sectionDescription")
        body.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(body)

    def dragEnterEvent(self, event) -> None:  # type: ignore[override]
        urls = event.mimeData().urls()
        if urls and any(
            Path(url.toLocalFile()).suffix.lower() in SUPPORTED_MEDIA_EXTENSIONS
            for url in urls
            if url.isLocalFile()
        ):
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
        icon_row.addWidget(
            screen_label, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )

        if warning_icon is not None:
            warning_label = QLabel()
            warning_label.setPixmap(warning_icon)
            warning_label.setToolTip(warning_tooltip)
            icon_row.addWidget(
                warning_label,
                0,
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            )

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
        self.configured_screens = [
            ConfiguredScreen.from_dict(item.to_dict())
            for item in config["configured_screens"]
        ]
        try:
            validate_unique_browser_bindings(self.configured_screens)
        except ValueError:
            self.configured_screens = []
        self.screen_groups = [
            ScreenGroup.from_dict(item.to_dict()) for item in config["screen_groups"]
        ]
        self.schedules = [ScheduleEntry.from_dict(item) for item in config["schedules"]]
        self.selected_monitor_ids = [
            str(item) for item in config["selected_monitor_ids"]
        ]
        self.enabled_screen_ids = [
            str(item) for item in config.get("enabled_screen_ids") or []
        ]
        self.video_directory: Path | None = (
            Path(config["video_directory"]).expanduser()
            if config["video_directory"]
            else None
        )
        if self.video_directory is not None and not self.video_directory.exists():
            self.video_directory = None
        self.screen_aliases: dict[str, str] = {
            str(key): str(value) for key, value in config["screen_aliases"].items()
        }
        self.screen_registry = ScreenRegistry(self)
        self.screen_aliases.update(self.screen_registry.configured_screen_name_map())
        self.transition_method = str(config["transition_method"])
        self.run_at_startup = bool(config["run_at_startup"])
        self.lan_pairing_required = bool(config.get("lan_pairing_required"))
        self.lan_allow_unpaired_clients = bool(config.get("lan_allow_unpaired_clients"))
        self.lan_paired_client_ids = [
            str(item).strip()
            for item in (config.get("lan_paired_client_ids") or [])
            if str(item).strip()
        ]
        self.available_videos: list[Path] = []
        self.current_selected_video_file = (
            self.schedules[0].video_file if self.schedules else ""
        )
        self.current_media_assignments: dict[str, ScheduleMediaAssignment] = {}
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
        self.backend_channel_versions: dict[str, int] = {
            name: 0 for name in STATE_CHANNELS
        }
        self.backend_listener = BackendStateListener(self.backend_client, self)
        self.backend_paused = False
        self.backend_playback_enabled = False
        self.backend_window_count = 0
        self.backend_library_scanning = False
        self.backend_library_error = ""
        self.backend_performance_metrics: dict[str, Any] = {}
        self._pending_transition_method: str | None = None

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
        self._low_value_timers_paused = False

        self.setWindowTitle("Background Screen Controller")
        self.resize(1180, 760)
        self.apply_styles()
        self.build_ui()
        self.setup_tray()
        self.backend_listener.snapshot_received.connect(self.apply_backend_snapshot)
        self.backend_listener.connection_changed.connect(
            self.on_backend_connection_changed
        )
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
                color: #112c40;
            }
            QMenu::item:disabled {
                color: rgba(23,48,67,0.48);
            }
            QMenu::separator {
                height: 1px;
                background: rgba(23,48,67,0.12);
                margin: 6px 10px;
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
            }
            QCheckBox::indicator {
                width: 17px;
                height: 17px;
                border-radius: 5px;
                border: 1px solid rgba(23,48,67,0.45);
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
        self.open_windows_display_settings_action = QAction(
            "Open Windows Display Settings", self
        )
        self.open_windows_display_settings_action.triggered.connect(
            self.open_windows_display_settings
        )
        engine_menu.addAction(self.open_windows_display_settings_action)
        self.refresh_engine_action = QAction("Refresh Engine Status", self)
        self.refresh_engine_action.triggered.connect(
            lambda: self.pull_backend_state(initial=False)
        )
        engine_menu.addAction(self.refresh_engine_action)
        self.export_diagnostics_action = QAction("Export Diagnostics...", self)
        self.export_diagnostics_action.triggered.connect(self.export_engine_diagnostics)
        engine_menu.addAction(self.export_diagnostics_action)
        screen_menu = self.menuBar().addMenu("Screen")
        self.screen_target_menu = QMenu("Default Group Screens", self)
        self.rename_screens_menu = QMenu("Name Screens", self)
        self.remote_screens_menu = QMenu("Remote LAN Screens", self)
        self.manage_groups_action = QAction("Manage Screen Groups...", self)
        self.manage_groups_action.triggered.connect(self.manage_screen_groups)
        self.purge_offline_screens_action = QAction(
            "Purge Offline Remembered Screens", self
        )
        self.purge_offline_screens_action.triggered.connect(self.purge_offline_screens)
        self.purge_all_screens_action = QAction("Purge All Remembered Screens", self)
        self.purge_all_screens_action.triggered.connect(self.purge_all_screens)

        self.open_manage_screens_action = QAction("Manage Screens", self)
        self.open_manage_screens_action.triggered.connect(
            self.open_manage_screens_popup
        )
        screen_menu.addAction(self.open_manage_screens_action)
        self.merge_duplicate_screens_action = QAction(
            "Merge Duplicate Remembered Screens...", self
        )
        self.merge_duplicate_screens_action.triggered.connect(
            self.merge_duplicate_screens
        )
        screen_menu.addAction(self.merge_duplicate_screens_action)
        self.manage_screens_action = QAction("Configured Screens...", self)
        self.manage_screens_action.triggered.connect(self.manage_configured_screens)
        screen_menu.addAction(self.manage_screens_action)
        refresh_screens_action = QAction("Refresh Screens", self)
        refresh_screens_action.triggered.connect(self.refresh_monitors)
        screen_menu.addAction(refresh_screens_action)

        help_menu = self.menuBar().addMenu("Help")
        self.open_help_docs_action = QAction("Setup & User Guide", self)
        self.open_help_docs_action.triggered.connect(self.open_help_documentation)
        help_menu.addAction(self.open_help_docs_action)
        self.open_release_checklist_action = QAction("Release Checklist", self)
        self.open_release_checklist_action.triggered.connect(
            self.open_release_checklist
        )
        help_menu.addAction(self.open_release_checklist_action)
        self.open_accessibility_review_action = QAction(
            "Accessibility & Contrast Review", self
        )
        self.open_accessibility_review_action.triggered.connect(
            self.open_accessibility_review
        )
        help_menu.addAction(self.open_accessibility_review_action)
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
        self.hero_subtitle_label = ElidedLabel(
            "Waiting for a matching UK-time schedule."
        )
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
        self.remote_screen_detail_label = ElidedLabel(
            "Open the TV URL on Whale OS screens to register them."
        )
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
        self.new_schedule_button.clicked.connect(
            lambda: self.load_schedule_into_form(None)
        )
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
        editor_copy = QLabel(
            "Schedules now point to video or image files inside the chosen library folder."
        )
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
        self.current_form_screen_ids: list[str] = [DEFAULT_GROUP_ID]
        self.schedule_screen_button = QToolButton()
        self.schedule_screen_button.setText("Target Screens")
        self.schedule_screen_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self.schedule_screen_menu = QMenu(self)
        self.schedule_screen_button.setMenu(self.schedule_screen_menu)
        self.schedule_screen_summary_label = ElidedLabel(DEFAULT_GROUP_NAME)
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
        self.per_screen_media_button = QPushButton("Per-Screen Media")
        self.per_screen_media_button.setProperty("variant", "ghost")
        self.per_screen_media_button.clicked.connect(self.edit_per_screen_media)
        self.transition_selector = QComboBox()
        for key, label in TRANSITION_METHODS.items():
            self.transition_selector.addItem(label, key)
        transition_caption = QLabel("Transition")
        transition_caption.setObjectName("mutedText")
        transition_index = self.transition_selector.findData(self.transition_method)
        self.transition_selector.setCurrentIndex(
            transition_index if transition_index >= 0 else 0
        )
        self.transition_selector.currentIndexChanged.connect(self.on_transition_changed)
        self.transition_selector.setMaximumWidth(190)
        action_layout.addWidget(self.schedule_screen_button)
        action_layout.addWidget(self.choose_media_button)
        action_layout.addWidget(self.per_screen_media_button)
        action_layout.addWidget(transition_caption)
        action_layout.addWidget(self.transition_selector)
        action_layout.addStretch(1)

        action_field_with_hint = QWidget()
        action_field_with_hint_layout = QVBoxLayout(action_field_with_hint)
        action_field_with_hint_layout.setContentsMargins(0, 0, 0, 0)
        action_field_with_hint_layout.setSpacing(4)
        action_field_with_hint_layout.addWidget(action_field)
        self.transition_scope_hint = QLabel(
            "Transition applies globally to playback outputs (not per-schedule slot)."
        )
        self.transition_scope_hint.setObjectName("mutedText")
        self.transition_scope_hint.setWordWrap(True)
        action_field_with_hint_layout.addWidget(self.transition_scope_hint)

        form_grid.addRow("Title", self.title_input)
        form_grid.addRow("Time", time_field)
        form_grid.addRow("Days", days_field)
        form_grid.addRow("Actions", action_field_with_hint)
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
        footer_total_icon = (
            self.style()
            .standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
            .pixmap(16, 16)
        )
        footer_global_icon = (
            self.style()
            .standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton)
            .pixmap(16, 16)
        )
        footer_active_icon = (
            self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay).pixmap(16, 16)
        )
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

        footer_diagnostics_icon = (
            self.style()
            .standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView)
            .pixmap(16, 16)
        )
        self.footer_diagnostics_icon = QLabel()
        self.footer_diagnostics_icon.setPixmap(footer_diagnostics_icon)
        self.footer_diagnostics_primary = QLabel("0 workers")
        self.footer_diagnostics_primary.setObjectName("footerPrimary")
        self.footer_diagnostics_secondary = QLabel("Engine diagnostics unavailable")
        self.footer_diagnostics_secondary.setObjectName("footerSecondary")
        diagnostics_text = QVBoxLayout()
        diagnostics_text.setSpacing(1)
        diagnostics_text.addWidget(self.footer_diagnostics_primary)
        diagnostics_text.addWidget(self.footer_diagnostics_secondary)
        diagnostics_group = QHBoxLayout()
        diagnostics_group.setSpacing(8)
        diagnostics_group.addWidget(
            self.footer_diagnostics_icon, 0, Qt.AlignmentFlag.AlignTop
        )
        diagnostics_group.addLayout(diagnostics_text)
        footer_layout.addLayout(time_group)
        footer_layout.addStretch(1)
        footer_layout.addLayout(total_group)
        footer_layout.addSpacing(18)
        footer_layout.addLayout(global_group)
        footer_layout.addSpacing(18)
        footer_layout.addLayout(active_group)
        footer_layout.addSpacing(18)
        footer_layout.addLayout(diagnostics_group)
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
        # Controller tray icon intentionally disabled. Engine tray icon is the single source.
        self.tray_icon = None

    def _set_low_value_timers_paused(self, paused: bool) -> None:
        desired = bool(paused)
        if bool(getattr(self, "_low_value_timers_paused", False)) == desired:
            return
        self._low_value_timers_paused = desired
        if desired:
            if hasattr(self, "ui_status_timer"):
                self.ui_status_timer.stop()
            if hasattr(self, "screen_refresh_timer"):
                self.screen_refresh_timer.stop()
            if hasattr(self, "folder_refresh_timer"):
                self.folder_refresh_timer.stop()
            return
        if hasattr(self, "ui_status_timer") and not self._shutdown_in_progress:
            self.ui_status_timer.start()

    def send_to_background(self) -> None:
        if self._allow_real_quit:
            return
        self._set_low_value_timers_paused(True)
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
        self._set_low_value_timers_paused(False)
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

    def call_backend_action(
        self,
        action: str,
        payload: dict[str, Any] | None = None,
        *,
        show_errors: bool = True,
    ) -> dict[str, Any] | None:
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
            self.set_form_message(
                str(response.get("error") or "Action failed."), "error"
            )
            return None
        return response

    def update_engine_menu_state(self) -> None:
        if not hasattr(self, "start_engine_action"):
            return
        self.start_engine_action.setEnabled(not self.backend_online)
        self.stop_engine_action.setEnabled(self.backend_online)
        self.manage_screens_action.setEnabled(True)
        if hasattr(self, "retry_firewall_action"):
            self.retry_firewall_action.setEnabled(self.backend_online)
        self.refresh_engine_action.setEnabled(True)
        if hasattr(self, "export_diagnostics_action"):
            self.export_diagnostics_action.setEnabled(self.backend_online)

    def set_backend_offline_ui(self, detail: str = "") -> None:
        self.backend_online = False
        self.backend_playback_enabled = False
        self.backend_window_count = 0
        self.backend_paused = False
        self.update_playback_state_label(False, 0)
        self.refresh_monitors()
        self.network_status_primary.setText("Background engine offline")
        self.network_status_secondary.setText(
            detail
            or "Local screens can still be detected, but playback control is unavailable until the engine responds."
        )
        self.network_url_label.setText(
            "Use Engine > Start Engine to bring the background engine online."
        )
        self.network_url_label.setToolTip(self.network_url_label.text())
        self.remote_screen_summary_label.setText("Engine offline")
        self.remote_screen_detail_label.setText(
            "Connected-device details appear here while the engine is running."
        )
        self.remote_screen_detail_label.setToolTip(
            self.remote_screen_detail_label.text()
        )
        self.backend_performance_metrics = {}
        self.update_engine_diagnostics_summary()
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

    def _flush_pending_transition_method(self) -> None:
        pending = str(self._pending_transition_method or "").strip()
        if not pending:
            return
        if pending not in TRANSITION_METHODS:
            self._pending_transition_method = None
            return
        if not self.backend_online:
            return
        response = self.call_backend_action(
            "set_transition_method",
            {"transition_method": pending},
            show_errors=False,
        )
        if response is not None and response.get("ok", True):
            self._pending_transition_method = None

    def on_backend_connection_changed(self, online: bool, detail: str = "") -> None:
        if online:
            if not self.backend_online:
                self.pull_backend_state(initial=False)
            self._flush_pending_transition_method()
            return
        if self.backend_online:
            self.set_backend_offline_ui(
                detail
                or "Local screens can still be detected, but playback control is unavailable until the engine responds."
            )

    def refresh_live_ui_status(self) -> None:
        if not self.backend_online:
            return
        clock_label, minute_of_day, weekday_index = current_uk_time()
        if not self.backend_playback_enabled:
            active_label = "Playback stopped"
        else:
            active_entry = active_schedule_for_minute(
                self.schedules, weekday_index, minute_of_day
            )
            active_label = (
                "No active schedule"
                if active_entry is None
                else f"{active_entry.title} • {active_entry.range_label} • {active_entry.video_label or active_entry.video_file}"
            )
        self.update_status_labels(clock_label, active_label)

    def start_backend_engine(self, checked: bool = False) -> None:
        if self.backend_online and self.backend_client.ping():
            self.set_form_message(
                "The background engine is already running.", "success"
            )
            self.update_engine_menu_state()
            return
        try:
            ensure_backend_running(self.backend_client)
        except Exception as error:  # noqa: BLE001
            message = (
                str(error) or "The controller could not start the background engine."
            )
            self.set_backend_offline_ui(message)
            self.set_form_message(message, "error")
            return
        self.pull_backend_state(initial=True)
        self.backend_listener.start()
        self._flush_pending_transition_method()
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
            self.set_form_message(
                "Background engine was not running or could not be stopped.", "error"
            )

    def manage_configured_screens(self, checked: bool = False) -> None:
        dialog = ConfiguredScreenManagerDialog(
            self.configured_screens, self.remote_screens, self
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        updated_screens = dialog.configured_screens()
        response = self.call_backend_action(
            "set_configured_screens",
            {"configured_screens": [screen.to_dict() for screen in updated_screens]},
        )
        if response is not None:
            self.set_form_message(
                f"Configured screens saved ({len(self.configured_screens)}).", "success"
            )

    def all_screen_groups(self) -> list[ScreenGroup]:
        registry = getattr(self, "screen_registry", None)
        if registry is not None:
            return registry.all_screen_groups()
        return combined_screen_groups(self.selected_monitor_ids, self.screen_groups)

    def manage_screen_groups(self, checked: bool = False) -> None:
        targets = [
            target
            for target in self.unified_screen_targets(include_offline=True)
            if not is_group_target_id(target.id)
        ]
        dialog = ScreenGroupManagerDialog(self.screen_groups, targets, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        updated_groups = dialog.screen_groups()
        response = self.call_backend_action(
            "set_screen_groups",
            {"screen_groups": [group.to_dict() for group in updated_groups]},
        )
        if response is not None:
            self.set_form_message(
                f"Screen groups saved ({len(self.screen_groups)}).", "success"
            )

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
            self.set_form_message(
                "LAN firewall check completed. Review the LAN panel warnings if screens still cannot connect.",
                "success",
            )

    def export_engine_diagnostics(self, checked: bool = False) -> None:
        default_name = f"background_screen_diagnostics_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        suggested_path = str((Path.home() / "Downloads" / default_name).expanduser())
        target_path, _filter = QFileDialog.getSaveFileName(
            self,
            "Export Engine Diagnostics",
            suggested_path,
            "Zip Archives (*.zip)",
        )
        if not target_path:
            return
        response = self.call_backend_action(
            "export_diagnostics",
            {"path": str(target_path)},
        )
        if response is None:
            return
        export_path = str(response.get("exportPath") or target_path)
        self.set_form_message(f"Diagnostics exported to {export_path}", "success")

    def open_windows_display_settings(self, checked: bool = False) -> None:
        try:
            if sys.platform == "win32":
                os.startfile("ms-settings:display")
                self.set_form_message("Opened Windows display settings.", "success")
        except OSError as error:
            self.set_form_message(
                f"Could not open Windows display settings: {error}", "error"
            )

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
        response = self.call_backend_action(
            "set_run_at_startup", {"value": self.run_at_startup_action.isChecked()}
        )
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
        if self._low_value_timers_paused:
            return
        self.screen_refresh_timer.start()

    def browser_configured_screen_ids(self) -> set[str]:
        registry = getattr(self, "screen_registry", None)
        if registry is not None:
            return registry.browser_configured_screen_ids()
        return configured_browser_screen_ids(self.configured_screens)

    def static_media_configured_screen_ids(self) -> set[str]:
        registry = getattr(self, "screen_registry", None)
        if registry is not None:
            return registry.static_media_configured_screen_ids()
        return {
            screen.id
            for screen in self.configured_screens
            if "static_media" in screen.capabilities
        }

    def unified_screen_targets(
        self, include_offline: bool = True
    ) -> list[UnifiedScreenTarget]:
        registry = getattr(self, "screen_registry", None)
        if registry is not None:
            return registry.unified_screen_targets(
                local_screens=available_screens(),
                screen_identifier=screen_identifier,
                screen_display_name=screen_display_name,
                include_offline=include_offline,
            )
        targets: list[UnifiedScreenTarget] = []
        static_media_ids = self.static_media_configured_screen_ids()
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
        for screen in sorted(
            (item for item in self.configured_screens if item.transport == "browser"),
            key=lambda item: item.name.lower(),
        ):
            if screen.id not in static_media_ids:
                continue
            online, detail, warning = browser_target_status_detail(
                screen, self.remote_screens
            )
            if not include_offline and not online:
                continue
            targets.append(
                UnifiedScreenTarget(
                    id=screen.id,
                    label=screen.name,
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
        pairing_required = bool(snapshot.get("pairingRequired"))
        pending_clients = snapshot.get("pendingClients") or []
        pending_count = int(
            snapshot.get("pendingClientCount") or len(pending_clients) or 0
        )
        preferred_interface = snapshot.get("preferred_interface") or ""
        interface_lines = [
            f"{item.get('label') or 'Network Interface'}: {item.get('ip') or ''}"
            for item in snapshot.get("interfaces") or []
            if item.get("ip")
        ]
        connected = sum(
            1 for item in self.remote_screens.values() if item.get("online")
        )
        total = len(self.remote_screens)
        if self.backend_online and preferred:
            self.network_status_primary.setText("Background engine online")
            self.network_status_secondary.setText(
                f"TV URL ready on port {snapshot.get('port')} • IP: {snapshot.get('preferred_ip') or 'Unavailable'}"
                + (
                    f" • Interface: {preferred_interface}"
                    if preferred_interface
                    else ""
                )
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
                self.network_url_label.setText(
                    "Connect this PC to the same LAN as the TVs and allow local firewall access."
                )
        else:
            self.network_status_primary.setText("Background engine offline")
            self.network_status_secondary.setText(
                "Start the engine to enable LAN screen connections."
            )
            self.network_url_label.setText(
                "Use Engine > Start Engine to bring the background engine online."
            )
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
            summary = f"{connected} connected • {total} known"
            if pairing_required:
                summary += f" • {pending_count} pending"
            self.remote_screen_summary_label.setText(summary)
        else:
            self.remote_screen_summary_label.setText("Engine offline")
        if self.backend_online and total:
            names = [
                f"{friendly_remote_name(screen_id, self.screen_aliases, state)} ({'online' if state.get('online') else 'offline'})"
                for screen_id, state in self.remote_screens.items()
            ]
            pending_lines = [
                f"{str(item.get('name') or item.get('client_id') or 'Unknown')} ({str(item.get('ip') or 'unknown ip')})"
                for item in pending_clients
                if isinstance(item, dict)
            ]
            if pairing_required and pending_count > 0:
                self.remote_screen_detail_label.setText(
                    "Registered LAN TVs are available. Pairing is enabled; pending clients need approval."
                )
            elif pairing_required:
                self.remote_screen_detail_label.setText(
                    "Registered LAN TVs are available. Pairing is enabled."
                )
            else:
                self.remote_screen_detail_label.setText(
                    "Registered LAN TVs are available alongside local HDMI screens."
                )
            tooltip_lines = names[:]
            if pending_lines:
                tooltip_lines.extend(["", "Pending unpaired clients:"])
                tooltip_lines.extend(pending_lines)
            self.remote_screen_detail_label.setToolTip("\n".join(tooltip_lines))
        elif self.backend_online:
            if pairing_required and pending_count > 0:
                self.remote_screen_detail_label.setText(
                    "Pairing is enabled. Pending LAN clients are waiting for approval."
                )
                pending_lines = [
                    f"{str(item.get('name') or item.get('client_id') or 'Unknown')} ({str(item.get('ip') or 'unknown ip')})"
                    for item in pending_clients
                    if isinstance(item, dict)
                ]
                self.remote_screen_detail_label.setToolTip("\n".join(pending_lines))
            else:
                self.remote_screen_detail_label.setText(
                    "Open the TV URL on Whale OS screens to register them."
                )
                self.remote_screen_detail_label.setToolTip(
                    self.network_url_label.text()
                )

    def refresh_monitors(self, *_args) -> None:
        self.screen_target_menu.clear()
        self.rename_screens_menu.clear()
        self.remote_screens_menu.clear()
        self.schedule_screen_menu.clear()
        configured_transport_by_id = {
            screen.id: screen.transport for screen in self.configured_screens
        }
        in_use_target_ids = (
            set(self.selected_monitor_ids)
            | set(self.current_form_screen_ids)
            | set(self.enabled_screen_ids)
        )
        for entry in self.schedules:
            in_use_target_ids.update(entry.screen_ids)

        raw_targets = self.unified_screen_targets(include_offline=True)
        visible_targets = [
            target
            for target in raw_targets
            if target.online or target.id in in_use_target_ids
        ]
        visible_targets.sort(
            key=lambda item: (0 if item.online else 1, item.label.lower())
        )

        for group in self.all_screen_groups():
            group_action = self.schedule_screen_menu.addAction(
                f"{group.name} ({len(group.screen_ids)})"
            )
            group_action.setCheckable(True)
            group_action.setChecked(group.id in self.current_form_screen_ids)
            group_action.setToolTip(
                ", ".join(
                    screen_label_from_id(
                        screen_id, self.screen_aliases, self.all_screen_groups()
                    )
                    for screen_id in group.screen_ids
                )
                or group.name
            )
            group_action.triggered.connect(
                lambda checked=False, group_id=group.id: (
                    self.toggle_schedule_screen_selection(group_id)
                )
            )
        self.schedule_screen_menu.addSeparator()
        offline_section_added = False
        for index, target in enumerate(visible_targets, start=1):
            label = target.label
            if target.kind == "local":
                local_screen = find_screen_by_id(target.id)
                if local_screen is not None:
                    geometry = local_screen.geometry()
                    label = f"{target.label} ({geometry.width()}x{geometry.height()})"
            else:
                transport_label = TRANSPORT_LABELS.get(
                    configured_transport_by_id.get(target.id, "browser"), "Remote"
                )
                label = f"{target.label} ({transport_label}{' • offline' if not target.online else ''})"
            if not target.online and not offline_section_added:
                self.screen_target_menu.addSeparator()
                self.screen_target_menu.addAction(
                    "Offline / remembered screens"
                ).setEnabled(False)
                self.schedule_screen_menu.addSeparator()
                self.schedule_screen_menu.addAction(
                    "Offline / remembered screens"
                ).setEnabled(False)
                offline_section_added = True

            action = self.screen_target_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(target.id in self.selected_monitor_ids)
            action.setToolTip(target.warning or target.detail)
            action.triggered.connect(
                lambda checked=False, screen_id=target.id: self.toggle_screen_selection(
                    screen_id
                )
            )
            rename_action = self.rename_screens_menu.addAction(
                f"Rename Screen {index} - {target.label}"
            )
            rename_action.triggered.connect(
                lambda checked=False, screen_id=target.id: self.rename_screen_alias(
                    screen_id
                )
            )
            schedule_action = self.schedule_screen_menu.addAction(label)
            schedule_action.setCheckable(True)
            schedule_action.setChecked(target.id in self.current_form_screen_ids)
            schedule_action.setToolTip(target.warning or target.detail)
            schedule_action.triggered.connect(
                lambda checked=False, screen_id=target.id: (
                    self.toggle_schedule_screen_selection(screen_id)
                )
            )
        for screen_id, state in sorted(
            self.remote_screens.items(),
            key=lambda item: friendly_remote_name(
                item[0], self.screen_aliases, item[1]
            ).lower(),
        ):
            remote_label = friendly_remote_name(screen_id, self.screen_aliases, state)
            suffix = "online" if state.get("online") else "offline"
            remote_action = self.remote_screens_menu.addAction(
                f"{remote_label} ({suffix})"
            )
            remote_action.setEnabled(False)
            detail = str(state.get("ip") or "")
            if int(state.get("width") or 0) and int(state.get("height") or 0):
                detail = f"{detail} • {int(state.get('width') or 0)}x{int(state.get('height') or 0)}".strip(
                    " •"
                )
            remote_action.setToolTip(detail or screen_id)
        self.update_monitor_summary()
        self.update_schedule_screen_summary()
        self.refresh_schedule_list()

    def toggle_screen_selection(self, screen_id: str) -> None:
        self.call_backend_action("toggle_screen_selection", {"screen_id": screen_id})

    def set_schedule_screen_targets(self, screen_ids: list[str]) -> None:
        self.current_form_screen_ids = normalize_schedule_target_ids(screen_ids)
        self.refresh_monitors()

    def toggle_schedule_screen_selection(self, screen_id: str) -> None:
        if screen_id in self.current_form_screen_ids:
            self.current_form_screen_ids.remove(screen_id)
        else:
            self.current_form_screen_ids.append(screen_id)
        self.current_form_screen_ids = normalize_schedule_target_ids(
            self.current_form_screen_ids
        )
        self.refresh_monitors()

    def rename_screen_alias(self, screen_id: str) -> None:
        screen = find_screen_by_id(screen_id)
        default_name = (
            screen.name()
            if screen is not None
            else friendly_remote_name(
                screen_id, self.screen_aliases, self.remote_screens.get(screen_id)
            )
        )
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
        self.call_backend_action(
            "rename_screen_alias", {"screen_id": screen_id, "alias": cleaned}
        )

    def purge_offline_screens(self, checked: bool = False) -> None:
        response = self.call_backend_action(
            "purge_remembered_screens", {"include_online": False}
        )
        if response is not None:
            removed = int(response.get("removed") or 0)
            removed_ids = {
                screen_id
                for screen_id in list(self.current_form_screen_ids)
                if is_remote_screen_id(screen_id)
                and screen_id not in self.remote_screens
            }
            if removed_ids:
                self.current_form_screen_ids = [
                    screen_id
                    for screen_id in self.current_form_screen_ids
                    if screen_id not in removed_ids
                ]
            if removed:
                self.set_form_message(
                    f"Purged {removed} offline remembered screen(s).", "success"
                )
            else:
                self.set_form_message(
                    "No offline remembered screens to purge.", "success"
                )

    def purge_all_screens(self, checked: bool = False) -> None:
        confirm = QMessageBox.question(
            self,
            "Purge all remembered screens",
            "This will remove all remembered remote LAN screens (including currently online ones) from saved selections, schedules, and aliases. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        response = self.call_backend_action(
            "purge_remembered_screens", {"include_online": True}
        )
        if response is not None:
            removed = int(response.get("removed") or 0)
            self.current_form_screen_ids = [
                screen_id
                for screen_id in self.current_form_screen_ids
                if not is_remote_screen_id(screen_id)
            ]
            self.set_form_message(f"Purged {removed} remembered screen(s).", "success")

    def _apply_duplicate_merge(
        self, source_id: str, target_id: str
    ) -> tuple[bool, str]:
        response = self.call_backend_action(
            "merge_duplicate_screen",
            {"source_screen_id": source_id, "target_screen_id": target_id},
        )
        if response is None:
            return False, "Merge failed because the engine is unavailable."
        merged = bool(response.get("merged"))
        if merged:
            message = "Merged duplicate remembered screen into target."
            self.set_form_message(message, "success")
            return True, message
        message = "No merge was applied."
        self.set_form_message(message, "error")
        return False, message

    def merge_duplicate_screens(self, checked: bool = False) -> None:
        remote_ids = sorted(self.remote_screens)
        if len(remote_ids) < 2:
            self.set_form_message(
                "Need at least two remembered remote screens to merge.", "error"
            )
            return

        source_labels = [
            f"{friendly_remote_name(screen_id, self.screen_aliases, self.remote_screens.get(screen_id))} ({screen_id})"
            for screen_id in remote_ids
        ]
        source_label, accepted = QInputDialog.getItem(
            self,
            "Merge Duplicate Screens",
            "Screen to merge from (will be removed):",
            source_labels,
            0,
            False,
        )
        if not accepted:
            return
        source_index = source_labels.index(source_label)
        source_id = remote_ids[source_index]

        target_candidates = [
            screen_id for screen_id in remote_ids if screen_id != source_id
        ]
        target_labels = [
            f"{friendly_remote_name(screen_id, self.screen_aliases, self.remote_screens.get(screen_id))} ({screen_id})"
            for screen_id in target_candidates
        ]
        target_label, accepted = QInputDialog.getItem(
            self,
            "Merge Duplicate Screens",
            "Screen to keep (target):",
            target_labels,
            0,
            False,
        )
        if not accepted:
            return
        target_index = target_labels.index(target_label)
        target_id = target_candidates[target_index]
        self._apply_duplicate_merge(source_id, target_id)

    def open_manage_screens_popup(self, checked: bool = False) -> None:
        self.refresh_monitors()
        dialog = ManageScreensWorkspaceDialog(
            self.unified_screen_targets(include_offline=True),
            self.selected_monitor_ids,
            self.enabled_screen_ids,
            self.screen_aliases,
            merge_handler=self._apply_duplicate_merge,
            parent=self,
        )

        def _sync_dialog_from_controller() -> None:
            dialog.sync_state(
                selected_monitor_ids=self.selected_monitor_ids,
                enabled_screen_ids=self.enabled_screen_ids,
                screen_aliases=self.screen_aliases,
            )
            dialog.replace_targets(self.unified_screen_targets(include_offline=True))

        def _manage_groups() -> None:
            self.manage_screen_groups()
            _sync_dialog_from_controller()

        def _manage_configured() -> None:
            self.manage_configured_screens()
            _sync_dialog_from_controller()

        def _refresh_workspace() -> None:
            self.pull_backend_state(initial=False)
            _sync_dialog_from_controller()

        def _purge_offline() -> None:
            self.purge_offline_screens()
            _sync_dialog_from_controller()

        def _purge_all() -> None:
            self.purge_all_screens()
            _sync_dialog_from_controller()

        dialog.manage_groups_button.clicked.connect(lambda *_args: _manage_groups())
        dialog.manage_configured_button.clicked.connect(
            lambda *_args: _manage_configured()
        )
        dialog.refresh_button.clicked.connect(lambda *_args: _refresh_workspace())

        def _merge_duplicates() -> None:
            self.merge_duplicate_screens()
            _sync_dialog_from_controller()

        dialog.merge_duplicates_button.clicked.connect(
            lambda *_args: _merge_duplicates()
        )
        dialog.purge_offline_button.clicked.connect(lambda *_args: _purge_offline())
        dialog.purge_all_button.clicked.connect(lambda *_args: _purge_all())
        if dialog.exec() == QDialog.DialogCode.Accepted:
            dialog.apply_current()
            updated_selected = sorted(dialog.selected_monitor_ids)
            updated_enabled = sorted(dialog.enabled_screen_ids)
            if updated_selected != sorted(self.selected_monitor_ids):
                self.call_backend_action(
                    "set_selected_screens", {"screen_ids": updated_selected}
                )
            if updated_enabled != sorted(self.enabled_screen_ids):
                self.call_backend_action(
                    "set_enabled_screens", {"screen_ids": updated_enabled}
                )
            all_alias_keys = set(self.screen_aliases) | set(dialog.screen_aliases)
            for screen_id in sorted(all_alias_keys):
                alias = dialog.screen_aliases.get(screen_id, "")
                if self.screen_aliases.get(screen_id, "") != alias:
                    self.call_backend_action(
                        "rename_screen_alias", {"screen_id": screen_id, "alias": alias}
                    )

    def open_help_documentation(self, checked: bool = False) -> None:
        guide_path = APP_ROOT / "docs" / "USER_GUIDE.md"
        if not guide_path.exists():
            self.set_form_message(
                "Help guide file is missing: docs/USER_GUIDE.md", "error"
            )
            return
        try:
            text = guide_path.read_text(encoding="utf-8")
        except OSError as error:
            self.set_form_message(f"Could not load help guide: {error}", "error")
            return
        dialog = HelpCenterDialog(text, self)
        dialog.exec()

    def open_release_checklist(self, checked: bool = False) -> None:
        checklist_path = APP_ROOT / "docs" / "RELEASE_CHECKLIST.md"
        if not checklist_path.exists():
            self.set_form_message(
                "Release checklist file is missing: docs/RELEASE_CHECKLIST.md", "error"
            )
            return
        try:
            text = checklist_path.read_text(encoding="utf-8")
        except OSError as error:
            self.set_form_message(f"Could not load release checklist: {error}", "error")
            return
        dialog = HelpCenterDialog(text, self)
        dialog.setWindowTitle("Release Checklist")
        dialog.exec()

    def open_accessibility_review(self, checked: bool = False) -> None:
        review_path = APP_ROOT / "docs" / "ACCESSIBILITY_CONTRAST_REVIEW.md"
        if not review_path.exists():
            self.set_form_message(
                "Accessibility review file is missing: docs/ACCESSIBILITY_CONTRAST_REVIEW.md",
                "error",
            )
            return
        try:
            text = review_path.read_text(encoding="utf-8")
        except OSError as error:
            self.set_form_message(
                f"Could not load accessibility review: {error}", "error"
            )
            return
        dialog = HelpCenterDialog(text, self)
        dialog.setWindowTitle("Accessibility & Contrast Review")
        dialog.exec()

    def on_transition_changed(self, *_args) -> None:
        self.transition_method = str(self.transition_selector.currentData())
        if self.transition_method not in TRANSITION_METHODS:
            self.transition_method = "fade_black"
        if not self.backend_online:
            self._pending_transition_method = self.transition_method
            self.set_form_message(
                "Transition preference saved locally and will apply when the engine reconnects.",
                "success",
            )
            return
        response = self.call_backend_action(
            "set_transition_method", {"transition_method": self.transition_method}
        )
        if response is not None:
            self._pending_transition_method = None
            self.set_form_message("Transition method updated.", "success")

    def choose_quick_play_targets(self, media_path: Path) -> list[str] | None:
        targets = self.unified_screen_targets(include_offline=False)
        if not targets:
            self.set_form_message("No screens are available for quick play.", "error")
            return None
        dialog = QuickPlayTargetDialog(
            targets, self.selected_monitor_ids, media_path.name, self
        )
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
            self.set_form_message(
                "Dropped file is not a supported media file.", "error"
            )
            return
        target_ids = self.choose_quick_play_targets(path)
        if not target_ids:
            return
        response = self.call_backend_action(
            "quick_play", {"path": str(path), "target_screen_ids": target_ids}
        )
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
            self.set_form_message(
                "Returned all quick-play screens to scheduled playback.", "success"
            )

    def refresh_schedule_list(self) -> None:
        selected_id = self.current_edit_id
        self.schedule_list.blockSignals(True)
        self.schedule_list.clear()
        warning_icon = (
            self.style()
            .standardIcon(QStyle.StandardPixmap.SP_MessageBoxCritical)
            .pixmap(16, 16)
        )
        screen_icon = (
            self.style()
            .standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
            .pixmap(16, 16)
        )
        for entry in sorted(
            self.schedules,
            key=lambda item: ScheduleEntry.time_to_minutes(item.start_time),
        ):
            item = QListWidgetItem()
            missing_screen_ids = []
            for screen_id in entry.screen_ids:
                if is_group_target_id(screen_id):
                    continue
                if is_remote_screen_id(screen_id):
                    remote_state = self.remote_screens.get(screen_id)
                    if remote_state is None or not remote_state.get("online"):
                        missing_screen_ids.append(screen_id)
                elif find_screen_by_id(screen_id) is None:
                    missing_screen_ids.append(screen_id)
            assigned_labels = [
                friendly_remote_name(
                    screen_id, self.screen_aliases, self.remote_screens.get(screen_id)
                )
                if is_remote_screen_id(screen_id)
                else screen_label_from_id(
                    screen_id, self.screen_aliases, self.all_screen_groups()
                )
                for screen_id in entry.screen_ids
            ]
            screens_tooltip = "Assigned targets\n" + "\n".join(assigned_labels)
            if missing_screen_ids:
                missing_labels = [
                    friendly_remote_name(
                        screen_id,
                        self.screen_aliases,
                        self.remote_screens.get(screen_id),
                    )
                    if is_remote_screen_id(screen_id)
                    else screen_label_from_id(
                        screen_id, self.screen_aliases, self.all_screen_groups()
                    )
                    for screen_id in missing_screen_ids
                ]
                warning_tooltip = (
                    "One or more attached screens are disconnected.\n\n"
                    + "\n".join(missing_labels)
                )
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
            self.set_time_button_values(
                QTime.fromString("00:00", "HH:mm"), QTime.fromString("01:00", "HH:mm")
            )
            for checkbox in self.day_checkboxes.values():
                checkbox.setChecked(True)
            self.current_form_screen_ids = [DEFAULT_GROUP_ID]
            self.current_selected_video_file = ""
            self.current_media_assignments = {}
            self.update_media_source_summary()
            self.update_schedule_screen_summary()
            self.refresh_monitors()
            self.delete_schedule_button.setEnabled(False)
            return

        self.title_input.setText(entry.title)
        self.set_time_button_values(
            QTime.fromString(entry.start_time, "HH:mm"),
            QTime.fromString(entry.end_time, "HH:mm"),
        )
        selected_days = set(entry.selected_days())
        for day_key, checkbox in self.day_checkboxes.items():
            checkbox.setChecked(day_key in selected_days)
        self.current_form_screen_ids = entry.screen_ids[:]
        self.current_selected_video_file = entry.video_file
        self.current_media_assignments = {
            screen_id: ScheduleMediaAssignment.from_dict(assignment.to_dict())
            for screen_id, assignment in entry.media_assignments.items()
        }
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
            entry = next(
                (item for item in self.schedules if item.id == self.current_edit_id),
                None,
            )
            if entry is not None and entry.video_file:
                self.choose_media_button.setText("Change Media")
                self.choose_media_button.setToolTip(
                    f"Current media: {entry.video_label or entry.video_file}"
                )
            else:
                self.choose_media_button.setText("Choose Media")
                self.choose_media_button.setToolTip(
                    "Choose a media file from the selected folder."
                )
        self.update_selection_summary()

    def open_media_picker(self) -> None:
        if self.video_directory is None:
            self.set_form_message(
                "Set the media folder first from the Library menu.", "error"
            )
            return
        if self.backend_library_scanning and not self.available_videos:
            self.set_form_message(
                "Media library is still indexing. Try again in a moment.", "error"
            )
            return
        if not self.available_videos:
            message = "No supported media files were found in the selected folder."
            if self.backend_library_error:
                message = f"Media indexing failed: {self.backend_library_error}"
            self.set_form_message(message, "error")
            return
        initial_selection = self.current_selected_video_file
        if not initial_selection:
            entry = next(
                (item for item in self.schedules if item.id == self.current_edit_id),
                None,
            )
            initial_selection = entry.video_file if entry is not None else ""
        dialog = MediaPickerDialog(
            self.available_videos, self.video_directory, initial_selection, self
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if dialog.selected_path:
            self.current_selected_video_file = dialog.selected_path
            self.update_media_source_summary()
            self.set_form_message(f"Media selected: {dialog.selected_path}", "success")

    def _relative_media_from_absolute(self, absolute_path: str) -> str:
        if self.video_directory is None:
            return ""
        target = Path(absolute_path)
        try:
            return (
                target.resolve().relative_to(self.video_directory.resolve()).as_posix()
            )
        except ValueError:
            return ""

    def edit_per_screen_media(self) -> None:
        if self.video_directory is None:
            self.set_form_message(
                "Set the media folder first from the Library menu.", "error"
            )
            return
        target_ids = expand_target_ids(
            self.current_form_screen_ids, self.all_screen_groups()
        )
        if not target_ids:
            self.set_form_message("Choose schedule target screens first.", "error")
            return
        picked, accepted = QInputDialog.getItem(
            self,
            "Per-Screen Media",
            "Select screen",
            [
                screen_label_from_id(
                    screen_id, self.screen_aliases, self.all_screen_groups()
                )
                + " | "
                + screen_id
                for screen_id in target_ids
            ],
            0,
            False,
        )
        if not accepted:
            return
        selected_screen_id = str(picked).rsplit(" | ", 1)[-1]
        mode, mode_ok = QInputDialog.getItem(
            self,
            "Assignment Mode",
            "Choose assignment type",
            ["Single Media", "Cycle Images"],
            0,
            False,
        )
        if not mode_ok:
            return
        if mode == "Cycle Images":
            files, _ = QFileDialog.getOpenFileNames(
                self,
                "Choose Image Set",
                str(self.video_directory),
                "Images (*.jpg *.jpeg *.png *.webp *.bmp)",
            )
            rel_files = [self._relative_media_from_absolute(item) for item in files]
            rel_files = [item for item in rel_files if item]
            if len(rel_files) < 2:
                self.set_form_message(
                    "Choose at least 2 images for cycle mode.", "error"
                )
                return
            self.current_media_assignments[selected_screen_id] = (
                ScheduleMediaAssignment(
                    screen_id=selected_screen_id,
                    media_files=rel_files,
                    mode="cycle",
                    interval_seconds=10,
                )
            )
            self.set_form_message(
                f"Saved image cycle override for {selected_screen_id}.", "success"
            )
            return
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose Media",
            str(self.video_directory),
            "Media Files (*.mp4 *.mov *.m4v *.webm *.ogg *.jpg *.jpeg *.png *.webp *.bmp)",
        )
        relative = self._relative_media_from_absolute(file_path) if file_path else ""
        if not relative:
            return
        self.current_media_assignments[selected_screen_id] = ScheduleMediaAssignment(
            screen_id=selected_screen_id,
            media_files=[relative],
            mode="single",
            interval_seconds=10,
        )
        self.set_form_message(
            f"Saved media override for {selected_screen_id}.", "success"
        )

    def set_time_button_values(self, start_time: QTime, end_time: QTime) -> None:
        self.start_time_input.setTime(start_time)
        self.end_time_input.setTime(end_time)
        self.start_time_button.setText(start_time.toString("HH:mm"))
        self.end_time_button.setText(end_time.toString("HH:mm"))

    def open_time_picker(self, field: str) -> None:
        current_time = (
            self.start_time_input.time()
            if field == "start"
            else self.end_time_input.time()
        )
        anchor_button = (
            self.start_time_button if field == "start" else self.end_time_button
        )
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
        if self._low_value_timers_paused:
            return
        self.folder_refresh_timer.start()

    def on_schedule_selected(
        self, current: QListWidgetItem | None, previous: QListWidgetItem | None
    ) -> None:
        if current is None:
            return
        self.load_schedule_into_form(str(current.data(Qt.ItemDataRole.UserRole)))

    def launch_selected_monitors(
        self, checked: bool = False, silent: bool = False
    ) -> None:
        response = self.call_backend_action("launch_selected")
        if response is not None and not silent:
            self.set_form_message("Playback started.", "success")

    def build_candidate_from_form(self) -> ScheduleEntry:
        existing = next(
            (item for item in self.schedules if item.id == self.current_edit_id), None
        )
        schedule_id = self.current_edit_id or uuid.uuid4().hex
        title = self.title_input.text().strip()
        fallback_title = title
        selected_video_file = self.current_selected_video_file or (
            existing.video_file if existing is not None else ""
        )
        if not fallback_title and selected_video_file:
            fallback_title = Path(selected_video_file).stem
        elif not fallback_title and existing is not None:
            fallback_title = existing.title
        elif not fallback_title:
            fallback_title = "Untitled schedule"

        video_file = selected_video_file
        video_label = selected_video_file if selected_video_file else ""
        target_ids = normalize_schedule_target_ids(self.current_form_screen_ids)
        if not target_ids:
            raise ValueError(
                "Choose at least one target screen or screen group for this schedule."
            )

        return ScheduleEntry(
            id=schedule_id,
            title=fallback_title,
            start_time=self.start_time_input.time().toString("HH:mm"),
            end_time=self.end_time_input.time().toString("HH:mm"),
            video_file=video_file,
            video_label=video_label,
            screen_ids=target_ids,
            days=[
                day_key
                for day_key, checkbox in self.day_checkboxes.items()
                if checkbox.isChecked()
            ],
            media_assignments={
                screen_id: ScheduleMediaAssignment.from_dict(assignment.to_dict())
                for screen_id, assignment in self.current_media_assignments.items()
            },
        )

    def save_schedule(self) -> None:
        try:
            candidate = self.build_candidate_from_form()
            response = self.call_backend_action(
                "save_schedule", {"schedule": candidate.to_dict()}
            )
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
        response = self.call_backend_action(
            "delete_schedule", {"schedule_id": current_id}
        )
        if response is not None:
            self.current_edit_id = None
            self.set_form_message("Schedule deleted.", "success")

    def apply_backend_snapshot(self, snapshot: dict[str, Any]) -> None:
        should_reload_form = False
        should_refresh_monitors = False
        should_update_network = False
        should_update_diagnostics = False
        should_update_status = False
        should_update_playback_label = False
        self.backend_online = True
        self.backend_state_version = int(
            snapshot.get("stateVersion") or self.backend_state_version
        )
        channel_versions = snapshot.get("channelVersions") or {}
        for name in STATE_CHANNELS:
            self.backend_channel_versions[name] = max(
                int(self.backend_channel_versions.get(name) or 0),
                int(channel_versions.get(name) or 0),
            )
        self.backend_listener.seed_state_version(self.backend_state_version)
        self.backend_listener.seed_channel_versions(self.backend_channel_versions)

        channels = snapshot.get("channels")
        if isinstance(channels, list) and channels:
            changed_channels = {str(item) for item in channels}
        else:
            changed_channels = set(STATE_CHANNELS)

        if "config" in changed_channels:
            previous_configured = [
                screen.to_dict() for screen in self.configured_screens
            ]
            previous_groups = [group.to_dict() for group in self.screen_groups]
            previous_schedule_ids = [entry.id for entry in self.schedules]
            previous_selected = list(self.selected_monitor_ids)
            previous_enabled = list(self.enabled_screen_ids)
            previous_aliases = dict(self.screen_aliases)

            self.configured_screens = parse_configured_screens(
                snapshot.get("configuredScreens")
            )
            self.screen_groups = parse_screen_groups(snapshot.get("screenGroups"))
            schedules_payload = snapshot.get("schedules") or []
            self.schedules = [
                ScheduleEntry.from_dict(item)
                for item in schedules_payload
                if isinstance(item, dict)
            ]
            self.selected_monitor_ids = [
                str(item) for item in snapshot.get("selectedMonitorIds") or []
            ]
            self.enabled_screen_ids = [
                str(item) for item in snapshot.get("enabledScreenIds") or []
            ]
            video_directory = str(snapshot.get("videoDirectory") or "").strip()
            self.video_directory = (
                Path(video_directory).expanduser() if video_directory else None
            )
            if self.video_directory is not None and not self.video_directory.exists():
                self.video_directory = None
            self.screen_aliases = {
                str(key): str(value)
                for key, value in (snapshot.get("screenAliases") or {}).items()
            }
            self.screen_aliases.update(
                self.screen_registry.configured_screen_name_map()
            )
            self.transition_method = str(
                snapshot.get("transitionMethod") or "fade_black"
            )
            self.run_at_startup = bool(snapshot.get("runAtStartup"))
            self.lan_pairing_required = bool(snapshot.get("lanPairingRequired"))
            self.lan_allow_unpaired_clients = bool(
                snapshot.get("lanAllowUnpairedClients")
            )
            self.lan_paired_client_ids = [
                str(item).strip()
                for item in (snapshot.get("lanPairedClientIds") or [])
                if str(item).strip()
            ]

            self.run_at_startup_action.blockSignals(True)
            self.run_at_startup_action.setChecked(self.run_at_startup)
            self.run_at_startup_action.blockSignals(False)

            transition_index = self.transition_selector.findData(self.transition_method)
            if (
                transition_index >= 0
                and transition_index != self.transition_selector.currentIndex()
            ):
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

            should_refresh_monitors = (
                should_refresh_monitors
                or previous_configured
                != [screen.to_dict() for screen in self.configured_screens]
                or previous_groups != [group.to_dict() for group in self.screen_groups]
                or previous_schedule_ids != [entry.id for entry in self.schedules]
                or previous_selected != list(self.selected_monitor_ids)
                or previous_enabled != list(self.enabled_screen_ids)
                or previous_aliases != dict(self.screen_aliases)
            )

        if "library" in changed_channels:
            available_media = snapshot.get("availableMedia") or []
            if self.video_directory is not None:
                self.available_videos = [
                    self.video_directory / str(item.get("relativePath") or "")
                    for item in available_media
                    if item.get("relativePath")
                ]
            else:
                self.available_videos = []
            library = snapshot.get("library") or {}
            self.backend_library_scanning = bool(library.get("scanning"))
            self.backend_library_error = str(library.get("error") or "")
            should_update_diagnostics = True

        if "runtime" in changed_channels:
            previous_remote = dict(self.remote_screens)
            previous_network = dict(self.backend_network_snapshot)
            previous_paused = bool(self.backend_paused)
            previous_playback_enabled = bool(self.backend_playback_enabled)
            previous_window_count = int(self.backend_window_count)

            self.remote_screens = {
                str(item.get("screen_id")): item
                for item in snapshot.get("remoteScreens") or []
                if isinstance(item, dict) and item.get("screen_id")
            }
            self.backend_network_snapshot = dict(snapshot.get("network") or {})
            playback = snapshot.get("playback") or {}
            self.backend_playback_enabled = bool(playback.get("enabled"))
            self.backend_paused = bool(playback.get("paused"))
            self.backend_window_count = int(playback.get("localWindowCount") or 0)

            should_refresh_monitors = (
                should_refresh_monitors or previous_remote != self.remote_screens
            )
            should_update_network = (
                should_update_network
                or previous_network != self.backend_network_snapshot
                or previous_remote != self.remote_screens
            )
            should_update_playback_label = (
                should_update_playback_label
                or previous_paused != self.backend_paused
                or previous_playback_enabled != self.backend_playback_enabled
                or previous_window_count != self.backend_window_count
            )
            should_update_status = True

        previous_performance = dict(self.backend_performance_metrics)
        self.backend_performance_metrics = dict(snapshot.get("performance") or {})
        if previous_performance != self.backend_performance_metrics:
            should_update_diagnostics = True

        if should_refresh_monitors:
            self.refresh_monitors()
        if should_reload_form:
            self.load_schedule_into_form(self.current_edit_id)
        status = snapshot.get("status") or {}
        if should_update_status and status:
            self.update_status_labels(
                str(status.get("clockLabel") or "--:--:--"),
                str(status.get("activeLabel") or "No active schedule"),
            )
        if should_update_playback_label or should_update_status:
            self.update_playback_state_label(
                self.backend_paused, self.backend_window_count
            )
        if should_update_network:
            self.update_network_summary()
        if should_update_diagnostics:
            self.update_engine_diagnostics_summary()

    def update_status_labels(self, clock_label: str, active_label: str) -> None:
        if not hasattr(self, "footer_time_primary"):
            return
        self.footer_time_primary.setText(clock_label)
        if active_label == "Playback stopped":
            self.hero_title_label.setText("Playback stopped")
            self.hero_subtitle_label.setText(
                "Screens remain idle until you launch playback again."
            )
            self.update_hero_progress(0.0)
            return
        now = current_uk_datetime()
        weekday_index = now.weekday()
        minute_of_day = (now.hour * 60) + now.minute
        active_entry = next(
            (
                entry
                for screen_id in self.selected_monitor_ids
                if (
                    entry := active_schedule_for_screen(
                        self.schedules,
                        weekday_index,
                        minute_of_day,
                        screen_id,
                        self.all_screen_groups(),
                    )
                )
                is not None
            ),
            active_schedule_for_minute(self.schedules, weekday_index, minute_of_day),
        )
        if active_entry is None:
            self.hero_title_label.setText(
                active_label
                if active_label != "No active schedule"
                else "No active slot"
            )
            self.hero_subtitle_label.setText("Waiting for a matching UK-time schedule.")
            self.update_hero_progress(0.0)
        else:
            self.hero_title_label.setText(
                f"{active_entry.title} • {active_entry.range_label} • {active_entry.video_label or active_entry.video_file}"
            )
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
            if state.get("online")
            and str(state.get("state") or "") in {"playing", "connected"}
            and str(state.get("current_media") or "")
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
            self.footer_global_primary.setText(f"{selected} in default")
            self.footer_global_secondary.setText("Default playback group")
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
            global_tooltip = "Default playback group\nNo screens were detected."
        elif selected == 0:
            global_tooltip = (
                "Default playback group\n"
                f"0 of {total} screen(s) are selected for the default playback group.\n"
                "Schedules can still target individual screens or named groups directly."
            )
        else:
            selected_names = [
                (
                    friendly_remote_name(
                        screen_id,
                        self.screen_aliases,
                        self.remote_screens.get(screen_id),
                    )
                    if is_remote_screen_id(screen_id)
                    else screen_display_name(screen, self.screen_aliases)
                )
                for screen_id in self.selected_monitor_ids
                if is_remote_screen_id(screen_id)
                or (screen := find_screen_by_id(screen_id)) is not None
            ]
            global_tooltip = (
                "Default playback group\n"
                f"{selected} of {total} screen(s) are selected for the default playback group.\n"
                "Schedules can still target individual screens or named groups directly.\n\n"
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
            self.footer_active_primary.setText(
                f"{self.current_active_screen_count} active"
            )
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
        target_label = format_screen_targets(
            self.current_form_screen_ids, self.screen_aliases, self.all_screen_groups()
        )
        self.schedule_screen_summary_label.setText(target_label)
        if hasattr(self, "schedule_screen_button"):
            self.schedule_screen_button.setText(
                "Target Screens"
                if not self.current_form_screen_ids
                else f"Target Screens ({len(self.current_form_screen_ids)})"
            )
        self.update_selection_summary()

    def update_engine_diagnostics_summary(self) -> None:
        if not hasattr(self, "footer_diagnostics_primary"):
            return
        primary, secondary, tooltip = format_engine_diagnostics_summary(
            self.backend_performance_metrics,
            scanning=self.backend_library_scanning,
            library_error=self.backend_library_error,
        )
        self.footer_diagnostics_primary.setText(primary)
        self.footer_diagnostics_secondary.setText(secondary)
        for widget in (
            getattr(self, "footer_diagnostics_icon", None),
            getattr(self, "footer_diagnostics_primary", None),
            getattr(self, "footer_diagnostics_secondary", None),
        ):
            if widget is not None:
                widget.setToolTip(tooltip)

    def update_library_summary(self) -> None:
        if self.video_directory is None:
            self.choose_media_button.setEnabled(True)
            existing_tooltip = self.choose_media_button.toolTip().strip()
            if existing_tooltip in {
                "",
                "Choose a media file from the selected folder.",
            }:
                self.choose_media_button.setToolTip(
                    "No media folder selected. Use Library > Set Media Folder."
                )
            return
        unique_files = sorted(referenced_video_files(self.schedules))
        available_count = len(self.available_videos)
        category_count = len(
            {
                media_category_label(path, self.video_directory)
                for path in self.available_videos
            }
        )
        existing_tooltip = self.choose_media_button.toolTip().strip()
        if self.backend_library_scanning:
            self.choose_media_button.setToolTip(
                f"Indexing media library... {available_count} file(s) discovered so far."
            )
            return
        if self.backend_library_error:
            self.choose_media_button.setToolTip(
                f"Media indexing error: {self.backend_library_error}"
            )
            return
        if (
            not self.current_selected_video_file
            and "Current media:" not in existing_tooltip
            and "Selected media:" not in existing_tooltip
        ):
            self.choose_media_button.setToolTip(
                f"{available_count} media file(s) across {category_count} folder group(s) • {len(unique_files)} linked."
            )

    def update_selection_summary(self) -> None:
        if not hasattr(self, "schedule_screen_button"):
            return
        target_label = format_screen_targets(
            self.current_form_screen_ids, self.screen_aliases, self.all_screen_groups()
        )
        transition_label = TRANSITION_METHODS.get(
            self.transition_method, "Fade Through Black"
        )
        self.schedule_screen_button.setToolTip(f"Target screens: {target_label}")
        self.transition_selector.setToolTip(f"Transition: {transition_label}")

    def closeEvent(self, event: QCloseEvent) -> None:  # type: ignore[override]
        if not self._allow_real_quit:
            self.prepare_for_exit(stop_engine=False)
            self._allow_real_quit = True
            event.accept()
            app = QApplication.instance()
            if app is not None:
                app.exit(0)
            return
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
        if event.type() == QEvent.Type.WindowStateChange:
            if self.isMinimized():
                self._set_low_value_timers_paused(True)
            elif self.isVisible():
                self._set_low_value_timers_paused(False)
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
        if not self.isMinimized():
            self._set_low_value_timers_paused(False)
        self.schedule_layout_refresh()

    def update_hero_progress(self, progress: float) -> None:
        self._hero_progress = max(0.0, min(progress, 1.0))
        track_width = self.hero_progress_track.width()
        fill_width = max(
            10 if self._hero_progress > 0 else 0, int(track_width * self._hero_progress)
        )
        self.hero_progress_fill.setGeometry(
            0, 0, fill_width, self.hero_progress_track.height()
        )

    def schedule_layout_refresh(self) -> None:
        for delay in (0, 40, 120, 260):
            QTimer.singleShot(delay, self.refresh_responsive_layout)

    def refresh_responsive_layout(self) -> None:
        if not hasattr(self, "content_splitter"):
            return
        screen = (
            self.windowHandle().screen()
            if self.windowHandle() is not None
            else self.screen()
        )
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
            mode != self._layout_mode or abs(width - self._last_layout_width) > 24
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
        if (
            self.centralWidget() is not None
            and self.centralWidget().layout() is not None
        ):
            self.centralWidget().layout().activate()


def current_uk_time_status(schedules: list[ScheduleEntry]) -> tuple[str, str]:
    clock_label, minute_of_day, weekday_index = current_uk_time()
    entry = active_schedule_for_minute(schedules, weekday_index, minute_of_day)
    if entry is None:
        return clock_label, "No active schedule"
    return (
        clock_label,
        f"{entry.title} • {entry.range_label} • {entry.video_label or entry.video_file}",
    )


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
                    "$script = [regex]::Escape('"
                    + script_path.replace("'", "''")
                    + "'); "
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


STATE_CHANNELS = ("config", "library", "runtime")


class ControllerApiClient:
    def __init__(self, port: int = ENGINE_CONTROL_PORT) -> None:
        self.base_url = f"http://127.0.0.1:{port}"

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.base_url}{path}", data=data, method=method, headers=headers
        )
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

    def wait_for_state_update(
        self,
        since: int,
        timeout: float = 30.0,
        channel_versions: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        timeout_ms = max(1000, int(timeout * 1000))
        versions = channel_versions or {}
        query = (
            f"since={int(since)}&timeout_ms={timeout_ms}"
            f"&c_config={max(0, int(versions.get('config') or 0))}"
            f"&c_library={max(0, int(versions.get('library') or 0))}"
            f"&c_runtime={max(0, int(versions.get('runtime') or 0))}"
        )
        return self._request(
            "GET",
            f"/api/subscribe?{query}",
            timeout=timeout + 5.0,
        )

    def wait_for_local_worker_command(
        self,
        screen_id: str,
        since_version: int,
        timeout: float = 25.0,
    ) -> dict[str, Any]:
        timeout_ms = max(1000, int(timeout * 1000))
        query = (
            f"screen_id={quote(str(screen_id))}"
            f"&since={max(0, int(since_version))}"
            f"&timeout_ms={timeout_ms}"
        )
        return self._request(
            "GET",
            f"/api/local-worker-command?{query}",
            timeout=timeout + 5.0,
        )

    def action(
        self, action: str, payload: dict[str, Any] | None = None, timeout: float = 5.0
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/action",
            {"action": action, "payload": payload or {}},
            timeout=timeout,
        )


class BackendStateListener(QObject):
    snapshot_received = Signal(dict)
    connection_changed = Signal(bool, str)

    def __init__(
        self, client: ControllerApiClient, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self.client = client
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._cursor_lock = threading.Lock()
        self._last_version = 0
        self._online = False
        self._consecutive_failures = 0
        self._channel_versions = {name: 0 for name in STATE_CHANNELS}

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="backend-state-listener", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None

    def seed_state_version(self, version: int) -> None:
        with self._cursor_lock:
            self._last_version = max(0, int(version))

    def seed_channel_versions(self, versions: dict[str, Any] | None) -> None:
        incoming = versions or {}
        with self._cursor_lock:
            for name in STATE_CHANNELS:
                self._channel_versions[name] = max(
                    0, int(incoming.get(name) or self._channel_versions.get(name) or 0)
                )

    def _emit_connection(self, online: bool, detail: str = "") -> None:
        if self._online == online and not detail:
            return
        self._online = online
        self.connection_changed.emit(online, detail)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            with self._cursor_lock:
                since = self._last_version
                channel_versions = dict(self._channel_versions)
            try:
                response = self.client.wait_for_state_update(
                    since,
                    timeout=20.0,
                    channel_versions=channel_versions,
                )
                self._consecutive_failures = 0
                version = int(response.get("stateVersion") or since)
                channel_versions_response = response.get("channelVersions") or {}
                with self._cursor_lock:
                    self._last_version = max(self._last_version, version)
                    for name in STATE_CHANNELS:
                        self._channel_versions[name] = max(
                            self._channel_versions.get(name, 0),
                            int(channel_versions_response.get(name) or 0),
                        )
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
    def __init__(
        self, engine: "ControllerEngine", port: int = ENGINE_CONTROL_PORT
    ) -> None:
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
            raise RuntimeError(
                f"Engine control server failed to bind to 127.0.0.1:{self.port}: {error}"
            ) from error
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="controller-engine-api", daemon=True
        )
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

    def _write_json(
        self,
        handler: BaseHTTPRequestHandler,
        payload: dict[str, Any],
        status: int = HTTPStatus.OK,
    ) -> None:
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
                self._write_json(
                    handler,
                    self.engine.run_on_engine_thread(
                        lambda: self.engine.snapshot_for_channels(None)
                    ),
                )
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

                def _parse_channel_version(param: str) -> int:
                    try:
                        raw = str((params.get(param) or ["0"])[0])
                        return max(0, int(raw))
                    except (TypeError, ValueError):
                        return 0

                client_channel_versions = {
                    "config": _parse_channel_version("c_config"),
                    "library": _parse_channel_version("c_library"),
                    "runtime": _parse_channel_version("c_runtime"),
                }
                updated, version = self.engine.wait_for_state_update(
                    since, timeout_seconds
                )
                payload: dict[str, Any] = {
                    "ok": True,
                    "updated": updated,
                    "stateVersion": version,
                    "channelVersions": self.engine.run_on_engine_thread(
                        self.engine.channel_versions_snapshot
                    ),
                }
                if updated:
                    payload["snapshot"] = self.engine.run_on_engine_thread(
                        lambda: self.engine.snapshot_for_channels(
                            client_channel_versions
                        )
                    )
                self._write_json(handler, payload)
                return
            if parsed.path == "/api/local-worker-command":
                params = parse_qs(parsed.query)
                screen_id = str((params.get("screen_id") or [""])[0]).strip()
                try:
                    since = max(0, int(str((params.get("since") or ["0"])[0])))
                except ValueError:
                    since = 0
                try:
                    timeout_ms = int(str((params.get("timeout_ms") or ["25000"])[0]))
                except ValueError:
                    timeout_ms = 25000
                timeout_seconds = min(max(timeout_ms / 1000.0, 1.0), 30.0)
                command_response = self.engine.wait_for_local_worker_command(
                    screen_id,
                    since,
                    timeout_seconds,
                )
                payload = {"ok": True, **command_response}
                self._write_json(handler, payload)
                return
            self._write_json(
                handler, {"ok": False, "error": "not_found"}, HTTPStatus.NOT_FOUND
            )
        except Exception as error:  # noqa: BLE001
            append_engine_startup_log(
                f"Engine control GET failure on {parsed.path}: {error}"
            )
            append_engine_diagnostics_event(
                "engine_control_get_failure",
                severity="error",
                details=str(error),
                context={"path": parsed.path},
            )
            self._write_json(
                handler,
                {"ok": False, "error": str(error)},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def _handle_post(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urlparse(handler.path)
        if parsed.path != "/api/action":
            self._write_json(
                handler, {"ok": False, "error": "not_found"}, HTTPStatus.NOT_FOUND
            )
            return
        request = self._read_json(handler)
        action = str(request.get("action") or "").strip()
        payload = request.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        try:
            result = self.engine.run_on_engine_thread(
                lambda: self.engine.handle_action(action, payload)
            )
            append_engine_diagnostics_event(
                "engine_action_succeeded",
                details=f"Action {action} applied.",
                context={"action": action},
            )
        except Exception as error:  # noqa: BLE001
            append_engine_diagnostics_event(
                "engine_action_failed",
                severity="error",
                details=str(error),
                context={"action": action},
            )
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
        self._channel_versions = {name: 0 for name in STATE_CHANNELS}
        self._media_scan_lock = threading.Lock()
        self._media_scan_generation = 0
        self._media_scan_target_dir: Path | None = None
        self._media_scan_active = False
        self._media_scan_started_monotonic = 0.0
        self.media_scan_in_progress = False
        self.media_scan_error = ""
        self.last_media_scan_duration_ms = 0
        self.last_media_scan_file_count = 0
        self.last_snapshot_size_bytes = 0
        self.last_snapshot_generated_at = ""
        config = load_config()
        self.configured_screens = [
            ConfiguredScreen.from_dict(item.to_dict())
            for item in config["configured_screens"]
        ]
        try:
            validate_unique_browser_bindings(self.configured_screens)
        except ValueError:
            self.configured_screens = []
        self.screen_groups = [
            ScreenGroup.from_dict(item.to_dict()) for item in config["screen_groups"]
        ]
        self.schedules = [ScheduleEntry.from_dict(item) for item in config["schedules"]]
        self.selected_monitor_ids = [
            str(item) for item in config["selected_monitor_ids"]
        ]
        self.enabled_screen_ids = [
            str(item) for item in config.get("enabled_screen_ids") or []
        ]
        self.video_directory: Path | None = (
            Path(config["video_directory"]).expanduser()
            if config["video_directory"]
            else None
        )
        if self.video_directory is not None and not self.video_directory.exists():
            self.video_directory = None
        self.screen_aliases = {
            str(key): str(value) for key, value in config["screen_aliases"].items()
        }
        self.screen_registry = ScreenRegistry(self)
        self.screen_aliases.update(self.screen_registry.configured_screen_name_map())
        self.transition_method = str(config["transition_method"])
        self.run_at_startup = bool(config["run_at_startup"])
        self.lan_pairing_required = bool(config.get("lan_pairing_required"))
        self.lan_allow_unpaired_clients = bool(config.get("lan_allow_unpaired_clients"))
        self.lan_paired_client_ids = [
            str(item).strip()
            for item in (config.get("lan_paired_client_ids") or [])
            if str(item).strip()
        ]
        self.available_videos: list[Path] = []
        self.remote_screens: dict[str, dict[str, Any]] = {}
        self.status_clock_label, self.status_active_label = current_uk_time_status(
            self.schedules
        )
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
        self.coordinator.set_virtual_screen_ids(self.browser_configured_screen_ids())
        self.coordinator.set_screen_groups(self.all_screen_groups())
        self.coordinator.set_video_directory(self.video_directory)
        self.coordinator.set_transition_method(self.transition_method)
        self.remote_server = LanRemoteServer()
        self.remote_server.set_change_callback(self.remote_server_changed.emit)
        self.remote_server.set_media_root(self.video_directory)
        self.remote_server.configure_pairing(
            self.lan_pairing_required,
            self.lan_allow_unpaired_clients,
            self.lan_paired_client_ids,
        )
        self.engine_tray_icon: QSystemTrayIcon | None = None
        self.remote_server.start()
        self.control_server = EngineControlServer(self)
        self.control_server.start()
        self.setup_engine_tray_icon()
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

    def browser_configured_screens(self) -> list[ConfiguredScreen]:
        registry = getattr(self, "screen_registry", None)
        if registry is not None:
            return registry.browser_configured_screens()
        return [
            screen
            for screen in self.configured_screens
            if screen.transport == "browser"
        ]

    def browser_configured_screen_ids(self) -> set[str]:
        registry = getattr(self, "screen_registry", None)
        if registry is not None:
            return registry.browser_configured_screen_ids()
        return configured_browser_screen_ids(self.configured_screens)

    def static_media_configured_screen_ids(self) -> set[str]:
        registry = getattr(self, "screen_registry", None)
        if registry is not None:
            return registry.static_media_configured_screen_ids()
        return {
            screen.id
            for screen in self.configured_screens
            if "static_media" in screen.capabilities
        }

    def all_screen_groups(self) -> list[ScreenGroup]:
        registry = getattr(self, "screen_registry", None)
        if registry is not None:
            return registry.all_screen_groups()
        return combined_screen_groups(self.selected_monitor_ids, self.screen_groups)

    def on_playback_state_changed(self, paused: bool, window_count: int) -> None:
        self.playback_paused = paused
        self.local_window_count = (
            len(self.local_playback_workers)
            if not self.coordinator.manage_local_windows
            else window_count
        )
        self.notify_state_changed("runtime")

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
            raise TimeoutError(
                "The engine UI thread did not process the request in time."
            )
        if "error" in outcome:
            raise outcome["error"]
        return outcome.get("result")

    def channel_versions_snapshot(self) -> dict[str, int]:
        return {
            name: int(self._channel_versions.get(name, 0)) for name in STATE_CHANNELS
        }

    def notify_state_changed(self, *channels: str) -> int:
        requested = [name for name in channels if name in STATE_CHANNELS]
        if not requested:
            requested = ["runtime"]
        for name in requested:
            self._channel_versions[name] = int(self._channel_versions.get(name, 0)) + 1
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

    def local_worker_command_payload(self, screen_id: str) -> dict[str, Any]:
        command = self.build_remote_server_commands().get(screen_id) or {
            "protocolVersion": COMMAND_PROTOCOL_VERSION,
            "screenId": screen_id,
            "version": 0,
            "type": "clear",
            "mode": "idle",
            "message": "",
            "label": "",
            "path": "",
            "paths": [],
            "relativePath": "",
            "relativePaths": [],
            "mediaKind": "",
            "mediaUrl": "",
            "mediaUrls": [],
            "cycle": False,
            "cycleIntervalSeconds": 10,
            "playAtMs": 0,
            "transition": "fade_black",
            "paused": False,
        }
        return command

    def wait_for_local_worker_command(
        self, screen_id: str, since_version: int, timeout: float
    ) -> dict[str, Any]:
        normalized_screen_id = str(screen_id or "").strip()
        baseline = max(0, int(since_version))
        timeout_seconds = max(0.0, float(timeout))
        deadline = time.monotonic() + timeout_seconds
        state_since = -1

        while True:
            command = self.run_on_engine_thread(
                lambda: self.local_worker_command_payload(normalized_screen_id)
            )
            version = max(0, int(command.get("version") or 0))
            if version != baseline:
                return {
                    "updated": True,
                    "commandVersion": version,
                    "command": command,
                }

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {
                    "updated": False,
                    "commandVersion": version,
                    "command": command,
                }

            wait_for = min(remaining, 5.0)
            updated, state_version = self.wait_for_state_update(state_since, wait_for)
            state_since = state_version
            if not updated:
                continue

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
            self.configured_screens,
            self.screen_groups,
            self.selected_monitor_ids,
            self.enabled_screen_ids,
            str(self.video_directory) if self.video_directory is not None else "",
            self.screen_aliases,
            self.transition_method,
            self.run_at_startup,
            self.lan_pairing_required,
            self.lan_allow_unpaired_clients,
            self.lan_paired_client_ids,
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

    def performance_metrics_payload(self) -> dict[str, Any]:
        return {
            "mediaScanDurationMs": int(self.last_media_scan_duration_ms),
            "indexedFileCount": int(self.last_media_scan_file_count),
            "snapshotSizeBytes": int(self.last_snapshot_size_bytes),
            "localPlaybackWorkerCount": len(self.local_playback_workers),
            "lastSnapshotGeneratedAt": self.last_snapshot_generated_at,
        }

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
            self.last_media_scan_duration_ms = 0
            self.last_media_scan_file_count = 0
            self.sync_playback_outputs()
            self.notify_state_changed("library", "runtime")
            return
        should_start_worker = False
        with self._media_scan_lock:
            if not self.media_scan_in_progress:
                self.media_scan_in_progress = True
                self.media_scan_error = ""
                self._media_scan_started_monotonic = time.monotonic()
                should_start_worker = True
            elif not self._media_scan_active:
                should_start_worker = True
        self.notify_state_changed("library", "runtime")
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

    def on_media_scan_completed(
        self, results: object, generation: int, error_message: str
    ) -> None:
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
            self.last_media_scan_file_count = len(self.available_videos)
            if self._media_scan_started_monotonic > 0:
                self.last_media_scan_duration_ms = max(
                    0,
                    int((time.monotonic() - self._media_scan_started_monotonic) * 1000),
                )
            else:
                self.last_media_scan_duration_ms = 0
            self._media_scan_started_monotonic = 0.0
            append_engine_startup_log(
                "Media scan completed: "
                f"{self.last_media_scan_file_count} file(s) in "
                f"{self.last_media_scan_duration_ms} ms"
                + (
                    f" (error: {self.media_scan_error})"
                    if self.media_scan_error
                    else ""
                )
            )
            self.sync_playback_outputs()
            self.notify_state_changed("library", "runtime")
            return
        if next_generation > 0 and next_directory is not None:
            self._launch_media_scan_worker(next_generation, next_directory)

    def refresh_remote_screens(self) -> None:
        previous_snapshot = {
            screen_id: remote_screen_digest(state)
            for screen_id, state in self.remote_screens.items()
        }
        self.remote_screens = {
            item["screen_id"]: item
            for item in self.remote_server.remote_screens_snapshot()
        }
        for screen_id, state in self.remote_screens.items():
            if (
                screen_id not in self.screen_aliases
                and str(state.get("name") or "").strip()
            ):
                self.screen_aliases[screen_id] = str(state.get("name")).strip()
        self.sync_remote_server_commands()
        current_snapshot = {
            screen_id: remote_screen_digest(state)
            for screen_id, state in self.remote_screens.items()
        }
        if current_snapshot != previous_snapshot:
            self.notify_state_changed("runtime")

    def ensure_firewall_access(self, force_retry: bool = False) -> None:
        before_warning = self.remote_server.network_snapshot().get("warnings") or []
        before_configured = bool(
            self.remote_server.network_snapshot().get("firewallConfigured")
        )
        configured = self.remote_server.ensure_firewall_rule(force_retry=force_retry)
        after_snapshot = self.remote_server.network_snapshot()
        after_warning = after_snapshot.get("warnings") or []
        after_configured = bool(after_snapshot.get("firewallConfigured"))
        if (
            configured != before_configured
            or after_warning != before_warning
            or after_configured != before_configured
        ):
            self.notify_state_changed("runtime")

    def unified_screen_targets(
        self, include_offline: bool = True
    ) -> list[UnifiedScreenTarget]:
        registry = getattr(self, "screen_registry", None)
        if registry is not None:
            return registry.unified_screen_targets(
                local_screens=available_screens(),
                screen_identifier=screen_identifier,
                screen_display_name=screen_display_name,
                include_offline=include_offline,
            )
        targets: list[UnifiedScreenTarget] = []
        static_media_ids = self.static_media_configured_screen_ids()
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
        for screen in sorted(
            self.browser_configured_screens(), key=lambda item: item.name.lower()
        ):
            if screen.id not in static_media_ids:
                continue
            online, detail, warning = browser_target_status_detail(
                screen, self.remote_screens
            )
            if not include_offline and not online:
                continue
            targets.append(
                UnifiedScreenTarget(
                    id=screen.id,
                    label=screen.name,
                    kind="remote",
                    online=online,
                    detail=detail,
                    warning=warning,
                )
            )
        return targets

    def build_remote_server_commands(self) -> dict[str, dict[str, Any]]:
        commands: dict[str, dict[str, Any]] = {}
        command_snapshots = self.coordinator.command_snapshots()
        prepared_commands: dict[str, dict[str, Any]] = {}
        for screen_id, command in command_snapshots.items():
            media_path = (
                Path(str(command.get("path") or "")) if command.get("path") else None
            )
            media_url = ""
            if media_path is not None:
                relative_path = str(command.get("relative_path") or "")
                if (
                    relative_path
                    and self.video_directory is not None
                    and media_path.exists()
                ):
                    media_url = f"/media/{quote(relative_path, safe='/')}"
                elif media_path.exists():
                    media_url = f"/api/media-file?path={quote(str(media_path))}"
            cycle_paths = [
                Path(str(item)) for item in command.get("paths") or [] if str(item)
            ]
            cycle_relative_paths = [
                str(item) for item in command.get("relative_paths") or []
            ]
            cycle_media_urls: list[str] = []
            for index, cycle_path in enumerate(cycle_paths):
                relative_value = (
                    cycle_relative_paths[index]
                    if index < len(cycle_relative_paths)
                    else ""
                )
                if (
                    relative_value
                    and self.video_directory is not None
                    and cycle_path.exists()
                ):
                    cycle_media_urls.append(f"/media/{quote(relative_value, safe='/')}")
                elif cycle_path.exists():
                    cycle_media_urls.append(
                        f"/api/media-file?path={quote(str(cycle_path))}"
                    )
            prepared_commands[screen_id] = {
                "protocolVersion": int(
                    command.get("protocol_version") or COMMAND_PROTOCOL_VERSION
                ),
                "screenId": screen_id,
                "version": int(command.get("version") or 0),
                "type": str(command.get("type") or "clear"),
                "mode": str(command.get("mode") or "idle"),
                "entryId": command.get("entry_id"),
                "message": str(command.get("message") or ""),
                "label": str(command.get("label") or ""),
                "path": str(command.get("path") or ""),
                "paths": [str(item) for item in cycle_paths],
                "relativePath": str(command.get("relative_path") or ""),
                "relativePaths": cycle_relative_paths,
                "mediaKind": str(command.get("media_kind") or ""),
                "mediaUrl": media_url,
                "mediaUrls": cycle_media_urls,
                "cycle": bool(command.get("cycle")),
                "cycleIntervalSeconds": int(
                    command.get("cycle_interval_seconds") or 10
                ),
                "playAtMs": int(command.get("play_at_ms") or 0),
                "transition": str(command.get("transition") or "fade_black"),
                "paused": bool(command.get("paused")),
            }
        commands.update(
            resolve_browser_commands(
                self.configured_screens, self.remote_screens, prepared_commands
            )
        )
        for screen_id, command in prepared_commands.items():
            if screen_id in self.browser_configured_screen_ids():
                continue
            commands[screen_id] = command
        return commands

    def sync_remote_server_commands(self) -> None:
        self.remote_server.set_media_root(self.video_directory)
        self.remote_server.set_commands(self.build_remote_server_commands())

    def desired_local_worker_screen_ids(self) -> list[str]:
        return sorted(
            screen_id
            for screen_id in self.coordinator.all_active_screen_ids()
            if (
                not is_remote_screen_id(screen_id)
                and screen_id not in self.browser_configured_screen_ids()
            )
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
            append_engine_startup_log(
                f"Failed to launch local playback worker for {screen_id}: {error}"
            )
            return
        self.local_playback_workers[screen_id] = process
        append_engine_startup_log(
            f"Local playback worker started for {screen_id}; worker_count={len(self.local_playback_workers)}"
        )

    def stop_local_playback_worker(self, screen_id: str) -> None:
        process = self.local_playback_workers.pop(screen_id, None)
        if process is None:
            return
        if process.poll() is not None:
            append_engine_startup_log(
                f"Local playback worker removed for {screen_id}; worker_count={len(self.local_playback_workers)}"
            )
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
        append_engine_startup_log(
            f"Local playback worker stopped for {screen_id}; worker_count={len(self.local_playback_workers)}"
        )

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
        prepared_commands = self.build_remote_server_commands()
        self.remote_server.set_media_root(self.video_directory)
        self.remote_server.set_commands(prepared_commands)
        self.sync_local_playback_workers()
        self.notify_state_changed("runtime")

    def available_media_payload(self) -> list[dict[str, Any]]:
        return build_media_library_index_from_paths(
            self.video_directory,
            [path for path in self.available_videos if path.exists()],
        ).payload()

    def current_status_payload(self) -> dict[str, Any]:
        now = current_uk_datetime()
        weekday_index = now.weekday()
        minute_of_day = (now.hour * 60) + now.minute
        active_entry = next(
            (
                entry
                for screen_id in self.selected_monitor_ids
                if (
                    entry := active_schedule_for_screen(
                        self.schedules,
                        weekday_index,
                        minute_of_day,
                        screen_id,
                        self.all_screen_groups(),
                    )
                )
                is not None
            ),
            active_schedule_for_minute(self.schedules, weekday_index, minute_of_day),
        )
        if (
            not self.coordinator.playback_enabled
            and not self.coordinator.quick_play_paths
        ):
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

    def snapshot_for_channels(
        self, client_channel_versions: dict[str, int] | None
    ) -> dict[str, Any]:
        current_versions = self.channel_versions_snapshot()
        requested_versions = client_channel_versions or {}
        changed_channels = {
            name
            for name in STATE_CHANNELS
            if int(requested_versions.get(name) or 0)
            < int(current_versions.get(name) or 0)
        }
        if not client_channel_versions:
            changed_channels = set(STATE_CHANNELS)

        remote_active = sum(
            1
            for state in self.remote_screens.values()
            if state.get("online")
            and str(state.get("state") or "") in {"playing", "connected"}
            and str(state.get("current_media") or "")
        )
        snapshot: dict[str, Any] = {
            "stateVersion": self._state_version,
            "channelVersions": current_versions,
            "channels": sorted(changed_channels),
        }

        if "config" in changed_channels:
            snapshot.update(
                {
                    "configuredScreens": [
                        screen.to_dict() for screen in self.configured_screens
                    ],
                    "screenGroups": [group.to_dict() for group in self.screen_groups],
                    "selectedMonitorIds": self.selected_monitor_ids,
                    "enabledScreenIds": self.enabled_screen_ids,
                    "videoDirectory": str(self.video_directory)
                    if self.video_directory is not None
                    else "",
                    "screenAliases": self.screen_aliases,
                    "transitionMethod": self.transition_method,
                    "runAtStartup": self.run_at_startup,
                    "lanPairingRequired": self.lan_pairing_required,
                    "lanAllowUnpairedClients": self.lan_allow_unpaired_clients,
                    "lanPairedClientIds": self.lan_paired_client_ids,
                    "schedules": [entry.to_dict() for entry in self.schedules],
                }
            )

        if "library" in changed_channels:
            snapshot.update(
                {
                    "availableMedia": self.available_media_payload(),
                    "library": {
                        "scanning": self.media_scan_in_progress,
                        "error": self.media_scan_error,
                        "indexedCount": len(self.available_videos),
                    },
                }
            )

        if "runtime" in changed_channels:
            snapshot.update(
                {
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
            )

        snapshot["performance"] = self.performance_metrics_payload()
        self.last_snapshot_generated_at = datetime.now().isoformat(timespec="seconds")
        snapshot["performance"] = self.performance_metrics_payload()
        try:
            self.last_snapshot_size_bytes = len(
                json.dumps(snapshot, separators=(",", ":")).encode("utf-8")
            )
        except Exception:
            self.last_snapshot_size_bytes = 0
        snapshot["performance"] = self.performance_metrics_payload()
        return snapshot

    def snapshot(self) -> dict[str, Any]:
        return self.snapshot_for_channels(None)

    def handle_action(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        extra: dict[str, Any] = {}
        changed_channels: set[str] = {"runtime"}
        if action == "toggle_screen_selection":
            screen_id = str(payload.get("screen_id") or "")
            if screen_id:
                if screen_id in self.selected_monitor_ids:
                    self.selected_monitor_ids.remove(screen_id)
                else:
                    self.selected_monitor_ids.append(screen_id)
                self.coordinator.set_selected_monitors(self.selected_monitor_ids)
                self.coordinator.set_screen_groups(self.all_screen_groups())
                self.persist_state()
                changed_channels.add("config")
        elif action == "set_selected_screens":
            raw_ids = payload.get("screen_ids") or []
            self.selected_monitor_ids = [
                str(item) for item in raw_ids if isinstance(item, str)
            ]
            self.coordinator.set_selected_monitors(self.selected_monitor_ids)
            self.coordinator.set_screen_groups(self.all_screen_groups())
            self.persist_state()
            changed_channels.add("config")
        elif action == "rename_screen_alias":
            screen_id = str(payload.get("screen_id") or "")
            alias = str(payload.get("alias") or "").strip()
            if alias:
                self.screen_aliases[screen_id] = alias
            else:
                self.screen_aliases.pop(screen_id, None)
            self.persist_state()
            changed_channels.add("config")
        elif action == "set_enabled_screens":
            raw_ids = payload.get("screen_ids") or []
            self.enabled_screen_ids = [
                str(item) for item in raw_ids if isinstance(item, str)
            ]
            self.persist_state()
            changed_channels.add("config")
        elif action == "purge_remembered_screens":
            include_online = bool(payload.get("include_online"))
            removed_ids = self.remote_server.purge_remote_screens(
                include_online=include_online
            )
            if removed_ids:
                removed_set = set(removed_ids)
                self.selected_monitor_ids = [
                    item
                    for item in self.selected_monitor_ids
                    if item not in removed_set
                ]
                for entry in self.schedules:
                    filtered_targets = [
                        target_id
                        for target_id in entry.screen_ids
                        if target_id not in removed_set
                    ]
                    entry.screen_ids = normalize_schedule_target_ids(
                        filtered_targets
                    ) or [DEFAULT_GROUP_ID]
                    entry.media_assignments = {
                        screen_id: assignment
                        for screen_id, assignment in entry.media_assignments.items()
                        if screen_id not in removed_set
                    }
                for group in self.screen_groups:
                    group.screen_ids = [
                        screen_id
                        for screen_id in group.screen_ids
                        if screen_id not in removed_set
                    ]
                self.enabled_screen_ids = [
                    item for item in self.enabled_screen_ids if item not in removed_set
                ]
                for screen_id in removed_ids:
                    self.screen_aliases.pop(screen_id, None)
                self.coordinator.set_selected_monitors(self.selected_monitor_ids)
                self.coordinator.set_screen_groups(self.all_screen_groups())
                self.coordinator.set_schedules(self.schedules)
                self.persist_state()
                changed_channels.add("config")
            extra["removed"] = len(removed_ids)
        elif action == "set_transition_method":
            transition_method = str(payload.get("transition_method") or "")
            if transition_method in TRANSITION_METHODS:
                self.transition_method = transition_method
                self.coordinator.set_transition_method(self.transition_method)
                self.sync_playback_outputs()
                self.persist_state()
                changed_channels.add("config")
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
            changed_channels.update({"config", "library"})
        elif action == "refresh_media":
            self.refresh_video_library()
            changed_channels.add("library")
        elif action == "launch_selected":
            self.coordinator.set_schedules(self.schedules)
            self.coordinator.set_selected_monitors(self.selected_monitor_ids)
            if not self.coordinator.has_launch_targets():
                raise ValueError(
                    "Choose at least one screen in the default playback group or target a screen/group within a schedule before launching playback."
                )
            self.coordinator.launch_windows()
            self.sync_playback_outputs()
        elif action == "toggle_pause":
            self.coordinator.toggle_pause()
        elif action == "stop_screens":
            self.coordinator.stop_windows()
            self.sync_playback_outputs()
        elif action == "retry_firewall_setup":
            self.ensure_firewall_access(force_retry=True)
        elif action == "export_diagnostics":
            requested_path = str(payload.get("path") or "").strip()
            destination = Path(requested_path).expanduser() if requested_path else None
            exported = export_engine_diagnostics_bundle(
                destination=destination,
                snapshot=self.snapshot_for_channels(None),
            )
            extra["exportPath"] = str(exported)
        elif action == "set_run_at_startup":
            self.run_at_startup = bool(payload.get("value"))
            self.sync_startup_registration()
            self.persist_state()
            changed_channels.add("config")
        elif action == "set_configured_screens":
            self.configured_screens = validate_configured_screens(
                payload.get("configured_screens")
            )
            validate_unique_browser_bindings(self.configured_screens)
            self.screen_aliases.update(
                configured_screen_name_map(self.configured_screens)
            )
            self.coordinator.set_virtual_screen_ids(
                self.browser_configured_screen_ids()
            )
            self.persist_state()
            changed_channels.add("config")
        elif action == "merge_duplicate_screen":
            source_id = str(payload.get("source_screen_id") or "").strip()
            target_id = str(payload.get("target_screen_id") or "").strip()
            if not source_id or not target_id:
                raise ValueError("Provide both source and target screen ids.")
            if source_id == target_id:
                raise ValueError("Source and target screen ids must be different.")
            if not source_id.startswith("remote:") or not target_id.startswith(
                "remote:"
            ):
                raise ValueError("Only remembered remote screens can be merged.")

            merged = self.remote_server.merge_remote_screens(source_id, target_id)
            if not merged:
                raise ValueError("Unable to merge screens. Verify both ids exist.")

            def remap_ids(values: list[str]) -> list[str]:
                remapped: list[str] = []
                for value in values:
                    remapped.append(target_id if value == source_id else value)
                return remapped

            self.selected_monitor_ids = list(
                dict.fromkeys(remap_ids(self.selected_monitor_ids))
            )
            self.enabled_screen_ids = list(
                dict.fromkeys(remap_ids(self.enabled_screen_ids))
            )

            for entry in self.schedules:
                entry.screen_ids = normalize_schedule_target_ids(
                    remap_ids(entry.screen_ids)
                ) or [DEFAULT_GROUP_ID]
                if source_id in entry.media_assignments:
                    if target_id not in entry.media_assignments:
                        assignment = entry.media_assignments[source_id]
                        assignment.screen_id = target_id
                        entry.media_assignments[target_id] = assignment
                    entry.media_assignments.pop(source_id, None)

            for group in self.screen_groups:
                group.screen_ids = list(dict.fromkeys(remap_ids(group.screen_ids)))

            source_alias = self.screen_aliases.get(source_id, "")
            target_alias = self.screen_aliases.get(target_id, "")
            if source_alias and not target_alias:
                self.screen_aliases[target_id] = source_alias
            self.screen_aliases.pop(source_id, None)

            remapped_configured: list[ConfiguredScreen] = []
            seen_configured_ids: set[str] = set()
            for screen in self.configured_screens:
                if screen.id == source_id:
                    replacement = ConfiguredScreen(
                        id=target_id,
                        name=screen.name,
                        transport=screen.transport,
                        capabilities=screen.capabilities[:],
                        binding=dict(screen.binding),
                    )
                else:
                    replacement = screen
                if replacement.id in seen_configured_ids:
                    continue
                seen_configured_ids.add(replacement.id)
                remapped_configured.append(replacement)
            self.configured_screens = remapped_configured

            self.coordinator.set_selected_monitors(self.selected_monitor_ids)
            self.coordinator.set_screen_groups(self.all_screen_groups())
            self.coordinator.set_schedules(self.schedules)
            self.coordinator.set_virtual_screen_ids(
                self.browser_configured_screen_ids()
            )
            self.persist_state()
            changed_channels.add("config")
            extra["merged"] = True
        elif action == "set_screen_groups":
            self.screen_groups = validate_screen_groups(payload.get("screen_groups"))
            self.coordinator.set_screen_groups(self.all_screen_groups())
            self.persist_state()
            changed_channels.add("config")
        elif action == "save_schedule":
            changed_channels.add("config")
            raw_schedule = payload.get("schedule")
            if not isinstance(raw_schedule, dict):
                raise ValueError("Invalid schedule payload.")
            candidate = ScheduleEntry.from_dict(raw_schedule)
            if self.video_directory is None:
                raise ValueError("Set the media folder first from the Library menu.")
            candidate_path = (
                self.video_directory / candidate.video_file
                if candidate.video_file
                else None
            )
            if candidate_path is None or not candidate_path.exists():
                raise ValueError("Choose a media file from the selected folder.")
            validate_schedule_candidate(
                candidate,
                self.schedules,
                candidate.id
                if any(item.id == candidate.id for item in self.schedules)
                else None,
                self.video_directory,
            )
            replaced = False
            for index, entry in enumerate(self.schedules):
                if entry.id == candidate.id:
                    self.schedules[index] = candidate
                    replaced = True
                    break
            if not replaced:
                self.schedules.append(candidate)
            self.schedules.sort(
                key=lambda item: ScheduleEntry.time_to_minutes(item.start_time)
            )
            self.persist_state()
            self.sync_startup_registration()
            self.coordinator.set_schedules(self.schedules)
        elif action == "delete_schedule":
            changed_channels.add("config")
            schedule_id = str(payload.get("schedule_id") or "")
            self.schedules = [item for item in self.schedules if item.id != schedule_id]
            self.persist_state()
            self.sync_startup_registration()
            self.coordinator.set_schedules(self.schedules)
        elif action == "shutdown_engine":
            QTimer.singleShot(0, self.shutdown)
        else:
            raise ValueError(f"Unsupported action: {action}")
        self.notify_state_changed(*sorted(changed_channels))
        return {"ok": True, "snapshot": self.snapshot_for_channels(None), **extra}

    def setup_engine_tray_icon(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.engine_tray_icon = None
            return
        self.engine_tray_icon = QSystemTrayIcon(
            QApplication.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        )
        tray_menu = QMenu()
        open_controller_action = tray_menu.addAction("Open Controller")
        open_controller_action.triggered.connect(self.launch_controller_window)
        tray_menu.addSeparator()
        stop_action = tray_menu.addAction("Stop Engine")
        stop_action.triggered.connect(self.shutdown)
        tray_menu.addSeparator()
        exit_action = tray_menu.addAction("Exit Engine App")
        exit_action.triggered.connect(
            lambda *_args: (
                QApplication.instance().quit()
                if QApplication.instance() is not None
                else None
            )
        )
        self.engine_tray_icon.setContextMenu(tray_menu)
        self.engine_tray_icon.activated.connect(
            lambda reason: (
                self.launch_controller_window()
                if reason == QSystemTrayIcon.ActivationReason.Trigger
                else None
            )
        )
        self.engine_tray_icon.setToolTip("Background Screen Engine")
        self.engine_tray_icon.show()

    def launch_controller_window(self) -> None:
        try:
            subprocess.Popen(
                controller_launch_args(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **windows_hidden_subprocess_kwargs(),
            )
        except OSError as error:
            append_engine_startup_log(f"Failed to launch controller window: {error}")

    def shutdown(self) -> None:
        self.folder_refresh_timer.stop()
        self.remote_refresh_timer.stop()
        self.firewall_check_timer.stop()
        watched_paths = self.folder_watcher.directories()
        if watched_paths:
            self.folder_watcher.removePaths(watched_paths)
        self.control_server.stop()
        self.remote_server.stop()
        if self.engine_tray_icon is not None:
            self.engine_tray_icon.hide()
            self.engine_tray_icon = None
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
        append_engine_startup_log(
            f"Local playback worker failed for {screen_id}: {error}"
        )
        return 1
    return app.exec()


def ensure_backend_running(client: ControllerApiClient) -> None:
    if client.ping():
        return
    remove_stale_engine_lock()
    existing_engine_pids = [
        pid for pid in known_engine_process_ids() if is_process_running(pid)
    ]
    if existing_engine_pids:
        status = read_engine_startup_status()
        status_message = str(status.get("message") or "").strip()
        stop_known_engine_processes()
        if client.ping():
            return
        if status_message:
            append_engine_startup_log(
                f"Restarting unresponsive engine instance(s): {existing_engine_pids}. Last status: {status_message}"
            )
        else:
            append_engine_startup_log(
                f"Restarting unresponsive engine instance(s): {existing_engine_pids}"
            )
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
        status_pid = (
            int(status.get("pid") or 0) if str(status.get("pid") or "").strip() else 0
        )
        if str(status.get("state") or "").strip().lower() == "failed":
            detail_suffix = (
                f" See {ENGINE_STARTUP_LOG_PATH} for details."
                if ENGINE_STARTUP_LOG_PATH.exists()
                else ""
            )
            raise RuntimeError(
                status_message
                or f"The background engine failed during startup.{detail_suffix}"
            )
        if status_pid > 0 and not is_process_running(status_pid):
            if status_message:
                extra = f"\n\n{status_details}" if status_details else ""
                raise RuntimeError(f"{status_message}{extra}")
            log_suffix = (
                f" See {ENGINE_STARTUP_LOG_PATH} for details."
                if ENGINE_STARTUP_LOG_PATH.exists()
                else ""
            )
            raise RuntimeError(
                f"The background engine exited during startup.{log_suffix}"
            )
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
    decode_mode = (
        "software-forced"
        if os.getenv("QT_FFMPEG_DECODING_HW_DEVICE_TYPES", "").strip() == ","
        else "qt-default"
    )
    append_engine_startup_log(f"Engine launch decode mode: {decode_mode}")
    cleanup_legacy_startup_script()
    write_engine_startup_status(
        "starting", "Launching background engine.", pid=os.getpid()
    )
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
        message = (
            "A stale engine lock blocked startup."
            if stale_lock_removed
            else "Unable to acquire the engine instance lock."
        )
        write_engine_startup_status("failed", message, pid=os.getpid())
        append_engine_startup_log(message)
        raise RuntimeError(message)
    try:
        app = QApplication(sys.argv)
        app.setApplicationName(f"{APP_NAME} Engine")
        app.setQuitOnLastWindowClosed(False)
        _engine = ControllerEngine()
        write_engine_startup_status(
            "ready", "Background engine ready.", pid=os.getpid()
        )
        return app.exec()
    except Exception as error:  # noqa: BLE001
        details = traceback.format_exc()
        write_engine_startup_status(
            "failed", str(error) or "Engine startup failed.", details, pid=os.getpid()
        )
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
