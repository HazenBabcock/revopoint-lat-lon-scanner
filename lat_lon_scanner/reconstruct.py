"""
Build a point cloud of the scanned object from a capture run.

    python -m lat_lon_scanner.reconstruct ~/Data/revopoint/<date>/<name>

Steps:

1. Each view's exposures are merged into one depth map (the median of the
   valid frames, dropping pixels where exposures disagree), pixels near holes
   and depth jumps are dropped, and the rest are turned into points in
   millimetres using the scanner's rectified left camera. Surfaces seen nearly
   edge-on are dropped too.
2. The two stage axes are fitted in the camera frame by minimizing the
   misalignment between neighboring views once each is rotated back by its
   commanded angles. The object frame is the camera frame at latitude 0,
   longitude 0. Then a nod of the object that grows with the gravity load,
   sin(latitude), about a pivot on the turntable axis is fitted (an object on
   a soft mount tips as the cradle tilts), and the axes are refitted with it.
3. Each view's pose is refined with a joint (multiway) ICP that starts from
   the fitted model (--no-refine skips it). An object on a soft mount can also
   shift in single views: the bust shifted by up to 2 mm during one row,
   likely on its gel pad, and the refinement merged the layers that left
   (misalignment 0.426 -> 0.324 mm). On the duck it made no difference.
4. All views are merged and cropped to the object: a sphere around the
   rotation center, with the post and cap it sits on cut away. The result is
   thinned, cleaned of stray points and disconnected fragments, and given
   surface normals.

Results go to <run>/reconstruction/ (or --output-name): point_cloud.ply
(points, normals, and the scanner's grayscale brightness as color),
stage_axes.json, and preview_*.png renderings. --extra-runs merges in more
runs of the same object, such as a close-up of one area at a longer exposure,
placed using the main run's fitted axes.
"""
import argparse
import json
import pathlib
import struct
import time
import warnings

import numpy as np
import open3d
import scipy.linalg
import scipy.ndimage
import scipy.optimize
import scipy.spatial
from scipy.spatial.transform import Rotation

import lat_lon_scanner.scanner as scanner_module

# Depth outside 73-194 mm is the wall, wrapped values or noise, with the
# scanner at the front of its mount slot (rotation center ~120 mm away). With
# it farther back, pass a band that follows it (--depth-range).
default_depth_band_raw = (15000, 40000)

# Drop pixels where exposures disagree by more than ~1 mm.
exposure_disagreement_raw = 200

# A frame is left out of its capture if more than these fractions of its
# pixels are missing from the other frames, or differ from them by more than
# ~0.25 mm (see consistent_frames).
frame_disagreement_raw = 50
frame_unique_fraction_limit = 0.10
frame_disagreement_fraction_limit = 0.15

# The exposure whose left image supplies each point's brightness: long enough
# for the duck's yellow, short enough not to saturate most surfaces.
brightness_exposure = 24000

# Surfaces tilted more than ~75 degrees from facing the camera are dropped.
minimum_facing_cosine = 0.25

default_crop_radius_millimeters = 32.0
default_voxel_millimeters = 0.3


def read_rectified_camera_matrix(path):
    """
    Return the rectified camera matrix from a scanner mapparam file. The file
    holds int32 height, width, count, then float32: a 3x3 matrix, cx, cy, fx,
    fy, 5 distortion coefficients, 7 zeros, and inv(K_rectified R_rectified).
    """
    data = pathlib.Path(path).read_bytes()
    floats = np.array(struct.unpack(f"<{(len(data) - 12) // 4}f", data[12:]))
    upper, rotation = scipy.linalg.rq(np.linalg.inv(floats[25:34].reshape(3, 3)))
    signs = np.diag(np.sign(np.diag(upper)))
    camera_matrix = upper @ signs
    return camera_matrix / camera_matrix[2, 2]


def consistent_frames(frames, depth_band_raw = default_depth_band_raw):
    """
    Return the indices of the depth frames that agree with the rest of their
    capture. The scanner occasionally delivers a frame left over from its
    previous stream as the first frame, which after a stage move shows the
    object at the previous pose; merged in, it leaves curved sheets off the
    surface. Such a frame has many pixels the other frames lack, or that
    disagree with them, where real frames match to a few raw units. On the
    2026-10-27 duck scans the two stale frames scored 48-56% unique pixels
    against at most 1.3% for the other 550 frames. Needs three or more frames;
    with fewer, all are kept.
    """
    depth = frames.astype(float)
    depth[(depth < depth_band_raw[0]) | (depth > depth_band_raw[1])] = np.nan
    if len(depth) < 3:
        return np.arange(len(depth))
    kept = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        for index in range(len(depth)):
            valid = np.isfinite(depth[index])
            if not valid.any():
                continue
            others = np.nanmedian(np.delete(depth, index, axis = 0), axis = 0)
            unique = np.mean(np.isnan(others[valid]))
            overlap = valid & np.isfinite(others)
            disagreeing = (np.mean(np.abs(depth[index][overlap] - others[overlap]) > frame_disagreement_raw)
                           if overlap.any() else 1.0)
            if (unique <= frame_unique_fraction_limit) and (disagreeing <= frame_disagreement_fraction_limit):
                kept.append(index)
    return np.array(kept, dtype = int)


def merge_exposures(view_data, exposures, depth_band_raw = default_depth_band_raw):
    """
    Return one depth map (raw units, NaN where invalid or outside
    depth_band_raw) from all exposures and frames of a view, leaving out frames
    that disagree with the rest of their capture (see consistent_frames).
    """
    per_exposure = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        for exposure in exposures:
            frames = view_data[f"depth_{exposure}"]
            frames = frames[consistent_frames(frames, depth_band_raw)].astype(float)
            if len(frames) == 0:
                continue
            frames[(frames < depth_band_raw[0]) | (frames > depth_band_raw[1])] = np.nan
            per_exposure.append(np.nanmedian(frames, axis = 0))
        if not per_exposure:
            # Nothing in the band at any exposure (e.g. the object is out of range).
            return np.full(view_data[f"depth_{exposures[0]}"].shape[1:], np.nan)
        stack = np.array(per_exposure)
        merged = np.nanmedian(stack, axis = 0)
        spread = np.nanmax(stack, axis = 0) - np.nanmin(stack, axis = 0)
    merged[spread > exposure_disagreement_raw] = np.nan
    return merged


def remove_depth_edges(depth, jump_millimeters = 2.0, margin_pixels = 2):
    """
    Return a copy of a raw depth map without the pixels near holes and depth
    jumps. Structured light blends the object and the background at silhouette
    edges, which shows up as thin curved sheets floating off the surface.
    """
    valid = np.isfinite(depth)
    filled = np.where(valid, depth, 0.0)
    jump_raw = jump_millimeters / scanner_module.depth_unit_millimeters
    local_maximum = scipy.ndimage.maximum_filter(np.where(valid, depth, -np.inf), size = 3)
    local_minimum = scipy.ndimage.minimum_filter(np.where(valid, depth, np.inf), size = 3)
    jumps = valid & ((local_maximum - filled > jump_raw) | (filled - local_minimum > jump_raw))
    trusted = scipy.ndimage.binary_erosion(valid & ~jumps, iterations = margin_pixels)
    return np.where(trusted, depth, np.nan)


def depth_to_points(depth, camera_matrix):
    """Return (points in mm, rows, columns) for the valid pixels of a raw depth map."""
    rows, columns = np.nonzero(np.isfinite(depth))
    z = depth[rows, columns] * scanner_module.depth_unit_millimeters
    focal, center_u, center_v = camera_matrix[0, 0], camera_matrix[0, 2], camera_matrix[1, 2]
    points = np.column_stack([(columns - center_u) * z / focal, (rows - center_v) * z / focal, z])
    return points, rows, columns


def axis_from_angles(base, tilt_angles):
    """Unit vector near the axis-aligned unit vector 'base', tilted by two small angles (radians)."""
    first_perpendicular = np.roll(np.abs(base), 1)
    second_perpendicular = np.cross(base, first_perpendicular)
    tilt = Rotation.from_rotvec(tilt_angles[0] * first_perpendicular + tilt_angles[1] * second_perpendicular)
    return tilt.apply(base)


class StageModel(object):
    """
    Where the stage's axes are in the camera frame (x right, y down, z away),
    and how far the object nods as the cradle tilts.

    A point fixed to the object, at p in the object frame (the camera frame at
    latitude 0, longitude 0), appears at

        R_lat(latitude) [N(latitude) [R_lon(longitude) (p - lon_point) + lon_point - pivot] + pivot - lat_point] + lat_point

    The latitude axis is near +x (+latitude tips the top toward the camera) and
    the longitude axis near up, -y (+longitude is counterclockwise from above).
    Parameters: two tilt angles for each axis, the (y, z) of the latitude axis
    where x = reference x, and the (x, z) of the longitude axis where y =
    reference y.

    N(latitude) is the nod: a rotation by nod_gain_degrees * sin(latitude),
    the same way as the latitude, about a line parallel to the latitude axis
    through the pivot, a point on the turntable axis nod_pivot_height mm above
    the rotation center (negative is below). It follows the gravity load as
    the cradle tilts: on 2026-10-27 the bust, held on its cap by a ~0.5 mm gel
    pad 28.5 mm below the rotation center, tipped toward the scanner at high
    latitudes and away at negative ones, 2.5 degrees x sin(latitude) about a
    pivot 8.5 mm below the center. A nod about one pivot plus a slide toward
    the scanner looks like a nod about another, so the pivot can't tell the
    object tipping and sliding on its mount from the cradle yielding.
    """
    latitude_base = np.array([1.0, 0.0, 0.0])
    longitude_base = np.array([0.0, -1.0, 0.0])

    def __init__(self, parameters, reference_point, nod_gain_degrees = 0.0, nod_pivot_height = 0.0):
        self.parameters = np.asarray(parameters, float)
        self.reference_point = np.asarray(reference_point, float)
        self.latitude_axis = axis_from_angles(self.latitude_base, self.parameters[0:2])
        self.longitude_axis = axis_from_angles(self.longitude_base, self.parameters[2:4])
        self.latitude_point = np.array([self.reference_point[0], self.parameters[4], self.parameters[5]])
        self.longitude_point = np.array([self.parameters[6], self.reference_point[1], self.parameters[7]])
        self.nod_gain_degrees = float(nod_gain_degrees)
        self.nod_pivot_height = float(nod_pivot_height)
        self.nod_pivot = self.closest_points()[1] + self.nod_pivot_height * self.longitude_axis

    def camera_to_object(self, latitude, longitude):
        """Return the 4x4 transform taking camera-frame points of a view into the object frame."""
        latitude_rotation = Rotation.from_rotvec(np.radians(latitude) * self.latitude_axis).inv().as_matrix()
        longitude_rotation = Rotation.from_rotvec(np.radians(longitude) * self.longitude_axis).inv().as_matrix()
        nod_degrees = self.nod_gain_degrees * np.sin(np.radians(latitude))
        nod_rotation = Rotation.from_rotvec(np.radians(nod_degrees) * self.latitude_axis).inv().as_matrix()
        rotation = longitude_rotation @ nod_rotation @ latitude_rotation
        translation = (longitude_rotation @ (nod_rotation @ (self.latitude_point - latitude_rotation @ self.latitude_point
                                                             - self.nod_pivot) + self.nod_pivot - self.longitude_point)
                       + self.longitude_point)
        transform = np.eye(4)
        transform[:3, :3] = rotation
        transform[:3, 3] = translation
        return transform

    def closest_points(self):
        """Return the points where the latitude and longitude axes come closest, on each."""
        normal = np.cross(self.latitude_axis, self.longitude_axis)
        matrix = np.column_stack([self.latitude_axis, -self.longitude_axis, normal])
        along_latitude, along_longitude, _ = np.linalg.solve(matrix, self.longitude_point - self.latitude_point)
        return (self.latitude_point + along_latitude * self.latitude_axis,
                self.longitude_point + along_longitude * self.longitude_axis)

    def rotation_center(self):
        """Return (the midpoint of the axes' closest approach, the gap between them in mm)."""
        on_latitude, on_longitude = self.closest_points()
        return 0.5 * (on_latitude + on_longitude), float(np.linalg.norm(on_latitude - on_longitude))


def apply_transform(transform, points):
    return points @ transform[:3, :3].T + transform[:3, 3]


def view_direction(latitude, longitude):
    """Unit vector for a view on the sphere of stage angles, for measuring how far apart views are."""
    latitude, longitude = np.radians(latitude), np.radians(longitude)
    return np.array([np.cos(latitude) * np.cos(longitude), np.cos(latitude) * np.sin(longitude), np.sin(latitude)])


def neighbor_pairs(poses, maximum_arc_degrees = 40.0):
    directions = np.array([view_direction(*pose) for pose in poses])
    arcs = np.degrees(np.arccos(np.clip(directions @ directions.T, -1, 1)))
    return [(i, j) for i in range(len(poses)) for j in range(len(poses)) if i != j and arcs[i, j] <= maximum_arc_degrees]


def nearest_upright_view(poses):
    """
    Index of the view nearest latitude 0, longitude 0 (the first one if several
    are equally near). Scan plans without a latitude 0 row, such as the 20
    degree plan's 10 and -10 rows, have no view at exactly (0, 0).
    """
    upright = view_direction(0.0, 0.0)
    return int(np.argmax([view_direction(*pose) @ upright for pose in poses]))


def downsample(points, voxel_size):
    cloud = open3d.geometry.PointCloud(open3d.utility.Vector3dVector(points))
    return np.asarray(cloud.voxel_down_sample(voxel_size).points)


def misalignment(object_clouds, pairs, keep_fraction, cap_millimeters = 5.0):
    """Mean over neighbor pairs of the trimmed mean nearest-neighbor distance (mm)."""
    trees = [scipy.spatial.cKDTree(cloud) for cloud in object_clouds]
    costs = []
    for i, j in pairs:
        distances, _ = trees[j].query(object_clouds[i], distance_upper_bound = cap_millimeters)
        distances = np.sort(np.minimum(distances, cap_millimeters))
        costs.append(np.mean(distances[:max(1, int(keep_fraction * len(distances)))]))
    return float(np.mean(costs))


def estimate_center(upright_points):
    """
    Rough rotation center from the view at (or nearest) latitude 0, longitude
    0: the largest cluster's middle, pushed back by half its width since only
    its front is seen.
    """
    cloud = open3d.geometry.PointCloud(open3d.utility.Vector3dVector(upright_points)).voxel_down_sample(0.7)
    labels = np.array(cloud.cluster_dbscan(eps = 2.0, min_points = 10))
    largest = np.asarray(cloud.points)[labels == np.argmax(np.bincount(labels[labels >= 0]))]
    width = np.percentile(largest[:, 0], 98) - np.percentile(largest[:, 0], 2)
    return np.array([np.median(largest[:, 0]), np.median(largest[:, 1]), np.percentile(largest[:, 2], 2) + width / 2])


def fit_stage_model(view_points, poses, center_guess, crop_radius):
    """
    Fit the StageModel: the axes by minimizing the misalignment between
    neighboring views, then the nod with views farther apart compared as well,
    then the axes again with that nod.

    One latitude row's nod barely differs from the next row's, but across
    several rows it adds up (1-2 mm between the bust's -20 and +50 rows), so
    the nod is fitted on views up to nod_arc_degrees apart, scored on the
    closest nod_keep_fraction of each pair's points since far-apart views
    overlap less. On the bust, this score fell 11% for the nod against 6% for
    the neighbors-only one.
    """
    nod_arc_degrees, nod_keep_fraction = 70.0, 0.4
    pairs = neighbor_pairs(poses)
    parameters = np.array([0, 0, 0, 0, center_guess[1], center_guess[2], center_guess[0], center_guess[2]], float)
    nod = np.zeros(2)

    def place(clouds, trial_parameters, trial_nod):
        model = StageModel(trial_parameters, center_guess, *trial_nod)
        return [apply_transform(model.camera_to_object(*pose), cloud) for cloud, pose in zip(clouds, poses)]

    def fit_axes(clouds, keep_fraction, label):
        cost = lambda trial_parameters: misalignment(place(clouds, trial_parameters, nod), pairs, keep_fraction)
        start_cost = cost(parameters)
        result = scipy.optimize.minimize(cost, parameters, method = "Powell",
                                         options = {"xtol" : 1e-3, "ftol" : 1e-5, "maxfev" : 4000})
        print(f"  {label}: misalignment {start_cost:.3f} -> {result.fun:.3f} mm", flush = True)
        return result.x, float(result.fun)

    for voxel_size, keep_fraction in [(1.5, 0.5), (0.8, 0.6)]:
        clouds = [downsample(points[np.linalg.norm(points - center_guess, axis = 1) < crop_radius], voxel_size)
                  for points in view_points]
        parameters, fit_misalignment = fit_axes(clouds, keep_fraction, f"axes fit at {voxel_size} mm")

    # The pivot starts halfway down the crop sphere, near where the mount usually is.
    nod_pairs = neighbor_pairs(poses, nod_arc_degrees)
    nod_cost = lambda trial_nod: misalignment(place(clouds, parameters, trial_nod), nod_pairs, nod_keep_fraction)
    start_cost = nod_cost(nod)
    result = scipy.optimize.minimize(nod_cost, [0.0, -0.5 * crop_radius], method = "Powell",
                                     bounds = [(-10.0, 10.0), (-crop_radius, crop_radius)],
                                     options = {"xtol" : 1e-2, "ftol" : 1e-5, "maxfev" : 300})
    nod = result.x
    print(f"  nod fit on views up to {nod_arc_degrees:.0f} degrees apart: misalignment {start_cost:.3f} -> {result.fun:.3f} mm, "
          f"nod {nod[0]:+.2f} degrees x sin(latitude) about a pivot {nod[1]:+.1f} mm along the turntable axis "
          f"from the rotation center", flush = True)
    parameters, fit_misalignment = fit_axes(clouds, keep_fraction, "axes refit with the nod")
    return StageModel(parameters, center_guess, *nod), fit_misalignment


def refine_poses(object_clouds, poses, voxel_size = 0.8, maximum_distance = 1.0):
    """
    Joint (multiway) ICP over neighboring views. Returns a small correction
    transform for each view, applied after the stage model.

    The corrections are anchored so that on average they leave the object where
    the stage model put it, keeping the mount on the fitted turntable axis for
    remove_mount. Pinning one view instead moves everything with that view's
    own error: on the 2026-10-27 bust, pinning the +90 view shifted the rest
    about 2 mm and the mount was no longer found.
    """
    clouds = []
    for points in object_clouds:
        cloud = open3d.geometry.PointCloud(open3d.utility.Vector3dVector(points)).voxel_down_sample(voxel_size)
        cloud.estimate_normals(open3d.geometry.KDTreeSearchParamHybrid(radius = 3 * voxel_size, max_nn = 30))
        clouds.append(cloud)
    pose_graph = open3d.pipelines.registration.PoseGraph()
    for _ in clouds:
        pose_graph.nodes.append(open3d.pipelines.registration.PoseGraphNode(np.eye(4)))
    estimation = open3d.pipelines.registration.TransformationEstimationPointToPlane()
    for i, j in neighbor_pairs(poses):
        if i >= j:
            continue
        result = open3d.pipelines.registration.registration_icp(clouds[i], clouds[j], maximum_distance, np.eye(4), estimation)
        information = open3d.pipelines.registration.get_information_matrix_from_point_clouds(
            clouds[i], clouds[j], maximum_distance, result.transformation)
        pose_graph.edges.append(open3d.pipelines.registration.PoseGraphEdge(
            i, j, result.transformation, information, uncertain = True))
    option = open3d.pipelines.registration.GlobalOptimizationOption(
        max_correspondence_distance = maximum_distance, edge_prune_threshold = 0.25, reference_node = 0)
    with open3d.utility.VerbosityContextManager(open3d.utility.VerbosityLevel.Error):
        open3d.pipelines.registration.global_optimization(
            pose_graph, open3d.pipelines.registration.GlobalOptimizationLevenbergMarquardt(),
            open3d.pipelines.registration.GlobalOptimizationConvergenceCriteria(), option)
    corrections = [np.array(node.pose) for node in pose_graph.nodes]
    average = np.eye(4)
    average[:3, :3] = Rotation.from_matrix([correction[:3, :3] for correction in corrections]).mean().as_matrix()
    average[:3, 3] = np.mean([correction[:3, 3] for correction in corrections], axis = 0)
    return [np.linalg.inv(average) @ correction for correction in corrections]


def remove_mount(points, axis_point, up, slice_millimeters = 0.5):
    """
    Return (mask keeping the object, mount top height, mount radius) to drop
    the post and cap the object sits on. axis_point is the point on the
    turntable axis level with the rotation center; heights are measured from it
    along up.

    The mount is a stack of cylinders on the turntable axis, and a slice through
    a cylinder wall puts every point at nearly the same distance from the axis,
    unlike a slice through the object. The post is the longest unbroken run of
    cylinder-like slices below the rotation center; from its top the walk up
    continues through cylinder-like slices (tolerating short transitions such
    as a step from post to cap) and the object starts where they stop.
    Everything well below that is dropped, and just above it anything inside
    the mount's radius.
    """
    height = (points - axis_point) @ up
    radial = np.linalg.norm((points - axis_point) - np.outer(height, up), axis = 1)
    slice_bottoms = np.arange(np.floor(height.min()), 0.0, slice_millimeters)
    spreads, outer_radii = [], []
    for bottom in slice_bottoms:
        in_slice = (height >= bottom) & (height < bottom + slice_millimeters)
        if in_slice.sum() < 30:
            spreads.append(np.inf)
            outer_radii.append(np.inf)
            continue
        low, high = np.percentile(radial[in_slice], [10, 90])
        spreads.append(high - low)
        outer_radii.append(high)
    spreads, outer_radii = np.array(spreads), np.array(outer_radii)
    cylinder_like = spreads < 1.0

    # Longest run of consecutive cylinder-like slices: the post.
    post_start, post_length, run_start = 0, 0, None
    for index, is_cylinder in enumerate(list(cylinder_like) + [False]):
        if is_cylinder and run_start is None:
            run_start = index
        elif not is_cylinder and run_start is not None:
            if index - run_start > post_length:
                post_start, post_length = run_start, index - run_start
            run_start = None
    if post_length == 0:
        raise RuntimeError("No cylinder-like mount found below the rotation center")

    index = post_start + post_length - 1
    last_cylinder_index = index
    misses = 0
    while index + 1 < len(slice_bottoms) and misses < 3:
        index += 1
        if cylinder_like[index]:
            last_cylinder_index = index
            misses = 0
        else:
            misses += 1
    walked = slice(post_start, last_cylinder_index + 1)
    mount_radius = float(np.max(outer_radii[walked][cylinder_like[walked]])) + 0.5
    mount_top = float(slice_bottoms[last_cylinder_index] + slice_millimeters)
    keep = (height >= mount_top - 1.0) & ~((height < mount_top + 1.5) & (radial < mount_radius))
    return keep, mount_top, mount_radius


def keep_largest_cluster(cloud, neighbor_distance):
    """Drop everything not connected (within neighbor_distance) to the largest piece."""
    labels = np.array(cloud.cluster_dbscan(eps = neighbor_distance, min_points = 5))
    largest = np.argmax(np.bincount(labels[labels >= 0]))
    return cloud.select_by_index(np.nonzero(labels == largest)[0])


def render_preview(points, normals, colors, eye_direction, up_hint, image_size = 600, point_millimeters = 0.3,
                   light_direction = None):
    """
    Orthographic rendering: each point is splatted as a square about
    point_millimeters across (so neighboring points leave no gaps), nearest
    point wins, shaded by its normal. Returns an (image_size, image_size, 3) uint8 image.
    """
    eye_direction = eye_direction / np.linalg.norm(eye_direction)
    right = np.cross(up_hint, eye_direction)
    right /= np.linalg.norm(right)
    up = np.cross(eye_direction, right)
    centered = points - points.mean(axis = 0)
    horizontal, vertical, toward_eye = centered @ right, centered @ up, centered @ eye_direction
    extent = 1.1 * max(np.abs(horizontal).max(), np.abs(vertical).max())
    columns = ((horizontal / extent + 1) * 0.5 * (image_size - 1)).astype(int)
    rows = ((1 - (vertical / extent + 1) * 0.5) * (image_size - 1)).astype(int)
    if light_direction is None:
        light_direction = eye_direction + 0.5 * up + 0.3 * right
    light_direction = light_direction / np.linalg.norm(light_direction)
    shade = 0.25 + 0.75 * np.clip(normals @ light_direction, 0, 1)
    shaded = np.clip(colors * shade[:, None], 0, 1)

    image = np.full((image_size, image_size, 3), 1.0)
    depth_buffer = np.full((image_size, image_size), -np.inf)
    order = np.argsort(toward_eye)    # far to near, so nearer points overwrite
    millimeters_per_pixel = 2 * extent / (image_size - 1)
    splat_pixels = max(2, int(np.ceil(1.5 * point_millimeters / millimeters_per_pixel)))
    offsets = range(-(splat_pixels // 2), splat_pixels - splat_pixels // 2)
    for row_offset in offsets:
        for column_offset in offsets:
            splat_rows = np.clip(rows[order] + row_offset, 0, image_size - 1)
            splat_columns = np.clip(columns[order] + column_offset, 0, image_size - 1)
            nearer = toward_eye[order] > depth_buffer[splat_rows, splat_columns]
            image[splat_rows[nearer], splat_columns[nearer]] = shaded[order][nearer]
            depth_buffer[splat_rows[nearer], splat_columns[nearer]] = toward_eye[order][nearer]
    return (255 * image).astype(np.uint8)


def load_run_views(run_directory, depth_band_raw = default_depth_band_raw):
    """
    Return (poses, points, normals, brightness) for each view of a run, in the
    camera frame, with edge pixels and edge-on surfaces already dropped.
    """
    run_directory = pathlib.Path(run_directory)
    information = json.loads((run_directory / "run.json").read_text())
    poses = [tuple(pose) for pose in information["poses"]]
    camera_matrix = read_rectified_camera_matrix(run_directory / "calibration" / "mapparamL.bin")
    # Brightness comes from brightness_exposure, or the nearest exposure the run has.
    exposure_for_brightness = min(information["exposures"], key = lambda exposure: abs(exposure - brightness_exposure))

    view_points, view_normals, view_brightness = [], [], []
    for view_index in range(len(poses)):
        view_data = np.load(run_directory / f"view_{view_index:03d}.npz")
        depth = remove_depth_edges(merge_exposures(view_data, information["exposures"], depth_band_raw))
        points, rows, columns = depth_to_points(depth, camera_matrix)
        # Normals in the camera frame, facing the camera that saw them.
        cloud = open3d.geometry.PointCloud(open3d.utility.Vector3dVector(points))
        cloud.estimate_normals(open3d.geometry.KDTreeSearchParamHybrid(radius = 1.0, max_nn = 30))
        cloud.orient_normals_towards_camera_location(np.zeros(3))
        normals = np.asarray(cloud.normals)
        # Drop surfaces seen nearly edge-on, where the depth is least reliable.
        facing = np.sum(normals * -points, axis = 1) / np.linalg.norm(points, axis = 1) > minimum_facing_cosine
        view_points.append(points[facing])
        view_normals.append(normals[facing])
        view_brightness.append(view_data[f"mono_{exposure_for_brightness}"][:400][rows[facing], columns[facing]] / 255.0)
    return poses, view_points, view_normals, view_brightness


def reconstruct(run_directory, crop_radius = default_crop_radius_millimeters, voxel_size = default_voxel_millimeters,
                refine = True, extra_runs = (), output_name = "reconstruction", depth_band_raw = default_depth_band_raw):
    """
    Reconstruct the object from run_directory. The stage axes are fitted from
    that run alone; views from extra_runs (taken without moving the scanner or
    re-zeroing the stage) are placed with the same axes and merged in.
    """
    run_directory = pathlib.Path(run_directory)
    output_directory = run_directory / output_name
    output_directory.mkdir(exist_ok = True)
    camera_matrix = read_rectified_camera_matrix(run_directory / "calibration" / "mapparamL.bin")

    start = time.monotonic()
    poses, view_points, view_normals, view_brightness = load_run_views(run_directory, depth_band_raw)
    print(f"{len(poses)} views loaded, {sum(len(p) for p in view_points)} points, {time.monotonic() - start:.0f} s", flush = True)

    upright_index = nearest_upright_view(poses)
    center_guess = estimate_center(view_points[upright_index])
    print(f"  first rotation center guess from the view at {poses[upright_index]}", flush = True)
    model, fit_misalignment = fit_stage_model(view_points, poses, center_guess, crop_radius)
    center, axis_gap = model.rotation_center()
    print(f"  rotation center {center.round(2)} mm, axes {axis_gap:.2f} mm apart", flush = True)

    transforms = [model.camera_to_object(*pose) for pose in poses]
    near_center = [np.linalg.norm(points - center, axis = 1) < crop_radius for points in view_points]
    object_clouds = [apply_transform(transform, points[mask]) for transform, points, mask in zip(transforms, view_points, near_center)]
    pairs = neighbor_pairs(poses)
    check_clouds = [downsample(cloud, 0.8) for cloud in object_clouds]
    before = misalignment(check_clouds, pairs, 0.6)
    if refine:
        corrections = refine_poses(object_clouds, poses)
        transforms = [correction @ transform for correction, transform in zip(corrections, transforms)]
        object_clouds = [apply_transform(correction, cloud) for correction, cloud in zip(corrections, object_clouds)]
        after = misalignment([downsample(cloud, 0.8) for cloud in object_clouds], pairs, 0.6)
        print(f"  joint refinement: misalignment {before:.3f} -> {after:.3f} mm", flush = True)
    else:
        after = before

    for extra_run in extra_runs:
        extra_poses, extra_points, extra_normals, extra_brightness = load_run_views(extra_run, depth_band_raw)
        extra_transforms = [model.camera_to_object(*pose) for pose in extra_poses]
        extra_near = [np.linalg.norm(points - center, axis = 1) < crop_radius for points in extra_points]
        object_clouds += [apply_transform(transform, points[mask])
                          for transform, points, mask in zip(extra_transforms, extra_points, extra_near)]
        transforms += extra_transforms
        near_center += extra_near
        view_normals += extra_normals
        view_brightness += extra_brightness
        print(f"  added {len(extra_poses)} views from {extra_run}", flush = True)

    points = np.concatenate(object_clouds)
    normals = np.concatenate([normals[mask] @ transform[:3, :3].T
                              for normals, mask, transform in zip(view_normals, near_center, transforms)])
    brightness = np.concatenate([values[mask] for values, mask in zip(view_brightness, near_center)])
    # Measure the mount from the turntable axis, not the rotation center: the
    # center sits midway between the axes, so it is off the turntable axis by
    # half their gap (0.79 mm for the 2026-10-27 bust), enough to make the post
    # look out of round.
    up = model.longitude_axis
    axis_point = model.longitude_point + ((center - model.longitude_point) @ up) * up
    keep, mount_top, mount_radius = remove_mount(points, axis_point, up)
    print(f"  mount cut away: top {mount_top:.1f} mm along the turntable axis from the rotation center, "
          f"radius {mount_radius:.1f} mm", flush = True)

    cloud = open3d.geometry.PointCloud(open3d.utility.Vector3dVector(points[keep]))
    cloud.normals = open3d.utility.Vector3dVector(normals[keep])
    cloud.colors = open3d.utility.Vector3dVector(np.repeat(brightness[keep][:, None], 3, axis = 1))
    cloud = cloud.voxel_down_sample(voxel_size)
    cloud, _ = cloud.remove_statistical_outlier(nb_neighbors = 20, std_ratio = 2.0)
    before_cluster = len(cloud.points)
    cloud = keep_largest_cluster(cloud, 3 * voxel_size)
    print(f"  kept the largest connected piece: {len(cloud.points)} of {before_cluster} points", flush = True)
    normals = np.asarray(cloud.normals)
    cloud.normals = open3d.utility.Vector3dVector(normals / np.linalg.norm(normals, axis = 1, keepdims = True))
    open3d.io.write_point_cloud(str(output_directory / "point_cloud.ply"), cloud)
    final_points = np.asarray(cloud.points)
    extent = final_points.max(axis = 0) - final_points.min(axis = 0)
    print(f"  {len(final_points)} points written, extent {extent.round(1)} mm", flush = True)

    axes_record = {"latitude_axis" : model.latitude_axis.tolist(), "latitude_point" : model.latitude_point.tolist(),
                   "longitude_axis" : model.longitude_axis.tolist(), "longitude_point" : model.longitude_point.tolist(),
                   "rotation_center" : center.tolist(), "axis_gap_millimeters" : axis_gap,
                   "nod_gain_degrees" : model.nod_gain_degrees, "nod_pivot_height_millimeters" : model.nod_pivot_height,
                   "nod_pivot" : model.nod_pivot.tolist(),
                   "fit_misalignment_millimeters" : fit_misalignment,
                   "misalignment_before_refinement_millimeters" : before,
                   "misalignment_after_refinement_millimeters" : after,
                   "mount_top_millimeters" : mount_top, "mount_radius_millimeters" : mount_radius,
                   "crop_radius_millimeters" : crop_radius, "voxel_millimeters" : voxel_size,
                   "depth_band_millimeters" : [raw * scanner_module.depth_unit_millimeters for raw in depth_band_raw],
                   "camera_matrix" : camera_matrix.tolist(), "point_count" : len(final_points),
                   "extra_runs" : [str(extra_run) for extra_run in extra_runs]}
    (output_directory / "stage_axes.json").write_text(json.dumps(axes_record, indent = 2))

    # Previews in the object frame. "front" is where the camera was at latitude 0, longitude 0
    # (it looked along +z); "right" is the camera's right (+x), and so on.
    brightness = np.asarray(cloud.colors)[:, 0]
    low, high = np.percentile(brightness, [2, 98])
    stretched = np.repeat(np.clip((brightness - low) / (high - low), 0, 1)[:, None], 3, axis = 1)
    gray = np.full_like(stretched, 0.8)
    toward_camera = np.array([0.0, 0.0, -1.0])
    views = {"front" : toward_camera,
             "right" : Rotation.from_rotvec(np.radians(90) * up).apply(toward_camera),
             "back" : -toward_camera,
             "left" : Rotation.from_rotvec(np.radians(-90) * up).apply(toward_camera),
             "top" : up + 0.05 * toward_camera,
             "three_quarter" : toward_camera + 0.8 * up + 0.8 * Rotation.from_rotvec(np.radians(90) * up).apply(toward_camera)}
    for name, direction in views.items():
        for suffix, point_colors in [("shape", gray), ("brightness", stretched)]:
            image = render_preview(final_points, np.asarray(cloud.normals), point_colors, direction, up,
                                   point_millimeters = voxel_size)
            open3d.io.write_image(str(output_directory / f"preview_{name}_{suffix}.png"), open3d.geometry.Image(image))
    print(f"done in {time.monotonic() - start:.0f} s -> {output_directory}", flush = True)


def main():
    parser = argparse.ArgumentParser(description = "Build a point cloud from a lat/lon scan run.")
    parser.add_argument("run_directory", type = pathlib.Path)
    parser.add_argument("--crop-radius", type = float, default = default_crop_radius_millimeters,
                        help = "Keep points within this distance (mm) of the rotation center (default %(default)s, "
                               "for an object the size of the example duck).")
    parser.add_argument("--depth-range", type = float, nargs = 2, metavar = ("NEAREST", "FARTHEST"), default = None,
                        help = "Keep depth readings between these distances from the scanner, in mm (default "
                               f"{default_depth_band_raw[0] * scanner_module.depth_unit_millimeters:.0f}-"
                               f"{default_depth_band_raw[1] * scanner_module.depth_unit_millimeters:.0f}, "
                               "for the scanner at the front of its mount slot).")
    parser.add_argument("--voxel", type = float, default = default_voxel_millimeters,
                        help = "Output point spacing in mm (default %(default)s).")
    parser.add_argument("--no-refine", dest = "refine", action = "store_false",
                        help = "Skip refining each view with joint ICP after the stage model (it takes a few minutes).")
    parser.add_argument("--extra-runs", type = pathlib.Path, nargs = "+", default = [],
                        help = "More runs of the same object, taken without moving the scanner or re-zeroing the "
                               "stage (e.g. a close-up of one area); placed with the main run's axes and merged in.")
    parser.add_argument("--output-name", default = "reconstruction",
                        help = "Output directory inside the run (default %(default)s).")
    arguments = parser.parse_args()
    depth_band_raw = default_depth_band_raw
    if arguments.depth_range is not None:
        depth_band_raw = tuple(distance / scanner_module.depth_unit_millimeters for distance in arguments.depth_range)
    reconstruct(arguments.run_directory, arguments.crop_radius, arguments.voxel, refine = arguments.refine,
                extra_runs = arguments.extra_runs, output_name = arguments.output_name, depth_band_raw = depth_band_raw)


if __name__ == "__main__":
    main()
