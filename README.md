# Background Screen App

Standalone desktop app for scheduled background media playback across one or more monitors.

The app now runs in two processes:

- a lightweight controller UI
- a background engine that owns playback, LAN TV serving, media scanning, and persistent state

The controller UI talks to the local engine over `127.0.0.1:8766`.

## What it does

- Lets you choose which monitors should show the fullscreen background video
- Lets you define global playback screens and also target specific screens per schedule slot
- Lets you point the app at a folder that already contains the videos and pictures
- Traverses subfolders inside that media folder and groups scheduled media by folder path
- Watches that folder and its contents and hot-reloads the available media list when files are added, removed, or swapped out
- Stores schedules in UK time down to the minute
- Remembers which media file belongs to which schedule entry
- Opens fullscreen playback windows on the selected monitors
- Switches videos or pictures automatically when the UK-time schedule changes
- Lets you drag and drop a video or picture for immediate quick play on one screen or on the selected screen group
- Lets quick play browse and play media from anywhere on disk without tying it to the scheduled media folder
- Gives quick play priority on the chosen screen until you stop quick play and return that screen to its scheduled slot
- Can keep running in the background from the system tray
- Can be set to run at Windows sign-in

## Run

Windows:

```bat
run_windows.bat
```

Silent Windows launcher:

```text
run_windows.vbs
```

Or:

```bash
python main.py
```

To run only the background engine:

```bash
python main.py --engine
```

To stop the detached background engine:

```bash
python main.py --stop-engine
```

## Build A Single EXE

Build this on Windows, not from WSL/Linux.

From the `background_screen_app` folder, run:

```bat
build_exe.bat
```

That script will:

- install `PyInstaller` if it is missing
- read the version from `VERSION`
- generate Windows version metadata for the packaged executable
- build a single-file Windows executable
- place the output at:

```text
dist\Background Screen Controller.exe
```

Important:

- a packaged `.exe` run by itself now stores data in the current user's Windows app-data location by default
- if you enable `Run at Windows Sign-in`, the packaged `.exe` will relaunch itself correctly

## Build A Windows Installer

To build a proper installer package on Windows, run:

```bat
build_installer.bat
```

That script will:

- build the single-file `.exe`
- compile an Inno Setup installer with the current app version
- generate a simple update manifest for hosted releases
- place the installer at:

```text
installer-dist\BackgroundScreenControllerSetup-<version>.exe
```

- and place the update manifest at:

```text
installer-dist\latest.json
```

Installer behavior:

- `Current user` install stores app data under `%APPDATA%\Background Screen Controller`
- `All users` install stores app data under `%PROGRAMDATA%\Background Screen Controller`
- the installer writes install context into the app folder so the app knows which data location to use
- config and other app data are kept outside the install directory, so uninstalling or replacing the app is less likely to remove them by mistake
- uninstall does not remove the app-data folder by default
- uninstall asks the installed app to stop the background engine before removing files
- upgrades reuse the same app identity, so a newer installer behaves like an in-place update instead of a second app

## Versioning And Updates

- `VERSION` is the single source of truth for release versioning
- `build_exe.bat` stamps that version into the packaged `.exe`
- `build_installer.bat` uses that version for the installer filename and Windows installer metadata
- `installer-dist\latest.json` is generated for each release and can be hosted beside the installer as a simple update feed

Example hosted release layout:

```text
/releases/background-screen-controller/
  latest.json
  BackgroundScreenControllerSetup-1.0.0.exe
```

That gives you a clean update path:

- publish a new versioned installer
- replace `latest.json`
- let the app or a future updater read that manifest to discover the latest installer

## Stored Data

The app creates:

- `%APPDATA%\Background Screen Controller\config.json` for per-user installed runs
- `%PROGRAMDATA%\Background Screen Controller\config.json` for all-users installed runs
- `app_data/config.json` for local source/dev runs

This holds the saved monitor selection, configured video folder, and schedule mapping.
