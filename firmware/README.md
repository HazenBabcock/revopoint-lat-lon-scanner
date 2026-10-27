# Stage firmware

Marlin 2.1.2.9 configured for the two-axis stage, running on a BigTreeTech
SKR Mini E3 V3.0 (STM32G0B1). The board takes G-code directly over its USB
serial port, so no host software has to stay running to hold the stage.

## Axes

| Axis | Motion | Motor | Run current |
|---|---|---|---|
| X | Latitude (tilt) | STEPPERONLINE NEMA 17, 2 A, 55 Ncm, 1.8° | 800 mA RMS |
| Y | Longitude (turntable) | STEPPERONLINE NEMA 17 pancake, 1 A, 17 Ncm, 1.8° | 480 mA RMS |

Positions are in **degrees**: 200 full steps × 16 microsteps / 360° = 8.888889
steps per degree, with the motors driving the axes directly. Feedrates (`F`)
are therefore degrees per minute.

## Changes from BTT's example

`config/` started as BTT's Ender-3 example for this board
(`MarlinFirmware/Configurations`, tag 2.1.2.9); the first commit is that file
unmodified, so `git diff` against it shows every change. In summary:

- Two axes only. No Z, no extruder, no heaters or thermistors, no LCD, SD
  card printing or bed levelling.
- Steps, feedrates (45°/s) and accelerations (90°/s²) in degrees.
- **Motors stay energized** once enabled. The idle timeout that normally
  switches them off after 120 s is removed, and the TMC2209s hold at full run
  current instead of dropping to half when stopped.
- Run currents set from the motor ratings above. Ratings are treated as peak
  current, so the RMS ceiling is the rating divided by √2.

## Building

Needs git and [PlatformIO](https://platformio.org/).

```bash
./build.sh
```

This clones Marlin 2.1.2.9 into `build/`, copies in `config/`, and leaves the
image at `build/firmware.bin`. Set `PIO` to the `pio` executable if it is not
on your `PATH`.

## Flashing

1. Copy `firmware.bin` to the root of a FAT32 microSD card, keeping that name.
2. With the board powered off, insert the card, then power it on.
3. The bootloader flashes the image and renames the file to `FIRMWARE.CUR`.

## Using it

The board appears as a USB serial port (`/dev/ttyACM0` on Linux); the baud
rate does not matter.

| G-code | Effect |
|---|---|
| `M17` | Energize both motors. They also energize on the first move. |
| `G92 X0 Y0` | Declare the current pose as zero. |
| `G90` / `G91` | Absolute / relative moves. |
| `G1 X30 Y90 F1200` | Move to latitude 30°, longitude 90° at 20°/s. |
| `M400` | Wait until all moves have finished. |
| `M114` | Report the position. |
| `M906 X800 Y480` | Change run currents (mA RMS). `M500` saves them. |
| `M18` / `M84` | Release the motors. |

Things to know:

- **There are no limit switches.** After power-up, put the stage in a known
  pose and send `G92`. Do not send `G28`: homing has nothing to find.
- **There are no firmware travel limits.** Marlin only applies its software
  limits to axes that have been homed, so the controlling program has to keep
  the latitude inside its safe range.
- Holding needs board power. Turning the board off releases both motors and
  loses the position.
- `M112` halts the firmware and releases the motors. `M410` stops motion
  immediately but keeps them energized; the position may be off afterwards.
- Settings saved with `M500` override the compiled defaults, even after
  reflashing. `M502` followed by `M500` restores the defaults.

## License

Marlin is licensed under the GNU General Public License v3; see `LICENSE`.
The files in `config/` are derived from Marlin's and are under the same
license. The corresponding source for a `firmware.bin` built here is Marlin
tag [2.1.2.9](https://github.com/MarlinFirmware/Marlin/tree/2.1.2.9) plus
the two files in `config/`.
