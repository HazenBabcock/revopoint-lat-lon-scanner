# revopoint-lat-lon-scanner

A two-axis stage that turns an object in front of a fixed Revopoint MINI 3D
scanner: a turntable (longitude) carried on a tilting cradle (latitude). Both
axes are stepper motors driven by a BigTreeTech SKR Mini E3 V3.0.

- [`firmware/`](firmware/) - Marlin configuration and build script for the
  board, which then takes G-code over USB. Licensed separately under the GPLv3.

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
