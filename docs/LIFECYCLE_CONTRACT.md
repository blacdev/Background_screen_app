# Background Screen Controller Lifecycle Contract

This document defines the intended runtime behavior of the controller, engine, playback workers, and tray interactions.

The goal is that operators, developers, and support staff can answer these questions clearly:

- what starts first
- what keeps running in the background
- what closing the controller actually does
- what tray actions are allowed to stop the engine
- how playback workers are expected to behave

---

## Process model

The app uses separate responsibilities at runtime:

### 1. Controller process

The controller process is the visible desktop UI.

It is responsible for:

- showing the operator interface
- editing schedules, screen state, and settings
- sending actions to the engine
- rendering live status from engine snapshots

It is not responsible for owning playback.

### 2. Engine process

The engine process is the long-running background owner of runtime state.

It is responsible for:

- persistent runtime state
- playback coordination
- LAN TV server
- media scanning
- local playback worker orchestration
- engine tray behavior

The engine is the source of truth while the app is running.

### 3. Local playback worker process

A local playback worker is a display-surface process for one local HDMI output.

It is responsible for:

- showing the fullscreen playback surface on one screen
- receiving commands from the engine
- switching between idle, image, image-cycle, and video playback states

A playback worker is not a user-facing control surface.

### 4. LAN browser playback client

A LAN browser client is a remote display surface opened from the TV URL.

It is responsible for:

- registering with the engine
- polling or receiving effective playback commands
- rendering remote playback content

---

## Startup contract

Expected startup sequence:

1. The controller starts.
2. The controller attempts to connect to the local engine.
3. If the engine is not already running, the operator can start it from `Engine > Start Engine`.
4. Once online, the controller reads state from the engine.
5. The engine starts or resumes:
   - control API
   - LAN server
   - media scanning
   - local playback worker orchestration
6. Playback workers are created only when the engine needs local output surfaces.

Key rule:

- the controller may exist without active playback
- the engine may exist without the controller window being open

---

## Shutdown contract

### Closing the controller window

When the operator closes the controller window using the window close button:

- the controller process exits
- the engine continues running
- playback continues if it was already active
- the engine tray remains available

This is intentional.

Closing the controller window is not the same as shutting down the full app.

### Quit completely / stop engine

When the operator uses the explicit engine/tray shutdown path:

- the controller requests engine shutdown
- the engine stops playback orchestration
- the engine shuts down its services
- playback workers are stopped
- the controller exits

This is the only normal path that should shut down the engine intentionally from the UI.

---

## Tray contract

### Controller tray behavior

Current contract:

- the controller does not own the authoritative tray icon
- the engine tray is the single tray entry point

### Engine tray behavior

The engine tray must allow:

- opening the controller window
- stopping the engine
- exiting the engine app

Expected behavior:

- if the controller window is closed, the engine tray can reopen it
- if the engine is stopped, playback workers must stop too

---

## Playback worker contract

Playback workers must behave like managed display surfaces, not normal desktop app windows.

Expected behavior:

- fullscreen on assigned display
- borderless
- not exposed as a normal taskbar control surface
- controlled only by engine/runtime lifecycle decisions
- safe to recreate if the engine decides the worker is no longer valid

Playback workers should:

- poll lightly when idle
- react faster when active playback is changing
- avoid unnecessary timers when idle or image-only

---

## State ownership contract

Runtime state ownership is intentionally centralized:

- the engine owns live playback/runtime state
- the controller reflects and edits that state
- playback workers apply commands derived from engine state

The controller must not become an independent runtime authority.

---

## Failure handling contract

### If the controller dies

Expected behavior:

- the engine keeps running
- playback keeps running
- the controller can be reopened from the engine tray

### If a playback worker dies

Expected behavior:

- the engine detects the missing worker
- the engine may recreate the worker if the screen is still required
- the rest of the app keeps running

### If the engine dies

Expected behavior:

- controller shows engine offline state
- playback control is unavailable
- tray recovery or manual restart is required

---

## Manual verification checklist

Use this checklist after lifecycle changes:

1. Start controller and engine.
2. Confirm engine tray appears.
3. Close the controller window.
4. Confirm the controller exits but playback/engine remain running.
5. Reopen the controller from the engine tray.
6. Confirm the controller restores correctly.
7. Start playback on a local screen.
8. Stop the engine from the tray.
9. Confirm playback workers stop.
10. Confirm no orphaned taskbar-style playback surface remains.

---

## Developer rules

When changing lifecycle behavior:

- do not make the controller the owner of playback state
- do not let ordinary controller-close behavior kill the engine silently
- do not let playback workers become independent user-managed windows
- update this document whenever the lifecycle contract changes
- keep lifecycle tests aligned with this contract
