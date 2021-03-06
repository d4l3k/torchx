#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# TODO(aivanou): Update documentation

import abc
import copy
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from string import Template
from typing import (
    Any,
    Callable,
    Dict,
    Generic,
    Iterable,
    List,
    Optional,
    Tuple,
    Type,
    TypeVar,
    Union,
)


SchedulerBackend = str


# ========================================
# ==== Distributed Application API =======
# ========================================
@dataclass
class Resource:
    """
    Represents resource requirements for a ``Container``.

    Args:
            cpu: number of cpu cores (note: not hyper threads)
            gpu: number of gpus
            memMB: MB of ram
            capabilities: additional hardware specs (interpreted by scheduler)
    """

    cpu: int
    gpu: int
    memMB: int
    capabilities: Dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def copy(original: "Resource", **capabilities: Any) -> "Resource":
        """
        Copies a resource and applies new capabilities. If the same capabilities
        are present in the original resource and as parameter, the one from parameter
        will be used.
        """
        res_capabilities = dict(original.capabilities)
        res_capabilities.update(capabilities)
        return Resource(
            cpu=original.cpu,
            gpu=original.gpu,
            memMB=original.memMB,
            capabilities=res_capabilities,
        )


# sentinel value used for cases when resource does not matter (e.g. ignored)
NULL_RESOURCE: Resource = Resource(cpu=-1, gpu=-1, memMB=-1)

# used as "*" scheduler backend
ALL: SchedulerBackend = "all"


@dataclass
class Container:
    """
    Represents the specifications of the container that instances of ``Roles``
    run on. Maps to the container abstraction that the underlying scheduler
    supports. This could be an actual container (e.g. Docker) or a physical
    instance depending on the scheduler.

    An ``image`` is a software bundle that is installed on a ``Container``.
    The container on the scheduler dictates what an image actually is.
    An image could be as simple as a tar-ball or map to a docker image.
    The scheduler typically knows how to "pull" the image given an
    image name (str), which could be a simple name (e.g. docker image) or a url
    (e.g. s3://path/my_image.tar).

    A ``Resource`` can be bound to a specific scheduler backend or ``SchedulerBackend.ALL`` (default)
    to specify that the same ``Resource`` is to be used for all schedulers.

    An optional ``base_image`` can be specified if the scheduler supports a
    concept of base images. For schedulers that run Docker containers the
    base image is not useful since the application image itself can be
    built from a base image (using the ``FROM base/image:latest`` construct in
    the Dockerfile). However the base image is useful for schedulers that
    work with simple image artifacts (e.g. ``*.tar.gz``) that do not have a built-in
    concept of base images. For these schedulers, specifying a base image that
    includes dependencies while the main image is the actual application code
    makes it possible to make changes to the application code without incurring
    the cost of re-building the uber artifact.

    Usage:

    ::

     # define resource for all schedulers
     my_container = Container(image="pytorch/torch:1")
                       .require(Resource(cpu=1, gpu=1, memMB=500))
                       .ports(tcp_store=8080, tensorboard=8081)

     # define resource for a specific scheduler
     my_container = Container(image="pytorch/torch:1")
                       .require(Resource(cpu=1, gpu=1, memMB=500), "custom_scheduler")
                       .ports(tcp_store=8080, tensorboard=8081)

    # for schedulers that support base_images
    my_container = Container(image="my/trainer:1", base_image="common/ml-tools:latest")
                      .require(...)
    """

    image: str
    base_image: Optional[str] = None
    resources: Resource = NULL_RESOURCE
    port_map: Dict[str, int] = field(default_factory=dict)

    def require(self, resources: Resource) -> "Container":
        """
        Sets resource requirements on the container.
        """
        self.resources = resources
        return self

    def ports(self, **kwargs: int) -> "Container":
        """
        Adds a port mapping for the container
        """
        self.port_map.update({**kwargs})
        return self


# sentinel value used to represent missing string attributes, such as image or entrypoint
MISSING: str = "<MISSING>"

# sentinel value used to represent "unset" optional string attributes
NONE: str = "<NONE>"

# sentinel value used as the "zero" element in the container group
NULL_CONTAINER: Container = Container(image=MISSING)


class macros:
    """
    Defines macros that can be used with ``Role.entrypoint`` and ``Role.args``.
    The macros will be substituted at runtime to their actual values.

    Available macros:

    1. ``img_root`` - root directory of the pulled conatiner.image
    2. ``base_img_root`` - root directory of the pulled container.base_image
                           (resolves to "<NONE>" if no base_image set)
    3. ``app_id`` - application id as assigned by the scheduler
    4. ``replica_id`` - unique id for each instance of a replica of a Role,
                        for instance a role with 3 replicas could have the 0, 1, 2
                        as replica ids. Note that when the container fails and is
                        replaced, the new container will have the same ``replica_id``
                        as the one it is replacing. For instance if node 1 failed and
                        was replaced by the scheduler the replacing node will also
                        have ``replica_id=1``.

    Example:

    ::

     # runs: hello_world.py --app_id ${app_id}
     trainer = Role(name="trainer").runs("hello_world.py", "--app_id", macros.app_id)
     app = Application("train_app").of(trainer)
     app_handle = session.run(app, scheduler="local", cfg=RunConfig())

    """

    img_root = "${img_root}"
    base_img_root = "${base_img_root}"
    app_id = "${app_id}"
    replica_id = "${replica_id}"

    @dataclass
    class Values:
        img_root: str
        app_id: str
        replica_id: str
        base_img_root: str = NONE

        def apply(self, role: "Role") -> "Role":
            """
            apply applies the values to a copy the specified role and returns it.
            """
            role = copy.deepcopy(role)
            role.args = [self.substitute(arg) for arg in role.args]
            role.env = {key: self.substitute(arg) for key, arg in role.env.items()}
            return role

        def substitute(self, arg: str) -> str:
            """
            substitute applies the values to the template arg.
            """
            return Template(arg).safe_substitute(**asdict(self))


class RetryPolicy(str, Enum):
    """
    Defines the retry policy for the ``Roles`` in the ``Application``.
    The policy defines the behavior when the role replica encounters a failure:

    1. unsuccessful (non zero) exit code
    2. hardware/host crashes
    3. preemption
    4. eviction

    .. note:: Not all retry policies are supported by all schedulers.
              However all schedulers must support ``RetryPolicy.APPLICATION``.
              Please refer to the scheduler's documentation for more information
              on the retry policies they support and behavior caveats (if any).

    1. REPLICA: Replaces the replica instance. Surviving replicas are untouched.
                Use with ``ElasticRole`` to have torchelastic coordinate restarts
                and membership changes. Otherwise, it is up to the application to
                deal with failed replica departures and replacement replica admittance.
    2. APPLICATION: Restarts the entire application.

    """

    REPLICA = "REPLICA"
    APPLICATION = "APPLICATION"


@dataclass
class Role:
    """
    A set of nodes that perform a specific duty within the ``Application``.
    Examples:

    1. Distributed data parallel app - made up of a single role (trainer).

    2. App with parameter server - made up of multiple roles (trainer, ps).

    Usage:

    ::

     trainer = Role(name="trainer")
                 .runs("my_trainer.py", "--arg", "foo", ENV_VAR="FOOBAR")
                 .on(container)
                 .replicas(4)

    Args:
            name: name of the role
            entrypoint: command (within the container) to invoke the role
            args: commandline arguments to the entrypoint cmd
            env: environment variable mappings
            container: container to run in
            replicas: number of container replicas to run
            max_retries: max number of retries before giving up
            retry_policy: retry behavior upon replica failures
            deployment_preference: hint to the scheduler on how to best
                                   deploy and manage replicas of this role

    """

    name: str
    entrypoint: str = MISSING
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    container: Container = NULL_CONTAINER
    num_replicas: int = 1
    max_retries: int = 0
    retry_policy: RetryPolicy = RetryPolicy.APPLICATION

    def runs(self, entrypoint: str, *args: str, **kwargs: str) -> "Role":
        self.entrypoint = entrypoint
        self.args += [*args]
        self.env.update({**kwargs})
        return self

    def on(self, container: Container) -> "Role":
        self.container = container
        return self

    def replicas(self, replicas: int) -> "Role":
        self.num_replicas = replicas
        return self

    def with_retry_policy(self, retry_policy: RetryPolicy, max_retries: int) -> "Role":
        self.retry_policy = retry_policy
        self.max_retries = max_retries
        return self

    def pre_proc(
        self,
        scheduler: SchedulerBackend,
        # pyre-fixme[24]: AppDryRunInfo was designed to work with Any request object
        dryrun_info: "AppDryRunInfo",
        # pyre-fixme[24]: AppDryRunInfo was designed to work with Any request object
    ) -> "AppDryRunInfo":
        """
        Modifies the scheduler request based on the role specific configuration.
        The method is invoked for each role during scheduler ``submit_dryrun``.
        If there are multiple roles, the method is invoked for each role in
        order that is defined by the ``Application.roles`` list.
        """
        return dryrun_info


class ElasticRole(Role):
    """
    A ``Role`` for which the user provided ``entrypoint`` is executed with the
    torchelastic agent (in the container). Note that the torchelastic agent
    invokes multiple copies of ``entrypoint``.

    For more information about torchelastic see
    `torchelastic quickstart docs <http://pytorch.org/elastic/0.2.0/quickstart.html>`__.

    .. important:: It is the responsibility of the user to ensure that the
                   container's image includes torchelastic. Since Torchx has no
                   control over the build process of the image, it cannot
                   automatically include torchelastic in the container's image.

    The following example launches 2 ``replicas`` (nodes) of an elastic ``my_train_script.py``
    that is allowed to scale between 2 to 4 nodes. Each node runs 8 workers which are allowed
    to fail and restart a maximum of 3 times.

    .. warning:: ``replicas`` MUST BE an integer between (inclusive) ``nnodes``. That is,
                   ``ElasticRole("trainer", nnodes="2:4").replicas(5)`` is invalid and will
                   result in undefined behavior.

    ::

     elastic_trainer = ElasticRole("trainer", nproc_per_node=8, nnodes="2:4", max_restarts=3)
                        .runs("my_train_script.py", "--script_arg", "foo", "--another_arg", "bar")
                        .on(container)
                        .replicas(2)
     # effectively runs:
     #    python -m torchelastic.distributed.launch
     #        --nproc_per_node 8
     #        --nnodes 2:4
     #        --max_restarts 3
     #        my_train_script.py --script_arg foo --another_arg bar

    """

    def __init__(self, name: str, **launch_kwargs: Any) -> None:
        super().__init__(name=name)
        self.entrypoint = "python"
        self.args: List[str] = []
        self.args += ["-m", "torchelastic.distributed.launch"]
        self.torchelastic_launch_args: List[str] = []

        launch_kwargs.setdefault("rdzv_backend", "etcd")
        launch_kwargs.setdefault("rdzv_id", macros.app_id)
        launch_kwargs.setdefault("role", name)

        for (arg, val) in launch_kwargs.items():
            if isinstance(val, bool):
                # treat boolean kwarg as a flag
                if val:
                    self.torchelastic_launch_args += [f"--{arg}"]
            else:
                self.torchelastic_launch_args += [f"--{arg}", str(val)]

    def runs(self, entrypoint: str, *args: str, **kwargs: str) -> "ElasticRole":
        if not os.path.isabs(entrypoint) and not entrypoint.startswith(macros.img_root):
            # make entrypoint relative to {img_root} ONLY if it is not an absolute path
            entrypoint = os.path.join(macros.img_root, entrypoint)

        self.args += self.torchelastic_launch_args
        self.args += [entrypoint, *args]
        self.env.update({**kwargs})
        return self


@dataclass
class Application:
    """
    Represents a distributed application made up of multiple ``Roles``
    and metadata. Contains the necessary information for the driver
    to submit this app to the scheduler.

    Args:
        name: Name of application
        roles: List of roles
        metadata: Application specific configuration, in comparison
            ``RunConfig`` is runtime specific configuration.
    """

    name: str
    roles: List[Role] = field(default_factory=list)
    metadata: Dict[str, str] = field(default_factory=dict)

    def of(self, *roles: Role) -> "Application":
        self.roles += [*roles]
        return self

    def add_metadata(self, key: str, value: str) -> "Application":
        """
        Adds metadata to the application.
        .. note:: If the key already exists, this method overwrites the metadata value.
        """
        self.metadata[key] = value
        return self

    def get_metadata(self, key: str) -> Optional[str]:
        return self.metadata.get(key)


class AppState(int, Enum):
    """
    State of the application. An application starts from an initial
    ``UNSUBMITTED`` state and moves through ``SUBMITTED``, ``PENDING``,
    ``RUNNING`` states finally reaching a terminal state:
    ``SUCCEEDED``,``FAILED``, ``CANCELLED``.

    If the scheduler supports preemption, the app moves from a ``RUNNING``
    state to ``PENDING`` upon preemption.

    If the user stops the application, then the application state moves
    to ``STOPPED``, then to ``CANCELLED`` when the job is actually cancelled
    by the scheduler.

    1. UNSUBMITTED - app has not been submitted to the scheduler yet
    2. SUBMITTED - app has been successfully submitted to the scheduler
    3. PENDING - app has been submitted to the scheduler pending allocation
    4. RUNNING - app is running
    5. SUCCEEDED - app has successfully completed
    6. FAILED - app has unsuccessfully completed
    7. CANCELLED - app was cancelled before completing
    """

    UNSUBMITTED = 0
    SUBMITTED = 1
    PENDING = 2
    RUNNING = 3
    SUCCEEDED = 4
    FAILED = 5
    CANCELLED = 6

    def __str__(self) -> str:
        return self.name


_TERMINAL_STATES: List[AppState] = [
    AppState.SUCCEEDED,
    AppState.FAILED,
    AppState.CANCELLED,
]


def is_terminal(state: AppState) -> bool:
    return state in _TERMINAL_STATES


# =======================
# ==== Status API =======
# =======================

# replica and app share the same states, simply alias it for now
ReplicaState = AppState


@dataclass
class ReplicaStatus:
    """
    The status of the replica during the job execution.

    Args:
        id: The node rank, note: this is not a worker rank.
        state: The current state of the node.
        role: The role name
        hostname: The hostname where the replica is running
        structured_error_msg: Error message if any, None if job succeeded.
    """

    id: int
    state: ReplicaState
    role: str
    hostname: str
    structured_error_msg: str = NONE


@dataclass
class RoleStatus:
    """
    The status of the role during the job execution.

    Args:
        role: Role name
        replicas: List of replica statuses
    """

    role: str
    replicas: List[ReplicaStatus]


@dataclass
class AppStatus:
    """
    The runtime status of the ``Application``. The scheduler can
    return an arbitrary text message (msg field).
    If any error occurs, scheduler can populate ``structured_error_msg``
    with json response.

    ``replicas`` represent the statuses of the replicas in the job. If the job
    runs with multiple retries, the parameter will contain the statuses of the
    most recent retry. Note: if the previous retries failed, but the most recent
    retry succeeded or in progress, ``replicas`` will not contain ocurred errors.
    """

    state: AppState
    num_restarts: int = 0
    msg: str = ""
    structured_error_msg: str = NONE
    ui_url: Optional[str] = None
    roles: List[RoleStatus] = field(default_factory=list)

    def is_terminal(self) -> bool:
        return is_terminal(self.state)

    def __repr__(self) -> str:
        app_status_dict = asdict(self)
        structured_error_msg = app_status_dict.pop("structured_error_msg")
        if structured_error_msg != NONE:
            structured_error_msg_parsed = json.loads(structured_error_msg)
        else:
            structured_error_msg_parsed = NONE
        app_status_dict["structured_error_msg"] = structured_error_msg_parsed
        return json.dumps(app_status_dict, indent=2)


# valid ``RunConfig`` values; only support primitives (str, int, float, bool, List[str])
# TODO(wilsonhong): python 3.9+ supports list[T] in typing, which can be used directly
# in isinstance(). Should replace with that.
# see: https://docs.python.org/3/library/stdtypes.html#generic-alias-type
ConfigValue = Union[str, int, float, bool, List[str], None]


# =======================
# ==== Run Config =======
# =======================
@dataclass(frozen=True)
class RunConfig:
    """
    Additional run configs for the app. These are typically
    scheduler runtime configs/arguments that do not bind
    to ``Application`` nor the ``Scheduler``. For example
    a particular cluster (within the scheduler) the application
    should be submitted to. Since the same app can be launched
    into multiple types of clusters (devo, prod) the
    cluster id config does not bind to the app. Neither
    does this bind to the scheduler since the cluster can
    be partitioned by size of the instances (S, M, L) or by
    a preemption setting (e.g. on-demand vs spot).

    Since ``Session`` allows the application to be submitted
    to multiple schedulers, users who want to submit the same
    app into multiple schedulers from the same session can
    union all the ``RunConfig``s into a single object. The
    scheduler implementation will selectively read the configs
    it needs.

    This class is intended to be trivially serialized and
    passed around or saved hence only allow primitives
    as config values. Should the scheduler need more than
    simple primitives (e.g. list of str) it is up to the
    scheduler to document a way to encode thie value as a
    str and parse it (e.g. representing list of str as
    comma delimited str).

    Usage:

    ::

     # write
     config = RunConfig()
     config.set("run_as_user", prod")
     config.set("priority", 10)

     # read
     config.get("run_as_user") # "prod"
     config.get("priority") # 10
     config.get("never_set") # None
    """

    cfgs: Dict[str, ConfigValue] = field(default_factory=dict)

    def set(self, cfg_key: str, cfg_val: ConfigValue) -> None:
        self.cfgs[cfg_key] = cfg_val

    def get(self, key: str) -> ConfigValue:
        return self.cfgs.get(key, None)

    def __repr__(self) -> str:
        return self.cfgs.__repr__()


T = TypeVar("T")


class AppDryRunInfo(Generic[T]):
    """
    Returned by ``Scheduler.submit_dryrun``. Represents the
    request that would have been made to the scheduler.
    The ``fmt_str()`` method of this object should return a
    pretty formatted string representation of the underlying
    request object such that ``print(info)`` yields a human
    readable representation of the underlying request.
    """

    def __init__(self, request: T, fmt: Callable[[T], str]) -> None:
        self.request = request
        self._fmt = fmt

        # fields below are only meant to be used by
        # Scheduler or Session implementations
        # and are back references to the parameters
        # to dryrun() that returned this AppDryRunInfo object
        # thus they are set in Session.dryrun() and Scheduler.submit_dryrun()
        # manually rather than through constructor arguments
        # DO NOT create getters or make these public
        # unless there is a good reason to
        self._app: Optional[Application] = None
        self._cfg: Optional[RunConfig] = None
        self._scheduler: Optional[SchedulerBackend] = None

    def __repr__(self) -> str:
        return self._fmt(self.request)


def get_type_name(tp: Type[ConfigValue]) -> str:
    """
    Gets the type's name as a string. If ``tp` is a primitive class like int, str, etc, then
    uses its attribute ``__name__``. Otherwise, use ``str(tp)``.

    Note: we use this mothod to print out generic typing like List[str].
    """
    if hasattr(tp, "__name__"):
        return tp.__name__
    else:
        return str(tp)


class runopts:
    """
    Holds the accepted scheduler run configuration
    keys, default value (if any), and help message string.
    These options are provided by the ``Scheduler`` and validated
    in ``Session.run`` against user provided ``RunConfig``.
    Allows ``None`` default values. Required opts must NOT have a
    non-None default.

    .. important:: This class has no accessors because it is intended to
                   be constructed and returned by ``Scheduler.run_config_options``
                   and printed out as a "help" tool or as part of an exception msg.
    Usage:

    ::
     opts = runopts()

     opts.add("run_as_user", type_=str, help="user to run the job as")
     opts.add("cluster_id", type_=int, help="cluster to submit the job", required=True)
     opts.add("priority", type_=float, default=0.5, help="job priority")
     opts.add("preemptible", type_=bool, default=False, help="is the job preemptible")

     # invalid
     opts.add("illegal", default=10, required=True)
     opts.add("bad_type", type=str, default=10)

     opts.check(RunConfig)
     print(opts)
    """

    def __init__(self) -> None:
        self._opts: Dict[str, Tuple[ConfigValue, Type[ConfigValue], bool, str]] = {}

    @staticmethod
    def is_type(obj: ConfigValue, tp: Type[ConfigValue]) -> bool:
        """
        Returns True if ``obj`` is type of ``tp``. Similar to isinstance() but supports
        tp = List[str], thus can be used to validate ConfigValue.
        """
        try:
            return isinstance(obj, tp)
        except TypeError:
            if isinstance(obj, list):
                return all(isinstance(e, str) for e in obj)
            else:
                return False

    def add(
        self,
        cfg_key: str,
        type_: Type[ConfigValue],
        help: str,
        default: ConfigValue = None,
        required: bool = False,
    ) -> None:
        """
        Adds the ``config`` option with the given help string and ``default``
        value (if any). If the ``default`` is not specified then this option
        is a required option.
        """
        if required and default is not None:
            raise ValueError(
                f"Required option: {cfg_key} must not specify default value. Given: {default}"
            )
        if default is not None:
            if not runopts.is_type(default, type_):
                raise TypeError(
                    f"Option: {cfg_key}, must be of type: {type_}."
                    f" Given: {default} ({type(default).__name__})"
                )

        self._opts[cfg_key] = (default, type_, required, help)

    def resolve(self, config: RunConfig) -> RunConfig:
        """
        Checks the given config against this ``runopts`` and sets default configs
        if not set.

        .. warning:: This method mutates the provided config!

        """

        # make a copy; don't need to be deep b/c the values are primitives
        resolved_cfg = RunConfig(config.cfgs.copy())

        for cfg_key, (default, type_, required, _help) in self._opts.items():
            val = resolved_cfg.get(cfg_key)

            # check required opt
            if required and val is None:
                raise InvalidRunConfigException(
                    f"Required run option: {cfg_key}, must be provided and not None",
                    config,
                    self,
                )

            # check type (None matches all types)
            if val is not None and not runopts.is_type(val, type_):
                raise InvalidRunConfigException(
                    f"Run option: {cfg_key}, must be of type: {get_type_name(type_)},"
                    f" but was: {val} ({type(val).__name__})",
                    config,
                    self,
                )

            # not required and not set, set to default
            if val is None:
                resolved_cfg.set(cfg_key, default)
        return resolved_cfg

    def __repr__(self) -> str:
        # make it a pretty printable dict
        pretty_opts = {}
        for cfg_key, (default, type_, required, help) in self._opts.items():
            key = f"*{cfg_key}" if required else cfg_key
            opt = {"type": get_type_name(type_)}
            if required:
                opt["required"] = "True"
            else:
                opt["default"] = str(default)
            opt["help"] = help

            pretty_opts[key] = opt
        import pprint

        return pprint.pformat(
            pretty_opts,
            indent=2,
            width=80,
        )


class InvalidRunConfigException(Exception):
    """
    Raised when the supplied ``RunConfig`` does not satisfy the
    ``runopts``, either due to missing required configs or value
    type mismatch.
    """

    def __init__(
        self, invalid_reason: str, run_config: RunConfig, runopts: "runopts"
    ) -> None:
        super().__init__(f"{invalid_reason}. Given: {run_config}, Expected: {runopts}")


class MalformedAppHandleException(Exception):
    """
    Raised when APIs are given a bad app handle.
    """

    def __init__(self, app_handle: str) -> None:
        super().__init__(
            f"{app_handle} is not of the form: <scheduler_backend>://<session_name>/<app_id>"
        )


class UnknownSchedulerException(Exception):
    def __init__(self, scheduler_backend: SchedulerBackend) -> None:
        super().__init__(
            f"Scheduler backend: {scheduler_backend} does not exist."
            f" Use session.scheduler_backends() to see all supported schedulers"
        )


# encodes information about a running app in url format
# {scheduler_backend}://{session_name}/{app_id}
AppHandle = str


class UnknownAppException(Exception):
    """
    Raised by ``Session`` APIs when either the application does not
    exist or the application is not owned by the session.
    """

    def __init__(self, app_handle: "AppHandle") -> None:
        super().__init__(
            f"Unknown app = {app_handle}. Did you forget to call session.run()?"
            f" Otherwise, the app may have already finished and purged by the scheduler"
        )


def make_app_handle(
    scheduler_backend: SchedulerBackend, session_name: str, app_id: str
) -> str:
    return f"{scheduler_backend}://{session_name}/{app_id}"


def parse_app_handle(app_handle: AppHandle) -> Tuple[SchedulerBackend, str, str]:
    """
    parses the app handle into ```(scheduler_backend, session_name, and app_id)```
    """

    # parse it manually b/c currently torchx does not
    # define allowed characters nor length for session name and app_id
    import re

    pattern = r"(?P<scheduler_backend>.+)://(?P<session_name>.+)/(?P<app_id>.+)"
    match = re.match(pattern, app_handle)
    if not match:
        raise MalformedAppHandleException(app_handle)
    gd = match.groupdict()
    return gd["scheduler_backend"], gd["session_name"], gd["app_id"]
