"""
Depth capture from the Revopoint MINI at several exposures, through
revopoint-python (github.com/HazenBabcock/revopoint-python).

A single exposure can't cover a whole scene: white parts saturate at long
exposures while yellow or red ones (which absorb the scanner's blue light)
only get depth at long ones. Capturing 12000, 24000 and 40000 and merging gave
32% more coverage on the test duck than the best single exposure, and where
exposures overlap they agree.
"""
import numpy as np
import requests

import revopoint_python.revopoint as revopoint

default_scanner_address = "192.168.1.36"
default_exposures = (12000, 24000, 40000)

# The exposure left set after a capture, a good general-purpose value.
resting_exposure = 12000

# Raw depth is Z along the left rectified camera's optical axis in units of
# 4.851 um, proportional with no offset. Measured 2026-10-27 by sliding the
# scanner the full 55.85 mm of its mount slot (11,514 raw units). Values wrap
# past 65535 (~318 mm), so anything farther reads as very near.
depth_unit_millimeters = 0.004851

# Frames thrown away at the start of every capture. The scanner occasionally
# delivers a frame left over from its previous stream first, which after a
# stage move shows the object at the previous pose (2 of 40 moves on the
# 2026-10-27 duck scan).
leading_frames_discarded = 1

# Per-camera calibration (intrinsics, distortion, rectification) on the scanner.
calibration_paths = ("/data/camparam/mapparamL.bin", "/data/camparam/mapparamR.bin")


class Scanner(object):

    def __init__(self, address = default_scanner_address):
        self.address = address
        self.revopoint = revopoint.Revopoint(ipAddr = address)

    def capture_exposures(self, exposures = default_exposures, frames_per_exposure = 4):
        """
        Capture frames_per_exposure depth frames at each exposure, after
        discarding the stream's first leading_frames_discarded frames.

        Returns {exposure: {"depth": (frames, 400, 640) uint16,
                            "mono": (800, 640) uint8}}, where "mono" is the
        stereo pair (left camera on top) from the last frame. The depth map is
        registered to the left image.

        Every stream is closed after its capture and the exposure is set back to
        resting_exposure, even on failure: a stream left open keeps the projector
        running.
        """
        captures = {}
        try:
            for exposure in exposures:
                self.revopoint.set_exposure(exposure)
                requested = frames_per_exposure + leading_frames_discarded
                images = self.revopoint.get_images(requested)
                self._close_streams()
                if len(images["depth"]) != requested:
                    raise RuntimeError(f"Got {len(images['depth'])} of {requested} frames at exposure {exposure}")
                captures[exposure] = {"depth" : np.array(images["depth"][leading_frames_discarded:]),
                                      "mono" : np.array(images["other"][-1])}
        finally:
            try:
                self.revopoint.set_exposure(resting_exposure)
            finally:
                self._close_streams()
        return captures

    def download_calibration(self):
        """Return {path: bytes} for the scanner's per-camera calibration files."""
        calibration = {}
        for path in calibration_paths:
            response = requests.get(f"http://{self.address}/cgi-bin/zx_cmd.cgi?download={path}", timeout = 10)
            response.raise_for_status()
            calibration[path] = response.content
        return calibration

    def firmware_version(self):
        # The scanner pads the version string with a NUL.
        return self.revopoint.get_version().replace("\x00", "").strip()

    def _close_streams(self):
        for _ in range(2):
            try:
                self.revopoint.close_streams()
            except Exception:
                pass
