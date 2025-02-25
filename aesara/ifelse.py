"""
IfElse introduces lazy evaluation in Aesara (coupled with the CVM/VM
linkers). It resembles the if clause of any programming language, that
has a `then` and `else` branch, and executes either one or the other
according to the condition provided.

This op differs from the already existent `switch` op, that evaluates both
branches of the clause and afterwards picks (according to the condition)
which value to report. Note also that `switch` is an elemwise operation (so
it picks each entry of a matrix according to the condition) while `ifelse`
is a global operation with a scalar condition.
"""

import logging
from copy import deepcopy
from typing import List, Sequence, Union

import numpy as np

import aesara.tensor as at
from aesara.compile import optdb
from aesara.configdefaults import config
from aesara.graph.basic import Apply, Variable, clone_replace, is_in_ancestors
from aesara.graph.op import _NoPythonOp
from aesara.graph.opt import GlobalOptimizer, in2out, local_optimizer
from aesara.tensor import basic
from aesara.tensor.shape import Reshape, Shape, SpecifyShape


__docformat__ = "restructedtext en"
__authors__ = (
    "Razvan Pascanu "
    "James Bergstra "
    "Dumitru Erhan "
    "David Warde-Farley"
    "PyMC Developers"
    "Aesara Developers"
)
__copyright__ = "(c) 2010, Universite de Montreal"

_logger = logging.getLogger("aesara.ifelse")


class IfElse(_NoPythonOp):
    """
    Op that provides conditional graph evaluation if used with the CVM/VM
    linkers. Note that there exist a helpful function `ifelse` that should
    be used to instantiate the op!

    According to a scalar condition `condition` the op evaluates and then
    returns all the tensors provided on the `then` branch, otherwise it
    evaluates and returns the tensors provided on the `else` branch. The op
    supports multiple tensors on each branch, with the condition that the same
    number of tensors are on the `then` as on the `else` and there is a one
    to one correspondence between them (shape and dtype wise).

    The `then` branch is defined as the first N tensors (after the
    condition), while the `else` branch is defined as the last N tensors.

    Example usage:

        ``rval = ifelse(condition, rval_if_true1, .., rval_if_trueN,
                        rval_if_false1, rval_if_false2, .., rval_if_falseN)``

    .. note:

        Other Linkers then CVM and VM are INCOMPATIBLE with this Op, and
        will ignore its lazy characteristic, computing both the True and
        False branch before picking one.

    """

    __props__ = ("as_view", "gpu", "n_outs")

    def __init__(self, n_outs, as_view=False, gpu=False, name=None):
        if as_view:
            # check destroyhandler and others to ensure that a view_map with
            # multiple inputs can work
            view_map = {}
            for idx in range(n_outs):
                view_map[idx] = [idx + 1]
            self.view_map = view_map
        self.as_view = as_view
        self.gpu = gpu
        self.n_outs = n_outs
        self.name = name

    def __eq__(self, other):
        if type(self) != type(other):
            return False
        if self.as_view != other.as_view:
            return False
        if self.gpu != other.gpu:
            return False
        if self.n_outs != other.n_outs:
            return False
        return True

    def __hash__(self):
        return hash((type(self), self.as_view, self.gpu, self.n_outs))

    def __str__(self):
        args = []
        if self.name is not None:
            args.append(self.name)
        if self.as_view:
            args.append("inplace")
        if self.gpu:
            args.append("gpu")
        return f"if{{{','.join(args)}}}"

    def infer_shape(self, fgraph, node, inputs_shapes):
        # By construction, corresponding then/else pairs have the same number
        # of dimensions

        ts_shapes = inputs_shapes[1:][: self.n_outs]
        fs_shapes = inputs_shapes[1:][self.n_outs :]
        # All elements of all shape tuples for the true and false outputs are
        # unpacked into the inputs of a separate ifelse, and then the outputs
        # of that ifelse are packed back into shape tuples.
        new_ts_inputs = []
        for ts_shape in ts_shapes:
            if isinstance(ts_shape, (list, tuple)):
                new_ts_inputs += list(ts_shape)
            else:
                # It can be None for generic objects
                return [None] * self.n_outs

        new_fs_inputs = []
        for fs_shape in fs_shapes:
            if isinstance(fs_shape, (list, tuple)):
                new_fs_inputs += list(fs_shape)
            else:
                # It can be None for generic objects
                return [None] * self.n_outs

        assert len(new_ts_inputs) == len(new_fs_inputs)
        if len(new_ts_inputs + new_fs_inputs) > 0:
            name_tokens = ["shape"]
            if self.name is not None:
                name_tokens.append(self.name)

            new_ifelse = IfElse(
                n_outs=len(new_ts_inputs),
                as_view=False,
                gpu=False,
                name="_".join(name_tokens),
            )
            new_outs = new_ifelse(
                node.inputs[0],
                *(new_ts_inputs + new_fs_inputs),
                return_list=True,
            )
        else:
            new_outs = []

        # generate pairs of shapes
        out_shapes = []
        for out in node.outputs:
            out_shapes.append(tuple(new_outs[: out.ndim]))
            new_outs = new_outs[out.ndim :]

        # new_outs should be an empty list after last iteration
        assert len(new_outs) == 0

        return out_shapes

    def make_node(self, c, *args):
        if len(args) != 2 * self.n_outs:
            raise ValueError(
                f"Wrong number of arguments to make_node: expected "
                f"{int(2 * self.n_outs)}, got {len(args)}"
            )
        c = at.basic.as_tensor_variable(c)
        if not self.gpu:
            # When gpu is true, we are given only gpuarrays, and we want
            # to keep them as gpuarrays
            nw_args = []
            for x in args:
                if isinstance(x, Variable):
                    nw_args.append(x)
                else:
                    nw_args.append(at.as_tensor_variable(x))
            args = nw_args
        aes = args[: self.n_outs]
        fs = args[self.n_outs :]

        for t, f in zip(aes, fs):
            # TODO: Attempt to convert types so that they match?
            # new_f = t.type.filter_variable(f)

            if not t.type.is_super(f.type):
                raise TypeError(
                    "IfElse requires compatible types for true and false return values: "
                    f"true_branch={t.type}, false_branch={f.type}"
                )
        if c.ndim > 0:
            raise TypeError(
                "Condition given to the op has to be a scalar "
                "with 0 standing for False, anything else "
                "for True"
            )
        return Apply(self, [c] + list(args), [t.type() for t in aes])

    def R_op(self, inputs, eval_points):
        return self(inputs[0], *eval_points[1:], return_list=True)

    def grad(self, ins, grads):
        aes = ins[1:][: self.n_outs]
        fs = ins[1:][self.n_outs :]
        if self.name is not None:
            nw_name_t = self.name + "_grad_t"
            nw_name_f = self.name + "_grad_f"
        else:
            nw_name_t = None
            nw_name_f = None
        if_true_op = IfElse(
            n_outs=self.n_outs, as_view=self.as_view, gpu=self.gpu, name=nw_name_t
        )

        if_false_op = IfElse(
            n_outs=self.n_outs, as_view=self.as_view, gpu=self.gpu, name=nw_name_f
        )

        # The grads can have a different dtype then the inputs.
        # As inputs true/false pair must have the same dtype,
        # we must cast the zeros to the corresponding grad dtype
        # and not the input dtype.
        if_true = (
            [ins[0]]
            + grads
            + [at.basic.zeros_like(t, dtype=grads[i].dtype) for i, t in enumerate(aes)]
        )
        if_false = (
            [ins[0]]
            + [at.basic.zeros_like(f, dtype=grads[i].dtype) for i, f in enumerate(fs)]
            + grads
        )

        condition = ins[0]
        # condition does affect the elements of the output so it is connected.
        # For the sake of making the gradient convenient we assume that
        # condition + epsilon always triggers the same branch as condition
        condition_grad = condition.zeros_like().astype(config.floatX)
        return (
            [condition_grad]
            + if_true_op(*if_true, return_list=True)
            + if_false_op(*if_false, return_list=True)
        )

    def make_thunk(self, node, storage_map, compute_map, no_recycling, impl=None):
        cond = node.inputs[0]
        aes = node.inputs[1:][: self.n_outs]
        fs = node.inputs[1:][self.n_outs :]
        outputs = node.outputs

        def thunk():
            if not compute_map[cond][0]:
                return [0]
            else:
                truthval = storage_map[cond][0]
                if truthval != 0:
                    ls = [
                        idx + 1
                        for idx in range(self.n_outs)
                        if not compute_map[aes[idx]][0]
                    ]
                    if len(ls) > 0:
                        return ls
                    else:
                        for out, t in zip(outputs, aes):
                            compute_map[out][0] = 1
                            val = storage_map[t][0]
                            if self.as_view:
                                storage_map[out][0] = val
                            # Work around broken numpy deepcopy
                            elif isinstance(val, (np.ndarray, np.memmap)):
                                storage_map[out][0] = val.copy()
                            else:
                                storage_map[out][0] = deepcopy(val)
                        return []
                else:
                    ls = [
                        1 + idx + self.n_outs
                        for idx in range(self.n_outs)
                        if not compute_map[fs[idx]][0]
                    ]
                    if len(ls) > 0:
                        return ls
                    else:
                        for out, f in zip(outputs, fs):
                            compute_map[out][0] = 1
                            # can't view both outputs unless destroyhandler
                            # improves
                            # Work around broken numpy deepcopy
                            val = storage_map[f][0]
                            if isinstance(val, (np.ndarray, np.memmap)):
                                storage_map[out][0] = val.copy()
                            else:
                                storage_map[out][0] = deepcopy(val)
                        return []

        thunk.lazy = True
        thunk.inputs = [storage_map[v] for v in node.inputs]
        thunk.outputs = [storage_map[v] for v in node.outputs]
        return thunk


def ifelse(
    condition: Variable,
    then_branch: Union[Variable, List[Variable]],
    else_branch: Union[Variable, List[Variable]],
    name: str = None,
) -> Union[Variable, Sequence[Variable]]:
    """
    This function corresponds to an if statement, returning (and evaluating)
    inputs in the ``then_branch`` if ``condition`` evaluates to True or
    inputs in the ``else_branch`` if ``condition`` evaluates to False.

    Parameters
    ==========
    condition
        ``condition`` should be a tensor scalar representing the condition.
        If it evaluates to 0 it corresponds to False, anything else stands
        for True.

    then_branch
        A single aesara variable or a list of aesara variables that the
        function should return as the output if ``condition`` evaluates to
        true. The number of variables should match those in the
        ``else_branch``, and there should be a one to one correspondence
        (type wise) with the tensors provided in the else branch

    else_branch
        A single aesara variable or a list of aesara variables that the
        function should return as the output if ``condition`` evaluates to
        false. The number of variables should match those in the then branch,
        and there should be a one to one correspondence (type wise) with the
        tensors provided in the then branch.

    Returns
    =======
        A sequence of aesara variables or a single variable (depending on the
        nature of the ``then_branch`` and ``else_branch``). More exactly if
        ``then_branch`` and ``else_branch`` is a tensor, then
        the return variable will be just a single variable, otherwise a
        sequence. The value returns correspond either to the values in the
        ``then_branch`` or in the ``else_branch`` depending on the value of
        ``condition``.
    """

    rval_type = None
    if isinstance(then_branch, (list, tuple)):
        rval_type = type(then_branch)
    else:
        then_branch = [then_branch]

    if not isinstance(else_branch, (list, tuple)):
        else_branch = [else_branch]

    # Some of the elements might be converted into another type,
    # we will store them in these new_... lists.
    new_then_branch = []
    new_else_branch = []
    for then_branch_elem, else_branch_elem in zip(then_branch, else_branch):
        if not isinstance(then_branch_elem, Variable):
            then_branch_elem = at.basic.as_tensor_variable(then_branch_elem)
        if not isinstance(else_branch_elem, Variable):
            else_branch_elem = at.basic.as_tensor_variable(else_branch_elem)

        # Make sure the types are compatible
        else_branch_elem = then_branch_elem.type.filter_variable(else_branch_elem)
        then_branch_elem = else_branch_elem.type.filter_variable(then_branch_elem)

        new_then_branch.append(then_branch_elem)
        new_else_branch.append(else_branch_elem)

    if len(then_branch) != len(else_branch):
        raise ValueError(
            "The number of values on the `then` branch"
            " should have the same number of variables as "
            "the `else` branch : (variables on `then` "
            f"{len(then_branch)}, variables on `else` "
            f"{len(else_branch)})"
        )

    new_ifelse = IfElse(n_outs=len(then_branch), as_view=False, gpu=False, name=name)

    ins = [condition] + list(new_then_branch) + list(new_else_branch)
    rval = new_ifelse(*ins, return_list=True)

    if rval_type is None:
        return rval[0]
    elif rval_type is list:
        return list(rval)
    else:
        return tuple(rval)


@local_optimizer([IfElse])
def cond_make_inplace(fgraph, node):
    op = node.op
    if (
        isinstance(op, IfElse)
        and not op.as_view
        and
        # For big graph, do not make inplace scalar to speed up
        # optimization.
        (
            len(fgraph.apply_nodes) < 500
            or not all(getattr(o.type, "ndim", -1) == 0 for o in node.outputs)
        )
    ):
        return IfElse(n_outs=op.n_outs, as_view=True, gpu=op.gpu, name=op.name)(
            *node.inputs, return_list=True
        )
    return False


optdb.register(
    "cond_make_inplace",
    in2out(cond_make_inplace, ignore_newtrees=True),
    "fast_run",
    "inplace",
    position=95,
)

# XXX: Optimizations commented pending further debugging (certain optimizations
# make computation less lazy than it should be currently).
#
# ifelse_equilibrium = graph.optdb.EquilibriumDB()
# ifelse_seqopt = graph.optdb.SequenceDB()
# ifelse_equilibrium.register('seq_ifelse', ifelse_seqopt, 'fast_run',
#                             'ifelse')
""" Comments:
I've wrote this comments to explain how the optimization of ifelse function
(for future developers that need to parse this part of code. Please try to
keep this comments in sync with whatever changes you add to the code.

ifelse optimization are registered before canonicalize !

The optimizations are called in sequence as follows:
    * equilibrium shell (runs until no change):
        * ifelse_lift
        * ifelse_merge_ifs
        * ifelse_merge_nodes
        * ifelse_remove_identical_inside
        * ifelse_sameCondTrue_inside
        * ifelse_sameCondFalse_inside
    * merge_nodes_1
    * ifelse_sameCondTrue
    * ifelse_sameCondFalse
    * ifelse_removeIdentical

where, each of the optimization do the following things:
    `ifelse_lift` (def cond_lift_single_if):

"""
# optdb.register('ifelse_equilibriumOpt', ifelse_equilibrium, 'fast_run',
#                'ifelse', position=.5)


acceptable_ops = (
    Shape,
    SpecifyShape,
    Reshape,
    basic.Rebroadcast,
    at.math.Dot,
    at.math.MaxAndArgmax,
    at.subtensor.Subtensor,
    at.subtensor.IncSubtensor,
    at.basic.Alloc,
    at.elemwise.Elemwise,
    at.elemwise.DimShuffle,
)


@local_optimizer(acceptable_ops)
def ifelse_lift_single_if_through_acceptable_ops(fgraph, main_node):
    """This optimization lifts up certain ifelse instances.

        op(ifelse(c, x, y)) -> ifelse(c, op(x), op(y))

    if `op` is in the `acceptable_ops` list, and there is no other if as
    input to that specific `op`, and the if has no other clients !?
    """
    if not (isinstance(main_node.op, acceptable_ops)):
        return False
    all_inp_nodes = set()
    for inp in main_node.inputs:
        all_inp_nodes.add(inp.owner)
    ifnodes = [x for x in list(all_inp_nodes) if x and isinstance(x.op, IfElse)]
    # if we have multiple ifs as inputs .. it all becomes quite complicated
    # :)
    if len(ifnodes) != 1:
        return False
    node = ifnodes[0]
    op = node.op

    aes = node.inputs[1:][: op.n_outs]
    fs = node.inputs[1:][op.n_outs :]

    # outs = main_node.outputs
    mop = main_node.op
    true_ins = []
    false_ins = []

    for x in main_node.inputs:
        if x in node.outputs:
            idx = node.outputs.index(x)
            true_ins.append(aes[idx])
            false_ins.append(fs[idx])
        else:
            true_ins.append(x)
            false_ins.append(x)
    true_eval = mop(*true_ins, return_list=True)
    false_eval = mop(*false_ins, return_list=True)
    # true_eval  = clone_replace(outs, replace = dict(zip(node.outputs, aes)))
    # false_eval = clone_replace(outs, replace = dict(zip(node.outputs, fs)))

    nw_outs = ifelse(node.inputs[0], true_eval, false_eval, return_list=True)
    return nw_outs


@local_optimizer([IfElse])
def cond_merge_ifs_true(fgraph, node):
    op = node.op
    if not isinstance(op, IfElse):
        return False
    t_ins = node.inputs[1:][: op.n_outs]

    replace = {}
    for idx, tval in enumerate(t_ins):
        if (
            tval.owner
            and isinstance(tval.owner.op, IfElse)
            and tval.owner.inputs[0] == node.inputs[0]
        ):
            ins_op = tval.owner.op
            ins_t = tval.owner.inputs[1:][: ins_op.n_outs]
            replace[idx + 1] = ins_t[tval.owner.outputs.index(tval)]

    if len(replace) == 0:
        return False

    old_ins = list(node.inputs)
    for pos, var in replace.items():
        old_ins[pos] = var
    return op(*old_ins, return_list=True)


@local_optimizer([IfElse])
def cond_merge_ifs_false(fgraph, node):
    op = node.op
    if not isinstance(op, IfElse):
        return False
    f_ins = node.inputs[1:][op.n_outs :]

    replace = {}
    for idx, fval in enumerate(f_ins):
        if (
            fval.owner
            and isinstance(fval.owner.op, IfElse)
            and fval.owner.inputs[0] == node.inputs[0]
        ):
            ins_op = fval.owner.op
            ins_t = fval.owner.inputs[1:][ins_op.n_outs :]
            replace[idx + 1 + op.n_outs] = ins_t[fval.owner.outputs.index(fval)]

    if len(replace) == 0:
        return False

    old_ins = list(node.inputs)
    for pos, var in replace.items():
        old_ins[pos] = var
    return op(*old_ins, return_list=True)


class CondMerge(GlobalOptimizer):
    """ Graph Optimizer that merges different cond ops """

    def add_requirements(self, fgraph):
        from aesara.graph.features import ReplaceValidate

        fgraph.add_feature(ReplaceValidate())

    def apply(self, fgraph):
        nodelist = list(fgraph.toposort())
        cond_nodes = [s for s in nodelist if isinstance(s.op, IfElse)]
        if len(cond_nodes) < 2:
            return False
        merging_node = cond_nodes[0]
        for proposal in cond_nodes[1:]:
            if proposal.inputs[0] == merging_node.inputs[0] and not is_in_ancestors(
                proposal, merging_node
            ):
                # Create a list of replacements for proposal
                mn_ts = merging_node.inputs[1:][: merging_node.op.n_outs]
                mn_fs = merging_node.inputs[1:][merging_node.op.n_outs :]
                pl_ts = proposal.inputs[1:][: proposal.op.n_outs]
                pl_fs = proposal.inputs[1:][proposal.op.n_outs :]
                new_ins = [merging_node.inputs[0]] + mn_ts + pl_ts + mn_fs + pl_fs
                mn_name = "?"
                if merging_node.op.name:
                    mn_name = merging_node.op.name
                pl_name = "?"
                # mn_n_ts = len(mn_ts)
                # mn_n_fs = len(mn_fs)
                if proposal.op.name:
                    pl_name = proposal.op.name
                new_ifelse = IfElse(
                    n_outs=len(mn_ts + pl_ts),
                    as_view=False,
                    gpu=False,
                    name=mn_name + "&" + pl_name,
                )
                new_outs = new_ifelse(*new_ins, return_list=True)
                new_outs = [clone_replace(x) for x in new_outs]
                old_outs = []
                if not isinstance(merging_node.outputs, (list, tuple)):
                    old_outs += [merging_node.outputs]
                else:
                    old_outs += merging_node.outputs
                if not isinstance(proposal.outputs, (list, tuple)):
                    old_outs += [proposal.outputs]
                else:
                    old_outs += proposal.outputs
                pairs = list(zip(old_outs, new_outs))
                fgraph.replace_all_validate(pairs, reason="cond_merge")


@local_optimizer([IfElse])
def cond_remove_identical(fgraph, node):
    op = node.op

    if not isinstance(op, IfElse):
        return False
    aes = node.inputs[1:][: op.n_outs]
    fs = node.inputs[1:][op.n_outs :]

    # sync outs
    out_map = {}
    for idx in range(len(node.outputs)):
        if idx not in out_map:
            for jdx in range(idx + 1, len(node.outputs)):
                if aes[idx] == aes[jdx] and fs[idx] == fs[jdx] and jdx not in out_map:
                    out_map[jdx] = idx

    if len(out_map) == 0:
        return False

    nw_ts = []
    nw_fs = []
    inv_map = {}
    pos = 0
    for idx in range(len(node.outputs)):
        if idx not in out_map:
            inv_map[idx] = pos
            pos = pos + 1
            nw_ts.append(aes[idx])
            nw_fs.append(fs[idx])

    new_ifelse = IfElse(n_outs=len(nw_ts), as_view=op.as_view, gpu=op.gpu, name=op.name)

    new_ins = [node.inputs[0]] + nw_ts + nw_fs
    new_outs = new_ifelse(*new_ins, return_list=True)

    rval = []
    for idx in range(len(node.outputs)):
        if idx in out_map:
            rval += [new_outs[inv_map[out_map[idx]]]]
        else:
            rval += [new_outs[inv_map[idx]]]

    return rval


@local_optimizer([IfElse])
def cond_merge_random_op(fgraph, main_node):
    if isinstance(main_node.op, IfElse):
        return False

    all_inp_nodes = set()
    for inp in main_node.inputs:
        all_inp_nodes.add(inp.owner)
    cond_nodes = [x for x in list(all_inp_nodes) if x and isinstance(x.op, IfElse)]

    if len(cond_nodes) < 2:
        return False

    merging_node = cond_nodes[0]
    for proposal in cond_nodes[1:]:
        if (
            proposal.inputs[0] == merging_node.inputs[0]
            and not is_in_ancestors(proposal, merging_node)
            and not is_in_ancestors(merging_node, proposal)
        ):
            # Create a list of replacements for proposal
            mn_ts = merging_node.inputs[1:][: merging_node.op.n_outs]
            mn_fs = merging_node.inputs[1:][merging_node.op.n_outs :]
            pl_ts = proposal.inputs[1:][: proposal.op.n_outs]
            pl_fs = proposal.inputs[1:][proposal.op.n_outs :]
            new_ins = [merging_node.inputs[0]] + mn_ts + pl_ts + mn_fs + pl_fs
            mn_name = "?"
            if merging_node.op.name:
                mn_name = merging_node.op.name
            pl_name = "?"
            # mn_n_ts = len(mn_ts)
            # mn_n_fs = len(mn_fs)
            if proposal.op.name:
                pl_name = proposal.op.name
            new_ifelse = IfElse(
                n_outs=len(mn_ts + pl_ts),
                as_view=False,
                gpu=False,
                name=mn_name + "&" + pl_name,
            )
            new_outs = new_ifelse(*new_ins, return_list=True)
            old_outs = []
            if not isinstance(merging_node.outputs, (list, tuple)):
                old_outs += [merging_node.outputs]
            else:
                old_outs += merging_node.outputs
            if not isinstance(proposal.outputs, (list, tuple)):
                old_outs += [proposal.outputs]
            else:
                old_outs += proposal.outputs
            pairs = list(zip(old_outs, new_outs))
            main_outs = clone_replace(main_node.outputs, replace=pairs)
            return main_outs


# XXX: Optimizations commented pending further debugging (certain optimizations
# make computation less lazy than it should be currently).
#
# pushout_equilibrium = graph.optdb.EquilibriumDB()
#
# XXX: This optimization doesn't seem to exist anymore?
# pushout_equilibrium.register("cond_lift_single_if",
#                              in2out(cond_lift_single_if,
#                                         ignore_newtrees=True),
#                              'fast_run', 'ifelse')
#
# pushout_equilibrium.register("cond_merge_random_op",
#                              in2out(cond_merge_random_op,
#                                         ignore_newtrees=True),
#                              'fast_run', 'ifelse')
#
#
# pushout_equilibrium.register("ifelse_merge",
#                              graph.opt.MergeOptimizer(skip_const_merge=False),
#                              'fast_run', 'ifelse')
#
# pushout_equilibrium.register("ifelse_remove_identical_inside",
#                              in2out(cond_remove_identical,
#                                         ignore_newtrees=True),
#                              'fast_run', 'ifelse')
#
# pushout_equilibrium.register('ifelse_sameCondTrue_inside',
#                              in2out(cond_merge_ifs_true,
#                                         ignore_newtrees=True),
#                              'fast_run', 'ifelse')
#
# pushout_equilibrium.register('ifelse_sameCondFalse_inside',
#                              in2out(cond_merge_ifs_false,
#                                         ignore_newtrees=True),
#                              'fast_run', 'ifelse')
#
# ifelse_seqopt.register('ifelse_condPushOut_equilibrium',
#                        pushout_equilibrium,
#                        'fast_run', 'ifelse', position=1)
#
# ifelse_seqopt.register('merge_nodes_1',
#                        graph.opt.MergeOptimizer(skip_const_merge=False),
#                        'fast_run', 'ifelse', position=2)
#
#
# ifelse_seqopt.register('ifelse_sameCondTrue',
#                        in2out(cond_merge_ifs_true,
#                                   ignore_newtrees=True),
#                        'fast_run', 'ifelse', position=3)
#
#
# ifelse_seqopt.register('ifelse_sameCondFalse',
#                        in2out(cond_merge_ifs_false,
#                                   ignore_newtrees=True),
#                        'fast_run', 'ifelse', position=4)
#
#
# ifelse_seqopt.register('ifelse_removeIdenetical',
#                        in2out(cond_remove_identical,
#                                   ignore_newtrees=True),
#                        'fast_run', 'ifelse', position=7)
