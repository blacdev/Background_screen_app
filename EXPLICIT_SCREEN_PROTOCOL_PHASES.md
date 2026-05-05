# Explicit Screen Protocol Phases

## Phase 0
- Put the project under git.
- Create a clean baseline commit.
- Move new work to `feature/explicit-screen-protocols`.
- Add test scaffolding for new protocol modules.

## Phase 1
- Add an explicit configured-screen model.
- Let the app store per-screen protocol and capability rules.
- Block unsupported protocol/capability combinations in the UI and engine.
- Keep existing playback behavior unchanged while the new registry is introduced.

App test:
- Open `Engine > Configured Screens...`
- Add, edit, and delete configured screens
- Verify invalid combinations cannot be saved

## Phase 2
- Bind configured `browser` screens to the existing LAN browser receiver path.
- Separate discovered browser clients from user-configured browser screens.
- Route browser-targeted quick play and schedules through configured screen ids.
- Enforce one connected browser receiver per configured browser screen binding.

App test:
- Configure a browser screen
- Bind it to a LAN receiver
- Start quick play and scheduled playback through that configured screen

## Phase 3
- Add a DLNA adapter with discovery and static-media playback.
- Allow configured DLNA screens to bind to discovered DLNA devices.
- Route image/video and playlist playback through DLNA when the screen uses DLNA.

App test:
- Discover a DLNA device
- Bind it to a configured DLNA screen
- Play an image/video and verify playlist sequencing

## Phase 4
- Add a Miracast adapter for live display/app view.
- Allow configured Miracast screens to bind to Miracast sessions/devices.
- Route live-view actions through Miracast for configured Miracast screens.

App test:
- Start a live-view session to a configured Miracast screen
- Verify view-only streaming starts and stops cleanly

## Phase 5
- Add configured live-view routing for browser screens.
- Introduce app/window/display live stream sessions under the explicit screen model.
- Keep live streaming view-only and no-audio in this first version.

App test:
- Stream a chosen window or display to configured browser screens
- Verify stop/start and reconnect behavior

## Coverage Rule
- All new pure protocol modules must have 100% unit-test coverage before a phase is complete.
- UI integration remains phase-testable through the app, while protocol/business logic must stay in separately testable modules.
