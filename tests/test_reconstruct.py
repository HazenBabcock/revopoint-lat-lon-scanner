"""Tests for lat_lon_scanner.reconstruct."""
import numpy as np
import pytest
from scipy.spatial.transform import Rotation

import lat_lon_scanner.capture as capture
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


def test_upright_view_without_a_latitude_0_row():
    # The 20 degree plan (2026-10-27 bust) has rows at 10 and -10 but none at 0.
    poses = capture.scan_plan((90, 70, 50, 30, 10, -10, -20), 20)
    assert (0.0, 0.0) not in poses
    assert poses[reconstruct.nearest_upright_view(poses)] == (10.0, 0.0)
    # With a latitude 0 row, the view at exactly (0, 0) is the one used.
    poses = capture.scan_plan()
    assert poses[reconstruct.nearest_upright_view(poses)] == (0.0, 0.0)


def test_depth_band_follows_the_scanner():
    # With the scanner farther back, the band moves out with it: a band that
    # starts beyond the synthetic surface (raw ~21000-23000) leaves no depth,
    # and one that covers it keeps the same depth as the default band.
    random_generator = np.random.default_rng(4)
    frames = np.array([synthetic_frames(random_generator) for _ in range(3)]).astype(np.uint16)
    view_data = {"depth_12000" : frames}
    beyond = reconstruct.merge_exposures(view_data, [12000], depth_band_raw = (25000, 45000))
    assert np.all(np.isnan(beyond))
    default = reconstruct.merge_exposures(view_data, [12000])
    covering = reconstruct.merge_exposures(view_data, [12000], depth_band_raw = (20000, 45000))
    assert np.array_equal(np.isnan(default), np.isnan(covering))
    assert np.nanmax(np.abs(default - covering)) == 0


def mount_and_box(random_generator):
    """
    Surface points of a mount (a radius 5 mm post from 40 to 30 mm below the
    origin, then a radius 8 mm cap up to 28.5 mm below) on the z axis, with a
    box-shaped object on top. Returns (points, number of mount points).
    """
    def cylinder(radius, bottom, top, count):
        angles = random_generator.uniform(0, 2 * np.pi, count)
        radii = radius + random_generator.normal(0, 0.1, count)
        return np.column_stack([radii * np.cos(angles), radii * np.sin(angles), random_generator.uniform(bottom, top, count)])

    mount = np.concatenate([cylinder(5.0, -40.0, -30.0, 20000), cylinder(8.0, -30.0, -28.5, 5000)])
    # The box's four walls, 30 x 20 mm, from the cap up.
    count = 50000
    position = random_generator.uniform(-1, 1, count)
    wall = random_generator.integers(0, 4, count)
    x = np.where(wall < 2, np.where(wall == 0, -15.0, 15.0), 15.0 * position)
    y = np.where(wall < 2, 10.0 * position, np.where(wall == 2, -10.0, 10.0))
    box = np.column_stack([x, y, random_generator.uniform(-28.5, 15.0, count)])
    return np.concatenate([mount, box]), len(mount)


def test_mount_is_measured_from_the_turntable_axis():
    # 2026-10-27 bust: the rotation center was 0.79 mm off the turntable axis.
    # Measured from there, the post's slices spread 1.5-1.7 mm, too much to
    # count as a cylinder, and no mount was found.
    points, mount_count = mount_and_box(np.random.default_rng(5))
    up = np.array([0.0, 0.0, 1.0])
    keep, mount_top, mount_radius = reconstruct.remove_mount(points, np.zeros(3), up)
    assert mount_top == pytest.approx(-28.5)
    assert 8.0 < mount_radius < 9.5
    assert not keep[:mount_count].any()
    assert keep[mount_count:].all()
    with pytest.raises(RuntimeError):
        reconstruct.remove_mount(points, np.array([0.8, 0.0, 0.0]), up)


def rotate_about(points, axis, point, degrees):
    return Rotation.from_rotvec(np.radians(degrees) * axis).apply(points - point) + point


def test_nod_is_undone():
    # A view is made the way the stage moves the object: the turntable turns,
    # the object nods on its mount about the pivot (gain * sin(latitude),
    # the same way as the latitude), then the cradle tilts. The model must
    # bring every point back to where it started.
    model = reconstruct.StageModel([0.01, -0.02, 0.03, 0.01, -1.0, 175.0, 43.0, 176.0], [43.0, 0.0, 175.0],
                                   nod_gain_degrees = 2.0, nod_pivot_height = -28.5)
    center, _ = model.rotation_center()
    assert (model.nod_pivot - center) @ model.longitude_axis == pytest.approx(-28.5)
    to_pivot = model.nod_pivot - model.longitude_point
    assert np.linalg.norm(np.cross(to_pivot, model.longitude_axis)) == pytest.approx(0.0, abs = 1e-9)

    points = center + np.random.default_rng(6).uniform(-30, 30, (200, 3))
    for latitude, longitude in [(30.0, 45.0), (-20.0, 200.0), (90.0, 0.0)]:
        turned = rotate_about(points, model.longitude_axis, model.longitude_point, longitude)
        nodded = rotate_about(turned, model.latitude_axis, model.nod_pivot, 2.0 * np.sin(np.radians(latitude)))
        camera = rotate_about(nodded, model.latitude_axis, model.latitude_point, latitude)
        assert np.allclose(reconstruct.apply_transform(model.camera_to_object(latitude, longitude), camera), points)
