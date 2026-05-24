# Background Screen Controller - Production Architecture Review & Remediation Plan

## Purpose

This document reviews the current architecture from a production-readiness standpoint and lays out a plan to remove architectural flaws without changing the product direction.

Scope reviewed:

- controller UI
- background engine
- local HDMI playback workers
- LAN/browser playback clients
- scheduling
- screen identity/management
- config persistence
- packaging/update considerations

---

## Executive summary

The application has evolved into a capable multi-process playback system, but much of the logic is concentrated in `main.py`. The product now needs clearer boundaries between UI, engine state, playback orchestration, LAN serving, persistence, and domain models.

The most important production risks are:

1. **Large monolithic `main.py`** makes changes risky and hard to test.
2. **State model is loosely structured** across config dicts, dataclasses, snapshots, and runtime dictionaries.
3. **Screen identity and lifecycle are still partly implicit**, especially for local HDMI displays and remembered remote screens.
4. **Controller and engine responsibilities are not cleanly separated enough**, especially around tray ownership and process lifecycle.
5. **Scheduling/playback behavior lacks a formal domain/service layer**, so UI/backend/playback code can drift.
6. **Testing is not representative of production workflows**, and current coverage goals are module-limited rather than behavior-complete.
7. **Persistence is not transactional or versioned enough** for safe migrations as features grow.
8. **LAN client protocol is ad hoc**, with no explicit versioning or compatibility contract.
9. **Error handling/logging is operationally thin**, making field diagnosis harder.
10. **The UI lacks a formal design system**, causing contrast/style regressions during feature work.
11. **Playback windows appear as taskbar items**, making scheduled display windows easy to kill accidentally from Windows taskbar.

The remediation plan below is staged to avoid risky rewrites.

---

## Current architectural shape

### Processes

```text
Controller UI process
  - MainWindow
  - dialogs
  - API client
  - local config awareness

Background engine process
  - ControllerEngine
  - EngineControlServer
  - LanRemoteServer
  - PlaybackCoordinator
  - local playback worker orchestration
  - config persistence

Local playback worker process(es)
  - LocalPlaybackWorkerController
  - PlaybackWindow

LAN browser client
  - HTML/JS served by LanRemoteServer
```

### Key file

Most production logic currently lives in:

```text
background_screen_app/main.py
```

This includes:

- domain models
- persistence
- engine control API
- LAN HTTP server
- remote browser UI HTML/JS
- local playback window
- controller UI
- dialogs
- tray behavior
- process launch behavior

This is the main architectural concern.

---

## Flaws and remediation plan

### Status legend

Each flaw now carries an implementation status:

- `[OPEN]` - not started.
- `[PARTIAL]` - useful remediation has landed, but the flaw is not fully resolved.
- `[FIXED]` - completed and covered by tests or equivalent validation.
- `[DEFERRED]` - intentionally postponed with rationale.

Status should be updated in this document whenever implementation work lands. A flaw is only `[FIXED]` when the implementation, tests, and any related documentation are complete.

### Current status snapshot

| # | Flaw | Status | Current note |
|---|---|---|---|
| 1 | Monolithic `main.py` | `[OPEN]` | Still dominant implementation file. |
| 2 | Weak domain boundaries | `[PARTIAL]` | `screen_registry.py`, `ui_styles.py`, and existing protocol/group helpers are now active facades/extractions, but `main.py` still owns too much behavior. |
| 3 | Screen identity not strong enough | `[PARTIAL]` | Browser client identity exists, but local HDMI identity remains weak. |
| 4 | Controller/engine lifecycle model | `[FIXED]` | Lifecycle contract is now documented in `LIFECYCLE_CONTRACT.md`, tested, and aligned with verified worker/taskbar behavior. |
| 5 | Config persistence lacks versioned migrations | `[PARTIAL]` | `schema_version`, atomic writes, and `migrate_config(...)` now exist; future schema-version migrations are still open. |
| 6 | Runtime command protocol is ad hoc | `[OPEN]` | Still dict/string protocol. |
| 7 | Remote HTML/JS embedded inside Python string | `[OPEN]` | Still embedded in `main.py`. |
| 8 | Scheduling/playback assignment purity | `[PARTIAL]` | Some helpers are tested, but assignment service is not extracted. |
| 9 | Testing strategy too narrow | `[PARTIAL]` | Added config, lifecycle, screen registry, and UI style tests; still missing integration/UI smoke breadth. |
| 10 | UI design system implicit | `[PARTIAL]` | Shared dialog theme helper is applied across dialogs; broader app-wide design system/catalog work is still open. |
| 11 | Logging/diagnostics weak | `[PARTIAL]` | Media-scan, snapshot-size, indexed-file-count, and worker-count metrics are now tracked, logged, exposed in engine state, and surfaced in the controller footer; broader structured diagnostics/export are still open. |
| 12 | Security/LAN exposure needs review | `[OPEN]` | Still unauthenticated LAN screen access. |
| 13 | Playback windows expose taskbar controls | `[FIXED]` | Verified on Windows: playback surfaces no longer expose taskbar controls and remain engine-controlled. |
| 14 | Build/update model basic | `[OPEN]` | No packaging/update hardening yet. |
| 15 | Media library scanning is full-tree and memory-heavy | `[OPEN]` | New performance finding. |
| 16 | State snapshots are too large and too frequent | `[OPEN]` | New performance finding. |
| 17 | Playback workers poll frequently and duplicate runtime cost | `[FIXED]` | Adaptive worker polling has been implemented and runtime-validated; deeper future optimizations like push IPC or worker consolidation remain optional follow-up improvements, not Phase 1 blockers. |
| 18 | Playback windows keep watchdog/media objects active when idle | `[FIXED]` | Verified: watchdog no longer runs for idle, image, image-cycle, or paused-video states; active video playback still behaves correctly. |
| 19 | Remote screen command/state handling copies JSON repeatedly | `[OPEN]` | New performance finding. |
| 20 | UI refresh work is broad rather than dirty-region based | `[OPEN]` | New performance finding. |

## 1) Monolithic `main.py` `[OPEN]`

### Flaw

`main.py` has too many responsibilities:

- domain data models
- config persistence
- controller UI
- engine state
- LAN server
- playback window
- remote HTML client
- process management
- tray behavior
- schedule logic

This increases risk because unrelated changes can break distant parts of the product.

### Risks

- high regression risk
- difficult testing
- difficult review
- circular assumptions between UI and engine
- harder onboarding for future developers

### Fix plan

Split into modules gradually:

```text
background_screen_app/
  app.py                    # process entrypoints
  models.py                 # ScheduleEntry, Screen, assignments
  config_store.py            # load/save/migrations
  scheduling.py              # active schedule resolution
  playback_window.py         # PlaybackWindow only
  playback_coordinator.py    # assignment + command generation
  lan_server.py              # LanRemoteServer
  engine.py                  # ControllerEngine
  engine_api.py              # EngineControlServer + client
  screen_registry.py         # screen identity/lifecycle/enabled state
  ui/
    main_window.py
    manage_screens_dialog.py
    help_center.py
    media_picker.py
    schedule_editor.py
  remote_client/
    tv_client.html
    tv_client.js
```

### Priority

High.

### Suggested sequence

1. Extract pure dataclasses and helpers first (`models.py`, `scheduling.py`).
2. Extract persistence (`config_store.py`).
3. Extract UI dialogs one by one.
4. Extract LAN server and remote client template.
5. Extract engine.

Avoid a big-bang rewrite.

---

## 2) Weak domain boundaries `[PARTIAL]`

### Flaw

Important domain concepts are represented inconsistently:

- screen IDs as raw strings
- group IDs as raw strings with `group:` prefix
- remote IDs as raw strings with `remote:` prefix
- config payloads as dictionaries
- runtime commands as dictionaries
- snapshots as dictionaries

### Risks

- accidental misuse of IDs
- UI-only assumptions leaking into engine
- validation gaps
- difficult migrations

### Fix plan

Introduce explicit domain types:

```python
@dataclass(frozen=True)
class ScreenId:
    value: str

@dataclass
class ScreenRecord:
    id: str
    display_name: str
    transport: Literal["local_hdmi", "browser"]
    online: bool
    enabled: bool
    source: Literal["detected", "remembered", "configured"]
    metadata: dict[str, Any]
```

Create a `ScreenRegistry` service responsible for:

- local screen discovery
- remote screen registry
- enabled/offline screen visibility
- aliases
- purge behavior
- stable identity matching

### Priority

High.

---

## 3) Screen identity is still not strong enough `[PARTIAL]`

### Flaw

Remote browser screens now use `clientId`, which is much better than IP matching. But identity still depends on browser local storage and does not have a pairing/recovery model.

Local HDMI screens use geometry/name-based IDs, which can change if Windows display topology changes.

### Risks

- duplicate remote screens after browser data reset
- local display IDs may change after GPU/driver/port/layout changes
- saved schedules may point to stale screen IDs

### Fix plan

#### Remote screens

Add a pairing model:

1. Remote client generates stable `clientId`.
2. Engine assigns a stable `screenId`.
3. User can bind/merge a remote client into an existing screen record.
4. Provide `Merge duplicate screen` workflow.

#### Local HDMI screens

Store a fingerprint with multiple fields:

- Qt screen name
- serial/manufacturer if available
- geometry
- DPI
- primary flag
- last seen timestamp

If exact ID changes, use fuzzy matching to suggest reconnection instead of creating unmanageable duplicates.

### Priority

High for remote merge workflow, medium for local HDMI fingerprinting.

---

## 4) Controller/engine lifecycle needs a formal process model `[FIXED]`

### Flaw

Controller and engine lifecycle behavior has changed multiple times:

- controller tray vs engine tray
- close vs minimize
- stop engine vs leave engine running

This indicates lifecycle is currently behavior-driven instead of architecture-driven.

### Risks

- user confusion
- zombie processes
- multiple tray icons
- accidental engine shutdown
- hard-to-debug production support issues

### Fix plan

Define official lifecycle contract:

| Action | Controller process | Engine process | Tray icon |
|---|---|---|---|
| Start app | starts/shows | ensure running | engine icon |
| Minimize | stays running hidden | running | engine icon |
| Close X | exits controller | running | engine icon |
| Tray Open Controller | starts controller | running | engine icon |
| Tray Stop Engine | stopped | stopped | icon removed |
| Quit Completely from controller | exits | stopped | removed |

Then centralize this in a `process_lifecycle.py` module.

### Priority

High.

---

## 5) Config persistence lacks versioned migrations `[PARTIAL]`

### Flaw

Config has evolved organically:

- schedules
- groups
- configured screens
- aliases
- enabled screens
- per-screen assignments

But there is no explicit schema version or migration pipeline.

### Risks

- older config can partially break new versions
- future fields may be silently dropped/misread
- purge/merge operations can leave inconsistent state

### Fix plan

Add:

```json
{
  "schema_version": 3,
  "configured_screens": [],
  "screen_registry": [],
  "screen_groups": [],
  "schedules": []
}
```

Implement migrations:

```python
def migrate_config(data: dict[str, Any]) -> dict[str, Any]:
    version = int(data.get("schema_version") or 1)
    if version < 2:
        data = migrate_v1_to_v2(data)
    if version < 3:
        data = migrate_v2_to_v3(data)
    return data
```

Also add atomic writes:

1. write temp file
2. fsync
3. replace target

### Priority

High.

---

## 6) Runtime command protocol is ad hoc `[OPEN]`

### Flaw

Commands sent to local workers and LAN browser clients are dictionaries without a formal versioned contract.

### Risks

- browser client may receive unsupported commands
- future changes may break older clients
- no clear compatibility strategy

### Fix plan

Create a protocol schema:

```python
COMMAND_PROTOCOL_VERSION = 2

@dataclass
class PlaybackCommand:
    protocol_version: int
    screen_id: str
    command_type: Literal["play", "clear"]
    mode: Literal["schedule", "quick_play", "idle"]
    media: list[MediaItem]
    cycle: CycleOptions | None
    transition: str
    play_at_ms: int
```

Browser client should reject/handle unknown versions gracefully.

### Priority

Medium-high.

---

## 7) Remote HTML/JS embedded inside Python string `[OPEN]`

### Flaw

The remote LAN client is a large HTML/JS string inside `main.py`.

### Risks

- hard to edit
- hard to test
- no linting
- easy to introduce syntax errors
- no asset versioning

### Fix plan

Move to:

```text
remote_client/
  tv_client.html
  tv_client.js
  tv_client.css
```

Serve static assets with cache-busting version:

```text
/tv?v=<app_version>
/static/tv_client.js?v=<app_version>
```

### Priority

Medium.

---

## 8) Scheduling logic and playback assignment should be pure/testable `[PARTIAL]`

### Flaw

Scheduling logic is partially pure, but playback assignment is inside `PlaybackCoordinator`, which also owns Qt timers/windows/signals.

### Risks

- difficult to test all schedule combinations
- UI/Qt dependencies make logic slower/harder to validate
- per-screen override rules can regress

### Fix plan

Create pure service:

```python
class PlaybackAssignmentService:
    def assignment_for_screen(...): ...
    def next_schedule_change(...): ...
```

`PlaybackCoordinator` should only:

- call assignment service
- apply commands/windows
- manage timers

### Priority

High.

---

## 9) Testing strategy is too narrow `[PARTIAL]`

### Flaw

Existing tests cover some helpers, but not enough production workflows:

- engine-controller API lifecycle
- config migrations
- screen registry behavior
- tray/process lifecycle
- LAN protocol commands
- schedule + per-screen media matrix

### Risks

- changes appear safe but break real workflows
- high dependence on manual testing

### Fix plan

Add test layers:

#### Unit tests

- models
- config migrations
- scheduling
- playback assignment
- screen registry

#### Integration tests

- engine API actions
- config load/save round trip
- command generation
- remote command resolution

#### UI smoke tests

- instantiate dialogs
- verify key widgets/actions exist
- no raw markdown help UI regressions

#### End-to-end manual checklist

- start engine
- open controller
- close controller, engine tray remains
- reopen controller from tray
- schedule HDMI/LAN
- quick play override
- purge/merge screens

### Priority

High.

---

## 10) UI design system is implicit `[PARTIAL]`

### Flaw

Styling is manually applied across widgets and dialogs. Several regressions already happened:

- Quick Play boundary accidentally removed
- Manage Screens contrast mismatch
- playback window and controller contrast drift
- raw markdown help viewer appeared

### Risks

- inconsistent UI
- accessibility issues
- regressions during unrelated feature work

### Fix plan

Create centralized style tokens:

```python
COLORS = {
    "bg": "#dfe9f3",
    "surface": "#f6fafd",
    "text": "#173043",
    "title": "#112c40",
    "accent": "#5db7f0",
    "success": "#84dcc6",
}
```

Create helper:

```python
def apply_app_styles(widget: QWidget) -> None:
    widget.setStyleSheet(APP_QSS)
```

All dialogs should use the same QSS.

Add visual review checklist before release.

### Priority

Medium-high.

---

## 11) Logging and operational diagnostics are weak `[PARTIAL]`

### Flaw

There is startup logging, but no structured event log for common support cases.

### Risks

- difficult to know why a remote screen disappeared
- hard to diagnose failed playback
- hard to support field installations

### Fix plan

Add structured app log:

```text
app_data/logs/engine.log
app_data/logs/controller.log
app_data/logs/playback-worker-<screen>.log
```

Include events:

- screen registered/refreshed/offline/purged
- schedule activated
- media missing
- quick play started/stopped
- engine lifecycle
- command sent to screen

Add Help > Diagnostics bundle export.

### Priority

Medium.

---

## 12) Security and LAN exposure need review `[OPEN]`

### Flaw

LAN server serves media and accepts remote clients without explicit pairing/auth.

### Risks

- any LAN device may register
- any LAN client may request media URLs if paths are known
- weak operator control in shared networks

### Fix plan

Introduce optional pairing code:

1. Controller displays pairing code.
2. TV client must enter code once.
3. Engine stores trusted client ID.
4. Unpaired clients appear in pending list but cannot receive playback commands.

Add optional setting:

- Allow unpaired LAN clients: on/off

### Priority

Medium for trusted networks, high for public/shared networks.

---

## 13) Playback windows expose taskbar controls `[FIXED]`

### Flaw

When a screen is displaying an image or video, the playback window can appear as an icon/window in the Windows taskbar. This makes it easy for an operator or user to accidentally close/kill a playback output from the taskbar instead of using the controller/engine lifecycle controls.

### Risks

- accidental termination of scheduled playback on one screen
- inconsistent multi-screen state if one playback window is killed manually
- support confusion because engine/controller still believe playback should be active
- poor kiosk/signage behavior for production deployments

### Fix plan

Playback output windows should behave like managed display surfaces, not user-manageable taskbar applications.

Implement platform-specific window flags/ownership so playback windows:

- do not create taskbar buttons
- remain fullscreen/borderless on assigned display
- cannot be casually closed from the Windows taskbar
- are still controllable by engine/controller lifecycle commands

For Windows/Qt, investigate and implement the safest combination of:

- `Qt.Tool` or equivalent non-taskbar window flag where compatible with fullscreen playback
- native Windows extended styles such as `WS_EX_TOOLWINDOW` and avoiding `WS_EX_APPWINDOW`
- parent/owner window strategy if needed
- playback-worker process behavior to avoid taskbar exposure

Add regression tests or manual release checklist item:

- start playback on HDMI screen
- verify no extra playback taskbar icon appears
- verify engine can still stop playback
- verify Alt+Tab/taskbar behavior is acceptable for signage mode

### Priority

High. This should be handled during lifecycle stabilization because it directly affects production reliability and operator safety.

---

## 14) Build/update model is basic `[OPEN]`

### Flaw

Packaging exists, but update mechanism is only partially represented by generated manifest/docs.

### Risks

- difficult field updates
- manual replacement mistakes
- mixed versions of controller/engine/workers

### Fix plan

Add app version handshake:

- controller queries engine version
- engine exposes protocol version
- worker protocol version checked
- remote browser client version displayed

Add updater plan:

- check manifest
- download installer
- verify hash/signature
- run installer

### Priority

Medium.

---

## 15) Media library scanning is full-tree and memory-heavy `[OPEN]`

### Flaw

`scan_video_directory()` walks the entire media folder with `Path.rglob("*")`, builds a complete list of `Path` objects, then sorts the whole list. The engine keeps the full `available_videos` list in memory and every state snapshot serializes it through `available_media_payload()`.

Current hot points:

- Full recursive scan on startup and folder refresh.
- `QFileSystemWatcher` watches only the root folder, so deep folder changes can require broad rescans.
- Large libraries create a large `list[Path]` plus repeated snapshot payloads.
- Sorting all files on every scan is `O(n log n)` even if only one folder changed.

### Risks

- High CPU spikes on startup or when a large media folder changes.
- Increased memory use proportional to full library size.
- Controller snapshots become expensive because media payloads are resent with unrelated state changes.
- Slow field machines may feel unresponsive while scanning or serializing media lists.

### Fix plan

- Introduce a `MediaLibraryIndex` service that stores lightweight records, not raw repeated payloads.
- Move scanning into an incremental/indexed model:
  - initial bounded background scan,
  - per-directory dirty flags,
  - optional max-depth or excluded folder settings,
  - stable sort keys cached per item.
- Send media library changes separately from general engine state snapshots.
- Add pagination/filtering for media picker payloads if the library can be large.
- Track scan metrics: files scanned, duration, skipped folders, last error.

### Priority

Performance Phase P1 / Roadmap Phase 1-2.

---

## 16) State snapshots are too large and too frequent `[OPEN]`

### Flaw

`ControllerEngine.snapshot()` includes configured screens, groups, schedules, available media, remote screens, network data, unified screens, status, and playback state in every response. `BackendStateListener` long-polls for state changes, but when any state changes the full snapshot is emitted and the controller reparses/rebuilds broad UI state.

Current hot points:

- `availableMedia` is included in every snapshot.
- `network_snapshot()` and `remote_screens_snapshot()` are rebuilt/copy-sorted as part of snapshots.
- Small changes such as playback pause/status can carry large library/config payloads.

### Risks

- Avoidable CPU and memory churn from JSON serialization/deserialization.
- Controller updates become slower as media library or screen count grows.
- More allocations and garbage collection pressure during normal playback.

### Fix plan

- Split state into channels:
  - `configState`,
  - `mediaLibraryState`,
  - `remoteScreenState`,
  - `playbackState`,
  - `networkState`.
- Track per-channel version numbers and return only changed channels from `/api/subscribe`.
- Keep full snapshot only for initial controller connection or explicit refresh.
- Add tests verifying that playback-only changes do not include the media library payload.

### Priority

Performance Phase P1 / Roadmap Phase 2-3.

---

## 17) Playback workers poll frequently and duplicate runtime cost `[FIXED]`

### Flaw

Each local HDMI playback worker is a separate Python/Qt process with a full `PlaybackWindow`, `QMediaPlayer`, timers, and an 800 ms HTTP polling loop against the LAN server API. Remote browser clients also poll. This is simple, but it is not extremely lightweight.

Current hot points:

- One full Python + Qt runtime per local playback output.
- Each worker performs regular `urlopen()` calls and JSON parsing even when idle.
- Worker command delivery reuses the LAN remote polling API instead of a local efficient IPC channel.
- Process startup memory can dominate on machines with multiple outputs.

### Risks

- High baseline memory per screen.
- Background CPU wakeups from polling even with no active schedule.
- Battery/thermal impact on small PCs or laptops used as controllers.
- More failure points across multiple Python processes.

### Fix plan

- Short term: adaptive polling interval:
  - fast only around scheduled transitions,
  - slow when idle/no command version changes,
  - immediate poll after worker spawn.
- Medium term: replace local worker HTTP polling with push IPC (`QLocalSocket`, named pipe, or engine control socket subscription).
- Long term: evaluate whether local playback can share one lightweight worker process managing multiple windows, while preserving crash isolation if needed.
- Measure memory per worker and document target budgets.

### Priority

Performance Phase P1 / Roadmap Phase 1-3.

---

## 18) Playback windows keep watchdog/media objects active when idle `[FIXED]`

### Flaw

`PlaybackWindow` creates `QMediaPlayer`, `QAudioOutput`, `QVideoWidget`, watchdog timers, overlay widgets, opacity effects, and mouse-tracking controls at construction time. The playback watchdog runs every 3 seconds even when the window is showing an idle message or static image.

### Risks

- Unnecessary timer wakeups when no video is playing.
- Extra GPU/graphics resources for idle windows.
- Memory is committed eagerly instead of only when media type requires it.
- Static image playback can still carry video-player overhead.

### Fix plan

- Start playback watchdog only while a video source is active.
- Stop/detach `QMediaPlayer` when clearing playback or switching to image-only mode.
- Lazily create video/audio objects on first video playback if feasible.
- Add lightweight idle mode for black/message screens.
- Add tests around watchdog start/stop behavior and static-image mode.

### Priority

Performance Phase P1 / Roadmap Phase 1-2.

---

## 19) Remote screen command/state handling copies JSON repeatedly `[OPEN]`

### Flaw

The remote command path performs defensive deep copies with `json.loads(json.dumps(commands))` in `LanRemoteServer.set_commands()`, and snapshots repeatedly copy/sort dictionaries. This is acceptable for small state, but inefficient as screen count or command size grows.

### Risks

- Repeated serialization allocations during command updates.
- CPU churn when commands include media cycle arrays.
- Larger garbage collection pressure during playback sync.

### Fix plan

- Replace JSON round-trip copying with targeted immutable/plain dict copies.
- Use typed command records/dataclasses from the command protocol work.
- Cache prepared remote commands and only update changed screen commands.
- Add micro-tests for command cache invalidation and copy isolation.

### Priority

Performance Phase P2 / Roadmap Phase 3.

---

## 20) UI refresh work is broad rather than dirty-region based `[OPEN]`

### Flaw

The controller applies broad snapshots and rebuilds multiple UI views from full state. It also has a 1-second live status timer. Many update paths call `refresh_monitors()`, `refresh_schedule_list()`, or state summary updates even when only one small slice changed.

### Risks

- Avoidable CPU wakeups while the controller is open.
- UI stutter with large media lists, many schedules, or many remembered screens.
- The controller consumes resources even when mostly idle.

### Fix plan

- Tie UI updates to state channels from flaw #16.
- Only rebuild schedule list when schedules/groups/aliases change.
- Only rebuild media browser when media library version changes.
- Pause low-value UI timers while the controller window is hidden or minimized.
- Add lightweight performance smoke tests for no-op snapshot application.

### Priority

Performance Phase P2 / Roadmap Phase 2-4.

---

## Lightweight performance targets

These targets should be measured on representative low-power Windows hardware and adjusted with real profiling data:

- Idle engine CPU: near 0%, with no constant high-frequency wakeups when no screens are playing.
- Idle controller CPU: near 0% when visible, and effectively 0% when hidden/minimized.
- Local playback worker idle CPU: near 0%; polling should back off when command versions are unchanged.
- Media scan: background only, cancellable/coalesced, and never blocking controller interactions.
- State updates: playback-only changes should not resend the media library or full configuration.
- Memory: avoid full duplicate media lists in controller and engine where a lightweight index or paged query is enough.
- Measurements to record: process RSS, thread count, timer/poll intervals, JSON snapshot byte size, media scan duration, media item count, and per-worker RSS.

---

## Remediation roadmap

## Phase 1 - Stabilize current behavior

Goal: make current architecture safer without rewriting.

Tasks:

1. `[FIXED]` Define official process lifecycle contract.
2. `[FIXED]` Add schema version to config.
3. `[FIXED]` Add atomic config writes.
4. `[FIXED]` Add tests for close/minimize/engine tray lifecycle.
5. `[FIXED]` Fix playback output windows so they do not appear as killable Windows taskbar items.
6. `[FIXED]` Add screen registry service facade while still backed by current data.
7. `[FIXED]` Add UI style helper and apply to all dialogs.
8. `[FIXED]` Add Help Center tests/smoke validation.
9. `[FIXED]` Start playback-window lightweight mode: stop watchdog when idle/static image and avoid unnecessary timer wakeups.
10. `[FIXED]` Add adaptive polling for local playback workers so idle outputs do not poll every 800 ms forever.
11. `[FIXED]` Add performance counters/logging for media scan duration, indexed file count, snapshot size, and worker count.

Estimated effort: 2-4 days.

---

## Phase 2 - Extract pure domain modules

Goal: reduce risk in schedule/playback/screen changes.

Tasks:

1. `[FIXED]` Extract `models.py`.
2. `[FIXED]` Extract `scheduling.py`.
3. `[PARTIAL]` Extract `screen_registry.py`.
4. `[FIXED]` Extract `playback_assignment.py`.
5. `[FIXED]` Extract `media_library.py` / `MediaLibraryIndex` for lightweight indexed media records.
6. `[FIXED]` Split engine state into versioned channels so large media/config payloads are not resent for playback-only changes.
7. `[FIXED]` Move tests to target extracted modules.
8. `[FIXED]` Keep UI and engine behavior unchanged.

Estimated effort: 4-7 days.

---

## Phase 3 - Formalize protocol and persistence

Goal: make engine/workers/LAN clients version-safe.

Tasks:

1. `[FIXED]` Add `PlaybackCommand` dataclass.
2. `[FIXED]` Add command protocol version.
3. `[FIXED]` Add browser client compatibility handling.
4. `[FIXED]` Add config migrations.
5. `[FIXED]` Add merge duplicate screen flow.
6. `[FIXED]` Add diagnostics logging.
7. `[FIXED]` Replace JSON round-trip command copying with typed command records and targeted copy isolation.
8. `[FIXED]` Replace local worker HTTP polling with push IPC or state subscription if measurements justify it.

Estimated effort: 5-10 days.

---

## Phase 4 - UI/product hardening

Goal: production polish and operator confidence.

Tasks:

1. `[FIXED]` Finish Manage Screens as main screen admin workspace.
2. `[FIXED]` Add screen merge workflow.
3. `[FIXED]` Add pending/unpaired LAN clients if security pairing is enabled.
4. `[FIXED]` Add diagnostics export.
5. `[FIXED]` Add release checklist.
6. `[FIXED]` Add accessibility/contrast review.
7. `[FIXED]` Make controller UI dirty-region/channel based so large lists are not rebuilt for no-op or playback-only state changes.
8. `[FIXED]` Pause low-value UI timers while controller is hidden/minimized.

Estimated effort: 5-10 days.

---

## Phase 5 - Packaging/update hardening

Goal: safe deployments.

Tasks:

1. `[OPEN]` Add version handshake across controller/engine/workers.
2. `[OPEN]` Add upgrade compatibility checks.
3. `[OPEN]` Add signed/hash-verified update path.
4. `[OPEN]` Add recovery flow if engine fails after update.
5. `[OPEN]` Add release performance budget checks for idle CPU, engine memory, controller memory, and per-playback-worker memory.

Estimated effort: 5-10 days.

---

## Recommended immediate next actions

The next work should not be another feature. It should be stabilization and measured lightweight operation:

1. Keep this document's status markers updated as each remediation lands.
2. Fix playback windows so scheduled output surfaces do not appear in the Windows taskbar.
3. Start playback-window lightweight mode by stopping the watchdog when idle/static-image only.
4. Add adaptive polling for local playback workers.
5. Add lightweight performance counters for snapshot size, media scan duration, indexed file count, and worker count.
6. Create `models.py`, `scheduling.py`, and continue extracting `screen_registry.py` from facade to full service.
7. Extract `media_library.py` / `MediaLibraryIndex` before adding more media-library features.
8. Add screen registry tests for:
   - IP change
   - name change
   - offline enabled screen
   - purge
   - duplicate merge candidate
9. Split state snapshots so playback-only changes do not include `availableMedia`.
10. Extract remote client HTML/JS out of `main.py`.

---

## Definition of done for architecture remediation

The application is production-architecture-ready when:

- every flaw in this document is marked `[FIXED]` or explicitly `[DEFERRED]` with rationale
- `main.py` is a thin entrypoint, not the whole product
- config has schema version and migrations
- screen identity is managed by a dedicated registry
- schedules/playback assignments are pure and heavily tested
- controller/engine lifecycle is documented and tested
- playback output windows do not appear as killable taskbar items
- LAN command protocol is versioned
- remote client is external static asset, not inline Python string
- Help UI and all dialogs use shared design system
- purge/merge/offline enable flows are covered by tests
- diagnostics logs can explain field failures
- idle CPU and memory usage meet the lightweight performance targets on representative hardware
- media library, remote screen, and playback state updates are incremental rather than full-payload by default
- local playback workers have adaptive or push-based command delivery instead of constant fast polling

---

## Final assessment

The product is functionally promising, but it has reached the point where continued feature work inside the current monolithic structure will increase bugs and regressions. The highest-value improvement is to extract domain/services gradually while preserving current behavior.

Do not rewrite everything at once. Stabilize, extract, test, then improve.
