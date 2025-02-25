import logging

import numpy as np
import pytest


pygpu = pytest.importorskip("pygpu")  # noqa

from collections import OrderedDict
from io import StringIO
from itertools import chain, product

import aesara
import aesara.tensor as at
import tests.unittest_tools as utt
from aesara.configdefaults import SUPPORTED_DNN_CONV_ALGO_FWD
from aesara.gpuarray import dnn
from aesara.gpuarray.basic_ops import GpuAllocEmpty
from aesara.gpuarray.type import GpuArrayType, gpuarray_shared_constructor
from aesara.tensor.math import (
    ceil,
    clip,
    dot,
    floor,
    log,
    max_and_argmax,
    mean,
    minimum,
    mod,
    prod,
    reciprocal,
    sqrt,
)
from aesara.tensor.math import sum as at_sum
from aesara.tensor.nnet import batchnorm, conv2d, softmax, softmax_legacy
from aesara.tensor.nnet.abstract_conv import (
    get_conv_gradinputs_shape,
    get_conv_output_shape,
)
from aesara.tensor.nnet.basic import LogSoftmax, Softmax, SoftmaxGrad
from aesara.tensor.nnet.corr import CorrMM
from aesara.tensor.nnet.corr3d import Corr3dMM
from aesara.tensor.shape import reshape
from aesara.tensor.signal.pool import (
    AveragePoolGrad,
    MaxPoolGrad,
    Pool,
    pool_2d,
    pool_3d,
)
from aesara.tensor.type import (
    TensorType,
    dtensor4,
    dtensor5,
    ftensor4,
    ftensor5,
    matrix,
    tensor,
    tensor3,
    tensor4,
    tensor5,
    tensor6,
    vector,
)
from tests.gpuarray import test_nnet
from tests.gpuarray.config import (
    mode_with_gpu,
    mode_without_gpu,
    ref_cast,
    test_ctx_name,
)
from tests.gpuarray.rnn_support import GRU, LSTM, Model, WrapperLayer
from tests.tensor.nnet.test_abstract_conv import (
    TestGroupedConv3dNoOptim,
    TestGroupedConvNoOptim,
)


if not dnn.dnn_available(test_ctx_name):
    pytest.skip(dnn.dnn_available.msg, allow_module_level=True)

mode_with_gpu = mode_with_gpu.including()
# Globally disabled for mode_without_gpu
mode_with_gpu.check_py_code = False


# This variable will store the list of pooling modes available with the current runtime cuDNN version.
# Don't use this variable directly, always call `get_dnn_pool_modes()` instead.
dnn_pool_modes = None


def get_dnn_pool_modes():
    # This function is called only by pooling tests to initialize and/or get dnn_pool_modes.
    global dnn_pool_modes
    if dnn_pool_modes is None:
        from .. import cudnn_defs

        dnn_pool_modes = cudnn_defs.get_definitions(
            dnn.version(raises=False)
        ).cudnnPoolingMode_t.get_aliases()
    return dnn_pool_modes


# If using float16, set CUDNN precision to float32
def set_precision(floatX):
    if floatX == "float16":
        precision = "float32"
    else:
        precision = aesara.config.floatX
    return precision


def test_dnn_conv_desc_merge():
    kern_shp = at.as_tensor_variable(np.asarray([3, 1, 2, 2]).astype("int64"))
    desc1 = dnn.GpuDnnConvDesc(
        border_mode="valid", subsample=(2, 2), dilation=(1, 1), conv_mode="conv"
    )(kern_shp)
    desc2 = dnn.GpuDnnConvDesc(
        border_mode="full", subsample=(1, 1), dilation=(1, 1), conv_mode="cross"
    )(kern_shp)
    # CDataType is not DeepCopyable so this will crash if we don't use
    # borrow=True
    f = aesara.function(
        [],
        [aesara.Out(desc1, borrow=True), aesara.Out(desc2, borrow=True)],
        mode=mode_with_gpu,
    )

    d1, d2 = f()

    # This will be the case if they are merged, which would be bad.
    assert d1 != d2


def test_dnn_conv_merge():
    # This test that we merge correctly multiple dnn_conv.
    img_shp = [2, 5, 6, 8]
    kern_shp = [3, 5, 5, 6]
    img = tensor4("img")
    kern = tensor4("kern")
    out = tensor4("out")
    desc = dnn.GpuDnnConvDesc(border_mode="valid")(kern.shape)

    # Test forward op
    o1 = dnn.dnn_conv(img, kern)
    o2 = dnn.dnn_conv(img, kern)
    f = aesara.function([img, kern], [o1, o2], mode=mode_with_gpu)
    d1, d2 = f(
        np.random.rand(*img_shp).astype(aesara.config.floatX),
        np.random.rand(*kern_shp).astype(aesara.config.floatX),
    )
    topo = f.maker.fgraph.toposort()
    assert len([n for n in topo if isinstance(n.op, dnn.GpuDnnConv)]) == 1

    # Test grad w op
    o1 = dnn.GpuDnnConvGradW()(img, kern, out, desc)
    o2 = dnn.GpuDnnConvGradW()(img, kern, out, desc)
    f = aesara.function([img, kern, out], [o1, o2], mode=mode_with_gpu)
    topo = f.maker.fgraph.toposort()
    assert len([n for n in topo if isinstance(n.op, dnn.GpuDnnConvGradW)]) == 1

    # Test grad i op
    o1 = dnn.GpuDnnConvGradI()(img, kern, out, desc)
    o2 = dnn.GpuDnnConvGradI()(img, kern, out, desc)
    f = aesara.function([img, kern, out], [o1, o2], mode=mode_with_gpu)
    topo = f.maker.fgraph.toposort()
    assert len([n for n in topo if isinstance(n.op, dnn.GpuDnnConvGradI)]) == 1


def test_dnn_conv_inplace():
    # This test that we have inplace work correctly even when
    # GpuAllocEmpty get merged together.

    img_shp = [2, 5, 6, 8]
    kern_shp = [3, 5, 5, 6]
    img = tensor4("img")
    kern = tensor4("kern")
    out = tensor4("out")
    desc1 = dnn.GpuDnnConvDesc(border_mode="valid", conv_mode="conv")(kern.shape)
    desc2 = dnn.GpuDnnConvDesc(border_mode="valid", conv_mode="cross")(kern.shape)

    # Test forward op
    o1 = dnn.dnn_conv(img, kern, conv_mode="conv")
    o2 = dnn.dnn_conv(img, kern, conv_mode="cross")
    f = aesara.function([img, kern], [o1, o2], mode=mode_with_gpu)
    d1, d2 = f(
        np.random.rand(*img_shp).astype(aesara.config.floatX),
        np.random.rand(*kern_shp).astype(aesara.config.floatX),
    )
    topo = f.maker.fgraph.toposort()
    convs = [n for n in topo if isinstance(n.op, dnn.GpuDnnConv)]
    assert len(convs) == 2
    assert all(node.op.inplace for node in convs)
    assert len([n for n in topo if isinstance(n.op, GpuAllocEmpty)]) == 2

    # Test grad w op
    out = GpuAllocEmpty(kern.dtype, test_ctx_name)(*kern.shape)
    o1 = dnn.GpuDnnConvGradW()(img, kern, out, desc1)
    o2 = dnn.GpuDnnConvGradW()(img, kern, out, desc2)
    f = aesara.function([img, kern], [o1, o2], mode=mode_with_gpu)
    topo = f.maker.fgraph.toposort()
    convs = [n for n in topo if isinstance(n.op, dnn.GpuDnnConvGradW)]
    assert len(convs) == 2
    assert all(node.op.inplace for node in convs)
    assert len([n for n in topo if isinstance(n.op, GpuAllocEmpty)]) == 2

    # Test grad i op
    out = GpuAllocEmpty(img.dtype, test_ctx_name)(*img.shape)
    o1 = dnn.GpuDnnConvGradI()(img, kern, out, desc1)
    o2 = dnn.GpuDnnConvGradI()(img, kern, out, desc2)
    f = aesara.function([img, kern], [o1, o2], mode=mode_with_gpu)
    topo = f.maker.fgraph.toposort()
    convs = [n for n in topo if isinstance(n.op, dnn.GpuDnnConvGradI)]
    assert len(convs) == 2
    assert all(node.op.inplace for node in convs)
    assert len([n for n in topo if isinstance(n.op, GpuAllocEmpty)]) == 2


def run_dnn_conv_invalid_precision(ndim):
    bc = (False,) * (ndim + 2)
    img = tensor(aesara.config.floatX, broadcastable=bc)
    kerns = tensor(aesara.config.floatX, broadcastable=bc)
    topgrad = tensor(aesara.config.floatX, broadcastable=bc)
    shape = np.arange(ndim + 2)
    if ndim == 2:
        dnn_conv_func = dnn.dnn_conv
        dnn_gradw_func = dnn.dnn_gradweight
        dnn_gradi_func = dnn.dnn_gradinput
    elif ndim == 3:
        dnn_conv_func = dnn.dnn_conv3d
        dnn_gradw_func = dnn.dnn_gradweight3d
        dnn_gradi_func = dnn.dnn_gradinput3d

    def dnn_gradw(precision):
        return dnn_gradw_func(img, topgrad, shape, precision=precision)

    def dnn_gradi(precision):
        return dnn_gradi_func(kerns, topgrad, shape, precision=precision)

    def dnn_conv(precision, border_mode, direction_hint):
        return dnn_conv_func(
            None,
            img,
            kerns,
            border_mode=border_mode,
            direction_hint=direction_hint,
            precision=precision,
        )

    dnn_gradw("float64")
    dnn_gradw("float32")
    with pytest.raises(TypeError):
        dnn_gradw("float16")

    dnn_gradi("float64")
    dnn_gradi("float32")
    with pytest.raises(TypeError):
        dnn_gradi("float16")

    for precision in ("float64", "float32"):
        dnn_conv(precision, "valid", None)
        dnn_conv(precision, "valid", "bprop weights")
        dnn_conv(precision, "full", None)
        dnn_conv(precision, "full", "forward!")

    dnn_conv("float16", "valid", None)
    with pytest.raises(TypeError):
        dnn_conv("float16", "valid", "bprop weights")
    with pytest.raises(TypeError):
        dnn_conv("float16", "full", None)
    dnn_conv("float16", "full", "forward!")


def test_dnn_conv_invalid_precision():
    run_dnn_conv_invalid_precision(2)
    run_dnn_conv_invalid_precision(3)


def test_dnn_conv_mixed_dtype():
    mf = ftensor4()
    md = dtensor4()

    def assert_types(conv):
        dt = conv.owner.inputs[0].dtype
        assert conv.owner.inputs[1].dtype == dt
        assert conv.owner.inputs[2].dtype == dt

    assert_types(dnn.dnn_conv(md, mf, precision="as_input"))
    assert_types(dnn.dnn_conv(mf, md, precision="as_input"))
    assert_types(dnn.dnn_gradweight(mf, md, kerns_shp=mf.shape, precision="as_input"))
    assert_types(dnn.dnn_gradweight(md, mf, kerns_shp=mf.shape, precision="as_input"))
    assert_types(dnn.dnn_gradinput(mf, md, img_shp=mf.shape, precision="as_input"))
    assert_types(dnn.dnn_gradinput(md, mf, img_shp=mf.shape, precision="as_input"))


def test_dnn_conv3d_mixed_dtype():
    mf = ftensor5()
    md = dtensor5()

    def assert_types(conv):
        dt = conv.owner.inputs[0].dtype
        assert conv.owner.inputs[1].dtype == dt
        assert conv.owner.inputs[2].dtype == dt

    assert_types(dnn.dnn_conv3d(None, md, mf, precision="as_input"))
    assert_types(dnn.dnn_conv3d(None, mf, md, precision="as_input"))
    assert_types(dnn.dnn_gradweight3d(mf, md, kerns_shp=mf.shape, precision="as_input"))
    assert_types(dnn.dnn_gradweight3d(md, mf, kerns_shp=mf.shape, precision="as_input"))
    assert_types(dnn.dnn_gradinput3d(mf, md, img_shp=mf.shape, precision="as_input"))
    assert_types(dnn.dnn_gradinput3d(md, mf, img_shp=mf.shape, precision="as_input"))


def test_pooling():

    modes = get_dnn_pool_modes()

    x = tensor4()
    for mode, pad in product(modes, ((0, 0), (1, 0), (0, 1), (2, 3), (3, 2))):
        if pad != (0, 0) and mode == "average_exc_pad":
            # Not implemented
            continue

        for ws in (4, 2, 5):
            for stride in (2, 3):
                if stride > ws:
                    continue
                if pad[0] > stride or pad[1] > stride:
                    # Not implemented
                    continue
                # We will check that the opt introduced it.
                out = pool_2d(
                    x,
                    (ws, ws),
                    stride=(stride, stride),
                    ignore_border=True,
                    pad=pad,
                    mode=mode,
                )
                mode_without_gpu2 = mode_without_gpu.including()
                mode_without_gpu2.check_isfinite = False

                # GPU implementation
                f_gpu = aesara.function([x], out, mode=mode_with_gpu)
                assert any(
                    [
                        isinstance(node.op, dnn.GpuDnnPool)
                        for node in f_gpu.maker.fgraph.apply_nodes
                    ]
                )

                # CPU implementation
                f_cpu = aesara.function([x], out, mode=mode_without_gpu2)
                assert not any(
                    [
                        isinstance(node.op, dnn.GpuDnnPool)
                        for node in f_cpu.maker.fgraph.apply_nodes
                    ]
                )
                assert any(
                    [
                        isinstance(node.op, Pool)
                        for node in f_cpu.maker.fgraph.apply_nodes
                    ]
                )

                for shp in [
                    (1, 10, 100, 100),
                    (1, 3, 99, 99),
                    (32, 1, 147, 197),
                ]:
                    data = np.random.normal(0, 1, shp).astype(aesara.config.floatX)
                    a = f_cpu(data).__array__()
                    b = f_gpu(data).__array__()
                    utt.assert_allclose(a, b)

        # Test the grad
        for shp in [(1, 1, 2, 2), (1, 1, 3, 3)]:
            data = np.random.normal(0, 1, shp).astype(aesara.config.floatX) * 10

            ws = 2
            stride = 2
            if pad[0] > stride or pad[1] > stride:
                # Not implemented
                continue

            # This tests the CPU grad + opt + GPU implementation
            def fn(x):
                return pool_2d(x, (ws, ws), ignore_border=True, pad=pad, mode=mode)

            utt.verify_grad(fn, [data], mode=mode_with_gpu)
            # Confirm that the opt would have inserted it.
            fg = aesara.function([x], aesara.grad(fn(x).sum(), x), mode=mode_with_gpu)
            assert any(
                [
                    isinstance(node.op, dnn.GpuDnnPoolGrad)
                    for node in fg.maker.fgraph.toposort()
                ]
            )

            # Test the GPU grad + GPU implementation
            def fn(x):
                dnn_op = dnn.dnn_pool(
                    x, ws=(ws, ws), stride=(stride, stride), pad=pad, mode=mode
                )
                return dnn_op

            utt.verify_grad(fn, [data], mode=mode_with_gpu)
            # Confirm that we get the good op.
            fg = aesara.function([x], aesara.grad(fn(x).sum(), x), mode=mode_with_gpu)
            assert any(
                [
                    isinstance(node.op, dnn.GpuDnnPoolGrad)
                    for node in fg.maker.fgraph.toposort()
                ]
            )


# This test will be run with different values of 'mode'
# (see next test below).
def run_pooling_with_tensor_vars(mode):

    x = tensor4()
    ws = aesara.shared(np.array([2, 2], dtype="int32"))
    stride = aesara.shared(np.array([1, 1], dtype="int32"))
    pad = aesara.shared(np.array([0, 0], dtype="int32"))

    def fn(x):
        dnn_op = dnn.dnn_pool(x, ws=ws, stride=stride, pad=pad, mode=mode)
        return dnn_op

    for shp in [(1, 1, 2, 2), (1, 1, 3, 3)]:
        data = np.random.normal(0, 1, shp).astype(aesara.config.floatX) * 10
        utt.verify_grad(fn, [data], mode=mode_with_gpu)

    mode_without_gpu2 = mode_without_gpu.including()
    mode_without_gpu2.check_isfinite = False

    # GPU implementation
    f_gpu = aesara.function([x], fn(x), mode=mode_with_gpu)
    assert any(
        isinstance(node.op, dnn.GpuDnnPool) for node in f_gpu.maker.fgraph.apply_nodes
    )

    # CPU implementation
    out_cpu = pool_2d(x, ws, ignore_border=True, stride=stride, pad=pad, mode=mode)
    f_cpu = aesara.function([x], out_cpu, mode=mode_without_gpu2)
    assert not any(
        isinstance(node.op, dnn.GpuDnnPool) for node in f_cpu.maker.fgraph.apply_nodes
    )
    assert any(isinstance(node.op, Pool) for node in f_cpu.maker.fgraph.apply_nodes)

    i = 1
    for shp in [(1, 10, 100, 100), (1, 3, 99, 99), (32, 1, 147, 197)]:
        data = np.random.normal(0, 1, shp).astype(aesara.config.floatX)

        # Change the window size dynamically
        ws.set_value(np.array([i, i]).astype("int32"))
        a = f_gpu(data).__array__()
        b = f_cpu(data).__array__()
        utt.assert_allclose(a, b)
        i += 1


def test_pooling_with_tensor_vars():
    # Let's test for mode 'max' and also for 'max_deterministic' if available.
    for mode in [m for m in get_dnn_pool_modes() if m in ("max", "max_deterministic")]:
        run_pooling_with_tensor_vars(mode)


@pytest.mark.skipif(dnn.version(raises=False) < 3000, reason=dnn.dnn_available.msg)
def test_pooling3d():
    # 3d pooling requires version 3 or newer.

    # We force the FAST_RUN as we don't want the reference to run in DebugMode.
    mode_without_gpu_ref = aesara.compile.mode.get_mode("FAST_RUN").excluding(
        "gpuarray"
    )

    modes = get_dnn_pool_modes()

    x = tensor5()
    for mode, pad in product(
        modes,
        ((0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1), (2, 3, 2), (3, 2, 2), (2, 2, 3)),
    ):
        if pad != (0, 0, 0) and mode == "average_exc_pad":
            # Not implemented
            continue

        for ws in (4, 2, 5):
            for stride in (2, 3):
                if stride > ws:
                    continue
                if pad[0] > stride or pad[1] > stride or pad[2] > stride:
                    # Not implemented
                    continue
                out = pool_3d(
                    x,
                    (ws, ws, ws),
                    stride=(stride, stride, stride),
                    ignore_border=True,
                    pad=pad,
                    mode=mode,
                )

                # GPU implementation
                f_gpu = aesara.function([x], out, mode=mode_with_gpu)
                assert any(
                    [
                        isinstance(node.op, dnn.GpuDnnPool)
                        for node in f_gpu.maker.fgraph.apply_nodes
                    ]
                )

                # CPU implementation
                f_cpu = aesara.function([x], out, mode=mode_without_gpu_ref)
                assert not any(
                    [
                        isinstance(node.op, dnn.GpuDnnPool)
                        for node in f_cpu.maker.fgraph.apply_nodes
                    ]
                )
                assert any(
                    [
                        isinstance(node.op, Pool)
                        for node in f_cpu.maker.fgraph.apply_nodes
                    ]
                )

                for shp in [
                    (1, 5, 50, 20, 50),
                    (1, 3, 99, 99, 29),
                    (2, 1, 147, 97, 37),
                ]:
                    data = np.random.normal(0, 1, shp).astype(aesara.config.floatX)
                    a = f_cpu(data).__array__()
                    b = f_gpu(data).__array__()
                    utt.assert_allclose(a, b, atol=np.finfo(aesara.config.floatX).eps)

        # Test the grad
        for shp in [
            (1, 1, 2, 2, 2),
            (1, 1, 3, 3, 3),
            (1, 1, 3, 3, 4),
            (1, 1, 3, 4, 3),
            (1, 1, 4, 3, 3),
            (1, 1, 4, 4, 4),
            (1, 1, 5, 5, 5),
        ]:
            data = np.random.normal(0, 1, shp).astype(aesara.config.floatX) * 10

            ws = 2
            stride = 2
            if pad[0] > stride or pad[1] > stride or pad[2] > stride:
                # Not implemented
                continue

            # Test the GPU grad + GPU implementation
            def fn(x):
                dnn_op = dnn.dnn_pool(
                    x,
                    ws=(ws, ws, ws),
                    stride=(stride, stride, stride),
                    pad=pad,
                    mode=mode,
                )
                return dnn_op

            utt.verify_grad(fn, [data], mode=mode_with_gpu)
            # Confirm that we get the good op.
            fg = aesara.function([x], aesara.grad(fn(x).sum(), x), mode=mode_with_gpu)
            assert any(
                [
                    isinstance(node.op, dnn.GpuDnnPoolGrad)
                    for node in fg.maker.fgraph.toposort()
                ]
            )


def test_pooling_opt():

    # 2D pooling
    x = matrix()

    f = aesara.function(
        [x],
        pool_2d(x, ws=(2, 2), mode="average_inc_pad", ignore_border=True),
        mode=mode_with_gpu,
    )

    assert any(isinstance(n.op, dnn.GpuDnnPool) for n in f.maker.fgraph.toposort())

    f(np.zeros((10, 10), dtype=aesara.config.floatX))

    # gradient of 2D pooling
    f = aesara.function(
        [x],
        aesara.grad(
            pool_2d(x, ws=(2, 2), mode="average_inc_pad", ignore_border=True).sum(), x
        ),
        mode=mode_with_gpu.including("cudnn"),
    )

    assert any(isinstance(n.op, dnn.GpuDnnPoolGrad) for n in f.maker.fgraph.toposort())

    f(np.zeros((10, 10), dtype=aesara.config.floatX))

    # Test sum pooling
    f = aesara.function(
        [x], pool_2d(x, ws=(2, 3), mode="sum", ignore_border=True), mode=mode_with_gpu
    )

    assert any(isinstance(n.op, dnn.GpuDnnPool) for n in f.maker.fgraph.toposort())
    data = np.random.rand(10, 10).astype(aesara.config.floatX)
    f(data)

    # 3D pooling
    x = tensor3()

    f = aesara.function(
        [x],
        pool_3d(x, ws=(2, 2, 2), mode="average_inc_pad", ignore_border=True),
        mode=mode_with_gpu,
    )

    assert any(isinstance(n.op, dnn.GpuDnnPool) for n in f.maker.fgraph.toposort())

    f(np.zeros((10, 10, 10), dtype=aesara.config.floatX))

    # gradient of 3D pooling
    f = aesara.function(
        [x],
        aesara.grad(
            pool_3d(x, ws=(2, 2, 2), mode="average_inc_pad", ignore_border=True).sum(),
            x,
        ),
        mode=mode_with_gpu.including("cudnn"),
    )

    assert any(isinstance(n.op, dnn.GpuDnnPoolGrad) for n in f.maker.fgraph.toposort())

    f(np.zeros((10, 10, 10), dtype=aesara.config.floatX))


def test_pooling_opt_arbitrary_dimensions():
    # test if input with an arbitrary number of non-pooling dimensions
    # is correctly reshaped to run on the GPU

    modes = get_dnn_pool_modes()

    for n_non_pool_dims in (0, 1, 2, 3):
        for ws in ((2, 2), (3, 3, 3)):
            # create input shape: non-pooling dimensions
            # followed by 2 or 3 pooling dimensions
            shp = tuple(range(2, 2 + n_non_pool_dims)) + tuple(range(5, 5 + len(ws)))
            data = np.random.normal(0, 1, shp).astype(aesara.config.floatX)
            input = gpuarray_shared_constructor(data)

            for mode in modes:
                out_pool = Pool(ndim=len(ws), mode=mode, ignore_border=True)(input, ws)
                out_pool_grad = aesara.grad(at_sum(out_pool), wrt=input)
                out = [out_pool, out_pool_grad]

                # run on GPU
                fg = aesara.function([], out, mode=mode_with_gpu)
                assert any(
                    [
                        isinstance(node.op, dnn.GpuDnnPool)
                        for node in fg.maker.fgraph.toposort()
                    ]
                )
                assert any(
                    [
                        isinstance(node.op, dnn.GpuDnnPoolGrad)
                        for node in fg.maker.fgraph.toposort()
                    ]
                )
                res_gpu = fg()

                # run on CPU
                fc = aesara.function([], out, mode=mode_without_gpu)
                assert any(
                    [isinstance(node.op, Pool) for node in fc.maker.fgraph.toposort()]
                )
                if mode in ("max", "max_deterministic"):
                    assert any(
                        [
                            isinstance(node.op, MaxPoolGrad)
                            for node in fc.maker.fgraph.toposort()
                        ]
                    )
                else:
                    assert any(
                        [
                            isinstance(node.op, AveragePoolGrad)
                            for node in fc.maker.fgraph.toposort()
                        ]
                    )
                res_cpu = fg()

                # check for similarity
                utt.assert_allclose(res_gpu[0], res_cpu[0])
                utt.assert_allclose(res_gpu[1], res_cpu[1])


def test_pooling_empty_batch():
    img_shp = (0, 5, 6, 8)
    img = ftensor4("img")

    o = dnn.dnn_pool(img, (2, 2), (2, 2))
    f = aesara.function([img], o, mode=mode_with_gpu)
    d = f(np.random.rand(*img_shp).astype("float32"))
    assert d.shape == (0, 5, 3, 4)

    g = aesara.grad(at_sum(o), wrt=img)
    f = aesara.function([img], g, mode=mode_with_gpu)
    d = f(np.random.rand(*img_shp).astype("float32"))
    # Not sure what to assert, it should just pass, that's all.
    assert d.shape == (0, 5, 6, 8)


def test_dnn_tag():
    # Test that if cudnn isn't avail we crash and that if it is avail, we use it.

    x = tensor4()
    old = aesara.config.on_opt_error
    aesara.config.on_opt_error = "raise"

    sio = StringIO()
    handler = logging.StreamHandler(sio)
    logging.getLogger("tests.compile.test_dnn").addHandler(handler)
    # Silence original handler when intentionnally generating warning messages
    logging.getLogger("aesara").removeHandler(aesara.logging_default_handler)
    raised = False
    try:
        f = aesara.function(
            [x],
            pool_2d(x, ws=(2, 2), ignore_border=True),
            mode=mode_with_gpu.including("cudnn"),
        )
    except (AssertionError, RuntimeError):
        assert not dnn.dnn_available(test_ctx_name)
        raised = True
    finally:
        aesara.config.on_opt_error = old
        logging.getLogger("tests.compile.test_dnn").removeHandler(handler)
        logging.getLogger("aesara").addHandler(aesara.logging_default_handler)

    if not raised:
        assert dnn.dnn_available(test_ctx_name)
        assert any(
            [isinstance(n.op, dnn.GpuDnnPool) for n in f.maker.fgraph.toposort()]
        )


class TestDnnInferShapes(utt.InferShapeTester):

    border_modes = ["valid", "full", "half"]
    conv_modes = ["conv", "cross"]

    def setup_method(self):
        self.mode = mode_with_gpu
        super().setup_method()

    def test_softmax(self):
        t = tensor4("t")
        rand_tensor = np.asarray(np.random.rand(5, 4, 3, 2), dtype=aesara.config.floatX)
        self._compile_and_check(
            [t],
            [dnn.GpuDnnSoftmax("accurate", "channel")(t)],
            [rand_tensor],
            dnn.GpuDnnSoftmax,
        )

        self._compile_and_check(
            [t],
            [aesara.grad(dnn.GpuDnnSoftmax("accurate", "channel")(t).mean(), t)],
            [rand_tensor],
            dnn.GpuDnnSoftmaxGrad,
        )

    def _test_conv(
        self,
        img,
        kerns,
        out,
        img_val,
        kern_vals,
        border_mode,
        conv_mode,
        subsamples,
        dilations,
        algo,
    ):
        img_val = np.asarray(img_val, dtype=aesara.config.floatX)
        kern_vals = np.asarray(kern_vals, dtype=aesara.config.floatX)

        for dilation in dilations:
            for subsample in subsamples:
                out_vals = np.zeros(
                    dnn.GpuDnnConv.get_out_shape(
                        img_val.shape,
                        kern_vals.shape,
                        border_mode=border_mode,
                        subsample=subsample,
                        dilation=dilation,
                    ),
                    dtype=aesara.config.floatX,
                )
                desc = dnn.GpuDnnConvDesc(
                    border_mode=border_mode,
                    subsample=subsample,
                    dilation=dilation,
                    conv_mode=conv_mode,
                    precision=set_precision(aesara.config.floatX),
                )(kerns.shape)
                conv = dnn.GpuDnnConv(algo=algo)(img, kerns, out, desc)
                self._compile_and_check(
                    [img, kerns, out],
                    [conv],
                    [img_val, kern_vals, out_vals],
                    dnn.GpuDnnConv,
                )

    @pytest.mark.parametrize(
        "algo, border_mode, conv_mode",
        chain(
            product([SUPPORTED_DNN_CONV_ALGO_FWD[0]], border_modes, conv_modes),
            product(
                SUPPORTED_DNN_CONV_ALGO_FWD[1:], [border_modes[0]], [conv_modes[0]]
            ),
        ),
    )
    def test_conv(self, algo, border_mode, conv_mode):
        # Currently only CUDNN_CONVOLUTION_FWD_ALGO_IMPLICIT_GEMM (algo 'none')
        # supports dilation > 1. 'time*' and 'guess*' should fallback to it.
        dilations = [(1, 1)]
        if dnn.version() >= 6000 and (
            algo == "none" or "time_" in algo or "guess_" in algo
        ):
            dilations += [(2, 2)]

        self._test_conv(
            tensor4("img"),
            tensor4("kerns"),
            tensor4("out"),
            np.random.rand(7, 2, 12, 16),
            np.random.rand(8, 2, 4, 3),
            border_mode,
            conv_mode,
            [(1, 1), (2, 2)],
            dilations,
            algo,
        )

    @pytest.mark.parametrize(
        "border_mode, conv_mode", product(border_modes, conv_modes)
    )
    def test_conv3d_none(self, border_mode, conv_mode):
        dilations = [(1, 1, 1), (2, 2, 2)] if dnn.version() >= 6000 else [(1, 1, 1)]

        self._test_conv(
            tensor5("img"),
            tensor5("kerns"),
            tensor5("out"),
            np.random.rand(10, 2, 15, 16, 17),
            np.random.rand(8, 2, 4, 3, 1),
            border_mode,
            conv_mode,
            [(1, 1, 1), (2, 2, 2)],
            dilations,
            "none",
        )

    def _test_conv_gradw(
        self,
        img,
        topgrad,
        kerns,
        img_shape,
        kerns_shape,
        border_mode,
        conv_mode,
        subsamples,
        dilations,
    ):
        kerns_vals = np.zeros(kerns_shape, dtype=aesara.config.floatX)
        kerns_shape_shared = aesara.shared(np.asarray(kerns_shape))

        for dilation in dilations:
            for subsample in subsamples:
                topgrad_shape = get_conv_output_shape(
                    img_shape, kerns_shape, border_mode, subsample, dilation
                )

                img_val = np.asarray(
                    np.random.rand(*img_shape), dtype=aesara.config.floatX
                )
                topgrad_vals = np.asarray(
                    np.random.rand(*topgrad_shape), dtype=aesara.config.floatX
                )

                desc = dnn.GpuDnnConvDesc(
                    border_mode=border_mode,
                    subsample=subsample,
                    dilation=dilation,
                    conv_mode=conv_mode,
                    precision=set_precision(aesara.config.floatX),
                )(kerns_shape_shared)
                conv_grad_w = dnn.GpuDnnConvGradW()(
                    img,
                    topgrad,
                    kerns,
                    desc,
                )
                self._compile_and_check(
                    [img, topgrad, kerns],
                    [conv_grad_w],
                    [img_val, topgrad_vals, kerns_vals],
                    dnn.GpuDnnConvGradW,
                )

    @pytest.mark.parametrize(
        "border_mode, conv_mode", product(border_modes, conv_modes)
    )
    def test_conv_gradw(self, border_mode, conv_mode):
        dilations = [(1, 1), (2, 2)] if dnn.version() >= 6000 else [(1, 1)]

        self._test_conv_gradw(
            tensor4("img"),
            tensor4("topgrad"),
            tensor4("kerns"),
            (5, 2, 6, 13),
            (1, 2, 3, 7),
            border_mode,
            conv_mode,
            [(1, 1)],
            dilations,
        )

    def test_conv_gradi(self):
        img = tensor4("img")
        kerns = tensor4("kerns")
        out = tensor4("out")
        kern_vals = np.asarray(np.random.rand(13, 4, 5, 6), dtype=aesara.config.floatX)
        out_vals = np.asarray(np.random.rand(3, 13, 9, 11), dtype=aesara.config.floatX)

        dilations = [(1, 1), (2, 2)] if dnn.version() >= 6000 else [(1, 1)]
        for border_mode, subsample, dilation, conv_mode in product(
            ["valid", "full"], [(1, 1)], dilations, ["conv", "cross"]
        ):
            shape = get_conv_gradinputs_shape(
                kern_vals.shape, out_vals.shape, border_mode, subsample, dilation
            )
            img_vals = np.zeros(shape, dtype=aesara.config.floatX)
            desc = dnn.GpuDnnConvDesc(
                border_mode=border_mode,
                subsample=subsample,
                dilation=dilation,
                conv_mode=conv_mode,
                precision=set_precision(aesara.config.floatX),
            )(kerns.shape)
            conv_grad_i = dnn.GpuDnnConvGradI()(
                kerns,
                out,
                img,
                desc,
            )
            self._compile_and_check(
                [kerns, img, out],
                [conv_grad_i],
                [kern_vals, img_vals, out_vals],
                dnn.GpuDnnConvGradI,
            )

    def test_pool(self):
        img = tensor4("img")
        img_val = np.asarray(np.random.rand(2, 3, 4, 5), dtype=aesara.config.floatX)

        modes = get_dnn_pool_modes()

        for params in product(
            [(1, 1), (2, 2), (3, 3)], [(1, 1), (2, 2), (3, 3)], modes
        ):
            self._compile_and_check(
                [img],
                [dnn.GpuDnnPool(mode=params[2])(img, params[0], params[1], (0, 0))],
                [img_val],
                dnn.GpuDnnPool,
            )

    def test_pool_3d(self):
        img = tensor5("img")
        img_val = np.asarray(np.random.rand(2, 3, 4, 5, 6), dtype=aesara.config.floatX)

        modes = get_dnn_pool_modes()

        for params in product(
            [(1, 1, 1), (2, 2, 2), (3, 3, 3)], [(1, 1, 1), (2, 2, 2), (3, 3, 3)], modes
        ):
            self._compile_and_check(
                [img],
                [dnn.GpuDnnPool(mode=params[2])(img, params[0], params[1], (0, 0, 0))],
                [img_val],
                dnn.GpuDnnPool,
            )

    def test_pool_grad(self):
        img = tensor4("img")
        img_grad = tensor4("img_grad")
        out = tensor4("out")
        img_val = np.asarray(np.random.rand(2, 3, 4, 5), dtype=aesara.config.floatX)
        img_grad_val = np.asarray(
            np.random.rand(2, 3, 4, 5), dtype=aesara.config.floatX
        )
        out_val = np.asarray(np.random.rand(2, 3, 4, 5), dtype=aesara.config.floatX)

        for params in product(
            [(1, 1), (2, 2), (3, 3)],
            [(1, 1), (2, 2), (3, 3)],
            # modes without `average_exc_pad`
            [m for m in get_dnn_pool_modes() if m != "average_exc_pad"],
        ):
            pool_grad = dnn.GpuDnnPoolGrad(mode=params[2])(
                img, out, img_grad, params[0], params[1], (0, 0)
            )
            self._compile_and_check(
                [img, img_grad, out],
                [pool_grad],
                [img_val, img_grad_val, out_val],
                dnn.GpuDnnPoolGrad,
            )

    def test_pool_3d_grad(self):
        img = tensor5("img")
        img_grad = tensor5("img_grad")
        out = tensor5("out")
        img_val = np.asarray(np.random.rand(2, 3, 4, 5, 6), dtype=aesara.config.floatX)
        img_grad_val = np.asarray(
            np.random.rand(2, 3, 4, 5, 6), dtype=aesara.config.floatX
        )
        out_val = np.asarray(np.random.rand(2, 3, 4, 5, 6), dtype=aesara.config.floatX)

        for params in product(
            [(1, 1, 1), (2, 2, 2), (3, 3, 3)],
            [(1, 1, 1), (2, 2, 2), (3, 3, 3)],
            # modes without `average_exc_pad`
            [m for m in get_dnn_pool_modes() if m != "average_exc_pad"],
        ):
            pool_grad = dnn.GpuDnnPoolGrad(mode=params[2])(
                img, out, img_grad, params[0], params[1], (0, 0, 0)
            )
            self._compile_and_check(
                [img, img_grad, out],
                [pool_grad],
                [img_val, img_grad_val, out_val],
                dnn.GpuDnnPoolGrad,
            )


# this has been a problem in the past
def test_dnn_conv_border_mode():
    img = tensor4()
    kern = tensor4()

    dnn.dnn_conv(img, kern, border_mode=1)
    dnn.dnn_conv(img, kern, border_mode=(2, 3))
    dnn.dnn_conv(img, kern, border_mode="full")
    dnn.dnn_conv(img, kern, border_mode="valid")
    dnn.dnn_conv(img, kern, border_mode="half")


def test_dnn_conv_alpha_output_merge():

    img = tensor4()
    kern = tensor4()
    out = tensor4()

    b = 1
    c = 4
    f = 3
    ih = 5
    iw = 8
    kh = 2
    kw = 6
    img_val = np.random.random((b, c, ih, iw)).astype(aesara.config.floatX)
    kern_val = np.random.random((f, c, kh, kw)).astype(aesara.config.floatX)
    out_val = np.random.random((b, f, ih - kh + 1, iw - kw + 1)).astype(
        aesara.config.floatX
    )

    conv = dnn.dnn_conv(img, kern)
    gw = aesara.grad(conv.sum(), kern)
    gi = aesara.grad(conv.sum(), img)

    lr = np.asarray(0.05, dtype=aesara.config.floatX)

    fr = lr * (conv + out)
    wr = kern + lr * gw
    ir = img + lr * gi

    f1 = aesara.function([img, kern, out], [fr, wr, ir], mode=mode_with_gpu)
    assert isinstance(
        f1.maker.fgraph.outputs[0].owner.inputs[0].owner.op, dnn.GpuDnnConv
    )
    assert isinstance(
        f1.maker.fgraph.outputs[1].owner.inputs[0].owner.op, dnn.GpuDnnConvGradW
    )
    assert isinstance(
        f1.maker.fgraph.outputs[2].owner.inputs[0].owner.op, dnn.GpuDnnConvGradI
    )

    mode = mode_with_gpu
    mode = mode.excluding("local_dnn_conv_alpha_merge")
    mode = mode.excluding("local_dnn_convw_alpha_merge")
    mode = mode.excluding("local_dnn_convi_alpha_merge")
    mode = mode.excluding("local_dnn_conv_output_merge")
    mode = mode.excluding("local_dnn_convw_output_merge")
    mode = mode.excluding("local_dnn_convi_output_merge")

    f2 = aesara.function([img, kern, out], [fr, wr, ir], mode=mode)

    assert not isinstance(
        f2.maker.fgraph.outputs[0].owner.inputs[0].owner.op, dnn.GpuDnnConv
    )
    assert not isinstance(
        f2.maker.fgraph.outputs[1].owner.inputs[0].owner.op, dnn.GpuDnnConvGradW
    )
    assert not isinstance(
        f2.maker.fgraph.outputs[2].owner.inputs[0].owner.op, dnn.GpuDnnConvGradI
    )

    out_f1 = f1(img_val, kern_val, out_val)
    out_f2 = f2(img_val, kern_val, out_val)

    assert len(out_f1) == len(out_f2)

    for v1, v2 in zip(out_f1, out_f2):
        utt.assert_allclose(v1, v2)


def test_dnn_conv_grad():

    b = 1
    c = 4
    f = 3
    ih = 2
    iw = 8
    kh = 2
    kw = 2
    img_val = np.random.random((b, c, ih, iw)).astype(aesara.config.floatX)
    kern_val = np.random.random((f, c, kh, kw)).astype(aesara.config.floatX)
    out_val = np.random.random((b, f, ih - kw + 1, iw - kw + 1)).astype(
        aesara.config.floatX
    )

    def dconv(img, kern, out):
        desc = dnn.GpuDnnConvDesc(
            border_mode="valid",
            subsample=(1, 1),
            dilation=(1, 1),
            conv_mode="conv",
            precision=set_precision(aesara.config.floatX),
        )(kern.shape)
        return dnn.GpuDnnConv()(img, kern, out, desc, alpha=0.5, beta=0.75)

    def dconvi(img, kern, out):
        desc = dnn.GpuDnnConvDesc(
            border_mode="valid",
            subsample=(1, 1),
            dilation=(1, 1),
            conv_mode="conv",
            precision=set_precision(aesara.config.floatX),
        )(kern.shape)
        return dnn.GpuDnnConvGradI()(kern, out, img, desc, alpha=-1.0, beta=0.0)

    def dconvw(img, kern, out):
        desc = dnn.GpuDnnConvDesc(
            border_mode="valid",
            subsample=(1, 1),
            dilation=(1, 1),
            conv_mode="conv",
            precision=set_precision(aesara.config.floatX),
        )(kern.shape)
        return dnn.GpuDnnConvGradW()(img, out, kern, desc, alpha=0.75, beta=-1.0)

    utt.verify_grad(dconv, [img_val, kern_val, out_val], eps=1e-3, mode=mode_with_gpu)
    utt.verify_grad(dconvi, [img_val, kern_val, out_val], eps=1e-3, mode=mode_with_gpu)
    utt.verify_grad(dconvw, [img_val, kern_val, out_val], eps=1e-3, mode=mode_with_gpu)


def get_conv3d_test_cases():
    # Every element of test_shapes follows the format
    # [input_shape, filter_shape, subsample, dilation]
    test_shapes = [
        [(128, 3, 5, 5, 5), (64, 3, 1, 2, 4), (1, 1, 1), (1, 1, 1)],
        [(8, 4, 20, 12, 15), (5, 4, 6, 12, 4), (2, 2, 2), (1, 1, 1)],
        [(8, 1, 20, 12, 15), (5, 1, 6, 12, 4), (3, 3, 3), (1, 1, 1)],
        [(8, 1, 20, 12, 15), (5, 1, 6, 12, 4), (3, 2, 1), (1, 1, 1)],
        # Test with 1x1x1 filters
        [(8, 1, 10, 10, 10), (10, 1, 1, 1, 1), (1, 1, 1), (1, 1, 1)],
        # Test with dimensions larger than 1024 (thread block dim)
        [(1025, 1, 2, 3, 4), (5, 1, 1, 2, 3), (1, 1, 1), (1, 1, 1)],
        [(8, 1, 2, 3, 4), (1025, 1, 1, 2, 3), (1, 1, 1), (1, 1, 1)],
        [(8, 1025, 2, 3, 4), (5, 1025, 1, 1, 2), (1, 1, 1), (1, 1, 1)],
        [(8, 1, 1030, 3, 4), (5, 1, 1025, 1, 1), (1, 1, 1), (1, 1, 1)],
        [(8, 1, 2, 1030, 4), (5, 1, 2, 1025, 1), (1, 1, 1), (1, 1, 1)],
        [(8, 1, 2, 3, 1030), (5, 1, 1, 2, 1025), (1, 1, 1), (1, 1, 1)],
        # The equivalent of this caused a crash with conv2d
        [(1, 1, 1, 44800, 1), (6, 1, 1, 1, 1), (1, 1, 1), (1, 1, 1)],
    ]

    # With border mode 'full', test with kernel bigger than image in some/all
    # dimensions
    test_shapes_full = [
        [(6, 2, 2, 2, 2), (4, 2, 3, 1, 1), (1, 1, 1), (1, 1, 1)],
        [(6, 2, 2, 2, 2), (4, 2, 1, 3, 1), (1, 1, 1), (1, 1, 1)],
        [(6, 2, 2, 2, 2), (4, 2, 1, 1, 3), (1, 1, 1), (1, 1, 1)],
        [(6, 2, 2, 2, 2), (4, 2, 5, 5, 5), (1, 1, 1), (1, 1, 1)],
    ]

    if dnn.version() >= 6000:
        test_shapes.extend(
            [
                [(8, 1, 20, 12, 15), (5, 1, 6, 3, 4), (1, 1, 2), (3, 2, 1)],
                [(8, 1, 20, 12, 15), (5, 1, 6, 3, 4), (2, 2, 1), (1, 2, 3)],
            ]
        )
        test_shapes_full.append(
            [(6, 2, 2, 2, 2), (4, 2, 5, 5, 5), (1, 1, 1), (3, 2, 1)]
        )

    border_modes = ["valid", "full", "half", (1, 2, 3), (3, 2, 1), 1, 2]
    conv_modes = ["conv", "cross"]

    itt = chain(
        product(test_shapes, border_modes, conv_modes),
        product(test_shapes_full, ["full"], conv_modes),
    )

    return itt


def run_conv_small_batched_vs_multicall(inputs_shape, filters_shape, batch_sub):
    # Function to check issue #5985 (see tests below): https://github.com/Theano/Theano/issues/5985

    # Error occurs with algorithm `small` (CUDNN_CONVOLUTION_FWD_ALGO_IMPLICIT_PRECOMP_GEMM)
    algo = "small"

    batch_size = inputs_shape[0]

    inputs_val = np.random.random(inputs_shape).astype("float32")
    filters_val = np.random.random(filters_shape).astype("float32")
    # Scale down the input values to prevent very large absolute errors
    # due to float rounding
    inputs_val /= 10
    filters_val /= 10
    inputs = aesara.shared(inputs_val)
    filters = aesara.shared(filters_val)

    if len(inputs_shape) == 5:
        dnn_func = dnn.dnn_conv3d
    else:
        dnn_func = dnn.dnn_conv
    conv = dnn_func(None, img=inputs, kerns=filters, algo=algo)
    # Just compute first and last outputs, to reduce execution time.
    sub_conv_top = dnn_func(None, img=inputs[:batch_sub], kerns=filters, algo=algo)
    sub_conv_bottom = dnn_func(
        None, img=inputs[(batch_size - batch_sub) :], kerns=filters, algo=algo
    )
    f = aesara.function([], [conv, sub_conv_top, sub_conv_bottom], mode=mode_with_gpu)
    res_all, res_batch_top, res_batch_bottom = f()
    for i in range(batch_sub):
        # Check first outputs.
        utt.assert_allclose(res_batch_top[i], res_all[i])
        # Then check last outputs.
        p = batch_size - batch_sub + i
        # It seems there is a limit batch size of 65536  with algorithm `small`.
        checked_limit = 2 ** 16
        if p >= checked_limit:
            # It seems results are repeated in the entire conv.
            # It should not happen.
            if np.allclose(res_all[p % checked_limit], res_all[p]):
                print(f"\nconv[{int(p % checked_limit)}] == conv[{p}] == {res_all[p]}")
        utt.assert_allclose(res_batch_bottom[i], res_all[p])


def test_batched_conv_small():
    run_conv_small_batched_vs_multicall((65536, 2, 2, 2), (1, 2, 2, 2), 5)
    # Should fail with cuDNN < V6020, but there's currently a workaround in `dnn_fwd.c` for that case.
    run_conv_small_batched_vs_multicall((65537, 2, 2, 2), (1, 2, 2, 2), 5)


def test_batched_conv3d_small():
    run_conv_small_batched_vs_multicall((65536, 2, 2, 2, 2), (1, 2, 2, 2, 2), 5)
    # Should fail with cuDNN < V6020, but there's currently a workaround in `dnn_fwd.c` for that case.
    run_conv_small_batched_vs_multicall((65537, 2, 2, 2, 2), (1, 2, 2, 2, 2), 5)


def test_conv3d_fwd():
    def run_conv3d_fwd(
        inputs_shape, filters_shape, subsample, dilation, border_mode, conv_mode
    ):

        inputs_val = np.random.random(inputs_shape).astype(aesara.config.floatX)
        filters_val = np.random.random(filters_shape).astype(aesara.config.floatX)

        # Scale down the input values to prevent very large absolute errors
        # due to float rounding
        inputs_val /= 10
        filters_val /= 10

        inputs = aesara.shared(inputs_val)
        filters = aesara.shared(filters_val)

        # Compile an Aesara function for the cuDNN implementation
        conv = dnn.dnn_conv3d(
            None,
            img=inputs,
            kerns=filters,
            border_mode=border_mode,
            subsample=subsample,
            dilation=dilation,
            conv_mode=conv_mode,
        )
        f = aesara.function([], conv, mode=mode_with_gpu)

        # If conv_mode is 'conv' the reference implementation should use
        # filters flipped according to the width, height and time axis
        if conv_mode == "conv":
            flipped_filters = filters[:, :, ::-1, ::-1, ::-1]
        else:
            flipped_filters = filters

        # Compile an Aesara function for the reference implementation
        conv_ref = Corr3dMM(
            border_mode=border_mode,
            subsample=subsample,
            filter_dilation=dilation,
        )(ref_cast(inputs), flipped_filters)
        f_ref = aesara.function([], conv_ref, mode="FAST_RUN")

        # Compare the results of the two implementations
        res_ref = f_ref()
        res = f()
        # raise rtol to make the test pass with more seed.
        rtol = None
        # Raise tolerance for float16
        if aesara.config.floatX == "float16":
            rtol = 6e-2
        utt.assert_allclose(res_ref, res, rtol=rtol)

    test_cases = get_conv3d_test_cases()
    for (i_shape, f_shape, subsample, dilation), border_mode, conv_mode in test_cases:
        run_conv3d_fwd(
            i_shape,
            f_shape,
            subsample,
            dilation,
            border_mode,
            conv_mode,
        )


def test_conv3d_bwd():
    def run_conv3d_bwd(
        inputs_shape, filters_shape, subsample, dilation, border_mode, conv_mode
    ):

        inputs_val = np.random.random(inputs_shape).astype(aesara.config.floatX)
        filters_val = np.random.random(filters_shape).astype(aesara.config.floatX)

        inputs = aesara.shared(inputs_val)
        filters = aesara.shared(filters_val)

        # Compile an Aesara function for the cuDNN implementation
        conv = dnn.dnn_conv3d(
            None,
            img=inputs,
            kerns=filters,
            border_mode=border_mode,
            subsample=subsample,
            dilation=dilation,
            conv_mode=conv_mode,
        )

        grad_i, grad_w = aesara.grad(conv.sum(), [inputs, filters])

        f = aesara.function([], [grad_i, grad_w], mode=mode_with_gpu)

        # If conv_mode is 'conv' the reference implementation should use
        # filters flipped according to the width, height and time axis
        if conv_mode == "conv":
            flipped_filters = filters[:, :, ::-1, ::-1, ::-1]
        else:
            flipped_filters = filters

        # Compile an Aesara function for the reference implementation
        conv_ref = Corr3dMM(
            border_mode=border_mode,
            subsample=subsample,
            filter_dilation=dilation,
        )(ref_cast(inputs), flipped_filters)
        (grad_i_ref, grad_w_ref) = aesara.grad(conv_ref.sum(), [inputs, filters])
        f_ref = aesara.function([], [grad_i_ref, grad_w_ref], mode="FAST_RUN")

        # Compare the results of the two implementations
        res_ref = f_ref()
        res = f()
        # Needed for big size for some seed
        # raise rtol to make the test pass with more seed.
        rtol = None
        # Raise tolerance for float16
        if aesara.config.floatX == "float16":
            rtol = 5e-2
        elif max(inputs_shape) > 1024 or max(filters_shape) > 1024:
            rtol = 2e-5
        utt.assert_allclose(res_ref[0], res[0], rtol=rtol)
        utt.assert_allclose(res_ref[1], res[1], rtol=rtol)

    test_cases = get_conv3d_test_cases()
    for (i_shape, f_shape, subsample, dilation), border_mode, conv_mode in test_cases:
        run_conv3d_bwd(i_shape, f_shape, subsample, dilation, border_mode, conv_mode)


def test_version():
    assert isinstance(dnn.version(), int)


class TestSoftMax(test_nnet.TestSoftMax):
    gpu_op = dnn.GpuDnnSoftmax
    gpu_grad_op = dnn.GpuDnnSoftmaxGrad
    mode = mode_with_gpu

    def test_softmax_shape_0(self):
        dims = (2, 0, 4, 5)
        data = np.arange(np.product(dims), dtype=aesara.config.floatX).reshape(dims)

        # Verify the forward op
        x_gpu = tensor4("x_gpu")
        f_gpu = dnn.GpuDnnSoftmax("accurate", "channel")(x_gpu)
        f_gpu = aesara.function([x_gpu], f_gpu, mode=self.mode)
        assert f_gpu(data).shape == dims

        # Verify the gradient op
        dy_gpu = tensor4("dy_gpu")
        sm_gpu = tensor4("sm_gpu")
        f_grad_gpu = dnn.GpuDnnSoftmaxGrad("accurate", "channel")(dy_gpu, sm_gpu)
        f_grad_gpu = aesara.function([dy_gpu, sm_gpu], f_grad_gpu, mode=self.mode)
        assert f_grad_gpu(data, data).shape == dims

    def test_softmax_f16(self):
        x = matrix("x", "float16")
        x_gpu = tensor4("x_gpu", "float16")
        f_z = softmax_legacy
        f_gpu = dnn.GpuDnnSoftmax("accurate", "channel")

        def cmp(n, m, f, f_gpu):
            data = np.random.random((n, m)).astype("float16")
            gdata = np.asarray(data)[:, :, None, None]

            out = f(data)
            gout = np.asarray(f_gpu(gdata))[:, :, 0, 0]
            utt.assert_allclose(out, gout)

        self._test_softmax(x, x_gpu, f_z, f_gpu, cmp)

    def test_softmax_grad(self):
        def cmp(n, m, f, f_gpu):
            data = np.arange(n * m, dtype=aesara.config.floatX).reshape(n, m)
            gdata = np.asarray(data)[:, :, None, None]

            out = f(data)
            gout = np.asarray(f_gpu(gdata))[:, :, 0, 0]
            utt.assert_allclose(out, gout)

        x = matrix("x")
        x_gpu = tensor4("x_gpu")
        f_z = softmax_legacy
        f_gpu = dnn.GpuDnnSoftmax("accurate", "channel")

        # Verify the grad operation
        dims = (2, 3, 4, 5)
        gdata = np.arange(np.product(dims), dtype=aesara.config.floatX).reshape(dims)
        utt.verify_grad(f_gpu, [gdata], rng=np.random, mode=mode_with_gpu)

        # Verify that the CPU and GPU implementations return the same results
        # up to a tolerance.

        self._test_softmax(x, x_gpu, f_z, f_gpu, cmp)

        self._test_softmax(x, x, f_z, f_z, self._cmp)

        # Verify that the SoftmaxGrad -> Gpu[Dnn]SoftmaxGrad
        # optimization is applied when cudnn is required
        y = vector("y")
        f = aesara.function([y], aesara.grad(softmax(y).mean(), y), mode=mode_with_gpu)
        sorted_f = f.maker.fgraph.toposort()
        val = np.random.rand(5).astype(aesara.config.floatX)
        out_dnn = f(val)
        assert len([i for i in sorted_f if isinstance(i.op, self.gpu_grad_op)]) == 1
        assert len([i for i in sorted_f if isinstance(i.op, SoftmaxGrad)]) == 0

        # Verify that the SoftmaxGrad -> Gpu[Dnn]SoftmaxGrad
        # optimization is not applied when cudnn is excluded or not
        # available
        mode_wo_cudnn = mode_with_gpu.excluding("cudnn")
        y = vector("y")
        f = aesara.function([y], aesara.grad(softmax(y).mean(), y), mode=mode_wo_cudnn)
        sorted_f = f.maker.fgraph.toposort()
        out_cpu = f(val)
        utt.assert_allclose(out_dnn, out_cpu)
        assert len([i for i in sorted_f if isinstance(i.op, self.gpu_grad_op)]) == 0
        assert len([i for i in sorted_f if isinstance(i.op, SoftmaxGrad)]) == 1

        # Verify that the SoftmaxGrad -> GpuDnnSoftmaxGrad do not
        # crash with manual graph
        y = vector("y")
        o = SoftmaxGrad()(y, y * 2)
        f = aesara.function([y], o, mode=mode_with_gpu)
        sorted_f = f.maker.fgraph.toposort()
        assert len([i for i in sorted_f if isinstance(i.op, self.gpu_grad_op)]) == 1
        assert len([i for i in sorted_f if isinstance(i.op, SoftmaxGrad)]) == 0

    @pytest.mark.skipif(
        dnn.version(raises=False) < 3000, reason="Log-softmax is only in cudnn v3+"
    )
    def test_log_softmax(self):
        # This is a test for an optimization that depends on cuDNN v3 or
        # more recent. Don't test if the cuDNN version is too old.
        x = tensor4()
        softmax_out = dnn.GpuDnnSoftmax("accurate", "channel")(x)
        log_out = log(at.as_tensor_variable(softmax_out))

        f = aesara.function([x], log_out, mode=mode_with_gpu)

        # Ensure that the optimization has been applied
        dnn_softmax_nodes = [
            n for n in f.maker.fgraph.toposort() if isinstance(n.op, dnn.GpuDnnSoftmax)
        ]
        assert len(dnn_softmax_nodes) == 1
        assert dnn_softmax_nodes[0].op.algo == "log"

        # Ensure that the output of the function is valid
        input_shapes = [
            (3, 4, 5, 6),
            (1025, 2, 3, 4),
            (2, 1025, 3, 4),
            (2, 3, 1025, 4),
            (2, 3, 4, 1025),
            (66000, 2, 3, 4),
            (2, 66000, 3, 4),
            (2, 3, 66000, 4),
            (2, 3, 4, 66000),
        ]

        for inp_shape in input_shapes:
            input_val = np.random.normal(0, 1, inp_shape).astype(aesara.config.floatX)

            out = f(input_val)
            expected_out = np.log(
                np.exp(input_val) / np.exp(input_val).sum(1)[:, None, :, :]
            )

            utt.assert_allclose(out, expected_out)

    @pytest.mark.skipif(
        dnn.version(raises=False) < 3000, reason="Log-softmax is only in cudnn v3+"
    )
    def test_log_softmax2(self):
        # Test that the op LogSoftmax is correctly replaced by the op
        # DnnSoftmax with the 'log' mode.

        # Compile a reference function, on the CPU, to be used to validate the
        # results of the other function.
        x = matrix()
        f_ref = aesara.function([x], LogSoftmax()(x))

        # Build the first graph and ensure that the optimization is applied
        log_softmax_out = LogSoftmax()(x)
        f = aesara.function([x], log_softmax_out, mode=mode_with_gpu)

        dnn_softmax_nodes = [
            n for n in f.maker.fgraph.toposort() if isinstance(n.op, dnn.GpuDnnSoftmax)
        ]
        assert len(dnn_softmax_nodes) == 1
        assert dnn_softmax_nodes[0].op.algo == "log"

        # Compare the output of the function with the reference function
        inp = np.random.normal(0, 1, (5, 6)).astype(aesara.config.floatX)
        utt.assert_allclose(f(inp), f_ref(inp))

        # Build the first graph and ensure that the optimization is applied
        log_softmax_out = log(Softmax()(x))
        f = aesara.function([x], log_softmax_out, mode=mode_with_gpu)

        dnn_softmax_nodes = [
            n for n in f.maker.fgraph.toposort() if isinstance(n.op, dnn.GpuDnnSoftmax)
        ]
        assert len(dnn_softmax_nodes) == 1
        assert dnn_softmax_nodes[0].op.algo == "log"

        # Compare the output of the function with the reference function
        inp = np.random.normal(0, 1, (5, 6)).astype(aesara.config.floatX)
        utt.assert_allclose(f(inp), f_ref(inp))


def dnn_reduction(nd, idtype, acc_dtype, odtype):
    inp = TensorType(idtype, (False,) * nd)()
    res = inp.sum(acc_dtype=acc_dtype, dtype=odtype)
    f = aesara.function([inp], res, mode=mode_with_gpu)
    assert any(
        isinstance(n.op, dnn.GpuDnnReduction) for n in f.maker.fgraph.apply_nodes
    )


@pytest.mark.skipif(dnn.version(raises=False) < 6000, reason=dnn.dnn_available.msg)
def test_dnn_reduction_opt():
    for nd in range(1, 9):
        dnn_reduction(nd, "float32", "float32", "float32")

    for idtype, adtype, odtype in (
        ("float64", "float64", "float64"),
        ("float16", "float32", "float16"),
        ("float16", "float32", "float32"),
    ):
        dnn_reduction(2, idtype, adtype, odtype)


@pytest.mark.skipif(dnn.version(raises=False) < 6000, reason=dnn.dnn_available.msg)
def test_dnn_reduction_sum_squares():
    M = matrix()
    for axis in (None, 0, 1):
        out = (M ** 2).sum(axis=axis)
        f = aesara.function([M], out, mode=mode_with_gpu)
        assert any(
            isinstance(node.op, dnn.GpuDnnReduction) and node.op.red_op == "norm2"
            for node in f.maker.fgraph.apply_nodes
        )
        M_val = np.random.random((4, 5)).astype(aesara.config.floatX)
        utt.assert_allclose((M_val ** 2).sum(axis=axis), f(M_val))


@pytest.mark.skipif(dnn.version(raises=False) < 6000, reason=dnn.dnn_available.msg)
def test_dnn_reduction_sum_abs():
    M = matrix()
    for axis in (None, 0, 1):
        out = abs(M).sum(axis=axis)
        f = aesara.function([M], out, mode=mode_with_gpu)
        assert any(
            isinstance(node.op, dnn.GpuDnnReduction) and node.op.red_op == "norm1"
            for node in f.maker.fgraph.apply_nodes
        )
        M_val = np.random.random((4, 5)).astype(aesara.config.floatX)
        utt.assert_allclose(np.abs(M_val).sum(axis=axis), f(M_val))


@pytest.mark.skipif(dnn.version(raises=False) < 6000, reason=dnn.dnn_available.msg)
def test_dnn_reduction_absmax():
    M = matrix()
    for axis in (None, 0, 1):
        out = abs(M).max(axis=axis)
        f = aesara.function([M], out, mode=mode_with_gpu)
        assert any(
            isinstance(node.op, dnn.GpuDnnReduction) and node.op.red_op == "absmax"
            for node in f.maker.fgraph.apply_nodes
        )
        M_val = np.random.random((4, 5)).astype(aesara.config.floatX)
        utt.assert_allclose(np.max(np.abs(M_val), axis=axis), f(M_val))


@pytest.mark.skipif(dnn.version(raises=False) < 6000, reason=dnn.dnn_available.msg)
def test_dnn_reduction_axis_size_one():
    for dtype in ("float16", "float32", "float64"):
        for shape, axis in [
            [(1, 2, 3), 0],
            [(2, 1, 3), 1],
            [(2, 3, 1), 2],
            [(1, 5, 1), (0, 2)],
            [(4, 1, 6, 1), (1, 3)],
        ]:

            x = TensorType(dtype=dtype, broadcastable=[False] * len(shape))()
            sum = x.sum(axis=axis)
            sum_squares = (x ** 2).sum(axis=axis)
            sum_abs = abs(x).sum(axis=axis)
            absmax = abs(x).max(axis=axis)

            cpu_f = aesara.function(
                [x], [sum, sum_squares, sum_abs, absmax], mode=mode_without_gpu
            )
            f1 = aesara.function([x], sum, mode=mode_with_gpu)
            f2 = aesara.function([x], sum_squares, mode=mode_with_gpu)
            f3 = aesara.function([x], sum_abs, mode=mode_with_gpu)
            f4 = aesara.function([x], absmax, mode=mode_with_gpu)

            for fn, red_op in (
                (f1, "add"),
                (f2, "norm2"),
                (f3, "norm1"),
                (f4, "absmax"),
            ):
                assert any(
                    isinstance(node.op, dnn.GpuDnnReduction)
                    and node.op.red_op == red_op
                    for node in fn.maker.fgraph.apply_nodes
                )

            xval = np.random.uniform(-10, -1, size=shape).astype(dtype)
            if isinstance(axis, int):
                xval_reshaped = xval.reshape(shape[:axis] + shape[(axis + 1) :])
            else:
                xval_reshaped = xval.reshape(
                    [n for i, n in enumerate(shape) if i not in axis]
                )
            test_val = abs(xval_reshaped)

            val_sum, val_sum_squares, val_sum_abs, val_absmax = (
                f1(xval),
                f2(xval),
                f3(xval),
                f4(xval),
            )
            cpu_val_sum, cpu_val_sum_squares, cpu_val_sum_abs, cpu_val_absmax = cpu_f(
                xval
            )

            utt.assert_allclose(cpu_val_sum, val_sum)
            utt.assert_allclose(cpu_val_sum_squares, val_sum_squares)
            utt.assert_allclose(cpu_val_sum_abs, val_sum_abs)
            utt.assert_allclose(cpu_val_absmax, val_absmax)
            utt.assert_allclose(xval_reshaped, val_sum)
            utt.assert_allclose(test_val ** 2, val_sum_squares)
            utt.assert_allclose(test_val, val_sum_abs)
            utt.assert_allclose(test_val, val_absmax)


def dnn_reduction_strides(shp, shuffle, slice):
    utt.fetch_seed()
    inp = GpuArrayType("float32", (False,) * len(shp), context_name=test_ctx_name)()
    tmp = inp.dimshuffle(shuffle)[slice]
    res = tmp.sum(acc_dtype="float32", dtype="float32")
    f = aesara.function([inp], res, mode=mode_with_gpu)
    assert any(
        isinstance(n.op, dnn.GpuDnnReduction) for n in f.maker.fgraph.apply_nodes
    )
    data = np.random.random(shp).astype("float32")
    res = np.sum(data)
    gdata = pygpu.array(data, context=inp.type.context)
    gres = f(gdata)
    utt.assert_allclose(res, np.array(gres))


def test_dnn_reduction_strides():
    dnn_reduction_strides((2, 3, 2), (1, 0, 2), slice(None, None, None))
    dnn_reduction_strides((2, 3, 2), (0, 1, 2), slice(None, None, -1))


def test_dnn_reduction_error():
    nLoops = 5
    vec = np.arange(0, 10, dtype=np.float32)
    slow_output = np.zeros((5, 10))
    for i in range(nLoops):
        slow_output[i, :] = 2.0 * vec

    slow_output = np.sum(slow_output.transpose(), axis=1)

    vecT = vector(dtype=aesara.config.floatX)
    outputT = at.alloc(2.0 * vecT, 5, vecT.shape[0])
    outputSummedT = at_sum(at.transpose(outputT), axis=1)
    f3 = aesara.function(inputs=[vecT], outputs=outputSummedT)

    output = f3(vec)
    utt.assert_allclose(slow_output, output)


def dnn_maxargmax(nd, idtype, axis):
    inp = TensorType(idtype, (False,) * nd)()
    res = max_and_argmax(inp, axis=axis)
    f = aesara.function([inp], res, mode=mode_with_gpu)
    assert any(
        isinstance(n.op, dnn.GpuDnnReduction) for n in f.maker.fgraph.apply_nodes
    )


@pytest.mark.skipif(dnn.version(raises=False) < 6000, reason=dnn.dnn_available.msg)
def test_dnn_maxandargmax_opt():
    for nd in range(1, 9):
        dnn_maxargmax(nd, "float32", None)

    for idtype in ("float64", "float16"):
        dnn_maxargmax(2, idtype, None)

    dnn_maxargmax(3, "float32", (0, 1))
    dnn_maxargmax(3, "float32", (0, 2))
    dnn_maxargmax(3, "float32", (1, 2))
    dnn_maxargmax(3, "float32", (0, 1, 2))
    dnn_maxargmax(3, "float32", (0,))
    dnn_maxargmax(3, "float32", (1,))
    dnn_maxargmax(3, "float32", (2,))
    dnn_maxargmax(3, "float32", ())


def test_dnn_batchnorm_train():

    for mode in ("per-activation", "spatial"):
        for vartype in (
            tensor6,
            tensor5,
            tensor4,
            tensor3,
            matrix,
            vector,
        ):
            x, scale, bias, running_mean, running_var = (
                vartype(n)
                for n in ("x", "scale", "bias", "running_mean", "running_var")
            )
            ndim = x.ndim
            eps = 5e-3  # some non-standard value to test if it's used
            running_average_factor = 0.3

            # forward pass, direct interface
            (
                out_gpu,
                x_mean_gpu,
                x_invstd_gpu,
                out_running_mean_gpu,
                out_running_var_gpu,
            ) = dnn.dnn_batch_normalization_train(
                x,
                scale,
                bias,
                mode,
                eps,
                running_average_factor,
                running_mean,
                running_var,
            )
            # forward pass, abstract interface
            (
                out_abstract,
                x_mean_abstract,
                x_invstd_abstract,
                out_running_mean_abstract,
                out_running_var_abstract,
            ) = batchnorm.batch_normalization_train(
                x,
                scale,
                bias,
                mode,
                eps,
                running_average_factor,
                running_mean,
                running_var,
            )
            # reference forward pass
            if mode == "per-activation":
                axes = (0,)
            elif mode == "spatial":
                axes = (0,) + tuple(range(2, ndim))
            x_mean_ref = x.mean(axis=axes, keepdims=True)
            x_var_ref = x.var(axis=axes, keepdims=True)
            x_invstd_ref = reciprocal(sqrt(x_var_ref + eps))
            scale_ref = at.addbroadcast(scale, *axes)
            bias_ref = at.addbroadcast(bias, *axes)
            m = at.cast(prod(x.shape) / prod(scale.shape), aesara.config.floatX)
            out_ref = (x - x_mean_ref) * (scale_ref * x_invstd_ref) + bias_ref
            out_running_mean_ref = (
                running_mean * (1 - running_average_factor)
                + x_mean_ref * running_average_factor
            )
            out_running_var_ref = (
                running_var * (1 - running_average_factor)
                + (m / (m - 1)) * x_var_ref * running_average_factor
            )
            # backward pass
            dy = vartype("dy")
            grads_gpu = aesara.grad(
                None, wrt=[x, scale, bias], known_grads={out_gpu: dy}
            )
            grads_abstract = aesara.grad(
                None, wrt=[x, scale, bias], known_grads={out_abstract: dy}
            )
            # reference backward pass
            grads_ref = aesara.grad(
                None, wrt=[x, scale, bias], known_grads={out_ref: dy}
            )
            # compile
            f_gpu = aesara.function(
                [x, scale, bias, running_mean, running_var, dy],
                [
                    out_gpu,
                    x_mean_gpu,
                    x_invstd_gpu,
                    out_running_mean_gpu,
                    out_running_var_gpu,
                ]
                + grads_gpu,
                mode=mode_with_gpu,
            )
            f_abstract = aesara.function(
                [x, scale, bias, running_mean, running_var, dy],
                [
                    out_abstract,
                    x_mean_abstract,
                    x_invstd_abstract,
                    out_running_mean_abstract,
                    out_running_var_abstract,
                ]
                + grads_abstract,
                mode=mode_with_gpu,
            )
            f_ref = aesara.function(
                [x, scale, bias, running_mean, running_var, dy],
                [
                    out_ref,
                    x_mean_ref,
                    x_invstd_ref,
                    out_running_mean_ref,
                    out_running_var_ref,
                ]
                + grads_ref,
                mode=mode_without_gpu,
            )
            # check if the abstract Ops have been replaced
            assert any(
                [
                    isinstance(n.op, dnn.GpuDnnBatchNorm)
                    for n in f_abstract.maker.fgraph.toposort()
                ]
            )
            assert any(
                [
                    isinstance(n.op, dnn.GpuDnnBatchNormGrad)
                    for n in f_abstract.maker.fgraph.toposort()
                ]
            )
            assert not any(
                [
                    isinstance(
                        n.op,
                        (
                            batchnorm.AbstractBatchNormTrain,
                            batchnorm.AbstractBatchNormInference,
                            batchnorm.AbstractBatchNormTrainGrad,
                        ),
                    )
                    for n in f_abstract.maker.fgraph.toposort()
                ]
            )
            # run
            for data_shape in (
                (5, 10, 30, 4, 10, 5),
                (4, 3, 1, 1, 1, 1),
                (2, 3, 5, 5, 5, 5),
            ):
                data_shape = data_shape[:ndim]
                param_shape = tuple(
                    1 if d in axes else s for d, s in enumerate(data_shape)
                )
                X = 4 + 3 * np.random.randn(*data_shape).astype(aesara.config.floatX)
                Dy = -1 + 2 * np.random.randn(*data_shape).astype(aesara.config.floatX)
                Scale = np.random.randn(*param_shape).astype(aesara.config.floatX)
                Bias = np.random.randn(*param_shape).astype(aesara.config.floatX)
                Running_mean = np.random.randn(*param_shape).astype(
                    aesara.config.floatX
                )
                Running_var = np.random.randn(*param_shape).astype(aesara.config.floatX)
                outputs_gpu = f_gpu(X, Scale, Bias, Running_mean, Running_var, Dy)
                outputs_abstract = f_abstract(
                    X, Scale, Bias, Running_mean, Running_var, Dy
                )
                outputs_ref = f_ref(X, Scale, Bias, Running_mean, Running_var, Dy)
                # compare outputs
                utt.assert_allclose(outputs_gpu[0], outputs_ref[0])  # out
                utt.assert_allclose(outputs_gpu[1], outputs_ref[1])  # mean
                utt.assert_allclose(outputs_gpu[2], outputs_ref[2])  # invstd
                utt.assert_allclose(outputs_gpu[3], outputs_ref[3])  # running_mean
                utt.assert_allclose(
                    np.nan_to_num(outputs_gpu[4]), np.nan_to_num(outputs_ref[4])
                )  # running_var
                utt.assert_allclose(outputs_abstract[0], outputs_ref[0])  # out
                utt.assert_allclose(outputs_abstract[1], outputs_ref[1])  # mean
                utt.assert_allclose(outputs_abstract[2], outputs_ref[2])  # invstd
                utt.assert_allclose(outputs_abstract[3], outputs_ref[3])  # running_mean
                utt.assert_allclose(
                    np.nan_to_num(outputs_abstract[4]), np.nan_to_num(outputs_ref[4])
                )  # running_var
                # compare gradients
                utt.assert_allclose(outputs_gpu[5], outputs_ref[5], atol=2e-4)  # dx
                utt.assert_allclose(
                    outputs_gpu[6], outputs_ref[6], rtol=4e-4, atol=1e-4
                )  # dscale
                utt.assert_allclose(outputs_gpu[7], outputs_ref[7])  # dbias
                utt.assert_allclose(
                    outputs_abstract[5], outputs_ref[5], atol=2e-4
                )  # dx
                utt.assert_allclose(
                    outputs_abstract[6], outputs_ref[6], rtol=4e-4, atol=1e-4
                )  # dscale
                utt.assert_allclose(outputs_abstract[7], outputs_ref[7])  # dbias


def test_dnn_batchnorm_train_without_running_averages():
    # compile and run batch_normalization_train without running averages

    x, scale, bias, dy = (
        tensor4("x"),
        tensor4("scale"),
        tensor4("bias"),
        tensor4("dy"),
    )
    data_shape = (5, 10, 30, 25)
    param_shape = (1, 10, 30, 25)

    # forward pass
    out_gpu, x_mean_gpu, x_invstd_gpu = dnn.dnn_batch_normalization_train(
        x, scale, bias, "per-activation"
    )
    (
        out_abstract,
        x_mean_abstract,
        x_invstd_abstract,
    ) = batchnorm.batch_normalization_train(x, scale, bias, "per-activation")
    # backward pass
    grads_gpu = aesara.grad(None, wrt=[x, scale, bias], known_grads={out_gpu: dy})
    grads_abstract = aesara.grad(
        None, wrt=[x, scale, bias], known_grads={out_abstract: dy}
    )
    # compile
    f_gpu = aesara.function(
        [x, scale, bias, dy],
        [out_gpu, x_mean_gpu, x_invstd_gpu] + grads_gpu,
        mode=mode_with_gpu,
    )
    f_abstract = aesara.function(
        [x, scale, bias, dy],
        [out_abstract, x_mean_abstract, x_invstd_abstract] + grads_abstract,
        mode=mode_with_gpu,
    )
    # check if the abstract Ops have been replaced
    assert any(
        [
            isinstance(n.op, dnn.GpuDnnBatchNorm)
            for n in f_abstract.maker.fgraph.toposort()
        ]
    )
    assert any(
        [
            isinstance(n.op, dnn.GpuDnnBatchNormGrad)
            for n in f_abstract.maker.fgraph.toposort()
        ]
    )
    assert not any(
        [
            isinstance(
                n.op,
                (
                    batchnorm.AbstractBatchNormTrain,
                    batchnorm.AbstractBatchNormInference,
                    batchnorm.AbstractBatchNormTrainGrad,
                ),
            )
            for n in f_abstract.maker.fgraph.toposort()
        ]
    )
    # run
    X = 4 + 3 * np.random.randn(*data_shape).astype(aesara.config.floatX)
    Dy = -1 + 2 * np.random.randn(*data_shape).astype(aesara.config.floatX)
    Scale = np.random.randn(*param_shape).astype(aesara.config.floatX)
    Bias = np.random.randn(*param_shape).astype(aesara.config.floatX)
    f_gpu(X, Scale, Bias, Dy)
    f_abstract(X, Scale, Bias, Dy)


def test_without_dnn_batchnorm_train_without_running_averages():
    # compile and run batch_normalization_train without running averages
    # But disable cudnn and make sure it run on the GPU.

    x, scale, bias, dy = (
        tensor4("x"),
        tensor4("scale"),
        tensor4("bias"),
        tensor4("dy"),
    )
    data_shape = (5, 10, 30, 25)
    param_shape = (1, 10, 30, 25)

    # forward pass
    (
        out_abstract,
        x_mean_abstract,
        x_invstd_abstract,
    ) = batchnorm.batch_normalization_train(x, scale, bias, "per-activation")
    # backward pass
    grads_abstract = aesara.grad(
        None, wrt=[x, scale, bias], known_grads={out_abstract: dy}
    )
    # compile
    f_abstract = aesara.function(
        [x, scale, bias, dy],
        [out_abstract, x_mean_abstract, x_invstd_abstract] + grads_abstract,
        mode=mode_with_gpu.excluding("cudnn"),
    )
    # check if the abstract Ops have been replaced
    assert not any(
        [
            isinstance(n.op, dnn.GpuDnnBatchNorm)
            for n in f_abstract.maker.fgraph.toposort()
        ]
    )
    assert not any(
        [
            isinstance(n.op, dnn.GpuDnnBatchNormGrad)
            for n in f_abstract.maker.fgraph.toposort()
        ]
    )
    assert not any(
        [
            isinstance(
                n.op,
                (
                    batchnorm.AbstractBatchNormTrain,
                    batchnorm.AbstractBatchNormInference,
                    batchnorm.AbstractBatchNormTrainGrad,
                ),
            )
            for n in f_abstract.maker.fgraph.toposort()
        ]
    )
    assert any(
        [isinstance(n.op, dnn.GpuElemwise) for n in f_abstract.maker.fgraph.toposort()]
    )
    # run
    X = 4 + 3 * np.random.randn(*data_shape).astype(aesara.config.floatX)
    Dy = -1 + 2 * np.random.randn(*data_shape).astype(aesara.config.floatX)
    Scale = np.random.randn(*param_shape).astype(aesara.config.floatX)
    Bias = np.random.randn(*param_shape).astype(aesara.config.floatX)
    f_abstract(X, Scale, Bias, Dy)


@utt.assertFailure_fast
def test_dnn_batchnorm_train_inplace():
    # test inplace_running_mean and inplace_running_var

    x, scale, bias = tensor4("x"), tensor4("scale"), tensor4("bias")
    data_shape = (5, 10, 30, 25)
    param_shape = (1, 10, 30, 25)
    running_mean = gpuarray_shared_constructor(
        np.random.randn(*param_shape).astype(aesara.config.floatX),
        broadcastable=(True, False, False, False),
    )
    running_var = gpuarray_shared_constructor(
        np.random.randn(*param_shape).astype(aesara.config.floatX),
        broadcastable=(True, False, False, False),
    )

    # forward pass
    (
        out,
        x_mean,
        x_invstd,
        new_running_mean,
        new_running_var,
    ) = dnn.dnn_batch_normalization_train(
        x,
        scale,
        bias,
        "per-activation",
        epsilon=5e-3,
        running_average_factor=0.3,
        running_mean=running_mean,
        running_var=running_var,
    )
    # update running averages
    updates = OrderedDict()
    updates[running_mean] = new_running_mean
    updates[running_var] = new_running_var
    # compile
    f = aesara.function(
        [x, scale, bias], [out, x_mean, x_invstd], updates=updates, mode=mode_with_gpu
    )
    # check for the inplace settings
    nodes = [
        n for n in f.maker.fgraph.toposort() if isinstance(n.op, dnn.GpuDnnBatchNorm)
    ]
    assert len(nodes) == 1
    assert nodes[0].op.inplace_running_mean
    assert nodes[0].op.inplace_running_var
    assert nodes[0].op.inplace_output
    # run
    X = 4 + 3 * np.random.randn(*data_shape).astype(aesara.config.floatX)
    Scale = np.random.randn(*param_shape).astype(aesara.config.floatX)
    Bias = np.random.randn(*param_shape).astype(aesara.config.floatX)
    f(X, Scale, Bias)


def test_batchnorm_inference():

    for mode in ("per-activation", "spatial"):
        for vartype in (
            tensor6,
            tensor5,
            tensor4,
            tensor3,
            matrix,
            vector,
        ):
            x, scale, bias, mean, var = (
                vartype(n) for n in ("x", "scale", "bias", "mean", "var")
            )
            ndim = x.ndim
            eps = 5e-3  # some non-standard value to test if it's used

            # forward pass, direct interface
            out_gpu = dnn.dnn_batch_normalization_test(
                x, scale, bias, mean, var, mode, eps
            )
            # forward pass, abstract interface
            out_abstract = batchnorm.batch_normalization_test(
                x, scale, bias, mean, var, mode, eps
            )
            # reference forward pass
            if mode == "per-activation":
                axes = (0,)
            elif mode == "spatial":
                axes = (0,) + tuple(range(2, ndim))
            scale_ref, bias_ref, mean_ref, var_ref = (
                at.addbroadcast(t, *axes) for t in (scale, bias, mean, var)
            )
            out_ref = (x - mean_ref) * (scale_ref / sqrt(var_ref + eps)) + bias_ref
            # backward pass
            dy = vartype("dy")
            grads_gpu = aesara.grad(
                None, wrt=[x, scale, bias, mean, var], known_grads={out_gpu: dy}
            )
            grads_abstract = aesara.grad(
                None, wrt=[x, scale, bias, mean, var], known_grads={out_abstract: dy}
            )
            # reference backward pass
            grads_ref = aesara.grad(
                None, wrt=[x, scale, bias, mean, var], known_grads={out_ref: dy}
            )
            # compile
            f_gpu = aesara.function(
                [x, scale, bias, mean, var, dy],
                [out_gpu] + grads_gpu,
                mode=mode_with_gpu,
            )
            f_abstract = aesara.function(
                [x, scale, bias, mean, var, dy],
                [out_abstract] + grads_abstract,
                mode=mode_with_gpu,
            )
            f_ref = aesara.function(
                [x, scale, bias, mean, var, dy], [out_ref] + grads_ref
            )
            # check if the abstract Ops have been replaced
            assert any(
                [
                    isinstance(n.op, dnn.GpuDnnBatchNormInference)
                    for n in f_abstract.maker.fgraph.toposort()
                ]
            )
            assert not any(
                [
                    isinstance(
                        n.op,
                        (
                            batchnorm.AbstractBatchNormTrain,
                            batchnorm.AbstractBatchNormInference,
                            batchnorm.AbstractBatchNormTrainGrad,
                        ),
                    )
                    for n in f_abstract.maker.fgraph.toposort()
                ]
            )
            # run
            for data_shape in (
                (10, 2, 30, 4, 10, 5),
                (4, 3, 1, 1, 1, 1),
                (1, 1, 5, 5, 5, 5),
            ):
                data_shape = data_shape[:ndim]
                param_shape = tuple(
                    1 if d in axes else s for d, s in enumerate(data_shape)
                )
                X = 4 + 3 * np.random.randn(*data_shape).astype(aesara.config.floatX)
                Dy = -1 + 2 * np.random.randn(*data_shape).astype(aesara.config.floatX)
                Scale = np.random.randn(*param_shape).astype(aesara.config.floatX)
                Bias = np.random.randn(*param_shape).astype(aesara.config.floatX)
                Mean = np.random.randn(*param_shape).astype(aesara.config.floatX)
                Var = np.random.rand(*param_shape).astype(aesara.config.floatX)
                outputs_gpu = f_gpu(X, Scale, Bias, Mean, Var, Dy)
                outputs_abstract = f_abstract(X, Scale, Bias, Mean, Var, Dy)
                outputs_ref = f_ref(X, Scale, Bias, Mean, Var, Dy)
                # compare outputs
                utt.assert_allclose(outputs_gpu[0], outputs_ref[0])  # out
                utt.assert_allclose(outputs_abstract[0], outputs_ref[0])  # out
                # compare gradients
                utt.assert_allclose(outputs_gpu[1], outputs_ref[1], atol=4e-5)  # dx
                utt.assert_allclose(outputs_gpu[2], outputs_ref[2], atol=4e-5)  # dscale
                utt.assert_allclose(outputs_gpu[3], outputs_ref[3])  # dbias
                utt.assert_allclose(outputs_gpu[4], outputs_ref[4])  # dmean
                utt.assert_allclose(
                    outputs_gpu[5], outputs_ref[5], rtol=2e-3, atol=4e-5
                )  # dvar
                utt.assert_allclose(
                    outputs_abstract[1], outputs_ref[1], atol=4e-5
                )  # dx
                utt.assert_allclose(
                    outputs_abstract[2], outputs_ref[2], atol=4e-5
                )  # dscale
                utt.assert_allclose(outputs_abstract[3], outputs_ref[3])  # dbias
                utt.assert_allclose(outputs_abstract[4], outputs_ref[4])  # dmean
                utt.assert_allclose(
                    outputs_abstract[5], outputs_ref[5], rtol=2e-3, atol=4e-5
                )  # dvar


@utt.assertFailure_fast
def test_batchnorm_inference_inplace():
    # test inplace

    x, scale, bias, mean, var = (
        tensor4(n) for n in ("x", "scale", "bias", "mean", "var")
    )
    data_shape = (5, 10, 30, 25)
    param_shape = (1, 10, 30, 25)

    out = dnn.dnn_batch_normalization_test(x, scale, bias, mean, var)
    f = aesara.function([x, scale, bias, mean, var], [out], mode=mode_with_gpu)

    # check for the inplace settings
    nodes = [
        n
        for n in f.maker.fgraph.toposort()
        if isinstance(n.op, dnn.GpuDnnBatchNormInference)
    ]
    assert len(nodes) == 1
    assert nodes[0].op.inplace

    # run
    X = 4 + 3 * np.random.randn(*data_shape).astype(aesara.config.floatX)
    Scale = np.random.randn(*param_shape).astype(aesara.config.floatX)
    Bias = np.random.randn(*param_shape).astype(aesara.config.floatX)
    Mean = np.random.randn(*param_shape).astype(aesara.config.floatX)
    Var = np.random.rand(*param_shape).astype(aesara.config.floatX)
    f(X, Scale, Bias, Mean, Var)


def test_dnn_batchnorm_valid_and_invalid_axes():
    for vartype in (tensor5, tensor4, tensor3, matrix):
        x, scale, bias, mean, var, dy = (
            vartype(n) for n in ("x", "scale", "bias", "mean", "var", "dy")
        )
        ndim = x.ndim

        # supported: per-activation and spatial
        valid_axes_lists = ((0,), (0,) + tuple(range(2, ndim)))
        # not supported: an axes list without 0 and including 1
        invalid_axes_lists = (tuple(range(1, ndim)),)
        for axes in valid_axes_lists + invalid_axes_lists:
            # forward pass, abstract interface
            out_train, x_mean, x_invstd = batchnorm.batch_normalization_train(
                x, scale, bias, axes
            )
            out_test = batchnorm.batch_normalization_test(
                x, scale, bias, mean, var, axes
            )
            # backward pass
            dy = vartype("dy")
            grads_train = aesara.grad(
                None, wrt=[x, scale, bias], known_grads={out_train: dy}
            )
            grads_test = aesara.grad(
                None, wrt=[x, scale, bias, mean, var], known_grads={out_test: dy}
            )
            # compile
            f = aesara.function(
                [x, scale, bias, mean, var, dy],
                [out_train, x_mean, x_invstd, out_test] + grads_train + grads_test,
                mode=mode_with_gpu,
            )

            if axes in valid_axes_lists:
                # check if the abstract Ops have been replaced by the cuDNN Ops
                assert any(
                    [
                        isinstance(n.op, dnn.GpuDnnBatchNorm)
                        for n in f.maker.fgraph.toposort()
                    ]
                )
                assert any(
                    [
                        isinstance(n.op, dnn.GpuDnnBatchNormGrad)
                        for n in f.maker.fgraph.toposort()
                    ]
                )
                assert any(
                    [
                        isinstance(n.op, dnn.GpuDnnBatchNormInference)
                        for n in f.maker.fgraph.toposort()
                    ]
                )
                assert not any(
                    [
                        isinstance(
                            n.op,
                            (
                                batchnorm.AbstractBatchNormTrain,
                                batchnorm.AbstractBatchNormInference,
                                batchnorm.AbstractBatchNormTrainGrad,
                            ),
                        )
                        for n in f.maker.fgraph.toposort()
                    ]
                )
            else:
                # check if the abstract Ops have been replaced, but not by the cuDNN Ops
                assert not any(
                    [
                        isinstance(
                            n.op,
                            (
                                dnn.GpuDnnBatchNorm,
                                dnn.GpuDnnBatchNormGrad,
                                batchnorm.AbstractBatchNormTrain,
                                batchnorm.AbstractBatchNormInference,
                                batchnorm.AbstractBatchNormTrainGrad,
                            ),
                        )
                        for n in f.maker.fgraph.toposort()
                    ]
                )


def test_dnn_rnn_gru():

    # test params
    input_dim = 32
    hidden_dim = 16
    batch_size = 2
    depth = 3
    timesteps = 5

    # test code
    X = tensor3("X")
    Y = tensor3("Y")
    h0 = tensor3("h0")

    rnnb = dnn.RNNBlock(aesara.config.floatX, hidden_dim, depth, "gru")
    psize = rnnb.get_param_size([batch_size, input_dim])
    params_cudnn = gpuarray_shared_constructor(
        np.zeros((psize,), dtype=aesara.config.floatX)
    )

    model = Model()
    last_layer = WrapperLayer(X)
    last_dim = input_dim
    for i in range(depth):
        gru = GRU(last_dim, hidden_dim, last_layer, s0=h0[i, :, :])
        model.add_layer(gru)
        last_layer = gru
        last_dim = hidden_dim
        layer_params = gru.get_params()
        dnn_params = rnnb.split_params(params_cudnn, i, [batch_size, input_dim])
        for j, p in enumerate(dnn_params):
            p[:] = layer_params[j].get_value(borrow=True, return_internal_type=True)

    def funcs(out, params, hy=None):
        cost = 0
        if out:
            cost += mean((Y - out) ** 2)
        if hy:
            cost += mean(hy ** 2)
        grad = aesara.grad(cost, [X, h0] + params)
        grad_fn = aesara.function(
            [X, Y, h0], grad, mode=mode_with_gpu, on_unused_input="ignore"
        )
        return grad_fn

    ref_y = last_layer.output()

    # This will grab the hy from the scan implementation
    ref_hy = at.stack(
        [model.layers[0].Y[-1], model.layers[1].Y[-1], model.layers[2].Y[-1]]
    )

    y, hy = rnnb.apply(params_cudnn, X, h0)

    ref_fn = aesara.function([X, h0], ref_y, mode=mode_with_gpu)
    cudnn_fn = aesara.function([X, h0], y, mode=mode_with_gpu)

    # Test with grad connected to y
    ref_grad_fn = funcs(ref_y, model.get_params())
    cudnn_grad_fn = funcs(y, [params_cudnn])

    # Test with grad connected to both y and hy
    ref2_grad_fn = funcs(ref_y, model.get_params(), ref_hy)
    cudnn2_grad_fn = funcs(y, [params_cudnn], hy)

    # Test with grad connected to hy
    ref3_grad_fn = funcs(None, model.get_params(), ref_hy)
    cudnn3_grad_fn = funcs(None, [params_cudnn], hy)

    ref_grad_fns = [ref_grad_fn, ref2_grad_fn, ref3_grad_fn]
    cudnn_grad_fns = [cudnn_grad_fn, cudnn2_grad_fn, cudnn3_grad_fn]

    x_val = np.random.random((timesteps, batch_size, input_dim)).astype(
        aesara.config.floatX
    )
    y_val = np.random.random((timesteps, batch_size, hidden_dim)).astype(
        aesara.config.floatX
    )
    h0_val = np.random.random((depth, batch_size, hidden_dim)).astype(
        aesara.config.floatX
    )

    ref_out = ref_fn(x_val, h0_val)
    cudnn_out = cudnn_fn(x_val, h0_val)

    utt.assert_allclose(ref_out, cudnn_out)

    for ref_grad_fn, cudnn_grad_fn in zip(ref_grad_fns, cudnn_grad_fns):
        ref_grads = ref_grad_fn(x_val, y_val, h0_val)
        cudnn_grads = cudnn_grad_fn(x_val, y_val, h0_val)

        utt.assert_allclose(ref_grads[0], cudnn_grads[0])
        utt.assert_allclose(ref_grads[1], cudnn_grads[1])

        ref_grad_params = ref_grads[2:]
        cudnn_grad_params = gpuarray_shared_constructor(cudnn_grads[2])

        for i in range(depth):
            cudnn_grad_layer = rnnb.split_params(
                cudnn_grad_params, i, [batch_size, input_dim]
            )
            ref_grad_layer = ref_grad_params[
                i * len(cudnn_grad_layer) : (i + 1) * len(cudnn_grad_layer)
            ]
            for j, g in enumerate(cudnn_grad_layer):
                utt.assert_allclose(ref_grad_layer[j], g)


def test_dnn_rnn_gru_bidi():

    # test params
    input_dim = 32
    hidden_dim = 16
    batch_size = 2
    depth = 3
    timesteps = 5

    # test code
    X = tensor3("X")
    Y = tensor3("Y")
    h0 = tensor3("h0")

    rnnb = dnn.RNNBlock(
        aesara.config.floatX, hidden_dim, depth, "gru", direction_mode="bidirectional"
    )
    psize = rnnb.get_param_size([batch_size, input_dim])
    params_cudnn = gpuarray_shared_constructor(
        np.random.random((psize,)).astype(aesara.config.floatX)
    )

    def funcs(out, params, hy=None):
        cost = 0
        if out:
            cost += mean((Y - out) ** 2)
        if hy:
            cost += mean(hy ** 2)
        grad = aesara.grad(cost, [X, h0] + params)
        grad_fn = aesara.function(
            [X, Y, h0], grad, mode=mode_with_gpu, on_unused_input="ignore"
        )
        return grad_fn

    y, hy = rnnb.apply(params_cudnn, X, h0)

    cudnn_fn = aesara.function([X, h0], y, mode=mode_with_gpu)

    cudnn_grad_fn = funcs(y, [params_cudnn])
    cudnn2_grad_fn = funcs(y, [params_cudnn], hy)
    cudnn3_grad_fn = funcs(None, [params_cudnn], hy)

    cudnn_grad_fns = [cudnn_grad_fn, cudnn2_grad_fn, cudnn3_grad_fn]

    x_val = np.random.random((timesteps, batch_size, input_dim)).astype(
        aesara.config.floatX
    )
    y_val = np.random.random((timesteps, batch_size, 2 * hidden_dim)).astype(
        aesara.config.floatX
    )
    h0_val = np.random.random((2 * depth, batch_size, hidden_dim)).astype(
        aesara.config.floatX
    )

    cudnn_fn(x_val, h0_val)

    for cudnn_grad_fn in cudnn_grad_fns:
        cudnn_grad_fn(x_val, y_val, h0_val)


def test_dnn_rnn_lstm():

    # test params
    input_dim = 32
    hidden_dim = 16
    batch_size = 2
    depth = 3
    timesteps = 5

    # test code
    X = tensor3("X")
    Y = tensor3("Y")
    h0 = tensor3("h0")
    c0 = tensor3("c0")

    rnnb = dnn.RNNBlock(aesara.config.floatX, hidden_dim, depth, "lstm")
    psize = rnnb.get_param_size([batch_size, input_dim])
    params_cudnn = gpuarray_shared_constructor(
        np.zeros((psize,), dtype=aesara.config.floatX)
    )

    model = Model()
    last_layer = WrapperLayer(X)
    last_dim = input_dim
    for i in range(depth):
        lstm = LSTM(last_dim, hidden_dim, last_layer, s0=h0[i, :, :], c0=c0[i, :, :])
        model.add_layer(lstm)
        last_layer = lstm
        last_dim = hidden_dim
        layer_params = lstm.get_params()
        dnn_params = rnnb.split_params(params_cudnn, i, [batch_size, input_dim])
        for j, p in enumerate(dnn_params):
            p[:] = layer_params[j].get_value(borrow=True, return_internal_type=True)

    def funcs(out, params):
        fn = aesara.function([X, h0, c0], out, mode=mode_with_gpu)
        cost = mean((Y - out) ** 2)
        grad = aesara.grad(cost, [X, h0, c0] + params)
        grad_fn = aesara.function([X, Y, h0, c0], grad, mode=mode_with_gpu)
        return fn, grad_fn

    ref_fn, ref_grad_fn = funcs(last_layer.output(), model.get_params())
    cudnn_fn, cudnn_grad_fn = funcs(
        rnnb.apply(params_cudnn, X, h0, c0)[0], [params_cudnn]
    )

    x_val = np.random.random((timesteps, batch_size, input_dim)).astype(
        aesara.config.floatX
    )
    y_val = np.random.random((timesteps, batch_size, hidden_dim)).astype(
        aesara.config.floatX
    )
    h0_val = np.random.random((depth, batch_size, hidden_dim)).astype(
        aesara.config.floatX
    )
    c0_val = np.random.random((depth, batch_size, hidden_dim)).astype(
        aesara.config.floatX
    )

    ref_out = ref_fn(x_val, h0_val, c0_val)
    cudnn_out = cudnn_fn(x_val, h0_val, c0_val)

    utt.assert_allclose(ref_out, cudnn_out)

    ref_grads = ref_grad_fn(x_val, y_val, h0_val, c0_val)
    cudnn_grads = cudnn_grad_fn(x_val, y_val, h0_val, c0_val)

    utt.assert_allclose(ref_grads[0], cudnn_grads[0])
    utt.assert_allclose(ref_grads[1], cudnn_grads[1])
    utt.assert_allclose(ref_grads[2], cudnn_grads[2])

    ref_grads_params = ref_grads[3:]
    cudnn_grads_params = gpuarray_shared_constructor(cudnn_grads[3])

    for i in range(depth):
        cudnn_grads_layer = rnnb.split_params(
            cudnn_grads_params, i, [batch_size, input_dim]
        )
        ref_grads_layer = ref_grads_params[
            i * len(cudnn_grads_layer) : (i + 1) * len(cudnn_grads_layer)
        ]
        for j, g in enumerate(cudnn_grads_layer):
            utt.assert_allclose(ref_grads_layer[j], g)


def test_dnn_rnn_lstm_grad_c():

    # test params
    input_dim = 32
    hidden_dim = 16
    batch_size = 2
    depth = 3
    timesteps = 5

    # test code
    X = tensor3("X")
    CY = tensor3("CY")
    h0 = tensor3("h0")
    c0 = tensor3("c0")

    rnnb = dnn.RNNBlock(aesara.config.floatX, hidden_dim, depth, "lstm")
    psize = rnnb.get_param_size([batch_size, input_dim])
    params_cudnn = gpuarray_shared_constructor(
        np.zeros((psize,), dtype=aesara.config.floatX)
    )

    model = Model()
    last_layer = WrapperLayer(X)
    last_dim = input_dim
    for i in range(depth):
        lstm = LSTM(last_dim, hidden_dim, last_layer, s0=h0[i, :, :], c0=c0[i, :, :])
        model.add_layer(lstm)
        last_layer = lstm
        last_dim = hidden_dim
        layer_params = lstm.get_params()
        dnn_params = rnnb.split_params(params_cudnn, i, [batch_size, input_dim])
        for j, p in enumerate(dnn_params):
            p[:] = layer_params[j].get_value(borrow=True, return_internal_type=True)

    def funcs(out, params):
        cost = mean((CY - out) ** 2)
        grad = aesara.grad(cost, [X, h0, c0] + params)
        grad_fn = aesara.function([X, CY, h0, c0], grad, mode=mode_with_gpu)
        return grad_fn

    _, _, cy = rnnb.apply(params_cudnn, X, h0, c0)
    ref_cy = at.stack(
        [model.layers[0].C[-1], model.layers[1].C[-1], model.layers[2].C[-1]]
    )

    ref_grad_fn = funcs(ref_cy, model.get_params())
    cudnn_grad_fn = funcs(cy, [params_cudnn])

    x_val = np.random.random((timesteps, batch_size, input_dim)).astype(
        aesara.config.floatX
    )
    cy_val = np.random.random((depth, batch_size, hidden_dim)).astype(
        aesara.config.floatX
    )
    h0_val = np.random.random((depth, batch_size, hidden_dim)).astype(
        aesara.config.floatX
    )
    c0_val = np.random.random((depth, batch_size, hidden_dim)).astype(
        aesara.config.floatX
    )

    ref_grads = ref_grad_fn(x_val, cy_val, h0_val, c0_val)
    cudnn_grads = cudnn_grad_fn(x_val, cy_val, h0_val, c0_val)

    utt.assert_allclose(ref_grads[0], cudnn_grads[0])
    utt.assert_allclose(ref_grads[1], cudnn_grads[1])
    utt.assert_allclose(ref_grads[2], cudnn_grads[2])

    ref_grads_params = ref_grads[3:]
    cudnn_grads_params = gpuarray_shared_constructor(cudnn_grads[3])

    for i in range(depth):
        cudnn_grads_layer = rnnb.split_params(
            cudnn_grads_params, i, [batch_size, input_dim]
        )
        ref_grads_layer = ref_grads_params[
            i * len(cudnn_grads_layer) : (i + 1) * len(cudnn_grads_layer)
        ]
        for j, g in enumerate(cudnn_grads_layer):
            utt.assert_allclose(ref_grads_layer[j], g)


class Cudnn_grouped_conv(TestGroupedConvNoOptim):
    mode = mode_with_gpu.excluding("conv_gemm")
    conv_op = dnn.GpuDnnConv
    conv_gradw_op = dnn.GpuDnnConvGradW
    conv_gradi_op = dnn.GpuDnnConvGradI

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


class Cudnn_grouped_conv3d(TestGroupedConv3dNoOptim):
    mode = mode_with_gpu.excluding("conv_gemm")
    conv_op = dnn.GpuDnnConv
    conv_gradw_op = dnn.GpuDnnConvGradW
    conv_gradi_op = dnn.GpuDnnConvGradI

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


def test_dnn_spatialtf():

    """
    Spatial Transformer implementation using Aesara from Lasagne
    Original author: skaae (https://github.com/skaae)
    """

    def spatialtf_cpu(inp, theta, scale_height, scale_width, border_mode="nearest"):
        num_batch, num_channels, height, width = inp.shape
        theta = reshape(theta, (-1, 2, 3))

        # grid of (x_t, y_t, 1), eq (1) in ref [1]
        out_height = at.cast(ceil(height * scale_height), "int64")
        out_width = at.cast(ceil(width * scale_width), "int64")
        grid = _meshgrid(out_height, out_width)
        # transform a x (x_t, y_t, 1)^t -> (x_s, y_s)
        t_g = dot(theta, grid)
        x_s = t_g[:, 0]
        y_s = t_g[:, 1]
        x_s_flat = x_s.flatten()
        y_s_flat = y_s.flatten()

        # dimshuffle input to  (bs, height, width, channels)
        input_dim = inp.dimshuffle(0, 2, 3, 1)
        input_transformed = _interpolate(
            input_dim, x_s_flat, y_s_flat, out_height, out_width, border_mode
        )

        output = reshape(
            input_transformed, (num_batch, out_height, out_width, num_channels)
        )
        output = output.dimshuffle(0, 3, 1, 2)  # dimshuffle to conv format
        return output

    def _interpolate(im, x, y, out_height, out_width, border_mode):
        # *_f are floats
        num_batch, height, width, channels = im.shape
        height_f = at.cast(height, aesara.config.floatX)
        width_f = at.cast(width, aesara.config.floatX)

        # scale coordinates from [-1, 1] to [0, dimension - 1], where dimension
        # can be the width or height
        x = (x + 1) / 2 * (width_f - 1)
        y = (y + 1) / 2 * (height_f - 1)

        # obtain indices of the 2x2 pixel neighborhood surrounding the coordinates;
        # we need those in floatX for interpolation and in int64 for indexing.
        x0_f = floor(x)
        y0_f = floor(y)
        x1_f = x0_f + 1
        y1_f = y0_f + 1

        # for indexing, we need to take care of the border mode for outside pixels.
        if border_mode == "nearest":
            x0 = clip(x0_f, 0, width_f - 1)
            x1 = clip(x1_f, 0, width_f - 1)
            y0 = clip(y0_f, 0, height_f - 1)
            y1 = clip(y1_f, 0, height_f - 1)
        elif border_mode == "mirror":
            w = 2 * (width_f - 1)
            x0 = minimum(x0_f % w, -x0_f % w)
            x1 = minimum(x1_f % w, -x1_f % w)
            h = 2 * (height_f - 1)
            y0 = minimum(y0_f % h, -y0_f % h)
            y1 = minimum(y1_f % h, -y1_f % h)
        elif border_mode == "wrap":
            x0 = mod(x0_f, width_f)
            x1 = mod(x1_f, width_f)
            y0 = mod(y0_f, height_f)
            y1 = mod(y1_f, height_f)
        else:
            raise ValueError(
                "border_mode must be one of " "'nearest', 'mirror', 'wrap'"
            )
        x0, x1, y0, y1 = (at.cast(v, "int64") for v in (x0, x1, y0, y1))

        # The input is [num_batch, height, width, channels]. We do the lookup in
        # the flattened input, i.e [num_batch*height*width, channels]. We need
        # to offset all indices to match the flat version
        dim2 = width
        dim1 = width * height
        base = at.repeat(
            at.arange(num_batch, dtype="int64") * dim1, out_height * out_width
        )
        base_y0 = base + y0 * dim2
        base_y1 = base + y1 * dim2
        idx_a = base_y0 + x0
        idx_b = base_y1 + x0
        idx_c = base_y0 + x1
        idx_d = base_y1 + x1

        # use indices to lookup pixels for all samples
        im_flat = im.reshape((-1, channels))
        Ia = im_flat[idx_a]
        Ib = im_flat[idx_b]
        Ic = im_flat[idx_c]
        Id = im_flat[idx_d]

        # calculate interpolated values
        wa = ((x1_f - x) * (y1_f - y)).dimshuffle(0, "x")
        wb = ((x1_f - x) * (y - y0_f)).dimshuffle(0, "x")
        wc = ((x - x0_f) * (y1_f - y)).dimshuffle(0, "x")
        wd = ((x - x0_f) * (y - y0_f)).dimshuffle(0, "x")
        output = at_sum([wa * Ia, wb * Ib, wc * Ic, wd * Id], axis=0)
        return output

    def _linspace(start, stop, num):
        # aesara linspace. Behaves similar to np.linspace
        start = at.cast(start, aesara.config.floatX)
        stop = at.cast(stop, aesara.config.floatX)
        num = at.cast(num, aesara.config.floatX)
        step = (stop - start) / (num - 1)
        return at.arange(num, dtype=aesara.config.floatX) * step + start

    def _meshgrid(height, width):
        # This function is the grid generator from eq. (1) in reference [1].
        # It is equivalent to the following numpy code:
        #  x_t, y_t = np.meshgrid(np.linspace(-1, 1, width),
        #                         np.linspace(-1, 1, height))
        #  ones = np.ones(np.prod(x_t.shape))
        #  grid = np.vstack([x_t.flatten(), y_t.flatten(), ones])
        # It is implemented in Aesara instead to support symbolic grid sizes.
        # Note: If the image size is known at layer construction time, we could
        # compute the meshgrid offline in numpy instead of doing it dynamically
        # in Aesara. However, it hardly affected performance when we tried.
        x_t = dot(at.ones((height, 1)), _linspace(-1.0, 1.0, width).dimshuffle("x", 0))
        y_t = dot(_linspace(-1.0, 1.0, height).dimshuffle(0, "x"), at.ones((1, width)))

        x_t_flat = x_t.reshape((1, -1))
        y_t_flat = y_t.reshape((1, -1))
        ones = at.ones_like(x_t_flat)
        grid = at.concatenate([x_t_flat, y_t_flat, ones], axis=0)
        return grid

    img_dims = (5, 3, 16, 16)
    img = np.random.random(size=img_dims).astype(aesara.config.floatX)

    scale_height = 0.25
    scale_width = 0.75

    # Transformation matrix
    transform = [[-1, 0, 0], [0, -1, 0]]
    theta = np.asarray(img_dims[0] * [transform], dtype=aesara.config.floatX)

    # Create symbolic variables for inputs and transformations
    t_img = tensor4("img")
    t_theta = tensor3("theta")

    st_dnn = dnn.dnn_spatialtf(
        t_img, t_theta, scale_height=scale_height, scale_width=scale_width
    )
    st_dnn_func = aesara.function([t_img, t_theta], st_dnn, mode=mode_with_gpu)
    # Check if function graph contains the spatial transformer's grid and sampler Ops
    apply_nodes = st_dnn_func.maker.fgraph.apply_nodes
    assert any(isinstance(node.op, dnn.GpuDnnTransformerGrid) for node in apply_nodes)
    assert any(
        isinstance(node.op, dnn.GpuDnnTransformerSampler) for node in apply_nodes
    )

    img_out_gpu = st_dnn_func(img, theta)
    img_out_gpu = np.asarray(img_out_gpu)

    # Setup CPU Op
    st_cpu = spatialtf_cpu(t_img, t_theta, scale_height, scale_width, "nearest")
    st_cpu_func = aesara.function([t_img, t_theta], st_cpu, mode=mode_without_gpu)
    img_out_cpu = st_cpu_func(img, theta)

    atol, rtol = None, None
    if aesara.config.floatX == "float16":
        # Raise relative error tolerance when using float16
        rtol = 5e-2
    utt.assert_allclose(img_out_cpu, img_out_gpu, atol=atol, rtol=rtol)


def test_dnn_spatialtf_invalid_shapes():
    inputs = tensor4("inputs")
    theta = tensor3("theta")

    st_dnn = dnn.dnn_spatialtf(inputs, theta)
    st_dnn_func = aesara.function([inputs, theta], st_dnn, mode=mode_with_gpu)

    inputs_val = np.ones((3, 5, 7, 7), dtype=aesara.config.floatX)

    def try_theta_shp(theta_shp):
        theta_val = np.ones(theta_shp, dtype=aesara.config.floatX)
        return st_dnn_func(inputs_val, theta_val)

    # the theta shape for this input should be (3, 2, 3)
    try_theta_shp((3, 2, 3))

    # incorrect parameter dimensions
    with pytest.raises(RuntimeError):
        try_theta_shp((3, 1, 3))
    with pytest.raises(RuntimeError):
        try_theta_shp((3, 2, 1))

    # number of rows does not match the number of input rows
    with pytest.raises(RuntimeError):
        try_theta_shp((1, 2, 3))
    with pytest.raises(RuntimeError):
        try_theta_shp((4, 2, 3))


def test_dnn_spatialtf_grad():

    inputs = tensor4("inputs")
    theta = tensor3("theta")

    out = dnn.dnn_spatialtf(inputs, theta, scale_height=0.25, scale_width=0.75)
    out_mean = mean(out)
    mean_gi = aesara.grad(out_mean, [inputs])
    mean_gt = aesara.grad(out_mean, [theta])

    f_gi = aesara.function([inputs, theta], mean_gi, mode=mode_with_gpu)
    assert any(
        [
            isinstance(node.op, dnn.GpuDnnTransformerGradI)
            for node in f_gi.maker.fgraph.apply_nodes
        ]
    )

    f_gt = aesara.function([inputs, theta], mean_gt, mode=mode_with_gpu)
    assert any(
        [
            isinstance(node.op, dnn.GpuDnnTransformerGradT)
            for node in f_gt.maker.fgraph.apply_nodes
        ]
    )

    input_dims = (5, 3, 16, 16)
    inputs_val = np.random.random(size=input_dims).astype(aesara.config.floatX)

    # Tensor with transformations
    theta_val = np.random.random((input_dims[0], 2, 3)).astype(aesara.config.floatX)
    # Using smaller values for theta, increases the precision of gradients
    # when using lower precision. Tests might fail for lower precision data
    # types if the values of theta or the inputs are very high.
    theta /= 100

    # Check that the gradients are computed
    f_gi(inputs_val, theta_val)
    f_gt(inputs_val, theta_val)

    def grad_functor(inputs, theta):
        out = dnn.dnn_spatialtf(inputs, theta)
        return out

    atol, rtol = None, None
    if aesara.config.floatX == "float32":
        rtol = 5e-2
    elif aesara.config.floatX == "float16":
        rtol = 1e-0

    utt.verify_grad(
        grad_functor,
        [inputs_val, theta_val],
        mode=mode_with_gpu,
        abs_tol=atol,
        rel_tol=rtol,
    )


class TestDnnConv2DRuntimeAlgorithms:
    ndim = 2
    cpu_conv_class = CorrMM
    runtime_shapes = [
        (3, [(2, 3, 10, 9), (5, 3, 7, 7)]),
        (1, [(1, 1, 100, 200), (1, 1, 50, 200)]),
        (1, [(4, 2, 20, 20), (2, 2, 20, 19)]),
        (3, [(2, 3, 10, 9), (5, 3, 7, 7)]),  # cache should be used
        (1, [(2, 2, 50, 50), (5, 2, 25, 31)]),
        (1, [(1, 1, 100, 200), (1, 1, 50, 200)]),  # cache should be used
        (1, [(4, 2, 20, 20), (2, 2, 20, 19)]),  # cache should be used
        (1, [(1, 2, 3, 4), (6, 2, 2, 1)]),
    ]

    def __init__(self):

        self.runtime_algorithms = (
            "time_once",
            "guess_once",
            "time_on_shape_change",
            "guess_on_shape_change",
        )

    def test_fwd_runtime_algorithms(self):
        dtype = "float32"
        unit_shape = (1,) * self.ndim
        _broadcastable = [False] * (2 + self.ndim)

        def run_fwd_runtime_algorithm(algo):
            inputs = TensorType(dtype, _broadcastable)()
            filters = TensorType(dtype, _broadcastable)()
            # Scale down the input values to prevent very large absolute errors
            # due to float rounding
            lower_inputs = inputs / 10
            lower_filters = filters / 10
            conv = dnn.dnn_conv(
                img=lower_inputs,
                kerns=lower_filters,
                algo=algo,
                precision=dtype,
                subsample=unit_shape,
                dilation=unit_shape,
            )
            f = aesara.function([inputs, filters], conv, mode=mode_with_gpu)
            if self.ndim == 3:
                flipped_filters = lower_filters[:, :, ::-1, ::-1, ::-1]
            else:
                flipped_filters = lower_filters[:, :, ::-1, ::-1]
            conv_ref = self.cpu_conv_class(subsample=unit_shape)(
                ref_cast(lower_inputs), flipped_filters
            )
            f_ref = aesara.function([inputs, filters], conv_ref, mode="FAST_RUN")
            runtime_shapes = self.runtime_shapes
            if algo in ("time_once", "guess_once"):
                runtime_shapes = [list(runtime_shapes[0])]
                runtime_shapes[0][0] = 5
            for ntimes, (inputs_shape, filters_shape) in runtime_shapes:
                for i in range(ntimes):
                    inputs_val = np.random.random(inputs_shape).astype(dtype)
                    filters_val = np.random.random(filters_shape).astype(dtype)
                    gpu_res = f(inputs_val, filters_val)
                    cpu_res = f_ref(inputs_val, filters_val)
                    # rtol is needed for the test to be more robust to
                    # different seed.
                    utt.assert_allclose(cpu_res, np.asarray(gpu_res), rtol=2e-5)

        for algo in self.runtime_algorithms:
            run_fwd_runtime_algorithm(algo)

    def test_gradinput_runtime_algorithms(self):
        dtype = "float32"
        unit_shape = (1,) * self.ndim
        _broadcastable = [False] * (2 + self.ndim)

        def run_gradinput_runtime_algorithm(algo):
            aesara.config.dnn__conv__algo_bwd_data = algo
            inputs = TensorType(dtype, _broadcastable)()
            filters = TensorType(dtype, _broadcastable)()
            conv = dnn.dnn_conv(
                img=inputs,
                kerns=filters,
                algo=algo,
                precision=dtype,
                subsample=unit_shape,
                dilation=unit_shape,
            )
            (grad_i,) = aesara.grad(conv.sum(), [inputs])
            f = aesara.function([inputs, filters], grad_i, mode=mode_with_gpu)
            assert 1 == len(
                [
                    node
                    for node in f.maker.fgraph.apply_nodes
                    if isinstance(node.op, dnn.GpuDnnConvGradI)
                ]
            )
            assert not any(
                isinstance(node.op, dnn.GpuDnnConv)
                for node in f.maker.fgraph.apply_nodes
            )
            assert not any(
                isinstance(node.op, dnn.GpuDnnConvGradW)
                for node in f.maker.fgraph.apply_nodes
            )
            if self.ndim == 3:
                flipped_filters = filters[:, :, ::-1, ::-1, ::-1]
            else:
                flipped_filters = filters[:, :, ::-1, ::-1]
            conv_ref = self.cpu_conv_class(subsample=unit_shape)(
                ref_cast(inputs), flipped_filters
            )
            (grad_i_ref,) = aesara.grad(conv_ref.sum(), [inputs])
            f_ref = aesara.function([inputs, filters], grad_i_ref, mode="FAST_RUN")
            runtime_shapes = self.runtime_shapes
            if algo in ("time_once", "guess_once"):
                runtime_shapes = [list(runtime_shapes[0])]
                runtime_shapes[0][0] = 5
            for ntimes, (inputs_shape, filters_shape) in runtime_shapes:
                for i in range(ntimes):
                    inputs_val = np.random.random(inputs_shape).astype(dtype)
                    filters_val = np.random.random(filters_shape).astype(dtype)
                    gpu_res = f(inputs_val, filters_val)
                    cpu_res = f_ref(inputs_val, filters_val)
                    utt.assert_allclose(cpu_res, np.asarray(gpu_res))

        for algo in self.runtime_algorithms:
            run_gradinput_runtime_algorithm(algo)

    def test_gradweight_runtime_algorithms(self):
        dtype = "float32"
        unit_shape = (1,) * self.ndim
        _broadcastable = [False] * (2 + self.ndim)

        def run_gradweight_runtime_algorithm(algo):
            aesara.config.dnn__conv__algo_bwd_filter = algo
            inputs = TensorType(dtype, _broadcastable)()
            filters = TensorType(dtype, _broadcastable)()
            conv = dnn.dnn_conv(
                img=inputs,
                kerns=filters,
                algo=algo,
                precision=dtype,
                subsample=unit_shape,
                dilation=unit_shape,
            )
            (grad_w,) = aesara.grad(conv.sum(), [filters])
            f = aesara.function([inputs, filters], grad_w, mode=mode_with_gpu)
            assert 1 == len(
                [
                    node
                    for node in f.maker.fgraph.apply_nodes
                    if isinstance(node.op, dnn.GpuDnnConvGradW)
                ]
            )
            assert not any(
                isinstance(node.op, dnn.GpuDnnConv)
                for node in f.maker.fgraph.apply_nodes
            )
            assert not any(
                isinstance(node.op, dnn.GpuDnnConvGradI)
                for node in f.maker.fgraph.apply_nodes
            )
            if self.ndim == 3:
                flipped_filters = filters[:, :, ::-1, ::-1, ::-1]
            else:
                flipped_filters = filters[:, :, ::-1, ::-1]
            conv_ref = self.cpu_conv_class(subsample=unit_shape)(
                ref_cast(inputs), flipped_filters
            )
            (grad_w_ref,) = aesara.grad(conv_ref.sum(), [filters])
            f_ref = aesara.function([inputs, filters], grad_w_ref, mode="FAST_RUN")
            runtime_shapes = self.runtime_shapes
            if algo in ("time_once", "guess_once"):
                runtime_shapes = [list(runtime_shapes[0])]
                runtime_shapes[0][0] = 5
            for ntimes, (inputs_shape, filters_shape) in runtime_shapes:
                for i in range(ntimes):
                    inputs_val = np.random.random(inputs_shape).astype(dtype)
                    filters_val = np.random.random(filters_shape).astype(dtype)
                    gpu_res = f(inputs_val, filters_val)
                    cpu_res = f_ref(inputs_val, filters_val)
                    utt.assert_allclose(cpu_res, np.asarray(gpu_res))

        for algo in self.runtime_algorithms:
            run_gradweight_runtime_algorithm(algo)


class TestDnnConv3DRuntimeAlgorithms(TestDnnConv2DRuntimeAlgorithms):
    ndim = 3
    cpu_conv_class = Corr3dMM
    runtime_shapes = [
        (3, [(2, 3, 5, 10, 9), (5, 3, 4, 7, 7)]),
        (1, [(1, 1, 5, 100, 200), (1, 1, 4, 50, 200)]),
        (1, [(4, 2, 20, 20, 20), (2, 2, 20, 19, 18)]),
        (3, [(2, 3, 5, 10, 9), (5, 3, 4, 7, 7)]),  # cache should be used
        (1, [(2, 2, 50, 50, 5), (5, 2, 25, 31, 4)]),
        (1, [(1, 1, 5, 100, 200), (1, 1, 4, 50, 200)]),  # cache should be used
        (1, [(4, 2, 20, 20, 20), (2, 2, 20, 19, 18)]),  # cache should be used
        (1, [(1, 2, 3, 4, 5), (6, 2, 3, 2, 1)]),
    ]


def test_conv_guess_once_with_dtypes():
    # This test checks that runtime conv algorithm selection does not raise any exception
    # when consecutive functions with different dtypes and precisions are executed.

    inputs_shape = (2, 3, 5, 5)
    filters_shape = (2, 3, 40, 4)
    border_mode = "full"

    def get_function(dtype, precision):
        inputs_val = np.random.random(inputs_shape).astype(dtype)
        filters_val = np.random.random(filters_shape).astype(dtype)
        inputs_val /= 10
        filters_val /= 10
        inputs = aesara.shared(inputs_val)
        filters = aesara.shared(filters_val)
        conv = dnn.dnn_conv(
            img=inputs,
            kerns=filters,
            border_mode=border_mode,
            precision=precision,
            algo="guess_once",
            direction_hint="forward!",
        )
        return aesara.function([], conv, mode=mode_with_gpu)

    f_true_half_config = get_function("float16", "float16")
    f_pseudo_half_config = get_function("float16", "float32")
    f_float_config = get_function("float32", "float32")
    f_double_config = get_function("float64", "float64")
    # Let's just see if everything runs without raising any exception.
    try:
        f_true_half_config()
    except RuntimeError as e:
        # float16 precision is not supported on all GPU cards.
        assert "CUDNN_STATUS_ARCH_MISMATCH" in str(e)
    f_pseudo_half_config()
    f_float_config()
    f_double_config()


def test_opt_f16_prec32():
    inputs = TensorType("float16", (False,) * 4)()
    filters = TensorType("float16", (False,) * 4)()
    conv = conv2d(inputs, filters)

    gfilt = aesara.grad(conv.sum(), filters)

    # If this compiles we are good
    aesara.function([inputs, filters], [conv, gfilt], mode=mode_with_gpu)
