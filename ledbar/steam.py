"""Steam integration: find libraries, parse app manifests, detect downloads.

Steam keeps one ``appmanifest_<appid>.acf`` file per installed app inside
every library's ``steamapps`` folder and rewrites it while a download or
update is in progress.  The ``StateFlags`` bit mask and the ``Bytes*``
counters in that file are enough to build a progress bar without talking to
the Steam client at all, which keeps this tool independent of Steam client
internals.

Nothing in here needs third party packages.
"""

from __future__ import annotations

import glob
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

# --------------------------------------------------------------------------
# Steam EAppState bit mask (as written to appmanifest StateFlags)
# --------------------------------------------------------------------------

STATE_UNINSTALLED = 1
STATE_UPDATE_REQUIRED = 2
STATE_FULLY_INSTALLED = 4
STATE_ENCRYPTED = 8
STATE_LOCKED = 16
STATE_FILES_MISSING = 32
STATE_APP_RUNNING = 64
STATE_FILES_CORRUPT = 128
STATE_UPDATE_RUNNING = 256
STATE_UPDATE_PAUSED = 512
STATE_UPDATE_STARTED = 1024
STATE_UNINSTALLING = 2048
STATE_BACKUP_RUNNING = 4096
STATE_RECONFIGURING = 65536
STATE_VALIDATING = 131072
STATE_ADDING_FILES = 262144
STATE_PREALLOCATING = 524288
STATE_DOWNLOADING = 1048576
STATE_STAGING = 2097152
STATE_COMMITTING = 4194304
STATE_UPDATE_STOPPING = 8388608

STATE_NAMES = {
    STATE_UNINSTALLED: "Uninstalled",
    STATE_UPDATE_REQUIRED: "Update Required",
    STATE_FULLY_INSTALLED: "Fully Installed",
    STATE_ENCRYPTED: "Encrypted",
    STATE_LOCKED: "Locked",
    STATE_FILES_MISSING: "Files Missing",
    STATE_APP_RUNNING: "App Running",
    STATE_FILES_CORRUPT: "Files Corrupt",
    STATE_UPDATE_RUNNING: "Update Running",
    STATE_UPDATE_PAUSED: "Update Paused",
    STATE_UPDATE_STARTED: "Update Started",
    STATE_UNINSTALLING: "Uninstalling",
    STATE_BACKUP_RUNNING: "Backup Running",
    STATE_RECONFIGURING: "Reconfiguring",
    STATE_VALIDATING: "Validating",
    STATE_ADDING_FILES: "Adding Files",
    STATE_PREALLOCATING: "Preallocating",
    STATE_DOWNLOADING: "Downloading",
    STATE_STAGING: "Staging",
    STATE_COMMITTING: "Committing",
    STATE_UPDATE_STOPPING: "Update Stopping",
}

# Any of these bits means Steam is actively working on the app right now.
ACTIVE_MASK = (
    STATE_UPDATE_RUNNING
    | STATE_RECONFIGURING
    | STATE_VALIDATING
    | STATE_ADDING_FILES
    | STATE_PREALLOCATING
    | STATE_DOWNLOADING
    | STATE_STAGING
    | STATE_COMMITTING
)


def describe_flags(flags: int) -> str:
    names = [name for bit, name in STATE_NAMES.items() if flags & bit]
    return ", ".join(names) if names else "None"


# --------------------------------------------------------------------------
# Minimal VDF / ACF parser
# --------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"""
    \s*(?:
        //[^\n]*            # comment
      | "((?:[^"\\]|\\.)*)" # quoted token
      | ([{}])              # brace
      | ([^\s"{}]+)         # bare token
    )
    """,
    re.VERBOSE,
)


def parse_vdf(text: str) -> dict[str, Any]:
    """Parse Valve's KeyValues text format into nested dicts.

    Values are always strings; nested blocks become dicts.  The parser is
    deliberately forgiving: unknown tokens are skipped and unbalanced braces
    do not raise.  Steam occasionally writes a manifest while we read it, so
    a half-written file must not crash the daemon.
    """
    tokens: list[str | None] = []
    pos = 0
    length = len(text)
    while pos < length:
        match = _TOKEN_RE.match(text, pos)
        if not match or match.end() == pos:
            pos += 1
            continue
        pos = match.end()
        quoted, brace, bare = match.group(1), match.group(2), match.group(3)
        if quoted is not None:
            tokens.append(_unescape(quoted))
        elif brace is not None:
            tokens.append(brace)
        elif bare is not None:
            tokens.append(bare)

    root: dict[str, Any] = {}
    stack: list[dict[str, Any]] = [root]
    pending_key: Optional[str] = None
    for token in tokens:
        current = stack[-1]
        if token == "{":
            if pending_key is None:
                continue
            child: dict[str, Any] = {}
            current[pending_key] = child
            stack.append(child)
            pending_key = None
        elif token == "}":
            if len(stack) > 1:
                stack.pop()
            pending_key = None
        elif pending_key is None:
            pending_key = token
        else:
            current[pending_key] = token
            pending_key = None
    return root


def _unescape(value: str) -> str:
    return value.replace('\\"', '"').replace("\\\\", "\\").replace("\\n", "\n").replace("\\t", "\t")


def get_ci(mapping: dict[str, Any], key: str, default: Any = None) -> Any:
    """Case-insensitive dictionary lookup (Steam is not consistent with case)."""
    if key in mapping:
        return mapping[key]
    lowered = key.lower()
    for existing, value in mapping.items():
        if existing.lower() == lowered:
            return value
    return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# Library discovery
# --------------------------------------------------------------------------

DEFAULT_STEAM_ROOTS = (
    "~/.local/share/Steam",
    "~/.steam/steam",
    "~/.steam/root",
    "~/.steam/debian-installation",
    "~/.var/app/com.valvesoftware.Steam/.local/share/Steam",
    "~/.var/app/com.valvesoftware.Steam/.steam/steam",
    "~/snap/steam/common/.local/share/Steam",
)


def find_steam_roots(extra: Iterable[str] = ()) -> list[Path]:
    """Return every Steam installation root that has a ``steamapps`` folder."""
    seen: set[str] = set()
    roots: list[Path] = []
    for candidate in list(extra) + list(DEFAULT_STEAM_ROOTS):
        path = Path(os.path.expanduser(os.path.expandvars(candidate)))
        try:
            real = str(path.resolve())
        except OSError:
            continue
        if real in seen:
            continue
        seen.add(real)
        if (path / "steamapps").is_dir():
            roots.append(path)
    return roots


def library_steamapps_dirs(roots: Iterable[Path]) -> list[Path]:
    """Expand Steam roots into every ``steamapps`` folder they reference.

    Steam lists additional library folders (second drive, SD card, ...) in
    ``steamapps/libraryfolders.vdf``.  Both the current format (``"0" {
    "path" "..." }``) and the old one (``"1" "/path"``) are handled.
    """
    seen: set[str] = set()
    result: list[Path] = []

    def add(path: Path) -> None:
        if not path.is_dir():
            return
        try:
            key = str(path.resolve())
        except OSError:
            return
        if key not in seen:
            seen.add(key)
            result.append(path)

    for root in roots:
        add(root / "steamapps")
        vdf_path = root / "steamapps" / "libraryfolders.vdf"
        if not vdf_path.is_file():
            continue
        try:
            data = parse_vdf(vdf_path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        folders = get_ci(data, "libraryfolders", {}) or get_ci(data, "LibraryFolders", {}) or {}
        if not isinstance(folders, dict):
            continue
        for key, value in folders.items():
            if not key.strip().isdigit():
                continue
            if isinstance(value, dict):
                path_value = get_ci(value, "path")
            else:
                path_value = value
            if isinstance(path_value, str) and path_value:
                add(Path(path_value.replace("\\\\", "/")) / "steamapps")
    return result


# --------------------------------------------------------------------------
# App manifests
# --------------------------------------------------------------------------


@dataclass
class AppStatus:
    """The interesting bits of one ``appmanifest_<appid>.acf`` file."""

    appid: int
    name: str
    flags: int
    bytes_to_download: int
    bytes_downloaded: int
    bytes_to_stage: int
    bytes_staged: int
    mtime: float
    path: Path
    downloading_dir: bool = False

    @property
    def is_paused(self) -> bool:
        return bool(self.flags & STATE_UPDATE_PAUSED)

    @property
    def is_active(self) -> bool:
        return bool(self.flags & ACTIVE_MASK)

    @property
    def is_starting(self) -> bool:
        """Steam has started an update but is not reporting a working phase yet.

        A fresh install sits at StateFlags 1026 (Update Required + Update
        Started) with the byte counters still at zero, sometimes for minutes
        while it preallocates.  The same bit is set for a merely *queued*
        download, so callers must corroborate this (see ``SteamMonitor``).
        """
        return bool(self.flags & STATE_UPDATE_STARTED) and not self.is_active

    @property
    def is_running(self) -> bool:
        return bool(self.flags & STATE_APP_RUNNING)

    @property
    def phase(self) -> str:
        if self.flags & STATE_DOWNLOADING:
            return "downloading"
        if self.flags & STATE_STAGING:
            return "installing"
        if self.flags & STATE_COMMITTING:
            return "committing"
        if self.flags & STATE_PREALLOCATING:
            return "preallocating"
        if self.flags & STATE_VALIDATING:
            return "validating"
        if self.flags & STATE_ADDING_FILES:
            return "adding files"
        if self.flags & STATE_RECONFIGURING:
            return "reconfiguring"
        if self.flags & STATE_UPDATE_RUNNING:
            return "updating"
        if self.flags & STATE_UPDATE_PAUSED:
            return "paused"
        if self.flags & STATE_UPDATE_STARTED:
            return "starting"
        return "idle"

    @property
    def fraction(self) -> Optional[float]:
        """Progress 0..1, or ``None`` when Steam gives us nothing to go on."""
        if self.is_starting and self.bytes_downloaded == 0 and self.bytes_staged == 0:
            # started, but no progress reported yet: better an indeterminate
            # animation than a bar pinned at 0%
            return None
        if self.flags & STATE_STAGING and self.bytes_to_stage > 0:
            return _ratio(self.bytes_staged, self.bytes_to_stage)
        if self.flags & STATE_COMMITTING:
            return 1.0
        if self.bytes_to_download > 0:
            return _ratio(self.bytes_downloaded, self.bytes_to_download)
        if self.bytes_to_stage > 0:
            return _ratio(self.bytes_staged, self.bytes_to_stage)
        return None


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    value = numerator / denominator
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


def parse_app_manifest(path: Path, text: Optional[str] = None) -> Optional[AppStatus]:
    """Parse one manifest.  Returns ``None`` if the file is unreadable/garbage."""
    try:
        if text is None:
            text = path.read_text(encoding="utf-8", errors="replace")
        stat = path.stat()
        mtime = stat.st_mtime
    except OSError:
        return None
    data = parse_vdf(text)
    state = get_ci(data, "AppState")
    if not isinstance(state, dict):
        return None
    appid = _to_int(get_ci(state, "appid"), -1)
    if appid < 0:
        match = re.search(r"appmanifest_(\d+)\.acf$", path.name)
        appid = int(match.group(1)) if match else 0
    name = str(get_ci(state, "name") or get_ci(state, "installdir") or f"app {appid}")
    downloading_dir = (path.parent / "downloading" / str(appid)).is_dir()
    return AppStatus(
        appid=appid,
        name=name,
        flags=_to_int(get_ci(state, "StateFlags")),
        bytes_to_download=_to_int(get_ci(state, "BytesToDownload")),
        bytes_downloaded=_to_int(get_ci(state, "BytesDownloaded")),
        bytes_to_stage=_to_int(get_ci(state, "BytesToStage")),
        bytes_staged=_to_int(get_ci(state, "BytesStaged")),
        mtime=mtime,
        path=path,
        downloading_dir=downloading_dir,
    )


# --------------------------------------------------------------------------
# Process detection (Linux /proc)
# --------------------------------------------------------------------------

_STEAM_PROCESS_NAMES = {"steam", "steamwebhelper", "steam.sh"}
_LAUNCH_RE = re.compile(rb"SteamLaunch\s+AppId=(\d+)")


def steam_is_running(proc_root: str = "/proc") -> bool:
    """True if a Steam client process exists (checked through ``/proc``)."""
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return False
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(os.path.join(proc_root, entry, "comm"), "rb") as handle:
                comm = handle.read(64).strip().decode(errors="replace")
        except OSError:
            continue
        if comm in _STEAM_PROCESS_NAMES:
            return True
    return False


def running_game_appid(proc_root: str = "/proc") -> Optional[int]:
    """Return the AppId of a game Steam launched, if one is running.

    Steam starts every game through its ``reaper`` helper with a command
    line containing ``SteamLaunch AppId=<id>``, which is easy to spot.
    """
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return None
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(os.path.join(proc_root, entry, "cmdline"), "rb") as handle:
                cmdline = handle.read(4096)
        except OSError:
            continue
        match = _LAUNCH_RE.search(cmdline)
        if match:
            return int(match.group(1))
    return None


# --------------------------------------------------------------------------
# Download monitor
# --------------------------------------------------------------------------


@dataclass
class DownloadState:
    """What the LED bar needs to know about Steam right now."""

    active: bool = False
    fraction: Optional[float] = None
    phase: str = "idle"
    app: Optional[AppStatus] = None
    paused: bool = False
    steam_running: bool = False
    running_appid: Optional[int] = None
    checked_at: float = 0.0
    libraries: list[Path] = field(default_factory=list)
    source: str = "manifest"          # "manifest" or "cef" (the Steam client)


class SteamMonitor:
    """Polls Steam's manifests and reports the current download, if any.

    ``poll()`` is cheap: manifests are only re-parsed when their size or
    modification time changes, and the process table is only scanned every
    couple of seconds.
    """

    def __init__(
        self,
        extra_roots: Iterable[str] = (),
        stale_seconds: float = 600.0,
        show_paused: bool = False,
        rescan_interval: float = 30.0,
        process_interval: float = 2.0,
        require_steam_process: bool = True,
        proc_root: str = "/proc",
        source: str = "manifest",
        cef_host: str = "127.0.0.1",
        cef_port: int = 8080,
    ) -> None:
        self.extra_roots = list(extra_roots)
        self.stale_seconds = stale_seconds
        self.show_paused = show_paused
        self.rescan_interval = rescan_interval
        self.process_interval = process_interval
        self.require_steam_process = require_steam_process
        self.proc_root = proc_root
        self.roots: list[Path] = []
        self.libraries: list[Path] = []
        self._cache: dict[str, tuple[float, int, Optional[AppStatus]]] = {}
        self._last_rescan = 0.0
        self._last_process_check = 0.0
        self._steam_running = False
        self._running_appid: Optional[int] = None
        self.state = DownloadState()
        self.source = source
        self.live: Any = None
        if source in ("auto", "cef"):
            from .steamcef import CefDownloadSource

            self.live = CefDownloadSource(host=cef_host, port=cef_port)

    # -- discovery ---------------------------------------------------------

    def rescan_libraries(self) -> None:
        self.roots = find_steam_roots(self.extra_roots)
        self.libraries = library_steamapps_dirs(self.roots)
        self._last_rescan = time.monotonic()

    # -- polling -----------------------------------------------------------

    def poll(self, now: Optional[float] = None) -> DownloadState:
        mono = time.monotonic()
        wall = time.time() if now is None else now
        if not self.libraries or mono - self._last_rescan > self.rescan_interval:
            self.rescan_libraries()
        if mono - self._last_process_check > self.process_interval:
            self._steam_running = steam_is_running(self.proc_root)
            self._running_appid = running_game_appid(self.proc_root) if self._steam_running else None
            self._last_process_check = mono

        statuses = list(self._read_manifests())

        state = DownloadState(
            steam_running=self._steam_running,
            running_appid=self._running_appid,
            checked_at=wall,
            libraries=list(self.libraries),
        )

        live = self.live.poll() if self.live is not None else None
        if live is not None:
            # The Steam client is authoritative: it knows about downloads whose
            # manifests have not been rewritten yet, and knows when one stops.
            state.source = "cef"
            if live.active:
                state.active = True
                state.fraction = live.fraction
                state.phase = live.state.lower() if live.state.lower() not in ("none", "") else "downloading"
                state.paused = live.paused
                state.app = self._app_for(statuses, live.appid)
            self.state = state
            return state

        if self.source == "cef":
            self.state = state          # live-only was requested but is unavailable
            return state

        state.source = "manifest"
        chosen = self.select_active(statuses, wall)
        if chosen is not None:
            state.active = True
            state.app = chosen
            state.fraction = chosen.fraction
            state.phase = chosen.phase
            state.paused = chosen.is_paused
        self.state = state
        return state

    def _app_for(self, statuses: list[AppStatus], appid: Optional[int]) -> Optional[AppStatus]:
        """Match the client's appid to a manifest, for the name; synthesise if absent."""
        if appid:
            for status in statuses:
                if status.appid == appid:
                    return status
        return AppStatus(
            appid=appid or 0, name=f"app {appid}" if appid else "download", flags=0,
            bytes_to_download=0, bytes_downloaded=0, bytes_to_stage=0, bytes_staged=0,
            mtime=0.0, path=Path(f"<steam client:{appid or 0}>"),
        )

    def _read_manifests(self) -> Iterable[AppStatus]:
        live: set[str] = set()
        for library in self.libraries:
            for file in glob.glob(os.path.join(str(library), "appmanifest_*.acf")):
                live.add(file)
                try:
                    stat = os.stat(file)
                except OSError:
                    continue
                key = (stat.st_mtime, stat.st_size)
                cached = self._cache.get(file)
                if cached is not None and (cached[0], cached[1]) == key:
                    status = cached[2]
                else:
                    status = parse_app_manifest(Path(file))
                    self._cache[file] = (stat.st_mtime, stat.st_size, status)
                if status is not None:
                    # the downloading folder can appear/disappear without the
                    # manifest changing, so refresh that bit every time.
                    status.downloading_dir = (Path(file).parent / "downloading" / str(status.appid)).is_dir()
                    yield status
        for stale in [k for k in self._cache if k not in live]:
            self._cache.pop(stale, None)

    # -- selection ---------------------------------------------------------

    def select_active(self, statuses: Iterable[AppStatus], now: float) -> Optional[AppStatus]:
        """Pick the manifest that best represents "the current download"."""
        if self.require_steam_process and not self._steam_running:
            return None
        candidates: list[AppStatus] = []
        for status in statuses:
            if status.is_paused and not self.show_paused:
                continue
            recent = (now - status.mtime) <= self.stale_seconds
            if status.is_active or (status.is_paused and self.show_paused):
                # a working phase: trust it while the manifest is fresh, or while
                # Steam still has a downloading/ folder open for it
                if not (recent or status.downloading_dir):
                    continue
            elif status.is_starting:
                # 1026 is also what a queued download looks like, so require both
                # a downloading/ folder and a manifest Steam is still touching
                if not (recent and status.downloading_dir):
                    continue
            else:
                continue
            candidates.append(status)
        if not candidates:
            return None
        # Prefer things actually moving bytes, then a real working phase, then recency.
        candidates.sort(
            key=lambda s: (bool(s.flags & (STATE_DOWNLOADING | STATE_STAGING)), s.is_active, s.mtime),
            reverse=True,
        )
        return candidates[0]
