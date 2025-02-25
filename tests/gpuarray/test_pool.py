import copy
import itertools

import numpy as np
import pytest

import aesara
from aesara import tensor as at
from aesara.gpuarray.pool import (
    GpuAveragePoolGrad,
    GpuDownsampleFactorMaxGradGrad,
    GpuMaxPoolGrad,
    GpuPool,
)
from aesara.gradient import Lop, Rop, grad
from aesara.tensor.signal.pool import (
    AveragePoolGrad,
    DownsampleFactorMaxGradGrad,
    MaxPoolGrad,
    Pool,
)
from tests import unittest_tools as utt
from tests.gpuarray.config import mode_with_gpu, mode_without_gpu
from tests.gpuarray.test_basic_ops import random


class TestPool:
    def test_pool_py_interface(self):
        shp = (2, 2, 2, 2)
        inp = aesara.shared(random(*shp), "a")
        inp = at.as_tensor_variable(inp)
        with pytest.raises(ValueError):
            # test when pad >= ws
            ds_op = GpuPool(ignore_border=True, ndim=2)
            ds_op(inp, [2, 2], pad=[3, 3])
        with pytest.raises(ValueError):
            # test when ignore_border and pad >= 0
            ds_op = GpuPool(ignore_border=False, ndim=2)
            ds_op(inp, [2, 2], pad=[1, 1])

    def test_pool_c_interface(self):
        gpu_mode = mode_with_gpu.excluding("cudnn")
        gpu_mode.check_py_code = False

        shp = (2, 2, 2, 2)
        inp = aesara.shared(random(*shp), "a")
        inp = at.as_tensor_variable(inp)
        with pytest.raises(ValueError):
            # test when ignore_border and pad >= 0
            ds_op = GpuPool(ignore_border=False, ndim=2)
            pad = at.as_tensor_variable([1, 1])
            f = aesara.function([], ds_op(inp, [2, 2], pad=pad), mode=gpu_mode)
            f()

    def test_pool_big_ws(self):
        gpu_mode = mode_with_gpu.excluding("cudnn")
        gpu_mode.check_py_code = False

        shp = (2, 2, 2, 2)
        inp = aesara.shared(random(*shp), "a")
        inp = at.as_tensor_variable(inp)
        ds_op = GpuPool(ignore_border=False, mode="average_exc_pad", ndim=2)
        pad = at.as_tensor_variable([0, 0])
        f = aesara.function(
            [], ds_op(inp, [5, 5], stride=[1, 1], pad=pad), mode=gpu_mode
        )
        f()


def test_pool2d():
    shps = [
        (1, 12),
        (1, 1, 12),
        (1, 1, 1, 12),
        (1, 1, 2, 2),
        (1, 1, 1, 1),
        (1, 1, 4, 4),
        (1, 1, 10, 11),
        (1, 2, 2, 2),
        (3, 5, 4, 4),
        (25, 1, 7, 7),
        (1, 1, 12, 12),
        (1, 1, 2, 14),
        (1, 1, 12, 14),
        (1, 1, 14, 14),
        (1, 1, 16, 16),
        (1, 1, 18, 18),
        (1, 1, 24, 24),
        (1, 6, 24, 24),
        (10, 1, 24, 24),
        (10, 6, 24, 24),
        (30, 6, 12, 12),
        (30, 2, 24, 24),
        (30, 6, 24, 24),
        (10, 10, 10, 11),
        (1, 1, 10, 1025),
        (1, 1, 10, 1023),
        (1, 1, 1025, 10),
        (1, 1, 1023, 10),
        (3, 2, 16, 16, 16),
        (3, 2, 6, 6, 6, 5),
        (3, 2, 6, 6, 6, 5, 7),
    ]

    np.random.default_rng(utt.fetch_seed()).shuffle(shps)
    test_ws = (2, 2), (3, 2), (1, 1)
    test_st = (2, 2), (3, 2), (1, 1)
    test_mode = ["max", "sum", "average_inc_pad", "average_exc_pad"]

    ref_mode = copy.copy(mode_without_gpu)
    ref_mode.check_py_code = False
    gpu_mode = mode_with_gpu.excluding("cudnn")
    gpu_mode.check_py_code = False

    for shp in shps:
        for mode, ws, st in itertools.product(test_mode, test_ws, test_st):
            if ws[0] > shp[-2] or ws[1] > shp[-1]:
                continue
            for ignore_border, pad in zip((True, False), [(1, 1), (0, 0)]):
                if pad[0] >= ws[0] or pad[1] >= ws[1]:
                    continue
                if mode == "average_exc_pad" and (pad[0] > 0 or pad[1] > 0):
                    continue
                # print('test_pool2d', shp, ws, st, pad, mode, ignore_border)
                ds_op = Pool(ndim=len(ws), mode=mode, ignore_border=ignore_border)

                a = aesara.shared(random(*shp), "a")
                a_pooled = ds_op(at.as_tensor_variable(a), ws, st, pad)

                f = aesara.function([], a_pooled, mode=gpu_mode)
                f2 = aesara.function([], a_pooled, mode=ref_mode)

                assert any(
                    [isinstance(node.op, GpuPool) for node in f.maker.fgraph.toposort()]
                )
                assert any(
                    [isinstance(node.op, Pool) for node in f2.maker.fgraph.toposort()]
                )
                assert np.allclose(f(), f2()), (shp, ws, st, pad, mode, ignore_border)

                a_pooled_grad = grad(a_pooled.sum(), a)

                g = aesara.function([], a_pooled_grad, mode=gpu_mode)
                g2 = aesara.function([], a_pooled_grad, mode=ref_mode)

                if mode == "max":
                    gop = GpuMaxPoolGrad
                    gop2 = MaxPoolGrad
                else:
                    gop = GpuAveragePoolGrad
                    gop2 = AveragePoolGrad
                assert any(
                    [isinstance(node.op, gop) for node in g.maker.fgraph.toposort()]
                )
                assert any(
                    [isinstance(node.op, gop2) for node in g2.maker.fgraph.toposort()]
                )

                assert np.allclose(g(), g2()), (shp, ws, st, pad, mode, ignore_border)

                # test rop and grad grad for max pooling
                # for average pooling grad grad is just average pooling grad
                if mode != "max":
                    continue

                ea = aesara.shared(random(*shp), "ea")

                gr = aesara.function([], Rop(a_pooled, a, ea), mode=gpu_mode)
                gr2 = aesara.function([], Rop(a_pooled, a, ea), mode=ref_mode)

                assert any(
                    [
                        isinstance(node.op, GpuDownsampleFactorMaxGradGrad)
                        for node in gr.maker.fgraph.toposort()
                    ]
                )
                assert any(
                    [
                        isinstance(node.op, DownsampleFactorMaxGradGrad)
                        for node in gr2.maker.fgraph.toposort()
                    ]
                )
                assert np.allclose(gr(), gr2()), (shp, ws, st, pad, mode, ignore_border)

                ggf = Lop(grad((a_pooled ** 2).sum(), a), a, a)

                gg = aesara.function([], ggf, mode=gpu_mode)
                gg2 = aesara.function([], ggf, mode=ref_mode)

                assert any(
                    [
                        isinstance(node.op, GpuDownsampleFactorMaxGradGrad)
                        for node in gg.maker.fgraph.toposort()
                    ]
                )
                assert any(
                    [
                        isinstance(node.op, DownsampleFactorMaxGradGrad)
                        for node in gg2.maker.fgraph.toposort()
                    ]
                )
                assert np.allclose(gg(), gg2()), (shp, ws, st, pad, mode, ignore_border)


def test_pool3d():
    shps = [
        (1, 1, 12),
        (1, 1, 1, 1, 1),
        (1, 1, 1, 1, 1025),
        (1, 1, 2, 2, 2),
        (1, 1, 7, 7, 7),
        (1, 1, 9, 10, 11),
        (1, 6, 18, 18, 18),
        (1, 1, 6, 24, 24),
        (1, 10, 1, 24, 24),
        (1, 10, 6, 24, 24),
        (1, 30, 6, 12, 12),
        (1, 30, 2, 24, 24),
        (1, 30, 6, 24, 24),
        (1, 10, 10, 10, 11),
        (1, 1, 10, 10, 1025),
        (1, 1, 10, 10, 1023),
        (1, 1, 10, 1025, 10),
        (1, 1, 10, 1023, 10),
        (3, 2, 6, 6, 6, 5),
        (3, 2, 6, 6, 6, 5, 7),
    ]

    np.random.default_rng(utt.fetch_seed()).shuffle(shps)
    test_ws = (2, 2, 2), (3, 2, 3), (1, 1, 1)
    test_st = (2, 2, 2), (2, 3, 2), (1, 1, 1)
    test_mode = ["max", "sum", "average_inc_pad", "average_exc_pad"]

    ref_mode = copy.copy(mode_without_gpu)
    ref_mode.check_py_code = False
    gpu_mode = mode_with_gpu.excluding("cudnn")
    gpu_mode.check_py_code = False

    for shp in shps:
        for mode, ws, st in itertools.product(test_mode, test_ws, test_st):
            if ws[0] > shp[-3] or ws[1] > shp[-2] or ws[2] > shp[-1]:
                continue
            for ignore_border, pad in zip((True, False), [(1, 1, 1), (0, 0, 0)]):
                if pad[0] >= ws[0] or pad[1] >= ws[1] or pad[2] >= ws[2]:
                    continue
                if mode == "average_exc_pad" and (
                    pad[0] > 0 or pad[1] > 0 or pad[2] > 0
                ):
                    continue
                # print('test_pool3d', shp, ws, st, pad, mode, ignore_border)
                ds_op = Pool(ndim=len(ws), mode=mode, ignore_border=ignore_border)

                a = aesara.shared(random(*shp), "a")
                a_pooled = ds_op(at.as_tensor_variable(a), ws, st, pad)

                f = aesara.function([], a_pooled, mode=gpu_mode)
                f2 = aesara.function([], a_pooled, mode=ref_mode)

                assert any(
                    [isinstance(node.op, GpuPool) for node in f.maker.fgraph.toposort()]
                )
                assert any(
                    [isinstance(node.op, Pool) for node in f2.maker.fgraph.toposort()]
                )
                assert np.allclose(f(), f2()), (shp, ws, st, pad, mode, ignore_border)

                a_pooled_grad = grad(a_pooled.sum(), a)

                g = aesara.function([], a_pooled_grad, mode=gpu_mode)
                g2 = aesara.function([], a_pooled_grad, mode=ref_mode)

                if mode == "max":
                    gop = GpuMaxPoolGrad
                    gop2 = MaxPoolGrad
                else:
                    gop = GpuAveragePoolGrad
                    gop2 = AveragePoolGrad
                assert any(
                    [isinstance(node.op, gop) for node in g.maker.fgraph.toposort()]
                )
                assert any(
                    [isinstance(node.op, gop2) for node in g2.maker.fgraph.toposort()]
                )

                assert np.allclose(g(), g2()), (shp, ws, st, pad, mode, ignore_border)

                # test rop and grad grad for max pooling
                # for average pooling grad grad is just average pooling grad
                if mode != "max":
                    continue

                ea = aesara.shared(random(*shp), "ea")

                gr = aesara.function([], Rop(a_pooled, a, ea), mode=gpu_mode)
                gr2 = aesara.function([], Rop(a_pooled, a, ea), mode=ref_mode)

                assert any(
                    [
                        isinstance(node.op, GpuDownsampleFactorMaxGradGrad)
                        for node in gr.maker.fgraph.toposort()
                    ]
                )
                assert any(
                    [
                        isinstance(node.op, DownsampleFactorMaxGradGrad)
                        for node in gr2.maker.fgraph.toposort()
                    ]
                )
                assert np.allclose(gr(), gr2()), (shp, ws, st, pad, mode, ignore_border)

                ggf = Lop(grad((a_pooled ** 2).sum(), a), a, a)

                gg = aesara.function([], ggf, mode=gpu_mode)
                gg2 = aesara.function([], ggf, mode=ref_mode)

                assert any(
                    [
                        isinstance(node.op, GpuDownsampleFactorMaxGradGrad)
                        for node in gg.maker.fgraph.toposort()
                    ]
                )
                assert any(
                    [
                        isinstance(node.op, DownsampleFactorMaxGradGrad)
                        for node in gg2.maker.fgraph.toposort()
                    ]
                )
                assert np.allclose(gg(), gg2()), (shp, ws, st, pad, mode, ignore_border)
