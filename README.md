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

The default plan is latitudes +90, +60, +30, 0 and −20, with each row's
longitudes spaced so neighboring views are about 30° of arc apart on the sphere
(`--spacing`): 1, 6, 10, 12 and 11 views, 40 in all at about 15 s each. Each
view captures 4 depth frames at each of three exposures (12000, 24000, 40000).
`python -m lat_lon_scanner.stage` also takes `position`,
`move <latitude> <longitude>` and `release`.

## Reconstruction

```bash
python -m lat_lon_scanner.reconstruct ~/Data/revopoint/<date>/duck
```

This needs the `reconstruct` extras (Open3D, SciPy) and writes
`reconstruction/` inside the run: `point_cloud.ply` (positions in mm, normals,
and the scanner's grayscale brightness as color), `stage_axes.json`, and
`preview_*.png` renderings from six directions.

The stage needs no separate calibration. The two rotation axes are fitted from
the scan itself, by minimizing how far neighboring views disagree once each is
turned back by its commanded angles. On the first duck scan the axes came out
0.5 mm from intersecting, and neighboring views agreed to about 0.3 mm. Each
view is filtered (pixels at depth edges and surfaces seen nearly edge-on are
dropped), the views are merged, and the result is cropped to the object: the
post and cap it sits on are found as a stack of cylinders on the turntable axis
and cut away. Red and yellow surfaces reflect little of the scanner's blue
light, so expect holes there (the duck's beak).

### Filling in a dark area

A dark area, such as the duck's red beak, keeps getting depth at longer
exposures after the rest of the object has stopped improving. Rather than
rescanning everything, scan just that area at 60000 (the top of the scanner's
useful range) as a second run, without moving the scanner or re-zeroing the
stage, then merge it in:

```bash
python -m lat_lon_scanner.capture duck_beak --exposures 60000 \
    --poses "-15:-37 -15:-22 -15:-7 -15:8 -15:23 0:23 0:8 0:-7 0:-22 0:-37"
python -m lat_lon_scanner.reconstruct ~/Data/revopoint/<date>/duck \
    --extra-runs ~/Data/revopoint/<date>/duck_beak --output-name reconstruction_with_beak
```

The extra run's views are placed using the axes fitted from the main run. For
the duck, 20 views around the beak took 3 minutes, lined up with the main scan
as well as its own views do (about 0.1 mm), and nearly doubled the points
within 5 mm of the beak's center.

## Tests

```bash
python -m pytest tests
```

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
