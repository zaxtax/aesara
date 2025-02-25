import logging

import numpy as np

from aesara import tensor as at
from aesara.gpuarray.basic_ops import (
    as_gpuarray_variable,
    gpuarray_helper_inc_dir,
    infer_context_name,
)
from aesara.gpuarray.type import gpu_context_type
from aesara.gradient import grad_undefined
from aesara.graph.basic import Apply
from aesara.link.c.op import _NoPythonExternalCOp
from aesara.link.c.params_type import ParamsType
from aesara.scalar import bool as bool_t
from aesara.tensor import as_tensor_variable
from aesara.tensor.type import discrete_dtypes


_logger = logging.getLogger("aesara.gpuarray.blocksparse")


class GpuSparseBlockGemv(_NoPythonExternalCOp):
    """
    GPU version of SparseBlockGemv. Check SparseBlockGemv's docstring for more
    information.

    This should not be directly called since the interface is subject
    to change without notice.  Use the sandbox.blocksparse.sparse_block_dot()
    function for a stable interface.
    """

    __props__ = ("inplace",)
    params_type = ParamsType(inplace=bool_t, context=gpu_context_type)
    # NB: DTYPE_INPUT_* is used in C code, so I think we should not set check_input to False.

    def __init__(self, inplace=False):
        super().__init__("c_code/blockgemv.c", "APPLY_SPECIFIC(blockgemv)")
        self.inplace = inplace
        if self.inplace:
            self.destroy_map = {0: [0]}

    def get_params(self, node):
        return self.params_type.get_params(self, context=node.inputs[0].type.context)

    def c_header_dirs(self, **kwargs):
        return [gpuarray_helper_inc_dir()]

    def c_headers(self, **kwargs):
        return [
            "<gpuarray/buffer_blas.h>",
            "<gpuarray/buffer.h>",
            "<gpuarray_helper.h>",
        ]

    def make_node(self, o, W, h, inputIdx, outputIdx):
        ctx = infer_context_name(o, W, h)
        o = as_gpuarray_variable(o, ctx)
        W = as_gpuarray_variable(W, ctx)
        h = as_gpuarray_variable(h, ctx)
        inputIdx = as_tensor_variable(inputIdx)
        outputIdx = as_tensor_variable(outputIdx)
        assert o.ndim == 3
        assert W.ndim == 4
        assert h.ndim == 3
        assert inputIdx.ndim == 2
        assert outputIdx.ndim == 2

        assert inputIdx.type.dtype in discrete_dtypes
        assert outputIdx.type.dtype in discrete_dtypes

        return Apply(self, [o, W, h, inputIdx, outputIdx], [o.type()])

    def infer_shape(self, fgraph, node, input_shapes):
        return [input_shapes[0]]

    def grad(self, inputs, grads):
        o, W, h, inputIdx, outputIdx = inputs
        go = grads[0]

        Wgrad = gpu_sparse_block_outer(W.zeros_like(), h, go, inputIdx, outputIdx)
        hgrad = gpu_sparse_block_gemv(
            h.zeros_like(), W.dimshuffle((1, 0, 3, 2)), go, outputIdx, inputIdx
        )
        return [
            go,
            Wgrad,
            hgrad,
            grad_undefined(self, 3, inputIdx, "grad of inputIdx makes no sense"),
            grad_undefined(self, 4, outputIdx, "grad of outputIdx makes no sense"),
        ]


gpu_sparse_block_gemv = GpuSparseBlockGemv(False)
gpu_sparse_block_gemv_inplace = GpuSparseBlockGemv(True)


class GpuSparseBlockOuter(_NoPythonExternalCOp):
    """
    GPU version of SparseBlockOuter. See SparseBlockOuter's docstring for more
    information.

    This op should not be called directly since its interface is
    subject to change without notice.  It is involved in the gradient
    of GpuSparseBlockGemv. The gradient is not implemented.
    """

    __props__ = ("inplace",)
    params_type = ParamsType(inplace=bool_t, context=gpu_context_type)

    def __init__(self, inplace=False):
        super().__init__(["c_code/blockger.c"], "APPLY_SPECIFIC(blockger)")
        self.inplace = inplace
        if self.inplace:
            self.destroy_map = {0: [0]}

    def get_params(self, node):
        return self.params_type.get_params(self, context=node.inputs[0].type.context)

    def make_node(self, o, x, y, xIdx, yIdx, alpha=None):
        ctx = infer_context_name(o, x, y)
        one = at.constant(np.asarray(1.0, dtype="float32"))
        o = as_gpuarray_variable(o, ctx)
        x = as_gpuarray_variable(x, ctx)
        y = as_gpuarray_variable(y, ctx)
        xIdx = as_tensor_variable(xIdx)
        yIdx = as_tensor_variable(yIdx)
        if alpha is None:
            alpha = one
        return Apply(self, [o, x, y, xIdx, yIdx, alpha], [o.type()])

    def infer_shape(self, fgraph, node, input_shapes):
        return [input_shapes[0]]

    def c_header_dirs(self, **kwargs):
        return [gpuarray_helper_inc_dir()]

    def c_headers(self, **kwargs):
        return [
            "<gpuarray/buffer_blas.h>",
            "<gpuarray/buffer.h>",
            "<gpuarray_helper.h>",
        ]


gpu_sparse_block_outer = GpuSparseBlockOuter(False)
gpu_sparse_block_outer_inplace = GpuSparseBlockOuter(True)
