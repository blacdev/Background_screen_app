# Per-Screen Scheduled Media Implementation Plan

## Goal

Add the ability for one saved time slot to assign different media to different screens.

Example:

- Time slot: `14:00 - 15:00`
- Screen A: cycles through a selected set of images
- Screen B: keeps playing its assigned video
- Screen C: keeps playing its assigned video
- Screen D: keeps playing its assigned video

This must work for:

- Local HDMI / Windows display screens
- LAN/browser controller screens

This must **not** change quick play priority. If quick play is active on a screen, quick play continues to override scheduled playback for that screen.

## Current Code Context

Main file: `main.py`

Current schedule model:

```python
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
```

Important current behavior:

- A schedule has one media file: `video_file`.
- A schedule targets screens/groups via `screen_ids`.
- `active_schedule_for_screen(...)` already resolves the active schedule per screen.
- `PlaybackCoordinator.assignment_for_screen(...)` checks quick play first:

```python
override_path = self.quick_play_paths.get(screen_id)
if override_path is not None:
    return quick_play_assignment
```

So quick play priority is already in the right place and should remain first.

Current scheduled playback path:

1. UI builds a `ScheduleEntry` in `MainWindow.build_candidate_from_form()`.
2. Backend action `save_schedule` validates and stores it.
3. `PlaybackCoordinator.assignment_for_screen(...)` finds the active schedule for each screen.
4. It resolves `entry.video_file` under `self.video_directory`.
5. Local HDMI playback uses `PlaybackWindow.switch_to_entry(...)`.
6. LAN/browser screens receive commands from `PlaybackCoordinator.build_screen_command(...)`.
7. Local worker / remote clients apply the command using `switch_to_override(...)`.

Image support already exists for single images:

- `SUPPORTED_IMAGE_EXTENSIONS`
- `is_image_file(...)`
- `PlaybackWindow.apply_pending_entry(...)` handles images and videos.

LAN/browser screens already receive `media_kind` in commands, so a selected image can be sent the same way as video.

## Proposed Data Model

Introduce per-screen media assignments while keeping backward compatibility with existing config files.

### New dataclass

Add a small dataclass near `ScheduleEntry`:

```python
@dataclass
class ScheduleMediaAssignment:
    screen_id: str
    media_files: list[str]
    mode: str = "single"  # "single" or "cycle"
    interval_seconds: int = 10

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScheduleMediaAssignment":
        ...

    def to_dict(self) -> dict[str, Any]:
        ...
```

Meaning:

- `screen_id`: concrete screen ID, not a group ID.
- `media_files`: relative paths under `video_directory`.
  - One item means play/show that one media file.
  - Multiple image files means cycle through them.
- `mode`:
  - `single`: use first media item only.
  - `cycle`: rotate through `media_files`.
- `interval_seconds`: image rotation delay. Default `10` is acceptable.

### Update `ScheduleEntry`

Add:

```python
media_assignments: dict[str, ScheduleMediaAssignment]
```

Keep existing fields for backward compatibility:

```python
video_file: str
video_label: str
screen_ids: list[str]
```

Recommended updated shape:

```python
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
    media_assignments: dict[str, ScheduleMediaAssignment]
```

### Config JSON format

Existing schedules should continue to load:

```json
{
  "id": "...",
  "title": "Afternoon",
  "start_time": "14:00",
  "end_time": "15:00",
  "video_file": "videos/default.mp4",
  "video_label": "videos/default.mp4",
  "screen_ids": ["group:default"],
  "days": ["mon", "tue"]
}
```

New schedules can add:

```json
{
  "id": "...",
  "title": "Afternoon",
  "start_time": "14:00",
  "end_time": "15:00",
  "video_file": "videos/default.mp4",
  "video_label": "videos/default.mp4",
  "screen_ids": ["group:default"],
  "days": ["mon", "tue"],
  "media_assignments": {
    "screen-a-id": {
      "screen_id": "screen-a-id",
      "media_files": [
        "images/a-1.jpg",
        "images/a-2.jpg",
        "images/a-3.jpg"
      ],
      "mode": "cycle",
      "interval_seconds": 10
    },
    "screen-b-id": {
      "screen_id": "screen-b-id",
      "media_files": ["videos/b.mp4"],
      "mode": "single",
      "interval_seconds": 10
    }
  }
}
```

Backward-compatible rule:

- If no assignment exists for a screen, use `entry.video_file` exactly as today.
- If assignment exists, use that screen-specific assignment.

## Playback Selection Logic

Add helper methods/functions:

```python
def schedule_assignment_for_screen(
    entry: ScheduleEntry,
    screen_id: str,
    screen_groups: list[ScreenGroup],
) -> ScheduleMediaAssignment | None:
    ...
```

Behavior:

1. Prefer exact `screen_id` match in `entry.media_assignments`.
2. If no exact assignment exists, use the legacy `entry.video_file` fallback.
3. Do not assign media based on group IDs in `media_assignments`; expand groups only for determining whether the schedule applies to a screen.

In `PlaybackCoordinator.assignment_for_screen(...)`:

- Keep quick play check first.
- After finding the active schedule, resolve the per-screen media assignment.
- Return an assignment dict that can represent either:
  - one media file
  - a cycling playlist of image files

Suggested returned assignment shape:

```python
{
    "key": "schedule:<entry-id>:<screen-id>:<assignment-hash>:<paused>",
    "type": "play",
    "mode": "schedule",
    "path": first_media_path,
    "paths": [Path(...), Path(...)],
    "label": "...",
    "relative_path": first_relative_path,
    "relative_paths": [...],
    "entry_id": entry.id,
    "cycle": True,
    "cycle_interval_seconds": 10,
    "message": "",
}
```

For missing media:

- If any assigned file is missing, return a clear/missing assignment with a useful message.
- Prefer strict validation at save time so missing files are caught before playback.

## Image Cycling Behavior

### Recommended scope for first implementation

Only cycle image files. Do not cycle videos in this feature unless explicitly requested later.

Rules:

- `media_files` length 1:
  - image: show image
  - video: play video
- `media_files` length > 1:
  - all files must be images
  - playback cycles images every `interval_seconds`

### Local HDMI playback

Modify `PlaybackWindow` to support an image playlist.

Current image display is single-source through:

- `switch_to_source(...)`
- `apply_pending_entry(...)`
- `current_image_path`
- `current_image_pixmap`

Add fields in `PlaybackWindow.__init__`:

```python
self.image_cycle_timer = QTimer(self)
self.image_cycle_timer.timeout.connect(self.advance_image_cycle)
self.image_cycle_paths: list[Path] = []
self.image_cycle_index = 0
self.image_cycle_interval_seconds = 10
```

Stop the timer in:

- `clear_playback(...)`
- video playback path in `apply_pending_entry(...)`
- any switch to quick play/single source

Add methods:

```python
def switch_to_image_cycle_at(
    self,
    source_key: str,
    paths: list[Path],
    display_label: str,
    entry_id: str | None,
    interval_seconds: int,
    play_at_ms: int | None = None,
    force_reload: bool = False,
) -> None:
    ...


def switch_to_image_cycle(...):
    ...


def advance_image_cycle(self) -> None:
    ...
```

Implementation idea:

- Validate paths exist before starting.
- Create a source key containing the ordered list of path resolves and maybe file signatures.
- Load first image immediately using existing image code.
- Start timer with `interval_seconds * 1000`.
- On each timer tick, move to next path and call `switch_to_source(...)` or a lower-level image-specific method.
- Avoid resetting `entry_id`/schedule state unnecessarily.

Important: quick play should call existing `switch_to_override(...)`, which should stop image cycle first.

### LAN/browser playback

Current command sends one `path` and `media_kind`.

Extend `build_screen_command(...)` to include cycle data:

```python
"paths": [str(path) for path in paths],
"relative_paths": [...],
"cycle": bool(...),
"cycle_interval_seconds": int(...),
```

For browser screens, inspect the LAN remote client HTML in `LanRemoteServer._remote_client_html`. It currently handles one command at a time. Add client-side support for commands with:

```json
{
  "type": "play",
  "media_kind": "image",
  "cycle": true,
  "paths": ["..."],
  "cycle_interval_seconds": 10
}
```

Remote client behavior:

- If `cycle` is true and `media_kind == "image"`:
  - Use existing image display path.
  - Keep an array of URLs/paths from the command.
  - Set a JavaScript interval to advance images.
  - Clear that interval whenever a new command arrives, clear command arrives, or video starts.
- Quick play remains higher priority because the backend command for that screen will be quick play while quick play is active.

Note: The remote client likely does not directly load local Windows file paths. Existing LAN command handling probably has a media-serving URL path. Follow the current pattern used by single scheduled images/videos and extend it to each path in the playlist.

## UI Plan

Current UI has one global `Choose Media` button for the schedule.

Add a screen-specific media assignment editor without disrupting the current simple flow.

### Minimal UX

Keep existing global media picker as the fallback/default media for the slot.

Add a new button near `Choose Media`:

```text
Per-Screen Media
```

When clicked, open a dialog showing concrete target screens for the schedule.

Dialog rows:

| Screen | Assignment | Actions |
|---|---|---|
| Screen A | 3 images, cycling every 10s | Choose Images / Clear Override |
| Screen B | videos/b.mp4 | Choose Media / Clear Override |
| Screen C | Using default media | Choose Media / Clear Override |

Important UI rules:

- Expand selected groups into concrete screen IDs for this dialog.
- Show local HDMI and LAN/browser configured screens.
- Do not allow assigning media to group IDs directly in `media_assignments`.
- If a schedule target group later changes, existing assignments for removed screens can remain in config but should not affect playback unless that screen is targeted by the schedule.

### Media selection controls

For each screen row:

- `Choose Media`: select one supported media file from the media library.
- `Choose Image Set`: select multiple image files from the media library.
- `Clear Override`: remove per-screen assignment and use the schedule default media.
- Optional interval spinbox for image sets: default 10 seconds, min 2 seconds, max 3600 seconds.

Qt note:

- `QFileDialog.getOpenFileNames(...)` can select multiple files.
- Current app uses `MediaPickerDialog` over indexed relative media. It may be better to create a new `MultiMediaPickerDialog` that uses `self.available_videos` but filters to image files for image sets.

### Form state in `MainWindow`

Add:

```python
self.current_media_assignments: dict[str, ScheduleMediaAssignment] = {}
```

Update:

- `load_schedule_into_form(None)`: reset to `{}`.
- `load_schedule_into_form(schedule_id)`: copy from `entry.media_assignments`.
- `build_candidate_from_form()`: pass assignments into `ScheduleEntry`.
- `update_selection_summary()` or nearby summary: include e.g. `3 per-screen overrides`.

## Validation Rules

Update `validate_schedule_candidate(...)` and backend `save_schedule` validation.

Required behavior:

1. Schedule must still have at least one target screen/group.
2. Schedule must have either:
   - a valid default `video_file`, or
   - at least one valid per-screen assignment for every targeted concrete screen.

Recommended simpler first implementation:

- Keep requiring default `video_file` for backward compatibility and as fallback.
- Validate all per-screen assignment files exist and are supported.
- If assignment has more than one file:
  - all files must be images
  - mode must be `cycle`
- If assignment has one file:
  - image or video is allowed
  - mode can be `single`
- `interval_seconds` must be clamped/validated, e.g. 2 to 3600.

Target validation:

- `media_assignments` keys should be concrete screen IDs only.
- Ignore assignments whose screen is not part of `expand_target_ids(entry.screen_ids, screen_groups)` during playback.
- Optionally warn in UI but do not fail save, because screens/groups may be offline or reconnected later.

## Conflict / Overlap Logic

Existing overlap check uses schedule target IDs:

```python
schedule_targets_overlap(candidate.screen_ids, entry.screen_ids)
```

This should remain unchanged for the first implementation.

Do not allow two schedules for the same target/group/time window just because they assign different per-screen media. That preserves existing behavior and avoids complicated partial-overlap rules.

## Backend / Snapshot Changes

Schedules already flow through snapshots using `entry.to_dict()` and `ScheduleEntry.from_dict(...)`.

Once the dataclass serialization is updated, these paths should mostly continue to work:

- `save_config(...)`
- `ControllerEngine.snapshot` / status payload paths
- `MainWindow.apply_backend_snapshot(...)`

Search for all `ScheduleEntry(` constructors and update them.

Known locations:

- `ScheduleEntry.from_dict(...)`
- `MainWindow.build_candidate_from_form(...)`
- tests in `tests/`

## Test Plan

Add/adjust tests in `tests/`.

### Serialization tests

Add tests for:

- Legacy schedule without `media_assignments` loads with empty assignments.
- New schedule with assignments serializes/deserializes correctly.
- Bad assignment payload is sanitized or rejected as intended.

### Validation tests

Add tests for:

- Single image assignment passes.
- Single video assignment passes.
- Multiple image assignment passes as `cycle`.
- Multiple video files fail.
- Mixed image/video files fail for cycle.
- Missing assignment media fails on save/validation where path checks are available.

### Assignment tests

Add tests around `PlaybackCoordinator.assignment_for_screen(...)` or a smaller helper if easier:

- Quick play wins over schedule assignment.
- Screen A receives image cycle assignment.
- Screen B receives video assignment.
- Screen with no specific assignment receives legacy `entry.video_file`.
- Group-targeted schedule expands to concrete screen IDs and per-screen assignment still matches concrete screen ID.

### Command tests

Add tests for `build_screen_command(...)`:

- Single media command remains backward compatible.
- Cycle command includes `paths`, `cycle`, and `cycle_interval_seconds`.

## Implementation Order

Recommended order for GPT-5.2:

1. Add `ScheduleMediaAssignment` dataclass and update `ScheduleEntry` serialization.
2. Add helper functions for validating/choosing per-screen schedule media.
3. Update `PlaybackCoordinator.assignment_for_screen(...)` and `build_screen_command(...)` to carry playlist/cycle metadata.
4. Update local HDMI playback in `PlaybackWindow` to support image cycling.
5. Update LAN/browser remote client command handling for image cycling.
6. Add UI dialog for per-screen media assignments.
7. Wire UI state through `MainWindow.load_schedule_into_form(...)`, `build_candidate_from_form(...)`, and save.
8. Add/update tests.
9. Run tests with `pytest` from `background_screen_app`.

## Non-Goals For First Pass

Do not implement unless specifically requested:

- Video playlists / cycling multiple videos.
- Mixed image/video playlists.
- Per-screen transition methods.
- Changing quick play behavior.
- Partial schedule-overlap support where screens inside a group can overlap independently.

## Acceptance Criteria

The feature is complete when:

1. A user can create one time slot targeting screens A-D.
2. The user can assign an image set to screen A.
3. The user can assign or keep a video for screens B-D.
4. During the time slot:
   - screen A cycles through the selected images;
   - screens B-D continue their assigned/default media;
   - HDMI and LAN/browser screens behave consistently.
5. Starting quick play on any screen overrides that screen’s scheduled media.
6. Clearing quick play returns the screen to its scheduled per-screen media.
7. Existing saved schedules without per-screen assignments still work unchanged.
8. Tests cover serialization, validation, assignment selection, and command payloads.
