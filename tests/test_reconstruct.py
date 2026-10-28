"""Tests for lat_lon_scanner.reconstruct."""
import json
import struct

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


def write_run(run_directory, view_depths, exposure = 12000):
    """
    A capture run on disk: run.json, the left camera's calibration file (an
    unrotated camera, f 896, center (320, 200)), and one view per raw depth
    stack in view_depths, at latitude 0, 30, 60, ...
    """
    (run_directory / "calibration").mkdir(parents = True)
    camera_matrix = np.array([[896.0, 0, 320], [0, 896.0, 200], [0, 0, 1]])
    floats = list(np.eye(3).ravel()) + [320.0, 200.0, 896.0, 896.0] + [0.0] * 12 + list(np.linalg.inv(camera_matrix).ravel())
    (run_directory / "calibration" / "mapparamL.bin").write_bytes(struct.pack("<3i", 400, 640, 5) + struct.pack(f"<{len(floats)}f", *floats))
    poses = [(30.0 * index, 0.0) for index in range(len(view_depths))]
    (run_directory / "run.json").write_text(json.dumps({"poses" : poses, "exposures" : [exposure]}))
    for index, depth in enumerate(view_depths):
        np.savez(run_directory / f"view_{index:03d}.npz", **{f"depth_{exposure}" : depth.astype(np.uint16),
                                                              f"mono_{exposure}" : np.full((800, 640), 128, np.uint8)})
    return poses


def test_view_without_depth_is_left_out(tmp_path):
    # 2026-10-28: a white nylon part, scanned at exposures that saturate it,
    # left a view with no depth at all, and the normal estimation stopped the
    # whole reconstruction. Such a view must be left out instead.
    random_generator = np.random.default_rng(9)
    good = np.array([synthetic_frames(random_generator) for _ in range(3)])
    poses = write_run(tmp_path / "run", [good, np.zeros_like(good), good])
    loaded_poses, view_points, view_normals, _, view_color_frames = reconstruct.load_run_views(tmp_path / "run")
    assert loaded_poses == [poses[0], poses[2]]
    assert len(view_points) == len(view_normals) == len(view_color_frames) == 2
    assert all(len(points) > 10000 for points in view_points)
    write_run(tmp_path / "empty_run", [np.zeros_like(good)])
    with pytest.raises(RuntimeError):
        reconstruct.load_run_views(tmp_path / "empty_run")


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


def box_surface(random_generator, count = 8000):
    """Points on the faces of a 30 x 20 x 40 mm box centered on the origin."""
    half_sizes = np.array([15.0, 10.0, 20.0])
    points = random_generator.uniform(-1, 1, (count, 3)) * half_sizes
    face_axis = random_generator.integers(0, 3, count)
    points[np.arange(count), face_axis] = np.sign(random_generator.uniform(-1, 1, count)) * half_sizes[face_axis]
    return points


def test_refinement_does_not_move_the_object_with_one_view():
    # 2026-10-27 bust: the refinement pinned the first view (the +90 view),
    # which was itself 2 mm off, so everything else moved with it and the mount
    # was no longer found on the turntable axis. One shifted view must be
    # brought back without moving the object as a whole.
    surface = box_surface(np.random.default_rng(7))
    poses = [(0.0, longitude) for longitude in range(0, 120, 20)]
    shift = np.array([0.6, 0.3, 0.0])
    clouds = [surface + shift] + [surface.copy() for _ in poses[1:]]
    corrections = reconstruct.refine_poses(clouds, poses)
    corrected = [reconstruct.apply_transform(correction, cloud) for correction, cloud in zip(corrections, clouds)]
    centroids = np.array([cloud.mean(axis = 0) for cloud in corrected])
    assert np.abs(centroids[0] - centroids[1]).max() < 0.05
    assert np.abs(centroids.mean(axis = 0) - np.mean([cloud.mean(axis = 0) for cloud in clouds], axis = 0)).max() < 0.05


def plane(x_range, y_range, z, spacing):
    x, y = np.meshgrid(np.arange(*x_range, spacing), np.arange(*y_range, spacing))
    return np.column_stack([x.ravel(), y.ravel(), np.full(x.size, z)])


def test_color_camera_occlusion():
    # The color camera sits 15 mm to the right of the left camera, so a nearer
    # square hides a different part of the wall from it. Those wall points must
    # get no color, the rest the frame's, and a point facing away gets none.
    color_camera = {"matrix" : np.array([[1000.0, 0, 640], [0, 1000.0, 400], [0, 0, 1]]), "distortion" : np.zeros(5),
                    "rotation" : np.eye(3), "translation" : np.array([-15.0, 0, 0])}
    square = plane((-5, 5), (-5, 5), 100.0, 0.15)
    wall = plane((-30, 30), (-20, 20), 150.0, 0.2)
    points = np.concatenate([square, wall])
    normals = np.tile([0.0, 0.0, -1.0], (len(points), 1))
    normals[-1] = [0.0, 0.0, 1.0]
    frame = np.full((800, 1280, 3), 128, np.uint8)
    colors, facing = reconstruct.view_colors(points, normals, frame, color_camera, spacing_millimeters = 0.2)
    seen = ~np.isnan(colors[:, 0])
    wall_seen = seen[len(square):]
    # Seen from the color camera, the square hides the wall at -15 < x < 0, |y| < 7.5.
    hidden = (wall[:, 0] > -13) & (wall[:, 0] < -2) & (np.abs(wall[:, 1]) < 6)
    # The left camera can't see the wall at 0 < x < 7.5 behind the square, but the color camera can.
    behind_square_from_the_left = (wall[:, 0] > 2) & (wall[:, 0] < 6) & (np.abs(wall[:, 1]) < 6)
    far_away = np.abs(wall[:, 0]) > 20
    far_away[-1] = False
    assert not wall_seen[hidden].any()
    assert wall_seen[behind_square_from_the_left].all()
    assert wall_seen[far_away].all()
    assert not seen[-1] and facing[-1] < 0
    assert seen[:len(square)].all()
    expected = reconstruct.srgb_to_linear(np.full(3, 128 / 255)) @ reconstruct.color_camera_to_linear_srgb
    assert np.allclose(colors[seen], expected)


def test_photo_colors_weight_frames_by_facing():
    # A square seen head-on in one view (frame value 100) and turned 60 degrees
    # in another (frame value 200): its center takes the mean of the two colors,
    # weighted 1 and cos(60) = 0.5.
    color_camera = {"matrix" : np.array([[1000.0, 0, 640], [0, 1000.0, 400], [0, 0, 1]]), "distortion" : np.zeros(5),
                    "rotation" : np.eye(3), "translation" : np.zeros(3)}
    square = plane((-5, 5.01), (-5, 5.01), 100.0, 0.1)
    normals = np.tile([0.0, 0.0, -1.0], (len(square), 1))
    turned = np.eye(4)
    turned[:3, :3] = Rotation.from_euler("y", 60, degrees = True).as_matrix()
    turned[:3, 3] = np.array([0.0, 0.0, 100.0]) - turned[:3, :3] @ np.array([0.0, 0.0, 100.0])
    color_views = [(np.eye(4), np.full((800, 1280, 3), 100, np.uint8), color_camera),
                   (np.linalg.inv(turned), np.full((800, 1280, 3), 200, np.uint8), color_camera)]
    colors, frame_counts = reconstruct.photo_colors(square, normals, color_views)
    center = np.argmin(np.linalg.norm(square - [0.0, 0.0, 100.0], axis = 1))
    head_on, at_60 = (reconstruct.srgb_to_linear(np.full(3, value / 255)) @ reconstruct.color_camera_to_linear_srgb
                      for value in (100, 200))
    assert frame_counts[center] == 2
    assert np.allclose(colors[center], (head_on + 0.5 * at_60) / 1.5)


def test_mesh_bridges_small_holes_and_trims_large_ones():
    # A sphere of points with a hole 1.6 mm across, like those left on the
    # duck's beak (no part of them more than 1 mm from a point), and one 16 mm
    # across, like where the mount is cut away. The mesh must cover the small
    # hole, leave most of the large one open, and stay on the sphere.
    radius, count = 15.0, 30000
    golden_angle = np.pi * (3 - np.sqrt(5))
    heights = 1 - 2 * (np.arange(count) + 0.5) / count
    directions = np.column_stack([np.sqrt(1 - heights ** 2) * np.cos(golden_angle * np.arange(count)),
                                  np.sqrt(1 - heights ** 2) * np.sin(golden_angle * np.arange(count)), heights])
    small_hole, large_hole = np.array([1.0, 0.0, 0.0]), np.array([0.0, 0.0, -1.0])

    def within(points, direction, distance):
        return np.arccos(np.clip(points @ direction / np.linalg.norm(points, axis = 1), -1, 1)) * radius < distance

    keep = ~within(directions, small_hole, 0.8) & ~within(directions, large_hole, 8.0)
    cloud = reconstruct.open3d.geometry.PointCloud(reconstruct.open3d.utility.Vector3dVector(radius * directions[keep]))
    cloud.normals = reconstruct.open3d.utility.Vector3dVector(directions[keep])
    mesh, _ = reconstruct.build_mesh(cloud, voxel_size = 0.3)
    vertices = np.asarray(mesh.vertices)
    assert within(vertices, small_hole, 0.4).sum() >= 3
    assert not within(vertices, large_hole, 6.0).any()
    error = np.abs(np.linalg.norm(vertices, axis = 1) - radius)
    assert error[~within(vertices, large_hole, 9.0)].max() < 0.05
    # The free edge left around the opening curls a little (0.15 mm here).
    assert error.max() < 0.25


def test_color_projection_matches_opencv():
    cv2 = pytest.importorskip("cv2")
    color_camera = {"matrix" : np.array([[1779.1, 0, 637.7], [0, 1782.5, 410.8], [0, 0, 1]]),
                    "distortion" : np.array([0.2233, -0.5897, -0.00096, -0.00145, 0.01]),
                    "rotation" : Rotation.from_euler("y", -9.6, degrees = True).as_matrix(),
                    "translation" : np.array([-14.44, 0.03, -3.05])}
    points = np.random.default_rng(8).uniform([-30, -20, 90], [30, 20, 160], (500, 3))
    pixels, depth = reconstruct.project_to_color_camera(points, color_camera)
    expected, _ = cv2.projectPoints(points, cv2.Rodrigues(color_camera["rotation"])[0], color_camera["translation"],
                                    color_camera["matrix"], color_camera["distortion"])
    assert np.allclose(pixels, expected.reshape(-1, 2), atol = 1e-6)
    assert np.allclose(depth, (points @ color_camera["rotation"].T + color_camera["translation"])[:, 2])
