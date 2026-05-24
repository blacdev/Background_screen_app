# Background Screen Controller
## Setup and Operator Guide

Audience: installers, operators, and support staff
Version: current build

---

## Start here - complete setup from start to finish

Use this section when you want one clear setup flow without jumping between topics.

Before you begin:

- install and launch the app on the Windows controller PC
- make sure all local HDMI displays are connected and powered on
- make sure any LAN display device is on the same network as the controller PC
- prepare a media folder that contains the videos and images you want to use

Complete setup flow:

1. Launch the app.
2. Start the background engine from `Engine > Start Engine` if it is not already running.
3. Set the media folder from `Library > Set Media Folder`.
4. Wait for the library to finish scanning.
5. Open `Screen > Manage Screens`.
6. Confirm local HDMI screens appear in the screen list.
7. For each local screen you want to use:
   - select it
   - give it a clear name
   - decide whether it should be part of the default playback group
   - click `Apply`
8. If you are using LAN browser screens, open the TV URL shown in the LAN panel on each remote device.
9. Return to `Screen > Manage Screens` and confirm the LAN screens appear.
10. For each LAN screen you want to use:
    - select it
    - give it a clear name
    - enable it for scheduling if you want it to remain targetable while offline
    - add it to the default playback group if needed
    - click `Apply`
11. If some screens should always be targeted together, click `Manage Groups...` and create your groups.
12. Build your first schedule:
    - enter a title
    - choose start and end times
    - choose days
    - choose target screens or groups
    - choose media
    - save the schedule
13. If different screens need different media during the same schedule window, use `Per-Screen Media` before saving.
14. Start playback.
15. Confirm each target screen is showing the expected content.

End-of-setup verification:

- the engine is online
- the media folder is set and scanned
- all required screens are named correctly
- default playback screens are correct
- any required groups exist
- at least one schedule is saved
- playback starts on the intended screens

If something does not work, go to the troubleshooting section at the end of this guide.

---

## What the app is for

Background Screen Controller plays scheduled media across:

- local HDMI screens connected to the controller PC
- LAN browser screens opened from the controller's TV URL

You can use it to:

- schedule media by day and time using UK time
- target default screens, specific screens, or groups
- assign different media to individual screens inside one schedule window
- run Quick Play overrides for urgent or temporary content
- manage offline-capable screen records for stable scheduling

---

## Core concepts you should understand first

### Screen
A playback endpoint. This can be a local HDMI display or a LAN browser display.

### Enabled screen
A screen that stays available for scheduling even when it is currently offline.

### Default playback group
The baseline set of screens used when a schedule targets the default group instead of specific screens.

### Screen group
A named collection of screens that you can target together.

### Schedule
A saved playback window with days, start time, end time, target screens, and media.

### Per-screen media
A schedule option that lets one screen use different media from the others in the same schedule window.

### Quick Play
A temporary priority override that immediately takes over selected screens until it is stopped.

---

## Add and verify the media library

Use this whenever you are preparing content.

Steps:

1. Open `Library > Set Media Folder`.
2. Choose the folder that contains your videos and images.
3. Wait for the app to finish scanning.
4. Open the media picker or schedule builder and confirm your files are visible.

Important rules:

- scheduled media must exist in the configured library
- image cycles require image files only
- if files were added after the initial setup, refresh the library before assuming they are missing

If media is missing:

- confirm the folder is correct
- confirm the file still exists on disk
- refresh the library
- check that the file type is supported

---

## Add local HDMI screens

Use this when the playback screen is physically connected to the controller PC.

Steps:

1. Connect the display to the controller PC.
2. Make sure Windows detects the display.
3. Open `Screen > Manage Screens`.
4. Confirm the screen appears in the left-hand list.
5. Select it.
6. Give it a clear operational name.
7. Decide whether it belongs in the default playback group.
8. Click `Apply`.

Good naming examples:

- Lobby Left
- Reception Portrait
- Hall A Main Screen

---

## Add LAN browser screens

Use this when the playback screen is another device on the network.

Steps:

1. Start the engine if it is not already running.
2. Look at the LAN panel in the controller.
3. Open the TV URL shown there on the remote device's browser.
4. Wait for the device to register.
5. Open `Screen > Manage Screens`.
6. Select the newly detected LAN screen.
7. Rename it clearly.
8. Enable it for scheduling if you want to target it while offline.
9. Add it to the default playback group if needed.
10. Click `Apply`.

Identity behavior:

- LAN browser screens use a stored client ID
- normal IP changes should not create duplicate screens
- renaming does not create a new identity
- clearing browser storage can create what looks like a new screen identity

If you replace a device and a stale screen remains, use the purge tools described below.

---

## Manage screens in one place

Open: `Screen > Manage Screens`

This workspace is the main place for:

- renaming screens
- enabling offline scheduling
- deciding default playback membership
- reviewing screen details
- opening group management
- purging stale remembered screens

Recommended operator flow:

1. Open Manage Screens.
2. Work through the left-hand list one screen at a time.
3. Rename each screen using a clear location-based name.
4. Enable screens that should remain schedulable while offline.
5. Add only the screens you want in the default playback group.
6. Click `Apply` after making changes.
7. Repeat until all important screens are configured.

---

## Create screen groups

Use groups when the same set of screens is usually targeted together.

Examples:

- Lobby Screens
- Reception Screens
- Floor 2 Portrait Displays

Steps:

1. Open `Screen > Manage Screens`.
2. Click `Manage Groups...`.
3. Create a group with a clear operational name.
4. Select the screens that belong in that group.
5. Save the group.
6. Repeat for any other locations or zones.

Why groups help:

- schedules stay easier to read
- location-based targeting is faster
- future renaming of individual screens is easier to manage operationally

---

## Build a schedule

Use this when you want playback to happen automatically.

Steps:

1. Open the schedule builder.
2. Enter a clear title.
3. Choose the start time.
4. Choose the end time.
5. Select the days.
6. Choose the target screens or groups.
7. Choose the default media.
8. Save the schedule.
9. Review the saved entry in the schedule list.

Important behavior:

- schedule times use UK time
- overlapping schedules on the same target/time are blocked
- default group targeting uses the screens currently marked as part of the default playback group

Good title examples:

- Morning Loop
- Lobby Afternoon Promo
- Closing Notices

---

## Use per-screen media in one schedule

Use this when multiple screens share one time window but should not all show the same media.

Examples:

- one lobby screen shows a promo video
- another lobby screen rotates a set of poster images
- a reception screen shows a different static welcome image

Steps:

1. Build or edit the schedule normally.
2. Set the default media for the schedule.
3. Open `Per-Screen Media`.
4. Choose the screen that needs different content.
5. Assign either:
   - a single media file, or
   - an image cycle
6. Repeat for other screens if needed.
7. Save the per-screen assignments.
8. Save the schedule.

Rules:

- image cycle requires two or more image files
- image cycle uses image files only
- files must exist in the configured library
- any screen without a per-screen override keeps the schedule's default media

---

## Use Quick Play correctly

Use Quick Play when content must override the schedule immediately.

Examples:

- emergency notice
- short-term promotion
- temporary event content

Steps:

1. Drag a video or image into the Quick Play area, or choose media manually.
2. Select the target screens.
3. Confirm the override.
4. Verify the selected screens switch immediately.
5. When finished, stop Quick Play so those screens return to scheduled playback.

Quick Play rules:

- Quick Play has higher priority than schedule playback
- it affects only the selected screens
- stopping Quick Play returns control to the normal schedule

---

## Read engine diagnostics quickly

Use the footer bar in the main window when you need a quick health summary without opening logs.

What the diagnostics area shows:

- current local playback worker count
- media library item count
- last media scan duration
- latest snapshot size
- scanning state or current library error

How to use it:

1. Look at the diagnostics block in the footer.
2. If the library is still indexing, wait before assuming media is missing.
3. If a library error is shown, fix that before troubleshooting schedules.
4. Hover the diagnostics block to see the full tooltip summary.

---

## Daily operating checklist

Use this at the start of a working day or before an event.

Checklist:

- engine is online
- LAN panel shows expected status
- critical screens appear correctly in Manage Screens
- media library is correct
- required schedules are present
- playback starts on the intended screens

If anything looks wrong, fix the screen or library issue before relying on the schedule.

---

## Replace a screen or clean up stale records

Use this when a TV, browser device, or remembered remote screen has changed.

### Replace a LAN device

1. Open `Screen > Manage Screens`.
2. Confirm whether the old device still exists as a remembered screen.
3. If the old device is offline and no longer needed, purge it.
4. Open the TV URL on the replacement device.
5. Wait for the new device to register.
6. Rename and configure it.
7. Verify schedule targeting still points where you expect.

### Purge Offline

Use this when you want to remove remembered remote screens that are currently offline.

### Purge All

Use this only when you intentionally want to remove all remembered remote screens, including online ones.

Purge cleanup also updates related references such as:

- default selected screens
- schedule targets
- per-screen overrides
- aliases
- group membership

---

## Troubleshooting from start to finish

### A screen is missing from the target list

Check in this order:

1. open `Manage Screens`
2. confirm the screen exists
3. if it is offline but should remain schedulable, enable it for scheduling
4. for LAN screens, confirm the TV URL is open on the remote device
5. refresh and try again

### A LAN screen appears twice

Check in this order:

1. identify which entry is the active online one
2. check whether the remote browser storage may have been reset
3. purge the stale offline entry
4. keep the active online record

### Media does not play

Check in this order:

1. confirm the media file still exists in the library
2. confirm the correct library folder is selected
3. refresh the library
4. confirm the file type is supported
5. re-open the schedule or Quick Play source and test again

### Playback does not start

Check in this order:

1. confirm the engine is online
2. confirm at least one valid target exists
3. confirm the current UK time matches an active schedule
4. confirm the schedule targets the intended screens or groups
5. confirm no Quick Play override is blocking what you expect

### A replaced TV is still referenced in schedules

Check in this order:

1. purge the stale remembered screen
2. open the TV URL on the replacement device
3. re-check group membership and screen targeting
4. review any per-screen media assignments that referenced the old screen

---

## Capability summary

| Capability | Local HDMI | LAN Browser |
|---|---:|---:|
| Default group targeting | ✅ | ✅ |
| Group targeting | ✅ | ✅ |
| Schedule playback | ✅ | ✅ |
| Per-screen single media | ✅ | ✅ |
| Per-screen image cycle | ✅ | ✅ |
| Quick Play override | ✅ | ✅ |
| Offline scheduling via enable flag | n/a | ✅ |
