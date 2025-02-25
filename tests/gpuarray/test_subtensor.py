import numpy as np

import aesara
from aesara.compile import DeepCopyOp
from aesara.gpuarray.basic_ops import GpuContiguous, GpuFromHost, HostFromGpu
from aesara.gpuarray.elemwise import GpuDimShuffle
from aesara.gpuarray.subtensor import (
    GpuAdvancedIncSubtensor,
    GpuAdvancedIncSubtensor1,
    GpuAdvancedIncSubtensor1_dev20,
    GpuAdvancedSubtensor,
    GpuAdvancedSubtensor1,
    GpuAllocDiag,
    GpuExtractDiag,
    GpuIncSubtensor,
    GpuSubtensor,
)
from aesara.gpuarray.type import gpuarray_shared_constructor
from aesara.tensor.basic import AllocDiag, ExtractDiag
from aesara.tensor.math import sum as at_sum
from aesara.tensor.subtensor import advanced_inc_subtensor1, inc_subtensor
from aesara.tensor.type import ivectors, matrix, tensor, tensor4, vector
from tests import unittest_tools as utt
from tests.gpuarray.config import mode_with_gpu, test_ctx_name
from tests.tensor.test_basic import TestAllocDiag
from tests.tensor.test_subtensor import TestAdvancedSubtensor, TestSubtensor


class TestGPUSubtensor(TestSubtensor):
    def setup_method(self):
        def shared(x, **kwargs):
            return gpuarray_shared_constructor(x, target=test_ctx_name, **kwargs)

        self.shared = shared
        self.sub = GpuSubtensor
        self.inc_sub = GpuIncSubtensor
        self.adv_sub1 = GpuAdvancedSubtensor1
        self.adv_incsub1 = GpuAdvancedIncSubtensor1
        self.adv_sub = GpuAdvancedSubtensor
        self.dimshuffle = GpuDimShuffle
        self.mode = mode_with_gpu
        # avoid errors with limited devices
        self.dtype = "float32"
        self.ignore_topo = (HostFromGpu, GpuFromHost, DeepCopyOp, GpuContiguous)
        # GPU opt can't run in fast_compile only.
        self.fast_compile = False
        assert self.sub == GpuSubtensor
        super().setup_method()


class TestGPUSubtensorF16(TestSubtensor):
    def setup_method(self):
        def shared(x, **kwargs):
            return gpuarray_shared_constructor(x, target=test_ctx_name, **kwargs)

        self.shared = shared
        self.sub = GpuSubtensor
        self.inc_sub = GpuIncSubtensor
        self.adv_sub1 = GpuAdvancedSubtensor1
        self.adv_incsub1 = GpuAdvancedIncSubtensor1
        self.adv_sub = GpuAdvancedSubtensor
        self.dimshuffle = GpuDimShuffle
        self.mode = mode_with_gpu
        # avoid errors with limited devices
        self.dtype = "float16"  # use floatX?
        self.ignore_topo = (HostFromGpu, GpuFromHost, DeepCopyOp, GpuContiguous)
        # GPU opt can't run in fast_compile only.
        self.fast_compile = False
        assert self.sub == GpuSubtensor
        super().setup_method()


def test_advinc_subtensor1():
    # Test the second case in the opt local_gpu_advanced_incsubtensor1
    for shp in [(3, 3), (3, 3, 3)]:
        shared = gpuarray_shared_constructor
        xval = np.arange(np.prod(shp), dtype="float32").reshape(shp) + 1
        yval = np.empty((2,) + shp[1:], dtype="float32")
        yval[:] = 10
        x = shared(xval, name="x")
        y = tensor(dtype="float32", broadcastable=(False,) * len(shp), name="y")
        expr = advanced_inc_subtensor1(x, y, [0, 2])
        f = aesara.function([y], expr, mode=mode_with_gpu)
        assert (
            sum(
                [
                    isinstance(node.op, GpuAdvancedIncSubtensor1)
                    for node in f.maker.fgraph.toposort()
                ]
            )
            == 1
        )
        rval = f(yval)
        rep = xval.copy()
        np.add.at(rep, [0, 2], yval)
        assert np.allclose(rval, rep)


def test_advinc_subtensor1_dtype():
    # Test the mixed dtype case
    shp = (3, 4)
    for dtype1, dtype2 in [
        ("float32", "int8"),
        ("float32", "float64"),
        ("uint64", "int8"),
        ("int64", "uint8"),
        ("float16", "int8"),
        ("float16", "float64"),
        ("float16", "float16"),
    ]:
        shared = gpuarray_shared_constructor
        xval = np.arange(np.prod(shp), dtype=dtype1).reshape(shp) + 1
        yval = np.empty((2,) + shp[1:], dtype=dtype2)
        yval[:] = 10
        x = shared(xval, name="x")
        y = tensor(dtype=yval.dtype, broadcastable=(False,) * len(yval.shape), name="y")
        expr = advanced_inc_subtensor1(x, y, [0, 2])
        f = aesara.function([y], expr, mode=mode_with_gpu)
        assert (
            sum(
                [
                    isinstance(node.op, GpuAdvancedIncSubtensor1_dev20)
                    for node in f.maker.fgraph.toposort()
                ]
            )
            == 1
        )
        rval = f(yval)
        rep = xval.copy()
        np.add.at(rep, [[0, 2]], yval)
        assert np.allclose(rval, rep)


@aesara.config.change_flags(deterministic="more")
def test_deterministic_flag():
    shp = (3, 4)
    for dtype1, dtype2 in [("float32", "int8")]:
        shared = gpuarray_shared_constructor
        xval = np.arange(np.prod(shp), dtype=dtype1).reshape(shp) + 1
        yval = np.empty((2,) + shp[1:], dtype=dtype2)
        yval[:] = 10
        x = shared(xval, name="x")
        y = tensor(dtype=yval.dtype, broadcastable=(False,) * len(yval.shape), name="y")
        expr = advanced_inc_subtensor1(x, y, [0, 2])
        f = aesara.function([y], expr, mode=mode_with_gpu)
        assert (
            sum(
                [
                    isinstance(node.op, GpuAdvancedIncSubtensor1)
                    for node in f.maker.fgraph.toposort()
                ]
            )
            == 1
        )
        rval = f(yval)
        rep = xval.copy()
        np.add.at(rep, [[0, 2]], yval)
        assert np.allclose(rval, rep)


def test_advinc_subtensor1_vector_scalar():
    # Test the case where x is a vector and y a scalar
    shp = (3,)
    for dtype1, dtype2 in [
        ("float32", "int8"),
        ("float32", "float64"),
        ("float16", "int8"),
        ("float16", "float64"),
        ("float16", "float16"),
        ("int8", "int8"),
        ("int16", "int16"),
    ]:
        shared = gpuarray_shared_constructor
        xval = np.arange(np.prod(shp), dtype=dtype1).reshape(shp) + 1
        yval = np.asarray(10, dtype=dtype2)
        x = shared(xval, name="x")
        y = tensor(dtype=yval.dtype, broadcastable=(False,) * len(yval.shape), name="y")
        expr = advanced_inc_subtensor1(x, y, [0, 2])
        f = aesara.function([y], expr, mode=mode_with_gpu)

        assert (
            sum(
                [
                    isinstance(
                        node.op,
                        (GpuAdvancedIncSubtensor1_dev20, GpuAdvancedIncSubtensor1),
                    )
                    for node in f.maker.fgraph.toposort()
                ]
            )
            == 1
        )
        rval = f(yval)
        rep = xval.copy()
        rep[[0, 2]] += yval
        assert np.allclose(rval, rep)


def test_incsub_f16():
    shp = (3, 3)
    shared = gpuarray_shared_constructor
    xval = np.arange(np.prod(shp), dtype="float16").reshape(shp) + 1
    yval = np.empty((2,) + shp[1:], dtype="float16")
    yval[:] = 2
    x = shared(xval, name="x")
    y = tensor(dtype="float16", broadcastable=(False,) * len(shp), name="y")
    expr = advanced_inc_subtensor1(x, y, [0, 2])
    f = aesara.function([y], expr, mode=mode_with_gpu)
    assert (
        sum(
            [
                isinstance(node.op, GpuAdvancedIncSubtensor1)
                for node in f.maker.fgraph.toposort()
            ]
        )
        == 1
    )
    rval = f(yval)
    rep = xval.copy()
    np.add.at(rep, [[0, 2]], yval)
    assert np.allclose(rval, rep)

    expr = inc_subtensor(x[1:], y)
    f = aesara.function([y], expr, mode=mode_with_gpu)
    assert (
        sum(
            [isinstance(node.op, GpuIncSubtensor) for node in f.maker.fgraph.toposort()]
        )
        == 1
    )
    rval = f(yval)
    rep = xval.copy()
    rep[1:] += yval
    assert np.allclose(rval, rep)


def test_incsub_offset():
    # Test for https://github.com/Theano/Theano/issues/5670

    # Build a GPU variable which value will have an offset (x1)
    x = gpuarray_shared_constructor(np.zeros(5, dtype=aesara.config.floatX))
    x1 = x[1:]
    # Use inc_subtensor on it
    y = vector()
    z = inc_subtensor(x1[2:], y)
    # Use updates so that inc_subtensor can happen inplace
    f = aesara.function([y], z, updates={x: z}, mode=mode_with_gpu)
    utt.assert_allclose(f([1, 2]), np.array([0, 0, 1, 2], dtype=aesara.config.floatX))


class TestGPUAdvancedSubtensor(TestAdvancedSubtensor):
    def setup_method(self):
        self.shared = gpuarray_shared_constructor
        self.sub = GpuAdvancedSubtensor
        self.inc_sub = GpuAdvancedIncSubtensor
        self.mode = mode_with_gpu
        # avoid errors with limited devices
        self.dtype = "float32"  # floatX?
        self.ignore_topo = (HostFromGpu, GpuFromHost, DeepCopyOp)
        # GPU opt can't run in fast_compile only.
        self.fast_compile = False
        assert self.sub == GpuAdvancedSubtensor
        super().setup_method()


class TestGPUAdvancedSubtensorF16(TestAdvancedSubtensor):
    def setup_method(self):
        self.shared = gpuarray_shared_constructor
        self.sub = GpuAdvancedSubtensor
        self.mode = mode_with_gpu
        # avoid errors with limited devices
        self.dtype = "float16"  # floatX?
        self.ignore_topo = (HostFromGpu, GpuFromHost, DeepCopyOp)
        # GPU opt can't run in fast_compile only.
        self.fast_compile = False
        assert self.sub == GpuAdvancedSubtensor
        super().setup_method()


def test_adv_subtensor():
    # Test the advancedsubtensor on gpu.
    shp = (2, 3, 4)
    shared = gpuarray_shared_constructor
    xval = np.arange(np.prod(shp), dtype=aesara.config.floatX).reshape(shp)
    idx1, idx2 = ivectors("idx1", "idx2")
    idxs = [idx1, None, slice(0, 2, 1), idx2, None]
    x = shared(xval, name="x")
    expr = x[idxs]
    f = aesara.function([idx1, idx2], expr, mode=mode_with_gpu)
    assert (
        sum(
            [
                isinstance(node.op, GpuAdvancedSubtensor)
                for node in f.maker.fgraph.toposort()
            ]
        )
        == 1
    )
    idx1_val = [0, 1]
    idx2_val = [0, 1]
    rval = f(idx1_val, idx2_val)
    rep = xval[idx1_val, None, slice(0, 2, 1), idx2_val, None]
    assert np.allclose(rval, rep)


class TestGpuExtractDiag:
    def test_extractdiag_opt(self):
        x = matrix()
        fn = aesara.function([x], ExtractDiag()(x), mode=mode_with_gpu)
        assert any(
            [isinstance(node.op, GpuExtractDiag) for node in fn.maker.fgraph.toposort()]
        )

    def test_matrix(self):
        x = matrix()
        np_x = np.arange(77).reshape(7, 11).astype(aesara.config.floatX)
        fn = aesara.function([x], GpuExtractDiag()(x), mode=mode_with_gpu)
        assert np.allclose(fn(np_x), np_x.diagonal())
        fn = aesara.function([x], GpuExtractDiag(2)(x), mode=mode_with_gpu)
        assert np.allclose(fn(np_x), np_x.diagonal(2))
        fn = aesara.function([x], GpuExtractDiag(-3)(x), mode=mode_with_gpu)
        assert np.allclose(fn(np_x), np_x.diagonal(-3))

    def test_tensor(self):
        x = tensor4()
        np_x = np.arange(30107).reshape(7, 11, 17, 23).astype(aesara.config.floatX)
        for offset, axis1, axis2 in [
            (1, 0, 1),
            (-1, 0, 1),
            (0, 1, 0),
            (-2, 1, 0),
            (-3, 1, 0),
            (-2, 2, 0),
            (3, 3, 0),
            (-1, 3, 2),
            (2, 2, 3),
            (-1, 2, 1),
            (1, 3, 1),
            (-1, 1, 3),
        ]:
            assert np.allclose(
                GpuExtractDiag(offset, axis1, axis2)(x).eval({x: np_x}),
                np_x.diagonal(offset, axis1, axis2),
            )

    def test_tensor_float16(self):
        x = tensor4()
        np_x = np.arange(30107).reshape(7, 11, 17, 23).astype("float16")
        for offset, axis1, axis2 in [
            (1, 0, 1),
            (-1, 0, 1),
            (0, 1, 0),
            (-2, 1, 0),
            (-3, 1, 0),
            (-2, 2, 0),
            (3, 3, 0),
            (-1, 3, 2),
            (2, 2, 3),
            (-1, 2, 1),
            (1, 3, 1),
            (-1, 1, 3),
        ]:
            assert np.allclose(
                GpuExtractDiag(offset, axis1, axis2)(x).eval({x: np_x}),
                np_x.diagonal(offset, axis1, axis2),
            )


class TestGpuAllocDiag(TestAllocDiag):
    def setup_method(self):
        self.alloc_diag = GpuAllocDiag
        self.mode = mode_with_gpu
        super().setup_method()

    def test_allocdiag_opt(self):
        x = vector()
        fn = aesara.function([x], AllocDiag()(x), mode=mode_with_gpu)
        assert any(
            [isinstance(node.op, GpuAllocDiag) for node in fn.maker.fgraph.toposort()]
        )

    def test_matrix(self):
        x = vector()
        np_x = np.arange(7).astype(aesara.config.floatX)
        fn = aesara.function([x], GpuAllocDiag()(x), mode=mode_with_gpu)
        assert np.allclose(fn(np_x), np.diag(np_x))
        fn = aesara.function([x], GpuAllocDiag(2)(x), mode=mode_with_gpu)
        assert np.allclose(fn(np_x), np.diag(np_x, 2))
        fn = aesara.function([x], GpuAllocDiag(-3)(x), mode=mode_with_gpu)
        assert np.allclose(fn(np_x), np.diag(np_x, -3))

    def test_grad(self):
        x = vector()
        np_x = np.random.randn(7).astype(aesara.config.floatX)

        # offset = 0 case:
        mtx_x = GpuAllocDiag()(x)
        sum_mtx_x = at_sum(mtx_x)
        grad_x = aesara.grad(sum_mtx_x, x)
        grad_mtx_x = aesara.grad(sum_mtx_x, mtx_x)

        fn_grad_x = aesara.function([x], grad_x, mode=mode_with_gpu)
        fn_grad_mtx_x = aesara.function([x], grad_mtx_x, mode=mode_with_gpu)

        computed_grad_x = fn_grad_x(np_x)
        computed_grad_mtx_x = fn_grad_mtx_x(np_x)
        true_grad_x = np.diagonal(computed_grad_mtx_x, 0)
        assert np.allclose(computed_grad_x, true_grad_x)

        # offset > 0 case:
        mtx_x = GpuAllocDiag(2)(x)
        sum_mtx_x = at_sum(mtx_x)
        grad_x = aesara.grad(sum_mtx_x, x)
        grad_mtx_x = aesara.grad(sum_mtx_x, mtx_x)

        fn_grad_x = aesara.function([x], grad_x, mode=mode_with_gpu)
        fn_grad_mtx_x = aesara.function([x], grad_mtx_x, mode=mode_with_gpu)

        computed_grad_x = fn_grad_x(np_x)
        computed_grad_mtx_x = fn_grad_mtx_x(np_x)
        true_grad_x = np.diagonal(computed_grad_mtx_x, 2)
        assert np.allclose(computed_grad_x, true_grad_x)

        # offset < 0 case:
        mtx_x = GpuAllocDiag(-3)(x)
        sum_mtx_x = at_sum(mtx_x)
        grad_x = aesara.grad(sum_mtx_x, x)
        grad_mtx_x = aesara.grad(sum_mtx_x, mtx_x)

        fn_grad_x = aesara.function([x], grad_x, mode=mode_with_gpu)
        fn_grad_mtx_x = aesara.function([x], grad_mtx_x, mode=mode_with_gpu)

        computed_grad_x = fn_grad_x(np_x)
        computed_grad_mtx_x = fn_grad_mtx_x(np_x)
        true_grad_x = np.diagonal(computed_grad_mtx_x, -3)
        assert np.allclose(computed_grad_x, true_grad_x)

        # assert
