# Guide for AI agents

This repository drives a two-axis stage that turns an object in front of a
fixed Revopoint MINI 3D scanner, and turns the scans into point clouds. The
stage is real hardware: a cradle tilts (latitude) and a turntable on it spins
(longitude), each driven by a stepper motor on a BigTreeTech SKR Mini E3 V3.0
board running Marlin.

This guide holds what the code and READMEs don't say outright: the hardware
rules, the conventions, and the gotchas found while building it. For commands
and options, read these first:

- `README.md`: install, scanning, reconstruction, stage conventions.
- `firmware/README.md`: the Marlin build, flashing, the G-code the board takes,
  and driver temperature handling.
- `cad/README.md`: the FreeCAD files, exports, bill of materials, and which
  third-party models are left out and why.
- The module docstrings in `lat_lon_scanner/`, and `--help` on each CLI.
- [revopoint-python](https://github.com/HazenBabcock/revopoint-python) and its
  `notes/` folder: everything known about the scanner's HTTP interface.

## Map

| Path | What it is |
|---|---|
| `lat_lon_scanner/stage.py` | `Stage`: G-code over USB serial (moves, position, zero, release, driver warnings), plus a small CLI. |
| `lat_lon_scanner/scanner.py` | `Scanner`: depth capture at several exposures through revopoint-python, and the scanner's calibration files. |
| `lat_lon_scanner/capture.py` | Scan runs: the pose plan, moving the stage, saving each view. |
| `lat_lon_scanner/reconstruct.py` | A run's views turned into one point cloud: exposure merging, stage axis fitting, mount removal, previews. |
| `tests/` | pytest tests for `reconstruct`. They use synthetic data and need no hardware. |
| `firmware/` | Marlin configuration and build script. GPLv3, unlike the rest (MIT). |
| `cad/` | The author's FreeCAD parts and assemblies, fabrication exports, and the BOM spreadsheet. |
| `examples/` | Example scans as point clouds (`.ply`, mm, with normals and brightness as color). |
| `images/` | Images for the README. |

Scan data lives outside the repo, by default in
`~/Data/revopoint/<date>/<run name>/`. The `capture.py` docstring describes a
run's layout.

## The stage

- **Zero is set by hand.** There are no limit switches. After the board powers
  up, the human puts the cradle upright, then `python -m lat_lon_scanner.stage
  zero` energizes the motors and declares that pose (0, 0) with `G92 X0 Y0`.
  Never send `G28`: homing has nothing to find.
- **Keep latitude within ±90°.** Beyond that, the longitude motor's cable
  winds onto the frame. The firmware doesn't enforce it (Marlin only applies
  travel limits to homed axes), so send every move through `Stage.move_to`,
  which refuses anything outside ±90°. The useful scanning range is +90° to
  −20°; below −20° the scanner mostly sees the mount.
- **Motors hold while the board has power.** Holding is what keeps the object
  from shifting between views. `Stage.release()`, `M18` or a power cycle lets
  the cradle drift, and the stage then needs zeroing again before a scan.
- **The stage moves a real object.** Before the first move in a session,
  check with the human that the object and cables are clear. Speeds are in
  degrees/s; the firmware tops out at 45, and the default is 15.
- **Driver temperature:** `Stage.driver_prewarnings()` reads the TMC2209
  pre-warning flags, and `capture.py` prints any it sees. The drivers can only
  report threshold flags, not a temperature.
- The board shows up as `/dev/serial/by-id/usb-STMicroelectronics_MARLIN_*`.
  The user needs read/write access to it (a udev rule or group membership).

Angles: X is latitude (+X tips the top of the stage toward the scanner), Y is
longitude (+Y turns the turntable counterclockwise seen from above). Poses are
written `(latitude, longitude)` in code and `LATITUDE:LONGITUDE` on the
command line.

## The scanner

- **Address:** `192.168.1.36` by default (`scanner.py`), over WiFi. It comes
  from DHCP, so it can change after the scanner restarts.
- **The projector runs whenever a depth stream is open.** Close every stream
  you open. `Scanner.capture_exposures` closes its streams even on errors;
  follow the same pattern in new code. A stream left open can wedge the
  projector, and then only power-cycling the scanner fixes it. The projector
  can't be seen remotely, so after a session ask the human to confirm it is
  off.
- **The first frame of a stream can be stale,** left over from the previous
  stream. After a stage move it shows the object at the old pose. Both
  revopoint-python's `get_images` and `scanner.py` drop it, and
  `reconstruct.consistent_frames` catches any that get through.
- **Use exposures of 12000 or more.** At 10000 and below, some frames come out
  nearly empty (the projector is dimmer on them). Scans use 12000, 24000 and 40000 by default.
- **Red and yellow surfaces reflect little of the projector's blue light,**
  so they leave holes. A second run at exposure 60000 aimed at the dark area,
  merged with `reconstruct --extra-runs`, fills them in (see the README).

Depth data:

- Frames are 640 × 400 `uint16`, registered to the scanner's **rectified left
  camera**. `reconstruct.read_rectified_camera_matrix` decodes that camera's
  matrix from `mapparamL.bin`.
- Depth is Z along that camera's axis: raw count × 0.004851 mm
  (`scanner.depth_unit_millimeters`), measured by sliding the scanner a known
  distance along its mount. The scanner's own `/oem/depth_scale` holds 200
  counts per mm (5.0 µm), 3% different. Which is right hasn't been settled.
- **The valid depth band depends on where the scanner sits** in its 55.85 mm
  mount slot. At the front (rotation center about 120 mm away) it is raw
  15000–40000, 73–194 mm, the `reconstruct` default. At the back (about
  176 mm) use `--depth-range 100 215`. Values wrap past 65535 (about 318 mm),
  so anything farther reads as very near; always apply a band. Ask the human
  where the scanner is when it matters; it can't be seen remotely. Size
  `--crop-radius` to the object (32 mm suits the duck).

## Coordinates

The **object frame** is the scanner's rectified left-camera frame at latitude
0, longitude 0: +x right, +y down, +z away from the scanner. The turntable
axis points close to −y (up through the object). `reconstruct` fits both stage
axes from the scan itself, plus a nod of the object that grows with
sin(latitude) (objects on the gel pad tip as the cradle tilts; see the
`StageModel` docstring), and writes them to `stage_axes.json` next to each
point cloud. Point clouds are in that frame, in millimeters.

## Color camera (work in progress)

The package doesn't capture or use color yet. What is known so far:

- The color camera (1280 × 800 JPEG, `camera_id=50` on the scanner's media
  endpoint; `get_images(color = True)` in revopoint-python) only delivers
  frames while a depth stream runs.
- Its calibration is on the scanner in `/oem/camparam/` (`/data/camparam`
  links there), readable with the same `download=` request `scanner.py` uses:
  - `Prgb.bin`: the image size it applies to (two int16: 640, 400), then the
    3 × 3 camera matrix as float32. Scale it by 2 for the 1280 × 800 stream.
  - `Distort.bin`: k1, k2, p1, p2, k3.
  - `LC_RT.bin`: a 3 × 3 rotation, then a translation in mm, taking points
    from the rectified left camera frame (where the depth is) into the color
    camera frame.

  At one test pose, projecting depth points through these landed within a few
  pixels of the matching color edges.
- The projector's stripes appear in every color frame in the same place, so
  averaging frames doesn't remove them. More room light, filtering, and
  blending across views are the candidates.

## Working on the code

- Match the existing style: descriptive, unabbreviated names
  (`frames_per_exposure`, not `n_fr`), spaces around `=` in keyword arguments
  and defaults (`speed = 15.0`), and a comment on each measured constant
  saying where its value came from.
- Run `python -m pytest tests` after changing `reconstruct.py`.
- Heavy reconstruction runs are CPU-bound; on a shared desktop, run them at
  low priority (`nice`).
- Keep third-party CAD models (GrabCAD, BigTreeTech, McMaster-Carr) out of the
  repo; their terms don't allow redistribution. `cad/README.md` links to them.

## The bill of materials

`cad/scanner_mount_bom.xlsx` is a copy of the author's working spreadsheet,
kept outside the repo. Ask the author before editing it, so the two copies stay
the same.

- Only the blue counts on the **By sub-assembly** sheet are typed in. Every
  other quantity is a formula. New parts go there as rows, and the totals
  follow.
- The Full BOM matches rows by description with `SUMIFS`, which treats `~`,
  `*` and `?` in the text as wildcards. Keep those characters out of
  descriptions (write "about 1.25 in", not "~1.25 in").
- openpyxl saves formulas without their computed values. After editing with
  it, recalculate in LibreOffice so the numbers are stored again. For a
  text-only change, editing `xl/sharedStrings.xml` inside the file keeps
  everything else intact.
