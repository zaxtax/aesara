"""
Provide a simple user friendly API to Aesara-managed memory.

"""

import copy
import logging

import numpy as np

from aesara.graph.basic import Variable
from aesara.graph.utils import add_tag_trace
from aesara.link.basic import Container
from aesara.link.c.type import generic


_logger = logging.getLogger("aesara.compile.sharedvalue")
__docformat__ = "restructuredtext en"


class SharedVariable(Variable):
    """
    Variable that is (defaults to being) shared between functions that
    it appears in.

    Parameters
    ----------
    name : str
        The name for this variable (see `Variable`).
    type : str
        The type for this variable (see `Variable`).
    value
        A value to associate with this variable (a new container will be
        created).
    strict
        True : assignments to .value will not be cast or copied, so they must
        have the correct type.
    allow_downcast
        Only applies if `strict` is False.
        True : allow assigned value to lose precision when cast during
        assignment.
        False : never allow precision loss.
        None : only allow downcasting of a Python float to a scalar floatX.
    container
        The container to use for this variable. Illegal to pass this as well as
        a value.

    Notes
    -----
    For more user-friendly constructor, see `shared`.

    """

    # Container object
    container = None
    """
    A container to use for this SharedVariable when it is an implicit
    function parameter.

    :type: `Container`
    """

    # default_update
    # If this member is present, its value will be used as the "update" for
    # this Variable, unless another update value has been passed to "function",
    # or the "no_default_updates" list passed to "function" contains it.

    def __init__(self, name, type, value, strict, allow_downcast=None, container=None):
        super().__init__(type=type, name=name, owner=None, index=None)

        if container is not None:
            self.container = container
            if (value is not None) or (strict is not None):
                raise TypeError(
                    "value and strict are ignored if you pass " "a container here"
                )
        else:
            self.container = Container(
                self,
                storage=[
                    type.filter(value, strict=strict, allow_downcast=allow_downcast)
                ],
                readonly=False,
                strict=strict,
                allow_downcast=allow_downcast,
            )

    def get_value(self, borrow=False, return_internal_type=False):
        """
        Get the non-symbolic value associated with this SharedVariable.

        Parameters
        ----------
        borrow : bool
            True to permit returning of an object aliased to internal memory.
        return_internal_type : bool
            True to permit the returning of an arbitrary type object used
            internally to store the shared variable.

        Only with borrow=False and return_internal_type=True does this function
        guarantee that you actually get the internal object.
        But in that case, you may get different return types when using
        different compute devices.

        """
        if borrow:
            return self.container.value
        else:
            return copy.deepcopy(self.container.value)

    def set_value(self, new_value, borrow=False):
        """
        Set the non-symbolic value associated with this SharedVariable.

        Parameters
        ----------
        borrow : bool
            True to use the new_value directly, potentially creating problems
            related to aliased memory.

        Changes to this value will be visible to all functions using
        this SharedVariable.

        Notes
        -----
        Set_value will work in-place on the GPU, if
        the following conditions are met:

            * The destination on the GPU must be c_contiguous.
            * The source is on the CPU.
            * The old value must have the same dtype as the new value
              (which is a given for now, since only float32 is
              supported).
            * The old and new value must have the same shape.
            * The old value is being completely replaced by the new
              value (not partially modified, e.g. by replacing some
              subtensor of it).

        It is also worth mentioning that, for efficient transfer to the GPU,
        Aesara will make the new data ``c_contiguous``. This can require an
        extra copy of the data on the host.

        The inplace on gpu memory work when borrow is either True or False.

        """
        if borrow:
            self.container.value = new_value
        else:
            self.container.value = copy.deepcopy(new_value)

    def get_test_value(self):
        return self.get_value(borrow=True, return_internal_type=True)

    def zero(self, borrow=False):
        """
        Set the values of a shared variable to 0.

        Parameters
        ----------
        borrow : bbol
            True to modify the value of a shared variable directly by using
            its previous value. Potentially this can cause problems
            regarding to the aliased memory.

        Changes done with this function will be visible to all functions using
        this SharedVariable.

        """
        if borrow:
            self.container.value[...] = 0
        else:
            self.container.value = 0 * self.container.value

    def clone(self):
        cp = self.__class__(
            name=self.name,
            type=self.type,
            value=None,
            strict=None,
            container=self.container,
        )
        cp.tag = copy.copy(self.tag)
        return cp

    def __getitem__(self, *args):
        # __getitem__ is not available for generic SharedVariable objects.
        # We raise a TypeError like Python would do if __getitem__ was not
        # implemented at all, but with a more explicit error message to help
        # Aesara users figure out the root of the problem more easily.
        value = self.get_value(borrow=True)
        if isinstance(value, np.ndarray):
            # Array probably had an unknown dtype.
            msg = (
                f"a Numpy array with dtype: '{value.dtype}'. This data type is not "
                "currently recognized by Aesara tensors: please cast "
                "your data into a supported numeric type if you need "
                "Aesara tensor functionalities."
            )
        else:
            msg = (
                f"an object of type: {type(value)}. Did you forget to cast it into "
                "a Numpy array before calling aesara.shared()?"
            )

        raise TypeError(
            "The generic 'SharedVariable' object is not subscriptable. "
            f"This shared variable contains {msg}"
        )

    def _value_get(self):
        raise Exception(
            "sharedvar.value does not exist anymore. Use "
            "sharedvar.get_value() or sharedvar.set_value()"
            " instead."
        )

    def _value_set(self, new_value):
        raise Exception(
            "sharedvar.value does not exist anymore. Use "
            "sharedvar.get_value() or sharedvar.set_value()"
            " instead."
        )

    # We keep this just to raise an error
    value = property(_value_get, _value_set)


def shared_constructor(ctor, remove=False):
    if remove:
        shared.constructors.remove(ctor)
    else:
        shared.constructors.append(ctor)
    return ctor


def shared(value, name=None, strict=False, allow_downcast=None, **kwargs):
    """Return a SharedVariable Variable, initialized with a copy or
    reference of `value`.

    This function iterates over constructor functions to find a
    suitable SharedVariable subclass.  The suitable one is the first
    constructor that accept the given value.  See the documentation of
    :func:`shared_constructor` for the definition of a constructor
    function.

    This function is meant as a convenient default.  If you want to use a
    specific shared variable constructor, consider calling it directly.

    ``aesara.shared`` is a shortcut to this function.

    .. attribute:: constructors

    A list of shared variable constructors that will be tried in reverse
    order.

    Notes
    -----
    By passing kwargs, you effectively limit the set of potential constructors
    to those that can accept those kwargs.

    Some shared variable have ``borrow`` as extra kwargs.

    Some shared variable have ``broadcastable`` as extra kwargs. As shared
    variable shapes can change, all dimensions default to not being
    broadcastable, even if ``value`` has a shape of 1 along some dimension.
    This parameter allows you to create for example a `row` or `column` 2d
    tensor.

    """

    try:
        if isinstance(value, Variable):
            raise TypeError(
                "Shared variable constructor needs numeric "
                "values and not symbolic variables."
            )

        for ctor in reversed(shared.constructors):
            try:
                var = ctor(
                    value,
                    name=name,
                    strict=strict,
                    allow_downcast=allow_downcast,
                    **kwargs,
                )
                add_tag_trace(var)
                return var
            except TypeError:
                continue
            # This may happen when kwargs were supplied
            # if kwargs were given, the generic_constructor won't be callable.
            #
            # This was done on purpose, the rationale being that if kwargs
            # were supplied, the user didn't want them to be ignored.

    except MemoryError as e:
        e.args = e.args + ("Consider using `aesara.shared(..., borrow=True)`",)
        raise

    raise TypeError(
        "No suitable SharedVariable constructor could be found."
        " Are you sure all kwargs are supported?"
        " We do not support the parameter dtype or type."
        f' value="{value}". parameters="{kwargs}"'
    )


shared.constructors = []


@shared_constructor
def generic_constructor(value, name=None, strict=False, allow_downcast=None):
    """
    SharedVariable Constructor.

    """
    return SharedVariable(
        type=generic,
        value=value,
        name=name,
        strict=strict,
        allow_downcast=allow_downcast,
    )
