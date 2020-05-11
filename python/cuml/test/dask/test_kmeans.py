# Copyright (c) 2019-2020, NVIDIA CORPORATION.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import numpy as np
import cupy as cp
import pytest

from cuml.test.utils import unit_param
from cuml.test.utils import quality_param
from cuml.test.utils import stress_param

import dask.array as da
from dask.distributed import Client, wait

from cuml.metrics import adjusted_rand_score
from sklearn.metrics import adjusted_rand_score as sk_adjusted_rand_score

from cuml.dask.common.dask_arr_utils import to_dask_cudf

SCORE_EPS = 0.06


@pytest.mark.mg
@pytest.mark.parametrize("nrows", [unit_param(1e3), quality_param(1e5),
                                   stress_param(5e6)])
@pytest.mark.parametrize("ncols", [10, 30])
@pytest.mark.parametrize("nclusters", [unit_param(5), quality_param(10),
                                       stress_param(50)])
@pytest.mark.parametrize("n_parts", [unit_param(None), quality_param(7),
                                     stress_param(50)])
@pytest.mark.parametrize("delayed_predict", [True, False])
@pytest.mark.parametrize("input_type", ["dataframe", "array"])
def test_end_to_end(nrows, ncols, nclusters, n_parts,
                    delayed_predict, input_type, cluster):

    client = None

    try:

        client = Client(cluster)
        from cuml.dask.cluster import KMeans as cumlKMeans

        from cuml.dask.datasets import make_blobs

        X, y = make_blobs(n_samples=int(nrows),
                          n_features=ncols,
                          centers=nclusters,
                          n_parts=n_parts,
                          cluster_std=0.01, verbose=False,
                          random_state=10)

        wait(X)
        if input_type == "dataframe":
            X_train = to_dask_cudf(X)
            y_train = to_dask_cudf(y)
        elif input_type == "array":
            X_train, y_train = X, y

        cumlModel = cumlKMeans(verbose=0, init="k-means||",
                               n_clusters=nclusters,
                               random_state=10)

        cumlModel.fit(X_train)
        cumlLabels = cumlModel.predict(X_train, delayed=delayed_predict)

        n_workers = len(list(client.has_what().keys()))

        # Verifying we are grouping partitions. This should be changed soon.
        if n_parts is not None and n_parts < n_workers:
            parts_len = n_parts
        else:
            parts_len = n_workers

        if input_type == "dataframe":
            assert cumlLabels.npartitions == parts_len
            cumlPred = cp.array(cumlLabels.compute().to_pandas().values)
            labels = cp.squeeze(y_train.compute().to_pandas().values)
        elif input_type == "array":
            assert len(cumlLabels.chunks[0]) == parts_len
            cumlPred = cp.array(cumlLabels.compute())
            labels = cp.squeeze(y_train.compute())

        assert cumlPred.shape[0] == nrows
        assert cp.max(cumlPred) == nclusters - 1
        assert cp.min(cumlPred) == 0

        score = adjusted_rand_score(labels, cumlPred)

        assert 1.0 == score

    finally:
        client.close()


@pytest.mark.parametrize('nrows', [500])
@pytest.mark.parametrize('ncols', [5])
@pytest.mark.parametrize('nclusters', [3, 10])
@pytest.mark.parametrize('n_parts', [1, 5])
@pytest.mark.parametrize('random_state', [i for i in [0, 100]])
def test_weighted_kmeans(nrows, ncols, nclusters, random_state,
                         n_parts, cluster):
    cluster_std = 10000.0
    np.random.seed(random_state)

    client = None

    try:

        client = Client(cluster)
        from cuml.dask.cluster import KMeans as cumlKMeans

        from cuml.dask.datasets import make_blobs

        # Using fairly high variance between points in clusters
        wt = np.array([0.00001 for j in range(nrows)])

        bound = nclusters * 100000

        # Open the space really large
        centers = np.random.uniform(-bound, bound,
                                    size=(nclusters, ncols))

        X_cudf, y = make_blobs(n_samples=nrows,
                               n_features=ncols,
                               centers=centers,
                               n_parts=n_parts,
                               cluster_std=cluster_std,
                               shuffle=False,
                               verbose=False,
                               random_state=10)

        wait(X_cudf)

        y_arr = y.compute().as_gpu_matrix().copy_to_host()

        # Choose one sample from each label and increase its weight
        for i in range(nclusters):
            wt[cp.argmax(cp.array(y_arr) == i).item()] = 5000.0

        cumlModel = cumlKMeans(verbose=0, init="k-means||",
                               n_clusters=nclusters,
                               random_state=10)

        chunk_parts = int(nrows / n_parts)
        sample_weights = da.from_array(wt, chunks=(chunk_parts, ))
        cumlModel.fit(X_cudf, sample_weight=sample_weights)

        X = X_cudf.compute().as_gpu_matrix()

        labels_ = cumlModel.predict(X_cudf).compute()\
            .to_gpu_array().copy_to_host()
        cluster_centers_ = cumlModel.cluster_centers_\
            .as_gpu_matrix().copy_to_host()

        for i in range(nrows):

            label = labels_[i]
            actual_center = cluster_centers_[label]

            diff = sum(abs(X[i].copy_to_host() - actual_center))

            # The large weight should be the centroid
            if wt[i] > 1.0:
                assert diff < 1.0

            # Otherwise it should be pretty far away
            else:
                assert diff > 1000.0

    finally:
        client.close()


@pytest.mark.mg
@pytest.mark.parametrize("nrows", [unit_param(5e3), quality_param(1e5),
                                   stress_param(1e6)])
@pytest.mark.parametrize("ncols", [unit_param(10), quality_param(30),
                                   stress_param(50)])
@pytest.mark.parametrize("nclusters", [1, 10, 30])
@pytest.mark.parametrize("n_parts", [unit_param(None), quality_param(7),
                                     stress_param(50)])
@pytest.mark.parametrize("input_type", ["dataframe", "array"])
def test_transform(nrows, ncols, nclusters, n_parts, input_type, cluster):

    client = None

    try:

        client = Client(cluster)

        from cuml.dask.cluster import KMeans as cumlKMeans

        from cuml.dask.datasets import make_blobs

        X, y = make_blobs(n_samples=int(nrows),
                          n_features=ncols,
                          centers=nclusters,
                          n_parts=n_parts,
                          cluster_std=0.01,
                          verbose=False,
                          shuffle=False,
                          random_state=10)
        y = y.astype('int64')

        wait(X)
        if input_type == "dataframe":
            X_train = to_dask_cudf(X)
            y_train = to_dask_cudf(y)
            labels = cp.squeeze(y_train.compute().to_pandas().values)
        elif input_type == "array":
            X_train, y_train = X, y
            labels = cp.squeeze(y_train.compute())

        cumlModel = cumlKMeans(verbose=0, init="k-means||",
                               n_clusters=nclusters,
                               random_state=10)

        cumlModel.fit(X_train)

        xformed = cumlModel.transform(X_train).compute()
        if input_type == "dataframe":
            xformed = cp.array(xformed
                               if len(xformed.shape) == 1
                               else xformed.as_gpu_matrix())

        if nclusters == 1:
            # series shape is (nrows,) not (nrows, 1) but both are valid
            # and equivalent for this test
            assert xformed.shape in [(nrows, nclusters), (nrows,)]
        else:
            assert xformed.shape == (nrows, nclusters)

        # The argmin of the transformed values should be equal to the labels
        # reshape is a quick manner of dealing with (nrows,) is not (nrows, 1)
        xformed_labels = cp.argmin(xformed.reshape((int(nrows),
                                                    int(nclusters))), axis=1)

        assert sk_adjusted_rand_score(cp.asnumpy(labels),
                                      cp.asnumpy(xformed_labels))

    finally:
        client.close()


@pytest.mark.mg
@pytest.mark.parametrize("nrows", [unit_param(1e3), quality_param(1e5),
                                   stress_param(5e6)])
@pytest.mark.parametrize("ncols", [10, 30])
@pytest.mark.parametrize("nclusters", [unit_param(5), quality_param(10),
                                       stress_param(50)])
@pytest.mark.parametrize("n_parts", [unit_param(None), quality_param(7),
                                     stress_param(50)])
@pytest.mark.parametrize("input_type", ["dataframe", "array"])
def test_score(nrows, ncols, nclusters, n_parts, input_type, cluster):

    client = None

    try:

        client = Client(cluster)
        from cuml.dask.cluster import KMeans as cumlKMeans

        from cuml.dask.datasets import make_blobs

        X, y = make_blobs(n_samples=int(nrows),
                          n_features=ncols,
                          centers=nclusters,
                          n_parts=n_parts,
                          cluster_std=0.01, verbose=False,
                          shuffle=False,
                          random_state=10)

        wait(X)
        if input_type == "dataframe":
            X_train = to_dask_cudf(X)
            y_train = to_dask_cudf(y)
            y = y_train
        elif input_type == "array":
            X_train, y_train = X, y

        cumlModel = cumlKMeans(verbose=0, init="k-means||",
                               n_clusters=nclusters,
                               random_state=10)

        cumlModel.fit(X_train)

        actual_score = cumlModel.score(X_train)

        predictions = cumlModel.predict(X_train).compute()

        if input_type == "dataframe":
            X = cp.array(X_train.compute().as_gpu_matrix())
            predictions = cp.array(predictions)

            centers = cp.array(cumlModel.cluster_centers_.as_gpu_matrix())
        elif input_type == "array":
            X = X_train.compute()
            centers = cumlModel.cluster_centers_

        expected_score = 0
        for idx, label in enumerate(predictions):

            x = X[idx]
            y = centers[label]

            dist = cp.sqrt(cp.sum((x - y)**2))
            expected_score += dist**2

        assert actual_score + SCORE_EPS \
            >= (-1 * expected_score) \
            >= actual_score - SCORE_EPS

    finally:
        client.close()
