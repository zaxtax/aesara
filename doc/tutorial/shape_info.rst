.. _shape_info:

==========================================
How Shape Information is Handled by Aesara
==========================================

Currently, information regarding shape is used in the following ways by Aesara:

- To remove computations in the graph when we only want to know the
  shape, but not the actual value of a variable. This is done with the
  :meth:`Op.infer_shape` method.

- To generate faster compiled code (e.g. for a 2D convolution).


Example:

>>> import aesara
>>> x = aesara.tensor.matrix('x')
>>> f = aesara.function([x], (x ** 2).shape)
>>> aesara.dprint(f)
MakeVector{dtype='int64'} [id A] ''   2
 |Shape_i{0} [id B] ''   1
 | |x [id C]
 |Shape_i{1} [id D] ''   0
   |x [id C]


The output of this compiled function does not contain any multiplication or
power computations; Aesara has removed them to compute the shape of the output
directly.

Aesara propagates information about shapes within a graph using specialized
:class:`Op`\s and static :class:`Type` information (see :ref:`aesara_type`).


Specifying Exact Shape
======================

Currently, specifying a shape is not as easy and flexible as we wish and we plan some
upgrade.  Here is the current state of what can be done:

- You can pass the shape info directly to the ``ConvOp`` created
  when calling ``conv2d``. You simply set the parameters ``image_shape``
  and ``filter_shape`` inside the call. They must be tuples of 4
  elements. For example:

.. code-block:: python

    aesara.tensor.nnet.conv2d(..., image_shape=(7, 3, 5, 5), filter_shape=(2, 3, 4, 4))

- You can use the ``SpecifyShape`` op to add shape information anywhere in the
  graph. This allows to perform some optimizations. In the following example,
  this makes it possible to precompute the Aesara function to a constant.


>>> import aesara
>>> x = aesara.tensor.matrix()
>>> x_specify_shape = aesara.tensor.specify_shape(x, (2, 2))
>>> f = aesara.function([x], (x_specify_shape ** 2).shape)
>>> aesara.printing.debugprint(f) # doctest: +NORMALIZE_WHITESPACE
DeepCopyOp [id A] ''   0
 |TensorConstant{(2,) of 2} [id B]

Problems with Shape inference
=============================

Sometimes this can lead to errors.  Consider this example:

>>> import numpy
>>> import aesara
>>> x = aesara.tensor.matrix('x')
>>> y = aesara.tensor.matrix('y')
>>> z = aesara.tensor.join(0, x, y)
>>> xv = numpy.random.rand(5, 4)
>>> yv = numpy.random.rand(3, 3)

>>> f = aesara.function([x, y], z.shape)
>>> aesara.printing.debugprint(f) # doctest: +NORMALIZE_WHITESPACE
MakeVector{dtype='int64'} [id A] ''   4
 |Elemwise{Add}[(0, 0)] [id B] ''   3
 | |Shape_i{0} [id C] ''   2
 | | |x [id D]
 | |Shape_i{0} [id E] ''   1
 |   |y [id F]
 |Shape_i{1} [id G] ''   0
   |x [id D]

>>> f(xv, yv) # DOES NOT RAISE AN ERROR AS SHOULD BE.
array([8, 4])

>>> f = aesara.function([x,y], z)# Do not take the shape.
>>> aesara.printing.debugprint(f) # doctest: +NORMALIZE_WHITESPACE
Join [id A] ''   0
 |TensorConstant{0} [id B]
 |x [id C]
 |y [id D]

>>> f(xv, yv)  # doctest: +ELLIPSIS
Traceback (most recent call last):
  ...
ValueError: ...

As you can see, when asking only for the shape of some computation (``join`` in the
example above), an inferred shape is computed directly, without executing
the computation itself (there is no ``join`` in the first output or debugprint).

This makes the computation of the shape faster, but it can also hide errors. In
this example, the computation of the shape of the output of ``join`` is done only
based on the first input Aesara variable, which leads to an error.

This might happen with other ops such as ``elemwise`` and ``dot``, for example.
Indeed, to perform some optimizations (for speed or stability, for instance),
Aesara assumes that the computation is correct and consistent
in the first place, as it does here.

You can detect those problems by running the code without this
optimization, using the Aesara flag
``optimizer_excluding=local_shape_to_shape_i``. You can also obtain the
same effect by running in the modes ``FAST_COMPILE`` (it will not apply this
optimization, nor most other optimizations) or ``DebugMode`` (it will test
before and after all optimizations (much slower)).
