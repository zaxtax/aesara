import numpy as np
import pytest

import aesara
import aesara.gpuarray
import aesara.tensor.slinalg as slinalg
from aesara import tensor as at
from aesara.breakpoint import PdbBreakpoint
from aesara.configdefaults import config
from aesara.gpuarray import basic_ops, blas, dnn, opt
from aesara.gpuarray.basic_ops import (
    GpuAlloc,
    GpuAllocEmpty,
    GpuFromHost,
    GpuReshape,
    HostFromGpu,
    host_from_gpu,
)
from aesara.gpuarray.blas import GpuGemm
from aesara.gpuarray.dnn import GpuDnnReduction
from aesara.gpuarray.elemwise import (
    Elemwise,
    GpuCAReduceCPY,
    GpuCAReduceCuda,
    GpuElemwise,
    max_inputs_to_GpuElemwise,
)
from aesara.gpuarray.linalg import GpuCholesky, GpuCusolverSolve, cusolver_available
from aesara.gpuarray.subtensor import GpuSubtensor
from aesara.gpuarray.type import GpuArrayType, get_context, gpuarray_shared_constructor
from aesara.graph.opt import check_stack_trace
from aesara.raise_op import Assert, assert_op
from aesara.tensor.basic import Alloc, AllocEmpty, MakeVector, Rebroadcast
from aesara.tensor.blas import batched_dot
from aesara.tensor.math import dot, eq, exp, gt, tanh
from aesara.tensor.nnet import abstract_conv
from aesara.tensor.type import (
    TensorType,
    bmatrix,
    cscalar,
    fmatrix,
    fscalar,
    ftensor4,
    iscalar,
    ivector,
    lscalar,
    lvector,
    matrix,
    scalar,
    tensor3,
    vector,
)
from tests import unittest_tools as utt
from tests.gpuarray.config import mode_with_gpu, mode_without_gpu, test_ctx_name
from tests.tensor.test_basic import TestSpecifyShape
from tests.test_ifelse import TestIfelse


def _check_stack_trace(thing):
    from aesara.tensor.shape import Shape, Shape_i

    def _ops_to_check(op):
        if not isinstance(op, aesara.graph.op.Op):
            op = op.op  # assume it is an apply node
        return not isinstance(
            op,
            (
                Shape_i,
                Shape,
                aesara.compile.ops.DeepCopyOp,
                MakeVector,
                aesara.tensor.subtensor.Subtensor,
                aesara.tensor.elemwise.Elemwise,
                aesara.ifelse.IfElse,
                GpuFromHost,
                HostFromGpu,
            ),
        )

    return check_stack_trace(thing, ops_to_check=_ops_to_check, bug_print="ignore")


def test_local_assert():
    x = fmatrix()
    a = assert_op(x, eq(x, 0).any())
    f = aesara.function([x], a, mode=mode_with_gpu)
    topo = f.maker.fgraph.toposort()
    a_op = [n for n in topo if isinstance(n.op, Assert)]
    assert len(a_op) == 1
    assert isinstance(a_op[0].inputs[0].type, GpuArrayType)


def test_local_remove_all_assert():
    x = fmatrix()
    a = assert_op(x, eq(x, 0).any())

    # By default `unsafe` should not be there
    f = aesara.function([x], a, mode=mode_with_gpu.excluding("unsafe"))
    topo = f.maker.fgraph.toposort()
    a_op = [n for n in topo if isinstance(n.op, Assert)]
    assert len(a_op) == 1

    # Put `unsafe`
    f = aesara.function([x], a, mode=mode_with_gpu.including("unsafe"))
    topo = f.maker.fgraph.toposort()
    a_op = [n for n in topo if isinstance(n.op, Assert)]
    assert len(a_op) == 0

    # Remove `unsafe`
    f = aesara.function([x], a, mode=mode_with_gpu.excluding("unsafe"))
    topo = f.maker.fgraph.toposort()
    a_op = [n for n in topo if isinstance(n.op, Assert)]
    assert len(a_op) == 1


def test_local_gpu_contiguous_gpu_contiguous():
    a = fmatrix()
    o1 = basic_ops.gpu_contiguous(a)
    o2 = basic_ops.gpu_contiguous(o1)
    f1 = aesara.function([a], o1, mode=mode_with_gpu)
    f2 = aesara.function([a], o2, mode=mode_with_gpu)
    assert 1 == len(
        [
            node
            for node in f1.maker.fgraph.toposort()
            if isinstance(node.op, basic_ops.GpuContiguous)
        ]
    )
    assert 1 == len(
        [
            node
            for node in f2.maker.fgraph.toposort()
            if isinstance(node.op, basic_ops.GpuContiguous)
        ]
    )
    assert _check_stack_trace(f1)
    assert _check_stack_trace(f2)


def test_local_gpu_contiguous():
    a = fmatrix()
    o = aesara.tensor.extra_ops.cpu_contiguous(a)
    f = aesara.function([a], o, mode=mode_with_gpu)
    assert 1 == len(
        [
            node
            for node in f.maker.fgraph.toposort()
            if isinstance(node.op, basic_ops.GpuContiguous)
        ]
    )
    f([[2.0]])
    assert _check_stack_trace(f)


def test_flatten():
    m = fmatrix()
    f = aesara.function([m], m.flatten(), mode=mode_with_gpu)
    val = np.random.rand(10, 11).astype("float32")
    res = f(val)
    utt.assert_allclose(res, val.flatten())
    assert res.shape == val.flatten().shape
    assert GpuReshape in [type(node.op) for node in f.maker.fgraph.toposort()]
    val = np.random.rand(10, 11).astype("float32")
    res = f(val)
    utt.assert_allclose(res, val.flatten())
    assert res.shape == val.flatten().shape
    assert GpuReshape in [type(node.op) for node in f.maker.fgraph.toposort()]
    assert _check_stack_trace(f)

    f = aesara.function(
        [m], m.flatten(ndim=2), mode=mode_with_gpu.excluding("local_useless_reshape")
    )
    val = np.random.rand(10, 11).astype("float32")
    res = f(val)
    utt.assert_allclose(res, val)
    assert res.shape == val.shape
    assert GpuReshape in [type(node.op) for node in f.maker.fgraph.toposort()]
    assert _check_stack_trace(f)

    m = tensor3()
    f = aesara.function([m], m.flatten(ndim=2), mode=mode_with_gpu)
    val = np.random.rand(10, 11, 12).astype("float32")
    res = f(val)
    utt.assert_allclose(res, val.reshape(10, -1))
    assert res.shape == val.reshape(10, -1).shape
    assert GpuReshape in [type(node.op) for node in f.maker.fgraph.toposort()]
    assert _check_stack_trace(f)


def test_reduce():
    kind = get_context(test_ctx_name).kind

    for method, param in [
        ("sum", dict(acc_dtype="float32")),
        ("prod", dict(acc_dtype="float32")),
        ("max", {}),
        ("min", {}),
    ]:
        m = fmatrix()
        f = aesara.function(
            [m], getattr(m, method)(axis=0, **param), mode=mode_with_gpu
        )
        # assert _check_stack_trace(f) this op is ok but since
        # it is using GpuCAReduceCuda that has an empty stack
        # trace, this assertion gives error.
        val = np.random.rand(10, 11).astype("float32")
        res = f(val)
        utt.assert_allclose(res, getattr(val, method)(axis=0))
        assert res.shape == (11,)
        topo = f.maker.fgraph.toposort()
        ops = [type(node.op) for node in topo]

        if kind == b"opencl" and method in ["max", "min"]:
            assert not (
                GpuCAReduceCuda in ops
                or GpuCAReduceCPY in ops
                or GpuDnnReduction in ops
            )
        else:
            assert (
                GpuCAReduceCuda in ops
                or GpuCAReduceCPY in ops
                or GpuDnnReduction in ops
            )


def test_local_gpualloc_memset_0():
    i = iscalar()
    z = np.zeros((1,), dtype="float32")
    o = np.ones((1,), dtype="float32")
    ones = np.ones((2,), dtype="float32")

    # Test with 0 from CPU op.
    # Should not be transferred as the only client is the output
    a = at.alloc(z, i)
    f = aesara.function([i], a, mode=mode_with_gpu)
    topo = f.maker.fgraph.toposort()
    assert len(topo) == 1
    assert isinstance(topo[0].op, Alloc)
    assert (np.asarray(f(6)) == 0).all()
    assert _check_stack_trace(f)

    # Test with 0 from CPU op.
    # Should be transferred as it is used by another op.
    a = at.alloc(z, i)
    f = aesara.function([i], a.cumsum(), mode=mode_with_gpu)
    topo = f.maker.fgraph.toposort()
    assert len(topo) == 3
    assert isinstance(topo[0].op, GpuAlloc)
    assert (np.asarray(f(6)) == 0).all()
    assert _check_stack_trace(f)

    # Test with 0
    a = GpuAlloc(test_ctx_name)(z, i)
    f = aesara.function([i], a, mode=mode_with_gpu)
    topo = f.maker.fgraph.toposort()
    assert len(topo) == 1
    assert isinstance(topo[0].op, GpuAlloc) and topo[0].op.memset_0
    assert (np.asarray(f(6)) == 0).all()
    assert _check_stack_trace(f)

    # Test with 1
    a = GpuAlloc(test_ctx_name)(o, i)
    f = aesara.function([i], a, mode=mode_with_gpu)
    topo = f.maker.fgraph.toposort()
    assert len(topo) == 1
    assert isinstance(topo[0].op, GpuAlloc)
    assert not topo[0].op.memset_0
    assert (np.asarray(f(6)) == 1).all()
    assert _check_stack_trace(f)

    # Test with 1, 1
    a = GpuAlloc(test_ctx_name)(ones, i)
    f = aesara.function([i], a, mode=mode_with_gpu)
    topo = f.maker.fgraph.toposort()
    assert len(topo) == 1
    assert isinstance(topo[0].op, GpuAlloc)
    assert not topo[0].op.memset_0
    assert (np.asarray(f(2)) == 1).all()
    assert _check_stack_trace(f)


def test_local_gpualloc_empty():
    i = iscalar()
    ii = iscalar()

    # Test with vector
    # Should not be moved as the only client is the output
    a = AllocEmpty("float32")(i)
    f = aesara.function([i], a, mode=mode_with_gpu)
    topo = f.maker.fgraph.toposort()
    assert len(topo) == 1
    assert isinstance(topo[0].op, AllocEmpty)
    # This return not initialized data, so we can only check the shape
    assert f(3).shape == (3,)
    assert _check_stack_trace(f)

    # Test with vector
    # Should be moved
    a = AllocEmpty("float32")(i)
    f = aesara.function([i], a.cumsum(), mode=mode_with_gpu)
    topo = f.maker.fgraph.toposort()
    assert len(topo) == 3
    assert isinstance(topo[0].op, GpuAllocEmpty)
    # This return not initialized data, so we can only check the shape
    assert f(3).shape == (3,)
    assert _check_stack_trace(f)

    # Test with matrix
    a = AllocEmpty("float32")(i, ii)
    f = aesara.function([i, ii], a.cumsum(axis=0), mode=mode_with_gpu)
    topo = f.maker.fgraph.toposort()
    assert len(topo) == 3
    assert isinstance(topo[0].op, GpuAllocEmpty)
    # This return not initialized data, so we can only check the shape
    assert f(3, 4).shape == (3, 4)
    assert _check_stack_trace(f)


def test_rebroadcast():
    d = np.random.rand(10, 10).astype("float32")
    v = fmatrix()
    up = at.unbroadcast(v.sum().dimshuffle("x", "x"), 0, 1)
    f = aesara.function([v], [up], mode=mode_with_gpu)

    f(d)

    topo = f.maker.fgraph.toposort()
    rebrs = [node for node in topo if isinstance(node.op, Rebroadcast)]
    assert len(rebrs) == 1
    rebr = rebrs[0]

    assert isinstance(rebr.inputs[0].type, GpuArrayType)
    assert isinstance(rebr.outputs[0].type, GpuArrayType)
    assert _check_stack_trace(f)


class TestSpecifyShape(TestSpecifyShape):
    mode = mode_with_gpu
    input_type = GpuArrayType


class TestGpuIfelse(TestIfelse):
    mode = mode_with_gpu

    @staticmethod
    def cast_output(v):
        return basic_ops.as_gpuarray_variable(v, test_ctx_name)

    shared = staticmethod(gpuarray_shared_constructor)

    def get_ifelse(self, n):
        return aesara.ifelse.IfElse(n, gpu=True, as_view=True)

    def test_lifter_with_inputs_of_graph(self):
        x = vector()
        cond = iscalar()
        f = aesara.function(
            [x, cond], aesara.ifelse.ifelse(cond, x.mean(), x.sum()), mode=mode_with_gpu
        )
        assert f(np.float32([1, 2, 3]), 0) == 6
        assert _check_stack_trace(f)

        x = vector()
        cond = scalar()
        f = aesara.function(
            [x, cond], aesara.ifelse.ifelse(cond, x.mean(), x.sum()), mode=mode_with_gpu
        )
        assert f(np.float32([1, 2, 3]), 0) == 6
        assert _check_stack_trace(f)

    def test_lifter_with_shared_var(self):
        x = lscalar("x")
        y = gpuarray_shared_constructor(
            np.asarray(1, dtype="float32"), target=test_ctx_name
        )
        z = at.constant(2.0)

        a = aesara.ifelse.ifelse(x, y, z)
        with config.change_flags(on_opt_error="raise"):
            aesara.function([x], [a], mode=mode_with_gpu)


def test_print_op():
    # Test that print ops don't block gpu optimization
    b = fmatrix()
    f = aesara.function([b], aesara.printing.Print()(b) * 2, mode=mode_with_gpu)
    topo = f.maker.fgraph.toposort()
    assert isinstance(topo[0].op, GpuFromHost)
    assert isinstance(topo[1].op, aesara.printing.Print)
    assert isinstance(topo[2].op, GpuElemwise)
    assert topo[3].op == host_from_gpu
    assert _check_stack_trace(f)
    f(np.random.random((5, 5)).astype("float32"))


def test_pdbbreakpoint_op():
    # Test that PdbBreakpoint ops don't block gpu optimization
    b = fmatrix()

    # Create a function composed of a breakpoint followed by
    # some computation
    condition = gt(b.sum(), 0)
    b_monitored = PdbBreakpoint(name="TestBreakpoint")(condition, b)
    output = b_monitored ** 2

    f = aesara.function([b], output, mode=mode_with_gpu)

    # Ensure that, in the compiled function, the computation following the
    # breakpoint has been moved to the gpu.
    topo = f.maker.fgraph.toposort()
    assert isinstance(topo[-2].op, GpuElemwise)
    assert topo[-1].op == host_from_gpu
    assert _check_stack_trace(f)


def test_local_gpu_elemwise_careduce():
    mode_with_gpu_no_cudnn = mode_with_gpu.excluding("cudnn")
    x = matrix()

    def fn_sum_square(x, axis):
        return (x * x).sum(axis=axis)

    def fn_sum_abs(x, axis):
        return abs(x).sum(axis=axis)

    def fn_max_abs(x, axis):
        return abs(x).max(axis=axis)

    for fn, pre_scalar_op in (
        (fn_sum_square, aesara.scalar.sqr),
        (fn_sum_abs, aesara.scalar.abs_),
        (fn_max_abs, aesara.scalar.abs_),
    ):
        for axis in (None, 0, 1):
            o = fn(x, axis)
            f = aesara.function([x], o, mode=mode_with_gpu_no_cudnn)
            topo = f.maker.fgraph.toposort()
            assert len(topo) == 3
            assert isinstance(topo[1].op, GpuCAReduceCuda)
            assert topo[1].op.pre_scalar_op == pre_scalar_op
            assert _check_stack_trace(f)
            data = np.random.rand(3, 4).astype(config.floatX)
            utt.assert_allclose(fn(data, axis), f(data))


def test_local_lift_dot22scalar():
    x = matrix()
    y = matrix()
    a = scalar()
    o = aesara.tensor.blas.Dot22Scalar()(x, y, a)
    f_cpu = aesara.function([x, y, a], o)
    f_gpu = aesara.function([x, y, a], o, mode=mode_with_gpu)
    assert not any(
        isinstance(n.op, aesara.tensor.blas.Dot22Scalar)
        for n in f_gpu.maker.fgraph.apply_nodes
    )
    assert any(isinstance(n.op, GpuGemm) for n in f_gpu.maker.fgraph.apply_nodes)
    x_val = np.random.random((2, 3)).astype(config.floatX)
    y_val = np.random.random((3, 4)).astype(config.floatX)
    a_val = 0.5
    utt.assert_allclose(f_cpu(x_val, y_val, a_val), f_gpu(x_val, y_val, a_val))
    assert _check_stack_trace(f_gpu)


def test_local_gpu_subtensor():
    # Test shared forced on CPU.
    t = aesara.shared(np.zeros(20, "float32"))
    f = aesara.function([], t[3:4], mode=mode_with_gpu)
    topo = f.maker.fgraph.toposort()
    assert any(type(node.op) is aesara.tensor.subtensor.Subtensor for node in topo)
    assert not any(isinstance(node.op, GpuSubtensor) for node in topo)
    assert _check_stack_trace(f)

    # Test graph input.
    t = fmatrix()
    f = aesara.function([t], t[3:4], mode=mode_with_gpu)
    topo = f.maker.fgraph.toposort()
    assert any(type(node.op) is aesara.tensor.subtensor.Subtensor for node in topo)
    assert not any(isinstance(node.op, GpuSubtensor) for node in topo)
    assert _check_stack_trace(f)

    # Test multiple use of the input
    # We want the subtensor to be on the GPU to prevent multiple transfer.
    t = fmatrix()
    f = aesara.function([t], [t[3:4], t + 1], mode=mode_with_gpu)
    topo = f.maker.fgraph.toposort()
    assert not any(type(node.op) is aesara.tensor.subtensor.Subtensor for node in topo)
    assert any(isinstance(node.op, GpuSubtensor) for node in topo)
    assert _check_stack_trace(f)

    # Test multiple use of the input + input as output
    # We want the subtensor to be on the GPU to prevent multiple transfer.
    t = fmatrix()
    f = aesara.function([t], [t[3:4], t + 1, t], mode=mode_with_gpu)
    topo = f.maker.fgraph.toposort()
    assert not any(type(node.op) is aesara.tensor.subtensor.Subtensor for node in topo)
    assert any(isinstance(node.op, GpuSubtensor) for node in topo)
    assert _check_stack_trace(f)

    # Test shared forced on CPU end we do computation on the output of
    # the subtensor.
    t = aesara.shared(np.zeros(20, "float32"))
    f = aesara.function([], t[3:4] + 1, mode=mode_with_gpu)
    topo = f.maker.fgraph.toposort()
    assert any(type(node.op) is aesara.tensor.subtensor.Subtensor for node in topo)
    assert not any(isinstance(node.op, GpuSubtensor) for node in topo)
    # Our optimizer isn't smart enough to move to the GPU Elemwise.
    # If it where just a little bit smarter, it could wrongly move it to the GPU.
    # If it where super smart, it would know it should not move it to the GPU.
    assert any(isinstance(node.op, aesara.tensor.elemwise.Elemwise) for node in topo)
    assert _check_stack_trace(f)


def test_local_gpu_elemwise():
    # Test local_gpu_elemwise when there is a dtype upcastable to float32

    a = bmatrix()
    b = fmatrix()
    c = fmatrix()

    a_v = (np.random.rand(4, 5) * 10).astype("int8")
    b_v = (np.random.rand(4, 5) * 10).astype("float32")
    c_v = (np.random.rand(4, 5) * 10).astype("float32")

    # Due to optimization order, this composite is created when all
    # the op are on the gpu.
    f = aesara.function([a, b, c], a + b + c, mode=mode_with_gpu)
    topo = f.maker.fgraph.toposort()
    assert sum(isinstance(node.op, GpuElemwise) for node in topo) == 1
    assert sum(type(node.op) == aesara.tensor.elemwise.Elemwise for node in topo) == 0
    utt.assert_allclose(f(a_v, b_v, c_v), a_v + b_v + c_v)
    assert _check_stack_trace(f)

    # Now test with the composite already on the cpu before we move it
    # to the gpu
    a_s = aesara.scalar.int8()
    b_s = aesara.scalar.float32()
    c_s = aesara.scalar.float32()
    out_s = aesara.scalar.Composite([a_s, b_s, c_s], [a_s + b_s + c_s])
    out_op = aesara.tensor.elemwise.Elemwise(out_s)
    f = aesara.function([a, b, c], out_op(a, b, c), mode=mode_with_gpu)
    topo = f.maker.fgraph.toposort()
    assert sum(isinstance(node.op, GpuElemwise) for node in topo) == 1
    assert sum(type(node.op) == aesara.tensor.elemwise.Elemwise for node in topo) == 0
    utt.assert_allclose(f(a_v, b_v, c_v), a_v + b_v + c_v)
    assert _check_stack_trace(f)

    return  # Not yet implemented
    # Test multiple output
    a_s = aesara.scalar.float32()
    a = fmatrix()
    from aesara.scalar.basic import identity

    out_s = aesara.scalar.Composite(
        [a_s, b_s, c_s], [identity(a_s), identity(c_s), identity(b_s)]
    )
    outs_op = aesara.tensor.elemwise.Elemwise(out_s)
    f = aesara.function([a, b, c], outs_op(a, b, c), mode=mode_with_gpu)
    topo = f.maker.fgraph.toposort()
    assert sum(isinstance(node.op, GpuElemwise) for node in topo) == 1
    assert sum(type(node.op) == aesara.tensor.elemwise.Elemwise for node in topo) == 0
    out = f(a_v, b_v, c_v)
    utt.assert_allclose(out[0], a_v)
    utt.assert_allclose(out[1], c_v)
    utt.assert_allclose(out[2], b_v)
    assert _check_stack_trace(f)

    # Test multiple output
    out_s = aesara.scalar.Composite([a_s, b_s, c_s], [a_s + b_s, a_s * b_s])
    outs_op = aesara.tensor.elemwise.Elemwise(out_s)
    f = aesara.function([a, b, c], outs_op(a, b, c), mode=mode_with_gpu)
    topo = f.maker.fgraph.toposort()
    assert sum(isinstance(node.op, GpuElemwise) for node in topo) == 1
    assert sum(type(node.op) == aesara.tensor.elemwise.Elemwise for node in topo) == 0
    out = f(a_v, b_v, c_v)
    utt.assert_allclose(out[0], a_v + b_v)
    utt.assert_allclose(out[1], a_v * c_v)
    assert _check_stack_trace(f)

    # Test non-contiguous input
    c = gpuarray_shared_constructor(np.asarray(c_v, dtype="float32"))
    f = aesara.function([a, b], outs_op(a[::2], b[::2], c[::2]), mode=mode_with_gpu)
    out = f(a_v, b_v)
    utt.assert_allclose(out[0], a_v[::2] + b_v[::2])
    utt.assert_allclose(out[1], a_v[::2] * c_v[::2])
    assert _check_stack_trace(f)


def test_many_arg_elemwise():
    # This test checks whether the + and * elemwise ops can handle
    # extremely large numbers of arguments on gpu.

    rng = np.random.default_rng([1, 2, 3])
    nb_of_inputs_overflows = []
    for num_args in [64]:
        for op_to_test in [aesara.tensor.add, aesara.tensor.mul]:
            for nb_dim in [2, 8]:
                shapes = [rng.integers(1, 5) for i in range(nb_dim)]
                args = [
                    np.cast["float32"](rng.standard_normal(shapes))
                    for arg in range(0, num_args)
                ]

                symb_args = [
                    TensorType("float32", (False,) * nb_dim)()
                    for arg in range(0, num_args)
                ]

                outputs = []
                for mode in [mode_with_gpu, mode_without_gpu]:
                    # test the optimization local_gpua_elemwise
                    output = op_to_test(*symb_args)
                    f = aesara.function(symb_args, output, mode=mode)
                    outputs.append(f(*args))

                    # assert that the test was done on the gpu.
                    if mode is mode_with_gpu:
                        nb_of_inputs_overflows.append(
                            max_inputs_to_GpuElemwise(output.owner) - num_args
                        )
                        nodelst = [node for node in f.maker.fgraph.apply_nodes]
                        assert any(isinstance(node.op, GpuElemwise) for node in nodelst)
                        assert not any(
                            isinstance(node.op, Elemwise)
                            for node in nodelst
                            if not isinstance(node.op, GpuElemwise)
                        )
                results_gpu, results_cpu = outputs
                utt.assert_allclose(results_gpu, results_cpu)

    # Make sure we test at least one case with no number of inputs overflow
    assert any(overflow >= 0 for overflow in nb_of_inputs_overflows)

    # Make sure we test at least one case with number of inputs overflow
    assert any(overflow < 0 for overflow in nb_of_inputs_overflows)


def test_not_useless_scalar_gpuelemwise():
    # We don't want to move elemwise on scalar on the GPU when the
    # result will not be used on the GPU!

    with config.change_flags(warn_float64="ignore"):
        X = fmatrix()
        x = np.random.standard_normal((32, 32)).astype(np.float32)
        m1 = aesara.shared(np.random.standard_normal((32, 32)).astype(np.float32))
        loss = (X - dot(X, m1)).norm(L=2)
        lr = aesara.shared(np.asarray(0.001, dtype=np.float32))
        grad = aesara.grad(loss, m1)

        train = aesara.function(
            inputs=[X], updates=[(m1, m1 - lr * grad)], mode=mode_with_gpu
        )
        train(x)
        topo = train.maker.fgraph.toposort()
        gemms = [app for app in topo if isinstance(app.op, GpuGemm)]
        assert len(gemms) == 2
        assert isinstance(gemms[1].inputs[1].owner.op, aesara.tensor.elemwise.Elemwise)


def test_local_lift_abstractconv_gpu_shape():
    with config.change_flags(on_opt_error="raise"):
        s = ivector()
        a = ftensor4()
        b = ftensor4()
        c = aesara.tensor.nnet.abstract_conv.AbstractConv2d_gradWeights()(a, b, s)
        f = aesara.function([s, a, b], c, mode=mode_with_gpu)
        assert _check_stack_trace(f)


def test_local_assert_no_cpu_op():
    rng = np.random.default_rng(utt.fetch_seed())
    m = rng.uniform(-1, 1, (10, 10)).astype("float32")
    ms = gpuarray_shared_constructor(m, name="m_shared")
    out = tanh(ms).dot(ms.T)

    mode_local_assert = mode_with_gpu.including("assert_no_cpu_op")
    mode_local_assert = mode_local_assert.excluding("local_gpua_elemwise")

    with config.change_flags(assert_no_cpu_op="raise", on_opt_error="ignore"):
        with pytest.raises(AssertionError):
            aesara.function([], out, mode=mode_local_assert)

    with config.change_flags(assert_no_cpu_op="ignore"):
        f = aesara.function([], out, mode=mode_local_assert)
        assert _check_stack_trace(f)


def test_no_complex():
    width_var = cscalar()
    freq_var = fscalar()
    signal_var = fscalar()
    stft_out = exp(width_var * freq_var) * signal_var
    f = aesara.function([width_var, freq_var, signal_var], stft_out, mode=mode_with_gpu)
    assert _check_stack_trace(f)


@utt.assertFailure_fast
@pytest.mark.skipif(not cusolver_available, reason="No cuSolver or SciPy")
def test_local_lift_solve():
    A = fmatrix()
    b = fmatrix()
    o = slinalg.solve(A, b)
    f_cpu = aesara.function([A, b], o, mode_without_gpu)
    f_gpu = aesara.function([A, b], o, mode=mode_with_gpu)
    assert not any(
        isinstance(n.op, slinalg.Solve) for n in f_gpu.maker.fgraph.apply_nodes
    )
    assert any(
        isinstance(n.op, GpuCusolverSolve) and n.op.inplace
        for n in f_gpu.maker.fgraph.apply_nodes
    )
    A_val = np.random.uniform(-0.4, 0.4, (5, 5)).astype("float32")
    b_val = np.random.uniform(-0.4, 0.4, (5, 3)).astype("float32")
    utt.assert_allclose(f_cpu(A_val, b_val), f_gpu(A_val, b_val))
    assert _check_stack_trace(f_gpu)


@pytest.mark.skipif(not cusolver_available, reason="No cuSolver or SciPy")
def test_gpu_solve_not_inplace():
    A = fmatrix()
    b = fmatrix()
    s = slinalg.solve(A, b)
    o = dot(A, s)
    f_cpu = aesara.function([A, b], o, mode_without_gpu)
    f_gpu = aesara.function([A, b], o, mode=mode_with_gpu)
    count_not_inplace = len(
        [
            n.op
            for n in f_gpu.maker.fgraph.apply_nodes
            if isinstance(n.op, GpuCusolverSolve) and not n.op.inplace
        ]
    )
    assert count_not_inplace == 1, count_not_inplace
    A_val = np.random.uniform(-0.4, 0.4, (5, 5)).astype("float32")
    b_val = np.random.uniform(-0.4, 0.4, (5, 3)).astype("float32")
    utt.assert_allclose(f_cpu(A_val, b_val), f_gpu(A_val, b_val))


@utt.assertFailure_fast
@pytest.mark.skipif(not cusolver_available, reason="No cuSolver or SciPy")
def test_local_lift_cholesky():
    A = fmatrix()
    o = slinalg.cholesky(A)
    f_cpu = aesara.function([A], o, mode=mode_without_gpu)
    f_gpu = aesara.function([A], o, mode=mode_with_gpu)
    assert not any(
        isinstance(n.op, slinalg.Cholesky) for n in f_gpu.maker.fgraph.apply_nodes
    )
    # GpuCholesky op in this graph should be inplace (as his input is not reused by other op).
    assert any(
        isinstance(n.op, GpuCholesky) and n.op.inplace
        for n in f_gpu.maker.fgraph.apply_nodes
    )
    M_val = np.random.normal(size=(3, 3)).astype("float32")
    # A = M.dot(M) will be positive definite for all non-singular M
    A_val = M_val.dot(M_val.T)
    utt.assert_allclose(f_cpu(A_val), f_gpu(A_val))


@pytest.mark.skipif(not cusolver_available, reason="No cuSolver or SciPy")
def test_gpu_cholesky_not_inplace():
    A = fmatrix()
    A_squared = A ** 2
    B = slinalg.cholesky(A_squared)
    D = B + A_squared
    f_cpu = aesara.function([A], D, mode=mode_without_gpu)
    f_gpu = aesara.function([A], D, mode=mode_with_gpu)
    # GpuCholesky op in this graph should NOT be inplace (as his input is reused in another op)
    count_cholesky_not_inplace = len(
        [
            n.op
            for n in f_gpu.maker.fgraph.apply_nodes
            if isinstance(n.op, GpuCholesky) and not n.op.inplace
        ]
    )
    assert count_cholesky_not_inplace == 1, count_cholesky_not_inplace
    M_val = np.random.normal(size=(3, 3)).astype("float32")
    # A = M.dot(M) will be positive definite for all non-singular M
    A_val = M_val.dot(M_val.T)
    utt.assert_allclose(f_cpu(A_val), f_gpu(A_val))


def test_local_gpua_advanced_incsubtensor():
    # test a corner case reported at gh-5589
    target = ftensor4()
    y = target.dimshuffle(1, 0, 2, 3).flatten(ndim=1)
    w = at.ones_like(y)
    w = aesara.tensor.subtensor.set_subtensor(w[eq(y, 1.0).nonzero()], 100)
    w = aesara.tensor.subtensor.set_subtensor(w[eq(y, -1.0).nonzero()], 0)
    f = aesara.function([target], w)
    assert _check_stack_trace(f)


def test_batched_dot_lifter():
    # The CPU Op accepts 2D and 3D inputs, as well as mixed dtypes.
    # Make sure the lifter adds the appropriate dimshuffles and casts
    rng = np.random.default_rng(utt.fetch_seed())

    def randX(*args):
        return rng.random(args).astype(config.floatX)

    cases = [
        (randX(3, 5, 7), randX(3, 7)),
        (randX(3, 5), randX(3, 5, 7)),
        (randX(3, 5), randX(3, 5)),
        (rng.random((3, 5, 7)).astype("float32"), randX(3, 7, 9)),
        (rng.random((3, 5, 7)).astype("float64"), randX(3, 7, 9)),
    ]
    for x_val, y_val in cases:
        x = TensorType(broadcastable=[s == 1 for s in x_val.shape], dtype=x_val.dtype)(
            "x"
        )
        y = TensorType(broadcastable=[s == 1 for s in y_val.shape], dtype=y_val.dtype)(
            "y"
        )
        z = batched_dot(x, y)
        f = aesara.function([x, y], z, mode=mode_with_gpu)
        f(x_val, y_val)
        assert check_stack_trace(f, ops_to_check="all")


def test_crossentropycategorical1hot_lifter():
    rng = np.random.default_rng(utt.fetch_seed())
    x = matrix()
    y = lvector()
    z = aesara.tensor.nnet.crossentropy_categorical_1hot(x, y)
    gx = aesara.grad(z.mean(), x)
    f = aesara.function([x, y], [z, gx], mode=mode_with_gpu)
    assert not any(
        isinstance(
            n.op,
            (
                aesara.tensor.nnet.CrossentropyCategorical1Hot,
                aesara.tensor.nnet.CrossentropyCategorical1HotGrad,
            ),
        )
        for n in f.maker.fgraph.apply_nodes
    )
    f(
        rng.uniform(0.1, 0.9, (13, 5)).astype(config.floatX),
        rng.integers(5, size=(13,)),
    )


class TestConv_opt:
    def optimizer_2d(
        self,
        input_shapes,
        direction,
        include_tags,
        exclude_tags,
        op,
        border_mode="valid",
        subsample=(1, 1),
        filter_dilation=(1, 1),
        num_groups=1,
        unshared=False,
        optimiser=None,
    ):

        inp1 = aesara.shared(np.random.random(input_shapes[0]).astype(config.floatX))
        inp2 = aesara.shared(np.random.random(input_shapes[1]).astype(config.floatX))
        if op is None:
            inp1 = basic_ops.as_gpuarray_variable(inp1, test_ctx_name)
            inp2 = basic_ops.as_gpuarray_variable(inp2, test_ctx_name)
        if direction == 0:
            conv_op = abstract_conv.AbstractConv2d(
                input_shapes[0],
                input_shapes[1],
                border_mode=border_mode,
                subsample=subsample,
                filter_dilation=filter_dilation,
                num_groups=num_groups,
                unshared=unshared,
            )(inp1, inp2)

        if direction == 1:
            conv_op = abstract_conv.AbstractConv2d_gradWeights(
                imshp=input_shapes[0],
                kshp=input_shapes[2],
                border_mode=border_mode,
                subsample=subsample,
                filter_dilation=filter_dilation,
                num_groups=num_groups,
                unshared=unshared,
            )(inp1, inp2, input_shapes[2][-2:])

        if direction == 2:
            conv_op = abstract_conv.AbstractConv2d_gradInputs(
                imshp=input_shapes[2],
                kshp=input_shapes[1],
                border_mode=border_mode,
                subsample=subsample,
                filter_dilation=filter_dilation,
                num_groups=num_groups,
                unshared=unshared,
            )(inp2, inp1, input_shapes[2][-2:])

        with config.change_flags(
            metaopt__optimizer_including=include_tags,
            metaopt__optimizer_excluding=exclude_tags,
        ):
            mode = (
                mode_with_gpu.including("conv_meta")
                .excluding("conv_dnn")
                .excluding("conv_gemm")
            )

            # All meta optimizer compile a new function. This need to know
            # the current linker, but this information is not available,
            # so it use the default mode.
            if op is None:
                # No convolutions optimization takes place
                assert optimiser.transform(None, conv_op.owner) is None
            else:
                ref_func = aesara.function([], conv_op, mode=mode_with_gpu)
                with config.change_flags(mode=mode):
                    conv_func = aesara.function([], conv_op, mode=mode)
                assert any(
                    [
                        isinstance(node.op, op)
                        for node in conv_func.maker.fgraph.toposort()
                    ]
                )
                utt.assert_allclose(conv_func(), ref_func())

    def optimizer_3d(
        self,
        input_shapes,
        direction,
        include_tags,
        exclude_tags,
        op,
        border_mode="valid",
        subsample=(1, 1, 1),
        filter_dilation=(1, 1, 1),
        num_groups=1,
        optimiser=None,
    ):
        inp1 = aesara.shared(np.random.random(input_shapes[0]).astype(config.floatX))
        inp2 = aesara.shared(np.random.random(input_shapes[1]).astype(config.floatX))

        if op is None:
            inp1 = basic_ops.as_gpuarray_variable(inp1, None)
            inp2 = basic_ops.as_gpuarray_variable(inp2, None)
        if direction == 0:
            conv_op = abstract_conv.AbstractConv3d(
                input_shapes[0],
                input_shapes[1],
                border_mode=border_mode,
                subsample=subsample,
                filter_dilation=filter_dilation,
                num_groups=num_groups,
            )(inp1, inp2)

        if direction == 1:
            conv_op = abstract_conv.AbstractConv3d_gradWeights(
                input_shapes[0],
                input_shapes[2],
                border_mode=border_mode,
                subsample=subsample,
                filter_dilation=filter_dilation,
                num_groups=num_groups,
            )(inp1, inp2, input_shapes[2][-3:])

        if direction == 2:
            conv_op = abstract_conv.AbstractConv3d_gradInputs(
                input_shapes[2],
                input_shapes[1],
                border_mode=border_mode,
                subsample=subsample,
                filter_dilation=filter_dilation,
                num_groups=num_groups,
            )(inp2, inp1, input_shapes[2][-3:])

        with config.change_flags(
            metaopt__optimizer_including=include_tags,
            metaopt__optimizer_excluding=exclude_tags,
        ):

            mode = (
                mode_with_gpu.including("conv_meta")
                .excluding("conv_dnn")
                .excluding("conv_gemm")
            )

            # All meta optimizer compile a new function. This need to know
            # the current linker, but this information is not available,
            # so it use the default mode.
            if op is None:
                # No convolutions optimization takes place
                assert optimiser.transform(None, conv_op.owner) is None
                return
            elif op != "conv3d2d":
                with config.change_flags(mode=mode):
                    conv_func = aesara.function([], conv_op, mode=mode)
                assert any(
                    [
                        isinstance(node.op, op)
                        for node in conv_func.maker.fgraph.toposort()
                    ]
                )
            else:
                with config.change_flags(mode=mode):
                    conv_func = aesara.function(
                        [], conv_op, mode=mode_with_gpu.including("conv_meta")
                    )
            ref_func = aesara.function([], conv_op, mode=mode_with_gpu)
            utt.assert_allclose(conv_func(), ref_func())

    @pytest.mark.skipif(config.cxx == "", reason="Need a c compiler.")
    def test_optimizers_2d(self):
        imshp2d = [(2, 3, 5, 5), (2, 2, 5, 7), (2, 1, 3, 3)]
        kshp2d = [(4, 3, 3, 3), (3, 2, 3, 5), (4, 1, 1, 1)]
        tshp2d = [(2, 4, 3, 3), (2, 3, 3, 3), (2, 4, 3, 3)]

        for imshp, kshp, tshp in zip(imshp2d, kshp2d, tshp2d):
            # forward passes
            self.optimizer_2d(
                [imshp, kshp, tshp], 0, "", "conv_dnn:alternative", blas.GpuCorrMM
            )
            self.optimizer_2d(
                [imshp, kshp, tshp],
                0,
                "alternative",
                "conv_dnn:default",
                blas.GpuCorrMM_gradWeights,
            )
            self.optimizer_2d(
                [imshp, kshp, tshp], 0, "", "conv_gemm:alternative", dnn.GpuDnnConv
            )
            self.optimizer_2d(
                [imshp, kshp, tshp],
                0,
                "alternative",
                "conv_gemm:default",
                dnn.GpuDnnConvGradW,
            )
            # backwards wrt weights
            self.optimizer_2d(
                [imshp, tshp, kshp],
                1,
                "",
                "conv_dnn:alternative",
                blas.GpuCorrMM_gradWeights,
            )
            self.optimizer_2d(
                [imshp, tshp, kshp],
                1,
                "alternative",
                "conv_dnn:default",
                blas.GpuCorrMM,
            )
            self.optimizer_2d(
                [imshp, tshp, kshp], 1, "", "conv_gemm:alternative", dnn.GpuDnnConvGradW
            )
            self.optimizer_2d(
                [imshp, tshp, kshp],
                1,
                "alternative",
                "conv_gemm:default",
                dnn.GpuDnnConv,
            )
            # backwards wrt to inputs
            self.optimizer_2d(
                [tshp, kshp, imshp],
                2,
                "",
                "conv_dnn:alternative",
                blas.GpuCorrMM_gradInputs,
            )
            self.optimizer_2d(
                [tshp, kshp, imshp],
                2,
                "alternative",
                "conv_dnn:default",
                blas.GpuCorrMM,
            )
            self.optimizer_2d(
                [tshp, kshp, imshp], 2, "", "conv_gemm:alternative", dnn.GpuDnnConvGradI
            )
            self.optimizer_2d(
                [tshp, kshp, imshp],
                2,
                "alternative",
                "conv_gemm:default",
                dnn.GpuDnnConv,
            )

    @pytest.mark.skipif(config.cxx == "", reason="Need a c compiler.")
    def test_optimizers_3d(self):
        imshp3d = [(2, 3, 5, 5, 5), (2, 2, 5, 7, 5), (2, 1, 3, 3, 3)]
        kshp3d = [(4, 3, 3, 3, 3), (3, 2, 3, 5, 3), (4, 1, 1, 1, 1)]
        tshp3d = [(2, 4, 3, 3, 3), (2, 3, 3, 3, 3), (2, 4, 3, 3, 3)]

        for imshp, kshp, tshp in zip(imshp3d, kshp3d, tshp3d):
            # forwards passes
            self.optimizer_3d(
                [imshp, kshp, tshp],
                0,
                "",
                "conv_dnn:alternative:conv3d2d",
                blas.GpuCorr3dMM,
            )
            self.optimizer_3d(
                [imshp, kshp, tshp],
                0,
                "alternative",
                "conv_dnn:default:conv3d2d",
                blas.GpuCorr3dMM_gradWeights,
            )
            self.optimizer_3d([imshp, kshp, tshp], 0, "conv3d2d", "default", "conv3d2d")
            self.optimizer_3d(
                [imshp, kshp, tshp],
                0,
                "alternative",
                "conv_gemm:default:conv3d2d",
                dnn.GpuDnnConvGradW,
            )
            self.optimizer_3d(
                [imshp, kshp, tshp],
                0,
                "",
                "conv_gemm:alternative:conv3d2d",
                dnn.GpuDnnConv,
            )
            # backward pass wrt weight
            self.optimizer_3d(
                [imshp, tshp, kshp],
                1,
                "",
                "conv_dnn:alternative",
                blas.GpuCorr3dMM_gradWeights,
            )
            self.optimizer_3d(
                [imshp, tshp, kshp],
                1,
                "alternative",
                "conv_dnn:default",
                blas.GpuCorr3dMM,
            )
            self.optimizer_3d(
                [imshp, tshp, kshp],
                1,
                "alternative",
                "conv_gemm:default",
                dnn.GpuDnnConv,
            )
            self.optimizer_3d(
                [imshp, tshp, kshp], 1, "", "conv_gemm:alternative", dnn.GpuDnnConvGradW
            )

            # backward pass wrt inputs
            self.optimizer_3d(
                [tshp, kshp, imshp],
                2,
                "",
                "conv_dnn:alternative",
                blas.GpuCorr3dMM_gradInputs,
            )
            self.optimizer_3d(
                [tshp, kshp, imshp],
                2,
                "alternative",
                "conv_dnn:default",
                blas.GpuCorr3dMM,
            )
            self.optimizer_3d(
                [tshp, kshp, imshp],
                2,
                "alternative",
                "conv_gemm:default",
                dnn.GpuDnnConv,
            )
            self.optimizer_3d(
                [tshp, kshp, imshp], 2, "", "conv_gemm:alternative", dnn.GpuDnnConvGradI
            )

    @pytest.mark.skipif(config.cxx == "", reason="Need a c compiler.")
    def test_optimizers_non_default(self):
        # conv2d forward pass with Non-default border_mode and filter_dilation
        imshp2d = [(2, 3, 5, 5), (4, 2, 5, 5)]
        kshp2d = [(4, 3, 3, 3), (3, 2, 3, 3)]
        filter_dilation = [(1, 1), (2, 2)]
        for imshp, kshp, fdil in zip(imshp2d, kshp2d, filter_dilation):
            self.optimizer_2d(
                [imshp, kshp],
                0,
                "",
                "conv_dnn:alternative",
                blas.GpuCorrMM,
                border_mode="full",
                filter_dilation=fdil,
            )
            self.optimizer_2d(
                [imshp, kshp],
                0,
                "alternative",
                "conv_dnn:default",
                blas.GpuCorrMM_gradInputs,
                border_mode="full",
                filter_dilation=fdil,
            )
            self.optimizer_2d(
                [imshp, kshp],
                0,
                "",
                "conv_gemm:alternative",
                dnn.GpuDnnConv,
                border_mode="full",
                filter_dilation=fdil,
            )
            self.optimizer_2d(
                [imshp, kshp],
                0,
                "alternative",
                "conv_gemm:default",
                dnn.GpuDnnConvGradI,
                border_mode="full",
                filter_dilation=fdil,
            )
        # conv3d forward pass with Non-default border_mode and filter_dilation
        imshp3d = [(2, 3, 5, 5, 5), (4, 2, 5, 5, 5)]
        kshp3d = [(4, 3, 3, 3, 3), (3, 2, 3, 3, 3)]
        filter_dilation = [(1, 1, 1), (2, 2, 2)]
        for imshp, kshp, fdil in zip(imshp3d, kshp3d, filter_dilation):
            self.optimizer_3d(
                [imshp, kshp],
                0,
                "",
                "conv_dnn:alternative:conv3d2d",
                blas.GpuCorr3dMM,
                border_mode="full",
                filter_dilation=fdil,
            )
            self.optimizer_3d(
                [imshp, kshp],
                0,
                "alternative",
                "conv_dnn:default:conv3d2d",
                blas.GpuCorr3dMM_gradInputs,
                border_mode="full",
                filter_dilation=fdil,
            )
            self.optimizer_3d(
                [imshp, kshp],
                0,
                "",
                "conv_gemm:alternative:conv3d2d",
                dnn.GpuDnnConv,
                border_mode="full",
                filter_dilation=fdil,
            )
            self.optimizer_3d(
                [imshp, kshp],
                0,
                "alternative",
                "conv_gemm:default:conv3d2d",
                dnn.GpuDnnConvGradI,
                border_mode="full",
                filter_dilation=fdil,
            )

        # test non default num_groups for default optimizers
        imshp2d = [(2, 6, 5, 5), (2, 4, 5, 5)]
        kshp2d = [(3, 2, 3, 3), (2, 2, 3, 3)]
        tshp2d = [(2, 3, 3, 3), (2, 2, 3, 3)]
        num_groups = [3, 2]
        for imshp, kshp, tshp, groups in zip(imshp2d, kshp2d, tshp2d, num_groups):
            # forward pass
            self.optimizer_2d(
                [imshp, kshp, tshp],
                0,
                "",
                "conv_dnn:alternative",
                blas.GpuCorrMM,
                num_groups=groups,
            )
            self.optimizer_2d(
                [imshp, kshp, tshp],
                0,
                "",
                "conv_gemm:alternative",
                dnn.GpuDnnConv,
                num_groups=groups,
            )
            # grad with respect to weights
            self.optimizer_2d(
                [imshp, tshp, kshp],
                1,
                "",
                "conv_dnn:alternative",
                blas.GpuCorrMM_gradWeights,
                num_groups=groups,
            )
            self.optimizer_2d(
                [imshp, tshp, kshp],
                1,
                "",
                "conv_gemm:alternative",
                dnn.GpuDnnConvGradW,
                num_groups=groups,
            )
            # grad with respect to inputs
            self.optimizer_2d(
                [tshp, kshp, imshp],
                2,
                "",
                "conv_dnn:alternative",
                blas.GpuCorrMM_gradInputs,
                num_groups=groups,
            )
            self.optimizer_2d(
                [tshp, kshp, imshp],
                2,
                "",
                "conv_gemm:alternative",
                dnn.GpuDnnConvGradI,
                num_groups=groups,
            )

        # test unshared for default optimizers
        imshp2d = [(2, 2, 4, 4), (3, 2, 5, 3)]
        kshp2d = [(2, 2, 2, 2, 3, 3), (2, 3, 1, 2, 3, 3)]
        tshp2d = [(2, 2, 2, 2), (3, 2, 3, 1)]
        for imshp, kshp, tshp, groups in zip(imshp2d, kshp2d, tshp2d, num_groups):
            # forward pass
            self.optimizer_2d(
                [imshp, kshp, tshp], 0, "", "alternative", blas.GpuCorrMM, unshared=True
            )
            # grad with respect to weights
            self.optimizer_2d(
                [imshp, tshp, kshp],
                1,
                "",
                "alternative",
                blas.GpuCorrMM_gradWeights,
                unshared=True,
            )
            # grad with respect to inputs
            self.optimizer_2d(
                [tshp, kshp, imshp],
                2,
                "",
                "alternative",
                blas.GpuCorrMM_gradInputs,
                unshared=True,
            )

        imshp3d = [(2, 6, 5, 5, 5), (2, 4, 5, 5, 5)]
        kshp3d = [(3, 2, 3, 3, 3), (2, 2, 3, 3, 3)]
        tshp3d = [(2, 3, 3, 3, 3), (2, 2, 3, 3, 3)]
        num_groups = [3, 2]
        for imshp, kshp, tshp, groups in zip(imshp3d, kshp3d, tshp3d, num_groups):
            # forward pass
            self.optimizer_3d(
                [imshp, kshp, tshp],
                0,
                "",
                "conv_dnn:alternative:conv3d2d",
                blas.GpuCorr3dMM,
                num_groups=groups,
            )
            self.optimizer_3d(
                [imshp, kshp, tshp],
                0,
                "",
                "conv_gemm:alternative:conv3d2d",
                dnn.GpuDnnConv,
                num_groups=groups,
            )
            # grad with respect to weights
            self.optimizer_3d(
                [imshp, tshp, kshp],
                1,
                "",
                "conv_dnn:alternative:conv3d2d",
                blas.GpuCorr3dMM_gradWeights,
                num_groups=groups,
            )
            self.optimizer_3d(
                [imshp, tshp, kshp],
                1,
                "",
                "conv_gemm:alternative:conv3d2d",
                dnn.GpuDnnConvGradW,
                num_groups=groups,
            )
            # grad with respect to inputs
            self.optimizer_3d(
                [tshp, kshp, imshp],
                2,
                "",
                "conv_dnn:alternative:conv3d2d",
                blas.GpuCorr3dMM_gradInputs,
                num_groups=groups,
            )
            self.optimizer_3d(
                [tshp, kshp, imshp],
                2,
                "",
                "conv_gemm:alternative:conv3d2d",
                dnn.GpuDnnConvGradI,
                num_groups=groups,
            )

    @pytest.mark.skipif(config.cxx == "", reason="Need a c compiler.")
    def test_returns_none_2d(self):
        # values given don't matter since it returns None
        imshp = (2, 3, 5, 5)
        kshp = (4, 3, 3, 3)
        tshp = (2, 4, 3, 3)
        conv_direction = [0, 1, 2]
        optimisers = [
            [opt.local_abstractconv_gemm_alt, opt.local_abstractconv_cudnn_alt],
            [
                opt.local_abstractconv_gemm_gradweights_alt,
                opt.local_abstractconv_cudnn_alt,
            ],
            [
                opt.local_abstractconv_gradinputs_gemm_alt,
                opt.local_abstractconv_cudnn_alt,
            ],
        ]
        # test that non default subsample returns None
        for opt_direction, direction in zip(optimisers, conv_direction):
            for optimiser in opt_direction:
                self.optimizer_2d(
                    [imshp, kshp, tshp],
                    direction,
                    "",
                    "",
                    None,
                    subsample=(2, 2),
                    optimiser=optimiser,
                )
        # test that non default num_groups returns None
        for opt_direction, direction in zip(optimisers, conv_direction):
            for optimiser in opt_direction:
                self.optimizer_2d(
                    [imshp, kshp, tshp],
                    direction,
                    "",
                    "",
                    None,
                    num_groups=3,
                    optimiser=optimiser,
                )
        # test that border_mode=half returns None
        for opt_direction, direction in zip(optimisers, conv_direction):
            for optimiser in opt_direction:
                self.optimizer_2d(
                    [imshp, kshp, tshp],
                    direction,
                    "",
                    "",
                    None,
                    border_mode="half",
                    optimiser=optimiser,
                )
        # test that Non-default filter dilation return None for
        # direction 1
        for optimiser in optimisers[1]:
            self.optimizer_2d(
                [imshp, kshp, tshp],
                1,
                "",
                "",
                None,
                filter_dilation=(2, 2),
                optimiser=optimiser,
            )
        imshp = (2, 2, 4, 4)
        kshp = (2, 2, 2, 2, 3, 3)
        tshp = (2, 2, 2, 2)
        shape_perms = [[imshp, kshp, tshp], [imshp, tshp, kshp], [tshp, kshp, imshp]]
        # test unshared convolution returns None
        for opt_direction, direction, perms in zip(
            optimisers, conv_direction, shape_perms
        ):
            for optimiser in opt_direction:
                self.optimizer_2d(
                    perms, direction, "", "", None, unshared=True, optimiser=optimiser
                )

    @pytest.mark.skipif(config.cxx == "", reason="Need a c compiler.")
    def test_returns_none_3d(self):
        imshp = (2, 3, 5, 5, 5)
        kshp = (4, 3, 3, 3, 3)
        tshp = (2, 4, 3, 3, 3)
        conv_direction = [0, 1, 2]
        optimisers = [
            [opt.local_abstractconv3d_alt, opt.local_abstractconv3d_cudnn_alt],
            [
                opt.local_abstractconv3d_gemm_gradweights_alt,
                opt.local_abstractconv3d_cudnn_alt,
            ],
            [
                opt.local_abstractconv3d_gradinputs_gemm_alt,
                opt.local_abstractconv3d_cudnn_alt,
            ],
        ]
        # test that non default subsample returns None
        for opt_direction, direction in zip(optimisers, conv_direction):
            for optimiser in opt_direction:
                self.optimizer_3d(
                    [imshp, kshp, tshp],
                    direction,
                    "",
                    "",
                    None,
                    subsample=(2, 2, 2),
                    optimiser=optimiser,
                )
        # test that non default num_groups returns None
        for opt_direction, direction in zip(optimisers, conv_direction):
            for optimiser in opt_direction:
                self.optimizer_3d(
                    [imshp, kshp, tshp],
                    direction,
                    "",
                    "",
                    None,
                    num_groups=3,
                    optimiser=optimiser,
                )
        # test that border_mode=half returns None
        for opt_direction, direction in zip(optimisers, conv_direction):
            for optimiser in opt_direction:
                self.optimizer_3d(
                    [imshp, kshp, tshp],
                    direction,
                    "",
                    "",
                    None,
                    border_mode="half",
                    optimiser=optimiser,
                )
        # test that Non-default filter dilation return None for
        # direction 1
        for optimiser in optimisers[1]:
            self.optimizer_3d(
                [imshp, kshp, tshp],
                1,
                "",
                "",
                None,
                filter_dilation=(2, 2, 2),
                optimiser=optimiser,
            )
