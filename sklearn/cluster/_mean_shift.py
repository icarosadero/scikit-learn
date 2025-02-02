"""Mean shift clustering algorithm.

Mean shift clustering aims to discover *blobs* in a smooth density of
samples. It is a centroid based algorithm, which works by updating candidates
for centroids to be the mean of the points within a given region. These
candidates are then filtered in a post-processing stage to eliminate
near-duplicates to form the final set of centroids.

Seeding is performed using a binning technique for scalability.
"""

# Authors: Conrad Lee <conradlee@gmail.com>
#          Alexandre Gramfort <alexandre.gramfort@inria.fr>
#          Gael Varoquaux <gael.varoquaux@normalesup.org>

import numpy as np
import warnings

from collections import defaultdict
from ..utils.validation import check_is_fitted
from ..utils import extmath, check_random_state, gen_batches, check_array
from ..base import BaseEstimator, ClusterMixin
from ..neighbors import NearestNeighbors
from ..metrics.pairwise import pairwise_distances_argmin, euclidean_distances


def estimate_bandwidth(X, quantile=0.3, n_samples=None, random_state=0):
    """Estimate the bandwidth to use with the mean-shift algorithm.

    That this function takes time at least quadratic in n_samples. For large
    datasets, it's wise to set that parameter to a small value.

    Parameters
    ----------
    X : array-like, shape=[n_samples, n_features]
        Input points.

    quantile : float, default 0.3
        should be between [0, 1]
        0.5 means that the median of all pairwise distances is used.

    n_samples : int, optional
        The number of samples to use. If not given, all samples are used.

    random_state : int or RandomState
        Pseudo-random number generator state used for random sampling.

    Returns
    -------
    bandwidth : float
        The bandwidth parameter.
    """
    random_state = check_random_state(random_state)
    if n_samples is not None:
        idx = random_state.permutation(X.shape[0])[:n_samples]
        X = X[idx]
    nbrs = NearestNeighbors(n_neighbors=int(X.shape[0] * quantile))
    nbrs.fit(X)

    bandwidth = 0.
    for batch in gen_batches(len(X), 500):
        d, _ = nbrs.kneighbors(X[batch, :], return_distance=True)
        bandwidth += np.max(d, axis=1).sum()

    return bandwidth / X.shape[0]


def mean_shift(X, bandwidth=None, seeds=None, bin_seeding=False,
               min_bin_freq=1, cluster_all=True, max_iter=300,
               max_iterations=None, kernel='flat', gamma=1.0):
    """Perform MeanShift Clustering of data using a flat kernel

    MeanShift clustering aims to discover *blobs* in a smooth density of
    samples. It is a centroid based algorithm, which works by updating
    candidates for centroids to be the mean of the points within a given
    region. These candidates are then filtered in a post-processing stage
    to eliminate near-duplicates to form the final set of centroids.

    Seeding is performed using a binning technique for scalability.

    Parameters
    ----------

    X : array-like, shape=[n_samples, n_features]
        Input data.

    bandwidth : float, optional
        Kernel bandwidth.

        If bandwidth is not given, it is determined using a heuristic based on
        the median of all pairwise distances. This will take quadratic time in
        the number of samples. The sklearn.cluster.estimate_bandwidth function
        can be used to do this more efficiently.
        The only exception is when an rbf kernel is used. In this case
        the default value of bandwidth is 3 / sqrt(gamma).

    seeds : array-like, shape=[n_seeds, n_features] or None
        Point used as initial kernel locations. If None and bin_seeding=False,
        each data point is used as a seed. If None and bin_seeding=True,
        see bin_seeding.

    bin_seeding : boolean, default=False
        If true, initial kernel locations are not locations of all
        points, but rather the location of the discretized version of
        points, where points are binned onto a grid whose coarseness
        corresponds to the bandwidth. Setting this option to True will speed
        up the algorithm because fewer seeds will be initialized.
        Ignored if seeds argument is not None.

    min_bin_freq : int, default=1
       To speed up the algorithm, accept only those bins with at least
       min_bin_freq points as seeds.

    cluster_all : boolean, default True
        If true, then all points are clustered, even those orphans that are
        not within any kernel. Orphans are assigned to the nearest kernel.
        If false, then orphans are given cluster label -1.

    max_iter : int, default 300
        Maximum number of iterations, per seed point before the clustering
        operation terminates (for that seed point), if has not converged yet.
    
    kernel : string, optional, default 'flat'
        The kernel used to update the centroid candidates. Implemented kernels
        are 'flat', 'rbf', 'epanechnikov' and 'biweight'.

    gamma : float, optional, default 1.0
        Kernel coefficient for 'rbf'. Higher values make it prefer more
        datapoints in the center of the cluster, lower values make the kernel
        behave more like the flat kernel.

    Returns
    -------

    cluster_centers : array, shape=[n_clusters, n_features]
        Coordinates of cluster centers.

    labels : array, shape=[n_samples]
        Cluster labels for each point.

    Notes
    -----
    See examples/cluster/plot_meanshift.py for an example.

    """
    # FIXME To be removed in 0.18
    if max_iterations is not None:
        warnings.warn("The `max_iterations` parameter has been renamed to "
                      "`max_iter` from version 0.16. The `max_iterations` "
                      "parameter will be removed in 0.18", DeprecationWarning)
        max_iter = max_iterations

    if bandwidth is None:
        if kernel == 'rbf':
            bandwidth = 3 / np.sqrt(gamma)
        else:
            bandwidth = estimate_bandwidth(X)
    elif bandwidth <= 0:
        raise ValueError("bandwidth needs to be greater than zero or None, got %f" %
                         bandwidth)
    if seeds is None:
        if bin_seeding:
            seeds = get_bin_seeds(X, bandwidth, min_bin_freq)
        else:
            seeds = X
    n_samples, n_features = X.shape
    stop_thresh = 1e-3 * bandwidth  # when mean has converged
    center_intensity_dict = {}
    nbrs = NearestNeighbors(radius=bandwidth).fit(X)

    # For each seed, climb gradient until convergence or max_iter
    for my_mean in seeds:
        completed_iterations = 0
        while True:
            # Find mean of points within bandwidth
            i_nbrs = nbrs.radius_neighbors([my_mean], bandwidth,
                                           return_distance=False)[0]
            points_within = X[i_nbrs]
            if len(points_within) == 0:
                break  # Depending on seeding strategy this condition may occur
            my_old_mean = my_mean  # save the old mean
            my_mean = _kernel_update(my_old_mean, points_within, bandwidth,
                                     kernel, gamma)
            # If converged or at max_iterations, addS the cluster
            if (extmath.np.linalg.norm(my_mean - my_old_mean) < stop_thresh or
                    completed_iterations == max_iter):
                center_intensity_dict[tuple(my_mean)] = len(points_within)
                break
            completed_iterations += 1

    if not center_intensity_dict:
        # nothing near seeds
        raise ValueError("No point was within bandwidth=%f of any seed."
                         " Try a different seeding strategy or increase the bandwidth."
                         % bandwidth)

    # POST PROCESSING: remove near duplicate points
    # If the distance between two kernels is less than the bandwidth,
    # then we have to remove one because it is a duplicate. Remove the
    # one with fewer points.
    sorted_by_intensity = sorted(center_intensity_dict.items(),
                                 key=lambda tup: tup[1], reverse=True)
    sorted_centers = np.array([tup[0] for tup in sorted_by_intensity])
    unique = np.ones(len(sorted_centers), dtype=bool)
    nbrs = NearestNeighbors(radius=bandwidth).fit(sorted_centers)
    for i, center in enumerate(sorted_centers):
        if unique[i]:
            neighbor_idxs = nbrs.radius_neighbors([center],
                                                  return_distance=False)[0]
            unique[neighbor_idxs] = 0
            unique[i] = 1  # leave the current point as unique
    cluster_centers = sorted_centers[unique]

    # ASSIGN LABELS: a point belongs to the cluster that it is closest to
    nbrs = NearestNeighbors(n_neighbors=1).fit(cluster_centers)
    labels = np.zeros(n_samples, dtype=int)
    distances, idxs = nbrs.kneighbors(X)
    if cluster_all:
        labels = idxs.flatten()
    else:
        labels.fill(-1)
        bool_selector = distances.flatten() <= bandwidth
        labels[bool_selector] = idxs.flatten()[bool_selector]
    return cluster_centers, labels


def get_bin_seeds(X, bin_size, min_bin_freq=1):
    """Find seeds for mean_shift.

    Finds seeds by first binning data onto a grid whose lines are
    spaced bin_size apart, and then choosing those bins with at least
    min_bin_freq points.

    Parameters
    ----------

    X : array-like of shape (n_samples, n_features)
        Input points, the same points that will be used in mean_shift.

    bin_size : float
        Controls the coarseness of the binning. Smaller values lead
        to more seeding (which is computationally more expensive). If you're
        not sure how to set this, set it to the value of the bandwidth used
        in clustering.mean_shift.

    min_bin_freq : int, default=1
        Only bins with at least min_bin_freq will be selected as seeds.
        Raising this value decreases the number of seeds found, which
        makes mean_shift computationally cheaper.

    Returns
    -------
    bin_seeds : array-like of shape (n_samples, n_features)
        Points used as initial kernel positions in clustering.mean_shift.
    """
    if bin_size == 0:
        return X

    # Bin points
    bin_sizes = defaultdict(int)
    for point in X:
        binned_point = np.round(point / bin_size)
        bin_sizes[tuple(binned_point)] += 1

    # Select only those bins as seeds which have enough members
    bin_seeds = np.array(
        [point for point, freq in bin_sizes.items() if freq >= min_bin_freq],
        dtype=np.float32,
    )
    if len(bin_seeds) == len(X):
        warnings.warn(
            "Binning data failed with provided bin_size=%f, using data points as seeds."
            % bin_size
        )
        return X
    bin_seeds = bin_seeds * bin_size
    return bin_seeds


def _kernel_update(old_cluster_center, points, bandwidth, kernel, gamma):
    """ Update cluster center according to kernel used

    Parameters
    ----------
    old_window_center : array_like, shape=[n_features]
        The location of the old centroid candidate.

    points : array_like, shape=[n_samples, n_features]
        All data points that fall within the window of which the new mean
        is to be computed.

    bandwidth : float
        Size of the kernel. All points within this range are used to
        compute the new mean. There should be no points outside this range
        in the points variable.

    kernel : string
        The kernel used for updating the window center. Available are:
        'flat', 'rbf', 'epanechnikov' and 'biweight'

    gamma : float
        Controls the width of the rbf kernel. Only used with rbf kernel
        Lower values make it more like the flat kernel, higher values give
        points closer to the old center more weights.

    Notes
    -----
    Kernels as mentioned in the paper "Mean Shift, Mode Seeking, and
    Clustering" by Yizong Cheng, published in IEEE Transaction On Pattern
    Analysis and Machine Intelligence, vol. 17, no. 8, august 1995.

    The rbf or Gaussian kernel is a truncated version, since in this
    implementation of Mean Shift clustering only the points within the
    range of bandwidth around old_cluster_center are fed into the points
    variable.
    """

    # The flat kernel gives all points within range equal weight
    # No extra calculations needed
    if kernel == 'flat':
        weighted_mean = np.mean(points, axis=0)

    else:
        # Define the weights function for each kernel
        if kernel == 'rbf':
            compute_weights = lambda p, b: np.exp(-1 * gamma * (p ** 2))

        elif kernel == 'epanechnikov':
            compute_weights = lambda p, b: 1.0 - (p ** 2 / b ** 2)

        elif kernel == 'biweight':
            compute_weights = lambda p, b: (1.0 - (p ** 2 / b ** 2)) ** 2

        else:
            implemented_kernels = ['flat', 'rbf', 'epanechnikov', 'biweight']
            raise ValueError("Unknown kernel, please use one of: %s" %
                             ", ".join(implemented_kernels))

        # Compute new mean
        distances = euclidean_distances(points, old_cluster_center.reshape(1,-1))
        weights = compute_weights(distances, bandwidth)
        weighted_mean = np.sum(points * weights, axis=0) / np.sum(weights)

    return weighted_mean


class MeanShift(BaseEstimator, ClusterMixin):
    """Mean shift clustering using a flat kernel.

    Mean shift clustering aims to discover "blobs" in a smooth density of
    samples. It is a centroid-based algorithm, which works by updating
    candidates for centroids to be the mean of the points within a given
    region. These candidates are then filtered in a post-processing stage to
    eliminate near-duplicates to form the final set of centroids.

    Seeding is performed using a binning technique for scalability.

    Parameters
    ----------
    bandwidth : float, optional
        Window size around centroid candidates used to compute new centroids.

        If not given, the bandwidth is estimated using
        sklearn.cluster.estimate_bandwidth; see the documentation for that
        function for hints on scalability (see also the Notes, below).

    seeds : array, shape=[n_samples, n_features], optional
        Seeds used to initialize kernels. If not set,
        the seeds are calculated by clustering.get_bin_seeds
        with bandwidth as the grid size and default values for
        other parameters.

    bin_seeding : boolean, optional
        If true, initial kernel locations are not locations of all
        points, but rather the location of the discretized version of
        points, where points are binned onto a grid whose coarseness
        corresponds to the bandwidth. Setting this option to True will speed
        up the algorithm because fewer seeds will be initialized.
        default value: False
        Ignored if seeds argument is not None.

    min_bin_freq : int, optional
       To speed up the algorithm, accept only those bins with at least
       min_bin_freq points as seeds. If not defined, set to 1.

    cluster_all : boolean, default True
        If true, then all points are clustered, even those orphans that are
        not within any kernel. Orphans are assigned to the nearest kernel.
        If false, then orphans are given cluster label -1.

    kernel : string, optional, default 'flat'
        The kernel used to update the centroid candidates. Implemented kernels
        are 'flat', 'rbf', 'epanechnikov' and 'biweight'.

    gamma : float, optional, default 1.0
        Kernel coefficient for 'rbf'. Higher values make it prefer more
        datapoints in the center of the cluster, lower values make the kernel
        behave more like the flat kernel.

    Attributes
    ----------
    cluster_centers_ : array, [n_clusters, n_features]
        Coordinates of cluster centers.

    labels_ :
        Labels of each point.

    Notes
    -----

    Scalability:

    Because this implementation uses a flat kernel and
    a Ball Tree to look up members of each kernel, the complexity will is
    to O(T*n*log(n)) in lower dimensions, with n the number of samples
    and T the number of points. In higher dimensions the complexity will
    tend towards O(T*n^2).

    Scalability can be boosted by using fewer seeds, for example by using
    a higher value of min_bin_freq in the get_bin_seeds function.

    Note that the estimate_bandwidth function is much less scalable than the
    mean shift algorithm and will be the bottleneck if it is used.

    References
    ----------

    Dorin Comaniciu and Peter Meer, "Mean Shift: A robust approach toward
    feature space analysis". IEEE Transactions on Pattern Analysis and
    Machine Intelligence. 2002. pp. 603-619.

    """
    def __init__(self, bandwidth=None, seeds=None, bin_seeding=False,
                 min_bin_freq=1, cluster_all=True, kernel='flat',
                 gamma=1.0):
        self.bandwidth = bandwidth
        self.seeds = seeds
        self.bin_seeding = bin_seeding
        self.cluster_all = cluster_all
        self.min_bin_freq = min_bin_freq
        self.kernel = kernel
        self.gamma = gamma

    def fit(self, X, y=None):
        """Perform clustering.

        Parameters
        -----------
        X : array-like, shape=[n_samples, n_features]
            Samples to cluster.
        """
        X = check_array(X)
        self.cluster_centers_, self.labels_ = \
            mean_shift(X, bandwidth=self.bandwidth, seeds=self.seeds,
                       min_bin_freq=self.min_bin_freq,
                       bin_seeding=self.bin_seeding,
                       cluster_all=self.cluster_all,
                       kernel=self.kernel,
                       gamma=self.gamma)
        return self

    def predict(self, X):
        """Predict the closest cluster each sample in X belongs to.

        Parameters
        ----------
        X : {array-like, sparse matrix}, shape=[n_samples, n_features]
            New data to predict.

        Returns
        -------
        labels : array, shape [n_samples,]
            Index of the cluster each sample belongs to.
        """
        check_is_fitted(self, "cluster_centers_")

        return pairwise_distances_argmin(X, self.cluster_centers_)
