.. _aesara_ctype:


==========================================
Implementing C support for :class:`Type`\s
==========================================

How does it work?
=================

In order to be C-compatible, a :class:`Type` must provide a C interface to the
Python data that satisfy the constraints it puts forward. In other
words, it must define C code that can convert a Python reference into
some type suitable for manipulation in C and it must define C code
that can convert some C structure in which the C implementation of an
operation stores its variables into a reference to an object that can be
used from Python and is a valid value for the :class:`Type`.

For example, in the current example, we have a :class:`Type` which represents a
Python float. First, we will choose a corresponding C type. The
natural choice would be the primitive ``double`` type. Then, we need
to write code that will take a ``PyObject*``, check that it is a
Python ``float`` and extract its value as a ``double``. Finally, we
need to write code that will take a C ``double`` and will build a
``PyObject*`` of Python type ``float`` that we can work with from
Python. We will be using CPython and thus special care must be given
to making sure reference counts are updated properly!

The C code we will write makes use of CPython's C API which you can
find here_.

.. _here: http://docs.python.org/c-api/index.html


What needs to be defined
========================

In order to be C-compatible, the :class:`Type` subclass interface :class:`CType` must be used.
It defines several additional methods, which all start with the ``c_``
prefix. The complete list can be found in the documentation for
:class:`CType`. Here, we'll focus on the most important ones:


.. class:: CLinkerType

    .. method:: c_declare(name, sub, check_input=True)

        This must return C code which declares variables. These variables
        will be available to operations defined in C. You may also write
        typedefs.

    .. method:: c_init(name, sub)

        This must return C code which initializes the variables declared in
        :meth:`CLinkerType.c_declare`. Either this or :meth:`CLinkerType.c_extract` will be called.

    .. method:: c_extract(name, sub, check_input=True, **kwargs)

        This must return C code which takes a reference to a Python object
        and initializes the variables declared in :meth:`CLinkerType.c_declare` to match the
        Python object's data. Either this or :meth:`CLinkerType.c_init` will be called.

    .. method:: c_sync(name, sub)

        When the computations are done, transfer the variables from the C
        structure we put them in to the destination Python object. This will
        only be called for the outputs.

    .. method:: c_cleanup(name, sub)

        When we are done using the data, clean up whatever we allocated and
        decrease the appropriate reference counts.

    .. method:: c_headers([c_compiler])
                c_libraries([c_compiler])
                c_header_dirs([c_compiler])
                c_lib_dirs([c_compiler])

        Allows you to specify headers, libraries and associated directories.

        These methods have two versions, one with a :meth:`CLinkerType.c_compiler`
        argument and one without. The version with c_compiler is tried
        first and if it doesn't work, the one without is.

        The :meth:`CLinkerType.c_compiler` argument is the C compiler that will be used
        to compile the C code for the node that uses this type.

    .. method:: c_compile_args([c_compiler])
                c_no_compile_args([c_compiler])

        Allows to specify special compiler arguments to add/exclude.

        These methods have two versions, one with a :meth:`CLinkerType.c_compiler`
        argument and one without. The version with c_compiler is tried
        first and if it doesn't work, the one without is.

        The :meth:`CLinkerType.c_compiler` argument is the C compiler that will be used
        to compile the C code for the node that uses this type.

    .. method:: c_init_code()

        Allows you to specify code that will be executed once when the
        module is initialized, before anything else is executed.
        For instance, if a type depends on NumPy's C API, then
        ``'import_array();'`` has to be among the snippets returned
        by :meth:`CLinkerType.c_init_code`.

    .. method:: c_support_code()

        Allows to add helper functions/structs (in a string or a list of
        strings) that the :class:`Type` needs.

    .. method:: c_compiler()

        Allows to specify a special compiler. This will force this compiler for
        the current compilation block (a particular :class:`Op` or the full
        graph).  This is used for the GPU code.

    .. method:: c_code_cache_version()

       Should return a tuple of hashable objects like integers. This
       specifies the version of the code. It is used to cache the
       compiled code. You MUST change the returned tuple for each
       change in the code. If you don't want to cache the compiled code
       return an empty tuple or don't implement it.

    .. method:: c_element_type()

       Optional: should return the name of the primitive C type of
       for the variables handled by this Aesara type. For example,
       for a matrix of 32-bit signed NumPy integers, it should return
       ``"npy_int32"``. If C type may change from an instance to another
       (e.g. ``ScalarType('int32')`` vs ``ScalarType('int64')``), consider
       implementing this method. If C type is fixed across instances,
       this method may be useless (as you already know the C type
       when you work with the C code).

Each of these functions take two arguments, ``name`` and ``sub`` which
must be used to parameterize the C code they return. ``name`` is a
string which is chosen by the compiler to represent a :class:`Variable` of
the :class:`CType` in such a way that there are no name conflicts between
different pieces of data. Therefore, all variables declared in
:meth:`CType.c_declare` should have a name which includes ``name``. Furthermore,
the name of the variable containing a pointer to the Python object
associated to the :class:`Variable` is ``py_<name>``.

``sub``, on the other hand, is a dictionary containing bits of C code
suitable for use in certain situations. For instance, ``sub['fail']``
contains code that should be inserted wherever an error is identified.

:meth:`CType.c_declare` and :meth:`CType.c_extract` also accept a third ``check_input``
optional argument. If you want your type to validate its inputs, it must
only do it when ``check_input`` is True.

The example code below should help you understand how everything plays
out:

.. warning::
   If some error condition occurs and you want to fail and/or raise an
   exception, you must use the ``fail`` code contained in
   ``sub['fail']`` (there is an example in the definition of :meth:`CType.c_extract`
   below). You must *NOT* use the ``return`` statement anywhere, ever,
   nor ``break`` outside of your own loops or ``goto`` to strange
   places or anything like that. Failure to comply with this
   restriction could lead to erratic behavior, segfaults and/or memory
   leaks because Aesara defines its own cleanup system and assumes
   that you are not meddling with it. Furthermore, advanced operations
   or types might do code transformations on your code such as
   inserting it in a loop -- in that case they can call your
   code-generating methods with custom failure code that takes into account
   what they are doing!


Defining the methods
====================

**c_declare**

.. testcode::

    from aesara.link.c.type import Generic


    class double(Generic):
        def c_declare(self, name, sub, check_input=True):
            return """
            double %(name)s;
            """ % dict(name = name)


Very straightforward. All we need to do is write C code to declare a
double. That double will be named whatever is passed to our function
in the ``name`` argument. That will usually be some mangled name like
``"V0"``, ``"V2"`` or ``"V92"`` depending on how many nodes there are in the
computation graph and what rank the current node has. This function
will be called for all :class:`Variable`\s whose type is ``double``.

You can declare as many variables as you want there and you can also
do typedefs. Make sure that the name of each variable contains the
``name`` argument in order to avoid name collisions (collisions *will*
happen if you don't parameterize the variable names as indicated
here). Also note that you cannot declare a variable called
``py_<name>`` or ``storage_<name>`` because Aesara already defines
them.

What you declare there is basically the C interface you are giving to
your :class:`CType`. If you wish people to develop operations that make use of
it, it's best to publish it somewhere.


**c_init**

.. testcode::

        def c_init(self, name, sub):
            return """
            %(name)s = 0.0;
            """ % dict(name = name)

This function has to initialize the double we declared previously to a suitable
value. This is useful if we want to avoid dealing with garbage values,
especially if our data type is a pointer. This is not going to be called for all
:class:`Variable`\s with
the ``double`` type. Indeed, if a :class:`Variable` is an input that we pass
from Python, we will want to extract that input from a Python object,
therefore it is the :meth:`COp.c_extract` method that will be called instead of
:meth:`COp.c_init`. You can therefore not assume, when writing :meth:`COp.c_extract`, that the
initialization has been done (in fact you can assume that it *hasn't*
been done).

:meth:`COp.c_init` will typically be called on output :class:`Variable`\s, but in general
you should only assume that either :meth:`COp.c_init` or :meth:`COp.c_extract` has been
called, without knowing for sure which of the two.


**c_extract**

.. testcode::

        def c_extract(self, name, sub, check_input=True, **kwargs):
            return """
            if (!PyFloat_Check(py_%(name)s)) {
                PyErr_SetString(PyExc_TypeError, "expected a float");
                %(fail)s
            }
            %(name)s = PyFloat_AsDouble(py_%(name)s);
            """ % dict(name = name, fail = sub['fail'])

This method is slightly more sophisticated. What happens here is that
we have a reference to a Python object which Aesara has placed in
``py_%(name)s`` where ``%(name)s`` must be substituted for the name
given in the inputs. This special variable is declared by Aesara as
``PyObject* py_%(name)s`` where ``PyObject*`` is a pointer to a Python
object as defined by CPython's C API. This is the reference that
corresponds, on the Python side of things, to a :class:`Variable` with the
``double`` type. It is what the end user will give and what he or she
expects to get back.

In this example, the user will give a Python ``float``. The first
thing we should do is verify that what we got is indeed a Python
``float``. The ``PyFloat_Check`` function is provided by CPython's C
API and does this for us. If the check fails, we set an exception and
then we insert code for failure. The code for failure is in
``sub["fail"]`` and it basically does a ``goto`` to cleanup code.

If the check passes then we convert the Python float into a double
using the ``PyFloat_AsDouble`` function (yet again provided by CPython's C
API) and we put it in our double variable that we declared previously.


**c_sync**

.. testcode::

    def c_sync(name, sub):
        return """
        Py_XDECREF(py_%(name)s);
        py_%(name)s = PyFloat_FromDouble(%(name)s);
        if (!py_%(name)s) {
            printf("PyFloat_FromDouble failed on: %%f\\n", %(name)s);
            Py_XINCREF(Py_None);
            py_%(name)s = Py_None;
        }
        """ % dict(name = name)
    double.c_sync = c_sync

This function is probably the trickiest. What happens here is that we
have computed some operation on doubles and we have put the variable
into the double variable ``%(name)s``. Now, we need to put this data
into a Python object that we can manipulate on the Python side of
things. This Python object must be put into the ``py_%(name)s``
variable which Aesara recognizes (this is the same pointer we get in
:meth:`CType.c_extract`).

Now, that pointer is already a pointer to a valid Python object
(unless you or a careless implementer did terribly wrong things with
it). If we want to point to another object, we need to tell Python
that we don't need the old one anymore, meaning that we need to
*decrease the previous object's reference count*. The first line,
``Py_XDECREF(py_%(name)s)`` does exactly this. If it is forgotten,
Python will not be able to reclaim the data even if it is not used
anymore and there will be memory leaks! This is especially important
if the data you work on is large.

Now that we have decreased the reference count, we call
``PyFloat_FromDouble`` on our double variable in order to convert it
to a Python ``float``. This returns a new reference which we assign to
``py_%(name)s``. From there Aesara will do the rest and the end user
will happily see a Python ``float`` come out of his computations.

The rest of the code is not absolutely necessary and it is basically
"good practice". ``PyFloat_FromDouble`` can return ``NULL`` on failure.
``NULL`` is a pretty bad reference to have and neither Python nor Aesara
like it. If this happens, we change the ``NULL`` pointer (which will
cause us problems) to a pointer to ``None`` (which is *not* a ``NULL``
pointer). Since ``None`` is an object like the others, we need to
increase its reference count before we can set a new pointer to it. This
situation is unlikely to ever happen, but if it ever does, better safe
than sorry.

.. warning::
   If you are going to change the ``py_%(name)s`` pointer to point to a
   new reference, you *must* decrease the reference count of whatever
   it was pointing to before you do the change. This is only valid if
   you change the pointer, if you are not going to change the pointer,
   do *NOT* decrease its reference count!


**c_cleanup**

.. testcode::

    def c_cleanup(name, sub):
        return ""
    double.c_cleanup = c_cleanup

We actually have nothing to do here. We declared a double on the stack
so the C language will reclaim it for us when its scope ends. We
didn't ``malloc()`` anything so there's nothing to ``free()``. Furthermore,
the ``py_%(name)s`` pointer hasn't changed so we don't need to do
anything with it. Therefore, we have nothing to cleanup. Sweet!

There are however two important things to keep in mind:

First, note that :meth:`CType.c_sync` and :meth:`CType.c_cleanup` might be called in
sequence, so they need to play nice together. In particular, let's
say that you allocate memory in :meth:`CType.c_init` or :meth:`CType.c_extract` for some
reason. You might want to either embed what you allocated to some Python
object in :meth:`CType.c_sync` or to free it in :meth:`CType.c_cleanup`. If you do the
former, you don't want to free the allocated storage so you should set
the pointer to it to ``NULL`` to avoid that :meth:`CType.c_cleanup` mistakenly
frees it. Another option is to declare a variable in :meth:`CType.c_declare` that
you set to true in :meth:`CType.c_sync` to notify :meth:`CType.c_cleanup` that :meth:`CType.c_sync`
was called.

Second, whenever you use ``%(fail)s`` in :meth:`CType.c_extract` or in the code of an
:ref:`operation <op>`, you can count on :meth:`CType.c_cleanup` being called right
after that. Therefore, it's important to make sure that :meth:`CType.c_cleanup`
doesn't depend on any code placed after a reference to
``%(fail)s``. Furthermore, because of the way Aesara blocks code together,
only the variables declared in :meth:`CType.c_declare` will be visible in :meth:`CType.c_cleanup`!


What the generated C will look like
===================================

:meth:`CType.c_init` and :meth:`CType.c_extract` will only be called if there is a Python
object on which we want to apply computations using C
code. Conversely, :meth:`CType.c_sync` will only be called if we want to
communicate the values we have computed to Python, and :meth:`CType.c_cleanup`
will only be called when we don't need to process the data with C
anymore. In other words, the use of these functions for a given :class:`Variable`
depends on the the relationship between Python and C with respect to
that :class:`Variable`. For instance, imagine you define the following function
and call it:

.. code-block:: python

   x, y, z = double('x'), double('y'), double('z')
   a = add(x, y)
   b = mul(a, z)
   f = function([x, y, z], b)
   f(1.0, 2.0, 3.0)


Using the CLinker, the code that will be produced will look roughly
like this:

.. code-block:: c

   // BEGIN defined by Aesara
   PyObject* py_x = ...;
   PyObject* py_y = ...;
   PyObject* py_z = ...;
   PyObject* py_a = ...; // note: this reference won't actually be used for anything
   PyObject* py_b = ...;
   // END defined by Aesara

   {
     double x; //c_declare for x
     x = ...; //c_extract for x
     {
       double y; //c_declare for y
       y = ...; //c_extract for y
       {
         double z; //c_declare for z
         z = ...; //c_extract for z
         {
           double a; //c_declare for a
           a = 0; //c_init for a
           {
             double b; //c_declare for b
             b = 0; //c_init for b
             {
               a = x + y; //c_code for add
               {
                 b = a * z; //c_code for mul
               labelmul:
                 //c_cleanup for mul
               }
             labeladd:
               //c_cleanup for add
             }
           labelb:
             py_b = ...; //c_sync for b
             //c_cleanup for b
           }
         labela:
           //c_cleanup for a
         }
       labelz:
         //c_cleanup for z
       }
     labely:
       //c_cleanup for y
     }
   labelx:
     //c_cleanup for x
   }

It's not pretty, but it gives you an idea of how things work (note that
the variable names won't be ``x``, ``y``, ``z``, etc. - they will
get a unique mangled name). The ``fail`` code runs a ``goto`` to the
appropriate label in order to run all cleanup that needs to be
done. Note which variables get extracted (the three inputs ``x``, ``y`` and
``z``), which ones only get initialized (the temporary variable ``a`` and the
output ``b``) and which one is synced (the final output ``b``).

The C code above is a single C block for the whole graph. Depending on
which :term:`linker` is used to process the computation graph, it is
possible that one such block is generated for each operation and that
we transit through Python after each operation. In that situation,
``a`` would be synced by the addition block and extracted by the
multiplication block.


Final version
=============

.. testcode::

   from aesara.graph.type import

   class Double(Type):

       def filter(self, x, strict=False, allow_downcast=None):
           if strict and not isinstance(x, float):
               raise TypeError('Expected a float!')
           return float(x)

       def values_eq_approx(self, x, y, tolerance=1e-4):
           return abs(x - y) / (x + y) < tolerance

       def __str__(self):
           return "double"

       def c_declare(self, name, sub):
           return """
           double %(name)s;
           """ % dict(name = name)

       def c_init(self, name, sub):
           return """
           %(name)s = 0.0;
           """ % dict(name = name)

       def c_extract(self, name, sub, **kwargs):
           return """
           if (!PyFloat_Check(py_%(name)s)) {
               PyErr_SetString(PyExc_TypeError, "expected a float");
               %(fail)s
           }
           %(name)s = PyFloat_AsDouble(py_%(name)s);
           """ % dict(sub, name = name)

       def c_sync(self, name, sub):
           return """
           Py_XDECREF(py_%(name)s);
           py_%(name)s = PyFloat_FromDouble(%(name)s);
           if (!py_%(name)s) {
               printf("PyFloat_FromDouble failed on: %%f\\n", %(name)s);
               Py_XINCREF(Py_None);
               py_%(name)s = Py_None;
           }
           """ % dict(name = name)

       def c_cleanup(self, name, sub):
           return ""

   double = Double()


:class:`DeepCopyOp`
===================

We have an internal :class:`Op` called :class:`DeepCopyOp`. It is used to make sure we
respect the user vs. Aesara memory region as described in the :ref:`tutorial
<aliasing>`. Aesara has a Python implementation that calls the object's
``copy`` or ``deepcopy`` method for Aesara types for which it does not
know how to generate C code.

You can implement :meth:`COp.c_code` for this :class:`Op`. It is registered as follows:

.. code-block:: python

   aesara.compile.ops.register_deep_copy_op_c_code(YOUR_TYPE_CLASS, THE_C_CODE, version=())

In your C code, you should use ``%(iname)s`` and ``%(oname)s`` to represent
the C variable names of the :class:`DeepCopyOp` input and output
respectively. See an example for the type ``GpuArrayType`` (GPU
array) in the file ``aesara/gpuarray/type.py``. The version
parameter is what is returned by :meth:`DeepCopyOp.c_code_cache_version`. By
default, it will recompile the C code for each process.

:class:`ViewOp`
===============

We have an internal :class:`Op` called :class:`ViewOp`. It is used for some
verification of inplace/view :class:`Op`\s. Its C implementation increments and
decrements Python reference counts, and thus only works with Python
objects. If your new type represents Python objects, you should tell
:class:`ViewOp` to generate C code when working with this type, as
otherwise it will use Python code instead. This is achieved by
calling:

.. code-block:: python

   aesara.compile.ops.register_view_op_c_code(YOUR_TYPE_CLASS, THE_C_CODE, version=())


:class:`Shape` and :class:`Shape_i`
===================================

We have two generic :class:`Op`\s, :class:`Shape` and :class:`Shape_i`, that return the shape of any
Aesara :class:`Variable` that has a shape attribute (:class:`Shape_i` returns only one of
the elements of the shape).


.. code-block:: python

   from aesara.tensor.shape import register_shape_c_code, register_shape_i_c_code

   register_shape_c_code(YOUR_TYPE_CLASS, THE_C_CODE, version=())
   register_shape_i_c_code(YOUR_TYPE_CLASS, THE_C_CODE, CHECK_INPUT, version=())

The C code works as the :class:`ViewOp`. :class:`Shape_i` has the additional ``i`` parameter
that you can use with ``%(i)s``.

In your ``CHECK_INPUT``, you must check that the input has enough dimensions to
be able to access the ``i``-th one.
