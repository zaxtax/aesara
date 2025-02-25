"""
Generate and compile C modules for Python.

"""
import atexit
import distutils.sysconfig
import importlib
import logging
import os
import pickle
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import warnings
from io import BytesIO, StringIO
from typing import Callable, Dict, List, Optional, Set, Tuple, cast

import numpy.distutils
from typing_extensions import Protocol

# we will abuse the lockfile mechanism when reading and writing the registry
from aesara.compile.compilelock import lock_ctx
from aesara.configdefaults import config, gcc_version_str
from aesara.configparser import BoolParam, StrParam
from aesara.graph.op import Op
from aesara.link.c.exceptions import CompileError, MissingGXX
from aesara.utils import (
    LOCAL_BITWIDTH,
    flatten,
    hash_from_code,
    maybe_add_to_os_environ_pathlist,
    output_subprocess_Popen,
    subprocess_Popen,
)


class StdLibDirsAndLibsType(Protocol):
    data: Optional[Tuple[List[str], ...]]
    __call__: Callable[[], Optional[Tuple[List[str], ...]]]


def is_StdLibDirsAndLibsType(
    fn: Callable[[], Optional[Tuple[List[str], ...]]]
) -> StdLibDirsAndLibsType:
    return cast(StdLibDirsAndLibsType, fn)


class GCCLLVMType(Protocol):
    is_llvm: Optional[bool]
    __call__: Callable[[], Optional[bool]]


def is_GCCLLVMType(fn: Callable[[], Optional[bool]]) -> GCCLLVMType:
    return cast(GCCLLVMType, fn)


_logger = logging.getLogger("aesara.link.c.cmodule")

METH_VARARGS = "METH_VARARGS"
METH_NOARGS = "METH_NOARGS"
# global variable that represent the total time spent in importing module.
import_time = 0


def debug_counter(name, every=1):
    """
    Debug counter to know how often we go through some piece of code.

    This is a utility function one may use when debugging.

    Examples
    --------
    debug_counter('I want to know how often I run this line')

    """
    setattr(debug_counter, name, getattr(debug_counter, name, 0) + 1)
    n = getattr(debug_counter, name)
    if n % every == 0:
        print(f"debug_counter [{name}]: {n}", file=sys.stderr)


class ExtFunction:
    """
    A C function to put into a DynamicModule.

    """

    name = ""
    """
    str - function's name.

    """
    code_block = ""
    """
    str - the entire code for the function.

    Has the form ``static PyObject* <name>([...]){ ... }

    See Python's C API Reference for how to write c functions for python
    modules.

    """
    method = ""
    """
    str - calling method for this function (i.e. 'METH_VARARGS', 'METH_NOARGS').

    """
    doc = ""
    """
    str - documentation string for this function.

    """

    def __init__(self, name, code_block, method, doc="undocumented"):
        self.name = name
        self.code_block = code_block
        self.method = method
        self.doc = doc

    def method_decl(self):
        """
        Returns the signature for this function.

        It goes into the DynamicModule's method table.

        """
        return f'\t{{"{self.name}", {self.name}, {self.method}, "{self.doc}"}}'


class DynamicModule:
    def __init__(self, name=None):
        assert name is None, (
            "The 'name' parameter of DynamicModule"
            " cannot be specified anymore. Instead, 'code_hash'"
            " will be automatically computed and can be used as"
            " the module's name."
        )
        # While the module is not finalized, we can call add_...
        # when it is finalized, a hash is computed and used instead of
        # the placeholder, and as module name.
        self.finalized = False
        self.code_hash = None
        self.hash_placeholder = "<<<<HASH_PLACEHOLDER>>>>"

        self.support_code = []
        self.functions = []
        self.includes = ["<Python.h>", "<iostream>", '"aesara_mod_helper.h"']
        self.init_blocks = []

    def print_methoddef(self, stream):
        print("static PyMethodDef MyMethods[] = {", file=stream)
        for f in self.functions:
            print(f.method_decl(), ",", file=stream)
        print("\t{NULL, NULL, 0, NULL}", file=stream)
        print("};", file=stream)

    def print_init(self, stream):
        print(
            f"""static struct PyModuleDef moduledef = {{
  PyModuleDef_HEAD_INIT,
  "{self.hash_placeholder}",
  NULL,
  -1,
  MyMethods,
}};
""",
            file=stream,
        )
        print(
            f"PyMODINIT_FUNC PyInit_{self.hash_placeholder}(void) {{",
            file=stream,
        )
        for block in self.init_blocks:
            print("  ", block, file=stream)
        print("    PyObject *m = PyModule_Create(&moduledef);", file=stream)
        print("    return m;", file=stream)
        print("}", file=stream)

    def add_include(self, str):
        assert not self.finalized
        self.includes.append(str)

    def add_init_code(self, code):
        assert not self.finalized
        self.init_blocks.append(code)

    def add_support_code(self, code):
        assert not self.finalized
        if code and code not in self.support_code:  # TODO: KLUDGE
            self.support_code.append(code)

    def add_function(self, fn):
        assert not self.finalized
        self.functions.append(fn)

    def code(self):
        sio = StringIO()
        for inc in self.includes:
            if not inc:
                continue
            if inc[0] == "<" or inc[0] == '"':
                print("#include", inc, file=sio)
            else:
                print(f'#include "{inc}"', file=sio)

        print("//////////////////////", file=sio)
        print("////  Support Code", file=sio)
        print("//////////////////////", file=sio)
        for sc in self.support_code:
            print(sc, file=sio)

        print("//////////////////////", file=sio)
        print("////  Functions", file=sio)
        print("//////////////////////", file=sio)
        for f in self.functions:
            print(f.code_block, file=sio)

        print("//////////////////////", file=sio)
        print("////  Module init", file=sio)
        print("//////////////////////", file=sio)
        self.print_methoddef(sio)
        self.print_init(sio)

        rval = sio.getvalue()
        # Make sure the hash of the code hasn't changed
        h = hash_from_code(rval)
        assert self.code_hash is None or self.code_hash == h
        self.code_hash = h
        rval = re.sub(self.hash_placeholder, self.code_hash, rval)
        # Finalize the Module, so no support code or function
        # can be added
        self.finalized = True

        return rval

    def list_code(self, ofile=sys.stdout):
        """
        Print out the code with line numbers to `ofile`.

        """
        for i, line in enumerate(self.code().split("\n")):
            print(f"{i + 1}", line, file=ofile)
        ofile.flush()

    # TODO: add_type


def _get_ext_suffix():
    """Get the suffix for compiled extensions"""
    dist_suffix = distutils.sysconfig.get_config_var("EXT_SUFFIX")
    if dist_suffix is None:
        dist_suffix = distutils.sysconfig.get_config_var("SO")
    return dist_suffix


def dlimport(fullpath, suffix=None):
    """
    Dynamically load a .so, .pyd, .dll, or .py file.

    Parameters
    ----------
    fullpath : str
        A fully-qualified path do a compiled python module.
    suffix : str
        A suffix to strip from the end of fullpath to get the
        import name.

    Returns
    -------
    object
        The dynamically loaded module (from __import__).

    """
    if not os.path.isabs(fullpath):
        raise ValueError("`fullpath` must be an absolute path", fullpath)
    if suffix is None:
        suffix = ""

        dist_suffix = _get_ext_suffix()
        if dist_suffix is not None and dist_suffix != "":
            if fullpath.endswith(dist_suffix):
                suffix = dist_suffix

        if suffix == "":
            if fullpath.endswith(".so"):
                suffix = ".so"
            elif fullpath.endswith(".pyd"):
                suffix = ".pyd"
            elif fullpath.endswith(".dll"):
                suffix = ".dll"
            elif fullpath.endswith(".py"):
                suffix = ".py"

    rval = None
    if fullpath.endswith(suffix):
        module_name = ".".join(fullpath.split(os.path.sep)[-2:])[: -len(suffix)]
    else:
        raise ValueError("path has wrong suffix", (fullpath, suffix))
    workdir = fullpath[: -len(module_name) - 1 - len(suffix)]

    _logger.debug(f"WORKDIR {workdir}")
    _logger.debug(f"module_name {module_name}")

    sys.path[0:0] = [workdir]  # insert workdir at beginning (temporarily)
    # Explicitly add gcc dll directory on Python 3.8+ on Windows
    if (sys.platform == "win32") & (hasattr(os, "add_dll_directory")):
        gcc_path = shutil.which("gcc")
        if gcc_path is not None:
            os.add_dll_directory(os.path.dirname(gcc_path))
    global import_time
    try:
        importlib.invalidate_caches()
        t0 = time.time()
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="numpy.ndarray size changed")
            rval = __import__(module_name, {}, {}, [module_name])
        t1 = time.time()
        import_time += t1 - t0
        if not rval:
            raise Exception("__import__ failed", fullpath)
    finally:
        del sys.path[0]

    assert fullpath.startswith(rval.__file__)
    return rval


def dlimport_workdir(basedir):
    """
    Return a directory where you should put your .so file for dlimport
    to be able to load it, given a basedir which should normally be
    config.compiledir.

    """
    return tempfile.mkdtemp(dir=basedir)


def last_access_time(path):
    """
    Return the number of seconds since the epoch of the last access of a
    given file.

    """
    return os.stat(path)[stat.ST_ATIME]


def module_name_from_dir(dirname, err=True, files=None):
    """
    Scan the contents of a cache directory and return full path of the
    dynamic lib in it.

    """
    if files is None:
        try:
            files = os.listdir(dirname)
        except OSError as e:
            if e.errno == 2 and not err:  # No such file or directory
                return None
    names = [file for file in files if file.endswith(".so") or file.endswith(".pyd")]
    if len(names) == 0 and not err:
        return None
    elif len(names) == 1:
        return os.path.join(dirname, names[0])
    else:
        raise ValueError("More than 1 compiled module in this directory:" + dirname)


def is_same_entry(entry_1, entry_2):
    """
    Return True iff both paths can be considered to point to the same module.

    This is the case if and only if at least one of these conditions holds:
        - They are equal.
        - Their real paths are equal.
        - They share the same temporary work directory and module file name.

    """
    if entry_1 == entry_2:
        return True
    if os.path.realpath(entry_1) == os.path.realpath(entry_2):
        return True
    if (
        os.path.basename(entry_1) == os.path.basename(entry_2)
        and (
            os.path.basename(os.path.dirname(entry_1))
            == os.path.basename(os.path.dirname(entry_2))
        )
        and os.path.basename(os.path.dirname(entry_1)).startswith("tmp")
    ):
        return True
    return False


def get_module_hash(src_code, key):
    """
    Return a SHA256 hash that uniquely identifies a module.

    This hash takes into account:
        1. The C source code of the module (`src_code`).
        2. The version part of the key.
        3. The compiler options defined in `key` (command line parameters and
           libraries to link against).
        4. The NumPy ABI version.

    """
    # `to_hash` will contain any element such that we know for sure that if
    # it changes, then the module hash should be different.
    # We start with the source code itself (stripping blanks might avoid
    # recompiling after a basic indentation fix for instance).
    to_hash = [l.strip() for l in src_code.split("\n")]
    # Get the version part of the key (ignore if unversioned).
    if key[0]:
        to_hash += list(map(str, key[0]))
    c_link_key = key[1]
    # Currently, in order to catch potential bugs early, we are very
    # convervative about the structure of the key and raise an exception
    # if it does not match exactly what we expect. In the future we may
    # modify this behavior to be less strict and be able to accommodate
    # changes to the key in an automatic way.
    # Note that if the key structure changes, the `get_safe_part` function
    # below may also need to be modified.
    error_msg = (
        "This should not happen unless someone modified the code "
        "that defines the CLinker key, in which case you should "
        "ensure this piece of code is still valid (and this "
        "AssertionError may be removed or modified to accommodate "
        "this change)"
    )
    assert c_link_key[0] == "CLinker.cmodule_key", error_msg
    for key_element in c_link_key[1:]:
        if isinstance(key_element, tuple):
            # This should be the C++ compilation command line parameters or the
            # libraries to link against.
            to_hash += list(key_element)
        elif isinstance(key_element, str):
            if key_element.startswith("md5:") or key_element.startswith("hash:"):
                # This is actually a sha256 hash of the config options.
                # Currently, we still keep md5 to don't break old Aesara.
                # We add 'hash:' so that when we change it in
                # the futur, it won't break this version of Aesara.
                break
            elif key_element.startswith("NPY_ABI_VERSION=0x") or key_element.startswith(
                "c_compiler_str="
            ):
                to_hash.append(key_element)
            else:
                raise AssertionError(error_msg)
        else:
            raise AssertionError(error_msg)
    return hash_from_code("\n".join(to_hash))


def get_safe_part(key):
    """
    Return a tuple containing a subset of `key`, to be used to find equal keys.

    This tuple should only contain objects whose __eq__ and __hash__ methods
    can be trusted (currently: the version part of the key, as well as the
    SHA256 hash of the config options).
    It is used to reduce the amount of key comparisons one has to go through
    in order to find broken keys (i.e. keys with bad implementations of __eq__
    or __hash__).


    """
    version = key[0]
    # This function should only be called on versioned keys.
    assert version

    # Find the hash part. This is actually a sha256 hash of the config
    # options.  Currently, we still keep md5 to don't break old
    # Aesara.  We add 'hash:' so that when we change it
    # in the futur, it won't break this version of Aesara.
    c_link_key = key[1]
    # In case in the future, we don't have an md5 part and we have
    # such stuff in the cache.  In that case, we can set None, and the
    # rest of the cache mechanism will just skip that key.
    hash = None
    for key_element in c_link_key[1:]:
        if isinstance(key_element, str):
            if key_element.startswith("md5:"):
                hash = key_element[4:]
                break
            elif key_element.startswith("hash:"):
                hash = key_element[5:]
                break

    return key[0] + (hash,)


class KeyData:
    """
    Used to store the key information in the cache.

    Parameters
    ----------
    keys
        Set of keys that are associated to the exact same module.
    module_hash
        Hash identifying the module (it should hash both the code and the
        compilation options).
    key_pkl
        Path to the file in which this KeyData object should be
        pickled.

    """

    def __init__(self, keys, module_hash, key_pkl, entry):
        self.keys = keys
        self.module_hash = module_hash
        self.key_pkl = key_pkl
        self.entry = entry

    def add_key(self, key, save_pkl=True):
        """
        Add a key to self.keys, and update pickled file if asked to.

        """
        assert key not in self.keys
        self.keys.add(key)
        if save_pkl:
            self.save_pkl()

    def remove_key(self, key, save_pkl=True):
        """
        Remove a key from self.keys, and update pickled file if asked to.

        """
        self.keys.remove(key)
        if save_pkl:
            self.save_pkl()

    def save_pkl(self):
        """
        Dump this object into its `key_pkl` file.

        May raise a cPickle.PicklingError if such an exception is raised at
        pickle time (in which case a warning is also displayed).

        """
        # Note that writing in binary mode is important under Windows.
        try:
            with open(self.key_pkl, "wb") as f:
                pickle.dump(self, f, protocol=pickle.HIGHEST_PROTOCOL)
        except pickle.PicklingError:
            _logger.warning(f"Cache leak due to unpickle-able key data {self.keys}")
            os.remove(self.key_pkl)
            raise

    def get_entry(self):
        """
        Return path to the module file.

        """
        # TODO This method may be removed in the future (e.g. in 0.5) since
        # its only purpose is to make sure that old KeyData objects created
        # before the 'entry' field was added are properly handled.
        if not hasattr(self, "entry"):
            self.entry = module_name_from_dir(os.path.dirname(self.key_pkl))
        return self.entry

    def delete_keys_from(self, entry_from_key, do_manual_check=True):
        """
        Delete from entry_from_key all keys associated to this KeyData object.

        Note that broken keys will not appear in the keys field, so we also
        manually look for keys associated to the same entry, unless
        do_manual_check is False.

        """
        entry = self.get_entry()
        for key in self.keys:
            try:
                del entry_from_key[key]
            except KeyError:
                # This happen if the compiledir was deleted during
                # this process execution.
                pass
        if do_manual_check:
            to_del = []
            for key, key_entry in entry_from_key.items():
                if key_entry == entry:
                    to_del.append(key)
            for key in to_del:
                try:
                    del entry_from_key[key]
                except KeyError:
                    # This happen if the compiledir was deleted during
                    # this process execution.
                    pass


class ModuleCache:
    """
    Interface to the cache of dynamically compiled modules on disk.

    Note that this interface does not assume exclusive use of the cache
    directory. It is built to handle the case where multiple programs are also
    using instances of this class to manage the same directory.

    The cache works on the basis of keys. Each key is mapped to only one
    dynamic module, but multiple keys may be mapped to the same module (see
    below for details). Each module is a dynamic library file, that Python
    can import.

    The cache contains one directory for each module, containing:
    - the dynamic library file itself (.so/.pyd),
    - an empty __init__.py file, so Python can import it,
    - a file containing the source code for the module (mod.cpp/mod.cu),
    - a key.pkl file, containing a KeyData object with all the keys
    associated with that module,
    - possibly a delete.me file, meaning this directory has been marked
    for deletion.

    Keys should be tuples of length 2: (version, rest). The
    ``rest`` can be anything hashable and picklable, that uniquely
    identifies the computation in the module. The key is returned by
    ``CLinker.cmodule_key_``.

    The ``version`` should be a hierarchy of tuples of integers.
    If the ``version`` is either 0 or (), then the key is unversioned, and its
    corresponding module will be marked for deletion in an atexit() handler.
    If the ``version`` is neither 0 nor (), then the module will be kept in the
    cache between processes.

    An unversioned module is not always deleted by the process that
    creates it.  Deleting such modules may not work on NFS filesystems
    because the tmpdir in which the library resides is in use until the
    end of the process' lifetime.  In this case, unversioned modules
    are left in their tmpdirs without corresponding .pkl files.  These
    modules and their directories are erased by subsequent processes'
    refresh() functions.

    Two different keys are mapped to the same module when all conditions below
    are met:
        - They have the same version.
        - They share the same compilation options in their ``rest`` part (see
          ``CLinker.cmodule_key_`` for how this part is built).
        - They share the same C code.
    These three elements uniquely identify a module, and are summarized
    in a single "module hash".

    Parameters
    ----------
    check_for_broken_eq
        A bad __eq__ implementation can break this cache mechanism.
        This option turns on a not-too-expensive sanity check every
        time a new key is added to the cache.

    do_refresh : bool
        If True, then the ``refresh`` method will be called
        in the constructor.

    """

    dirname: str = ""
    """
    The working directory that is managed by this interface.

    """
    module_from_name: Dict = {}
    """
    Maps a module filename to the loaded module object.

    """
    entry_from_key: Dict = {}
    """
    Maps keys to the filename of a .so/.pyd.

    """
    similar_keys: Dict = {}
    """
    Maps a part-of-key to all keys that share this same part.

    """
    module_hash_to_key_data: Dict = {}
    """
    Maps a module hash to its corresponding KeyData object.

    """
    stats: List = []
    """
    A list with counters for the number of hits, loads, compiles issued by
    module_from_key().

    """
    loaded_key_pkl: Set = set()
    """
    Set of all key.pkl files that have been loaded.

    """

    def __init__(self, dirname, check_for_broken_eq=True, do_refresh=True):
        self.dirname = dirname
        self.module_from_name = dict(self.module_from_name)
        self.entry_from_key = dict(self.entry_from_key)
        self.module_hash_to_key_data = dict(self.module_hash_to_key_data)
        self.similar_keys = dict(self.similar_keys)
        self.stats = [0, 0, 0]
        self.check_for_broken_eq = check_for_broken_eq
        self.loaded_key_pkl = set()
        self.time_spent_in_check_key = 0

        if do_refresh:
            self.refresh()

    age_thresh_use = config.cmodule__age_thresh_use  # default 24 days
    """
    The default age threshold (in seconds) for cache files we want to use.

    Older modules will be deleted in ``clear_old``.

    """

    def _get_module(self, name):
        """
        Fetch a compiled module from the loaded cache or the disk.

        """
        if name not in self.module_from_name:
            _logger.debug(f"loading name {name}")
            self.module_from_name[name] = dlimport(name)
            self.stats[1] += 1
        else:
            _logger.debug(f"returning compiled module from cache {name}")
            self.stats[0] += 1
        return self.module_from_name[name]

    def refresh(self, age_thresh_use=None, delete_if_problem=False, cleanup=True):
        """
        Update cache data by walking the cache directory structure.

        Load key.pkl files that have not been loaded yet.
        Remove entries which have been removed from the filesystem.
        Also, remove malformed cache directories.

        Parameters
        ----------
        age_thresh_use
            Do not use modules other than this. Defaults to self.age_thresh_use.
        delete_if_problem : bool
            If True, cache entries that meet one of those two conditions are
            deleted:
            - Those for which unpickling the KeyData file fails with
              an unknown exception.
            - Duplicated modules, regardless of their age.
        cleanup : bool
            Do a cleanup of the cache removing expired and broken modules.

        Returns
        -------
        list
            A list of modules of age higher than age_thresh_use.

        """
        if age_thresh_use is None:
            age_thresh_use = self.age_thresh_use
        start_time = time.time()
        too_old_to_use = []

        to_delete = []
        to_delete_empty = []

        def rmtree(*args, **kwargs):
            if cleanup:
                to_delete.append((args, kwargs))

        def rmtree_empty(*args, **kwargs):
            if cleanup:
                to_delete_empty.append((args, kwargs))

        # add entries that are not in the entry_from_key dictionary
        time_now = time.time()
        # Go through directories in alphabetical order to ensure consistent
        # behavior.
        try:
            subdirs = sorted(os.listdir(self.dirname))
        except OSError:
            # This can happen if the dir don't exist.
            subdirs = []
        files, root = None, None  # To make sure the "del" below works
        for subdirs_elem in subdirs:
            # Never clean/remove lock_dir
            if subdirs_elem == "lock_dir":
                continue
            root = os.path.join(self.dirname, subdirs_elem)
            # Don't delete the gpuarray kernel cache
            if root == config.gpuarray__cache_path:
                continue
            key_pkl = os.path.join(root, "key.pkl")
            if key_pkl in self.loaded_key_pkl:
                continue
            if not os.path.isdir(root):
                continue
            files = os.listdir(root)
            if not files:
                rmtree_empty(root, ignore_nocleanup=True, msg="empty dir")
                continue
            if "delete.me" in files:
                rmtree(root, ignore_nocleanup=True, msg="delete.me found in dir")
                continue
            elif "key.pkl" in files:
                try:
                    entry = module_name_from_dir(root, files=files)
                except ValueError:  # there is a key but no dll!
                    if not root.startswith("/tmp"):
                        # Under /tmp, file are removed periodically by the
                        # os. So it is normal that this happens from time
                        # to time.
                        _logger.warning(
                            "ModuleCache.refresh() Found key "
                            f"without dll in cache, deleting it. {key_pkl}",
                        )
                    rmtree(
                        root,
                        ignore_nocleanup=True,
                        msg="missing module file",
                        level=logging.INFO,
                    )
                    continue
                if (time_now - last_access_time(entry)) < age_thresh_use:
                    _logger.debug(f"refresh adding {key_pkl}")

                    def unpickle_failure():
                        _logger.info(
                            f"ModuleCache.refresh() Failed to unpickle cache file {key_pkl}",
                        )

                    try:
                        with open(key_pkl, "rb") as f:
                            key_data = pickle.load(f)
                    except EOFError:
                        # Happened once... not sure why (would be worth
                        # investigating if it ever happens again).
                        unpickle_failure()
                        rmtree(
                            root,
                            ignore_nocleanup=True,
                            msg="broken cache directory [EOF]",
                            level=logging.WARNING,
                        )
                        continue
                    except Exception:
                        unpickle_failure()
                        if delete_if_problem:
                            rmtree(
                                root,
                                ignore_nocleanup=True,
                                msg="broken cache directory",
                                level=logging.INFO,
                            )
                        else:
                            # This exception is often triggered by keys
                            # that contain references to classes that have
                            # not yet been imported (e.g. when running two
                            # different Aesara-based scripts). They are not
                            # necessarily broken, but we cannot load them
                            # now. They will be loaded later if needed.
                            pass
                        continue

                    if not isinstance(key_data, KeyData):
                        # This is some old cache data, that does not fit
                        # the new cache format. It would be possible to
                        # update it, but it is not entirely safe since we
                        # do not know the config options that were used.
                        # As a result, we delete it instead (which is also
                        # simpler to implement).
                        rmtree(
                            root,
                            ignore_nocleanup=True,
                            msg=(
                                "invalid cache entry format -- this "
                                "should not happen unless your cache "
                                "was really old"
                            ),
                            level=logging.WARN,
                        )
                        continue

                    # Check the path to the module stored in the KeyData
                    # object matches the path to `entry`. There may be
                    # a mismatch e.g. due to symlinks, or some directory
                    # being renamed since last time cache was created.
                    kd_entry = key_data.get_entry()
                    if kd_entry != entry:
                        if is_same_entry(entry, kd_entry):
                            # Update KeyData object. Note that we also need
                            # to update the key_pkl field, because it is
                            # likely to be incorrect if the entry itself
                            # was wrong.
                            key_data.entry = entry
                            key_data.key_pkl = key_pkl
                        else:
                            # This is suspicious. Better get rid of it.
                            rmtree(
                                root,
                                ignore_nocleanup=True,
                                msg="module file path mismatch",
                                level=logging.INFO,
                            )
                            continue

                    # Find unversioned keys from other processes.
                    # TODO: check if this can happen at all
                    to_del = [key for key in key_data.keys if not key[0]]
                    if to_del:
                        _logger.warning(
                            "ModuleCache.refresh() Found unversioned "
                            f"key in cache, removing it. {key_pkl}",
                        )
                        # Since the version is in the module hash, all
                        # keys should be unversioned.
                        if len(to_del) != len(key_data.keys):
                            _logger.warning(
                                "Found a mix of unversioned and "
                                "versioned keys for the same "
                                f"module {key_pkl}",
                            )
                        rmtree(
                            root,
                            ignore_nocleanup=True,
                            msg="unversioned key(s) in cache",
                            level=logging.INFO,
                        )
                        continue

                    mod_hash = key_data.module_hash
                    if mod_hash in self.module_hash_to_key_data:
                        # This may happen when two processes running
                        # simultaneously compiled the same module, one
                        # after the other. We delete one once it is old
                        # enough (to be confident there is no other process
                        # using it), or if `delete_if_problem` is True.
                        # Note that it is important to walk through
                        # directories in alphabetical order so as to make
                        # sure all new processes only use the first one.
                        if cleanup:
                            age = time.time() - last_access_time(entry)
                            if delete_if_problem or age > self.age_thresh_del:
                                rmtree(
                                    root,
                                    ignore_nocleanup=True,
                                    msg="duplicated module",
                                    level=logging.DEBUG,
                                )
                            else:
                                _logger.debug(
                                    "Found duplicated module not "
                                    "old enough yet to be deleted "
                                    f"(age: {age}): {entry}",
                                )
                        continue

                    # Remember the map from a module's hash to the KeyData
                    # object associated with it.
                    self.module_hash_to_key_data[mod_hash] = key_data

                    for key in key_data.keys:
                        if key not in self.entry_from_key:
                            self.entry_from_key[key] = entry
                            # Assert that we have not already got this
                            # entry somehow.
                            assert entry not in self.module_from_name
                            # Store safe part of versioned keys.
                            if key[0]:
                                self.similar_keys.setdefault(
                                    get_safe_part(key), []
                                ).append(key)
                        else:
                            dir1 = os.path.dirname(self.entry_from_key[key])
                            dir2 = os.path.dirname(entry)
                            _logger.warning(
                                "The same cache key is associated to "
                                f"different modules ({dir1} and {dir2}). This "
                                "is not supposed to happen! You may "
                                "need to manually delete your cache "
                                "directory to fix this.",
                            )
                    # Clean up the name space to prevent bug.
                    if key_data.keys:
                        del key
                    self.loaded_key_pkl.add(key_pkl)
                else:
                    too_old_to_use.append(entry)

            # If the compilation failed, no key.pkl is in that
            # directory, but a mod.* should be there.
            # We do nothing here.

        # Clean up the name space to prevent bug.
        del root, files, subdirs

        # Remove entries that are not in the filesystem.
        items_copy = list(self.module_hash_to_key_data.items())
        for module_hash, key_data in items_copy:
            entry = key_data.get_entry()
            try:
                # Test to see that the file is [present and] readable.
                open(entry).close()
                gone = False
            except OSError:
                gone = True

            if gone:
                # Assert that we did not have one of the deleted files
                # loaded up and in use.
                # If so, it should not have been deleted. This should be
                # considered a failure of the OTHER process, that deleted
                # it.
                if entry in self.module_from_name:
                    _logger.warning(
                        "A module that was loaded by this "
                        "ModuleCache can no longer be read from file "
                        f"{entry}... this could lead to problems.",
                    )
                    del self.module_from_name[entry]

                _logger.info(f"deleting ModuleCache entry {entry}")
                key_data.delete_keys_from(self.entry_from_key)
                del self.module_hash_to_key_data[module_hash]
                if key_data.keys and list(key_data.keys)[0][0]:
                    # this is a versioned entry, so should have been on
                    # disk. Something weird happened to cause this, so we
                    # are responding by printing a warning, removing
                    # evidence that we ever saw this mystery key.
                    pkl_file_to_remove = key_data.key_pkl
                    if not key_data.key_pkl.startswith("/tmp"):
                        # Under /tmp, file are removed periodically by the
                        # os. So it is normal that this happen from time to
                        # time.
                        _logger.warning(
                            f"Removing key file {pkl_file_to_remove} because the "
                            "corresponding module is gone from the "
                            "file system."
                        )
                    self.loaded_key_pkl.remove(pkl_file_to_remove)

        if to_delete or to_delete_empty:
            with lock_ctx():
                for a, kw in to_delete:
                    _rmtree(*a, **kw)
                for a, kw in to_delete_empty:
                    files = os.listdir(a[0])
                    if not files:
                        _rmtree(*a, **kw)

        _logger.debug(f"Time needed to refresh cache: {time.time() - start_time}")

        return too_old_to_use

    def _get_from_key(self, key, key_data=None):
        """
        Returns a module if the passed-in key is found in the cache
        and None otherwise.

        May raise ValueError if the key is malformed.

        """
        name = None
        if key is not None:
            assert key_data is None
            try:
                _version, _rest = key
            except (TypeError, ValueError):
                raise ValueError("Invalid key. key must have form (version, rest)", key)
            if key in self.entry_from_key:
                name = self.entry_from_key[key]
        else:
            assert key_data is not None
            name = key_data.get_entry()
        if name is None:
            return None
        return self._get_module(name)

    def _get_from_hash(self, module_hash, key):
        if module_hash in self.module_hash_to_key_data:
            key_data = self.module_hash_to_key_data[module_hash]
            module = self._get_from_key(None, key_data)
            with lock_ctx():
                try:
                    key_data.add_key(key, save_pkl=bool(key[0]))
                    key_broken = False
                except pickle.PicklingError:
                    key_data.remove_key(key)
                    key_broken = True
                # We need the lock while we check in case of parallel
                # process that could be changing the file at the same
                # time.
                if key[0] and not key_broken and self.check_for_broken_eq:
                    self.check_key(key, key_data.key_pkl)
            self._update_mappings(
                key, key_data, module.__file__, check_in_keys=not key_broken
            )
            return module
        else:
            return None

    def _update_mappings(self, key, key_data, name, check_in_keys):
        all_keys = key_data.keys
        if not all_keys:
            all_keys = [key]
        if check_in_keys:
            assert key in all_keys
        for k in all_keys:
            if k in self.entry_from_key:
                assert self.entry_from_key[k] == name, (self.entry_from_key[k], name)
            else:
                self.entry_from_key[k] = name
                if key[0]:
                    self.similar_keys.setdefault(get_safe_part(k), []).append(key)

    def _add_to_cache(self, module, key, module_hash):
        """
        This function expects the compile lock to be held.

        """
        name = module.__file__
        _logger.debug(f"Adding module to cache {key} {name}")
        # Changing the hash of the key is not allowed during
        # compilation. That is the only cause found that makes
        # the following assert fail.
        assert key not in self.entry_from_key

        location = os.path.dirname(name)
        key_pkl = os.path.join(location, "key.pkl")
        assert not os.path.exists(key_pkl)
        key_data = KeyData(
            keys={key}, module_hash=module_hash, key_pkl=key_pkl, entry=name
        )

        key_broken = False
        if key[0]:
            try:
                key_data.save_pkl()
            except pickle.PicklingError:
                key_broken = True
                key_data.remove_key(key)
                key_data.save_pkl()
            if not key_broken and self.check_for_broken_eq:
                self.check_key(key, key_pkl)
            self.loaded_key_pkl.add(key_pkl)
        elif config.cmodule__warn_no_version:
            key_flat = flatten(key)
            ops = [k for k in key_flat if isinstance(k, Op)]
            _logger.warning(
                "not all the"
                " following op(s) implement"
                " c_code_cache_version(). This makes them"
                " recompiled for each process." + str(ops)
            )
        self._update_mappings(key, key_data, module.__file__, not key_broken)
        return key_data

    def module_from_key(self, key, lnk=None):
        """
        Return a module from the cache, compiling it if necessary.

        Parameters
        ----------
        key
            The key object associated with the module. If this hits a match,
            we avoid compilation.
        lnk
            Usually a CLinker instance, but it can be any object that defines
            the `get_src_code()` and `compile_cmodule(location)` functions. The
            first one returns the source code of the module to load/compile and
            the second performs the actual compilation.

        """
        # Is the module in the cache?
        module = self._get_from_key(key)
        if module is not None:
            return module

        src_code = lnk.get_src_code()
        # Is the source code already in the cache?
        module_hash = get_module_hash(src_code, key)
        module = self._get_from_hash(module_hash, key)
        if module is not None:
            return module

        with lock_ctx():
            # 1) Maybe somebody else compiled it for us while we
            #    where waiting for the lock. Try to load it again.
            # 2) If other repo that import Aesara have Aesara ops defined,
            #    we need to refresh the cache here. Otherwise, there are import
            #    order problems.
            #    When device=gpu, we compile during Aesara
            #    import. This triggers the loading of the cache. But
            #    unpickling the cache asks that the external Ops are
            #    completely loaded, which isn't always the case!
            #    If a module isn't completely loaded and its unpickling
            #    fails, it means it is safe for this function
            #    compilation to skip them, but not for future
            #    compilations. So reloading the cache here
            #    compilation fixes this problem. (we could do that only once)
            self.refresh(cleanup=False)

            module = self._get_from_key(key)
            if module is not None:
                return module

            module = self._get_from_hash(module_hash, key)
            if module is not None:
                return module

            hash_key = hash(key)

            nocleanup = False
            try:
                location = dlimport_workdir(self.dirname)
                module = lnk.compile_cmodule(location)
                name = module.__file__
                assert name.startswith(location)
                assert name not in self.module_from_name
                self.module_from_name[name] = module
                nocleanup = True
            except OSError as e:
                _logger.error(e)
                if e.errno == 31:
                    _logger.error(
                        f"There are {len(os.listdir(config.compiledir))} files in {config.compiledir}"
                    )
                raise
            finally:
                if not nocleanup:
                    _rmtree(
                        location,
                        ignore_if_missing=True,
                        msg="exception during compilation",
                    )

            # Changing the hash of the key is not allowed during
            # compilation.
            assert hash(key) == hash_key

            key_data = self._add_to_cache(module, key, module_hash)
            self.module_hash_to_key_data[module_hash] = key_data

        self.stats[2] += 1
        return module

    def check_key(self, key, key_pkl):
        """
        Perform checks to detect broken __eq__ / __hash__ implementations.

        Parameters
        ----------
        key
            The key to be checked.
        key_pkl
            Its associated pickled file containing a KeyData.

        """
        start_time = time.time()
        # Verify that when we reload the KeyData from the pickled file, the
        # same key can be found in it, and is not equal to more than one
        # other key.
        for i in range(3):
            try:
                with open(key_pkl, "rb") as f:
                    key_data = pickle.load(f)
                break
            except EOFError:
                # This file is probably getting written/updated at the
                # same time.  This can happen as we read the cache
                # without taking the lock.
                if i == 2:
                    with lock_ctx():
                        with open(key_pkl, "rb") as f:
                            key_data = pickle.load(f)
                time.sleep(2)

        found = sum(key == other_key for other_key in key_data.keys)
        msg = ""
        if found == 0:
            msg = "Key not found in unpickled KeyData file"
            if key_data.keys:
                # This is to make debugging in pdb easier, by providing
                # the offending keys in the local context.
                # key_data_keys = list(key_data.keys)
                # import pdb; pdb.set_trace()
                pass
        elif found > 1:
            msg = "Multiple equal keys found in unpickled KeyData file"
        if msg:
            raise AssertionError(
                f"{msg}. Verify the __eq__ and __hash__ functions of your "
                f"Ops. The file is: {key_pkl}. The key is: {key}"
            )
        # Also verify that there exists no other loaded key that would be equal
        # to this key. In order to speed things up, we only compare to keys
        # with the same version part and config hash, since we can assume this
        # part of the key is not broken.
        for other in self.similar_keys.get(get_safe_part(key), []):
            if other is not key and other == key and hash(other) != hash(key):
                raise AssertionError(
                    "Found two keys that are equal but have a different hash. "
                    "Verify the __eq__ and __hash__ functions of your Ops. "
                    f"The keys are:\n  {other}\nand\n  {key}\n(found in {key_pkl})."
                )

        self.time_spent_in_check_key += time.time() - start_time

    # default 31 days
    age_thresh_del = config.cmodule__age_thresh_use + 60 * 60 * 24 * 7
    age_thresh_del_unversioned = 60 * 60 * 24 * 7  # 7 days
    """
    The default age threshold for `clear_old` (in seconds).

    """

    def clear_old(self, age_thresh_del=None, delete_if_problem=False):
        """Delete entries from the filesystem for cache entries that are too old.

        This refreshes the content of the cache. Don't hold the lock
        while calling this method, this is useless. It will be taken
        if needed.

        Parameters
        ----------
        age_thresh_del
            Dynamic modules whose last access time is more than
            ``age_thresh_del`` seconds ago will be erased.
            Defaults to 31-day age if not provided.
        delete_if_problem
            See help of refresh() method.

        """
        if age_thresh_del is None:
            age_thresh_del = self.age_thresh_del

        # Ensure that the too_old_to_use list return by refresh() will
        # contain all modules older than age_thresh_del.
        if age_thresh_del < self.age_thresh_use:
            if age_thresh_del > 0:
                _logger.warning(
                    "Clearing modules that were not deemed "
                    f"too old to use: age_thresh_del={age_thresh_del}, "
                    f"self.age_thresh_use={self.age_thresh_use}"
                )
            else:
                _logger.info("Clearing all modules.")
            age_thresh_use = age_thresh_del
        else:
            age_thresh_use = None

        too_old_to_use = self.refresh(
            age_thresh_use=age_thresh_use,
            delete_if_problem=delete_if_problem,
            # The clean up is done at init, no need to trigger it again
            cleanup=False,
        )
        if not too_old_to_use:
            return
        with lock_ctx():
            # Update the age of modules that have been accessed by other
            # processes and get all module that are too old to use
            # (not loaded in self.entry_from_key).

            for entry in too_old_to_use:
                # TODO: we are assuming that modules that haven't been
                # accessed in over age_thresh_del are not currently in
                # use by other processes, but that could be false for
                # long-running jobs, or if age_thresh_del < 0.
                assert entry not in self.module_from_name
                parent = os.path.dirname(entry)
                assert parent.startswith(os.path.join(self.dirname, "tmp"))
                _rmtree(
                    parent,
                    msg="old cache directory",
                    level=logging.INFO,
                    ignore_nocleanup=True,
                )

    def clear(
        self, unversioned_min_age=None, clear_base_files=False, delete_if_problem=False
    ):
        """
        Clear all elements in the cache.

        Parameters
        ----------
        unversioned_min_age
            Forwarded to `clear_unversioned`. In particular, you can set it
            to -1 in order to delete all unversioned cached modules regardless
            of their age.
        clear_base_files : bool
            If True, then delete base directories 'cutils_ext',
            'lazylinker_ext' and 'scan_perform' if they are present.
            If False, those directories are left intact.
        delete_if_problem
            See help of refresh() method.

        """
        with lock_ctx():
            self.clear_old(age_thresh_del=-1.0, delete_if_problem=delete_if_problem)
            self.clear_unversioned(min_age=unversioned_min_age)
            if clear_base_files:
                self.clear_base_files()

    def clear_base_files(self):
        """
        Remove base directories 'cutils_ext', 'lazylinker_ext' and
        'scan_perform' if present.

        Note that we do not delete them outright because it may not work on
        some systems due to these modules being currently in use. Instead we
        rename them with the '.delete.me' extension, to mark them to be deleted
        next time we clear the cache.

        """
        with lock_ctx():
            for base_dir in ("cutils_ext", "lazylinker_ext", "scan_perform"):
                to_delete = os.path.join(self.dirname, base_dir + ".delete.me")
                if os.path.isdir(to_delete):
                    try:
                        shutil.rmtree(to_delete)
                        _logger.debug(f"Deleted: {to_delete}")
                    except Exception:
                        _logger.warning(f"Could not delete {to_delete}")
                        continue
                to_rename = os.path.join(self.dirname, base_dir)
                if os.path.isdir(to_rename):
                    try:
                        shutil.move(to_rename, to_delete)
                    except Exception:
                        _logger.warning(f"Could not move {to_rename} to {to_delete}")

    def clear_unversioned(self, min_age=None):
        """Delete unversioned dynamic modules.

        They are deleted both from the internal dictionaries and from the
        filesystem.

        No need to have the lock when calling this method. It does not
        take the lock as unversioned module aren't shared.

        This method does not refresh the cache content, it just
        accesses the in-memory known module(s).

        Parameters
        ----------
        min_age
            Minimum age to be deleted, in seconds. Defaults to
            7-day age if not provided.

        """
        if min_age is None:
            min_age = self.age_thresh_del_unversioned

        # As this delete object that we build and other don't use, we
        # don't need the lock.
        all_key_datas = list(self.module_hash_to_key_data.values())
        for key_data in all_key_datas:
            if not key_data.keys:
                # May happen for broken versioned keys.
                continue
            for key_idx, key in enumerate(key_data.keys):
                version, rest = key
                if version:
                    # Since the version is included in the module hash,
                    # it should not be possible to mix versioned and
                    # unversioned keys in the same KeyData object.
                    assert key_idx == 0
                    break
            if not version:
                # Note that unversioned keys cannot be broken, so we can
                # set do_manual_check to False to speed things up.
                key_data.delete_keys_from(self.entry_from_key, do_manual_check=False)
                entry = key_data.get_entry()
                # Entry is guaranteed to be in this dictionary, because
                # an unversioned entry should never have been loaded via
                # refresh.
                assert entry in self.module_from_name

                del self.module_from_name[entry]
                del self.module_hash_to_key_data[key_data.module_hash]

                parent = os.path.dirname(entry)
                assert parent.startswith(os.path.join(self.dirname, "tmp"))
                _rmtree(
                    parent, msg="unversioned", level=logging.INFO, ignore_nocleanup=True
                )

        # Sanity check: all unversioned keys should have been removed at
        # this point.
        for key in self.entry_from_key:
            assert key[0]

        to_del = []
        time_now = time.time()
        for filename in os.listdir(self.dirname):
            if filename.startswith("tmp"):
                try:
                    fname = os.path.join(self.dirname, filename, "key.pkl")
                    open(fname).close()
                    has_key = True
                except OSError:
                    has_key = False
                if not has_key:
                    # Use the compiled file by default
                    path = module_name_from_dir(
                        os.path.join(self.dirname, filename), False
                    )
                    # If it don't exist, use any file in the directory.
                    if path is None:
                        path = os.path.join(self.dirname, filename)
                        try:
                            files = os.listdir(path)
                        except OSError as e:
                            if e.errno == 2:  # No such file or directory
                                # if it don't exist anymore, it mean
                                # the clean up was already done by
                                # someone else, so nothing to do about
                                # it.
                                continue
                        if files:
                            path = os.path.join(path, files[0])
                        else:
                            # If the directory is empty skip it.
                            # They are deleted elsewhere.
                            continue
                    age = time_now - last_access_time(path)

                    # In normal case, the processus that created this
                    # directory will delete it. However, if this processus
                    # crashes, it will not be cleaned up.
                    # As we don't know if this directory is still used,
                    # we wait one week and suppose that the processus
                    # crashed, and we take care of the clean-up.
                    if age > min_age:
                        to_del.append(os.path.join(self.dirname, filename))

        # No need to take the lock as it isn't shared.
        for f in to_del:
            _rmtree(f, msg="old unversioned", level=logging.INFO, ignore_nocleanup=True)

    def _on_atexit(self):
        # Note: no need to call refresh() since it is called by clear_old().

        # Note: no need to take the lock. For unversioned files, we
        # don't need it as they aren't shared. For old unversioned
        # files, this happen rarely, so we take the lock only when
        # this happen.

        # Note: for clear_old(), as this happen unfrequently, we only
        # take the lock when it happen.
        self.clear_old()
        self.clear_unversioned()
        _logger.debug(f"Time spent checking keys: {self.time_spent_in_check_key}")


def _rmtree(
    parent, ignore_nocleanup=False, msg="", level=logging.DEBUG, ignore_if_missing=False
):
    """
    On NFS filesystems, it is impossible to delete a directory with open
    files in it.

    So instead, some commands in this file will respond to a
    failed rmtree() by touching a 'delete.me' file.  This file is a message
    for a future process to try deleting the directory.

    Parameters:
    ----------
    parent
        Root node to start deleting from
    ignore_nocleanup
        Delete the tree if flag is TRUE
    level
        Python Logging level. Set to "DEBUG" by default
    ignore_if_missing
        If set to True, just return without any issue if parent is NULL
    """
    if ignore_if_missing and not os.path.exists(parent):
        return
    try:
        if ignore_nocleanup or not config.nocleanup:
            log_msg = "Deleting"
            if msg:
                log_msg += f" ({msg})"
            _logger.log(level, f"{log_msg}: {parent}")
            shutil.rmtree(parent)
    except Exception as e:
        # If parent still exists, mark it for deletion by a future refresh()
        _logger.debug(f"In _rmtree, encountered exception: {type(e)}({e})")
        if os.path.exists(parent):
            try:
                _logger.info(f'placing "delete.me" in {parent}')
                open(os.path.join(parent, "delete.me"), "w").close()
            except Exception as ee:
                _logger.warning(
                    f"Failed to remove or mark cache directory {parent} for removal {ee}"
                )


_module_cache = None


def get_module_cache(dirname, init_args=None):
    """
    Create a new module_cache with the (k, v) pairs in this dictionary

    Parameters
    ----------
    init_args
        If not None, the (k, v) pairs in this dictionary will be forwarded to
        the ModuleCache constructor as keyword arguments.

    """
    global _module_cache
    if init_args is None:
        init_args = {}
    if _module_cache is None:
        _module_cache = ModuleCache(dirname, **init_args)
        atexit.register(_module_cache._on_atexit)
    elif init_args:
        _logger.warning(
            "Ignoring init arguments for module cache because it "
            "was created prior to this call"
        )
    if _module_cache.dirname != dirname:
        _logger.warning(
            "Returning module cache instance with different "
            f"dirname ({_module_cache.dirname}) than you requested ({dirname})"
        )
    return _module_cache


def get_lib_extension():
    """
    Return the platform-dependent extension for compiled modules.

    """
    if sys.platform == "win32":
        return "pyd"
    elif sys.platform == "cygwin":
        return "dll"
    else:
        return "so"


def get_gcc_shared_library_arg():
    """
    Return the platform-dependent GCC argument for shared libraries.

    """
    if sys.platform == "darwin":
        return "-dynamiclib"
    else:
        return "-shared"


def std_include_dirs():
    numpy_inc_dirs = numpy.distutils.misc_util.get_numpy_include_dirs()
    py_inc = distutils.sysconfig.get_python_inc()
    py_plat_spec_inc = distutils.sysconfig.get_python_inc(plat_specific=True)
    python_inc_dirs = (
        [py_inc] if py_inc == py_plat_spec_inc else [py_inc, py_plat_spec_inc]
    )
    gof_inc_dir = os.path.join(os.path.abspath(os.path.dirname(__file__)), "c_code")
    return numpy_inc_dirs + python_inc_dirs + [gof_inc_dir]


@is_StdLibDirsAndLibsType
def std_lib_dirs_and_libs() -> Optional[Tuple[List[str], ...]]:
    # We cache the results as on Windows, this trigger file access and
    # this method is called many times.
    if std_lib_dirs_and_libs.data is not None:
        return std_lib_dirs_and_libs.data
    python_inc = distutils.sysconfig.get_python_inc()
    if sys.platform == "win32":
        # Obtain the library name from the Python version instead of the
        # installation directory, in case the user defined a custom
        # installation directory.
        python_version = distutils.sysconfig.get_python_version()
        libname = "python" + python_version.replace(".", "")
        # Also add directory containing the Python library to the library
        # directories.
        python_lib_dirs = [os.path.join(os.path.dirname(python_inc), "libs")]
        if "Canopy" in python_lib_dirs[0]:
            # Canopy stores libpython27.a and libmsccr90.a in this directory.
            # For some reason, these files are needed when compiling Python
            # modules, even when libpython27.lib and python27.dll are
            # available, and the *.a files have to be found earlier than
            # the other ones.

            # When Canopy is installed for the user:
            # sys.prefix:C:\Users\username\AppData\Local\Enthought\Canopy\User
            # sys.base_prefix:C:\Users\username\AppData\Local\Enthought\Canopy\App\appdata\canopy-1.1.0.1371.win-x86_64
            # When Canopy is installed for all users:
            # sys.base_prefix: C:\Program Files\Enthought\Canopy\App\appdata\canopy-1.1.0.1371.win-x86_64
            # sys.prefix: C:\Users\username\AppData\Local\Enthought\Canopy\User
            # So we need to use sys.prefix as it support both cases.
            # sys.base_prefix support only one case
            libdir = os.path.join(sys.prefix, "libs")

            for f, lib in [("libpython27.a", "libpython 1.2")]:
                if not os.path.exists(os.path.join(libdir, f)):
                    print(
                        "Your Python version is from Canopy. "
                        + "You need to install the package '"
                        + lib
                        + "' from Canopy package manager."
                    )
            libdirs = [
                # Used in older Canopy
                os.path.join(sys.prefix, "libs"),
                # Used in newer Canopy
                os.path.join(sys.prefix, r"EGG-INFO\mingw\usr\x86_64-w64-mingw32\lib"),
            ]
            for f, lib in [
                ("libmsvcr90.a", "mingw 4.5.2 or 4.8.1-2 (newer could work)")
            ]:
                if not any(
                    [
                        os.path.exists(os.path.join(tmp_libdir, f))
                        for tmp_libdir in libdirs
                    ]
                ):
                    print(
                        "Your Python version is from Canopy. "
                        + "You need to install the package '"
                        + lib
                        + "' from Canopy package manager."
                    )
            python_lib_dirs.insert(0, libdir)
        std_lib_dirs_and_libs.data = [libname], python_lib_dirs

    # Suppress -lpython2.x on OS X since the `-undefined dynamic_lookup`
    # makes it unnecessary.
    elif sys.platform == "darwin":
        std_lib_dirs_and_libs.data = [], []
    else:
        if platform.python_implementation() == "PyPy":
            # Assume Linux (note: Ubuntu doesn't ship this .so)
            libname = "pypy3-c"
            # Unfortunately the only convention of this .so is that it appears
            # next to the location of the interpreter binary.
            libdir = os.path.dirname(os.path.realpath(sys.executable))
        else:
            # Assume Linux
            # Typical include directory: /usr/include/python2.6

            # get the name of the python library (shared object)

            libname = str(distutils.sysconfig.get_config_var("LDLIBRARY"))

            if libname.startswith("lib"):
                libname = libname[3:]

            # remove extension if present
            if libname.endswith(".so"):
                libname = libname[:-3]
            elif libname.endswith(".a"):
                libname = libname[:-2]

            libdir = str(distutils.sysconfig.get_config_var("LIBDIR"))

        std_lib_dirs_and_libs.data = [libname], [libdir]

    # sometimes, the linker cannot find -lpython so we need to tell it
    # explicitly where it is located this returns
    # somepath/lib/python2.x

    python_lib = str(
        distutils.sysconfig.get_python_lib(plat_specific=True, standard_lib=True)
    )
    python_lib = os.path.dirname(python_lib)
    if python_lib not in std_lib_dirs_and_libs.data[1]:
        std_lib_dirs_and_libs.data[1].append(python_lib)
    return std_lib_dirs_and_libs.data


std_lib_dirs_and_libs.data = None


def std_libs():
    return std_lib_dirs_and_libs()[0]


def std_lib_dirs():
    return std_lib_dirs_and_libs()[1]


def gcc_version():
    return gcc_version_str


@is_GCCLLVMType
def gcc_llvm() -> Optional[bool]:
    """
    Detect if the g++ version used is the llvm one or not.

    It don't support all g++ parameters even if it support many of them.

    """
    if gcc_llvm.is_llvm is None:
        try:
            p_out = output_subprocess_Popen([config.cxx, "--version"])
            output = p_out[0] + p_out[1]
        except OSError:
            # Typically means g++ cannot be found.
            # So it is not an llvm compiler.

            # Normally this should not happen as we should not try to
            # compile when g++ is not available. If this happen, it
            # will crash later so supposing it is not llvm is "safe".
            output = b""
        gcc_llvm.is_llvm = b"llvm" in output
    return gcc_llvm.is_llvm


gcc_llvm.is_llvm = None


class Compiler:
    """
    Meta compiler that offer some generic function.

    """

    @classmethod
    def _try_compile_tmp(
        cls,
        src_code,
        tmp_prefix="",
        flags=(),
        try_run=False,
        output=False,
        compiler=None,
        comp_args=True,
    ):
        """
        Try to compile (and run) a test program.

        This is useful in various occasions, to check if libraries
        or compilers are behaving as expected.

        If try_run is True, the src_code is assumed to be executable,
        and will be run.

        If try_run is False, returns the compilation status.
        If try_run is True, returns a (compile_status, run_status) pair.
        If output is there, we append the stdout and stderr to the output.

        Compile arguments from the Compiler's compile_args() method are added
        if comp_args=True.
        """
        if not compiler:
            return False
        flags = list(flags)
        # Get compile arguments from compiler method if required
        if comp_args:
            args = cls.compile_args()
        else:
            args = []
        compilation_ok = True
        run_ok = False
        out, err = None, None
        try:
            fd, path = tempfile.mkstemp(suffix=".c", prefix=tmp_prefix)
            exe_path = path[:-2]
            if os.name == "nt":
                path = '"' + path + '"'
                exe_path = '"' + exe_path + '"'
            try:
                try:
                    src_code = src_code.encode()
                except AttributeError:  # src_code was already bytes
                    pass
                os.write(fd, src_code)
                os.close(fd)
                fd = None
                out, err, p_ret = output_subprocess_Popen(
                    [compiler] + args + [path, "-o", exe_path] + flags
                )
                if p_ret != 0:
                    compilation_ok = False
                elif try_run:
                    out, err, p_ret = output_subprocess_Popen([exe_path])
                    run_ok = p_ret == 0
            finally:
                try:
                    if fd is not None:
                        os.close(fd)
                finally:
                    if os.path.exists(path):
                        os.remove(path)
                    if os.path.exists(exe_path):
                        os.remove(exe_path)
                    if os.path.exists(exe_path + ".exe"):
                        os.remove(exe_path + ".exe")
        except OSError as e:
            if err is None:
                err = str(e)
            else:
                err = str(err) + "\n" + str(e)
            compilation_ok = False

        if not try_run and not output:
            return compilation_ok
        elif not try_run and output:
            return (compilation_ok, out, err)
        elif not output:
            return (compilation_ok, run_ok)
        else:
            return (compilation_ok, run_ok, out, err)

    @classmethod
    def _try_flags(
        cls,
        flag_list,
        preamble="",
        body="",
        try_run=False,
        output=False,
        compiler=None,
        comp_args=True,
    ):
        """
        Try to compile a dummy file with these flags.

        Returns True if compilation was successful, False if there
        were errors.

        Compile arguments from the Compiler's compile_args() method are added
        if comp_args=True.

        """
        if not compiler:
            return False

        code = (
            """
        %(preamble)s
        int main(int argc, char** argv)
        {
            %(body)s
            return 0;
        }
        """
            % locals()
        ).encode()
        return cls._try_compile_tmp(
            code,
            tmp_prefix="try_flags_",
            flags=flag_list,
            try_run=try_run,
            output=output,
            compiler=compiler,
            comp_args=comp_args,
        )


def try_blas_flag(flags):
    test_code = textwrap.dedent(
        """\
        extern "C" double ddot_(int*, double*, int*, double*, int*);
        int main(int argc, char** argv)
        {
            int Nx = 5;
            int Sx = 1;
            double x[5] = {0, 1, 2, 3, 4};
            double r = ddot_(&Nx, x, &Sx, x, &Sx);

            if ((r - 30.) > 1e-6 || (r - 30.) < -1e-6)
            {
                return -1;
            }
            return 0;
        }
        """
    )
    cflags = list(flags)
    # to support path that includes spaces, we need to wrap it with double quotes on Windows
    path_wrapper = '"' if os.name == "nt" else ""
    cflags.extend([f"-L{path_wrapper}{d}{path_wrapper}" for d in std_lib_dirs()])

    res = GCC_compiler.try_compile_tmp(
        test_code, tmp_prefix="try_blas_", flags=cflags, try_run=True
    )
    # res[0]: shows successful compilation
    # res[1]: shows successful execution
    if res and res[0] and res[1]:
        return " ".join(flags)
    else:
        return ""


def try_march_flag(flags):
    """
    Try to compile and run a simple C snippet using current flags.
    Return: compilation success (True/False), execution success (True/False)
    """
    test_code = textwrap.dedent(
        """\
            #include <cmath>
            using namespace std;
            int main(int argc, char** argv)
            {
                float Nx = -1.3787706641;
                float Sx = 25.0;
                double r = Nx + sqrt(Sx);
                if (abs(r - 3.621229) > 0.01)
                {
                    return -1;
                }
                return 0;
            }
            """
    )

    cflags = flags + ["-L" + d for d in std_lib_dirs()]
    compilation_result, execution_result = GCC_compiler.try_compile_tmp(
        test_code, tmp_prefix="try_march_", flags=cflags, try_run=True
    )
    return compilation_result, execution_result


class GCC_compiler(Compiler):
    # The equivalent flags of --march=native used by g++.
    march_flags = None

    supports_amdlibm = True

    @staticmethod
    def version_str():
        return config.cxx + " " + gcc_version_str

    @staticmethod
    def compile_args(march_flags=True):
        cxxflags = [flag for flag in config.gcc__cxxflags.split(" ") if flag]
        if "-fopenmp" in cxxflags:
            raise ValueError(
                "Do not use -fopenmp in Aesara flag gcc__cxxflags."
                " To enable OpenMP, use the Aesara flag openmp=True"
            )
        # Add the equivalent of -march=native flag.  We can't use
        # -march=native as when the compiledir is shared by multiple
        # computers (for example, if the home directory is on NFS), this
        # won't be optimum or cause crash depending if the file is compiled
        # on an older or more recent computer.
        # Those URL discuss how to find witch flags are used by -march=native.
        # http://en.gentoo-wiki.com/wiki/Safe_Cflags#-march.3Dnative
        # http://en.gentoo-wiki.com/wiki/Hardware_CFLAGS
        detect_march = GCC_compiler.march_flags is None and march_flags
        if detect_march:
            for f in cxxflags:
                # If the user give an -march=X parameter, don't add one ourself
                if f.startswith("--march=") or f.startswith("-march="):
                    detect_march = False
                    GCC_compiler.march_flags = []
                    break

        if (
            "g++" not in config.cxx
            and "clang++" not in config.cxx
            and "clang-omp++" not in config.cxx
            and "icpc" not in config.cxx
        ):
            _logger.warning(
                "Your Aesara flag `cxx` seems not to be"
                " the g++ compiler. So we disable the compiler optimization"
                " specific to g++ that tell to compile for a specific CPU."
                " At worst, this could cause slow down.\n"
                "         You can add those parameters to the compiler yourself"
                " via the Aesara flag `gcc__cxxflags`."
            )
            detect_march = False

        if detect_march:
            GCC_compiler.march_flags = []

            def get_lines(cmd, parse=True):
                p = subprocess_Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.PIPE,
                    shell=True,
                )
                # For mingw64 with GCC >= 4.7, passing os.devnull
                # as stdin (which is the default) results in the process
                # waiting forever without returning. For that reason,
                # we use a pipe, and use the empty string as input.
                (stdout, stderr) = p.communicate(input=b"")
                if p.returncode != 0:
                    return None

                lines = BytesIO(stdout + stderr).readlines()
                lines = (l.decode() for l in lines)
                if parse:
                    selected_lines = []
                    for line in lines:
                        if (
                            "COLLECT_GCC_OPTIONS=" in line
                            or "CFLAGS=" in line
                            or "CXXFLAGS=" in line
                            or "-march=native" in line
                        ):
                            continue
                        for reg in ("-march=", "-mtune=", "-target-cpu", "-mabi="):
                            if reg in line:
                                selected_lines.append(line.strip())
                    lines = list(set(selected_lines))  # to remove duplicate

                return lines

            # The '-' at the end is needed. Otherwise, g++ do not output
            # enough information.
            native_lines = get_lines(f"{config.cxx} -march=native -E -v -")
            if native_lines is None:
                _logger.info(
                    "Call to 'g++ -march=native' failed," "not setting -march flag"
                )
                detect_march = False
            else:
                _logger.info(f"g++ -march=native selected lines: {native_lines}")

        if detect_march:
            if len(native_lines) != 1:
                if len(native_lines) == 0:
                    # That means we did not select the right lines, so
                    # we have to report all the lines instead
                    reported_lines = get_lines(
                        f"{config.cxx} -march=native -E -v -", parse=False
                    )
                else:
                    reported_lines = native_lines
                _logger.warning(
                    "Aesara was not able to find the"
                    " g++ parameters that tune the compilation to your "
                    " specific CPU. This can slow down the execution of Aesara"
                    " functions. Please submit the following lines to"
                    " Aesara's mailing list so that we can fix this"
                    f" problem:\n {reported_lines}"
                )
            else:
                default_lines = get_lines(f"{config.cxx} -E -v -")
                _logger.info(f"g++ default lines: {default_lines}")
                if len(default_lines) < 1:
                    _logger.warning(
                        "Aesara was not able to find the"
                        " default g++ parameters. This is needed to tune"
                        " the compilation to your specific"
                        " CPU. This can slow down the execution of Aesara"
                        " functions. Please submit the following lines to"
                        " Aesara's mailing list so that we can fix this"
                        " problem:\n %s",
                        get_lines(f"{config.cxx} -E -v -", parse=False),
                    )
                else:
                    # Some options are actually given as "-option value",
                    # we want to treat them as only one token when comparing
                    # different command lines.
                    # Heuristic: tokens not starting with a dash should be
                    # joined with the previous one.
                    def join_options(init_part):
                        new_part = []
                        for i in range(len(init_part)):
                            p = init_part[i]
                            if p.startswith("-"):
                                p_list = [p]
                                while (i + 1 < len(init_part)) and not init_part[
                                    i + 1
                                ].startswith("-"):
                                    # append that next part to p_list
                                    p_list.append(init_part[i + 1])
                                    i += 1
                                new_part.append(" ".join(p_list))
                            elif i == 0:
                                # The first argument does not usually start
                                # with "-", still add it
                                new_part.append(p)
                            # Else, skip it, as it was already included
                            # with the previous part.
                        return new_part

                    part = join_options(native_lines[0].split())

                    for line in default_lines:
                        if line.startswith(part[0]):
                            part2 = [
                                p
                                for p in join_options(line.split())
                                if (
                                    "march" not in p
                                    and "mtune" not in p
                                    and "target-cpu" not in p
                                )
                            ]
                            if sys.platform == "darwin":
                                # We only use translated target-cpu on
                                # mac since the other flags are not
                                # supported as compiler flags for the
                                # driver.
                                new_flags = [p for p in part if "target-cpu" in p]
                            else:
                                new_flags = [p for p in part if p not in part2]
                            # Replace '-target-cpu value', which is an option
                            # of clang, with '-march=value'.
                            for i, p in enumerate(new_flags):
                                if "target-cpu" in p:
                                    opt = p.split()
                                    if len(opt) == 2:
                                        opt_name, opt_val = opt
                                        new_flags[i] = f"-march={opt_val}"

                            # Some versions of GCC report the native arch
                            # as "corei7-avx", but it generates illegal
                            # instructions, and should be "corei7" instead.
                            # Affected versions are:
                            # - 4.6 before 4.6.4
                            # - 4.7 before 4.7.3
                            # - 4.8 before 4.8.1
                            # Earlier versions did not have arch "corei7-avx"
                            for i, p in enumerate(new_flags):
                                if "march" not in p:
                                    continue
                                opt = p.split("=")
                                if len(opt) != 2:
                                    # Inexpected, but do not crash
                                    continue
                                opt_val = opt[1]
                                if not opt_val.endswith("-avx"):
                                    # OK
                                    continue
                                # Check the version of GCC
                                version = gcc_version_str.split(".")
                                if len(version) != 3:
                                    # Unexpected, but should not be a problem
                                    continue
                                mj, mn, patch = [int(vp) for vp in version]
                                if (
                                    ((mj, mn) == (4, 6) and patch < 4)
                                    or ((mj, mn) == (4, 7) and patch <= 3)
                                    or ((mj, mn) == (4, 8) and patch < 1)
                                ):
                                    new_flags[i] = p.rstrip("-avx")

                            # Go back to split arguments, like
                            # ["-option", "value"],
                            # as this is the way g++ expects them split.
                            split_flags = []
                            for p in new_flags:
                                split_flags.extend(p.split())

                            GCC_compiler.march_flags = split_flags
                            break
                    _logger.info(
                        f"g++ -march=native equivalent flags: {GCC_compiler.march_flags}"
                    )

            # Find working march flag:
            #   -- if current GCC_compiler.march_flags works, we're done.
            #   -- else replace -march and -mtune with ['core-i7-avx', 'core-i7', 'core2']
            #      and retry with all other flags and arguments intact.
            #   -- else remove all other flags and only try with -march = default + flags_to_try.
            #   -- if none of that worked, set GCC_compiler.march_flags = [] (for x86).

            default_compilation_result, default_execution_result = try_march_flag(
                GCC_compiler.march_flags
            )
            if not default_compilation_result or not default_execution_result:
                march_success = False
                march_ind = None
                mtune_ind = None
                default_detected_flag = []
                march_flags_to_try = ["corei7-avx", "corei7", "core2"]

                for m_ in range(len(GCC_compiler.march_flags)):
                    march_flag = GCC_compiler.march_flags[m_]
                    if "march" in march_flag:
                        march_ind = m_
                        default_detected_flag = [march_flag]
                    elif "mtune" in march_flag:
                        mtune_ind = m_

                for march_flag in march_flags_to_try:
                    if march_ind is not None:
                        GCC_compiler.march_flags[march_ind] = "-march=" + march_flag
                    if mtune_ind is not None:
                        GCC_compiler.march_flags[mtune_ind] = "-mtune=" + march_flag

                    compilation_result, execution_result = try_march_flag(
                        GCC_compiler.march_flags
                    )

                    if compilation_result and execution_result:
                        march_success = True
                        break

                if not march_success:
                    # perhaps one of the other flags was problematic; try default flag in isolation again:
                    march_flags_to_try = default_detected_flag + march_flags_to_try
                    for march_flag in march_flags_to_try:
                        compilation_result, execution_result = try_march_flag(
                            ["-march=" + march_flag]
                        )
                        if compilation_result and execution_result:
                            march_success = True
                            GCC_compiler.march_flags = ["-march=" + march_flag]
                            break

                if not march_success:
                    GCC_compiler.march_flags = []

        # Add the detected -march=native equivalent flags
        if march_flags and GCC_compiler.march_flags:
            cxxflags.extend(GCC_compiler.march_flags)

        # NumPy 1.7 Deprecate the old API.
        # The following macro asserts that we don't bring new code
        # that use the old API.
        cxxflags.append("-DNPY_NO_DEPRECATED_API=NPY_1_7_API_VERSION")

        # Platform-specific flags.
        # We put them here, rather than in compile_str(), so they en up
        # in the key of the compiled module, avoiding potential conflicts.

        # Figure out whether the current Python executable is 32
        # or 64 bit and compile accordingly. This step is ignored for
        # ARM (32-bit and 64-bit) and RISC-V architectures in order to make
        # Aesara compatible with the Raspberry Pi, Raspberry Pi 2, or
        # other systems with ARM or RISC-V processors.
        if (not any("arm" in flag or "riscv" in flag for flag in cxxflags)) and (
            not any(arch in platform.machine() for arch in ("arm", "aarch", "riscv"))
        ):
            n_bits = LOCAL_BITWIDTH
            cxxflags.append(f"-m{int(n_bits)}")
            _logger.debug(f"Compiling for {n_bits} bit architecture")

        if sys.platform != "win32":
            # Under Windows it looks like fPIC is useless. Compiler warning:
            # '-fPIC ignored for target (all code is position independent)'
            cxxflags.append("-fPIC")

        if sys.platform == "win32" and LOCAL_BITWIDTH == 64:
            # Under 64-bit Windows installation, sys.platform is 'win32'.
            # We need to define MS_WIN64 for the preprocessor to be able to
            # link with libpython.
            cxxflags.append("-DMS_WIN64")

        if sys.platform == "darwin":
            # Use the already-loaded python symbols.
            cxxflags.extend(["-undefined", "dynamic_lookup"])

        if sys.platform == "win32":
            # Workaround for https://github.com/Theano/Theano/issues/4926.
            # https://github.com/python/cpython/pull/11283/ removed the "hypot"
            # redefinition for recent CPython versions (>=2.7.16 and >=3.7.3).
            # The following nullifies that redefinition, if it is found.
            python_version = sys.version_info[:3]
            if (3,) <= python_version < (3, 7, 3):
                config_h_filename = distutils.sysconfig.get_config_h_filename()
                try:
                    with open(config_h_filename) as config_h:
                        if any(
                            line.startswith("#define hypot _hypot") for line in config_h
                        ):
                            cxxflags.append("-D_hypot=hypot")
                except OSError:
                    pass

        return cxxflags

    @classmethod
    def try_compile_tmp(
        cls,
        src_code,
        tmp_prefix="",
        flags=(),
        try_run=False,
        output=False,
        comp_args=True,
    ):
        return cls._try_compile_tmp(
            src_code, tmp_prefix, flags, try_run, output, config.cxx, comp_args
        )

    @classmethod
    def try_flags(
        cls,
        flag_list,
        preamble="",
        body="",
        try_run=False,
        output=False,
        comp_args=True,
    ):
        return cls._try_flags(
            flag_list, preamble, body, try_run, output, config.cxx, comp_args
        )

    @staticmethod
    def compile_str(
        module_name,
        src_code,
        location=None,
        include_dirs=None,
        lib_dirs=None,
        libs=None,
        preargs=None,
        py_module=True,
        hide_symbols=True,
    ):
        """
        Parameters
        ----------
        module_name : str
            This has been embedded in the src_code.
        src_code
            A complete c or c++ source listing for the module.
        location
            A pre-existing filesystem directory where the cpp file and .so will
            be written.
        include_dirs
            A list of include directory names (each gets prefixed with -I).
        lib_dirs
            A list of library search path directory names (each gets prefixed
            with -L).
        libs
            A list of libraries to link with (each gets prefixed with -l).
        preargs
            A list of extra compiler arguments.
        py_module
            If False, compile to a shared library, but do not import it as a
            Python module.
        hide_symbols
            If True (the default) all symbols will be hidden from the library
            symbol table (which means that other objects can't use them).

        Returns
        -------
        object
            Dynamically-imported python module of the compiled code (unless
            py_module is False, in that case returns None).

        """
        # TODO: Do not do the dlimport in this function

        if not config.cxx:
            raise MissingGXX("g++ not available! We can't compile c code.")

        if include_dirs is None:
            include_dirs = []
        if lib_dirs is None:
            lib_dirs = []
        if libs is None:
            libs = []
        if preargs is None:
            preargs = []

        # Remove empty string directory
        include_dirs = [d for d in include_dirs if d]
        lib_dirs = [d for d in lib_dirs if d]

        include_dirs = include_dirs + std_include_dirs()
        libs = libs + std_libs()
        lib_dirs = lib_dirs + std_lib_dirs()

        cppfilename = os.path.join(location, "mod.cpp")
        with open(cppfilename, "w") as cppfile:

            _logger.debug(f"Writing module C++ code to {cppfilename}")

            cppfile.write(src_code)
            # Avoid gcc warning "no newline at end of file".
            if not src_code.endswith("\n"):
                cppfile.write("\n")

        if platform.python_implementation() == "PyPy":
            suffix = "." + get_lib_extension()

            dist_suffix = distutils.sysconfig.get_config_var("SO")
            if dist_suffix is not None and dist_suffix != "":
                suffix = dist_suffix

            filepath = f"{module_name}{suffix}"
        else:
            filepath = f"{module_name}.{get_lib_extension()}"

        lib_filename = os.path.join(location, filepath)

        _logger.debug(f"Generating shared lib {lib_filename}")
        cmd = [config.cxx, get_gcc_shared_library_arg(), "-g"]

        if config.cmodule__remove_gxx_opt:
            cmd.extend(p for p in preargs if not p.startswith("-O"))
        else:
            cmd.extend(preargs)
        # to support path that includes spaces, we need to wrap it with double quotes on Windows
        path_wrapper = '"' if os.name == "nt" else ""
        cmd.extend([f"-I{path_wrapper}{idir}{path_wrapper}" for idir in include_dirs])
        cmd.extend([f"-L{path_wrapper}{ldir}{path_wrapper}" for ldir in lib_dirs])
        if hide_symbols and sys.platform != "win32":
            # This has been available since gcc 4.0 so we suppose it
            # is always available. We pass it here since it
            # significantly reduces the size of the symbol table for
            # the objects we want to share. This in turns leads to
            # improved loading times on most platforms (win32 is
            # different, as usual).
            cmd.append("-fvisibility=hidden")
        cmd.extend(["-o", f"{path_wrapper}{lib_filename}{path_wrapper}"])
        cmd.append(f"{path_wrapper}{cppfilename}{path_wrapper}")
        cmd.extend([f"-l{l}" for l in libs])
        # print >> sys.stderr, 'COMPILING W CMD', cmd
        _logger.debug(f"Running cmd: {' '.join(cmd)}")

        def print_command_line_error():
            # Print command line when a problem occurred.
            print(
                ("Problem occurred during compilation with the " "command line below:"),
                file=sys.stderr,
            )
            print(" ".join(cmd), file=sys.stderr)

        try:
            p_out = output_subprocess_Popen(cmd)
            compile_stderr = p_out[1].decode()
        except Exception:
            # An exception can occur e.g. if `g++` is not found.
            print_command_line_error()
            raise

        status = p_out[2]

        if status:
            tf = tempfile.NamedTemporaryFile(
                mode="w", prefix="aesara_compilation_error_", delete=False
            )
            # gcc put its messages to stderr, so we add ours now
            tf.write("===============================\n")
            for i, l in enumerate(src_code.split("\n")):
                tf.write(f"{i + 1}\t{l}\n")
            tf.write("===============================\n")
            tf.write(
                "Problem occurred during compilation with the " "command line below:\n"
            )
            tf.write(" ".join(cmd))
            # Print errors just below the command line.
            tf.write(compile_stderr)
            tf.close()
            print("\nYou can find the C code in this temporary file: " + tf.name)
            not_found_libraries = re.findall('-l["."-_a-zA-Z0-9]*', compile_stderr)
            for nf_lib in not_found_libraries:
                print("library " + nf_lib[2:] + " is not found.")
                if re.search('-lPYTHON["."0-9]*', nf_lib, re.IGNORECASE):
                    py_string = re.search(
                        '-lpython["."0-9]*', nf_lib, re.IGNORECASE
                    ).group()[8:]
                    if py_string != "":
                        print(
                            "Check if package python-dev "
                            + py_string
                            + " or python-devel "
                            + py_string
                            + " is installed."
                        )
                    else:
                        print(
                            "Check if package python-dev or python-devel is installed."
                        )

            # We replace '\n' by '. ' in the error message because when Python
            # prints the exception, having '\n' in the text makes it more
            # difficult to read.
            # compile_stderr = compile_stderr.replace("\n", ". ")
            raise CompileError(
                f"Compilation failed (return status={status}):\n{' '.join(cmd)}\n{compile_stderr}"
            )
        elif config.cmodule__compilation_warning and compile_stderr:
            # Print errors just below the command line.
            print(compile_stderr)

        if py_module:
            # touch the __init__ file
            open(os.path.join(location, "__init__.py"), "w").close()
            assert os.path.isfile(lib_filename)
            return dlimport(lib_filename)


def icc_module_compile_str(*args):
    raise NotImplementedError()


def check_mkl_openmp():
    if not config.blas__check_openmp:
        return
    if sys.platform == "darwin":
        return
    if (
        "MKL_THREADING_LAYER" in os.environ
        and os.environ["MKL_THREADING_LAYER"] == "GNU"
    ):
        return
    try:
        import numpy._mklinit  # noqa

        return
    except ImportError:
        pass
    try:
        import mkl

        if "2018" in mkl.get_version_string():
            raise RuntimeError(
                """
To use MKL 2018 with Aesara either update the numpy conda packages to
their latest build or set "MKL_THREADING_LAYER=GNU" in your
environment.
"""
            )
    except ImportError:
        raise RuntimeError(
            """
Could not import 'mkl'.  If you are using conda, update the numpy
packages to the latest build otherwise, set MKL_THREADING_LAYER=GNU in
your environment for MKL 2018.

If you have MKL 2017 install and are not in a conda environment you
can set the Aesara flag blas__check_openmp to False.  Be warned that if
you set this flag and don't set the appropriate environment or make
sure you have the right version you *will* get wrong results.
"""
        )


def default_blas_ldflags():
    """Read local NumPy and MKL build settings and construct `ld` flags from them.

    Returns
    -------
    str

    """
    import numpy.distutils  # noqa

    warn_record = []
    try:
        # We do this import only here, as in some setup, if we
        # just import aesara and exit, with the import at global
        # scope, we get this error at exit: "Exception TypeError:
        # "'NoneType' object is not callable" in <bound method
        # Popen.__del__ of <subprocess.Popen object at 0x21359d0>>
        # ignored"

        # This happen with Python 2.7.3 |EPD 7.3-1 and numpy 1.8.1
        # isort: off
        import numpy.distutils.system_info  # noqa

        # We need to catch warnings as in some cases NumPy print
        # stuff that we don't want the user to see.
        # I'm not able to remove all printed stuff
        with warnings.catch_warnings(record=True):
            numpy.distutils.system_info.system_info.verbosity = 0
            blas_info = numpy.distutils.system_info.get_info("blas_opt")

        # If we are in a EPD installation, mkl is available
        if "EPD" in sys.version:
            use_unix_epd = True
            if sys.platform == "win32":
                return " ".join(
                    ['-L"%s"' % os.path.join(sys.prefix, "Scripts")]
                    +
                    # Why on Windows, the library used are not the
                    # same as what is in
                    # blas_info['libraries']?
                    [f"-l{l}" for l in ("mk2_core", "mk2_intel_thread", "mk2_rt")]
                )
            elif sys.platform == "darwin":
                # The env variable is needed to link with mkl
                new_path = os.path.join(sys.prefix, "lib")
                v = os.getenv("DYLD_FALLBACK_LIBRARY_PATH", None)
                if v is not None:
                    # Explicit version could be replaced by a symbolic
                    # link called 'Current' created by EPD installer
                    # This will resolve symbolic links
                    v = os.path.realpath(v)

                # The python __import__ don't seam to take into account
                # the new env variable "DYLD_FALLBACK_LIBRARY_PATH"
                # when we set with os.environ['...'] = X or os.putenv()
                # So we warn the user and tell him what todo.
                if v is None or new_path not in v.split(":"):
                    _logger.warning(
                        "The environment variable "
                        "'DYLD_FALLBACK_LIBRARY_PATH' does not contain "
                        "the '{new_path}' path in its value. This will make "
                        "Aesara use a slow version of BLAS. Update "
                        "'DYLD_FALLBACK_LIBRARY_PATH' to contain the "
                        "said value, this will disable this warning."
                    )

                    use_unix_epd = False
            if use_unix_epd:
                return " ".join(
                    ["-L%s" % os.path.join(sys.prefix, "lib")]
                    + ["-l%s" % l for l in blas_info["libraries"]]
                )

                # Canopy
        if "Canopy" in sys.prefix:
            subsub = "lib"
            if sys.platform == "win32":
                subsub = "Scripts"
            lib_path = os.path.join(sys.base_prefix, subsub)
            if not os.path.exists(lib_path):
                # Old logic to find the path. I don't think we still
                # need it, but I don't have the time to test all
                # installation configuration. So I keep this as a fall
                # back in case the current expectation don't work.

                # This old logic don't work when multiple version of
                # Canopy is installed.
                p = os.path.join(sys.base_prefix, "..", "..", "appdata")
                assert os.path.exists(p), "Canopy changed the location of MKL"
                lib_paths = os.listdir(p)
                # Try to remove subdir that can't contain MKL
                for sub in lib_paths:
                    if not os.path.exists(os.path.join(p, sub, subsub)):
                        lib_paths.remove(sub)
                assert len(lib_paths) == 1, (
                    "Unexpected case when looking for Canopy MKL libraries",
                    p,
                    lib_paths,
                    [os.listdir(os.path.join(p, sub)) for sub in lib_paths],
                )
                lib_path = os.path.join(p, lib_paths[0], subsub)
                assert os.path.exists(lib_path), "Canopy changed the location of MKL"

            if sys.platform == "linux2" or sys.platform == "darwin":
                return " ".join(
                    ["-L%s" % lib_path] + ["-l%s" % l for l in blas_info["libraries"]]
                )
            elif sys.platform == "win32":
                return " ".join(
                    ['-L"%s"' % lib_path]
                    +
                    # Why on Windows, the library used are not the
                    # same as what is in blas_info['libraries']?
                    [f"-l{l}" for l in ("mk2_core", "mk2_intel_thread", "mk2_rt")]
                )

        # MKL
        # If mkl can be imported then use it. On conda:
        # "conda install mkl-service" installs the Python wrapper and
        # the low-level C libraries as well as optimised version of
        # numpy and scipy.
        try:
            import mkl  # noqa
        except ImportError:
            pass
        else:
            # This branch is executed if no exception was raised
            if sys.platform == "win32":
                lib_path = os.path.join(sys.prefix, "Library", "bin")
                flags = [f'-L"{lib_path}"']
            else:
                lib_path = blas_info.get("library_dirs", [])
                flags = []
                if lib_path:
                    flags = [f"-L{lib_path[0]}"]
            if "2018" in mkl.get_version_string():
                thr = "mkl_gnu_thread"
            else:
                thr = "mkl_intel_thread"
            base_flags = list(flags)
            flags += [f"-l{l}" for l in ("mkl_core", thr, "mkl_rt")]
            res = try_blas_flag(flags)

            if not res and sys.platform == "win32" and thr == "mkl_gnu_thread":
                # Check if it would work for intel OpenMP on windows
                flags = base_flags + [
                    f"-l{l}" for l in ("mkl_core", "mkl_intel_thread", "mkl_rt")
                ]
                res = try_blas_flag(flags)

            if res:
                check_mkl_openmp()
                return res

            flags.extend(["-Wl,-rpath," + l for l in blas_info.get("library_dirs", [])])
            res = try_blas_flag(flags)
            if res:
                check_mkl_openmp()
                maybe_add_to_os_environ_pathlist("PATH", lib_path[0])
                return res

        # to support path that includes spaces, we need to wrap it with double quotes on Windows
        path_wrapper = '"' if os.name == "nt" else ""
        ret = (
            # TODO: the Gemm op below should separate the
            # -L and -l arguments into the two callbacks
            # that CLinker uses for that stuff.  for now,
            # we just pass the whole ldflags as the -l
            # options part.
            [
                f"-L{path_wrapper}{l}{path_wrapper}"
                for l in blas_info.get("library_dirs", [])
            ]
            + [f"-l{l}" for l in blas_info.get("libraries", [])]
            + blas_info.get("extra_link_args", [])
        )
        # For some very strange reason, we need to specify -lm twice
        # to get mkl to link correctly.  I have no idea why.
        if any("mkl" in fl for fl in ret):
            ret.extend(["-lm", "-lm"])
        res = try_blas_flag(ret)
        if res:
            if "mkl" in res:
                check_mkl_openmp()
            return res

        # If we are using conda and can't reuse numpy blas, then doing
        # the fallback and test -lblas could give slow computation, so
        # warn about this.
        for warn in warn_record:
            _logger.warning(warn)
        del warn_record

        # Some environment don't have the lib dir in LD_LIBRARY_PATH.
        # So add it.
        ret.extend(["-Wl,-rpath," + l for l in blas_info.get("library_dirs", [])])
        res = try_blas_flag(ret)
        if res:
            if "mkl" in res:
                check_mkl_openmp()
            return res

        # Add sys.prefix/lib to the runtime search path. On
        # non-system installations of Python that use the
        # system linker, this is generally necessary.
        if sys.platform in ("linux", "darwin"):
            lib_path = os.path.join(sys.prefix, "lib")
            ret.append("-Wl,-rpath," + lib_path)
            res = try_blas_flag(ret)
            if res:
                if "mkl" in res:
                    check_mkl_openmp()
                return res

    except KeyError:
        pass

    # Even if we could not detect what was used for numpy, or if these
    # libraries are not found, most Linux systems have a libblas.so
    # readily available. We try to see if that's the case, rather
    # than disable blas. To test it correctly, we must load a program.
    # Otherwise, there could be problem in the LD_LIBRARY_PATH.
    return try_blas_flag(["-lblas"])


def add_blas_configvars():
    config.add(
        "blas__ldflags",
        "lib[s] to include for [Fortran] level-3 blas implementation",
        StrParam(default_blas_ldflags),
        # Added elsewhere in the c key only when needed.
        in_c_key=False,
    )

    config.add(
        "blas__check_openmp",
        "Check for openmp library conflict.\nWARNING: Setting this to False leaves you open to wrong results in blas-related operations.",
        BoolParam(True),
        in_c_key=False,
    )


# Register config parameters that are specific to this module:
add_blas_configvars()
