import numpy as np

import aesara
import aesara.tensor as at
import tests.unittest_tools as utt
from aesara.gpuarray.nnet import (
    GpuCrossentropySoftmax1HotWithBiasDx,
    GpuCrossentropySoftmaxArgmax1HotWithBias,
    GpuSoftmax,
    GpuSoftmaxWithBias,
)
from aesara.gradient import grad
from aesara.tensor.math import argmax, log, mean
from aesara.tensor.nnet import crossentropy_softmax_1hot_with_bias_dx
from aesara.tensor.type import fmatrix, fvector, lvector, matrix, vector
from tests.gpuarray.config import mode_with_gpu, mode_without_gpu


mode_wo_cudnn = mode_with_gpu.excluding("cudnn")


def test_GpuCrossentropySoftmaxArgmax1HotWithBias():
    # This is basic test for GpuCrossentropySoftmaxArgmax1HotWithBias
    # We check that we loop when their is too much threads

    n_in = 1000
    batch_size = 4097
    n_out = 1250

    if not isinstance(mode_with_gpu, aesara.compile.debugmode.DebugMode):
        n_in = 4098
        n_out = 4099

    y = lvector("y")

    b = fvector("b")

    # we precompute the dot with big shape before to allow the test of
    # GpuCrossentropySoftmax1HotWithBiasDx to don't fail with the error
    # (the launch timed out and was terminated) on GPU card not
    # powerful enough. We need the big shape to check for corner
    # case.
    dot_result = fmatrix("dot_result")

    xx = np.asarray(np.random.rand(batch_size, n_in), dtype=np.float32)
    yy = np.ones((batch_size,), dtype="int32")
    b_values = np.zeros((n_out,), dtype="float32")
    W_values = np.asarray(np.random.rand(n_in, n_out), dtype="float32")

    dot_value = np.asarray(np.dot(xx, W_values), dtype="float32")
    del W_values
    p_y_given_x = aesara.tensor.nnet.softmax(dot_result + b)
    y_pred = argmax(p_y_given_x, axis=-1)
    loss = -mean(log(p_y_given_x)[at.arange(y.shape[0]), y])
    dW = grad(loss, dot_result)
    classify = aesara.function(
        inputs=[y, b, dot_result], outputs=[loss, y_pred, dW], mode=mode_without_gpu
    )
    classify_gpu = aesara.function(
        inputs=[y, b, dot_result], outputs=[loss, y_pred, dW], mode=mode_with_gpu
    )

    assert any(
        [
            isinstance(
                node.op, aesara.tensor.nnet.CrossentropySoftmaxArgmax1HotWithBias
            )
            for node in classify.maker.fgraph.toposort()
        ]
    )
    assert any(
        [
            isinstance(node.op, GpuCrossentropySoftmaxArgmax1HotWithBias)
            for node in classify_gpu.maker.fgraph.toposort()
        ]
    )

    out = classify(yy, b_values, dot_value)
    gout = classify_gpu(yy, b_values, dot_value)

    assert len(out) == len(gout) == 3
    utt.assert_allclose(out[0], gout[0])
    utt.assert_allclose(out[2], gout[2], atol=3e-6)
    utt.assert_allclose(out[1], gout[1])


def test_GpuCrossentropySoftmax1HotWithBiasDx():
    # This is basic test for GpuCrossentropySoftmax1HotWithBiasDx
    # We check that we loop when their is too much threads

    batch_size = 4097
    n_out = 1250

    if not isinstance(mode_with_gpu, aesara.compile.debugmode.DebugMode):
        n_out = 4099

    softmax_output_value = np.random.rand(batch_size, n_out).astype("float32")
    dnll_value = np.asarray(np.random.rand(batch_size), dtype="float32")
    y_idx_value = np.random.randint(low=0, high=5, size=batch_size)

    softmax_output = fmatrix()
    softmax_output /= softmax_output.sum(axis=1).reshape(softmax_output.shape[1], 1)
    op = crossentropy_softmax_1hot_with_bias_dx(dnll_value, softmax_output, y_idx_value)

    cpu_f = aesara.function([softmax_output], op, mode=mode_without_gpu)
    gpu_f = aesara.function([softmax_output], op, mode=mode_with_gpu)
    # aesara.printing.debugprint(cpu_f)
    # aesara.printing.debugprint(gpu_f)

    assert any(
        [
            isinstance(node.op, aesara.tensor.nnet.CrossentropySoftmax1HotWithBiasDx)
            for node in cpu_f.maker.fgraph.toposort()
        ]
    )
    assert any(
        [
            isinstance(node.op, GpuCrossentropySoftmax1HotWithBiasDx)
            for node in gpu_f.maker.fgraph.toposort()
        ]
    )

    cpu_out = cpu_f(softmax_output_value)
    gpu_out = gpu_f(softmax_output_value)

    rtol = 1e-5
    atol = 1e-6
    utt.assert_allclose(cpu_out, gpu_out, rtol=rtol, atol=atol)


def test_softmax_with_bias_float16():
    softmax_with_bias_unittest_template(dtypeInput="float16", dtypeBias="float32")
    softmax_with_bias_unittest_template(dtypeInput="float16", dtypeBias="float16")
    softmax_with_bias_unittest_template(dtypeInput="float32", dtypeBias="float16")


def test_softmax_with_bias_float32():
    softmax_with_bias_unittest_template(dtypeInput="float32", dtypeBias="float32")


def test_softmax_with_bias_float64():
    softmax_with_bias_unittest_template(dtypeInput="float32", dtypeBias="float64")
    softmax_with_bias_unittest_template(dtypeInput="float64", dtypeBias="float32")
    softmax_with_bias_unittest_template(dtypeInput="float64", dtypeBias="float64")


def softmax_with_bias_unittest_template(dtypeInput, dtypeBias):
    # This is a basic test for GpuSoftmaxWithBias.
    #
    # We check that we loop when there are too many blocks.
    #
    # TODO: check that we loop when there are too many threads. (THIS IS
    # NOT IMPLEMENTED)

    x = matrix("x", dtype=dtypeInput)
    b = vector("b", dtype=dtypeBias)

    z = aesara.tensor.nnet.softmax_with_bias(x, b)

    f = aesara.function([x, b], z, mode=mode_without_gpu)
    f_gpu = aesara.function([x, b], z, mode=mode_with_gpu)
    assert f.maker.fgraph.toposort()[-1].op == aesara.tensor.nnet.softmax_with_bias
    assert isinstance(f_gpu.maker.fgraph.toposort()[-2].op, GpuSoftmaxWithBias)

    def cmp(n, m):
        data = np.random.uniform(1e-7, 1, (n, m)).astype(dtype=dtypeInput)
        b_data = np.random.uniform(1e-7, 1, (m,)).astype(dtype=dtypeBias)

        out = f(data, b_data)
        gout = f_gpu(data, b_data)
        utt.assert_allclose(out, gout)

    cmp(2, 5)
    # we need to test n>32*1024 to check that we make the block loop.
    cmp(2 << 15, 5)
    cmp(4074, 400)
    cmp(784, 784)
    cmp(4, 1000)
    cmp(4, 1024)
    cmp(4, 2000)
    cmp(4, 2024)
    # GTX285 don't have enough shared mem for this case.
    cmp(4, 4074)
    # The GTX580, 680 and kepler don't have enough shared memory.
    cmp(2, 10000)
    cmp(128, 16 * 1024)
    cmp(128, 64 * 1024)


def test_softmax_float16():
    softmax_unittest_template("float16")


def test_softmax_float32():
    softmax_unittest_template("float32")


def test_softmax_float64():
    softmax_unittest_template("float64")


def softmax_unittest_template(dtypeInput):
    # This is basic test for GpuSoftmax.
    #
    # We check that we loop when their is too much block
    # We use slower code when there isn't enough shared memory

    x = matrix("x", dtype=dtypeInput)

    z = aesara.tensor.nnet.softmax(x)
    f = aesara.function([x], z, mode=mode_without_gpu)
    f_gpu = aesara.function([x], z, mode=mode_wo_cudnn)
    assert f.maker.fgraph.toposort()[-1].op == aesara.tensor.nnet.softmax_legacy
    assert isinstance(f_gpu.maker.fgraph.toposort()[-2].op, GpuSoftmax)

    def cmp(n, m):
        data = np.random.uniform(0, 1, (n, m)).astype(dtype=dtypeInput)

        out = f(data)
        gout = f_gpu(data)
        utt.assert_allclose(out, gout)

    # we need to test n>32*1024 to check that we make the block loop.
    cmp(2, 5)
    cmp(2 << 15, 5)
    cmp(4074, 400)
    cmp(784, 784)
    cmp(4, 1000)
    cmp(4, 1024)
    cmp(4, 2000)
    cmp(4, 2024)
    # The GTX285 don't have enough shared memory.
    cmp(4, 4074)
    # The GTX580, 680 and kepler don't have enough shared memory.
    cmp(2, 10000)
    cmp(128, 16 * 1024)
    cmp(128, 64 * 1024)


class TestSoftMax:
    gpu_op = GpuSoftmax
    mode = mode_wo_cudnn

    def _test_softmax(self, x, x_gpu, f_z, f_gpu_z, cmp):
        # This is basic test for GpuSoftmax and GpuDnnSoftmax
        #
        # We check that we loop when there is too much block
        # We use slower code when there isn't enough shared memory

        f_z_out = f_z(x)
        f_gpu_z_out = f_gpu_z(x_gpu)

        f = aesara.function([x], f_z_out, mode=mode_without_gpu)
        f_gpu = aesara.function([x_gpu], f_gpu_z_out, mode=self.mode)
        self._check_types(f, f_gpu, aesara.tensor.nnet.Softmax, self.gpu_op)

        # we need to test n>32*1024 to check that we make the block loop.
        cmp(1, 5, f, f_gpu)
        cmp(2, 5, f, f_gpu)
        cmp(10, 5, f, f_gpu)
        cmp(100, 5, f, f_gpu)
        cmp(1000, 5, f, f_gpu)
        cmp(10000, 5, f, f_gpu)
        cmp(4074, 400, f, f_gpu)
        cmp(784, 784, f, f_gpu)
        cmp(4, 1000, f, f_gpu)
        cmp(4, 1024, f, f_gpu)
        cmp(4, 2000, f, f_gpu)
        cmp(4, 2024, f, f_gpu)
        # The GTX285 don't have enough shared memory.
        cmp(4, 4074, f, f_gpu)
        # The GTX580, 680 and kepler don't have enough shared memory.
        cmp(2, 10000, f, f_gpu)
        cmp(128, 16 * 1024, f, f_gpu)
        cmp(128, 64 * 1024, f, f_gpu)
        # cudnn permits no more than 2^15 - 1 rows
        cmp((2 << 15) - 1, 5, f, f_gpu)
        cmp(5, 2 << 15, f, f_gpu)

        return f, f_gpu

    def _cmp(self, n, m, f, f_gpu):
        data = np.arange(n * m, dtype="float32").reshape(n, m)
        out = f(data)
        gout = f_gpu(data)
        utt.assert_allclose(out, gout)

    def _check_types(self, graph, graph_gpu, f_type, f_gpu_type):
        assert isinstance(graph.maker.fgraph.toposort()[-1].op, f_type)
        assert (
            len(
                [
                    node
                    for node in graph_gpu.maker.fgraph.toposort()
                    if isinstance(node.op, f_gpu_type)
                ]
            )
            == 1
        )

    def test_softmax(self):
        x = fmatrix("x")
        z = aesara.tensor.nnet.softmax_legacy

        f, f_gpu = self._test_softmax(x, x, z, z, self._cmp)

        self._cmp(2 << 15, 5, f, f_gpu)

    def test_softmax_shape_0(self):
        x = fmatrix("x")
        z = aesara.tensor.nnet.softmax_legacy

        f, f_gpu = self._test_softmax(x, x, z, z, self._cmp)
        # Aesara can handle that case, but cudnn can't
        self._cmp(0, 10, f, f_gpu)
