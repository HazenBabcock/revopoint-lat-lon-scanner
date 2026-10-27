# revopoint-lat-lon-scanner

A two-axis stage that turns an object in front of a fixed Revopoint MINI 3D
scanner: a turntable (longitude) carried on a tilting cradle (latitude). Both
axes are stepper motors driven by a BigTreeTech SKR Mini E3 V3.0.

- [`firmware/`](firmware/) - Marlin configuration and build script for the
  board, which then takes G-code over USB. Licensed separately under the GPLv3.
- [`lat_lon_scanner/`](lat_lon_scanner/) - Python package that drives the stage
  and the scanner.

## Scanning

Install [revopoint-python](https://github.com/HazenBabcock/revopoint-python)
(`pip install -e <checkout>`), then this package (`pip install -e .`).

After powering up the stage, energize the motors, push the cradle upright by
hand (the motors hold it there), and declare that pose as zero:

```bash
python -m lat_lon_scanner.stage zero
```

Then scan. Each run goes to `~/Data/revopoint/<date>/<name>/`, with one `.npz`
per view holding the depth frames and stereo images at each exposure:

```bash
python -m lat_lon_scanner.capture duck
```

The default plan is latitudes +90, +60, +30, 0 and −20 with longitude every
30° (49 views, about 15 s each), capturing 4 depth frames at each of three
exposures (12000, 24000, 40000). `python -m lat_lon_scanner.stage` also takes
`position`, `move <latitude> <longitude>` and `release`.

## Stage conventions

Positions are in degrees. Zero is the cradle upright (turntable axis vertical),
set with `G92 X0 Y0` after each power-up since there are no limit switches.

| Axis | Motion | Positive direction | Hard limit | Scanning range |
|---|---|---|---|---|
| X | Latitude (tilt) | Top of the stage tilts toward the scanner | ±90° | +90° to −20° |
| Y | Longitude (turntable) | Counterclockwise seen from above (cradle upright) | None | Any |

- The latitude hard limit is set by the longitude motor's cable, which winds
  onto the apparatus beyond ±90°.
- Below −20° the scanner mostly sees the underside of the mount and the
  turntable plate.
- Turning the object counterclockwise moves the scanner's viewpoint clockwise
  around it, so +Y lowers the viewpoint's longitude on the object.

## License

MIT (see `LICENSE`), except `firmware/`, which is derived from Marlin and
licensed under the GPLv3 (see `firmware/LICENSE`).
