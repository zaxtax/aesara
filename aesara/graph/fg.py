"""A container for specifying and manipulating a graph with distinct inputs and outputs."""
import time
from collections import OrderedDict
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
    cast,
)

from typing_extensions import Literal

import aesara
from aesara.configdefaults import config
from aesara.graph.basic import Apply, Constant, Node, Variable, applys_between
from aesara.graph.basic import as_string as graph_as_string
from aesara.graph.basic import clone_get_equiv, graph_inputs, io_toposort, vars_between
from aesara.graph.features import AlreadyThere, Feature, ReplaceValidate
from aesara.graph.utils import MetaObject, MissingInputError, TestValueError
from aesara.misc.ordered_set import OrderedSet


ApplyOrOutput = Union[Apply, Literal["output"]]
ClientType = Tuple[ApplyOrOutput, int]


class FunctionGraph(MetaObject):
    r"""
    A `FunctionGraph` represents a subgraph bound by a set of input variables and
    a set of output variables, ie a subgraph that specifies an Aesara function.
    The inputs list should contain all the inputs on which the outputs depend.
    `Variable`\s of type `Constant` are not counted as inputs.

    The `FunctionGraph` supports the replace operation which allows to replace
    a variable in the subgraph by another, e.g. replace ``(x + x).out`` by
    ``(2 * x).out``. This is the basis for optimization in Aesara.

    This class is also responsible for verifying that a graph is valid
    (ie, all the dtypes and broadcast patterns are compatible with the
    way the `Variable`\s are used) and for tracking the `Variable`\s with
    a :attr:`FunctionGraph.clients` ``dict`` that specifies which `Apply` nodes
    use the `Variable`.  The :attr:`FunctionGraph.clients` field, combined with
    the :attr:`Variable.owner` and each :attr:`Apply.inputs`, allows the graph
    to be traversed in both directions.

    It can also be extended with new features using
    :meth:`FunctionGraph.attach_feature`.  See `Feature` for event types and
    documentation.  Extra features allow the `FunctionGraph` to verify new
    properties of a graph as it is optimized.

    The constructor creates a `FunctionGraph` which operates on the subgraph
    bound by the inputs and outputs sets.

    This class keeps lists for the inputs and outputs and modifies them
    in-place.

    """

    def __init__(
        self,
        inputs: Optional[Sequence[Variable]] = None,
        outputs: Optional[Sequence[Variable]] = None,
        features: Optional[Sequence[Feature]] = None,
        clone: bool = True,
        update_mapping: Optional[Dict[Variable, Variable]] = None,
        memo: Optional[Dict[Variable, Variable]] = None,
        copy_inputs: bool = True,
        copy_orphans: bool = True,
    ):
        """
        Create a `FunctionGraph` which operates on the subgraph between the
        `inputs` and `outputs`.

        Parameters
        ----------
        inputs
            Input variables of the graph.
        outputs
            Output variables of the graph.
        clone
            If ``True``, the graph will be cloned.
        features
            A list of features to be added to the `FunctionGraph`.
        update_mapping
            Mapping between the `inputs` with updates and the `outputs`
            corresponding to their updates.
        memo
            See :func:`aesara.graph.basic.clone_get_equiv`.
        copy_inputs
            See :func:`aesara.graph.basic.clone_get_equiv`.
        copy_orphans
            See :func:`aesara.graph.basic.clone_get_equiv`.
        """
        if outputs is None:
            raise ValueError("No outputs specified")

        if inputs is None:
            inputs = [i for i in graph_inputs(outputs) if not isinstance(i, Constant)]

        if clone:
            _memo = clone_get_equiv(
                inputs,
                outputs,
                copy_inputs=copy_inputs,
                copy_orphans=copy_orphans,
                memo=cast(Dict[Node, Node], memo),
            )
            outputs = [cast(Variable, _memo[o]) for o in outputs]
            inputs = [cast(Variable, _memo[i]) for i in inputs]

        self.execute_callbacks_time: float = 0.0
        self.execute_callbacks_times: Dict[Feature, float] = {}

        if features is None:
            features = []

        self._features: List[Feature] = []

        # All apply nodes in the subgraph defined by inputs and
        # outputs are cached in this field
        self.apply_nodes: Set[Apply] = set()

        # Ditto for variable nodes.
        # It must contain all fgraph.inputs and all apply_nodes
        # outputs even if they aren't used in the graph.
        self.variables: Set[Variable] = set()

        self.inputs: List[Variable] = []
        self.outputs: List[Variable] = list(outputs)
        self.clients: Dict[Variable, List[ClientType]] = {}

        for f in features:
            self.attach_feature(f)

        self.attach_feature(ReplaceValidate())

        for in_var in inputs:
            if in_var.owner is not None:
                raise ValueError(
                    "One of the provided inputs is the output of "
                    "an already existing node. "
                    "If that is okay, either discard that "
                    "input's owner or use graph.clone."
                )

            self.add_input(in_var, check=False)

        for output in outputs:
            self.import_var(output, reason="init")
        for i, output in enumerate(outputs):
            self.clients[output].append(("output", i))

        self.profile = None
        self.update_mapping = update_mapping

    def add_input(self, var: Variable, check: bool = True) -> None:
        """Add a new variable as an input to this `FunctionGraph`.

        Parameters
        ----------
        var : aesara.graph.basic.Variable

        """
        if check and var in self.inputs:
            return

        self.inputs.append(var)
        self.setup_var(var)
        self.variables.add(var)

    def setup_var(self, var: Variable) -> None:
        """Set up a variable so it belongs to this `FunctionGraph`.

        Parameters
        ----------
        var : aesara.graph.basic.Variable

        """
        self.clients.setdefault(var, [])

    def get_clients(self, var: Variable) -> List[ClientType]:
        """Return a list of all the `(node, i)` pairs such that `node.inputs[i]` is `var`."""
        return self.clients[var]

    def add_client(self, var: Variable, new_client: ClientType) -> None:
        """Update the clients of `var` with `new_clients`.

        Parameters
        ----------
        var : Variable
            The `Variable` to be updated.
        new_client : (Apply, int)
            A ``(node, i)`` pair such that ``node.inputs[i]`` is `var`.

        """
        if not isinstance(new_client[0], Apply) and new_client[0] != "output":
            raise TypeError(
                'The first entry of `new_client` must be an `Apply` node or the string `"output"`'
            )
        self.clients[var].append(new_client)

    def remove_client(
        self,
        var: Variable,
        client_to_remove: ClientType,
        reason: Optional[str] = None,
    ) -> None:
        """Recursively remove clients of a variable.

        This is the main method to remove variables or `Apply` nodes from
        a `FunctionGraph`.

        This will remove `var` from the `FunctionGraph` if it doesn't have any
        clients remaining. If it has an owner and all the outputs of the owner
        have no clients, it will also be removed.

        Parameters
        ----------
        var : Variable
            The clients of `var` that will be removed.
        client_to_remove : pair of (Apply, int)
            A ``(node, i)`` pair such that ``node.inputs[i]`` will no longer be
            `var` in this `FunctionGraph`.

        """

        removal_stack = [(var, client_to_remove)]
        while removal_stack:
            var, client_to_remove = removal_stack.pop()

            try:
                var_clients = self.clients[var]
                var_clients.remove(client_to_remove)
            except ValueError:
                # In this case, the original `var` could've been removed from
                # the current `var`'s client list before this call.
                # There's nothing inherently wrong with that, so we continue as
                # if it were removed here.
                var_clients = None

            if var_clients:
                continue

            # Now, `var` has no more clients, so check if we need to remove it
            # and its `Apply` node
            if not var.owner:
                # The `var` is a `Constant` or an input without a client, so we
                # remove it
                self.variables.remove(var)
            else:
                apply_node = var.owner
                if not any(
                    output for output in apply_node.outputs if self.clients[output]
                ):
                    # The `Apply` node is not used and is not an output, so we
                    # remove it and its outputs
                    if not hasattr(apply_node.tag, "removed_by"):
                        apply_node.tag.removed_by = []

                    apply_node.tag.removed_by.append(str(reason))

                    self.apply_nodes.remove(apply_node)

                    self.variables.difference_update(apply_node.outputs)

                    self.execute_callbacks("on_prune", apply_node, reason)

                    for i, in_var in enumerate(apply_node.inputs):
                        removal_stack.append((in_var, (apply_node, i)))

    def import_var(
        self, var: Variable, reason: Optional[str] = None, import_missing: bool = False
    ) -> None:
        """Import variables into this `FunctionGraph`.

        This will also import the `variable`'s `Apply` node.

        Parameters
        ----------
        variable : aesara.graph.basic.Variable
            The variable to be imported.
        reason : str
            The name of the optimization or operation in progress.
        import_missing : bool
            Add missing inputs instead of raising an exception.

        """
        # Imports the owners of the variables
        if var.owner and var.owner not in self.apply_nodes:
            self.import_node(var.owner, reason=reason, import_missing=import_missing)
        elif (
            var.owner is None
            and not isinstance(var, Constant)
            and var not in self.inputs
        ):
            from aesara.graph.null_type import NullType

            if isinstance(var.type, NullType):
                raise TypeError(
                    f"Computation graph contains a NaN. {var.type.why_null}"
                )
            if import_missing:
                self.add_input(var)
            else:
                raise MissingInputError(f"Undeclared input: {var}", variable=var)
        self.setup_var(var)
        self.variables.add(var)

    def import_node(
        self,
        apply_node: Apply,
        check: bool = True,
        reason: Optional[str] = None,
        import_missing: bool = False,
    ) -> None:
        """Recursively import everything between an ``Apply`` node and the ``FunctionGraph``'s outputs.

        Parameters
        ----------
        apply_node : Apply
            The node to be imported.
        check : bool
            Check that the inputs for the imported nodes are also present in
            the `FunctionGraph`.
        reason : str
            The name of the optimization or operation in progress.
        import_missing : bool
            Add missing inputs instead of raising an exception.
        """
        # We import the nodes in topological order. We only are interested in
        # new nodes, so we use all variables we know of as if they were the
        # input set.  (The functions in the graph module only use the input set
        # to know where to stop going down.)
        new_nodes = io_toposort(self.variables, apply_node.outputs)

        if check:
            for node in new_nodes:
                for var in node.inputs:
                    if (
                        var.owner is None
                        and not isinstance(var, Constant)
                        and var not in self.inputs
                    ):
                        if import_missing:
                            self.add_input(var)
                        else:
                            error_msg = (
                                f"Input {node.inputs.index(var)} ({var})"
                                " of the graph (indices start "
                                f"from 0), used to compute {node}, was not "
                                "provided and not given a value. Use the "
                                "Aesara flag exception_verbosity='high', "
                                "for more information on this error."
                            )
                            raise MissingInputError(error_msg, variable=var)

        for node in new_nodes:
            assert node not in self.apply_nodes
            self.apply_nodes.add(node)
            if not hasattr(node.tag, "imported_by"):
                node.tag.imported_by = []
            node.tag.imported_by.append(str(reason))
            for output in node.outputs:
                self.setup_var(output)
                self.variables.add(output)
            for i, input in enumerate(node.inputs):
                if input not in self.variables:
                    self.setup_var(input)
                    self.variables.add(input)
                self.add_client(input, (node, i))
            self.execute_callbacks("on_import", node, reason)

    def change_node_input(
        self,
        node: ApplyOrOutput,
        i: int,
        new_var: Variable,
        reason: Optional[str] = None,
        import_missing: bool = False,
        check: bool = True,
    ) -> None:
        """Change ``node.inputs[i]`` to `new_var`.

        ``new_var.type.is_super(old_var.type)`` must be ``True``, where
        ``old_var`` is the current value of ``node.inputs[i]`` which we want to
        replace.

        For each feature that has an `on_change_input` method, this method calls:
        ``feature.on_change_input(function_graph, node, i, old_var, new_var, reason)``

        Parameters
        ----------
        node
            The node for which an input is to be changed.  If the value is
            the string ``"output"`` then the ``self.outputs`` will be used
            instead of ``node.inputs``.
        i
            The index in `node.inputs` that we want to change.
        new_var
            The new variable to take the place of ``node.inputs[i]``.
        import_missing
            Add missing inputs instead of raising an exception.
        check
            When ``True``, perform a type check between the variable being
            replaced and its replacement.  This is primarily used by the
            `History` `Feature`, which needs to revert types that have been
            narrowed and would otherwise fail this check.
        """
        # TODO: ERROR HANDLING FOR LISTENERS (should it complete the change or revert it?)
        if node == "output":
            r = self.outputs[i]
            if check and not r.type.is_super(new_var.type):
                raise TypeError(
                    f"The type of the replacement ({new_var.type}) must be "
                    f"compatible with the type of the original Variable ({r.type})."
                )
            self.outputs[i] = new_var
        else:
            assert isinstance(node, Apply)
            r = node.inputs[i]
            if check and not r.type.is_super(new_var.type):
                raise TypeError(
                    f"The type of the replacement ({new_var.type}) must be "
                    f"compatible with the type of the original Variable ({r.type})."
                )
            node.inputs[i] = new_var

        if r is new_var:
            return

        self.import_var(new_var, reason=reason, import_missing=import_missing)
        self.add_client(new_var, (node, i))
        self.remove_client(r, (node, i), reason=reason)
        # Precondition: the substitution is semantically valid However it may
        # introduce cycles to the graph, in which case the transaction will be
        # reverted later.
        self.execute_callbacks("on_change_input", node, i, r, new_var, reason=reason)

    def replace(
        self,
        var: Variable,
        new_var: Variable,
        reason: Optional[str] = None,
        verbose: Optional[bool] = None,
        import_missing: bool = False,
    ) -> None:
        """Replace a variable in the `FunctionGraph`.

        This is the main interface to manipulate the subgraph in `FunctionGraph`.
        For every node that uses `var` as input, makes it use `new_var` instead.

        Parameters
        ----------
        var
            The variable to be replaced.
        new_var
            The variable to replace `var`.
        reason
            The name of the optimization or operation in progress.
        verbose
            Print `reason`, `var`, and `new_var`.
        import_missing
            Import missing variables.

        """
        if verbose is None:
            verbose = config.optimizer_verbose
        if verbose:
            print(
                f"optimizer: rewrite {reason} replaces {var} of {var.owner} with {new_var} of {new_var.owner}"
            )

        new_var = var.type.filter_variable(new_var, allow_convert=True)

        if var not in self.variables:
            # TODO: Raise an actual exception here.
            # Old comment:
            # this variable isn't in the graph... don't raise an
            # exception here, just return silently because it makes it
            # easier to implement some optimizations for
            # multiple-output ops
            # raise ValueError()
            return

        if config.compute_test_value != "off":
            try:
                tval = aesara.graph.op.get_test_value(var)
                new_tval = aesara.graph.op.get_test_value(new_var)
            except TestValueError:
                pass
            else:
                tval_shape = getattr(tval, "shape", None)
                new_tval_shape = getattr(new_tval, "shape", None)
                if tval_shape != new_tval_shape:
                    raise AssertionError(
                        "The replacement variable has a test value with "
                        "a shape different from the original variable's "
                        f"test value. Original: {tval_shape}, new: {new_tval_shape}"
                    )

        for node, i in list(self.clients[var]):
            assert (node == "output" and self.outputs[i] is var) or (
                isinstance(node, Apply) and node.inputs[i] is var
            )
            self.change_node_input(
                node, i, new_var, reason=reason, import_missing=import_missing
            )

    def replace_all(self, pairs: Iterable[Tuple[Variable, Variable]], **kwargs) -> None:
        """Replace variables in the `FunctionGraph` according to ``(var, new_var)`` pairs in a list."""
        for var, new_var in pairs:
            self.replace(var, new_var, **kwargs)

    def attach_feature(self, feature: Feature) -> None:
        """Add a ``graph.features.Feature`` to this function graph and trigger its ``on_attach`` callback."""
        # Filter out literally identical `Feature`s
        if feature in self._features:
            return  # the feature is already present

        # Filter out functionally identical `Feature`s.
        # `Feature`s may use their `on_attach` method to raise
        # `AlreadyThere` if they detect that some
        # installed `Feature` does the same thing already
        attach = getattr(feature, "on_attach", None)
        if attach is not None:
            try:
                attach(self)
            except AlreadyThere:
                return
        self.execute_callbacks_times.setdefault(feature, 0.0)
        # It would be nice if we could require a specific class instead of
        # a "workalike" so we could do actual error checking
        # if not isinstance(feature, Feature):
        #    raise TypeError("Expected Feature instance, got "+\
        #            str(type(feature)))

        # Add the feature
        self._features.append(feature)

    def remove_feature(self, feature: Feature) -> None:
        """Remove a feature from the graph.

        Calls ``feature.on_detach(function_graph)`` if an ``on_detach`` method
        is defined.

        """
        try:
            # Why do we catch the exception anyway?
            self._features.remove(feature)
        except ValueError:
            return
        detach = getattr(feature, "on_detach", None)
        if detach is not None:
            detach(self)

    def execute_callbacks(self, name: str, *args, **kwargs) -> None:
        """Execute callbacks.

        Calls ``getattr(feature, name)(*args)`` for each feature which has
        a method called after name.

        """
        t0 = time.time()
        for feature in self._features:
            try:
                fn = getattr(feature, name)
            except AttributeError:
                # this is safe because there is no work done inside the
                # try; the AttributeError really must come from feature.${name}
                # not existing
                continue
            tf0 = time.time()
            fn(self, *args, **kwargs)
            self.execute_callbacks_times[feature] += time.time() - tf0
        self.execute_callbacks_time += time.time() - t0

    def collect_callbacks(self, name: str, *args) -> Dict[Feature, Any]:
        """Collects callbacks

        Returns a dictionary d such that ``d[feature] == getattr(feature, name)(*args)``
        For each feature which has a method called after name.
        """
        d = {}
        for feature in self._features:
            try:
                fn = getattr(feature, name)
            except AttributeError:
                continue
            d[feature] = fn(*args)
        return d

    def toposort(self) -> List[Apply]:
        r"""Return a toposorted list of the nodes.

        Return an ordering of the graph's :class:`Apply` nodes such that:

        * all the nodes of the inputs of a node are before that node, and
        * they satisfy the additional orderings provided by
          :meth:`FunctionGraph.orderings`.

        """
        if len(self.apply_nodes) < 2:
            # No sorting is necessary
            return list(self.apply_nodes)

        return io_toposort(self.inputs, self.outputs, self.orderings())

    def orderings(self) -> Dict[Apply, List[Apply]]:
        """Return a map of node to node evaluation dependencies.

        Each key node is mapped to a list of nodes that must be evaluated
        before the key nodes can be evaluated.

        This is used primarily by the :class:`DestroyHandler` :class:`Feature`
        to ensure that the clients of any destroyed inputs have already
        computed their outputs.

        Notes
        -----
        This only calls the :meth:`Feature.orderings` method of each
        :class:`Feature` attached to the :class:`FunctionGraph`. It does not
        take care of computing the dependencies by itself.

        """
        assert isinstance(self._features, list)
        all_orderings: List[OrderedDict] = []

        for feature in self._features:
            if hasattr(feature, "orderings"):
                orderings = feature.orderings(self)
                if not isinstance(orderings, OrderedDict):
                    raise TypeError(
                        "Non-deterministic return value from "
                        + str(feature.orderings)
                        + ". Nondeterministic object is "
                        + str(orderings)
                    )
                if len(orderings) > 0:
                    all_orderings.append(orderings)
                    for node, prereqs in orderings.items():
                        if not isinstance(prereqs, (list, OrderedSet)):
                            raise TypeError(
                                "prereqs must be a type with a "
                                "deterministic iteration order, or toposort "
                                " will be non-deterministic."
                            )
        if len(all_orderings) == 1:
            # If there is only 1 ordering, we reuse it directly.
            return all_orderings[0].copy()
        else:
            # If there is more than 1 ordering, combine them.
            ords: Dict[Apply, List[Apply]] = OrderedDict()
            for orderings in all_orderings:
                for node, prereqs in orderings.items():
                    ords.setdefault(node, []).extend(prereqs)
            return ords

    def check_integrity(self) -> None:
        """Check the integrity of nodes in the graph."""
        nodes = set(applys_between(self.inputs, self.outputs))
        if self.apply_nodes != nodes:
            nodes_missing = nodes.difference(self.apply_nodes)
            nodes_excess = self.apply_nodes.difference(nodes)
            raise Exception(
                "The nodes are inappropriately cached. missing, in excess: ",
                nodes_missing,
                nodes_excess,
            )
        for node in nodes:
            for i, variable in enumerate(node.inputs):
                clients = self.clients[variable]
                if (node, i) not in clients:
                    raise Exception(
                        f"Inconsistent clients list {(node, i)} in {clients}"
                    )
        variables = set(vars_between(self.inputs, self.outputs))
        if set(self.variables) != variables:
            vars_missing = variables.difference(self.variables)
            vars_excess = self.variables.difference(variables)
            raise Exception(
                "The variables are inappropriately cached. missing, in excess: ",
                vars_missing,
                vars_excess,
            )
        for variable in variables:
            if (
                variable.owner is None
                and variable not in self.inputs
                and not isinstance(variable, Constant)
            ):
                raise Exception(f"Undeclared input: {variable}")
            for cl_node, i in self.clients[variable]:
                if cl_node == "output":
                    if self.outputs[i] is not variable:
                        raise Exception(
                            f"Inconsistent clients list: {variable}, {self.outputs[i]}"
                        )
                    continue

                assert isinstance(cl_node, Apply)

                if cl_node not in nodes:
                    raise Exception(
                        f"Client not in FunctionGraph: {variable}, {(cl_node, i)}"
                    )
                if cl_node.inputs[i] is not variable:
                    raise Exception(
                        f"Inconsistent clients list: {variable}, {cl_node.inputs[i]}"
                    )

    def __repr__(self):
        return f"FunctionGraph({', '.join(graph_as_string(self.inputs, self.outputs))})"

    def clone(self, check_integrity=True) -> "FunctionGraph":
        """Clone the graph."""
        return self.clone_get_equiv(check_integrity)[0]

    def clone_get_equiv(
        self, check_integrity: bool = True, attach_feature: bool = True
    ) -> Tuple["FunctionGraph", Dict[Node, Node]]:
        """Clone the graph and return a ``dict`` that maps old nodes to new nodes.

        Parameters
        ----------
        check_integrity
            Whether to check integrity.
        attach_feature
            Whether to attach feature of origin graph to cloned graph.

        Returns
        -------
        e
            Cloned fgraph. Every node in cloned graph is cloned.
        equiv
            A ``dict`` that maps old nodes to the new nodes.
        """
        equiv = clone_get_equiv(self.inputs, self.outputs)

        if check_integrity:
            self.check_integrity()
        e = FunctionGraph(
            [cast(Variable, equiv[i]) for i in self.inputs],
            [cast(Variable, equiv[o]) for o in self.outputs],
            clone=False,
        )
        if check_integrity:
            e.check_integrity()

        if attach_feature:
            for feature in self._features:
                e.attach_feature(feature)
        return e, equiv

    def __getstate__(self):
        # This is needed as some features introduce instance methods
        # This is not picklable
        d = self.__dict__.copy()
        for feature in self._features:
            for attr in getattr(feature, "pickle_rm_attr", []):
                del d[attr]
        # The class Updater take fct as parameter and they are lambda function, so unpicklable.

        # execute_callbacks_times have reference to optimizer, and they can't
        # be pickled as the decorators with parameters aren't pickable.
        if "execute_callbacks_times" in d:
            del d["execute_callbacks_times"]

        return d

    def __setstate__(self, dct):
        self.__dict__.update(dct)
        for feature in self._features:
            if hasattr(feature, "unpickle"):
                feature.unpickle(self)

    def __contains__(self, item: Union[Variable, Apply]) -> bool:
        if isinstance(item, Variable):
            return item in self.variables
        elif isinstance(item, Apply):
            return item in self.apply_nodes
        else:
            raise TypeError()
