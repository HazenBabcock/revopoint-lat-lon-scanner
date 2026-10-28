"""Tests for lat_lon_scanner.reconstruct."""
import numpy as np

import lat_lon_scanner.reconstruct as reconstruct


def synthetic_frames(random_generator, shift_pixels = 0):
    """
    A smooth raw depth surface (an off-center bump, ~100-110 mm) with a little
    noise and a background hole, optionally shifted sideways the way the
    object moves in a frame taken at a different stage pose.
    """
    rows, columns = np.mgrid[0:400, 0:640]
    surface = 21000.0 + 2000.0 * np.exp(-((rows - 200) ** 2 + (columns - 300 - shift_pixels) ** 2) / (2 * 80.0 ** 2))
    surface += random_generator.normal(0, 3, surface.shape)
    surface[np.abs(columns - 300 - shift_pixels) > 150] = 0     # background outside the object reads as no depth
    return surface


def test_stale_frame_from_another_pose_is_dropped():
    # 2026-10-27 duck scan: a capture's first frame was occasionally left over
    # from the previous pose; merged in, it left curved sheets off the surface.
    random_generator = np.random.default_rng(1)
    frames = np.array([synthetic_frames(random_generator, shift_pixels = 60)] +
                      [synthetic_frames(random_generator) for _ in range(3)])
    kept = reconstruct.consistent_frames(frames)
    assert list(kept) == [1, 2, 3]


def test_consistent_frames_are_all_kept():
    random_generator = np.random.default_rng(2)
    frames = np.array([synthetic_frames(random_generator) for _ in range(4)])
    assert list(reconstruct.consistent_frames(frames)) == [0, 1, 2, 3]


def test_merge_ignores_the_stale_frame():
    random_generator = np.random.default_rng(3)
    good = [synthetic_frames(random_generator) for _ in range(3)]
    stale = synthetic_frames(random_generator, shift_pixels = 60)
    view_data = {"depth_12000" : np.array([stale] + good).astype(np.uint16),
                 "depth_24000" : np.array(good + [good[0]]).astype(np.uint16)}
    merged = reconstruct.merge_exposures(view_data, [12000, 24000])
    # Where only the stale frame saw anything (it is shifted right), there must be no depth.
    assert np.all(np.isnan(merged[:, 451:510]))
