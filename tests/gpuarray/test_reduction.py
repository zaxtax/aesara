import math

import numpy as np
import pytest

import aesara
import aesara.tensor as at
from aesara.gpuarray import GpuArrayType
from aesara.gpuarray.dnn import GpuDnnReduction
from aesara.gpuarray.reduction import GpuMaxAndArgmax
from aesara.tensor.math import argmax
from aesara.tensor.math import max as at_max
from tests import unittest_tools as utt
from tests.gpuarray.config import mode_with_gpu, mode_without_gpu
from tests.gpuarray.test_basic_ops import rand_gpuarray


# Number of values to be used in test tensors (except with 0-D tensors!).
test_size = 10000

# NB: This order of "unsorted axes" is arbitrary and is here
# just to have the same information on profile output
# from one test to another.
unsorted_axes = (2, 4, 0, 3, 1)

np.random.seed()


def numpy_random_array(shapes):
    size = 1
    for dimsize in shapes:
        size *= dimsize
    return np.random.normal(size=size).astype(aesara.config.floatX).reshape(shapes)


def numpy_maxandargmax(X, axis=None):
    if axis is None:
        axis = list(range(X.ndim))
    elif not isinstance(axis, (tuple, list)):
        axis = [int(axis)]
    axis = list(set(axis))  # remove duplicated values.
    axis.sort()
    axis = tuple(axis)
    ref_max = np.max(X, axis=axis)
    # Following code is copied from MaxAndArgmax.perform():
    # Numpy does not support multiple axes for argmax. Work around.
    keep_axes = np.array([i for i in range(X.ndim) if i not in axis], dtype="int64")
    # Not-reduced axes in front
    transposed_x = np.transpose(X, np.concatenate((keep_axes, axis)))
    kept_shape = transposed_x.shape[: len(keep_axes)]
    reduced_shape = transposed_x.shape[len(keep_axes) :]
    new_shape = kept_shape + (np.prod(reduced_shape),)
    new_shape = tuple(int(i) for i in new_shape)
    reshaped_x = transposed_x.reshape(new_shape)
    return (ref_max, np.argmax(reshaped_x, axis=-1))


def check_if_gpu_reduce_in_graph(aesara_function):
    assert any(
        isinstance(node.op, (GpuMaxAndArgmax, GpuDnnReduction))
        for node in aesara_function.maker.fgraph.apply_nodes
    )


def check_if_gpu_reduce_not_in_graph(aesara_function):
    assert all(
        not isinstance(node.op, (GpuMaxAndArgmax, GpuDnnReduction))
        for node in aesara_function.maker.fgraph.apply_nodes
    )


class BaseTest:
    # This attribute must be set in subclasses.
    tensor_size = None
    shape = None

    dtype = aesara.config.floatX

    def get_shape(self):
        if self.tensor_size == 0:
            return []
        return [
            int(math.ceil(math.pow(test_size, 1 / self.tensor_size)))
        ] * self.tensor_size

    def setup_method(self):
        if not isinstance(self.tensor_size, int):
            pytest.skip("No tensor ndim defined.")
        if self.tensor_size < 0 or self.tensor_size > 5:
            pytest.skip(
                "We allow from 0 (included) to 5 (included) dimensons for these tests."
            )
        if self.shape is None:
            self.shape = self.get_shape()

    def get_host_tensor(self):
        broadcastable = (False,) * self.tensor_size
        return at.tensor(self.dtype, broadcastable)

    def get_gpu_tensor(self):
        broadcastable = (False,) * self.tensor_size
        return GpuArrayType(self.dtype, broadcastable)()

    def get_host_value(self):
        return numpy_random_array(self.shape)

    def get_gpu_value(self):
        return rand_gpuarray(*self.shape)

    # NB: In compute_host() and compute_gpu(),
    # the first call of the aesara function should be ignored in profiling,
    # with Aesara config flag profiling__ignore_first_call=True.

    def compute_host(self, test_tensor, axis):
        M = self.get_host_tensor()
        f = aesara.function(
            [M],
            [at_max(M, axis=axis), argmax(M, axis=axis)],
            name="shape:" + str(test_tensor.shape) + "/axis:" + str(axis) + "/HOST",
            mode=mode_without_gpu,
        )
        check_if_gpu_reduce_not_in_graph(f)
        f(test_tensor)
        aesara_max, aesara_argmax = f(test_tensor)
        ref_max, ref_argmax = numpy_maxandargmax(test_tensor, axis=axis)
        utt.assert_allclose(ref_max, aesara_max)
        utt.assert_allclose(ref_argmax, aesara_argmax)

    def compute_gpu(self, test_gpu_tensor, test_host_tensor, axis):
        M = self.get_gpu_tensor()
        f = aesara.function(
            [M],
            [at_max(M, axis=axis), argmax(M, axis=axis)],
            name="shape:" + str(test_gpu_tensor.shape) + "/axis:" + str(axis) + "/GPU",
            mode=mode_with_gpu,
        )
        check_if_gpu_reduce_in_graph(f)
        f(test_gpu_tensor)
        aesara_max, aesara_argmax = f(test_gpu_tensor)
        ref_max, ref_argmax = numpy_maxandargmax(test_host_tensor, axis=axis)
        utt.assert_allclose(ref_max, aesara_max)
        utt.assert_allclose(ref_argmax, aesara_argmax)

    def compute(self, axis=None):
        # We want to run CPU op and GPU op on the same tensor randomly generated.
        test_gpu_tensor = self.get_gpu_value()
        test_host_tensor = np.asarray(test_gpu_tensor)
        self.compute_host(test_host_tensor, axis)
        self.compute_gpu(test_gpu_tensor, test_host_tensor, axis)

    def compute_axis(self, pos):
        if self.tensor_size != 1 and 0 <= pos < self.tensor_size:
            self.compute(pos)

    def compute_some_axes(self, count):
        if 0 <= count < self.tensor_size:
            self.compute([i for i in unsorted_axes if i < self.tensor_size][:count])

    # Equivalent to test reduction on all axes.
    def test_none(self):
        self.compute(None)

    def test_axis_1(self):
        self.compute_axis(0)

    def test_axis_2(self):
        self.compute_axis(1)

    def test_axis_3(self):
        self.compute_axis(2)

    def test_axis_4(self):
        self.compute_axis(3)

    def test_axis_5(self):
        self.compute_axis(4)

    # For the tests below, we expect CPU op to run with Python implementation.

    def test_2_axes(self):
        self.compute_some_axes(2)

    def test_3_axes(self):
        self.compute_some_axes(3)

    def test_4_axes(self):
        self.compute_some_axes(4)


class TestScalar(BaseTest):
    tensor_size = 0


class TestVector(BaseTest):
    tensor_size = 1


# Special case
class TestRow(BaseTest):
    tensor_size = 2
    shape = [1, test_size]


# Special case
class TestColumn(BaseTest):
    tensor_size = 2
    shape = [test_size, 1]


class TestMatrix(BaseTest):
    tensor_size = 2


class TestTensor5(BaseTest):
    tensor_size = 5
