"""
Classes defining Abinit calculations and workflows
"""
from __future__ import division, print_function

import os
import time
import shutil
import collections
import abc
import warnings
import copy
import numpy as np

from pymatgen.core.design_patterns import Enum, AttrDict
from pymatgen.util.string_utils import stream_has_colours, is_string, list_strings
from pymatgen.serializers.json_coders import MSONable, json_load, json_pretty_dump

from pymatgen.io.abinitio.utils import abinit_output_iscomplete, File
from pymatgen.io.abinitio.events import EventParser
from pymatgen.io.abinitio.qadapters import qadapter_class

import logging
logger = logging.getLogger(__name__)

__author__ = "Matteo Giantomassi"
__copyright__ = "Copyright 2013, The Materials Project"
__version__ = "0.1"
__maintainer__ = "Matteo Giantomassi"

__all__ = [
    "task_factory",
    "TaskManager",
]


class FakeProcess(object):
    """
    This object is attached to a Task instance if the task has not been submitted
    This trick allows us to simulate a process that is still running so that we can safely poll task.process.
    """
    def poll(self):
        return None

    def wait(self):
        raise RuntimeError("Cannot wait a FakeProcess")

    def communicate(self, input=None):
        raise RuntimeError("Cannot communicate with a FakeProcess")

    def kill(self):
        raise RuntimeError("Cannot kill a FakeProcess")

    @property
    def returncode(self):
        return None


def task_factory(strategy, workdir, manager, task_id=1, links=None, **kwargs):
    """Factory function for Task instances."""
    # TODO
    # Instanciate subclasses depending on the runlevel.
    classes = {
       "scf": AbinitTask,
       "nscf": AbinitTask,
       "relax": AbinitTask,
       "dfpt": AbinitTask,
       "screening": AbinitTask,
       "sigma": AbinitTask,
       "bse": AbinitTask,
    }

    return classes[strategy.runlevel](strategy, workdir, manager, task_id=task_id, links=links, **kwargs)


class TaskError(Exception):
    """Base Exception for Task methods"""


class Task(object):
    __metaclass__ = abc.ABCMeta

    Error = TaskError

    # Possible status of the task.
    S_READY = 1    # Task is ready for submission.
    S_SUB = 2      # Task has been submitted.
    S_RUN = 4      # Task is running.
    S_DONE = 8     # Task done, This does not imply that results are ok or that the calculation completed successfully
    S_ERROR = 16   # Task raised some kind of Error (either the process or ABINIT).
    S_OK = 32      # Task completed successfully.

    STATUS2STR = collections.OrderedDict([
        (S_READY, "Ready"),
        (S_SUB, "Submitted"),
        (S_RUN, "Running"),
        (S_DONE, "Done"),
        (S_ERROR, "Error"),
        (S_OK, "Completed"),
    ])

    POSSIBLE_STATUS = STATUS2STR.keys()

    def __init__(self):
        # Set the initial status.
        self.set_status(Task.S_READY)

        # Used to push additional info on the task during its execution. 
        self.history = collections.deque(maxlen=10)

    def __getstate__(self):
        """
        Return state is pickled as the contents for the instance.
                                                                                      
        In this case we just remove the process since Subprocess objects cannot be pickled.
        This is the reason why we have to store the returncode in self_returncode instead
        of using self.process.returncode.
        """
        return {k:v for k,v in self.__dict__.items() if k not in ["_process",]}

    @abc.abstractproperty
    def executable(self):
        """
        Path to the executable associated to the task (internally stored in self._executable).
        """

    def set_executable(self, executable):
        """Set the executable associate to this task."""
        self._executable = executable

    # Interface modeled after subprocess.Popen
    @abc.abstractproperty
    def process(self):
        """Return an object that supports the `subprocess.Popen` protocol."""

    def poll(self):
        """Check if child process has terminated. Set and return returncode attribute."""
        self._returncode = self.process.poll()

        if self._returncode is not None:
            self.set_status(Task.S_DONE)

        return self._returncode

    def wait(self):
        """Wait for child process to terminate. Set and return returncode attribute."""
        self._returncode = self.process.wait()
        self.set_status(Task.S_DONE)

        return self._returncode

    def communicate(self, input=None):
        """
        Interact with process: Send data to stdin. Read data from stdout and stderr, until end-of-file is reached. 
        Wait for process to terminate. The optional input argument should be a string to be sent to the 
        child process, or None, if no data should be sent to the child.

        communicate() returns a tuple (stdoutdata, stderrdata).
        """
        stdoutdata, stderrdata = self.process.communicate(input=input)
        self._returncode = self.process.returncode
        self.set_status(Task.S_DONE)

        return stdoutdata, stderrdata 

    def kill(self):
        """Kill the child."""
        self.process.kill()
        self._returncode = self.process.returncode

    @property
    def returncode(self):
        """
        The child return code, set by poll() and wait() (and indirectly by communicate()). 
        A None value indicates that the process hasn't terminated yet.
        A negative value -N indicates that the child was terminated by signal N (Unix only).
        """
        try: 
            return self._returncode
        except AttributeError:
            return 0

    @property
    def id(self):
        """Task identifier."""
        return self._id

    def set_id(self, id):
        """Set the task identifier."""
        self._id = id

    @property
    def queue_id(self):
        """Queue identifier returned by the Queue manager. None if not set"""
        try:
            return self._queue_id
        except AttributeError:
            return None

    def set_queue_id(self, queue_id):
        """Set the task identifier."""
        self._queue_id = queue_id

    @property
    def tot_ncpus(self):
        """Total number of CPUs used to run the task."""
        return self.manager.tot_ncpus
                                                         
    @property
    def mpi_ncpus(self):
        """Number of CPUs used for MPI."""
        return self.manager.mpi_ncpus
                                                         
    @property
    def omp_ncpus(self):
        """Number of CPUs used for OpenMP."""
        return self.manager.omp_ncpus

    @property
    def status(self):
        """Gives the status of the task."""
        return self._status

    @property
    def str_status(self):
        """String representation of the status."""
        return self.STATUS2STR[self.status]

    def set_status(self, status, err_info=None):
        """Set the status of the task."""

        if status not in self.POSSIBLE_STATUS:
            raise RuntimeError("Unknown status: %s" % status)

        changed = False
        if hasattr(self, "_status"):
            changed = status != self._status

        self._status = status

        if changed:
            if status == self.S_SUB: 
                self._submission_time = time.time()
                self.history.append("Submitted on %s" % time.asctime())

            if status == self.S_OK:
                self.history.append("Completed on %s" % time.asctime())

            if status == self.S_ERROR:
                self.history.append("Error info:\n %s" % str(err_info))

            # Notify the observers
            #self.notify_observers(self)

        return status

    def check_status(self):
        if self.status < self.S_SUB:
            return

        # Check the returncode of the process first.
        if self.returncode != 0:
            return self.set_status(self.S_ERROR, err_info="return code %s" % self.returncode)

        # Start to check when the output file has been created.
        if not self.output_file.exists:
            #print("out does not exists")
            if not self.stderr_file.exists:
                # The job is still in the queue.
                return self.status
            else:
                # TODO check possible errors in stderr and queue manager 
                err_msg = self.stderr_file.read()
                if err_msg:
                    logger.critical("stderr message:\n" + err_msg)
                    return self.set_status(self.S_ERROR, err_info=err_msf)

        # Check if the run completed successfully.
        parser = EventParser()

        try:
            report = parser.parse(self.output_file.path)

        except parser.Error as exc:
            logger.critical("Exception while parsing ABINIT events:\n" + str(exc))
            return self.set_status(self.S_ERROR, err_info=str(exc))

        if report.run_completed:
            return self.set_status(self.S_OK)

        # This is the delicate part since we have to discern among different possibilities:
        #
        # 1) Calculation stopped due to an Abinit Error or Bug.
        #
        # 2) Segmentation fault that (by definition) was not handled by ABINIT.
        #    In this case we check if the ABINIT error file is not empty.
        #
        # 3) Problem with the resource manager and/or the OS (walltime error, resource error, phase of the moon ...)
        #    In this case we check if the error file of the queue manager is not empty.
        # 
        # 4) Calculation is still running!
        #
        # Point 2) and 3) are the most complicated since there's no standard!

        # 1) Search for possible errors or bugs in the ABINIT **output** file.
        if report.errors or report.bugs:
            logger.critical("Found Errors or Bugs in ABINIT main output!")
            return self.set_status(self.S_ERROR, err_info=str(report.errors) + str(report_bus))

        # 2) Analyze the stderr file for Fortran runtime errors.
        if self.stderr_file.exists:
            err_info = self.stderr_file.read()
            if err_info:
                return self.set_status(self.S_ERROR, err_info=err_info)

        # 3) Analyze the error file of the resource manager.
        if self.qerr_file.exists:
            err_info = self.qerr_file.read()
            if err_info:
                return self.set_status(self.S_ERROR, err_info=err_info)

        # 4) Assume the job is still running.
        return self.set_status(self.S_RUN)

    @property
    def links(self):
        """
        Iterable with the links of the status. 
        Empty list if self is not connected to other tasks.
        """
        return self._links

    @property
    def links_status(self):
        """Returns a list with the status of the links."""
        if not self.links:
            return [Task.S_OK]

        return [l.status for l in self.links]

    @property
    def is_allocated(self):
        """True if the task has been allocated, i.e. if it has been submitted or if it's running."""
        return self.status in [Task.S_SUB, Task.S_RUN]

    @property
    def is_completed(self):
        """True if the task has been executed."""
        return self.status >= Task.S_DONE

    @property
    def can_run(self):
        """The task can run if its status is S_READY and all the other links (if any) are done!"""
        return (self.status == Task.S_READY) and all([stat >= Task.S_DONE for stat in self.links_status])

    def connect(self):
        """Create symbolic links to the output files produced by the other tasks."""
        for link in self.links:
            filepaths, exts = link.get_filepaths_and_exts()
            for (path, ext) in zip(filepaths, exts):
                dst = self.idata_path_from_ext(ext)

                if not os.path.exists(path): 
                    # Try netcdf file.
                    # TODO: this case should be treated in a cleaner way.
                    path += "-etsf.nc"
                    if os.path.exists(path):
                        dst += "-etsf.nc"

                if not os.path.exists(path):
                    raise self.Error("%s is needed by this task but it does not exist" % path)

                # Link path to dst if dst link does not exist.
                # else check that it points to the expected file.
                logger.debug("Linking path %s --> %s" % (path, dst))
                if not os.path.exists(dst):
                    os.symlink(path, dst)
                else:
                    try:
                        assert os.path.realpath(dst) == path
                    except AssertionError as exc:
                        raise self.Error(str(exc))

    #@abc.abstractmethod
    #def restart(self, *args, **kwargs):

    @abc.abstractmethod
    def setup(self, *args, **kwargs):
        """Public method called before submitting the task."""

    def _setup(self, *args, **kwargs):
        """
        This method calls self.setup after having performed additional operations
        such as the creation of the symbolic links needed to connect different tasks.
        """
        self.connect()
        self.setup(*args, **kwargs)

    def get_event_report(self):
        return self.parse_events()

    def parse_events(self):
        """
        Analyzes the main output for possible errors or warnings.

        Returns:
            `EventReport` instance or None if the main output file does not exist.
        """
        if not os.path.exists(self.output_file.path):
            return None

        parser = EventParser()
        try:
            return parser.parse(self.output_file.path)

        except parser.Error as exc:
            logger.critical("Exception while parsing ABINIT events:\n" + str(exc))
            raise
            # TODO: Handle possible errors in the parser by generating a custom EventList object
            #return EventList(self.output_file.path, events=[Error(str(exc))])

    @property
    def events(self):
        """List of errors or warnings reported by ABINIT."""
        if self.status is None or self.status < self.S_DONE:
            raise self.Error(
                "Task %s is not completed.\nYou cannot access its events now, use parse_events" % repr(self))

        try:
            return self._events

        except AttributeError:
            self._events = self.parse_events()
            return self._events

    @property
    def results(self):
        """The results produced by the task. Set by get_results"""
        try:
            return self._results

        except AttributeError:
            self._results = self.get_results()
            return self._results 

    def get_results(self, *args, **kwargs):
        """
        Method called once the calculation is completed, 
        Updates self._results and returns TaskResults instance.
        Subclasses should extend this method (if needed) by adding 
        specialized code that performs some kind of post-processing.
        """
        # Check whether the process completed.
        if self.returncode is None:
            raise self.Error("return code is None, you should call wait, communitate or poll")

        if self.status is None or self.status < self.S_DONE:
            raise self.Error("Task is not completed")

        return TaskResults({
            "task_name"      : self.name,
            "task_returncode": self.returncode,
            "task_status"    : self.status,
            "task_events"    : self.events.to_dict
        })

    def move(self, dst, isabspath=False):
        """
        Recursively move self.workdir to another location. This is similar to the Unix "mv" command.
        The destination path must not already exist. If the destination already exists
        but is not a directory, it may be overwritten depending on os.rename() semantics.

        Be default, dst is located in the parent directory of self.workdir, use isabspath=True
        to specify an absolute path.
        """
        if not isabspath:
            dst = os.path.join(os.path.dirname(self.workdir), dst)

        shutil.move(self.workdir, dst)


class AbinitTask(Task):
    """
    Base class defining an ABINIT calculation
    """
    # Prefixes for Abinit (input, output, temporary) files.
    Prefix = collections.namedtuple("Prefix", "idata odata tdata")
    pj = os.path.join

    prefix = Prefix(pj("indata", "in"), pj("outdata", "out"), pj("tmpdata", "tmp"))
    del Prefix, pj

    def __init__(self, strategy, workdir, manager, task_id=1, links=None, **kwargs):
        """
        Args:
            strategy: 
                `Strategy` instance describing the calculation.
            workdir:
                Path to the working directory.
            manager:
                `TaskManager` object.
            policy:
                `TaskPolicy` object.
            task_id:
                Task identifier (must be unique if self belongs to a `Workflow`).
            links:
                List of `WorkLink` objects specifying the dependencies of the task.
                Used for tasks belonging to a `Workflow`.
            kwargs:
                keyword arguments (not used for the time being)
        """
        super(AbinitTask, self).__init__()

        self.workdir = os.path.abspath(workdir)

        self.strategy = strategy

        self.manager = manager

        self.set_id(task_id)

        # Connect this task to the other tasks 
        # (needed if are creating a Work instance with dependencies
        self._links = []
        if links is not None: 
            if not isinstance(links, collections.Iterable):
                links = [links,]

            self._links = links
            for link in links:
                logger.debug("Adding abivars %s " % str(link.get_abivars()))
                self.strategy.add_extra_abivars(link.get_abivars())

        # Files required for the execution.
        self.input_file = File(os.path.join(self.workdir, "run.abi"))
        self.output_file = File(os.path.join(self.workdir, "run.abo"))
        self.files_file = File(os.path.join(self.workdir, "run.files"))
        self.job_file = File(os.path.join(self.workdir, "job.sh"))
        self.log_file = File(os.path.join(self.workdir, "run.log"))
        self.stderr_file = File(os.path.join(self.workdir, "run.err"))

        # stderr and output file of the queue manager.
        self.qerr_file = File(os.path.join(self.workdir, "queue.err"))
        self.qout_file = File(os.path.join(self.workdir, "queue.out"))

    def __repr__(self):
        return "<%s at %s, workdir = %s>" % (
            self.__class__.__name__, id(self), os.path.basename(self.workdir))

    def __str__(self):
        return self.__repr__()

    @classmethod
    def from_input(cls, abinit_input, workdir, manager, task_id=1, links=None, **kwargs):
        """
        Create an instance of `AbinitTask` from an ABINIT input.

        Args:
            abinit_input:
                `AbinitInput` object.
            workdir:
                Path to the working directory.
            manager:
                `TaskManager` object.
            policy:
                `TaskPolicy` object.
            task_id:
                Task identifier (must be unique if self belongs to a `Workflow`).
            links:
                List of `WorkLink` objects specifying the dependencies of the task.
                Used for tasks belonging to a `Workflow`.
            kwargs:
                keyword arguments (not used here)
        """
        # TODO: Find a better way to do this. I will likely need to refactor the Strategy object
        from pymatgen.io.abinitio.strategies import StrategyWithInput
        strategy = StrategyWithInput(abinit_input)

        return cls(strategy, workdir, manager, task_id=task_id, links=links, **kwargs)

    @property
    def executable(self):
        try:
            return self._executable
        except AttributeError:
            return "abinit"

    #def set_name(name):
    #    self._name = name

    @property
    def name(self):
        return self.workdir
        #try:
        #    return self._name
        #except AttributeError:

    @property
    def short_name(self):
        return os.path.basename(self.workdir)

    @property
    def process(self):
        try:
            return self._process
        except AttributeError:
            # Attach a fake process so that we can poll it.
            return FakeProcess()

    @property
    def indata_dir(self):
        """Directory with the input data."""
        head, tail = os.path.split(self.prefix.idata)
        return os.path.join(self.workdir, head)

    @property
    def outdata_dir(self):
        """Directory with the output data."""
        head, tail = os.path.split(self.prefix.odata)
        return os.path.join(self.workdir, head)

    @property
    def tmpdata_dir(self):
        """Directory with the temporary data."""
        head, tail = os.path.split(self.prefix.tdata)
        return os.path.join(self.workdir, head)

    def idata_path_from_ext(self, ext):
        """Returns the path of the input file with extension ext."""
        return os.path.join(self.workdir, self.prefix.idata + ext)

    def odata_path_from_ext(self, ext):
        """Returns the path of the output file with extension ext"""
        return os.path.join(self.workdir, self.prefix.odata + ext)

    @property
    def pseudos(self):
        """List of pseudos used in the calculation."""
        return self.strategy.pseudos

    @property
    def filesfile_string(self):
        """String with the list of files and prefixex needed to execute ABINIT."""
        lines = []
        app = lines.append
        pj = os.path.join

        app(self.input_file.path)                 # Path to the input file
        app(self.output_file.path)                # Path to the output file
        app(pj(self.workdir, self.prefix.idata))  # Prefix for in data
        app(pj(self.workdir, self.prefix.odata))  # Prefix for out data
        app(pj(self.workdir, self.prefix.tdata))  # Prefix for tmp data

        # Paths to the pseudopotential files.
        for pseudo in self.pseudos:
            app(pseudo.path)

        return "\n".join(lines)

    @property
    def to_dict(self):
        raise NotImplementedError("")
        d = {k: v.to_dict for k, v in self.items()}
        d["@module"] = self.__class__.__module__
        d["@class"] = self.__class__.__name__
        d["strategy"] = self.strategy 
        d["workdir"] = workdir 
        return d
                                                                    
    @staticmethod
    def from_dict(d):
        raise NotImplementedError("")

    def isdone(self):
        """True if the output file is complete."""
        return abinit_output_iscomplete(self.output_file.path)

    @property
    def isnc(self):
        """True if norm-conserving calculation."""
        return all(p.isnc for p in self.pseudos)

    @property
    def ispaw(self):
        """True if PAW calculation"""
        return all(p.ispaw for p in self.pseudos)

    def make_input(self):
        """Construct and write the input file of the calculation."""
        return self.strategy.make_input()

    def outfiles(self):
        """Return all the output data files produced."""
        files = []
        for fname in os.listdir(self.outdata_dir):
            if fname.startswith(os.path.basename(self.prefix.odata)):
                files.append(os.path.join(self.outdata_dir, fname))

        return files
                                                                  
    def tmpfiles(self):
        """Return all the input data files produced."""
        files = []
        for fname in os.listdir(self.tmpdata_dir):
            if file.startswith(os.path.basename(self.prefix.tdata)):
                files.append(os.path.join(self.tmpdata_dir, fname))

        return files

    def path_in_workdir(self, filename):
        """Create the absolute path of filename in the top-level working directory."""
        return os.path.join(self.workdir, filename)

    def path_in_indatadir(self, filename):
        """Create the absolute path of filename in the indata directory."""
        return os.path.join(self.indata_dir, filename)

    def path_in_outdatadir(self, filename):
        """Create the absolute path of filename in the outdata directory."""
        return os.path.join(self.outdata_dir, filename)

    def path_in_tmpdatadir(self, filename):
        """Create the absolute path of filename in the tmp directory."""
        return os.path.join(self.tmpdata_dir, filename)

    def rename(self, src_basename, dest_basename, datadir="outdir"):
        """
        Rename a file located in datadir.

        src_basename and dest_basename are the basename of the source file
        and of the destination file, respectively.
        """
        if datadir == "outdir":
            src = self.path_in_outdatadir(src_basename)
            dest = self.path_in_outdatadir(dest_basename)

        elif datadir == "tmpdir":
            src = self.path_in_tmpdatadir(src_basename)
            dest = self.path_in_tmpdatadir(dest_basename)

        else:
            raise ValueError("Wrong datadir %s" % datadir)

        os.rename(src, dest)

    def build(self, *args, **kwargs):
        """
        Creates the working directory and the input files of the `Task`.
        It does not overwrite files if they already exist.
        """
        # Top-level dir
        if not os.path.exists(self.workdir):
            os.makedirs(self.workdir)

        # Create dirs for input, output and tmp data.
        if not os.path.exists(self.indata_dir):
            os.makedirs(self.indata_dir)

        if not os.path.exists(self.outdata_dir):
            os.makedirs(self.outdata_dir)

        if not os.path.exists(self.tmpdata_dir):
            os.makedirs(self.tmpdata_dir)

        # Write files file and input file.
        if not self.files_file.exists:
            self.files_file.write(self.filesfile_string)

        self.input_file.write(self.make_input())

        self.manager.write_jobfile(self)

    def rmtree(self, exclude_wildcard=""):
        """
        Remove all files and directories in the working directory

        Args:
            exclude_wildcard:
                Optional string with regular expressions separated by |.
                Files matching one of the regular expressions will be preserved.
                example: exclude_wildcard="*.nc|*.txt" preserves all the files
                whose extension is in ["nc", "txt"].
        """
        if not exclude_wildcard:
            shutil.rmtree(self.workdir)

        else:
            #w = WildCard(exclude_wildcards)
            pats = exclude_wildcard.split("|")

            import fnmatch

            def keep(fname):
                """True if fname must be preserved."""
                for pat in pats:
                    if fnmatch.fnmatch(fname, pat):
                        return True
                return False

            for dirpath, dirnames, filenames in os.walk(self.workdir):
                for fname in filenames:
                    filepath = os.path.join(dirpath, fname)
                    if not keep(fname):
                        os.remove(filepath)

    def remove_files(self, *filenames):
        """Remove all the files listed in filenames."""
        filenames = list_strings(filenames)

        for dirpath, dirnames, fnames in os.walk(self.workdir):
            for fname in fnames:
                if fname in filenames:
                    filepath = os.path.join(dirpath, fname)
                    os.remove(filepath)

    def rm_tmpdatadir(self):
        """Remove the directory with the temporary files."""
        shutil.rmtree(self.tmpdata_dir)

    def rm_indatadir(self):
        """Remove the directory with the input files (indata dir)."""
        shutil.rmtree(self.indata_dir)

    def setup(self, *args, **kwargs):
        pass

    def start(self, *args, **kwargs):
        """
        Starts the calculation by performing the following steps:

            - build dirs and files
            - call the _setup method
            - execute the job file by executing/submitting the job script.
        """
        self.build(*args, **kwargs)

        self._setup(*args, **kwargs)

        # Automatic parallelization
        self.manager.autoparal(self)

        # Start the calculation in a subprocess and return.
        self._process = self.manager.launch(self)

    def start_and_wait(self, *args, **kwargs):
        """Helper method to start the task and wait."""
        self.start(*args, **kwargs)
        return self.wait()


class TaskResults(dict, MSONable):
    """
    Dictionary used to store the most important results produced by a Task.
    """
    _MANDATORY_KEYS = [
        "task_name",
        "task_returncode",
        "task_status",
        "task_events",
    ]

    EXC_KEY = "_exceptions"

    def __init__(self, *args, **kwargs):
        super(TaskResults, self).__init__(*args, **kwargs)
                                                               
        if self.EXC_KEY not in self:
            self[self.EXC_KEY] = []

    @property
    def exceptions(self):
        return self[self.EXC_KEY]

    def push_exceptions(self, *exceptions):
        for exc in exceptions:
            newstr = str(exc)
            if newstr not in self.exceptions:
                self[self.EXC_KEY] += [newstr,]

    def assert_valid(self):
        """
        Returns an empty list if results seem valid. 

        The try assert except trick allows one to get a string with info on the exception.
        We use the += operator so that sub-classes can add their own message.
        """
        # TODO Better treatment of events.
        try:
            assert (self["task_returncode"] == 0 and self["task_status"] == self.S_OK)
        except AssertionError as exc:
            self.push_exceptions(exc)

        return self.exceptions

    @property
    def to_dict(self):
        d = {k: v for k,v in self.items()}
        d["@module"] = self.__class__.__module__
        d["@class"] = self.__class__.__name__
        return d
                                                                                
    @classmethod
    def from_dict(cls, d):
        return cls({k: v for k,v in d.items() if k not in ["@module", "@class",]})

    def json_dump(self, filename):
        json_pretty_dump(self.to_dict, filename) 

    @classmethod
    def json_load(cls, filename):
        return cls.from_dict(json_load(filename))    


class ParalHintsError(Exception):
    """Base error class for `ParalHints`."""


class ParalConf(AttrDict):
    """
    This object store the parameters associated to one 
    of the possible parallel configurations reported by ABINIT.
    Essentialy it is a dictionary whose keys can be accessed as 
    attributes. It also provides default values for selected keys
    that might not be present in the ABINIT dictionary.

    Example:

        <RUN_HINTS>
        header: 
            version: 1,
            autoparal: 1,
            max_ncpus: 108,

        configurations:
        -
            tot_ncpus: 2,     # Total number of CPUs
            mpi_ncpus: 2,     # Number of MPI processes.
            omp_ncpus: 1,     # Number of OMP threads (1 if not present)
            mem_per_cpus: 10, # Estimated memory requirement per MPI processor in Gigabytes (None if not specified)
            efficiency: 0.4,  # 1.0 corresponds to an "expected" optimal efficiency (strong scaling).
            vars: {        # Dictionary with the variables that should be added to the input.
                  varname1: varvalue1,
                  varname2: varvalue2,
                  },
        -
             ...
        </RUN_HINTS>

    For paral_kgb we have:
    nproc     npkpt  npspinor    npband     npfft    bandpp    weight   
       108       1         1        12         9         2        0.25
       108       1         1       108         1         2       27.00
        96       1         1        24         4         1        1.50
        84       1         1        12         7         2        0.25
    """
    _DEFAULTS = {
        "omp_ncpus": 1,     
        "mem_per_cpu": 0.0, 
        "vars": {}       
    }

    def __init__(self, *args, **kwargs):
        super(ParalConf, self).__init__(*args, **kwargs)
        
        # Add default values if not already in self.
        for k, v in self._DEFAULTS.items():
            if k not in self:
                self[k] = v

    @property
    def speedup(self):
        return self.efficiency * self.tot_ncpus


class ParalHintsParser(object):

    Error = ParalHintsError

    def parse(self, filename):
        """
        Read the <RUN_HINTS> section from file filename
        Assumes the file contains only one section.
        """
        START_TAG = "<RUN_HINTS>"
        END_TAG = "</RUN_HINTS>"

        start, end = None, None
        with open(filename, "r") as fh:
            lines = fh.readlines()

        for i, line in enumerate(lines):
            if START_TAG in line:
                start = i
            elif END_TAG in line:
                end = i
                break

        if start is None or end is None:
            raise self.Error("%s does not contain any valid RUN_HINTS section" % filename)

        if start == end:
            # Empy section ==> One has to enable Yaml in ABINIT.
            raise self.Error("%s contains an empty RUN_HINTS section. Enable Yaml support in ABINIT" % filename)

        s = "".join(l for l in lines[start+1:end])

        import yaml
        try:
            d = yaml.load(s)
        except:
            raise self.Error("Malformatted Yaml file")

        return ParalHints(header=d["header"], confs=d["configurations"])


class ParalHints(collections.Iterable):
    """
    Iterable with the hints for the parallel execution reported by ABINIT.
    """
    Error = ParalHintsError

    def __init__(self, header, confs):
        self.header = header
        self._confs = [ParalConf(**d) for d in confs]

    def __getitem__(self, key):
        return self._confs[key]

    def __iter__(self):
        return self._confs.__iter__()

    def __len__(self):
        return self._confs.__len__()

    def __str__(self):
        return "\n".join(str(conf) for conf in self)

    def copy(self):
        return self.__class__(header=self.header.copy(), confs=self._confs[:])

    def apply_filter(self, filter):
        raise NotImplementedError("")
        new_confs = []

        for conf in self:
            if not filter(costraint):
                new_confs.append(conf)

        self._confs = new_confs

    #def select(self, command)
    #    new_confs = []
    #    for conf in self:
    #       if command(conf):
    #            new_confs.append(conf)
    #    self._confs = new_confs

    def sort_by_efficiency(self, reverse=False):
        """
        Sort the configurations in place so that conf with lowest efficieny 
        appears in the first positions.
        """
        self._confs.sort(key=lambda c: c.efficiency, reverse=reverse)

    def sort_by_speedup(self, reverse=False):
        """
        Sort the configurations in place so that conf with lowest speedup 
        appears in the first positions.
        """
        self._confs.sort(key=lambda c: c.speedup, reverse=reverse)

    #def sort_by_mem_cpu(self, reverse=False):
    #    self._confs.sort(key=lambda c: c.mem_per_cpus, reverse=reverse)

    def select_optimal_conf(self, policy):
        """Find the optimal configuration according on policy."""
        # Make a copy since we are gonna change the object in place.
        hints = self.copy()

        # First select the configurations satisfying the 
        # constraints specified by the user (if any)
        #for constraint in policy.constraints:
        #    hints.apply_filter(constraint)

        hints.sort_by_speedup()

        # If no configuration fullfills the requirements, 
        # we return the one with the highest speedup.
        if not hints:
            self.copy().sort_by_speedup()
            return hints[-1].copy()

        # Find the optimal configuration according to policy.mode.
        #mode = policy.mode
        #if mode in ["default", "aggressive"]:
        #    hints.sort_by_spedup(reverse=True)
        #elif mode == "conservative":
        #    hints.sort_by_efficiency(reverse=True)
        #else:
        #    raise ValueError("Wrong value for mode: %s" % str(mode))

        # Return a copy of the configuration.
        optimal = hints[-1].copy()
        print("Will relaunch the job with optimized parameters:\n %s" % str(optimal))
        return optimal


class TaskPolicy(AttrDict):
    """
    This object stores the parameters used by the `TaskManager` to 
    create the submission script and/or to modify the ABINIT variables 
    governing the parallel execution. A `TaskPolicy` object contains 
    a set of variables that specify the launcher, and options
    and constraints used to select the optimal configuration for the parallel run 
    """
    # Default values.
    _DEFAULTS = dict(
        autoparal=0,        # ABINIT autoparal option. Set it to 0 to disable this feature.
        mode="default",     # ["default", "aggressive", "conservative"]
        max_ncpus=None,     # Max number of CPUs that can be used (users should specify this value when autoparal > 0)
        use_fw=False,       # True if we are using fireworks.
        constraints = [],   # List of constraints (mongodb syntax).
        #automated=False,
    )

    #def __init__(self, autoparal=0, mode="default", max_ncpus=None, use_fw=False, constraints=()): #, automated=False, 
    #    """
    #    Args:
    #        autoparal: 
    #            Value of autoparal input variable. None to disable the autoparal feature.
    #        mode:
    #        max_ncpus:
    #        use_fw: 
    #        automated:
    #        constraints: 
    #            List of constraints (Mongodb syntax)
    #    """
    #    self.autoparal = autoparal
    #    self.mode = mode 
    #    self.max_ncpus = max_ncpus
    #    self.use_fw = use_fw 
    #    self.automated = automated
    #    self.constraints = [] if constraints is None else constraints

    def __init__(self, *args, **kwargs):
        super(TaskPolicy, self).__init__(*args, **kwargs)

        err_msg = ""
        for k, v in self.items():
            if k not in self._DEFAULTS:
                err_msg += "Unknow key %s\n" % k

        if err_msg:
            raise ValueError(err_msg)

        for k, v in self._DEFAULTS.items():
            if k not in self:
                self[k] = v

        if self.autoparal and self.max_ncpus is None:
            raise ValueError("When autoparal is not zero, max_ncpus must be specified.")

    @classmethod
    def default_policy(cls):
        d = cls._DEFAULTS.copy()
        return cls(**d)


class TaskManager(object):
    """
    A `TaskManager` is responsible for the generation of the job script and the submission 
    of the task, as well as of the specification of the parameters passed to the Resource Manager
    (e.g. Slurm, PBS ...) and/or the run-time specification of the ABINIT variables governing the 
    parallel execution. A `TaskManager` delegates the generation of the submission
    script and the submission of the task to the `QueueAdapter`. 
    A `TaskManager` has a `TaskPolicy` that governs the specification of the 
    parameters for the parallel executions.
    """
    def __init__(self, qtype, qparams=None, setup=None, modules=None, shell_env=None, omp_env=None, 
                 pre_run=None, post_run=None, mpi_runner=None, policy=None):

        qad_class = qadapter_class(qtype)

        self.qadapter = qad_class(qparams=qparams, setup=setup, modules=modules, shell_env=shell_env, omp_env=omp_env, 
                                  pre_run=pre_run, post_run=post_run, mpi_runner=mpi_runner)

        self.policy = TaskPolicy(**policy) if policy is not None else TaskPolicy.default_policy()
        #print(type(self.policy), self.policy)

    def __str__(self):
        """String representation."""
        lines = []
        app = lines.append
        app("tot_ncpus %d, mpi_ncpus %d, omp_ncpus %s" % (self.tot_ncpus, self.mpi_ncpus, self.omp_ncpus))
        app("MPI_RUNNER %s" % str(self.qadapter.mpi_runner))
        app("policy: %s" % str(self.policy))

        return "\n".join(lines)

    @classmethod
    def from_dict(cls, d):
        return cls(**d)

    @classmethod
    def from_file(cls, filename):
        """Read the configuration parameters from a Yaml file."""
        import yaml
        with open(filename, "r") as fh:
            d = yaml.load(fh)

        return cls.from_dict(d)

    #@classmethod
    #def from_user_config(cls):
    #    Try in the current directory.
    #    fname = "taskmanager.yaml"
    #    path = fname
    #    if os.path.exists(path):
    #        return cls.from_file(path)

    #    Try in the configuration directory.
    #    path = os.path.join(home, path)
    #    if os.path.exists(home, path):
    #        return cls.from_file(path)
    #
    #    raise RuntimeError("Cannot locate %s neither in current directory nor in home directory" % fname)

    @classmethod 
    def sequential(cls):
        """
        Build a simple `TaskManager` that submits jobs with a simple shell script.
        Assume the shell environment has been initialized.
        """
        return cls(qtype="shell")

    @classmethod 
    def simple_mpi(cls, mpi_runner="mpirun", mpi_ncpus=1, policy=None):
        """
        Build a `TaskManager` that submits jobs with a simple shell script and mpirun.
        Assume the shell environment is already properly initialized.
        """
        return cls(qtype="shell", qparams=dict(MPI_NCPUS=mpi_ncpus), mpi_runner=mpi_runner, policy=policy)

    @property
    def tot_ncpus(self):
        """Total number of CPUs used to run the task."""
        return self.qadapter.tot_ncpus

    @property
    def mpi_ncpus(self):
        """Number of CPUs used for MPI."""
        return self.qadapter.mpi_ncpus

    @property
    def omp_ncpus(self):
        """Number of CPUs used for OpenMP."""
        return self.qadapter.omp_ncpus

    def to_shell_manager(self, mpi_ncpus=1, policy=None):
        """
        Returns a new `TaskManager` with the same parameters as self but replace the `QueueAdapter` 
        with a `ShellAdapter` with mpi_ncpus so that we can submit the job without passing through the queue.
        """
        cls = self.__class__
        qad = self.qadapter

        policy = self.policy if policy is None else policy

        new = cls("shell", qparams={"MPI_NCPUS": mpi_ncpus}, setup=qad.setup, modules=qad.modules, 
                  shell_env=qad.shell_env, omp_env=qad.omp_env, pre_run=qad.pre_run, 
                  post_run=qad.post_run, mpi_runner=qad.mpi_runner, policy=policy)

        new.set_mpi_ncpus(mpi_ncpus)
        return new

    def copy(self):
        """Shallow copy of self."""
        return copy.copy(self)

    def deepcopy(self):
        """Deep copy of self."""
        return copy.deepcopy(self)

    def set_mpi_ncpus(self, mpi_ncpus):
        """Set the number of MPI nodes to use."""
        self.qadapter.set_mpi_ncpus(mpi_ncpus)

    def set_omp_ncpus(self, omp_ncpus):
        """Set the number of OpenMp threads to use."""
        self.qadapter.set_omp_ncpus(omp_ncpus)

    #def set_mem_per_cpu(self, mem_per_cpu):
    #    self.qadapter.set_mem_per_cpu(mem_per_cpu)

    def autoparal(self, task):
        """
        Find optimal parameters for the execution of the task 
        using the options specified in self.policy.
     
        This method can change the ABINIT input variables and/or the 
        parameters passed to the `TaskManager` e.g. the number of CPUs for MPI and OpenMp.

        Returns:
           `ParalHints` object with the configuration reported by autoparal and the optimal configuration.
           (None, None) if some problem occurred.
        """
        print(type(self.policy), self.policy)
        policy = self.policy

        # FIXME: Refactor policy object, avoid AttrDict
        if policy["autoparal"] == 0 or policy.max_ncpus is None: # nothing to do
            return None, None

        assert policy.autoparal == 1
        # 1) Run ABINIT in sequential to get the possible configurations with max_ncpus

        # Set the variables for automatic parallelization
        autoparal_vars = dict(
            autoparal=policy.autoparal,
            max_ncpus=policy.max_ncpus,
        )

        task.strategy.add_extra_abivars(autoparal_vars)
        task.build()

        # Build a simple manager that runs jobs in a shell subprocess.
        seq_manager = self.to_shell_manager(mpi_ncpus=1)

        process = seq_manager.launch(task)
        process.wait()
        # return code is always != 0 
        #return process.returncode

        # Reset the status, remove garbage files ...
        task.set_status(task.S_READY)

        # Remove the variables added for the automatic parallelization
        task.strategy.remove_extra_abivars(autoparal_vars.keys())

        # Remove the output file since Abinit likes to create new files 
        # with extension .outA, .outB if the file already exists.
        try:
            os.remove(task.output_file.path)
        except:
            pass

        # 2) Parse the autoparal configurations
        parser = ParalHintsParser()

        try:
            confs = parser.parse(task.log_file.path)
        except parser.Error:
            return None, None

        # 3) Select the optimal configuration according to policy
        optimal = confs.select_optimal_conf(policy)

        # 4) Change the input file and/or the submission script
        task.strategy.add_extra_abivars(optimal.vars)
                                                                  
        # Change the number of MPI nodes.
        self.set_mpi_ncpus(optimal.mpi_ncpus)

        # Change the number of OpenMP threads.
        #if optimal.omp_ncpus > 1:
        #    self.set_omp_ncpus(optimal.omp_ncpus)
        #else:
        #    self.qadapter.disable_omp()

        # Change the memory per node.
        #if optimal.mem_per_cpu is not None
        #    self.qadapter.set_mem_per_cpu(optimal.mem_per_cpu)

        return confs, optimal

    def write_jobfile(self, task):
        """
        Write the submission script.

        Args:
            task:
                `AbinitTask` object.

        Returns:
            The path of the script file.
        """
        script = self.qadapter.get_script_str(
            job_name=task.name, 
            launch_dir=task.workdir, 
            executable=task.executable,
            qout_path=task.qout_file.path,
            qerr_path=task.qerr_file.path,
            stdin=task.files_file.path, 
            stdout=task.log_file.path,
            stderr=task.stderr_file.path,
            )

        # Write the script.
        script_file = task.job_file.path
        with open(script_file, "w") as fh:
            fh.write(script)

        return script_file

    def launch(self, task):
        """
        Build the input files and submit the task via the `Qadapter` 

        Args:
            task:
                `TaskObject`
        
        Returns:
            Process object.
        """
        task.build()

        script_file = self.write_jobfile(task)

        # Submit the script.
        task.set_status(task.S_SUB)

        process, queue_id = self.qadapter.submit_to_queue(script_file)

        task.set_queue_id(queue_id)
        #task.set_status(task.S_RUN)

        return process
