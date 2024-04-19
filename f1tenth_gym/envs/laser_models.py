from __future__ import annotations
import unittest

import numpy as np
from .track import Track
from numba import njit
from scipy.ndimage import distance_transform_edt as edt


def get_dt(bitmap, resolution):
    """Distance transformation, returns the distance matrix from the input bitmap.
    Uses scipy.ndimage, cannot be JITted.

    Parameters
    ----------
    bitmap : np.ndarray
        input binary bitmap of the environment, where 0 is obstacles, and 255 (or anything > 0) is freespace
    resolution : float
        resolution of the input bitmap (m/cell)

    Returns
    -------
    np.ndarray
        output distance matrix, where each cell has the corresponding distance (in meters) to the closest obstacle
    """
    dt = resolution * edt(bitmap)
    return dt


@njit(cache=True)
def xy_2_rc(x, y, orig_x, orig_y, orig_c, orig_s, height, width, resolution):
    """Translate (x, y) coordinate into (r, c) in the matrix

    Parameters
    ----------
    x : float
        coordinate in x (m)
    y : float
        coordinate in y (m)
    orig_x : float
        x coordinate of the map origin (m)
    orig_y : float
        y coordinate of the map origin (m)
    orig_c : float
        cosine of the map origin rotation
    orig_s : float
        sine of the map origin rotation
    height : int
        height of map (pixel)
    width : int
        width of map (pixel)
    resolution : float
        resolution of the input bitmap (m/cell)

    Returns
    -------
    r : int
        row index
    c : int
        column index
    """
    # translation
    x_trans = x - orig_x
    y_trans = y - orig_y

    # rotation
    x_rot = x_trans * orig_c + y_trans * orig_s
    y_rot = -x_trans * orig_s + y_trans * orig_c

    # clip the state to be a cell
    if (
        x_rot < 0
        or x_rot >= width * resolution
        or y_rot < 0
        or y_rot >= height * resolution
    ):
        c = -1
        r = -1
    else:
        c = int(x_rot / resolution)
        r = int(y_rot / resolution)

    return r, c


@njit(cache=True)
def distance_transform(
    x, y, orig_x, orig_y, orig_c, orig_s, height, width, resolution, dt
):
    """Look up corresponding distance in the distance transform matrix

    Parameters
    ----------
    x : _type_
        x coordinate of the lookup point
    y : _type_
        y coordinate of the lookup point
    orig_x : _type_
        x coordinate of the map origin (m)
    orig_y : _type_
        y coordinate of the map origin (m)
    orig_c : _type_
        cosine of the map origin rotation
    orig_s : _type_
        sine of the map origin rotation
    height : _type_
        map height
    width : _type_
        map width
    resolution : _type_
        resolution of the input bitmap (m/cell)
    dt : _type_
        distance transform matrix

    Returns
    -------
    float
        distance to closest obstacle from the look up point
    """
    r, c = xy_2_rc(x, y, orig_x, orig_y, orig_c, orig_s, height, width, resolution)
    distance = dt[r, c]
    return distance


@njit(cache=True)
def trace_ray(
    x,
    y,
    theta_index,
    sines,
    cosines,
    eps,
    orig_x,
    orig_y,
    orig_c,
    orig_s,
    height,
    width,
    resolution,
    dt,
    max_range,
):
    """Find the length of a specific ray at a specific scan angle theta.

    Parameters
    ----------
    x : float
        current x coordinate of the ego (scan) frame
    y : float
        current y coordinate of the ego (scan) frame
    theta_index : int
        current index of the scan beam in the scan range
    sines : np.ndarray
        pre-calculated sines of the angle array
    cosines : np.ndarray
        pre-calculated cosines of the angle array
    eps : float
        tolerance to stop ray tracing
    orig_x : _type_
        x coordinate of the map origin (m)
    orig_y : _type_
        y coordinate of the map origin (m)
    orig_c : _type_
        cosine of the map origin rotation
    orig_s : _type_
        sine of the map origin rotation
    height : _type_
        map height
    width : _type_
        map width
    resolution : _type_
        resolution of the input bitmap (m/cell)
    dt : _type_
        distance transform matrix
    max_range : float
        maximum range for laser scan rays

    Returns
    -------
    float
        ray distance
    """
    # int casting, and index precal trigs
    theta_index_ = int(theta_index)
    s = sines[theta_index_]
    c = cosines[theta_index_]

    # distance to nearest initialization
    dist_to_nearest = distance_transform(
        x, y, orig_x, orig_y, orig_c, orig_s, height, width, resolution, dt
    )
    total_dist = dist_to_nearest

    # ray tracing iterations
    while dist_to_nearest > eps and total_dist <= max_range:
        # move in the direction of the ray by dist_to_nearest
        x += dist_to_nearest * c
        y += dist_to_nearest * s

        # update dist_to_nearest for current point on ray
        # also keeps track of total ray length
        dist_to_nearest = distance_transform(
            x, y, orig_x, orig_y, orig_c, orig_s, height, width, resolution, dt
        )
        total_dist += dist_to_nearest

    if total_dist > max_range:
        total_dist = max_range

    return total_dist


@njit(cache=True)
def get_scan(
    pose,
    theta_dis,
    fov,
    num_beams,
    theta_index_increment,
    sines,
    cosines,
    eps,
    orig_x,
    orig_y,
    orig_c,
    orig_s,
    height,
    width,
    resolution,
    dt,
    max_range,
):
    """Perform the scan for each discretized angle of each beam of the laser

    Parameters
    ----------
    pose : np.ndarray
        current pose of the scan frame in the map
    theta_dis : int
        number of steps to discretize the angles between 0 and 2pi for look up
    fov : float
        field of view of the laser scan
    num_beams : int
        number of beams in the scan
    theta_index_increment : float
        increment between angle indices after discretization
    sines : np.ndarray
        pre-calculated sines of the angle array
    cosines : np.ndarray
        pre-calculated cosines of the angle array
    eps : float
        tolerance to stop ray tracing
    orig_x : _type_
        x coordinate of the map origin (m)
    orig_y : _type_
        y coordinate of the map origin (m)
    orig_c : _type_
        cosine of the map origin rotation
    orig_s : _type_
        sine of the map origin rotation
    height : _type_
        map height
    width : _type_
        map width
    resolution : _type_
        resolution of the input bitmap (m/cell)
    dt : _type_
        distance transform matrix
    max_range : float
        maximum range for laser scan rays

    Returns
    -------
    np.ndarray
        resulting laser scan at the pose
    """
    # empty scan array init
    scan = np.empty((num_beams,))

    # make theta discrete by mapping the range [-pi, pi] onto [0, theta_dis]
    theta_index = theta_dis * (pose[2] - fov / 2.0) / (2.0 * np.pi)

    # make sure it's wrapped properly
    theta_index = np.fmod(theta_index, theta_dis)
    while theta_index < 0:
        theta_index += theta_dis

    # sweep through each beam
    for i in range(0, num_beams):
        # trace the current beam
        scan[i] = trace_ray(
            pose[0],
            pose[1],
            theta_index,
            sines,
            cosines,
            eps,
            orig_x,
            orig_y,
            orig_c,
            orig_s,
            height,
            width,
            resolution,
            dt,
            max_range,
        )

        # increment the beam index
        theta_index += theta_index_increment

        # make sure it stays in the range [0, theta_dis)
        while theta_index >= theta_dis:
            theta_index -= theta_dis

    return scan


@njit(cache=True, error_model="numpy")
def check_ttc_jit(scan, vel, scan_angles, cosines, side_distances, ttc_thresh):
    """Checks the iTTC of each beam in a scan for collision with environment

    Parameters
    ----------
    scan : np.ndarray
        current scan to check
    vel : float
        current velocity
    scan_angles : np.ndarray
        precomped angles of each beam
    cosines : np.ndarray
        precomped cosines of the scan angles
    side_distances : np.ndarray
        precomped distances at each beam from the laser to the sides of the car
    ttc_thresh : float
        threshold for iTTC for collision

    Returns
    -------
    bool
        if scan indicates collision
    """
    in_collision = False
    if vel != 0.0:
        num_beams = scan.shape[0]
        for i in range(num_beams):
            proj_vel = vel * cosines[i]
            ttc = (scan[i] - side_distances[i]) / proj_vel
            if (ttc < ttc_thresh) and (ttc >= 0.0):
                in_collision = True
                break
    else:
        in_collision = False

    return in_collision


@njit(cache=True)
def cross(v1, v2):
    """Cross product of two 2-vectors

    Parameters
    ----------
    v1 : np.ndarray
        input vector 1
    v2 : np.ndarray
        input vector 2

    Returns
    -------
    float
        cross product
    """
    return v1[0] * v2[1] - v1[1] * v2[0]


@njit(cache=True)
def are_collinear(pt_a, pt_b, pt_c):
    """Checks if three points are collinear in 2D

    Parameters
    ----------
    pt_a : np.ndarray
        point to check in 2D
    pt_b : np.ndarray
        point to check in 2D
    pt_c : np.ndarray
        point to check in 2D

    Returns
    -------
    bool
        whether three points are collinear
    """
    tol = 1e-8
    ba = pt_b - pt_a
    ca = pt_a - pt_c
    col = np.fabs(cross(ba, ca)) < tol
    return col


@njit(cache=True)
def get_range(pose, beam_theta, va, vb):
    """Get the distance at a beam angle to the vector formed by two of the four vertices of a vehicle

    Parameters
    ----------
    pose : np.ndarray
        pose of the scanning vehicle
    beam_theta : float
        angle of the current beam (world frame)
    va : np.ndarray
        the two vertices forming an edge
    vb : np.ndarray
        the two vertices forming an edge

    Returns
    -------
    float
        smallest distance at beam theta from scanning pose to edge
    """
    o = pose[0:2]
    v1 = o - va
    v2 = vb - va
    v3 = np.array([np.cos(beam_theta + np.pi / 2.0), np.sin(beam_theta + np.pi / 2.0)])

    denom = v2.dot(v3)
    distance = np.inf

    if np.fabs(denom) > 0.0:
        d1 = cross(v2, v1) / denom
        d2 = v1.dot(v3) / denom
        if d1 >= 0.0 and d2 >= 0.0 and d2 <= 1.0:
            distance = d1
    elif are_collinear(o, va, vb):
        da = np.linalg.norm(va - o)
        db = np.linalg.norm(vb - o)
        distance = min(da, db)

    return distance


@njit(cache=True)
def get_blocked_view_indices(pose, vertices, scan_angles):
    """Get the indices of the start and end of blocked fov in scans by another vehicle

    Parameters
    ----------
    pose : np.ndarray
        pose of the scanning vehicle
    vertices : np.ndarray
        four vertices of a vehicle pose
    scan_angles : np.ndarray
        corresponding beam angles

    Returns
    -------
    min_ind : int
        smaller index of blocked portion of laser scan array
    max_ind : int
        larger index of blocked portion of laser scan array
    """
    # find four vectors formed by pose and 4 vertices:
    vecs = vertices - pose[:2]
    vec_sq = np.square(vecs)
    norms = np.sqrt(vec_sq[:, 0] + vec_sq[:, 1])
    unit_vecs = vecs / norms.reshape(norms.shape[0], 1)

    # find angles between all four and pose vector
    ego_x_vec = np.array([[np.cos(pose[2])], [np.sin(pose[2])]])

    angles_with_x = np.empty((4,))
    for i in range(4):
        angle = np.arctan2(ego_x_vec[1], ego_x_vec[0]) - np.arctan2(
            unit_vecs[i, 1], unit_vecs[i, 0]
        )
        if angle > np.pi:
            angle = angle - 2 * np.pi
        elif angle < -np.pi:
            angle = angle + 2 * np.pi
        angles_with_x[i] = -angle[0]

    ind1 = int(np.argmin(np.abs(scan_angles - angles_with_x[0])))
    ind2 = int(np.argmin(np.abs(scan_angles - angles_with_x[1])))
    ind3 = int(np.argmin(np.abs(scan_angles - angles_with_x[2])))
    ind4 = int(np.argmin(np.abs(scan_angles - angles_with_x[3])))
    inds = [ind1, ind2, ind3, ind4]
    return min(inds), max(inds)


@njit(cache=True)
def ray_cast(pose, scan, scan_angles, vertices):
    """Modify a scan by ray casting onto another agent's four vertices

    Parameters
    ----------
    pose : np.ndarray
        pose of the vehicle performing scan
    scan : np.ndarray
        original scan to modify
    scan_angles : np.ndarray
        corresponding beam angles
    vertices : np.ndarray
        four vertices of a vehicle pose

    Returns
    -------
    np.ndarray
        modified scan
    """
    # pad vertices so loops around
    looped_vertices = np.empty((5, 2))
    looped_vertices[0:4, :] = vertices
    looped_vertices[4, :] = vertices[0, :]

    min_ind, max_ind = get_blocked_view_indices(pose, vertices, scan_angles)
    # looping over beams
    for i in range(min_ind, max_ind + 1):
        # looping over vertices
        for j in range(4):
            # check if original scan is longer than ray casted distance
            scan_range = get_range(
                pose,
                pose[2] + scan_angles[i],
                looped_vertices[j, :],
                looped_vertices[j + 1, :],
            )
            if scan_range < scan[i]:
                scan[i] = scan_range
    return scan


class ScanSimulator2D(object):
    """2D LIDAR scan simulator class

    Parameters
    ----------
    num_beams : int
        number of beams in the scan
    fov : float
        field of view of the laser scan
    eps : float, optional
        ray tracing iteration termination condition, by default 0.0001
    theta_dis : int, optional
        number of steps to discretize the angles between 0 and 2pi for look up, by default 2000
    max_range : float, optional
        maximum range of the laser, by default 30.0
    """

    def __init__(self, num_beams, fov, eps=0.0001, theta_dis=2000, max_range=30.0):
        # initialization
        self.num_beams = num_beams
        self.fov = fov
        self.eps = eps
        self.theta_dis = theta_dis
        self.max_range = max_range
        self.angle_increment = self.fov / (self.num_beams - 1)
        self.theta_index_increment = theta_dis * self.angle_increment / (2.0 * np.pi)
        self.orig_c = None
        self.orig_s = None
        self.orig_x = None
        self.orig_y = None
        self.map_height = None
        self.map_width = None
        self.map_resolution = None
        self.track = None
        self.map_img = None
        self.origin = None
        self.dt = None

        # precomputing corresponding cosines and sines of the angle array
        theta_arr = np.linspace(0.0, 2 * np.pi, num=theta_dis)
        self.sines = np.sin(theta_arr)
        self.cosines = np.cos(theta_arr)

    def set_map(self, map: str | Track):
        """Set the bitmap of the scan simulator by path

        Parameters
        ----------
        map : str | Track
            path to the map file, or Track object

        Returns
        -------
        bool
            if image reading and loading is successful
        """
        if isinstance(map, str):
            self.track = Track.from_track_name(map)
        elif isinstance(map, Track):
            self.track = map

        # load map image
        self.map_img = self.track.occupancy_map
        self.map_height = self.map_img.shape[0]
        self.map_width = self.map_img.shape[1]

        # load map specification
        self.map_resolution = self.track.spec.resolution
        self.origin = self.track.spec.origin

        self.orig_x = self.origin[0]
        self.orig_y = self.origin[1]
        self.orig_s = np.sin(self.origin[2])
        self.orig_c = np.cos(self.origin[2])

        # get the distance transform
        self.dt = get_dt(self.map_img, self.map_resolution)

        return True

    def scan(self, pose, rng, std_dev=0.01):
        """Perform simulated 2D scan by pose on the given map

        Parameters
        ----------
        pose : np.ndarray
            pose of the scan frame (x, y, theta)
        rng : np.random.Generator
            random number generator to use for whitenoise in scan
        std_dev : float, optional
            standard deviation of the generated whitenoise in the scan, by default 0.01

        Returns
        -------
        np.ndarray
            data array of the laserscan

        Raises
        ------
        ValueError
            Map is not set for scan simulator.
        """
        if self.map_height is None:
            raise ValueError("Map is not set for scan simulator.")

        scan = get_scan(
            pose,
            self.theta_dis,
            self.fov,
            self.num_beams,
            self.theta_index_increment,
            self.sines,
            self.cosines,
            self.eps,
            self.orig_x,
            self.orig_y,
            self.orig_c,
            self.orig_s,
            self.map_height,
            self.map_width,
            self.map_resolution,
            self.dt,
            self.max_range,
        )

        if rng is not None:
            noise = rng.normal(0.0, std_dev, size=self.num_beams)
            scan += noise

        return scan

    def get_increment(self):
        """Return angle increment between scans

        Returns
        -------
        float
            angle increment
        """        
        return self.angle_increment


"""
Unit test for the 2D scan simulator class
Author: Hongrui Zheng
TODO: do we still need these?

Test cases:
    1, 2: Comparison between generated scan array of the new simulator and the legacy C++ simulator, generated data used, MSE is used as the metric
    2. FPS test, should be greater than 500
"""


class ScanTests(unittest.TestCase):
    def setUp(self):
        # test params
        self.num_beams = 1080
        self.fov = 4.7

        self.num_test = 10
        self.test_poses = np.zeros((self.num_test, 3))
        self.test_poses[:, 2] = np.linspace(-1.0, 1.0, num=self.num_test)

        # # legacy gym data
        # sample_scan = np.load('legacy_scan.npz')
        # self.berlin_scan = sample_scan['berlin']
        # self.skirk_scan = sample_scan['skirk']

    # def test_map_berlin(self):
    #     scan_rng = np.random.default_rng(seed=12345)
    #     scan_sim = ScanSimulator2D(self.num_beams, self.fov)
    #     new_berlin = np.empty((self.num_test, self.num_beams))
    #     map_path = '../../../maps/Berlin_map.yaml'
    #     map_ext = '.png'
    #     scan_sim.set_map(map_path, map_ext)
    #     # scan gen loop
    #     for i in range(self.num_test):
    #         test_pose = self.test_poses[i]
    #         new_berlin[i,:] = scan_sim.scan(test_pose, scan_rng)
    #     diff = self.berlin_scan - new_berlin
    #     mse = np.mean(diff**2)
    #     # print('Levine distance test, norm: ' + str(norm))

    #     # plotting
    #     import matplotlib.pyplot as plt
    #     theta = np.linspace(-self.fov/2., self.fov/2., num=self.num_beams)
    #     plt.polar(theta, new_berlin[1,:], '.', lw=0)
    #     plt.polar(theta, self.berlin_scan[1,:], '.', lw=0)
    #     plt.show()

    #     self.assertLess(mse, 2.)

    # def test_map_skirk(self):
    #     scan_rng = np.random.default_rng(seed=12345)
    #     scan_sim = ScanSimulator2D(self.num_beams, self.fov)
    #     new_skirk = np.empty((self.num_test, self.num_beams))
    #     map_path = '../../../maps/Skirk_map.yaml'
    #     map_ext = '.png'
    #     scan_sim.set_map(map_path, map_ext)
    #     print('map set')
    #     # scan gen loop
    #     for i in range(self.num_test):
    #         test_pose = self.test_poses[i]
    #         new_skirk[i,:] = scan_sim.scan(test_pose, scan_rng)
    #     diff = self.skirk_scan - new_skirk
    #     mse = np.mean(diff**2)
    #     print('skirk distance test, mse: ' + str(mse))

    #     # plotting
    #     import matplotlib.pyplot as plt
    #     theta = np.linspace(-self.fov/2., self.fov/2., num=self.num_beams)
    #     plt.polar(theta, new_skirk[1,:], '.', lw=0)
    #     plt.polar(theta, self.skirk_scan[1,:], '.', lw=0)
    #     plt.show()

    #     self.assertLess(mse, 2.)

    def test_fps(self):
        # scan fps should be greater than 500

        scan_rng = np.random.default_rng(seed=12345)
        scan_sim = ScanSimulator2D(self.num_beams, self.fov)
        map_path = "../../../maps/Berlin/Berlin_map.yaml"
        map_ext = ".png"
        scan_sim.set_map(map_path, map_ext)

        import time

        start = time.time()
        for i in range(10000):
            x_test = i / 10000
            scan_sim.scan(np.array([x_test, 0.0, 0.0]), scan_rng)
        end = time.time()
        fps = 10000 / (end - start)
        # print('FPS test')
        # print('Elapsed time: ' + str(end-start) + ' , FPS: ' + str(1/fps))
        self.assertGreater(fps, 500.0)

    def test_rng(self):
        num_beams = 1080
        fov = 4.7
        map_path = "../../../maps/Berlin/Berlin_map.yaml"
        map_ext = ".png"
        it = 100

        scan_rng = np.random.default_rng(seed=12345)
        scan_sim = ScanSimulator2D(num_beams, fov)
        scan_sim.set_map(map_path, map_ext)
        scan1 = scan_sim.scan(np.array([0.0, 0.0, 0.0]), scan_rng)
        scan2 = scan_sim.scan(np.array([0.0, 0.0, 0.0]), scan_rng)
        for i in range(it):
            scan3 = scan_sim.scan(np.array([0.0, 0.0, 0.0]), scan_rng)
        scan4 = scan_sim.scan(np.array([0.0, 0.0, 0.0]), scan_rng)

        scan_rng = np.random.default_rng(seed=12345)
        scan5 = scan_sim.scan(np.array([0.0, 0.0, 0.0]), scan_rng)
        scan2 = scan_sim.scan(np.array([0.0, 0.0, 0.0]), scan_rng)
        for i in range(it):
            _ = scan_sim.scan(np.array([0.0, 0.0, 0.0]), scan_rng)
        scan6 = scan_sim.scan(np.array([0.0, 0.0, 0.0]), scan_rng)

        self.assertTrue(np.allclose(scan1, scan5))
        self.assertFalse(np.allclose(scan1, scan2))
        self.assertFalse(np.allclose(scan1, scan3))
        self.assertTrue(np.allclose(scan4, scan6))


def main():
    num_beams = 1080
    fov = 4.7
    # map_path = '../envs/maps/Berlin_map.yaml'
    map_path = "../../../maps/Example/Example_map.yaml"
    map_ext = ".png"
    scan_rng = np.random.default_rng(seed=12345)
    scan_sim = ScanSimulator2D(num_beams, fov)
    scan_sim.set_map(map_path, map_ext)
    scan_sim.scan(np.array([0.0, 0.0, 0.0]), scan_rng)

    # fps test
    import time

    start = time.time()
    for i in range(10000):
        x_test = i / 10000
        scan_sim.scan(np.array([x_test, 0.0, 0.0]), scan_rng)
    end = time.time()
    fps = (end - start) / 10000
    print("FPS test")
    print("Elapsed time: " + str(end - start) + " , FPS: " + str(1 / fps))

    # visualization
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation

    num_iter = 100
    theta = np.linspace(-fov / 2.0, fov / 2.0, num=num_beams)
    fig = plt.figure()
    ax = fig.add_subplot(111, projection="polar")
    ax.set_ylim(0, 31)
    (line,) = ax.plot([], [], ".", lw=0)

    def update(i):
        # x_ani = i * 3. / num_iter
        theta_ani = -i * 2 * np.pi / num_iter
        x_ani = 0.0
        current_scan = scan_sim.scan(np.array([x_ani, 0.0, theta_ani]), scan_rng)
        print(np.max(current_scan))
        line.set_data(theta, current_scan)
        return (line,)

    FuncAnimation(fig, update, frames=num_iter, blit=True)
    plt.show()


if __name__ == "__main__":
    unittest.main()
    # main()

    # import time
    # pt_a = np.array([1., 1.])
    # pt_b = np.array([1., 2.])
    # pt_c = np.array([1., 3.])
    # col = are_collinear(pt_a, pt_b, pt_c)
    # print(col)

    # pose = np.array([0., 0., -1.])
    # beam_theta = 0.
    # start = time.time()
    # dist = get_range(pose, beam_theta, pt_a, pt_b)
    # print(dist, time.time()-start)

    # num_beams = 1080
    # scan = 100.*np.ones((num_beams, ))
    # scan_angles = np.linspace(-2.35, 2.35, num=num_beams)
    # assert scan.shape[0] == scan_angles.shape[0]
    # vertices = np.asarray([[4,11.],[5,5],[9,9],[10,10]])
    # start = time.time()
    # new_scan = ray_cast(pose, scan, scan_angles, vertices)
    # print(time.time()-start)