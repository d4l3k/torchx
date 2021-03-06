#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.


import dataclasses
import json
import os
import unittest
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Union, Text, cast
from unittest.mock import MagicMock

from torchx.specs.api import (
    _TERMINAL_STATES,
    MISSING,
    NULL_CONTAINER,
    NULL_RESOURCE,
    AppHandle,
    Application,
    AppState,
    AppStatus,
    Container,
    ElasticRole,
    InvalidRunConfigException,
    MalformedAppHandleException,
    Resource,
    RetryPolicy,
    AppDryRunInfo,
    Role,
    RunConfig,
    SchedulerBackend,
    get_type_name,
    macros,
    make_app_handle,
    parse_app_handle,
    runopts,
)


class AppDryRunInfoTest(unittest.TestCase):
    def test_repr(self) -> None:
        request_mock = MagicMock()
        to_string_mock = MagicMock()
        info = AppDryRunInfo(request_mock, to_string_mock)
        info.__repr__()
        self.assertEqual(request_mock, info.request)

        to_string_mock.assert_called_once_with(request_mock)


class ApplicationStatusTest(unittest.TestCase):
    def test_is_terminal(self) -> None:
        for s in AppState:
            is_terminal = AppStatus(state=s).is_terminal()
            if s in _TERMINAL_STATES:
                self.assertTrue(is_terminal)
            else:
                self.assertFalse(is_terminal)

    def test_serialize(self) -> None:
        status = AppStatus(AppState.FAILED)
        serialized = status.__repr__()
        deser_status_dict = json.loads(serialized)
        deser_status = AppStatus(**deser_status_dict)
        self.assertEqual(status.state, deser_status.state)
        self.assertEqual(status.msg, deser_status.msg)
        self.assertEqual(status.structured_error_msg, deser_status.structured_error_msg)

    def test_serialize_embed_json(self) -> None:
        status = AppStatus(
            AppState.FAILED, structured_error_msg='{"message": "test error"}'
        )
        serialized = status.__repr__()
        deser_status_dict = json.loads(serialized)
        scheduler_msg = deser_status_dict.pop("structured_error_msg")
        scheduler_msg_json = json.dumps(scheduler_msg)
        deser_status_dict["structured_error_msg"] = scheduler_msg_json
        deser_status = AppStatus(**deser_status_dict)
        self.assertEqual(status.state, deser_status.state)
        self.assertEqual(status.msg, deser_status.msg)
        self.assertEqual(status.structured_error_msg, deser_status.structured_error_msg)


class ResourceTest(unittest.TestCase):
    def test_copy_resource(self) -> None:
        old_capabilities = {"test_key": "test_value", "old_key": "old_value"}
        resource = Resource(1, 2, 3, old_capabilities)
        new_resource = Resource.copy(
            resource, test_key="test_value_new", new_key="new_value"
        )
        self.assertEqual(new_resource.cpu, 1)
        self.assertEqual(new_resource.gpu, 2)
        self.assertEqual(new_resource.memMB, 3)

        self.assertEqual(len(new_resource.capabilities), 3)
        self.assertEqual(new_resource.capabilities["old_key"], "old_value")
        self.assertEqual(new_resource.capabilities["test_key"], "test_value_new")
        self.assertEqual(new_resource.capabilities["new_key"], "new_value")
        self.assertEqual(resource.capabilities["test_key"], "test_value")


class ContainerBuilderTest(unittest.TestCase):
    def test_create_container_with_resource(self) -> None:
        res1 = Resource(cpu=1, gpu=2, memMB=128)
        res2 = Resource(cpu=1, gpu=2, memMB=256)
        container = Container("torch").require(res1).require(res2)
        self.assertEqual(res2, container.resources)

    def test_create_container_no_backend(self) -> None:
        res1 = Resource(cpu=1, gpu=2, memMB=128)
        container = Container("torch").require(res1)
        self.assertEqual(res1, container.resources)


class RoleBuilderTest(unittest.TestCase):
    def test_defaults(self) -> None:
        default = Role("foobar")
        self.assertEqual("foobar", default.name)
        self.assertEqual(MISSING, default.entrypoint)
        self.assertEqual({}, default.env)
        self.assertEqual([], default.args)
        self.assertEqual(NULL_CONTAINER, default.container)
        self.assertEqual(1, default.num_replicas)
        self.assertEqual(0, default.max_retries)
        self.assertEqual(RetryPolicy.APPLICATION, default.retry_policy)

    def test_build_role(self) -> None:
        # runs: ENV_VAR_1=FOOBAR /bin/echo hello world
        container = Container(image="test_image")
        container.ports(foo=8080)
        trainer = (
            Role("trainer")
            .runs("/bin/echo", "hello", "world", ENV_VAR_1="FOOBAR")
            .on(container)
            .replicas(2)
            .with_retry_policy(RetryPolicy.REPLICA, max_retries=5)
        )

        self.assertEqual("trainer", trainer.name)
        self.assertEqual("/bin/echo", trainer.entrypoint)
        self.assertEqual({"ENV_VAR_1": "FOOBAR"}, trainer.env)
        self.assertEqual(["hello", "world"], trainer.args)
        self.assertEqual(container, trainer.container)
        self.assertEqual(2, trainer.num_replicas)
        self.assertEqual(5, trainer.max_retries)
        self.assertEqual(RetryPolicy.REPLICA, trainer.retry_policy)


class ElasticRoleBuilderTest(unittest.TestCase):
    def test_build_elastic_role(self) -> None:
        # runs: python -m torchelastic.distributed.launch
        #                    --nnodes 2:4
        #                    --max_restarts 3
        #                    --no_python True
        #                    --rdzv_backend etcd
        #                    --rdzv_id ${app_id}
        #                    /bin/echo hello world
        container = Container(image="test_image")
        container.ports(foo=8080)
        elastic_trainer = (
            ElasticRole("elastic_trainer", nnodes="2:4", max_restarts=3, no_python=True)
            .runs("/bin/echo", "hello", "world", ENV_VAR_1="FOOBAR")
            .on(container)
            .replicas(2)
        )
        self.assertEqual("elastic_trainer", elastic_trainer.name)
        self.assertEqual("python", elastic_trainer.entrypoint)
        self.assertEqual(
            [
                "-m",
                "torchelastic.distributed.launch",
                "--nnodes",
                "2:4",
                "--max_restarts",
                "3",
                "--no_python",
                "--rdzv_backend",
                "etcd",
                "--rdzv_id",
                macros.app_id,
                "--role",
                "elastic_trainer",
                "/bin/echo",
                "hello",
                "world",
            ],
            elastic_trainer.args,
        )
        self.assertEqual({"ENV_VAR_1": "FOOBAR"}, elastic_trainer.env)
        self.assertEqual(container, elastic_trainer.container)
        self.assertEqual(2, elastic_trainer.num_replicas)

    def test_build_elastic_role_override_rdzv_params(self) -> None:
        role = ElasticRole(
            "test_role", nnodes="2:4", rdzv_backend="etcd", rdzv_id="foobar"
        ).runs("user_script.py", "--script_arg", "foo")
        self.assertEqual(
            [
                "-m",
                "torchelastic.distributed.launch",
                "--nnodes",
                "2:4",
                "--rdzv_backend",
                "etcd",
                "--rdzv_id",
                "foobar",
                "--role",
                "test_role",
                os.path.join(macros.img_root, "user_script.py"),
                "--script_arg",
                "foo",
            ],
            role.args,
        )

    def test_build_elastic_role_flag_args(self) -> None:
        role = ElasticRole("test_role", no_python=False).runs("user_script.py")
        self.assertEqual(
            [
                "-m",
                "torchelastic.distributed.launch",
                "--rdzv_backend",
                "etcd",
                "--rdzv_id",
                macros.app_id,
                "--role",
                "test_role",
                os.path.join(macros.img_root, "user_script.py"),
            ],
            role.args,
        )

    def test_build_elastic_role_img_root_already_in_entrypoint(self) -> None:
        role = ElasticRole("test_role", no_python=False).runs(
            os.path.join(macros.img_root, "user_script.py")
        )
        self.assertEqual(
            [
                "-m",
                "torchelastic.distributed.launch",
                "--rdzv_backend",
                "etcd",
                "--rdzv_id",
                macros.app_id,
                "--role",
                "test_role",
                os.path.join(macros.img_root, "user_script.py"),
            ],
            role.args,
        )

    def test_json_serialization(self) -> None:
        """
        Tests that an ElasticRole can be serialized into json (dict)
        then recreated as a Role. An ElasticRole is really just a builder
        utility to make it easy for users to create a Role with the entrypoint
        being ``torchelastic.distributed.launch``
        """
        resources = Resource(cpu=1, gpu=0, memMB=512)
        container = Container(image="user_image", resources=resources).ports(
            tensorboard=8080
        )
        elastic_role = (
            ElasticRole(
                "test_role", nnodes="2:4", rdzv_backend="etcd", rdzv_id="foobar"
            )
            .runs("user_script.py", "--script_arg", "foo")
            .on(container)
            .replicas(3)
        )

        # this is effectively JSON
        elastic_json = dataclasses.asdict(elastic_role)
        container_json = elastic_json.pop("container")
        resource_json = container_json.pop("resources")
        container_json["resources"] = Resource(**resource_json)

        role = Role(
            **elastic_json,
            container=Container(**container_json),
        )
        self.assertEqual(container, role.container)
        self.assertEqual(elastic_role.name, role.name)
        self.assertEqual(elastic_role.entrypoint, role.entrypoint)
        self.assertEqual(
            elastic_role.args,
            role.args,
        )
        self.assertEqual(dataclasses.asdict(elastic_role), dataclasses.asdict(role))


class AppHandleTest(unittest.TestCase):
    def test_parse_malformed_app_handles(self) -> None:
        bad_app_handles = {
            "my_session/my_application_id": "missing scheduler backend",
            "local://my_session/": "missing app_id",
            "local://my_application_id": "missing session",
        }

        for handle, msg in bad_app_handles.items():
            with self.subTest(f"malformed app handle: {msg}", handle=handle):
                with self.assertRaises(MalformedAppHandleException):
                    parse_app_handle(handle)

    def test_parse(self) -> None:
        (scheduler_backend, session_name, app_id) = parse_app_handle(
            "local://my_session/my_app_id_1234"
        )
        self.assertEqual("local", scheduler_backend)
        self.assertEqual("my_session", session_name)
        self.assertEqual("my_app_id_1234", app_id)

    def test_make(self) -> None:
        app_handle = make_app_handle(
            scheduler_backend="local",
            session_name="my_session",
            app_id="my_app_id_1234",
        )
        self.assertEqual("local://my_session/my_app_id_1234", app_handle)


class ApplicationTest(unittest.TestCase):
    def test_application(self) -> None:
        container = Container(image="test_image")
        trainer = Role("trainer").runs("/bin/sleep", "10").on(container).replicas(2)
        app = Application(name="test_app").of(trainer)
        self.assertEqual("test_app", app.name)
        self.assertEqual(1, len(app.roles))
        self.assertEqual(trainer, app.roles[0])

    def test_application_default(self) -> None:
        app = Application(name="test_app")
        self.assertEqual(0, len(app.roles))

    def test_getset_metadata(self) -> None:
        app = Application(name="test_app").add_metadata("test_key", "test_value")
        self.assertEqual("test_value", app.get_metadata("test_key"))
        self.assertEqual(None, app.get_metadata("non_existent"))


class RunConfigTest(unittest.TestCase):
    def get_cfg(self) -> RunConfig:
        cfg = RunConfig()
        cfg.set("run_as", "root")
        cfg.set("cluster_id", 123)
        cfg.set("priority", 0.5)
        cfg.set("preemtible", True)
        return cfg

    def test_valid_values(self) -> None:
        cfg = self.get_cfg()

        self.assertEqual("root", cfg.get("run_as"))
        self.assertEqual(123, cfg.get("cluster_id"))
        self.assertEqual(0.5, cfg.get("priority"))
        self.assertTrue(cfg.get("preemtible"))
        self.assertIsNone(cfg.get("unknown"))

    def test_serde(self) -> None:
        """
        tests trivial serialization into dict then back
        """
        cfg = self.get_cfg()
        ser = dataclasses.asdict(cfg)
        deser = RunConfig(**ser)

        self.assertEqual("root", deser.get("run_as"))
        self.assertEqual(123, deser.get("cluster_id"))
        self.assertEqual(0.5, deser.get("priority"))
        self.assertTrue(deser.get("preemtible"))

    def test_runopts_add(self) -> None:
        """
        tests for various add option variations
        does not assert anything, a successful test
        should not raise any unexpected errors
        """
        opts = runopts()
        opts.add("run_as", type_=str, help="run as user")
        opts.add("run_as_default", type_=str, help="run as user", default="root")
        opts.add("run_as_required", type_=str, help="run as user", required=True)

        with self.assertRaises(ValueError):
            opts.add(
                "run_as", type_=str, help="run as user", default="root", required=True
            )

        opts.add("priority", type_=int, help="job priority", default=10)

        with self.assertRaises(TypeError):
            opts.add("priority", type_=int, help="job priority", default=0.5)

        # this print is intentional (demonstrates the intended usecase)
        print(opts)

    def get_runopts(self) -> runopts:
        opts = runopts()
        opts.add("run_as", type_=str, help="run as user", required=True)
        opts.add("priority", type_=int, help="job priority", default=10)
        opts.add("cluster_id", type_=str, help="cluster to submit job")
        return opts

    def test_runopts_resolve_minimal(self) -> None:
        opts = self.get_runopts()

        cfg = RunConfig()
        cfg.set("run_as", "foobar")

        resolved = opts.resolve(cfg)
        self.assertEqual("foobar", resolved.get("run_as"))
        self.assertEqual(10, resolved.get("priority"))
        self.assertIsNone(resolved.get("cluster_id"))

        # make sure original config is untouched
        self.assertEqual("foobar", cfg.get("run_as"))
        self.assertIsNone(cfg.get("priority"))
        self.assertIsNone(cfg.get("cluster_id"))

    def test_runopts_resolve_override(self) -> None:
        opts = self.get_runopts()

        cfg = RunConfig()
        cfg.set("run_as", "foobar")
        cfg.set("priority", 20)
        cfg.set("cluster_id", "test_cluster")

        resolved = opts.resolve(cfg)
        self.assertEqual("foobar", resolved.get("run_as"))
        self.assertEqual(20, resolved.get("priority"))
        self.assertEqual("test_cluster", resolved.get("cluster_id"))

    def test_runopts_resolve_missing_required(self) -> None:
        opts = self.get_runopts()

        cfg = RunConfig()
        cfg.set("priority", 20)
        cfg.set("cluster_id", "test_cluster")

        with self.assertRaises(InvalidRunConfigException):
            opts.resolve(cfg)

    def test_runopts_resolve_bad_type(self) -> None:
        opts = self.get_runopts()

        cfg = RunConfig()
        cfg.set("run_as", "foobar")
        cfg.set("cluster_id", 123)

        with self.assertRaises(InvalidRunConfigException):
            opts.resolve(cfg)

    def test_runopts_resolve_unioned(self) -> None:
        # runconfigs is a union of all run opts for all schedulers
        # make sure  opts resolves run configs that have more
        # configs than it knows about
        opts = self.get_runopts()

        cfg = RunConfig()
        cfg.set("run_as", "foobar")
        cfg.set("some_other_opt", "baz")

        resolved = opts.resolve(cfg)
        self.assertEqual("foobar", resolved.get("run_as"))
        self.assertEqual(10, resolved.get("priority"))
        self.assertIsNone(resolved.get("cluster_id"))
        self.assertEqual("baz", resolved.get("some_other_opt"))

    def test_runopts_is_type(self) -> None:
        # primitive types
        self.assertTrue(runopts.is_type(3, int))
        self.assertFalse(runopts.is_type("foo", int))
        # List[str]
        self.assertFalse(runopts.is_type(None, List[str]))
        self.assertTrue(runopts.is_type([], List[str]))
        self.assertTrue(runopts.is_type(["a", "b"], List[str]))


class GetTypeNameTest(unittest.TestCase):
    def test_get_type_name(self) -> None:
        self.assertEqual("int", get_type_name(int))
        self.assertEqual("list", get_type_name(list))
        self.assertEqual("typing.Union[str, int]", get_type_name(Union[str, int]))
        self.assertEqual("typing.List[int]", get_type_name(List[int]))
        self.assertEqual("typing.Dict[str, int]", get_type_name(Dict[str, int]))
        self.assertEqual(
            "typing.List[typing.List[int]]", get_type_name(List[List[int]])
        )


class MacrosTest(unittest.TestCase):
    def test_substitute(self) -> None:
        v = macros.Values(
            img_root="img_root",
            app_id="app_id",
            replica_id="replica_id",
            base_img_root="base_img_root",
        )
        for key, val in dataclasses.asdict(v).items():
            template = f"tmpl-{getattr(macros, key)}"
            self.assertEqual(v.substitute(template), f"tmpl-{val}")

    def test_apply(self) -> None:
        role = Role(name="test").runs("foo.py", macros.img_root, FOO=macros.app_id)
        v = macros.Values(
            img_root="img_root",
            app_id="app_id",
            replica_id="replica_id",
            base_img_root="base_img_root",
        )
        newrole = v.apply(role)
        self.assertNotEqual(newrole, role)
        self.assertEqual(newrole.args, ["img_root"])
        self.assertEqual(newrole.env, {"FOO": "app_id"})
