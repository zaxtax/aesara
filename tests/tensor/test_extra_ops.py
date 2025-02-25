import numpy as np
import pytest

import aesara
from aesara import function
from aesara import tensor as at
from aesara.compile.mode import Mode
from aesara.configdefaults import config
from aesara.gradient import grad
from aesara.graph.basic import applys_between
from aesara.graph.optdb import OptimizationQuery
from aesara.raise_op import Assert
from aesara.tensor.elemwise import DimShuffle
from aesara.tensor.extra_ops import (
    Bartlett,
    BroadcastTo,
    CpuContiguous,
    CumOp,
    DiffOp,
    FillDiagonal,
    FillDiagonalOffset,
    RavelMultiIndex,
    Repeat,
    SearchsortedOp,
    Unique,
    UnravelIndex,
    bartlett,
    bincount,
    broadcast_arrays,
    broadcast_shape,
    broadcast_to,
    compress,
    cpu_contiguous,
    cumprod,
    cumsum,
    diff,
    fill_diagonal,
    fill_diagonal_offset,
    geomspace,
    linspace,
    logspace,
    ravel_multi_index,
    repeat,
    searchsorted,
    squeeze,
    to_one_hot,
    unravel_index,
)
from aesara.tensor.subtensor import AdvancedIncSubtensor
from aesara.tensor.type import (
    TensorType,
    dmatrix,
    dscalar,
    dtensor3,
    fmatrix,
    fvector,
    integer_dtypes,
    iscalar,
    ivector,
    lscalar,
    matrix,
    scalar,
    tensor,
    tensor3,
    vector,
)
from aesara.utils import LOCAL_BITWIDTH, PYTHON_INT_BITWIDTH
from tests import unittest_tools as utt


def set_test_value(x, v):
    x.tag.test_value = v
    return x


def test_cpu_contiguous():
    a = fmatrix("a")
    i = iscalar("i")
    a_val = np.asarray(np.random.random((4, 5)), dtype="float32")
    f = aesara.function([a, i], cpu_contiguous(a.reshape((5, 4))[::i]))
    topo = f.maker.fgraph.toposort()
    assert any(isinstance(node.op, CpuContiguous) for node in topo)
    assert f(a_val, 1).flags["C_CONTIGUOUS"]
    assert f(a_val, 2).flags["C_CONTIGUOUS"]
    assert f(a_val, 3).flags["C_CONTIGUOUS"]
    # Test the grad:

    utt.verify_grad(cpu_contiguous, [np.random.random((5, 7, 2))])


class TestSearchsortedOp(utt.InferShapeTester):
    def setup_method(self):
        super().setup_method()
        self.op_class = SearchsortedOp
        self.op = SearchsortedOp()

        self.x = vector("x")
        self.v = tensor3("v")
        self.rng = np.random.default_rng(utt.fetch_seed())
        self.a = 30 * self.rng.random(50).astype(config.floatX)
        self.b = 30 * self.rng.random((8, 10, 5)).astype(config.floatX)
        self.idx_sorted = np.argsort(self.a).astype("int32")

    def test_searchsortedOp_on_sorted_input(self):
        f = aesara.function([self.x, self.v], searchsorted(self.x, self.v))
        assert np.allclose(
            np.searchsorted(self.a[self.idx_sorted], self.b),
            f(self.a[self.idx_sorted], self.b),
        )

        sorter = vector("sorter", dtype="int32")
        f = aesara.function(
            [self.x, self.v, sorter],
            self.x.searchsorted(self.v, sorter=sorter, side="right"),
        )
        assert np.allclose(
            self.a.searchsorted(self.b, sorter=self.idx_sorted, side="right"),
            f(self.a, self.b, self.idx_sorted),
        )

        sa = self.a[self.idx_sorted]
        f = aesara.function([self.x, self.v], self.x.searchsorted(self.v, side="right"))
        assert np.allclose(sa.searchsorted(self.b, side="right"), f(sa, self.b))

    def test_searchsortedOp_wrong_side_kwd(self):
        with pytest.raises(ValueError):
            searchsorted(self.x, self.v, side="asdfa")

    def test_searchsortedOp_on_no_1d_inp(self):
        no_1d = dmatrix("no_1d")
        with pytest.raises(ValueError):
            searchsorted(no_1d, self.v)
        with pytest.raises(ValueError):
            searchsorted(self.x, self.v, sorter=no_1d)

    def test_searchsortedOp_on_float_sorter(self):
        sorter = vector("sorter", dtype="float32")
        with pytest.raises(TypeError):
            searchsorted(self.x, self.v, sorter=sorter)

    def test_searchsortedOp_on_int_sorter(self):
        compatible_types = ("int8", "int16", "int32")
        if PYTHON_INT_BITWIDTH == 64:
            compatible_types += ("int64",)
        # 'uint8', 'uint16', 'uint32', 'uint64')
        for dtype in compatible_types:
            sorter = vector("sorter", dtype=dtype)
            f = aesara.function(
                [self.x, self.v, sorter],
                searchsorted(self.x, self.v, sorter=sorter),
                allow_input_downcast=True,
            )
            assert np.allclose(
                np.searchsorted(self.a, self.b, sorter=self.idx_sorted),
                f(self.a, self.b, self.idx_sorted),
            )

    def test_searchsortedOp_on_right_side(self):
        f = aesara.function(
            [self.x, self.v], searchsorted(self.x, self.v, side="right")
        )
        assert np.allclose(
            np.searchsorted(self.a, self.b, side="right"), f(self.a, self.b)
        )

    def test_infer_shape(self):
        # Test using default parameters' value
        self._compile_and_check(
            [self.x, self.v],
            [searchsorted(self.x, self.v)],
            [self.a[self.idx_sorted], self.b],
            self.op_class,
        )

        # Test parameter ``sorter``
        sorter = vector("sorter", dtype="int32")
        self._compile_and_check(
            [self.x, self.v, sorter],
            [searchsorted(self.x, self.v, sorter=sorter)],
            [self.a, self.b, self.idx_sorted],
            self.op_class,
        )

        # Test parameter ``side``
        la = np.ones(10).astype(config.floatX)
        lb = np.ones(shape=(1, 2, 3)).astype(config.floatX)
        self._compile_and_check(
            [self.x, self.v],
            [searchsorted(self.x, self.v, side="right")],
            [la, lb],
            self.op_class,
        )

    def test_grad(self):
        utt.verify_grad(self.op, [self.a[self.idx_sorted], self.b], rng=self.rng)


class TestCumOp(utt.InferShapeTester):
    def setup_method(self):
        super().setup_method()
        self.op_class = CumOp
        self.op = CumOp()

    def test_cum_op(self):
        x = tensor3("x")
        a = np.random.random((3, 5, 2)).astype(config.floatX)

        # Test axis out of bounds
        with pytest.raises(ValueError):
            cumsum(x, axis=3)
        with pytest.raises(ValueError):
            cumsum(x, axis=-4)
        with pytest.raises(ValueError):
            cumprod(x, axis=3)
        with pytest.raises(ValueError):
            cumprod(x, axis=-4)

        f = aesara.function([x], [cumsum(x), cumprod(x)])
        s, p = f(a)
        assert np.allclose(np.cumsum(a), s)  # Test axis=None
        assert np.allclose(np.cumprod(a), p)  # Test axis=None

        for axis in range(-len(a.shape), len(a.shape)):
            f = aesara.function([x], [cumsum(x, axis=axis), cumprod(x, axis=axis)])
            s, p = f(a)
            assert np.allclose(np.cumsum(a, axis=axis), s)
            assert np.allclose(np.cumprod(a, axis=axis), p)

    def test_infer_shape(self):
        x = tensor3("x")
        a = np.random.random((3, 5, 2)).astype(config.floatX)

        # Test axis=None
        self._compile_and_check([x], [self.op(x)], [a], self.op_class)

        for axis in range(-len(a.shape), len(a.shape)):
            self._compile_and_check([x], [cumsum(x, axis=axis)], [a], self.op_class)

    def test_grad(self):
        a = np.random.random((3, 5, 2)).astype(config.floatX)

        utt.verify_grad(self.op_class(mode="add"), [a])  # Test axis=None
        utt.verify_grad(self.op_class(mode="mul"), [a])  # Test axis=None

        for axis in range(-len(a.shape), len(a.shape)):
            utt.verify_grad(self.op_class(axis=axis, mode="add"), [a], eps=4e-4)
            utt.verify_grad(self.op_class(axis=axis, mode="mul"), [a], eps=4e-4)


class TestBinCount(utt.InferShapeTester):
    def test_bincountFn(self):
        w = vector("w")

        def ref(data, w=None, minlength=None):
            size = int(data.max() + 1)
            if minlength:
                size = max(size, minlength)
            if w is not None:
                out = np.zeros(size, dtype=w.dtype)
                for i in range(data.shape[0]):
                    out[data[i]] += w[i]
            else:
                out = np.zeros(size, dtype=a.dtype)
                for i in range(data.shape[0]):
                    out[data[i]] += 1
            return out

        for dtype in (
            "int8",
            "int16",
            "int32",
            "int64",
            "uint8",
            "uint16",
            "uint32",
            "uint64",
        ):
            x = vector("x", dtype=dtype)

            a = np.random.randint(1, 51, size=(25)).astype(dtype)
            weights = np.random.random((25,)).astype(config.floatX)

            f1 = aesara.function([x], bincount(x))
            f2 = aesara.function([x, w], bincount(x, weights=w))

            assert (ref(a) == f1(a)).all()
            assert np.allclose(ref(a, weights), f2(a, weights))
            f3 = aesara.function([x], bincount(x, minlength=55))
            f4 = aesara.function([x], bincount(x, minlength=5))
            assert (ref(a, minlength=55) == f3(a)).all()
            assert (ref(a, minlength=5) == f4(a)).all()
            # skip the following test when using unsigned ints
            if not dtype.startswith("u"):
                a[0] = -1
                f5 = aesara.function([x], bincount(x, assert_nonneg=True))
                with pytest.raises(AssertionError):
                    f5(a)


class TestDiffOp(utt.InferShapeTester):
    def test_diffOp(self):
        x = matrix("x")
        a = np.random.random((30, 50)).astype(config.floatX)

        f = aesara.function([x], diff(x))
        assert np.allclose(np.diff(a), f(a))

        for axis in (-2, -1, 0, 1):
            for n in (0, 1, 2, a.shape[0], a.shape[0] + 1):
                g = aesara.function([x], diff(x, n=n, axis=axis))
                assert np.allclose(np.diff(a, n=n, axis=axis), g(a))

    @pytest.mark.parametrize(
        "x_type",
        (
            at.TensorType("float64", (None, None)),
            at.TensorType("float64", (None, 30)),
            at.TensorType("float64", (10, None)),
            at.TensorType("float64", (10, 30)),
        ),
    )
    def test_output_type(self, x_type):
        x = x_type("x")
        x_test = np.empty((10, 30))
        for axis in (-2, -1, 0, 1):
            for n in (0, 1, 2, 10, 11):
                out = diff(x, n=n, axis=axis)
                out_test = np.diff(x_test, n=n, axis=axis)
                for i in range(2):
                    if x.type.shape[i] is None:
                        assert out.type.shape[i] is None
                    else:
                        assert out.type.shape[i] == out_test.shape[i]

    def test_infer_shape(self):
        x = matrix("x")
        a = np.random.random((30, 50)).astype(config.floatX)

        # Test default n and axis
        self._compile_and_check([x], [DiffOp()(x)], [a], DiffOp)

        for axis in (-2, -1, 0, 1):
            for n in (0, 1, 2, a.shape[0], a.shape[0] + 1):
                self._compile_and_check([x], [diff(x, n=n, axis=axis)], [a], DiffOp)

    def test_grad(self):
        a = np.random.random(50).astype(config.floatX)

        # Test default n and axis
        utt.verify_grad(DiffOp(), [a])

        for n in (0, 1, 2, a.shape[0]):
            utt.verify_grad(DiffOp(n=n), [a], eps=7e-3)

    @pytest.mark.xfail(reason="gradient is wrong when n is larger than input size")
    def test_grad_n_larger_than_input(self):
        # Gradient is wrong when n is larger than the input size. Until it is fixed,
        # this test ensures the behavior is documented
        a = np.random.random(10).astype(config.floatX)
        utt.verify_grad(DiffOp(n=11), [a], eps=7e-3)

    def test_grad_not_implemented(self):
        x = at.matrix("x")
        with pytest.raises(NotImplementedError):
            grad(diff(x).sum(), x)


class TestSqueeze(utt.InferShapeTester):
    shape_list = [(1, 3), (1, 2, 3), (1, 5, 1, 1, 6)]
    broadcast_list = [
        [True, False],
        [True, False, False],
        [True, False, True, True, False],
    ]

    def setup_method(self):
        super().setup_method()
        self.op = squeeze

    def test_op(self):
        for shape, broadcast in zip(self.shape_list, self.broadcast_list):
            data = np.random.random(size=shape).astype(config.floatX)
            variable = TensorType(config.floatX, broadcast)()

            f = aesara.function([variable], self.op(variable))

            expected = np.squeeze(data)
            tested = f(data)

            assert tested.shape == expected.shape
            assert np.allclose(tested, expected)

    def test_infer_shape(self):
        for shape, broadcast in zip(self.shape_list, self.broadcast_list):
            data = np.random.random(size=shape).astype(config.floatX)
            variable = TensorType(config.floatX, broadcast)()

            self._compile_and_check(
                [variable], [self.op(variable)], [data], DimShuffle, warn=False
            )

    def test_grad(self):
        for shape, broadcast in zip(self.shape_list, self.broadcast_list):
            data = np.random.random(size=shape).astype(config.floatX)

            utt.verify_grad(self.op, [data])

    def test_var_interface(self):
        # same as test_op, but use a_aesara_var.squeeze.
        for shape, broadcast in zip(self.shape_list, self.broadcast_list):
            data = np.random.random(size=shape).astype(config.floatX)
            variable = TensorType(config.floatX, broadcast)()

            f = aesara.function([variable], variable.squeeze())

            expected = np.squeeze(data)
            tested = f(data)

            assert tested.shape == expected.shape
            assert np.allclose(tested, expected)

    def test_axis(self):
        variable = TensorType(config.floatX, [False, True, False])()
        res = squeeze(variable, axis=1)

        assert res.broadcastable == (False, False)

        variable = TensorType(config.floatX, [False, True, False])()
        res = squeeze(variable, axis=(1,))

        assert res.broadcastable == (False, False)

        variable = TensorType(config.floatX, [False, True, False, True])()
        res = squeeze(variable, axis=(1, 3))

        assert res.broadcastable == (False, False)

        variable = TensorType(config.floatX, [True, False, True, False, True])()
        res = squeeze(variable, axis=(0, -1))

        assert res.broadcastable == (False, True, False)

    def test_invalid_axis(self):
        # Test that trying to squeeze a non broadcastable dimension raises error
        variable = TensorType(config.floatX, [True, False])()
        with pytest.raises(
            ValueError, match="Cannot drop a non-broadcastable dimension"
        ):
            squeeze(variable, axis=1)


class TestCompress(utt.InferShapeTester):
    axis_list = [None, -1, 0, 0, 0, 1]
    cond_list = [
        [1, 0, 1, 0, 0, 1],
        [0, 1, 1, 0],
        [0, 1, 1, 0],
        [],
        [0, 0, 0, 0],
        [1, 1, 0, 1, 0],
    ]
    shape_list = [(2, 3), (4, 3), (4, 3), (4, 3), (4, 3), (3, 5)]

    def setup_method(self):
        super().setup_method()
        self.op = compress

    def test_op(self):
        for axis, cond, shape in zip(self.axis_list, self.cond_list, self.shape_list):
            cond_var = ivector()
            data = np.random.random(size=shape).astype(config.floatX)
            data_var = matrix()

            f = aesara.function(
                [cond_var, data_var], self.op(cond_var, data_var, axis=axis)
            )

            expected = np.compress(cond, data, axis=axis)
            tested = f(cond, data)

            assert tested.shape == expected.shape
            assert np.allclose(tested, expected)


class TestRepeat(utt.InferShapeTester):
    def _possible_axis(self, ndim):
        return [None] + list(range(ndim)) + [-i for i in range(ndim)]

    def setup_method(self):
        super().setup_method()
        self.op_class = Repeat
        self.op = Repeat()
        # uint64 always fails
        # int64 and uint32 also fail if python int are 32-bit
        if LOCAL_BITWIDTH == 64:
            self.numpy_unsupported_dtypes = ("uint64",)
        if LOCAL_BITWIDTH == 32:
            self.numpy_unsupported_dtypes = ("uint32", "int64", "uint64")

    def test_basic(self):
        for ndim in [1, 3]:
            x = TensorType(config.floatX, [False] * ndim)()
            a = np.random.random((10,) * ndim).astype(config.floatX)

            for axis in self._possible_axis(ndim):
                for dtype in integer_dtypes:
                    r_var = scalar(dtype=dtype)
                    r = np.asarray(3, dtype=dtype)
                    if dtype == "uint64" or (
                        dtype in self.numpy_unsupported_dtypes and r_var.ndim == 1
                    ):
                        with pytest.raises(TypeError):
                            repeat(x, r_var, axis=axis)
                    else:
                        f = aesara.function([x, r_var], repeat(x, r_var, axis=axis))
                        assert np.allclose(np.repeat(a, r, axis=axis), f(a, r))

                        r_var = vector(dtype=dtype)
                        if axis is None:
                            r = np.random.randint(1, 6, size=a.size).astype(dtype)
                        else:
                            r = np.random.randint(1, 6, size=(10,)).astype(dtype)

                        if dtype in self.numpy_unsupported_dtypes and r_var.ndim == 1:
                            with pytest.raises(TypeError):
                                repeat(x, r_var, axis=axis)
                        else:
                            f = aesara.function([x, r_var], repeat(x, r_var, axis=axis))
                            assert np.allclose(np.repeat(a, r, axis=axis), f(a, r))

                        # check when r is a list of single integer, e.g. [3].
                        r = np.random.randint(1, 11, size=()).astype(dtype) + 2
                        f = aesara.function([x], repeat(x, [r], axis=axis))
                        assert np.allclose(np.repeat(a, r, axis=axis), f(a))
                        assert not np.any(
                            [
                                isinstance(n.op, Repeat)
                                for n in f.maker.fgraph.toposort()
                            ]
                        )

                        # check when r is  aesara tensortype that broadcastable is (True,)
                        r_var = TensorType(shape=(True,), dtype=dtype)()
                        r = np.random.randint(1, 6, size=(1,)).astype(dtype)
                        f = aesara.function([x, r_var], repeat(x, r_var, axis=axis))
                        assert np.allclose(np.repeat(a, r[0], axis=axis), f(a, r))
                        assert not np.any(
                            [
                                isinstance(n.op, Repeat)
                                for n in f.maker.fgraph.toposort()
                            ]
                        )

    @pytest.mark.slow
    def test_infer_shape(self):
        for ndim in [1, 3]:
            x = TensorType(config.floatX, [False] * ndim)()
            shp = (np.arange(ndim) + 1) * 3
            a = np.random.random(shp).astype(config.floatX)

            for axis in self._possible_axis(ndim):
                for dtype in ["int8", "uint8", "uint64"]:
                    r_var = scalar(dtype=dtype)
                    r = np.asarray(3, dtype=dtype)
                    if dtype in self.numpy_unsupported_dtypes:
                        r_var = vector(dtype=dtype)
                        with pytest.raises(TypeError):
                            repeat(x, r_var)
                    else:
                        self._compile_and_check(
                            [x, r_var],
                            [Repeat(axis=axis)(x, r_var)],
                            [a, r],
                            self.op_class,
                        )

                        r_var = vector(dtype=dtype)
                        if axis is None:
                            r = np.random.randint(1, 6, size=a.size).astype(dtype)
                        elif a.size > 0:
                            r = np.random.randint(1, 6, size=a.shape[axis]).astype(
                                dtype
                            )
                        else:
                            r = np.random.randint(1, 6, size=(10,)).astype(dtype)

                        self._compile_and_check(
                            [x, r_var],
                            [Repeat(axis=axis)(x, r_var)],
                            [a, r],
                            self.op_class,
                        )

    def test_grad(self):
        for ndim in range(3):
            a = np.random.random((10,) * ndim).astype(config.floatX)

            for axis in self._possible_axis(ndim):
                utt.verify_grad(lambda x: Repeat(axis=axis)(x, 3), [a])

    def test_broadcastable(self):
        x = TensorType(config.floatX, [False, True, False])()
        r = Repeat(axis=1)(x, 2)
        assert r.broadcastable == (False, False, False)
        r = Repeat(axis=1)(x, 1)
        assert r.broadcastable == (False, True, False)
        r = Repeat(axis=0)(x, 2)
        assert r.broadcastable == (False, True, False)


class TestBartlett(utt.InferShapeTester):
    def setup_method(self):
        super().setup_method()
        self.op_class = Bartlett
        self.op = bartlett

    def test_perform(self):
        x = lscalar()
        f = function([x], self.op(x))
        M = np.random.randint(3, 51, size=())
        assert np.allclose(f(M), np.bartlett(M))
        assert np.allclose(f(0), np.bartlett(0))
        assert np.allclose(f(-1), np.bartlett(-1))
        b = np.array([17], dtype="uint8")
        assert np.allclose(f(b[0]), np.bartlett(b[0]))

    def test_infer_shape(self):
        x = lscalar()
        self._compile_and_check(
            [x], [self.op(x)], [np.random.randint(3, 51, size=())], self.op_class
        )
        self._compile_and_check([x], [self.op(x)], [0], self.op_class)
        self._compile_and_check([x], [self.op(x)], [1], self.op_class)


class TestFillDiagonal(utt.InferShapeTester):

    rng = np.random.default_rng(43)

    def setup_method(self):
        super().setup_method()
        self.op_class = FillDiagonal
        self.op = fill_diagonal

    def test_perform(self):
        x = matrix()
        y = scalar()
        f = function([x, y], fill_diagonal(x, y))
        for shp in [(8, 8), (5, 8), (8, 5)]:
            a = np.random.random(shp).astype(config.floatX)
            val = np.cast[config.floatX](np.random.random())
            out = f(a, val)
            # We can't use np.fill_diagonal as it is bugged.
            assert np.allclose(np.diag(out), val)
            assert (out == val).sum() == min(a.shape)

        # test for 3dtt
        a = np.random.random((3, 3, 3)).astype(config.floatX)
        x = tensor3()
        y = scalar()
        f = function([x, y], fill_diagonal(x, y))
        val = np.cast[config.floatX](np.random.random() + 10)
        out = f(a, val)
        # We can't use np.fill_diagonal as it is bugged.
        assert out[0, 0, 0] == val
        assert out[1, 1, 1] == val
        assert out[2, 2, 2] == val
        assert (out == val).sum() == min(a.shape)

    @pytest.mark.slow
    def test_gradient(self):
        utt.verify_grad(
            fill_diagonal,
            [np.random.random((5, 8)), np.random.random()],
            n_tests=1,
            rng=TestFillDiagonal.rng,
        )
        utt.verify_grad(
            fill_diagonal,
            [np.random.random((8, 5)), np.random.random()],
            n_tests=1,
            rng=TestFillDiagonal.rng,
        )

    def test_infer_shape(self):
        z = dtensor3()
        x = dmatrix()
        y = dscalar()
        self._compile_and_check(
            [x, y],
            [self.op(x, y)],
            [np.random.random((8, 5)), np.random.random()],
            self.op_class,
        )
        self._compile_and_check(
            [z, y],
            [self.op(z, y)],
            # must be square when nd>2
            [np.random.random((8, 8, 8)), np.random.random()],
            self.op_class,
            warn=False,
        )


class TestFillDiagonalOffset(utt.InferShapeTester):

    rng = np.random.default_rng(43)

    def setup_method(self):
        super().setup_method()
        self.op_class = FillDiagonalOffset
        self.op = fill_diagonal_offset

    def test_perform(self):
        x = matrix()
        y = scalar()
        z = iscalar()

        f = function([x, y, z], fill_diagonal_offset(x, y, z))
        for test_offset in (-5, -4, -1, 0, 1, 4, 5):
            for shp in [(8, 8), (5, 8), (8, 5), (5, 5)]:
                a = np.random.random(shp).astype(config.floatX)
                val = np.cast[config.floatX](np.random.random())
                out = f(a, val, test_offset)
                # We can't use np.fill_diagonal as it is bugged.
                assert np.allclose(np.diag(out, test_offset), val)
                if test_offset >= 0:
                    assert (out == val).sum() == min(
                        min(a.shape), a.shape[1] - test_offset
                    )
                else:
                    assert (out == val).sum() == min(
                        min(a.shape), a.shape[0] + test_offset
                    )

    def test_gradient(self):
        for test_offset in (-5, -4, -1, 0, 1, 4, 5):
            # input 'offset' will not be tested
            def fill_diagonal_with_fix_offset(a, val):
                return fill_diagonal_offset(a, val, test_offset)

            utt.verify_grad(
                fill_diagonal_with_fix_offset,
                [np.random.random((5, 8)), np.random.random()],
                n_tests=1,
                rng=TestFillDiagonalOffset.rng,
            )
            utt.verify_grad(
                fill_diagonal_with_fix_offset,
                [np.random.random((8, 5)), np.random.random()],
                n_tests=1,
                rng=TestFillDiagonalOffset.rng,
            )
            utt.verify_grad(
                fill_diagonal_with_fix_offset,
                [np.random.random((5, 5)), np.random.random()],
                n_tests=1,
                rng=TestFillDiagonalOffset.rng,
            )

    def test_infer_shape(self):
        x = dmatrix()
        y = dscalar()
        z = iscalar()
        for test_offset in (-5, -4, -1, 0, 1, 4, 5):
            self._compile_and_check(
                [x, y, z],
                [self.op(x, y, z)],
                [np.random.random((8, 5)), np.random.random(), test_offset],
                self.op_class,
            )
            self._compile_and_check(
                [x, y, z],
                [self.op(x, y, z)],
                [np.random.random((5, 8)), np.random.random(), test_offset],
                self.op_class,
            )


def test_to_one_hot():
    v = ivector()
    o = to_one_hot(v, 10)
    f = aesara.function([v], o)
    out = f([1, 2, 3, 5, 6])
    assert out.dtype == config.floatX
    assert np.allclose(
        out,
        [
            [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            [0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0],
            [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0],
        ],
    )

    v = ivector()
    o = to_one_hot(v, 10, dtype="int32")
    f = aesara.function([v], o)
    out = f([1, 2, 3, 5, 6])
    assert out.dtype == "int32"
    assert np.allclose(
        out,
        [
            [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            [0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0],
            [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0],
        ],
    )


class TestUnique(utt.InferShapeTester):
    def setup_method(self):
        super().setup_method()
        self.op_params = [
            (False, False, False),
            (True, False, False),
            (False, True, False),
            (True, True, False),
            (False, False, True),
            (True, False, True),
            (False, True, True),
            (True, True, True),
        ]

    @pytest.mark.parametrize(
        ("x", "inp", "axis"),
        [
            (vector(), np.asarray([2, 1, 3, 2], dtype=config.floatX), None),
            (matrix(), np.asarray([[2, 1], [3, 2], [2, 1]], dtype=config.floatX), None),
            (vector(), np.asarray([2, 1, 3, 2], dtype=config.floatX), 0),
            (matrix(), np.asarray([[2, 1], [3, 2], [2, 1]], dtype=config.floatX), 0),
            (vector(), np.asarray([2, 1, 3, 2], dtype=config.floatX), -1),
            (matrix(), np.asarray([[2, 1], [3, 2], [2, 1]], dtype=config.floatX), -1),
        ],
    )
    def test_basic_vector(self, x, inp, axis):
        list_outs_expected = [
            np.unique(inp, axis=axis),
            np.unique(inp, True, axis=axis),
            np.unique(inp, False, True, axis=axis),
            np.unique(inp, True, True, axis=axis),
            np.unique(inp, False, False, True, axis=axis),
            np.unique(inp, True, False, True, axis=axis),
            np.unique(inp, False, True, True, axis=axis),
            np.unique(inp, True, True, True, axis=axis),
        ]
        for params, outs_expected in zip(self.op_params, list_outs_expected):
            out = at.unique(x, *params, axis=axis)
            f = aesara.function(inputs=[x], outputs=out)
            outs = f(inp)
            for out, out_exp in zip(outs, outs_expected):
                utt.assert_allclose(out, out_exp)

    @pytest.mark.parametrize(
        ("x", "inp", "axis"),
        [
            (vector(), np.asarray([2, 1, 3, 2], dtype=config.floatX), None),
            (matrix(), np.asarray([[2, 1], [3, 2], [2, 1]], dtype=config.floatX), None),
            (vector(), np.asarray([2, 1, 3, 2], dtype=config.floatX), 0),
            (matrix(), np.asarray([[2, 1], [3, 2], [2, 1]], dtype=config.floatX), 0),
            (vector(), np.asarray([2, 1, 3, 2], dtype=config.floatX), -1),
            (matrix(), np.asarray([[2, 1], [3, 2], [2, 1]], dtype=config.floatX), -1),
        ],
    )
    def test_infer_shape(self, x, inp, axis):
        for params in self.op_params:
            if not params[1]:
                continue
            if params[0]:
                f = at.unique(x, *params, axis=axis)[2]
            else:
                f = at.unique(x, *params, axis=axis)[1]
            self._compile_and_check(
                [x],
                [f],
                [inp],
                Unique,
            )


class TestUnravelIndex(utt.InferShapeTester):
    def test_unravel_index(self):
        def check(shape, index_ndim, order):
            indices = np.arange(np.product(shape))
            # test with scalars and higher-dimensional indices
            if index_ndim == 0:
                indices = indices[-1]
            elif index_ndim == 2:
                indices = indices[:, np.newaxis]
            indices_symb = aesara.shared(indices)

            # reference result
            ref = np.unravel_index(indices, shape, order=order)

            def fn(i, d):
                return function([], unravel_index(i, d, order=order))

            # shape given as a tuple
            f_array_tuple = fn(indices, shape)
            f_symb_tuple = fn(indices_symb, shape)
            np.testing.assert_equal(ref, f_array_tuple())
            np.testing.assert_equal(ref, f_symb_tuple())

            # shape given as an array
            shape_array = np.array(shape)
            f_array_array = fn(indices, shape_array)
            np.testing.assert_equal(ref, f_array_array())

            # shape given as an Aesara variable
            shape_symb = aesara.shared(shape_array)
            f_array_symb = fn(indices, shape_symb)
            np.testing.assert_equal(ref, f_array_symb())

            # shape given as a Shape op (unravel_index will use get_vector_length
            # to infer the number of dimensions)
            indexed_array = aesara.shared(np.random.uniform(size=shape_array))
            f_array_shape = fn(indices, indexed_array.shape)
            np.testing.assert_equal(ref, f_array_shape())

            # shape testing
            self._compile_and_check(
                [],
                unravel_index(indices, shape_symb, order=order),
                [],
                UnravelIndex,
            )

        for order in ("C", "F"):
            for index_ndim in (0, 1, 2):
                check((3,), index_ndim, order)
                check((3, 4), index_ndim, order)
                check((3, 4, 5), index_ndim, order)

        # must specify ndim if length of dims is not fixed
        with pytest.raises(ValueError):
            unravel_index(ivector(), ivector())

        # must provide integers
        with pytest.raises(TypeError):
            unravel_index(fvector(), (3, 4))
        with pytest.raises(TypeError):
            unravel_index((3, 4), (3.4, 3.2))

        # dims must be a 1D sequence
        with pytest.raises(TypeError):
            unravel_index((3, 4), 3)
        with pytest.raises(TypeError):
            unravel_index((3, 4), ((3, 4),))


class TestRavelMultiIndex(utt.InferShapeTester):
    def test_ravel_multi_index(self):
        def check(shape, index_ndim, mode, order):
            multi_index = np.unravel_index(
                np.arange(np.product(shape)), shape, order=order
            )
            # create some invalid indices to test the mode
            if mode in ("wrap", "clip"):
                multi_index = (multi_index[0] - 1,) + multi_index[1:]
            # test with scalars and higher-dimensional indices
            if index_ndim == 0:
                multi_index = tuple(i[-1] for i in multi_index)
            elif index_ndim == 2:
                multi_index = tuple(i[:, np.newaxis] for i in multi_index)
            multi_index_symb = [aesara.shared(i) for i in multi_index]

            # reference result
            ref = np.ravel_multi_index(multi_index, shape, mode, order)

            def fn(mi, s):
                return function([], ravel_multi_index(mi, s, mode, order))

            # shape given as a tuple
            f_array_tuple = fn(multi_index, shape)
            f_symb_tuple = fn(multi_index_symb, shape)
            np.testing.assert_equal(ref, f_array_tuple())
            np.testing.assert_equal(ref, f_symb_tuple())

            # shape given as an array
            shape_array = np.array(shape)
            f_array_array = fn(multi_index, shape_array)
            np.testing.assert_equal(ref, f_array_array())

            # shape given as an Aesara variable
            shape_symb = aesara.shared(shape_array)
            f_array_symb = fn(multi_index, shape_symb)
            np.testing.assert_equal(ref, f_array_symb())

            # shape testing
            self._compile_and_check(
                [],
                [ravel_multi_index(multi_index, shape_symb, mode, order)],
                [],
                RavelMultiIndex,
            )

        for mode in ("raise", "wrap", "clip"):
            for order in ("C", "F"):
                for index_ndim in (0, 1, 2):
                    check((3,), index_ndim, mode, order)
                    check((3, 4), index_ndim, mode, order)
                    check((3, 4, 5), index_ndim, mode, order)

        # must provide integers
        with pytest.raises(TypeError):
            ravel_multi_index((fvector(), ivector()), (3, 4))
        with pytest.raises(TypeError):
            ravel_multi_index(((3, 4), ivector()), (3.4, 3.2))

        # dims must be a 1D sequence
        with pytest.raises(TypeError):
            ravel_multi_index(((3, 4),), ((3, 4),))


def test_broadcast_shape_basic():
    def shape_tuple(x, use_bcast=True):
        if use_bcast:
            return tuple(
                s if not bcast else 1
                for s, bcast in zip(tuple(x.shape), x.broadcastable)
            )
        else:
            return tuple(s for s in tuple(x.shape))

    x = np.array([[1], [2], [3]])
    y = np.array([4, 5, 6])
    b = np.broadcast(x, y)
    x_at = at.as_tensor_variable(x)
    y_at = at.as_tensor_variable(y)
    b_at = broadcast_shape(x_at, y_at)
    assert np.array_equal([z.eval() for z in b_at], b.shape)
    # Now, we try again using shapes as the inputs
    #
    # This case also confirms that a broadcast dimension will
    # broadcast against a non-broadcast dimension when they're
    # both symbolic (i.e. we couldn't obtain constant values).
    b_at = broadcast_shape(
        shape_tuple(x_at, use_bcast=False),
        shape_tuple(y_at, use_bcast=False),
        arrays_are_shapes=True,
    )
    assert any(
        isinstance(node.op, Assert) for node in applys_between([x_at, y_at], b_at)
    )
    assert np.array_equal([z.eval() for z in b_at], b.shape)
    b_at = broadcast_shape(shape_tuple(x_at), shape_tuple(y_at), arrays_are_shapes=True)
    assert np.array_equal([z.eval() for z in b_at], b.shape)

    x = np.array([1, 2, 3])
    y = np.array([4, 5, 6])
    b = np.broadcast(x, y)
    x_at = at.as_tensor_variable(x)
    y_at = at.as_tensor_variable(y)
    b_at = broadcast_shape(x_at, y_at)
    assert np.array_equal([z.eval() for z in b_at], b.shape)
    b_at = broadcast_shape(shape_tuple(x_at), shape_tuple(y_at), arrays_are_shapes=True)
    assert np.array_equal([z.eval() for z in b_at], b.shape)

    x = np.empty((1, 2, 3))
    y = np.array(1)
    b = np.broadcast(x, y)
    x_at = at.as_tensor_variable(x)
    y_at = at.as_tensor_variable(y)
    b_at = broadcast_shape(x_at, y_at)
    assert b_at[0].value == 1
    assert np.array_equal([z.eval() for z in b_at], b.shape)
    b_at = broadcast_shape(shape_tuple(x_at), shape_tuple(y_at), arrays_are_shapes=True)
    assert np.array_equal([z.eval() for z in b_at], b.shape)

    x = np.empty((2, 1, 3))
    y = np.empty((2, 1, 1))
    b = np.broadcast(x, y)
    x_at = at.as_tensor_variable(x)
    y_at = at.as_tensor_variable(y)
    b_at = broadcast_shape(x_at, y_at)
    assert b_at[1].value == 1
    assert np.array_equal([z.eval() for z in b_at], b.shape)
    b_at = broadcast_shape(shape_tuple(x_at), shape_tuple(y_at), arrays_are_shapes=True)
    assert np.array_equal([z.eval() for z in b_at], b.shape)

    x1_shp_at = iscalar("x1")
    x2_shp_at = iscalar("x2")
    y1_shp_at = iscalar("y1")
    x_shapes = (1, x1_shp_at, x2_shp_at)
    x_at = at.ones(x_shapes)
    y_shapes = (y1_shp_at, 1, x2_shp_at)
    y_at = at.ones(y_shapes)
    b_at = broadcast_shape(x_at, y_at)
    res = at.as_tensor(b_at).eval(
        {
            x1_shp_at: 10,
            x2_shp_at: 4,
            y1_shp_at: 2,
        }
    )
    assert np.array_equal(res, (2, 10, 4))

    y_shapes = (y1_shp_at, 1, y1_shp_at)
    y_at = at.ones(y_shapes)
    b_at = broadcast_shape(x_at, y_at)
    assert isinstance(b_at[-1].owner.op, Assert)


@pytest.mark.parametrize(
    ("s1_vals", "s2_vals", "exp_res"),
    [
        ((2, 2), (1, 2), (2, 2)),
        ((0, 2), (1, 2), (0, 2)),
        ((1, 2, 1), (2, 1, 2, 1), (2, 1, 2, 1)),
    ],
)
def test_broadcast_shape_symbolic(s1_vals, s2_vals, exp_res):
    s1s = at.lscalars(len(s1_vals))
    eval_point = {}
    for s, s_val in zip(s1s, s1_vals):
        eval_point[s] = s_val
        s.tag.test_value = s_val

    s2s = at.lscalars(len(s2_vals))
    for s, s_val in zip(s2s, s2_vals):
        eval_point[s] = s_val
        s.tag.test_value = s_val

    res = broadcast_shape(s1s, s2s, arrays_are_shapes=True)
    res = at.as_tensor(res)

    assert tuple(res.eval(eval_point)) == exp_res


class TestBroadcastTo(utt.InferShapeTester):

    rng = np.random.default_rng(43)

    def setup_method(self):
        super().setup_method()
        self.op_class = BroadcastTo
        self.op = broadcast_to

    def test_avoid_useless_scalars(self):
        x = scalar()
        y = broadcast_to(x, ())
        assert y is x

    def test_avoid_useless_subtensors(self):
        x = scalar()
        y = broadcast_to(x, (1, 2))
        # There shouldn't be any unnecessary `Subtensor` operations
        # (e.g. from `at.as_tensor((1, 2))[0]`)
        assert y.owner.inputs[1].owner is None
        assert y.owner.inputs[2].owner is None

    @config.change_flags(compute_test_value="raise")
    def test_perform(self):
        a = scalar()
        a.tag.test_value = 5

        s_1 = iscalar("s_1")
        s_1.tag.test_value = 4
        shape = (s_1, 1)

        bcast_res = broadcast_to(a, shape)

        assert bcast_res.broadcastable == (False, True)

        bcast_np = np.broadcast_to(5, (4, 1))
        bcast_at = bcast_res.get_test_value()

        assert np.array_equal(bcast_at, bcast_np)
        assert np.shares_memory(bcast_at, a.get_test_value())

    @pytest.mark.parametrize(
        "fn,input_dims",
        [
            [lambda x: broadcast_to(x, (1,)), (1,)],
            [lambda x: broadcast_to(x, (6, 2, 5, 3)), (1,)],
            [lambda x: broadcast_to(x, (6, 2, 5, 3)), (5, 1)],
            [lambda x: broadcast_to(x, (6, 2, 1, 3)), (2, 1, 3)],
        ],
    )
    def test_gradient(self, fn, input_dims):
        utt.verify_grad(
            fn,
            [np.random.random(input_dims).astype(config.floatX)],
            n_tests=1,
            rng=self.rng,
        )

    def test_infer_shape(self):
        a = tensor(config.floatX, [False, True, False])
        shape = list(a.shape)
        out = self.op(a, shape)

        self._compile_and_check(
            [a] + shape,
            [out],
            [np.random.random((2, 1, 3)).astype(config.floatX), 2, 1, 3],
            self.op_class,
        )

        a = tensor(config.floatX, [False, True, False])
        shape = [iscalar() for i in range(4)]
        self._compile_and_check(
            [a] + shape,
            [self.op(a, shape)],
            [np.random.random((2, 1, 3)).astype(config.floatX), 6, 2, 5, 3],
            self.op_class,
        )

    def test_inplace(self):
        """Make sure that in-place optimizations are *not* performed on the output of a ``BroadcastTo``."""
        a = at.zeros((5,))
        d = at.vector("d")
        c = at.set_subtensor(a[np.r_[0, 1, 3]], d)
        b = broadcast_to(c, (5,))
        q = b[np.r_[0, 1, 3]]
        e = at.set_subtensor(q, np.r_[0, 0, 0])

        opts = OptimizationQuery(include=["inplace"])
        py_mode = Mode("py", opts)
        e_fn = function([d], e, mode=py_mode)

        advincsub_node = e_fn.maker.fgraph.outputs[0].owner
        assert isinstance(advincsub_node.op, AdvancedIncSubtensor)
        assert isinstance(advincsub_node.inputs[0].owner.op, BroadcastTo)

        assert advincsub_node.op.inplace is False


def test_broadcast_arrays():
    x, y = at.dvector(), at.dmatrix()
    x_bcast, y_bcast = broadcast_arrays(x, y)

    py_mode = Mode("py", None)
    bcast_fn = function([x, y], [x_bcast, y_bcast], mode=py_mode)

    x_val = np.array([1.0], dtype=np.float64)
    y_val = np.array([[1.0, 2.0], [3.0, 4.0]], dtype=np.float64)
    x_bcast_val, y_bcast_val = bcast_fn(x_val, y_val)
    x_bcast_exp, y_bcast_exp = np.broadcast_arrays(x_val, y_val)

    assert np.array_equal(x_bcast_val, x_bcast_exp)
    assert np.array_equal(y_bcast_val, y_bcast_exp)


@pytest.mark.parametrize(
    "start, stop, num_samples",
    [
        (1, 10, 50),
        (np.array([5, 6]), np.array([[10, 10], [10, 10]]), 25),
        (1, np.array([5, 6]), 30),
    ],
)
def test_space_ops(start, stop, num_samples):
    z = linspace(start, stop, num_samples)
    aesara_res = function(inputs=[], outputs=z)()
    numpy_res = np.linspace(start, stop, num=num_samples)
    assert np.allclose(aesara_res, numpy_res)

    z = logspace(start, stop, num_samples)
    aesara_res = function(inputs=[], outputs=z)()
    numpy_res = np.logspace(start, stop, num=num_samples)
    assert np.allclose(aesara_res, numpy_res)

    z = geomspace(start, stop, num_samples)
    aesara_res = function(inputs=[], outputs=z)()
    numpy_res = np.geomspace(start, stop, num=num_samples)
    assert np.allclose(aesara_res, numpy_res)
