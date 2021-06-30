# Copyright 2019 Red Hat, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import json

import testtools

from zuul import model
from zuul.model import BuildRequestState, HoldRequest
from zuul.zk import ZooKeeperClient
from zuul.zk.config_cache import UnparsedConfigCache
from zuul.zk.exceptions import LockException
from zuul.zk.executor import ExecutorApi
from zuul.zk.nodepool import ZooKeeperNodepool
from zuul.zk.sharding import (
    RawShardIO,
    BufferedShardReader,
    BufferedShardWriter,
    NODE_BYTE_SIZE_LIMIT,
)
from zuul.zk.components import (
    BaseComponent, ComponentRegistry, ExecutorComponent
)

from tests.base import BaseTestCase, iterate_timeout


class ZooKeeperBaseTestCase(BaseTestCase):

    def setUp(self):
        super().setUp()

        self.setupZK()

        self.zk_client = ZooKeeperClient(
            self.zk_chroot_fixture.zk_hosts,
            tls_cert=self.zk_chroot_fixture.zookeeper_cert,
            tls_key=self.zk_chroot_fixture.zookeeper_key,
            tls_ca=self.zk_chroot_fixture.zookeeper_ca)
        self.addCleanup(self.zk_client.disconnect)
        self.zk_client.connect()


class TestNodepool(ZooKeeperBaseTestCase):

    def setUp(self):
        super().setUp()
        self.zk_nodepool = ZooKeeperNodepool(self.zk_client)

    def _createRequest(self):
        req = HoldRequest()
        req.count = 1
        req.reason = 'some reason'
        req.expiration = 1
        return req

    def test_hold_requests_api(self):
        # Test no requests returns empty list
        self.assertEqual([], self.zk_nodepool.getHoldRequests())

        # Test get on non-existent request is None
        self.assertIsNone(self.zk_nodepool.getHoldRequest('anything'))

        # Test creating a new request
        req1 = self._createRequest()
        self.zk_nodepool.storeHoldRequest(req1)
        self.assertIsNotNone(req1.id)
        self.assertEqual(1, len(self.zk_nodepool.getHoldRequests()))

        # Test getting the request
        req2 = self.zk_nodepool.getHoldRequest(req1.id)
        self.assertEqual(req1.toDict(), req2.toDict())

        # Test updating the request
        req2.reason = 'a new reason'
        self.zk_nodepool.storeHoldRequest(req2)
        req2 = self.zk_nodepool.getHoldRequest(req2.id)
        self.assertNotEqual(req1.reason, req2.reason)

        # Test lock operations
        self.zk_nodepool.lockHoldRequest(req2, blocking=False)
        with testtools.ExpectedException(
            LockException, "Timeout trying to acquire lock .*"
        ):
            self.zk_nodepool.lockHoldRequest(req2, blocking=True, timeout=2)
        self.zk_nodepool.unlockHoldRequest(req2)
        self.assertIsNone(req2.lock)

        # Test deleting the request
        self.zk_nodepool.deleteHoldRequest(req1)
        self.assertEqual([], self.zk_nodepool.getHoldRequests())


class TestSharding(ZooKeeperBaseTestCase):

    def test_reader(self):
        shard_io = RawShardIO(self.zk_client.client, "/test/shards")
        self.assertEqual(len(shard_io._shards), 0)

        with BufferedShardReader(
            self.zk_client.client, "/test/shards"
        ) as shard_reader:
            self.assertEqual(shard_reader.read(), b"")
            shard_io.write(b"foobar")
            self.assertEqual(len(shard_io._shards), 1)
            self.assertEqual(shard_io.read(), b"foobar")

    def test_writer(self):
        shard_io = RawShardIO(self.zk_client.client, "/test/shards")
        self.assertEqual(len(shard_io._shards), 0)

        with BufferedShardWriter(
            self.zk_client.client, "/test/shards"
        ) as shard_writer:
            shard_writer.write(b"foobar")

        self.assertEqual(len(shard_io._shards), 1)
        self.assertEqual(shard_io.read(), b"foobar")

    def test_truncate(self):
        shard_io = RawShardIO(self.zk_client.client, "/test/shards")
        shard_io.write(b"foobar")
        self.assertEqual(len(shard_io._shards), 1)

        with BufferedShardWriter(
            self.zk_client.client, "/test/shards"
        ) as shard_writer:
            shard_writer.truncate(0)

        self.assertEqual(len(shard_io._shards), 0)

    def test_shard_bytes_limit(self):
        with BufferedShardWriter(
            self.zk_client.client, "/test/shards"
        ) as shard_writer:
            shard_writer.write(b"x" * (NODE_BYTE_SIZE_LIMIT + 1))
            shard_writer.flush()
            self.assertEqual(len(shard_writer.raw._shards), 2)

    def test_json(self):
        data = {"key": "value"}
        with BufferedShardWriter(
            self.zk_client.client, "/test/shards"
        ) as shard_io:
            shard_io.write(json.dumps(data).encode("utf8"))

        with BufferedShardReader(
            self.zk_client.client, "/test/shards"
        ) as shard_io:
            self.assertDictEqual(json.load(shard_io), data)


class TestUnparsedConfigCache(ZooKeeperBaseTestCase):

    def setUp(self):
        super().setUp()
        self.config_cache = UnparsedConfigCache(self.zk_client)

    def test_files_cache(self):
        master_files = self.config_cache.getFilesCache("project", "master")

        with self.config_cache.readLock("project"):
            self.assertEqual(len(master_files), 0)

        with self.config_cache.writeLock("project"):
            master_files["/path/to/file"] = "content"

        with self.config_cache.readLock("project"):
            self.assertEqual(master_files["/path/to/file"], "content")
            self.assertEqual(len(master_files), 1)

    def test_valid_for(self):
        tpc = model.TenantProjectConfig("project")
        tpc.extra_config_files = {"foo.yaml", "bar.yaml"}
        tpc.extra_config_dirs = {"foo.d/", "bar.d/"}

        master_files = self.config_cache.getFilesCache("project", "master")
        self.assertFalse(master_files.isValidFor(tpc, cache_ltime=-1))

        master_files.setValidFor(tpc.extra_config_files, tpc.extra_config_dirs)
        self.assertTrue(master_files.isValidFor(tpc, cache_ltime=-1))

        tpc.extra_config_files = set()
        tpc.extra_config_dirs = set()
        self.assertTrue(master_files.isValidFor(tpc, cache_ltime=-1))

        tpc.extra_config_files = {"bar.yaml"}
        tpc.extra_config_dirs = {"bar.d/"}
        # Valid for subset
        self.assertTrue(master_files.isValidFor(tpc, cache_ltime=-1))

        tpc.extra_config_files = {"foo.yaml", "bar.yaml"}
        tpc.extra_config_dirs = {"foo.d/", "bar.d/", "other.d/"}
        # Invalid for additional dirs
        self.assertFalse(master_files.isValidFor(tpc, cache_ltime=-1))

        tpc.extra_config_files = {"foo.yaml", "bar.yaml", "other.yaml"}
        tpc.extra_config_dirs = {"foo.d/", "bar.d/"}
        # Invalid for additional files
        self.assertFalse(master_files.isValidFor(tpc, cache_ltime=-1))

    def test_branch_cleanup(self):
        master_files = self.config_cache.getFilesCache("project", "master")
        release_files = self.config_cache.getFilesCache("project", "release")

        master_files["/path/to/file"] = "content"
        release_files["/path/to/file"] = "content"

        self.config_cache.clearCache("project", "master")
        self.assertEqual(len(master_files), 0)
        self.assertEqual(len(release_files), 1)

    def test_project_cleanup(self):
        master_files = self.config_cache.getFilesCache("project", "master")
        stable_files = self.config_cache.getFilesCache("project", "stable")
        other_files = self.config_cache.getFilesCache("other", "master")

        self.assertEqual(len(master_files), 0)
        self.assertEqual(len(stable_files), 0)
        master_files["/path/to/file"] = "content"
        stable_files["/path/to/file"] = "content"
        other_files["/path/to/file"] = "content"
        self.assertEqual(len(master_files), 1)
        self.assertEqual(len(stable_files), 1)
        self.assertEqual(len(other_files), 1)

        self.config_cache.clearCache("project")
        self.assertEqual(len(master_files), 0)
        self.assertEqual(len(stable_files), 0)
        self.assertEqual(len(other_files), 1)


class TestComponentRegistry(ZooKeeperBaseTestCase):
    def setUp(self):
        super().setUp()
        self.second_zk_client = ZooKeeperClient(
            self.zk_chroot_fixture.zk_hosts,
            tls_cert=self.zk_chroot_fixture.zookeeper_cert,
            tls_key=self.zk_chroot_fixture.zookeeper_key,
            tls_ca=self.zk_chroot_fixture.zookeeper_ca,
        )
        self.addCleanup(self.second_zk_client.disconnect)
        self.second_zk_client.connect()
        self.component_registry = ComponentRegistry(self.second_zk_client)

    def assertComponentAttr(self, component_name, attr_name,
                            attr_value, timeout=10):
        for _ in iterate_timeout(
            timeout,
            f"{component_name} in cache has {attr_name} set to {attr_value}",
        ):
            components = list(self.component_registry.all(component_name))
            if (
                len(components) > 0 and
                getattr(components[0], attr_name) == attr_value
            ):
                break

    def assertComponentState(self, component_name, state, timeout=10):
        return self.assertComponentAttr(
            component_name, "state", state, timeout
        )

    def assertComponentStopped(self, component_name, timeout=10):
        for _ in iterate_timeout(
            timeout, f"{component_name} in cache is stopped"
        ):
            components = list(self.component_registry.all(component_name))
            if len(components) == 0:
                break

    def test_component_registry(self):
        self.component_info = ExecutorComponent(self.zk_client, 'test')
        self.component_info.register()
        self.assertComponentState("executor", BaseComponent.STOPPED)

        self.zk_client.client.stop()
        self.assertComponentStopped("executor")

        self.zk_client.client.start()
        self.assertComponentState("executor", BaseComponent.STOPPED)

        self.component_info.state = self.component_info.RUNNING
        self.assertComponentState("executor", BaseComponent.RUNNING)

        self.log.debug("DISCONNECT")
        self.second_zk_client.client.stop()
        self.second_zk_client.client.start()
        self.log.debug("RECONNECT")
        self.component_info.state = self.component_info.PAUSED
        self.assertComponentState("executor", BaseComponent.PAUSED)

        # Make sure the registry didn't create any read/write
        # component objects that re-registered themselves.
        components = list(self.component_registry.all('executor'))
        self.assertEqual(len(components), 1)

        self.component_info.state = self.component_info.RUNNING
        self.assertComponentState("executor", BaseComponent.RUNNING)


class TestExecutorApi(ZooKeeperBaseTestCase):

    def test_lost_build_requests(self):
        executor_api = ExecutorApi(self.zk_client, use_cache=True)

        executor_api.submit("A", "tenant", "pipeline", {}, "zone")
        path_b = executor_api.submit("B", "tenant", "pipeline", {}, "zone")
        path_c = executor_api.submit("C", "tenant", "pipeline", {}, "zone")
        path_d = executor_api.submit("D", "tenant", "pipeline", {}, "zone")
        path_e = executor_api.submit("E", "tenant", "pipeline", {}, "zone")

        b = executor_api.get(path_b)
        c = executor_api.get(path_c)
        d = executor_api.get(path_d)
        e = executor_api.get(path_e)

        b.state = BuildRequestState.RUNNING
        executor_api.update(b)

        c.state = BuildRequestState.RUNNING
        executor_api.update(c)
        executor_api.lock(c)

        d.state = BuildRequestState.COMPLETED
        executor_api.update(d)

        e.state = BuildRequestState.PAUSED
        executor_api.update(e)

        # Wait until the latest state transition is reflected in the Executor
        # APIs cache. Using a DataWatch for this purpose could lead to race
        # conditions depending on which DataWatch is executed first. The
        # DataWatch might be triggered for the correct event, but the cache
        # might still be outdated as the DataWatch that updates the cache
        # itself wasn't triggered yet.
        for _ in iterate_timeout(30, "wait for cache to be up-to-date"):
            if (
                executor_api._cached_build_requests[path_e].state
                == BuildRequestState.PAUSED
            ):
                break

        # The lost_builds method should only return builds which are running or
        # paused, but not locked by any executor, in this case build b and e.
        lost_build_requests = list(executor_api.lostBuildRequests())

        self.assertEqual(2, len(lost_build_requests))
        self.assertEqual(b.path, lost_build_requests[0].path)
