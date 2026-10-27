"""
Run a lat/lon scan: move the stage through a grid of views and capture depth
at several exposures at each one.

The stage must already be energized and zeroed upright this power-up
(python -m lat_lon_scanner.stage zero); the scan does not set zero itself.

Each run is written to <data_root>/<date>/<name>/:

    run.json                      settings, poses and scanner firmware
    calibration/mapparamL.bin     the scanner's per-camera calibration files
    calibration/mapparamR.bin
    view_000.npz, view_001.npz... one per pose: latitude, longitude, and
                                  depth_<exposure> / mono_<exposure> arrays

    python -m lat_lon_scanner.capture duck
"""
import argparse
import datetime
import json
import os
import pathlib
import time

import numpy as np

import lat_lon_scanner.scanner as scanner_module
import lat_lon_scanner.stage as stage_module

default_data_root = pathlib.Path.home() / "Data" / "revopoint"

# The scanning range runs from +90 (looking straight down on the object) to
# -20; below that the view is mostly the mount and turntable plate.
default_latitudes = (90, 60, 30, 0, -20)
default_longitude_step_degrees = 30


def scan_plan(latitudes = default_latitudes, longitude_step_degrees = default_longitude_step_degrees):
    """
    Return the list of (latitude, longitude) poses to visit, in order.

    At +-90 latitude the turntable axis points at the scanner, so turning it
    only rolls the same view; one pose is taken there. The longitude direction
    alternates after each full row so the turntable never has to swing back.
    """
    longitudes = list(np.arange(0, 360, longitude_step_degrees, dtype = float))
    poses = []
    ascending = True
    for latitude in latitudes:
        if abs(latitude) == stage_module.latitude_hard_limit_degrees:
            row = [poses[-1][1] if poses else 0.0]
        else:
            row = longitudes if ascending else longitudes[::-1]
            ascending = not ascending
        poses.extend((float(latitude), float(longitude)) for longitude in row)
    return poses


def run_scan(stage, scanner, poses, run_directory, exposures = scanner_module.default_exposures,
             frames_per_exposure = 4, speed = stage_module.default_speed_degrees_per_second,
             settle_seconds = 0.5, settings = None):
    """
    Visit each pose, capture, and save. Returns the stage to (0, 0) when the
    scan finishes; after an error the stage is left where it stopped.
    """
    run_directory = pathlib.Path(run_directory)
    run_directory.mkdir(parents = True, exist_ok = False)
    calibration_directory = run_directory / "calibration"
    calibration_directory.mkdir()
    for path, contents in scanner.download_calibration().items():
        (calibration_directory / os.path.basename(path)).write_bytes(contents)

    run_information = {"started" : datetime.datetime.now().isoformat(timespec = "seconds"),
                       "scanner_firmware" : scanner.firmware_version(),
                       "scanner_address" : scanner.address,
                       "stage_port" : stage.port_path,
                       "depth_unit_millimeters" : scanner_module.depth_unit_millimeters,
                       "exposures" : list(exposures),
                       "frames_per_exposure" : frames_per_exposure,
                       "speed_degrees_per_second" : speed,
                       "settle_seconds" : settle_seconds,
                       "poses" : poses,
                       "settings" : settings or {}}
    (run_directory / "run.json").write_text(json.dumps(run_information, indent = 2))

    start_time = time.monotonic()
    for view_index, (latitude, longitude) in enumerate(poses):
        stage.move_to(latitude, longitude, speed)
        time.sleep(settle_seconds)
        captures = scanner.capture_exposures(exposures, frames_per_exposure)
        arrays = {"latitude" : latitude, "longitude" : longitude}
        for exposure, capture in captures.items():
            arrays[f"depth_{exposure}"] = capture["depth"]
            arrays[f"mono_{exposure}"] = capture["mono"]
        np.savez_compressed(run_directory / f"view_{view_index:03d}.npz", **arrays)

        prewarnings = [axis for axis, warned in stage.driver_prewarnings().items() if warned]
        elapsed = time.monotonic() - start_time
        print(f"view {view_index + 1:3d}/{len(poses)}  latitude {latitude:6.1f}  longitude {longitude:6.1f}  "
              f"{elapsed:6.0f} s" + (f"  DRIVER PRE-WARNING {prewarnings}" if prewarnings else ""), flush = True)

    stage.move_to(0.0, 0.0, speed)
    run_information["finished"] = datetime.datetime.now().isoformat(timespec = "seconds")
    (run_directory / "run.json").write_text(json.dumps(run_information, indent = 2))


def main():
    parser = argparse.ArgumentParser(description = "Scan an object on the lat/lon stage.")
    parser.add_argument("name", help = "Run name, e.g. 'duck'.")
    parser.add_argument("--date", default = datetime.date.today().isoformat(),
                        help = "Date directory (default today, %(default)s).")
    parser.add_argument("--data-root", type = pathlib.Path, default = default_data_root,
                        help = "Default %(default)s.")
    parser.add_argument("--latitudes", type = float, nargs = "+", default = default_latitudes)
    parser.add_argument("--longitude-step", type = float, default = default_longitude_step_degrees)
    parser.add_argument("--exposures", type = int, nargs = "+", default = scanner_module.default_exposures)
    parser.add_argument("--frames", type = int, default = 4, help = "Depth frames per exposure.")
    parser.add_argument("--speed", type = float, default = stage_module.default_speed_degrees_per_second,
                        help = "Stage speed, degrees/s.")
    parser.add_argument("--maximum-views", type = int, default = None,
                        help = "Stop after this many poses (for a quick test).")
    arguments = parser.parse_args()

    for latitude in arguments.latitudes:
        if abs(latitude) > stage_module.latitude_hard_limit_degrees:
            parser.error(f"Latitude {latitude} is outside +-{stage_module.latitude_hard_limit_degrees}")
    poses = scan_plan(arguments.latitudes, arguments.longitude_step)
    if arguments.maximum_views is not None:
        poses = poses[:arguments.maximum_views]
    run_directory = arguments.data_root / arguments.date / arguments.name
    print(f"{len(poses)} views -> {run_directory}")

    with stage_module.Stage() as stage:
        run_scan(stage, scanner_module.Scanner(), poses, run_directory,
                 exposures = arguments.exposures, frames_per_exposure = arguments.frames,
                 speed = arguments.speed, settings = vars(arguments) | {"data_root" : str(arguments.data_root)})


if __name__ == "__main__":
    main()
