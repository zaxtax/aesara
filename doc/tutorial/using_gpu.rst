.. _using_gpu:

=============
Using the GPU
=============

For an introductory discussion of *Graphical Processing Units* (GPU)
and their use for intensive parallel computation purposes, see `GPGPU
<http://en.wikipedia.org/wiki/GPGPU>`_.

One of Aesara's design goals is to specify computations at an abstract
level, so that the internal function compiler has a lot of flexibility
about how to carry out those computations.  One of the ways we take
advantage of this flexibility is in carrying out calculations on a
graphics card.

Using the GPU in Aesara is as simple as setting the ``device``
configuration flag to ``device=cuda``. You can optionally target a
specific gpu by specifying the number of the gpu as in
e.g. ``device=cuda2``.  It is also encouraged to set the floating
point precision to float32 when working on the GPU as that is usually
much faster.  For example:
``AESARA_FLAGS='device=cuda,floatX=float32'``.  You can also set these
options in the .aesararc file's ``[global]`` section:

     .. code-block:: cfg

        [global]
        device = cuda
        floatX = float32

.. note::

    * If your computer has multiple GPUs and you use ``device=cuda``,
      the driver selects the one to use (usually cuda0).
    * You can use the program ``nvidia-smi`` to change this policy.
    * By default, when ``device`` indicates preference for GPU computations,
      Aesara will fall back to the CPU if there is a problem with the GPU.
      You can use the flag ``force_device=True`` to instead raise an error when
      Aesara cannot use the GPU.

.. _gpuarray:

GpuArray Backend
----------------

If you have not done so already, you will need to install libgpuarray
as well as at least one computing toolkit (CUDA or OpenCL). Detailed
instructions to accomplish that are provided at
`libgpuarray <http://deeplearning.net/software/libgpuarray/installation.html>`_.

To install Nvidia's GPU-programming toolchain (CUDA) and configure
Aesara to use it, see the installation instructions for
:ref:`Linux <gpu_linux>`, :ref:`MacOS <gpu_macos>` and :ref:`Windows <gpu_windows>`.

While all types of devices are supported if using OpenCL, for the
remainder of this section, whatever compute device you are using will
be referred to as GPU.

.. note::
  GpuArray backend uses ``config.gpuarray__preallocate`` for GPU memory
  allocation.

.. warning::

  The backend was designed to support OpenCL, however current support is
  incomplete. A lot of very useful ops still do not support it because they
  were ported from the old backend with minimal change.

  .. _testing_the_gpu:

Testing Aesara with GPU
~~~~~~~~~~~~~~~~~~~~~~~

To see if your GPU is being used, cut and paste the following program
into a file and run it.

Use the Aesara flag ``device=cuda`` to require the use of the GPU. Use the flag
``device=cuda{0,1,...}`` to specify which GPU to use.

.. testcode::

  from aesara import function, config, shared, tensor as at
  import numpy
  import time

  vlen = 10 * 30 * 768  # 10 x #cores x # threads per core
  iters = 1000

  rng = numpy.random.RandomState(22)
  x = shared(numpy.asarray(rng.rand(vlen), config.floatX))
  f = function([], at.exp(x))
  print(f.maker.fgraph.toposort())
  t0 = time.time()
  for i in range(iters):
      r = f()
  t1 = time.time()
  print("Looping %d times took %f seconds" % (iters, t1 - t0))
  print("Result is %s" % (r,))
  if numpy.any([isinstance(x.op, aesara.tensor.elemwise.Elemwise) and
                ('Gpu' not in type(x.op).__name__)
                for x in f.maker.fgraph.toposort()]):
      print('Used the cpu')
  else:
      print('Used the gpu')

The program just computes ``exp()`` of a bunch of random numbers.  Note
that we use the :func:`aesara.shared` function to make sure that the
input *x* is stored on the GPU.

.. testoutput::
   :hide:
   :options: +ELLIPSIS

   [Elemwise{exp,no_inplace}(<TensorType(float64, (None,))>)]
   Looping 1000 times took ... seconds
   Result is ...
   Used the cpu

.. code-block:: none

  $ AESARA_FLAGS=device=cpu python gpu_tutorial1.py
  [Elemwise{exp,no_inplace}(<TensorType(float64, (None,))>)]
  Looping 1000 times took 2.271284 seconds
  Result is [ 1.23178032  1.61879341  1.52278065 ...,  2.20771815  2.29967753
    1.62323285]
  Used the cpu

  $ AESARA_FLAGS=device=cuda0 python gpu_tutorial1.py
  Using cuDNN version 5105 on context None
  Mapped name None to device cuda0: GeForce GTX 750 Ti (0000:07:00.0)
  [GpuElemwise{exp,no_inplace}(<GpuArrayType<None>(float64, (False,))>), HostFromGpu(gpuarray)(GpuElemwise{exp,no_inplace}.0)]
  Looping 1000 times took 1.697514 seconds
  Result is [ 1.23178032  1.61879341  1.52278065 ...,  2.20771815  2.29967753
    1.62323285]
  Used the gpu


Returning a Handle to Device-Allocated Data
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

By default functions that execute on the GPU still return a standard
numpy ndarray.  A transfer operation is inserted just before the
results are returned to ensure a consistent interface with CPU code.
This allows changing the device some code runs on by only replacing
the value of the ``device`` flag without touching the code.

If you don't mind a loss of flexibility, you can ask aesara to return
the GPU object directly.  The following code is modified to do just that.

.. testcode::

  from aesara import function, config, shared, tensor as at
  import numpy
  import time

  vlen = 10 * 30 * 768  # 10 x #cores x # threads per core
  iters = 1000

  rng = numpy.random.RandomState(22)
  x = shared(numpy.asarray(rng.rand(vlen), config.floatX))
  f = function([], at.exp(x).transfer(None))
  print(f.maker.fgraph.toposort())
  t0 = time.time()
  for i in range(iters):
      r = f()
  t1 = time.time()
  print("Looping %d times took %f seconds" % (iters, t1 - t0))
  print("Result is %s" % (numpy.asarray(r),))
  if numpy.any([isinstance(x.op, aesara.tensor.elemwise.Elemwise) and
                ('Gpu' not in type(x.op).__name__)
                for x in f.maker.fgraph.toposort()]):
      print('Used the cpu')
  else:
      print('Used the gpu')

Here ``at.exp(x).transfer(None)`` means "copy ``exp(x)`` to the GPU",
with ``None`` the default GPU context when not explicitly given.
For information on how to set GPU contexts, see :ref:`tut_using_multi_gpu`.

The output is

.. testoutput::
   :hide:
   :options: +ELLIPSIS, +SKIP

   $ AESARA_FLAGS=device=cuda0 python gpu_tutorial2.py
   Using cuDNN version 5105 on context None
   Mapped name None to device cuda0: GeForce GTX 750 Ti (0000:07:00.0)
   [GpuElemwise{exp,no_inplace}(<GpuArrayType<None>(float64, (False,))>)]
   Looping 1000 times took 0.040277 seconds
   Result is [ 1.23178032  1.61879341  1.52278065 ...,  2.20771815  2.29967753
     1.62323285]
   Used the gpu


.. code-block:: none

  $ AESARA_FLAGS=device=cuda0 python gpu_tutorial2.py
  Using cuDNN version 5105 on context None
  Mapped name None to device cuda0: GeForce GTX 750 Ti (0000:07:00.0)
  [GpuElemwise{exp,no_inplace}(<GpuArrayType<None>(float64, (False,))>)]
  Looping 1000 times took 0.040277 seconds
  Result is [ 1.23178032  1.61879341  1.52278065 ...,  2.20771815  2.29967753
    1.62323285]
  Used the gpu

While the time per call appears to be much lower than the two previous
invocations (and should indeed be lower, since we avoid a transfer)
the massive speedup we obtained is in part due to asynchronous nature
of execution on GPUs, meaning that the work isn't completed yet, just
'launched'.  We'll talk about that later.

The object returned is a GpuArray from pygpu.  It mostly acts as a
numpy ndarray with some exceptions due to its data being on the GPU.
You can copy it to the host and convert it to a regular ndarray by
using usual numpy casting such as ``numpy.asarray()``.

For even more speed, you can play with the ``borrow`` flag.  See
:ref:`borrowfunction`.

What Can be Accelerated on the GPU
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The performance characteristics will of course vary from device to
device, and also as we refine our implementation:

* In general, matrix multiplication, convolution, and large element-wise
  operations can be accelerated a lot (5-50x) when arguments are large enough
  to keep 30 processors busy.
* Indexing, dimension-shuffling and constant-time reshaping will be
  equally fast on GPU as on CPU.
* Summation over rows/columns of tensors can be a little slower on the
  GPU than on the CPU.
* Copying of large quantities of data to and from a device is relatively slow,
  and often cancels most of the advantage of one or two accelerated functions
  on that data. Getting GPU performance largely hinges on making data transfer
  to the device pay off.

The backend supports all regular aesara data types (float32, float64,
int, ...), however GPU support varies and some units can't deal with
double (float64) or small (less than 32 bits like int16) data types.
You will get an error at compile time or runtime if this is the case.

By default all inputs will get transferred to GPU. You can prevent an
input from getting transferred by setting its ``tag.target`` attribute to
'cpu'.

Complex support is untested and most likely completely broken.

Tips for Improving Performance on GPU
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

* Consider adding ``floatX=float32`` (or the type you are using) to your
  ``.aesararc`` file if you plan to do a lot of GPU work.
* The GPU backend supports *float64* variables, but they are still slower
  to compute than *float32*. The more *float32*, the better GPU performance
  you will get.
* Prefer constructors like ``matrix``, ``vector`` and ``scalar`` (which
  follow the type set in ``floatX``) to ``dmatrix``, ``dvector`` and
  ``dscalar``. The latter enforce double precision (*float64* on most
  machines), which slows down GPU computations on current hardware.
* Minimize transfers to the GPU device by using ``shared`` variables
  to store frequently-accessed data (see :func:`shared()<shared.shared>`).
  When using the GPU, tensor ``shared`` variables are stored on
  the GPU by default to eliminate transfer time for GPU ops using those
  variables.
* If you aren't happy with the performance you see, try running your
  script with ``profile=True`` flag. This should print some timing
  information at program termination. Is time being used sensibly?  If
  an op or Apply is taking more time than its share, then if you know
  something about GPU programming, have a look at how it's implemented
  in aesara.gpuarray.  Check the line similar to *Spent Xs(X%) in cpu
  op, Xs(X%) in gpu op and Xs(X%) in transfer op*. This can tell you
  if not enough of your graph is on the GPU or if there is too much
  memory transfer.
* To investigate whether all the Ops in the computational graph are
  running on GPU, it is possible to debug or check your code by providing
  a value to `assert_no_cpu_op` flag, i.e. `warn`, for warning, `raise` for
  raising an error or `pdb` for putting a breakpoint in the computational
  graph if there is a CPU Op.

  .. _gpu_async:

GPU Async Capabilities
~~~~~~~~~~~~~~~~~~~~~~

By default, all operations on the GPU are run asynchronously.  This
means that they are only scheduled to run and the function returns.
This is made somewhat transparently by the underlying libgpuarray.

A forced synchronization point is introduced when doing memory
transfers between device and host.

It is possible to force synchronization for a particular GpuArray by
calling its ``sync()`` method.  This is useful to get accurate timings
when doing benchmarks.


Changing the Value of Shared Variables
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

To change the value of a ``shared`` variable, e.g. to provide new data
to processes, use ``shared_variable.set_value(new_value)``. For a lot
more detail about this, see :ref:`aliasing`.

Exercise
~~~~~~~~

Consider again the logistic regression:

.. testcode::

    import numpy
    import aesara
    import aesara.tensor as at
    rng = numpy.random

    N = 400
    feats = 784
    D = (rng.randn(N, feats).astype(aesara.config.floatX),
    rng.randint(size=N,low=0, high=2).astype(aesara.config.floatX))
    training_steps = 10000

    # Declare Aesara symbolic variables
    x = at.matrix("x")
    y = at.vector("y")
    w = aesara.shared(rng.randn(feats).astype(aesara.config.floatX), name="w")
    b = aesara.shared(numpy.asarray(0., dtype=aesara.config.floatX), name="b")
    x.tag.test_value = D[0]
    y.tag.test_value = D[1]

    # Construct Aesara expression graph
    p_1 = 1 / (1 + at.exp(-at.dot(x, w)-b)) # Probability of having a one
    prediction = p_1 > 0.5 # The prediction that is done: 0 or 1
    xent = -y*at.log(p_1) - (1-y)*at.log(1-p_1) # Cross-entropy
    cost = xent.mean() + 0.01*(w**2).sum() # The cost to optimize
    gw,gb = at.grad(cost, [w,b])

    # Compile expressions to functions
    train = aesara.function(
                inputs=[x,y],
                outputs=[prediction, xent],
                updates=[(w, w-0.01*gw), (b, b-0.01*gb)],
                name = "train")
    predict = aesara.function(inputs=[x], outputs=prediction,
                name = "predict")

    if any([x.op.__class__.__name__ in ['Gemv', 'CGemv', 'Gemm', 'CGemm'] for x in
            train.maker.fgraph.toposort()]):
        print('Used the cpu')
    elif any([x.op.__class__.__name__ in ['GpuGemm', 'GpuGemv'] for x in
              train.maker.fgraph.toposort()]):
        print('Used the gpu')
    else:
        print('ERROR, not able to tell if aesara used the cpu or the gpu')
        print(train.maker.fgraph.toposort())

    for i in range(training_steps):
        pred, err = train(D[0], D[1])

    print("target values for D")
    print(D[1])

    print("prediction on D")
    print(predict(D[0]))

    print("floatX=", aesara.config.floatX)
    print("device=", aesara.config.device)

.. testoutput::
   :hide:
   :options: +ELLIPSIS

   Used the cpu
   target values for D
   ...
   prediction on D
   ...

Modify and execute this example to run on GPU with ``floatX=float32``
and time it using the command line ``time python file.py``. (Of
course, you may use some of your answer to the exercise in section
:ref:`Configuration Settings and Compiling Mode<using_modes>`.)

Is there an increase in speed from CPU to GPU?

Where does it come from? (Use ``profile=True`` flag.)

What can be done to further increase the speed of the GPU version? Put
your ideas to test.

:download:`Solution<using_gpu_solution_1.py>`

-------------------------------------------


Software for Directly Programming a GPU
---------------------------------------

Leaving aside Aesara which is a meta-programmer, there are:

* **CUDA**: GPU programming API by NVIDIA based on extension to C (CUDA C)

  * Vendor-specific

  * Numeric libraries (BLAS, RNG, FFT) are maturing.

* **OpenCL**: multi-vendor version of CUDA

  * More general, standardized.

  * Fewer libraries, lesser spread.

* **PyCUDA**: Python bindings to CUDA driver interface allow to access Nvidia's CUDA parallel
  computation API from Python

  * Convenience:

    Makes it easy to do GPU meta-programming from within Python.

    Abstractions to compile low-level CUDA code from Python (``pycuda.driver.SourceModule``).

    GPU memory buffer (``pycuda.gpuarray.GPUArray``).

    Helpful documentation.

  * Completeness: Binding to all of CUDA's driver API.

  * Automatic error checking: All CUDA errors are automatically translated into Python exceptions.

  * Speed: PyCUDA's base layer is written in C++.

  * Good memory management of GPU objects:

    Object cleanup tied to lifetime of objects (RAII, 'Resource Acquisition Is Initialization').

    Makes it much easier to write correct, leak- and crash-free code.

    PyCUDA knows about dependencies (e.g. it won't detach from a context before all memory
    allocated in it is also freed).


  (This is adapted from PyCUDA's `documentation <http://documen.tician.de/pycuda/index.html>`_
  and Andreas Kloeckner's `website <http://mathema.tician.de/software/pycuda>`_ on PyCUDA.)


* **PyOpenCL**: PyCUDA for OpenCL

Learning to Program with PyCUDA
-------------------------------

If you already enjoy a good proficiency with the C programming language, you
may easily leverage your knowledge by learning, first, to program a GPU with the
CUDA extension to C (CUDA C) and, second, to use PyCUDA to access the CUDA
API with a Python wrapper.

The following resources will assist you in this learning process:

* **CUDA API and CUDA C: Introductory**

  * `NVIDIA's slides <http://www.sdsc.edu/us/training/assets/docs/NVIDIA-02-BasicsOfCUDA.pdf>`_

  * `Stein's (NYU) slides <http://www.cs.nyu.edu/manycores/cuda_many_cores.pdf>`_

* **CUDA API and CUDA C: Advanced**

  * `MIT IAP2009 CUDA <https://sites.google.com/site/cudaiap2009/home>`_
    (full coverage: lectures, leading Kirk-Hwu textbook, examples, additional resources)

  * `Course U. of Illinois <http://courses.engr.illinois.edu/ece498/al/index.html>`_
    (full lectures, Kirk-Hwu textbook)

  * `NVIDIA's knowledge base <http://www.nvidia.com/content/cuda/cuda-developer-resources.html>`_
    (extensive coverage, levels from introductory to advanced)

  * `practical issues <http://stackoverflow.com/questions/2392250/understanding-cuda-grid-dimensions-block-dimensions-and-threads-organization-s>`_
    (on the relationship between grids, blocks and threads; see also linked and related issues on same page)

  * `CUDA optimization <http://www.gris.informatik.tu-darmstadt.de/cuda-workshop/slides.html>`_

* **PyCUDA: Introductory**

  * `Kloeckner's slides <http://www.gputechconf.com/gtcnew/on-demand-gtc.php?sessionTopic=&searchByKeyword=kloeckner&submit=&select=+&sessionEvent=2&sessionYear=2010&sessionFormat=3>`_

  * `Kloeckner' website <http://mathema.tician.de/software/pycuda>`_

* **PYCUDA: Advanced**

  * `PyCUDA documentation website <http://documen.tician.de/pycuda/>`_


The following examples give a foretaste of programming a GPU with PyCUDA. Once
you feel competent enough, you may try yourself on the corresponding exercises.

**Example: PyCUDA**


.. code-block:: python

  # (from PyCUDA's documentation)
  import pycuda.autoinit
  import pycuda.driver as drv
  import numpy

  from pycuda.compiler import SourceModule
  mod = SourceModule("""
  __global__ void multiply_them(float *dest, float *a, float *b)
  {
    const int i = threadIdx.x;
    dest[i] = a[i] * b[i];
  }
  """)

  multiply_them = mod.get_function("multiply_them")

  a = numpy.random.randn(400).astype(numpy.float32)
  b = numpy.random.randn(400).astype(numpy.float32)

  dest = numpy.zeros_like(a)
  multiply_them(
          drv.Out(dest), drv.In(a), drv.In(b),
          block=(400,1,1), grid=(1,1))

  assert numpy.allclose(dest, a*b)
  print(dest)


Exercise
~~~~~~~~

Run the preceding example.

Modify and execute to work for a matrix of shape (20, 10).



.. _pyCUDA_aesara:

**Example: Aesara + PyCUDA**


.. code-block:: python

    import numpy, aesara
    import aesara.misc.pycuda_init
    from pycuda.compiler import SourceModule
    import aesara.sandbox.cuda as cuda
    from aesara.graph.basic import Apply
    from aesara.graph.op import Op


    class PyCUDADoubleOp(Op):

        __props__ = ()

        def make_node(self, inp):
            inp = cuda.basic_ops.gpu_contiguous(
               cuda.basic_ops.as_cuda_ndarray_variable(inp))
            assert inp.dtype == "float32"
            return Apply(self, [inp], [inp.type()])

        def make_thunk(self, node, storage_map, _, _2, impl):
            mod = SourceModule("""
        __global__ void my_fct(float * i0, float * o0, int size) {
        int i = blockIdx.x*blockDim.x + threadIdx.x;
        if(i<size){
            o0[i] = i0[i]*2;
        }
      }""")
            pycuda_fct = mod.get_function("my_fct")
            inputs = [storage_map[v] for v in node.inputs]
            outputs = [storage_map[v] for v in node.outputs]

            def thunk():
                z = outputs[0]
                if z[0] is None or z[0].shape != inputs[0][0].shape:
                    z[0] = cuda.CudaNdarray.zeros(inputs[0][0].shape)
                grid = (int(numpy.ceil(inputs[0][0].size / 512.)), 1)
                pycuda_fct(inputs[0][0], z[0], numpy.intc(inputs[0][0].size),
                           block=(512, 1, 1), grid=grid)
            return thunk


Use this code to test it:

>>> x = aesara.tensor.type.fmatrix()
>>> f = aesara.function([x], PyCUDADoubleOp()(x))  # doctest: +SKIP
>>> xv = numpy.ones((4, 5), dtype="float32")
>>> assert numpy.allclose(f(xv), xv*2)  # doctest: +SKIP
>>> print(numpy.asarray(f(xv)))  # doctest: +SKIP


Exercise
~~~~~~~~

Run the preceding example.

Modify and execute to multiply two matrices: *x* * *y*.

Modify and execute to return two outputs: *x + y* and *x - y*.

(Notice that Aesara's current *elemwise fusion* optimization is
only applicable to computations involving a single output. Hence, to gain
efficiency over the basic solution that is asked here, the two operations would
have to be jointly optimized explicitly in the code.)

Modify and execute to support *stride* (i.e. to avoid constraining the input to be *C-contiguous*).

Note
----

* See :ref:`example_other_random` to know how to handle random numbers
  on the GPU.

* The mode `FAST_COMPILE` disables C code, so also disables the GPU. You
  can use the Aesara flag optimizer='fast_compile' to speed up
  compilation and keep the GPU.
