# Background Screen Controller - Release Checklist

## 1) Pre-release verification

- Confirm `VERSION` is updated for this release.
- Confirm remediation/task statuses are updated in `docs/ARCHITECTURE_REVIEW_AND_REMEDIATION_PLAN.md`.
- Ensure user-facing documentation is current (`docs/USER_GUIDE.md`, release notes if used).

## 2) Build artifacts

- Run the Windows executable build (`build_exe.bat`).
- Verify build output exists in `dist/`.
- Run installer build (`build_installer.bat`).
- Confirm installer artifact is produced and launchable.

## 3) Startup and lifecycle checks

- Launch controller and verify engine can start from **Engine > Start Engine**.
- Verify **Engine > Stop Engine** cleanly stops background process.
- Verify **Run at Windows Sign-in** toggle persists across restart.
- Verify controller minimize/restore and tray lifecycle behavior.

## 4) Playback and screen checks

- Verify local HDMI screen playback starts/stops correctly.
- Verify LAN/browser screen registers from `/tv` URL and receives commands.
- Verify schedule playback, quick play, pause/resume, and stop behavior.
- Verify Manage Screens workflow: enable/default settings, merge duplicates, and group edits.

## 5) Diagnostics and operations

- Verify **Engine > Export Diagnostics...** creates a ZIP bundle.
- Confirm exported bundle includes startup status/log, diagnostics JSONL, and snapshot metadata.
- Check LAN summary warnings and firewall setup guidance on a clean machine.

## 6) Upgrade and compatibility smoke test

- Test upgrading over a previous install with existing app data.
- Confirm configuration migration completes without data loss.
- Verify schedules, aliases, screen groups, and configured screens remain intact.

## 7) Final release gate

- Run targeted automated tests for changed areas.
- Run a basic manual end-to-end pass (engine start, media set, schedule/quick play, diagnostics export).
- Record release date, version, and operator notes.
- Archive installer and checksum/hash according to team policy.
