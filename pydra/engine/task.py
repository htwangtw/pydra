# -*- coding: utf-8 -*-
"""task.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1RRV1gHbGJs49qQB1q1d5tQEycVRtuhw6

## Notes:

### Environment specs
1. neurodocker json
2. singularity file+hash
3. docker hash
4. conda env
5. niceman config
6. environment variables

### Monitors/Audit
1. internal monitor
2. external monitor
3. callbacks

### Resuming
1. internal tracking
2. external tracking (DMTCP)

### Provenance
1. Local fragments
2. Remote server

### Isolation
1. Working directory
2. File (copy to local on write)
3. read only file system
"""


import abc
import cloudpickle as cp
import dataclasses as dc
import json
import os
from pathlib import Path
from tempfile import mkdtemp
import typing as ty
import inspect

from ..utils.messenger import (send_message, make_message, gen_uuid, now,
                               AuditFlag)
from .specs import (BaseSpec, Runtime, Result, RuntimeSpec, File)


develop = True


def ensure_list(obj):
    if obj is None:
        return []
    if isinstance(obj, list):
        return obj
    return [obj]


def print_help(obj):
    help = ['Help for {}'.format(obj.__class__.__name__)]
    if dc.fields(obj.input_spec):
        help += ['Input Parameters:']
    for f in dc.fields(obj.input_spec):
        default = ''
        if f.default is not dc.MISSING and not f.name.startswith('_'):
            default = ' (default: {})'.format(f.default)
        help += ['\t{}: {}{}'.format(f.name, f.type.__name__, default)]
    if dc.fields(obj.input_spec):
        help += ['Output Parameters:']
    for f in dc.fields(obj.output_spec):
        help += ['\t{}: {}'.format(f.name, f.type.__name__)]
    print('\n'.join(help))
    return help


def load_result(checksum, cache_locations):
    if not cache_locations:
        return None
    for location in cache_locations:
        if (location / checksum).exists():
            result_file = (location / checksum / '_result.pklz')
            if result_file.exists():
                return cp.loads(result_file.read_bytes())
            else:
                return None
    return None


def save_result(result_path: Path, result):
    with (result_path / '_result.pklz').open('wb') as fp:
        return cp.dump(dc.asdict(result), fp)


def task_hash(task_obj):
    """
    input hash, output hash, environment hash
    
    :param task_obj: 
    :return: 
    """
    return NotImplementedError


def gather_runtime_info(fname):
    runtime = Runtime(rss_peak_gb=None, vms_peak_gb=None,
                      cpu_peak_percent=None)

    # Read .prof file in and set runtime values
    with open(fname, 'rt') as fp:
        data = [[float(el) for el in val.strip().split(',')]
                for val in fp.readlines()]
        if data:
            runtime.rss_peak_gb = max([val[2] for val in data]) / 1024
            runtime.vms_peak_gb = max([val[3] for val in data]) / 1024
            runtime.cpu_peak_percent = max([val[1] for val in data])
        '''
        runtime.prof_dict = {
            'time': vals[:, 0].tolist(),
            'cpus': vals[:, 1].tolist(),
            'rss_GiB': (vals[:, 2] / 1024).tolist(),
            'vms_GiB': (vals[:, 3] / 1024).tolist(),
        }
        '''
    return runtime


class BaseTask:
    """This is a base class for Task objects.
    """

    _api_version: str = "0.0.1"  # Should generally not be touched by subclasses
    _task_version: ty.Optional[str] = None  # Task writers encouraged to define and increment when implementation changes sufficiently
    _version: str  # Version of tool being wrapped

    input_spec = BaseSpec  # See BaseSpec
    output_spec = BaseSpec  # See BaseSpec
    audit_flags: AuditFlag = AuditFlag.NONE  # What to audit. See audit flags for details

    _can_resume = False  # Does the task allow resuming from previous state
    _redirect_x = False  # Whether an X session should be created/directed

    _runtime_requirements = RuntimeSpec()
    _runtime_hints = None

    _input_sets = None  # Dictionaries of predefined input settings
    _cache_dir = None  # Working directory in which to operate
    _references = None  # List of references for a task

    def __init__(self, inputs: ty.Union[ty.Text, File, ty.Dict, None]=None,
                 audit_flags: AuditFlag=AuditFlag.NONE,
                 messengers=None, messenger_args=None):
        """Initialize task with given args."""
        super().__init__()
        if not self.input_spec:
            raise Exception(
                'No input_spec in class: %s' % self.__class__.__name__)
        self.inputs = self.input_spec(
            **{f.name:None
               for f in dc.fields(self.input_spec)
               if f.default is dc.MISSING})
        self.audit_flags = audit_flags
        self.messengers = ensure_list(messengers)
        self.messenger_args = messenger_args
        if self._input_sets is None:
            self._input_sets = {}
        if inputs:
            if isinstance(inputs, dict):
                self.inputs = dc.replace(self.inputs, **inputs)
            elif Path(inputs).is_file():
                inputs = json.loads(Path(inputs).read_text())
            elif isinstance(inputs, str):
                if self._input_sets is None or inputs not in self._input_sets:
                    raise ValueError("Unknown input set {!r}".format(inputs))
                inputs = self._input_sets[inputs]

    def audit(self, message, flags=None):
        if develop:
            with open(Path(os.path.dirname(__file__))
                      / '..' / 'schema/context.jsonld', 'rt') as fp:
                context = json.load(fp)
        else:
            context = {"@context": 'https://raw.githubusercontent.com/satra/pydra/enh/task/pydra/schema/context.jsonld'}
        if self.audit_flags & flags:
            if self.messenger_args:
                send_message(make_message(message, context=context),
                             messengers=self.messengers,
                             **self.messenger_args)
            else:              
                send_message(make_message(message, context=context),
                             messengers=self.messengers)

    @property
    def can_resume(self):
        """Task can reuse partial results after interruption
        """
        return self._can_resume

    def help(self, returnhelp=False):
        """ Prints class help
        """
        help_obj = print_help(self)
        if returnhelp:
            return help_obj

    @property
    def output_names(self):
        return [f.name for f in dc.fields(self.output_spec)]

    @property
    def version(self):
        return self._version
    
    def save_set(self, name, inputs, force=False):
        if name in self._input_sets and not force:
            raise KeyError('Key {} already saved. Use force=True to override.')
        self._input_sets[name] = inputs

    @property
    def checksum(self):
        return '_'.join((self.__class__.__name__, self.inputs.hash))

    @abc.abstractmethod
    def _run_task(self):
        pass

    def result(self, cache_locations=None):
        result = load_result(self.checksum,
                             ensure_list(cache_locations) + 
                             ensure_list(self._cache_dir))
        if result is not None:
            if 'output' in result:
                output = self.output_spec(**result['output'])
            return Result(output=output)
        return None

    @property
    def cache_dir(self):
        return self._cache_dir
    
    @cache_dir.setter
    def cache_dir(self, location):
        self._cache_dir = Path(location)

    def audit_check(self, flag):
        return self.audit_flags & flag

    def __call__(self, cache_locations=None, **kwargs):
        return self.run(cache_locations=cache_locations, **kwargs)

    def run(self, cache_locations=None, **kwargs):
        self.inputs = dc.replace(self.inputs, **kwargs)
        checksum = self.checksum
        
        # Eagerly retrieve cached
        result = load_result(checksum,
                             ensure_list(cache_locations) + 
                             ensure_list(self._cache_dir))
        if result is not None:
            return result
        # start recording provenance, but don't send till directory is created
        # in case message directory is inside task output directory
        if self.audit_check(AuditFlag.PROV):
            aid = "uid:{}".format(gen_uuid())
            start_message = {"@id": aid, "@type": "task", "startedAtTime": now()}
        # Not cached
        if self._cache_dir is None:
            self.cache_dir = mkdtemp()
        cwd = os.getcwd()
        odir = self.cache_dir / checksum
        odir.mkdir(parents=True, exist_ok=True if self.can_resume else False)
        os.chdir(odir)
        if self.audit_check(AuditFlag.PROV):
            self.audit(start_message, AuditFlag.PROV)
            # audit inputs
        #check_runtime(self._runtime_requirements)
        #isolate inputs if files
        #cwd = os.getcwd()
        if self.audit_check(AuditFlag.RESOURCE):
            from ..utils.profiler import ResourceMonitor
            resource_monitor = ResourceMonitor(os.getpid(), freq=0.01,
                                               logdir=odir)
        result = Result(output=None, runtime=None)
        try:
            if self.audit_check(AuditFlag.RESOURCE):
                resource_monitor.start()
                if self.audit_check(AuditFlag.PROV):
                    mid = "uid:{}".format(gen_uuid())
                    self.audit({"@id": mid, "@type": "monitor",
                                "startedAtTime": now()}, AuditFlag.PROV)
            self._run_task()
            result.output = self._collect_outputs()
        except Exception as e:
            #record_error(self, e)
            raise
        finally:
            if self.audit_check(AuditFlag.RESOURCE):
                resource_monitor.stop()
                result.runtime = gather_runtime_info(resource_monitor.fname)
                if self.audit_check(AuditFlag.PROV):
                    self.audit({"@id": mid, "endedAtTime": now()}, AuditFlag.PROV)
                    # audit resources/runtime information
                    eid = "uid:{}".format(gen_uuid())
                    entity = dc.asdict(result.runtime)
                    entity.update(**{"@id": eid, "@type": "runtime",
                                     "prov:wasGeneratedBy": aid})
                    self.audit(entity, AuditFlag.PROV)
                    self.audit({"@type": "prov:Generation",
                                "entity_generated": eid,
                                "hadActivity": mid}, AuditFlag.PROV)
            save_result(odir, result)
            os.chdir(cwd)
            if self.audit_check(AuditFlag.PROV):
                # audit outputs
                self.audit({"@id": aid, "endedAtTime": now()}, AuditFlag.PROV)
        return result

    # TODO: Decide if the following two functions should be separated
    @abc.abstractmethod
    def _list_outputs(self):
        pass

    def _collect_outputs(self):
        run_output = self._list_outputs()
        output = self.output_spec(**{f.name: None for f in
                                            dc.fields(self.output_spec)})
        return dc.replace(output, **dict(zip(self.output_names,
                                             run_output)))


class FunctionTask(BaseTask):

    def __init__(self, func: ty.Callable, output_spec: ty.Optional[BaseSpec]=None,
                 audit_flags: AuditFlag=AuditFlag.NONE,
                 messengers=None, messenger_args=None, **kwargs):
        self.input_spec = dc.make_dataclass(
            'Inputs', 
            [(val.name, val.annotation, val.default)
                  if val.default is not inspect.Signature.empty
                  else (val.name, val.annotation)
             for val in inspect.signature(func).parameters.values() 
             ] + [('_func', str, cp.dumps(func))],
            bases=(BaseSpec,))
        super(FunctionTask, self).__init__(inputs=kwargs, 
                                           audit_flags=audit_flags,
                                           messengers=messengers,
                                           messenger_args=messenger_args)
        if output_spec is None:
            if 'return' not in func.__annotations__:
                output_spec = dc.make_dataclass('Output', 
                                                [('out', ty.Any)],
                                                bases=(BaseSpec,))            
            else:
                return_info = func.__annotations__['return']
                output_spec = dc.make_dataclass(return_info.__name__, 
                                                return_info.__annotations__.items(),
                                                bases=(BaseSpec,))
        elif 'return' in func.__annotations__:
            raise NotImplementedError('Branch not implemented')
        self.output_spec = output_spec

    def _run_task(self):
        inputs = dc.asdict(self.inputs)
        del inputs['_func']
        self.output_ = None
        output = cp.loads(self.inputs._func)(**inputs)
        if not isinstance(output, tuple):
            output = (output,)
        self.output_ = output

    def _list_outputs(self):
        return self.output_


def to_task(func_to_decorate):
    def create_func(**original_kwargs):
        function_task = FunctionTask(func=func_to_decorate,
                                     **original_kwargs)
        return function_task
    return create_func


class ShellTask(BaseTask):
    pass


class BashTask(ShellTask):
    pass


class MATLABTask(ShellTask):
    pass
