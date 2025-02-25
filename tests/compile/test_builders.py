from functools import partial

import numpy as np
import pytest

from aesara.compile import shared
from aesara.compile.builders import OpFromGraph
from aesara.compile.function import function
from aesara.configdefaults import config
from aesara.gradient import DisconnectedType, Rop, disconnected_type, grad
from aesara.graph.fg import FunctionGraph
from aesara.graph.null_type import NullType
from aesara.graph.opt_utils import optimize_graph
from aesara.printing import debugprint
from aesara.tensor.basic import as_tensor
from aesara.tensor.basic_opt import ShapeOptimizer
from aesara.tensor.math import dot, exp
from aesara.tensor.math import round as at_round
from aesara.tensor.math import sigmoid
from aesara.tensor.math import sum as at_sum
from aesara.tensor.random.utils import RandomStream
from aesara.tensor.shape import specify_shape
from aesara.tensor.type import TensorType, matrices, matrix, scalar, vector, vectors
from tests import unittest_tools
from tests.graph.utils import MyVariable


class TestOpFromGraph(unittest_tools.InferShapeTester):
    def test_valid_input(self):
        x, y, z = matrices("xyz")

        with pytest.raises(TypeError):
            OpFromGraph((x,), (x,))

        with pytest.raises(TypeError):
            OpFromGraph([1], [1])

        with pytest.raises(TypeError):
            OpFromGraph([x, as_tensor(1)], [x])

        with pytest.raises(NotImplementedError):
            OpFromGraph([x], [x], updates={})

    @pytest.mark.parametrize(
        "cls_ofg", [OpFromGraph, partial(OpFromGraph, inline=True)]
    )
    def test_straightforward(self, cls_ofg):
        x, y, z = matrices("xyz")
        e = x + y * z
        op = cls_ofg([x, y, z], [e])
        # (1+3*5=array of 16) - (3+1*5=array of 8)
        f = op(x, y, z) - op(y, z, x)

        fn = function([x, y, z], f)
        xv = np.ones((2, 2), dtype=config.floatX)
        yv = np.ones((2, 2), dtype=config.floatX) * 3
        zv = np.ones((2, 2), dtype=config.floatX) * 5
        assert np.all(8.0 == fn(xv, yv, zv))
        assert np.all(8.0 == fn(xv, yv, zv))

    @pytest.mark.parametrize(
        "cls_ofg", [OpFromGraph, partial(OpFromGraph, inline=True)]
    )
    def test_size_changes(self, cls_ofg):
        x, y, z = matrices("xyz")
        e = dot(x, y)
        op = cls_ofg([x, y], [e])
        f = op(x, op(y, z))
        fn = function([x, y, z], f)
        xv = np.ones((2, 3), dtype=config.floatX)
        yv = np.ones((3, 4), dtype=config.floatX) * 3
        zv = np.ones((4, 5), dtype=config.floatX) * 5
        res = fn(xv, yv, zv)
        assert res.shape == (2, 5)
        assert np.all(180.0 == res)
        res = fn(xv, yv, zv)
        assert res.shape == (2, 5)
        assert np.all(180.0 == res)

    @pytest.mark.parametrize(
        "cls_ofg", [OpFromGraph, partial(OpFromGraph, inline=True)]
    )
    def test_grad(self, cls_ofg):
        x, y, z = matrices("xyz")
        e = x + y * z
        op = cls_ofg([x, y, z], [e])
        f = op(x, y, z)
        f = f - grad(at_sum(f), y)
        fn = function([x, y, z], f)
        xv = np.ones((2, 2), dtype=config.floatX)
        yv = np.ones((2, 2), dtype=config.floatX) * 3
        zv = np.ones((2, 2), dtype=config.floatX) * 5
        assert np.all(11.0 == fn(xv, yv, zv))

    @pytest.mark.parametrize(
        "cls_ofg", [OpFromGraph, partial(OpFromGraph, inline=True)]
    )
    def test_grad_grad(self, cls_ofg):
        x, y, z = matrices("xyz")
        e = x + y * z
        op = cls_ofg([x, y, z], [e])
        f = op(x, y, z)
        f = f - grad(at_sum(f), y)
        f = f - grad(at_sum(f), y)
        fn = function([x, y, z], f)
        xv = np.ones((2, 2), dtype=config.floatX)
        yv = np.ones((2, 2), dtype=config.floatX) * 3
        zv = np.ones((2, 2), dtype=config.floatX) * 5
        np.testing.assert_array_almost_equal(6.0, fn(xv, yv, zv), 4)

    @pytest.mark.parametrize(
        "cls_ofg", [OpFromGraph, partial(OpFromGraph, inline=True)]
    )
    def test_shared(self, cls_ofg):
        x, y, z = matrices("xyz")
        s = shared(np.random.rand(2, 2).astype(config.floatX))
        e = x + y * z + s
        op = cls_ofg([x, y, z], [e])
        # (1+3*5=array of 16) - (3+1*5=array of 8)
        f = op(x, y, z) - op(y, z, x)

        fn = function([x, y, z], f)
        xv = np.ones((2, 2), dtype=config.floatX)
        yv = np.ones((2, 2), dtype=config.floatX) * 3
        zv = np.ones((2, 2), dtype=config.floatX) * 5
        # print function, function.__module__
        # print fn.maker.fgraph.toposort()
        np.testing.assert_array_almost_equal(8.0, fn(xv, yv, zv), 4)
        np.testing.assert_array_almost_equal(8.0, fn(xv, yv, zv), 4)

    @pytest.mark.parametrize(
        "cls_ofg", [OpFromGraph, partial(OpFromGraph, inline=True)]
    )
    def test_shared_grad(self, cls_ofg):
        x, y, z = matrices("xyz")
        s = shared(np.random.rand(2, 2).astype(config.floatX))
        e = x + y * z + s
        op = cls_ofg([x, y, z], [e])
        f = op(x, y, z)
        f = f - grad(at_sum(f), y)
        fn = function([x, y, z], f)
        xv = np.ones((2, 2), dtype=config.floatX)
        yv = np.ones((2, 2), dtype=config.floatX) * 3
        zv = np.ones((2, 2), dtype=config.floatX) * 5
        np.testing.assert_array_almost_equal(11.0 + s.get_value(), fn(xv, yv, zv), 4)

        # grad again the shared variable
        f = op(x, y, z)
        f = f - grad(at_sum(f), s)
        fn = function([x, y, z], f)
        np.testing.assert_array_almost_equal(15.0 + s.get_value(), fn(xv, yv, zv), 4)

    @pytest.mark.parametrize(
        "cls_ofg", [OpFromGraph, partial(OpFromGraph, inline=True)]
    )
    def test_grad_override(self, cls_ofg):
        x, y = vectors("xy")

        def go(inps, gs):
            x, y = inps
            (g,) = gs
            return [g * y * 2, g * x * 1.5]

        dedz = vector("dedz")
        op_mul_grad = cls_ofg([x, y, dedz], go([x, y], [dedz]))

        op_mul = cls_ofg([x, y], [x * y], grad_overrides=go)
        op_mul2 = cls_ofg([x, y], [x * y], grad_overrides=op_mul_grad)

        # single override case (function or OfG instance)
        xx, yy = vector("xx"), vector("yy")
        for op in [op_mul, op_mul2]:
            zz = at_sum(op(xx, yy))
            dx, dy = grad(zz, [xx, yy])
            fn = function([xx, yy], [dx, dy])
            xv = np.random.rand(16).astype(config.floatX)
            yv = np.random.rand(16).astype(config.floatX)
            dxv, dyv = fn(xv, yv)
            np.testing.assert_array_almost_equal(yv * 2, dxv, 4)
            np.testing.assert_array_almost_equal(xv * 1.5, dyv, 4)

        # list override case
        def go1(inps, gs):
            x, w, b = inps
            g = gs[0]
            return g * w * 2

        def go2(inps, gs):
            x, w, b = inps
            g = gs[0]
            return g * x * 1.5

        w, b = vectors("wb")
        # we make the 3rd gradient default (no override)
        op_linear = cls_ofg(
            [x, w, b], [x * w + b], grad_overrides=[go1, go2, "default"]
        )
        xx, ww, bb = vector("xx"), vector("yy"), vector("bb")
        zz = at_sum(op_linear(xx, ww, bb))
        dx, dw, db = grad(zz, [xx, ww, bb])
        fn = function([xx, ww, bb], [dx, dw, db])
        xv = np.random.rand(16).astype(config.floatX)
        wv = np.random.rand(16).astype(config.floatX)
        bv = np.random.rand(16).astype(config.floatX)
        dxv, dwv, dbv = fn(xv, wv, bv)
        np.testing.assert_array_almost_equal(wv * 2, dxv, 4)
        np.testing.assert_array_almost_equal(xv * 1.5, dwv, 4)
        np.testing.assert_array_almost_equal(np.ones(16, dtype=config.floatX), dbv, 4)

        # NullType and DisconnectedType
        op_linear2 = cls_ofg(
            [x, w, b],
            [x * w + b],
            grad_overrides=[go1, NullType()(), DisconnectedType()()],
        )
        zz2 = at_sum(op_linear2(xx, ww, bb))
        dx2, dw2, db2 = grad(
            zz2,
            [xx, ww, bb],
            return_disconnected="Disconnected",
            disconnected_inputs="ignore",
            null_gradients="return",
        )
        assert isinstance(dx2.type, TensorType)
        assert dx2.ndim == 1
        assert isinstance(dw2.type, NullType)
        assert isinstance(db2.type, DisconnectedType)

    @pytest.mark.parametrize(
        "cls_ofg", [OpFromGraph, partial(OpFromGraph, inline=True)]
    )
    def test_lop_override(self, cls_ofg):
        x = vector()
        y = 1.0 / (1.0 + exp(-x))

        def lop_ov(inps, outs, grads):
            (y_,) = outs
            (dedy_,) = grads
            return [2.0 * y_ * (1.0 - y_) * dedy_]

        y_, dedy = vector(), vector()
        op_lop_ov = cls_ofg([x, y_, dedy], [2.0 * y_ * (1.0 - y_) * dedy])

        xx = vector()
        yy1 = at_sum(sigmoid(xx))
        gyy1 = 2.0 * grad(yy1, xx)

        for ov in [lop_ov, op_lop_ov]:
            op = cls_ofg([x], [y], lop_overrides=ov)
            yy2 = at_sum(op(xx))
            gyy2 = grad(yy2, xx)
            fn = function([xx], [gyy1, gyy2])

            xval = np.random.rand(32).astype(config.floatX)
            y1val, y2val = fn(xval)
            np.testing.assert_array_almost_equal(y1val, y2val, 4)

    @pytest.mark.parametrize(
        "cls_ofg", [OpFromGraph, partial(OpFromGraph, inline=True)]
    )
    def test_rop(self, cls_ofg):
        a = vector()
        M = matrix()
        b = dot(a, M)
        op_matmul = cls_ofg([a, M], [b])
        x = vector()
        W = matrix()
        y = op_matmul(x, W)
        du = vector()
        dv = Rop(y, x, du)
        fn = function([x, W, du], dv)
        xval = np.random.rand(16).astype(config.floatX)
        Wval = np.random.rand(16, 16).astype(config.floatX)
        duval = np.random.rand(16).astype(config.floatX)
        dvval = np.dot(duval, Wval)
        dvval2 = fn(xval, Wval, duval)
        np.testing.assert_array_almost_equal(dvval2, dvval, 4)

    @pytest.mark.parametrize(
        "cls_ofg", [OpFromGraph, partial(OpFromGraph, inline=True)]
    )
    def test_rop_override(self, cls_ofg):
        x, y = vectors("xy")

        def ro(inps, epts):
            x, y = inps
            u, v = epts
            return [u * y * 2.0 + x * v * 1.5]

        u, v = vectors("uv")
        op_mul_rop = cls_ofg([x, y, u, v], ro([x, y], [u, v]))
        op_mul = cls_ofg([x, y], [x * y], rop_overrides=ro)
        op_mul2 = cls_ofg([x, y], [x * y], rop_overrides=op_mul_rop)

        # single override case
        xx, yy = vector("xx"), vector("yy")
        du, dv = vector("du"), vector("dv")
        for op in [op_mul, op_mul2]:
            zz = op_mul(xx, yy)
            dw = Rop(zz, [xx, yy], [du, dv])
            fn = function([xx, yy, du, dv], dw)
            vals = np.random.rand(4, 32).astype(config.floatX)
            dwval = fn(*vals)
            np.testing.assert_array_almost_equal(
                dwval, vals[0] * vals[3] * 1.5 + vals[1] * vals[2] * 2.0, 4
            )

        # TODO list override case

    @pytest.mark.parametrize(
        "cls_ofg", [OpFromGraph, partial(OpFromGraph, inline=True)]
    )
    def test_connection_pattern_override(self, cls_ofg):
        x, y = vectors("xy")

        def f1(x, y):
            del x
            # but we know how to backpropagate for x for some reasons
            # and we don't care about the gradient wrt y.
            return y + at_round(y)

        def f1_back(inputs, output_gradients):
            return [output_gradients[0], disconnected_type()]

        op = cls_ofg(
            inputs=[x, y],
            outputs=[f1(x, y)],
            grad_overrides=f1_back,
            connection_pattern=[[True], [False]],  # This is new
            on_unused_input="ignore",
        )  # This is new

        c = op(x, y)

        g1 = grad(c.sum(), x)

        out = g1.eval(
            {x: np.ones((5,), dtype=np.float32), y: np.ones((5,), dtype=np.float32)}
        )
        np.testing.assert_array_almost_equal(out, [1.0] * 5, 4)

    @pytest.mark.parametrize(
        "cls_ofg", [OpFromGraph, partial(OpFromGraph, inline=True)]
    )
    def test_nested(self, cls_ofg):
        x, y = vectors("xy")
        u, v = x + y, x - y
        op_ft = cls_ofg([x, y], [u, v])
        op_ift = cls_ofg([x, y], [u / 2, v / 2])

        xx, yy = vector("xx"), vector("yy")
        xx2, yy2 = op_ift(*op_ft(xx, yy))
        fn = function([xx, yy], [xx2, yy2])

        xv = np.random.rand(16).astype(config.floatX)
        yv = np.random.rand(16).astype(config.floatX)
        xv2, yv2 = fn(xv, yv)
        np.testing.assert_array_almost_equal(xv, xv2, 4)
        np.testing.assert_array_almost_equal(yv, yv2, 4)

    @pytest.mark.parametrize(
        "cls_ofg", [OpFromGraph, partial(OpFromGraph, inline=True)]
    )
    def test_connection_pattern(self, cls_ofg):
        # Basic case
        x, y, z = matrices("xyz")
        out1 = x * y
        out2 = y * z

        op1 = cls_ofg([x, y, z], [out1, out2])
        results = op1.connection_pattern(None)
        expect_result = [[True, False], [True, True], [False, True]]
        assert results == expect_result

        # Graph with ops that don't have a 'full' connection pattern
        # and with ops that have multiple outputs
        m, n, p, q = matrices("mnpq")
        o1, o2 = op1(m, n, p)
        out1, out2 = op1(o1, q, o2)
        op2 = cls_ofg([m, n, p, q], [out1, out2])

        results = op2.connection_pattern(None)
        expect_result = [[True, False], [True, True], [False, True], [True, True]]
        assert results == expect_result

        # Inner graph where some computation doesn't rely on explicit inputs
        srng = RandomStream(seed=234)
        rv_u = srng.uniform((2, 2))
        x, y = matrices("xy")
        out1 = x + rv_u
        out2 = y + 3
        out3 = 3 + rv_u
        op3 = cls_ofg([x, y], [out1, out2, out3])

        results = op3.connection_pattern(None)
        expect_result = [
            [True, False, False],
            [False, True, False],
            [True, False, True],
        ]
        assert results == expect_result

    def test_infer_shape(self):
        # test infer shape does not need to against inline case
        # since the Op is remove during optimization phase
        x = matrix("x")
        y = matrix("y")
        o1 = x + y
        o2 = x * y
        op_graph = OpFromGraph([x, y], [o1, o2])

        q = matrix("q")
        p = matrix("p")
        self._compile_and_check(
            [q, p],
            op_graph(q, p),
            [
                np.ones([3, 4], dtype=config.floatX),
                np.ones([3, 4], dtype=config.floatX),
            ],
            OpFromGraph,
        )

        # Make sure `OpFromGraph.infer_shape` can handle objects without a
        # shape
        x = MyVariable("x")
        y = matrix("y")
        z = specify_shape(vector("z"), (2,))

        op_graph = OpFromGraph([x, y, z], [x, y])

        op_var = op_graph(x, y, z)

        fg = FunctionGraph(outputs=[op_var[1]], clone=False)
        opt_res = optimize_graph(fg, custom_opt=ShapeOptimizer())

        assert opt_res.shape_feature.shape_of[x] is None
        assert opt_res.shape_feature.shape_of[z][0].data == 2

    @config.change_flags(compute_test_value="raise")
    def test_compute_test_value(self):
        x = scalar("x")
        x.tag.test_value = np.array(1.0, dtype=config.floatX)
        op = OpFromGraph([x], [x ** 3])
        y = scalar("y")
        y.tag.test_value = np.array(1.0, dtype=config.floatX)
        f = op(y)
        grad_f = grad(f, y)
        assert grad_f.tag.test_value is not None


def test_debugprint():
    x, y, z = matrices("xyz")
    e = x + y * z
    op = OpFromGraph([x, y, z], [e])
    out = op(x, y, z)

    output_str = debugprint(out, file="str")
    lines = output_str.split("\n")

    exp_res = """OpFromGraph{inline=False} [id A] ''
 |x [id B]
 |y [id C]
 |z [id D]

Inner graphs:

OpFromGraph{inline=False} [id A] ''
 >Elemwise{add,no_inplace} [id E] ''
 > |x [id F]
 > |Elemwise{mul,no_inplace} [id G] ''
 >   |y [id H]
 >   |z [id I]
"""

    for truth, out in zip(exp_res.split("\n"), lines):
        assert truth.strip() == out.strip()
