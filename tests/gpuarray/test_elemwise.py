import numpy as np
import pytest

import aesara
import aesara.scalar as aes
import aesara.tensor as at


pygpu = pytest.importorskip("pygpu")
gpuarray = pygpu.ndgpuarray

from copy import copy

from aesara.compile.debugmode import DebugMode
from aesara.compile.mode import Mode
from aesara.gpuarray.dnn import GpuDnnReduction
from aesara.gpuarray.elemwise import (
    GpuCAReduceCPY,
    GpuCAReduceCuda,
    GpuDimShuffle,
    GpuElemwise,
    GpuErfcinv,
    GpuErfinv,
)
from aesara.gpuarray.type import GpuArrayType, get_context, gpuarray_shared_constructor
from aesara.link.basic import PerformLinker
from aesara.link.c.basic import CLinker
from aesara.tensor.math import erfcinv, erfinv, mul, tanh
from aesara.tensor.type import bvector, float_dtypes, fmatrix, fvector, vector
from tests.gpuarray.config import mode_with_gpu, mode_without_gpu, test_ctx_name
from tests.gpuarray.test_basic_ops import rand_gpuarray
from tests.tensor import test_elemwise
from tests.unittest_tools import assert_allclose


# This is actually a test for GpuElemwise
class TestGpuBroadcast(test_elemwise.TestBroadcast):
    cop = GpuElemwise
    ctype = GpuArrayType
    # The order is important
    linkers = [PerformLinker, CLinker]

    def rand_cval(self, shp):
        return rand_gpuarray(*shp, cls=gpuarray)


def test_elemwise_pow():
    # Test that GpuElemwise(pow) can compile with any combination of integer
    # or float input dtype.
    dtypes = [
        "uint8",
        "uint16",
        "uint32",
        "uint64",
        "int8",
        "int16",
        "int32",
        "int64",
        "float16",
        "float32",
        "float64",
    ]

    for dtype_base in dtypes:
        for dtype_exp in dtypes:

            # Compile a gpu function with the specified dtypes
            base_val = np.random.randint(0, 5, size=10).astype(dtype_base)
            exp_val = np.random.randint(0, 3, size=10).astype(dtype_exp)

            base = vector(dtype=dtype_base)
            exp = gpuarray_shared_constructor(exp_val)
            assert exp.dtype == dtype_exp
            output = base ** exp
            f = aesara.function([base], output, mode=mode_with_gpu)
            # We don't transfer to the GPU when the output dtype is int*
            n = len(
                [n for n in f.maker.fgraph.apply_nodes if isinstance(n.op, GpuElemwise)]
            )
            assert n == (output.dtype in float_dtypes)

            # Call the function to make sure the output is valid
            out = f(base_val)
            expected_out = base_val ** exp_val
            assert_allclose(out, expected_out)


class TestMathErrorFunctions:
    dtypes = ["float64", "float32", "float16"]
    default_arrays = {}
    expected_erfinv_outputs = {}
    expected_erfcinv_outputs = {}

    @classmethod
    def setup_class(cls):
        scipy_special = pytest.importorskip("scipy.special")

        # NB: erfinv is defined in ]-1;1[, and erfcinv is defined in ]0;2[,
        # so we just take some values in an interval that covers both domains
        # (this will also allow to test some values outside the domains).
        # We take [-5;5[ by default and we concatenate it 1000 times
        # to have the GPU ops run on large data.
        default_array = [x / 10.0 for x in range(-50, 50)] * 1000
        for dtype in cls.dtypes:
            numpy_array = np.asarray(default_array, dtype=dtype)
            cls.default_arrays[dtype] = numpy_array
            cls.expected_erfinv_outputs[dtype] = scipy_special.erfinv(numpy_array)
            cls.expected_erfcinv_outputs[dtype] = scipy_special.erfcinv(numpy_array)

        # Since there are infinite values, we need to disable that check
        # in DebugMode if needed
        if isinstance(mode_with_gpu, DebugMode):
            cls.mode_with_gpu = copy(mode_with_gpu)
            cls.mode_with_gpu.check_isfinite = False
        else:
            cls.mode_with_gpu = mode_with_gpu
        if isinstance(mode_without_gpu, DebugMode):
            cls.mode_without_gpu = copy(mode_without_gpu)
            cls.mode_without_gpu.check_isfinite = False
        else:
            cls.mode_without_gpu = mode_without_gpu

    def check_gpu_scalar_op(self, aesara_function, scalar_optype):
        for node in aesara_function.maker.fgraph.apply_nodes:
            if isinstance(node.op, GpuElemwise) and isinstance(
                node.op.scalar_op, scalar_optype
            ):
                return True
        aesara.printing.debugprint(aesara_function)
        return False

    def test_elemwise_erfinv(self):
        for dtype in self.dtypes:
            vec = vector(dtype=dtype)
            output = erfinv(vec)
            f_host = aesara.function(
                [vec],
                output,
                name="HOST/erfinv/" + dtype,
                mode=self.mode_without_gpu,
            )
            f_gpu = aesara.function(
                [vec], output, name="GPU/erfinv/" + dtype, mode=self.mode_with_gpu
            )
            assert (
                len(
                    [
                        n
                        for n in f_host.maker.fgraph.apply_nodes
                        if isinstance(n.op, GpuElemwise)
                    ]
                )
                == 0
            )
            if not aesara.config.device.startswith("opencl"):
                assert self.check_gpu_scalar_op(
                    f_gpu, GpuErfinv
                ), 'Function graph does not contains scalar op "GpuErfinv".'
            vector_val = self.default_arrays[dtype]
            f_host(vector_val)
            f_gpu(vector_val)
            out_host = f_host(vector_val)
            out_gpu = f_gpu(vector_val)
            assert_allclose(out_host, out_gpu)
            assert_allclose(self.expected_erfinv_outputs[dtype], out_gpu)

    def test_elemwise_erfcinv(self):
        for dtype in self.dtypes:
            vec = vector(dtype=dtype)
            output = erfcinv(vec)
            f_host = aesara.function(
                [vec],
                output,
                name="HOST/erfcinv/" + dtype,
                mode=self.mode_without_gpu,
            )
            f_gpu = aesara.function(
                [vec], output, name="GPU/erfcinv/" + dtype, mode=self.mode_with_gpu
            )
            assert (
                len(
                    [
                        n
                        for n in f_host.maker.fgraph.apply_nodes
                        if isinstance(n.op, GpuElemwise)
                    ]
                )
                == 0
            )
            if not aesara.config.device.startswith("opencl"):
                assert self.check_gpu_scalar_op(
                    f_gpu, GpuErfcinv
                ), 'Function graph does not contains scalar op "GpuErfcinv".'
            vector_val = self.default_arrays[dtype]
            f_host(vector_val)
            f_gpu(vector_val)
            out_host = f_host(vector_val)
            out_gpu = f_gpu(vector_val)
            assert_allclose(out_host, out_gpu)
            assert_allclose(self.expected_erfcinv_outputs[dtype], out_gpu)


class TestFloat16:
    def test_composite_elemwise_float16(self):
        w = bvector()
        x = vector(dtype="float16")
        y = fvector()

        cz = tanh(x + at.cast(y, "float16"))
        o = (
            cz
            - cz ** 2
            + at.cast(x, "int16")
            + at.cast(x, "float32")
            + at.cast(w, "float16")
            - at.constant(np.float16(1.0))
        )

        aesara.function([w, x, y], o, mode=mode_with_gpu)

        v = vector(dtype="uint8")
        w = vector(dtype="float16")
        x = vector(dtype="float16")
        y = vector(dtype="float16")
        z = vector(dtype="float16")

        o = at.switch(v, mul(w, x, y), z)
        aesara.function([v, w, x, y, z], o, mode=mode_with_gpu)

    def test_cast_float16(self):
        f16 = vector(dtype="float16")
        f32 = fvector()
        i8 = bvector()
        f = aesara.function(
            [f16, f32, i8],
            [
                f16.astype("float32"),
                f32.astype("float16"),
                f32.astype("float64"),
                f16.astype("int8"),
                f32.astype("int8"),
                i8.astype("float16"),
                i8.astype("float32"),
            ],
            mode=mode_with_gpu,
        )

        d1 = (np.random.rand(4) * 10).astype("float16")
        d2 = (np.random.rand(5) * 10).astype("float32")
        d3 = (np.random.rand(6) * 10).astype("int8")
        res = f(d1, d2, d3)

        for i, out in enumerate(f.outputs):
            dtype = out.variable.dtype
            assert res[i].dtype == dtype
            inp = out.variable.owner.inputs[0]
            if inp.dtype == "float16":
                d = d1
            elif inp.dtype == "float32":
                d = d2
            else:
                d = d3
            assert_allclose(d.astype(dtype), res[i])


class TestGpuDimShuffle(test_elemwise.TestDimShuffle):
    op = GpuDimShuffle


class TestGpuCAReduceCPY(test_elemwise.TestCAReduce):
    dtypes = ["float32"]
    bin_dtypes = ["uint8", "int8"]
    op = GpuCAReduceCPY
    reds = [aes.add, aes.mul]
    pre_scalar_op = None
    mode = mode_with_gpu

    def test_perform(self):
        for dtype in self.dtypes + self.bin_dtypes:
            for op in self.reds:
                self.with_mode(
                    Mode(linker="py", optimizer=mode_with_gpu.optimizer),
                    op,
                    dtype=dtype,
                    pre_scalar_op=self.pre_scalar_op,
                )

    def test_perform_nan(self):
        for dtype in self.dtypes:
            if not dtype.startswith("float"):
                continue
            for op in self.reds:
                self.with_mode(
                    Mode(linker="py", optimizer=mode_with_gpu.optimizer),
                    op,
                    dtype=dtype,
                    test_nan=True,
                    pre_scalar_op=self.pre_scalar_op,
                )

    def test_c(self):
        for dtype in self.dtypes + self.bin_dtypes:
            for op in self.reds:
                self.with_mode(
                    Mode(linker="c", optimizer=mode_with_gpu.optimizer),
                    op,
                    dtype=dtype,
                    pre_scalar_op=self.pre_scalar_op,
                )

    def test_c_nan(self):
        for dtype in self.dtypes:
            if not dtype.startswith("float"):
                continue
            for op in self.reds:
                self.with_mode(
                    Mode(linker="c", optimizer=mode_with_gpu.optimizer),
                    op,
                    dtype=dtype,
                    test_nan=True,
                    pre_scalar_op=self.pre_scalar_op,
                )

    def test_infer_shape(self):
        for dtype in self.dtypes:
            super().test_infer_shape(dtype)


class TestGpuCAReduceCuda(TestGpuCAReduceCPY):
    dtypes = ["float32", "int64"]
    bin_dtypes = ["uint8", "int8"]

    cases = [
        ((5, 6), None),
        ((5, 6), (0, 1)),
        ((5, 6), (0,)),
        ((5, 6), (1,)),
        ((5, 6), (-1,)),
        ((5, 6), (-2,)),
        # ((5, 6), ()),  #reduce on no axis(copy) isn't implemented
        # ((2, 3, 4, 5), (0, 1, 3)), mask 1101 isn't implemented
        # ((2, 3, 4, 5), (-2, -3)), mask 0110 isn't implemented
        ((5, 0), None),
        ((5, 0), (0,)),
        ((5, 0), (1,)),
        # ((5, 0), ()), reduce on no axis isn't implemented
        # ((), None), reduce on no axis isn't implemented
        # ((), ()) reduce on no axis isn't implemented
        # Test all GPU cases implemented
        ((1, 0), (1,)),
        ((0, 1), (1,)),
        ((0, 0), (1,)),
        ((0, 0, 0), (1, 2)),
        ((0, 0, 0, 0), (1, 2, 3)),
        ((2, 1), (1,)),
        ((1, 2), (1,)),
        ((100, 3, 1300), [1]),
        ((0,), [0]),
        ((5,), [0]),
        ((0, 0), [0, 1]),
        ((1, 0), [0, 1]),
        ((5, 4), [0, 1]),
        ((33, 31), [0, 1]),
        ((5, 4), [1]),
        ((5, 4), [0]),  # need something bigger then 32 for some opt test.
        ((5, 4, 3), [0]),
        ((5, 4, 3), [1]),
        ((5, 4, 3), [0, 1]),
        ((5, 4, 3), [2]),
        ((5, 4, 3), [1, 2]),
        ((5, 4, 3), [0, 1, 2]),
        ((0, 0, 0, 0), [0, 1, 2, 3]),
        ((5, 4, 3, 20), [2, 3]),
        ((5, 4, 3, 2), [0, 1, 2, 3]),
        ((5, 4, 3, 2), [0, 2, 3]),
        ((5, 4, 3, 2), [1, 2, 3]),
        # test shape bigger then 4096 on each dimension to make sure that we work correctly when we don't have enough thread/block in each dimensions
        ((4100, 3), [0]),
        ((3, 4101), [0]),  # 10
        ((1024, 33), [0]),
        ((33, 1024), [0]),  # 10
        ((1025, 33), [0]),
        ((33, 1025), [0]),  # 10
        ((4100, 3), [1]),
        ((3, 4101), [1]),  # 01
        ((1024, 33), [1]),
        ((33, 1024), [1]),  # 01
        ((1025, 33), [1]),
        ((33, 1025), [1]),  # 01
        ((4100, 3), [0, 1]),
        ((3, 4101), [0, 1]),  # 11
        ((1024, 33), [0, 1]),
        ((33, 1024), [0, 1]),  # 01
        ((1025, 33), [0, 1]),
        ((33, 1025), [0, 1]),  # 01
        ((4100, 4, 3), [0]),
        ((5, 4100, 3), [0]),
        ((5, 4, 4100), [0]),
        ((3, 65536, 1), [0]),  # 100
        ((4100, 4, 3), [1]),
        ((5, 4100, 3), [1]),
        ((5, 4, 4100), [1]),  # 010
        ((4100, 4, 3), [2]),
        ((5, 4100, 3), [2]),
        ((5, 4, 4100), [2]),  # 001
        ((4100, 4, 3), [0, 1]),
        ((5, 4100, 3), [0, 1]),
        ((5, 4, 4100), [0, 1]),  # 110
        ((4100, 4, 3), [1, 2]),
        ((5, 4100, 3), [1, 2]),
        ((5, 4, 4100), [1, 2]),  # 011
        ((4100, 4, 3), [0, 2]),
        ((5, 4100, 3), [0, 2]),
        ((5, 4, 4100), [0, 2]),  # 101
        ((4100, 4, 3), [0, 1, 2]),
        ((5, 4100, 3), [0, 1, 2]),
        ((5, 4, 4100), [0, 1, 2]),  # 111
        ((65, 4, 3), [0, 1, 2]),
        ((5, 65, 3), [0, 1, 2]),
        ((5, 4, 65), [0, 1, 2]),  # 111
        # reduce over 2d
        ((4100, 4, 3, 2), [2, 3]),
        ((4, 4100, 3, 2), [2, 3]),
        ((4, 3, 4100, 2), [2, 3]),
        ((4, 3, 2, 4100), [2, 3]),  # 0011
        ((4100, 4, 3, 2), [1, 3]),
        ((4, 4100, 3, 2), [1, 3]),
        ((4, 3, 4100, 2), [1, 3]),
        ((4, 3, 2, 4100), [1, 3]),  # 0101
        # ((4100, 4, 3, 2), [1, 2]), ((4, 4100, 3, 2), [1, 2]), ((4, 3, 4100, 2), [1, 2]), ((4, 3, 2, 4100), [1, 2]),  # 0110 by reshape
        # ((4100,4,3,2),[0,3]),((4,4100,3,2),[0,3]),((4,3,4100,2),[0,3]),((4,3,2,4100),[0,3]),  # 1001 by reshape
        # ((4100,4,3,2),[0,2]),((4,4100,3,2),[0,2]),((4,3,4100,2),[0,2]),((4,3,2,4100),[0,2]),  # 1010 not implemented
        # ((4100, 4, 3, 2), [0, 1]), ((4, 4100, 3, 2), [0, 1]), ((4, 3, 4100, 2), [0, 1]), ((4, 3, 2, 4100), [0, 1]),  # 1100 by reshape
        # reduce over 3d
        # 3d not tested: 1101, 1110, 1111
        # ((4100,4,3,2),[0,1,3]),((4,4100,3,2),[0,1,3]),((4,3,4100,2),[0,1,3]),((4,3,2,4100),[0,1,3]),  # 1101 by reshape
        # ((4100, 4, 3, 2), [0, 1, 2]), ((4, 4100, 3, 2), [0, 1, 2]), ((4, 3, 4100, 2), [0, 1, 2]), ((4, 3, 2, 4100), [0, 1, 2]),  # 1110 by reshape
        ((4100, 4, 3, 2), [0, 2, 3]),
        ((4, 4100, 3, 2), [0, 2, 3]),
        ((4, 3, 4100, 2), [0, 2, 3]),  # ((4,3,2,4100),[0,2,3]),  # 1011
        ((4100, 4, 3, 2), [1, 2, 3]),
        ((4, 4100, 3, 2), [1, 2, 3]),
        ((4, 3, 4100, 2), [1, 2, 3]),
        ((4, 3, 2, 4100), [1, 2, 3]),  # 0111
        ((65, 4, 3, 2), [1, 2, 3]),
        ((4, 65, 3, 2), [1, 2, 3]),
        ((4, 3, 65, 2), [1, 2, 3]),
        ((4, 3, 2, 65), [1, 2, 3]),  # 0111
        # reduce over 4d
        ((4100, 2, 3, 4), [0, 1, 2, 3]),
        ((2, 4100, 3, 4), [0, 1, 2, 3]),
        ((2, 3, 4100, 4), [0, 1, 2, 3]),
        ((2, 3, 4, 4100), [0, 1, 2, 3]),
        ((128, 1, 3, 3), [0, 1, 2, 3]),  # 1111
        # test pattern implemented by reshape
        # Skip them as this test the op directly, not the optimization with reshape
        # ((4100,4,3,2),[0]),((4,4100,3,2),[0]),((4,3,4100,2),[0]),((4,3,2,4100),[0]),#1000
        # ((4100,4,3,2),[1]),((4,4100,3,2),[1]),((4,3,4100,2),[1]),((4,3,2,4100),[1]),#0100
        # ((4100,4,3,2),[2]),((4,4100,3,2),[2]),((4,3,4100,2),[2]),((4,3,2,4100),[2]),#0010
        # ((4100,4,3,2),[3]),((4,4100,3,2),[3]),((4,3,4100,2),[3]),((4,3,2,4100),[3]),#0001
        # ((1100,2,3,4,5),[0,1,2,3,4]),((2,1100,3,4,5),[0,1,2,3,4]),((2,3,1100,4,5),[0,1,2,3,4]),((2,3,4,1100,5),[0,1,2,3,4]),((2,3,4,5,1100),[0,1,2,3,4]),#11111
        # ((5,4,3,10,11),[1,2]),
    ]
    op = GpuCAReduceCuda
    reds = [aes.add, aes.mul, aes.scalar_maximum, aes.scalar_minimum]
    pre_scalar_op = None

    def test_perform_noopt(self):
        return

    def test_perform(self):
        return

    def test_perform_nan(self):
        return

    def setup_method(self):
        super().setup_method()
        if get_context(test_ctx_name).kind != b"cuda":
            pytest.skip("Cuda specific tests")


class TestGpuReduceDtype(test_elemwise.TestReduceDtype):
    mode = mode_with_gpu.excluding("local_cut_useless_reduce")

    # GpuDnnReduction doesn't cover all cases, but should cover some
    op = (GpuCAReduceCuda, GpuDnnReduction)
    # Currently we don't support reduction on 0 axis
    axes = [None, 0, 1, 1, [0], [1], [0, 1]]
    # We don't support complex dtype
    dtypes = [
        "int8",
        "int16",
        "int32",
        "int64",
        "uint8",
        "uint16",
        "uint32",
        "uint64",
        "float32",
        "float64",
    ]

    def setup_method(self):
        if get_context(test_ctx_name).kind != b"cuda":
            pytest.skip("Cuda specific tests")


def speed_reduce10():
    data = np.random.rand(1000, 1000).astype("float32")
    m = fmatrix()
    f = aesara.function([m], [m.sum(axis=0), m.T.sum(axis=0)], mode=mode_with_gpu)
    f(data)
