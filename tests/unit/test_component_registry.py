# Copyright 2021 BMW Group
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

from zuul.lib.fingergw import FingerGateway
from zuul.zk import ZooKeeperClient
from zuul.zk.components import BaseComponent, ComponentRegistry

from tests.base import iterate_timeout, ZuulTestCase, ZuulWebFixture


class TestComponentRegistry(ZuulTestCase):
    tenant_config_file = 'config/single-tenant/main.yaml'

    def setUp(self):
        super().setUp()

        self.zk_client = ZooKeeperClient(
            self.zk_chroot_fixture.zk_hosts,
            tls_cert=self.zk_chroot_fixture.zookeeper_cert,
            tls_key=self.zk_chroot_fixture.zookeeper_key,
            tls_ca=self.zk_chroot_fixture.zookeeper_ca,
        )
        self.addCleanup(self.zk_client.disconnect)
        self.zk_client.connect()
        self.component_registry = ComponentRegistry(self.zk_client)

    def assertComponentState(self, component_name, state, timeout=5):
        for _ in iterate_timeout(
            timeout, f"{component_name} in cache is in state {state}"
        ):
            components = list(self.component_registry.all(component_name))
            if len(components) > 0 and components[0].state == state:
                break

    def assertComponentStopped(self, component_name, timeout=5):
        for _ in iterate_timeout(
            timeout, f"{component_name} in cache is stopped"
        ):
            components = list(self.component_registry.all(component_name))
            if len(components) == 0:
                break

    def test_scheduler_component(self):
        self.assertComponentState("scheduler", BaseComponent.RUNNING)

    def test_executor_component(self):
        self.assertComponentState("executor", BaseComponent.RUNNING)

        self.executor_server.pause()
        self.assertComponentState("executor", BaseComponent.PAUSED)

        self.executor_server.unpause()
        self.assertComponentState("executor", BaseComponent.RUNNING)

    def test_merger_component(self):
        self._startMerger()
        self.assertComponentState("merger", BaseComponent.RUNNING)

        self.merge_server.pause()
        self.assertComponentState("merger", BaseComponent.PAUSED)

        self.merge_server.unpause()
        self.assertComponentState("merger", BaseComponent.RUNNING)

        self.merge_server.stop()
        self.merge_server.join()
        # Set the merger to None so the test doesn't try to stop it again
        self.merge_server = None

        self.assertComponentStopped("merger")

    def test_fingergw_component(self):
        gateway = FingerGateway(
            self.config,
            ("127.0.0.1", self.gearman_server.port, None, None, None),
            ("127.0.0.1", 0),
            user=None,
            command_socket=None,
            pid_file=None
        )
        gateway.start()

        try:
            self.assertComponentState("fingergw", BaseComponent.RUNNING)
        finally:
            gateway.stop()
            gateway.wait()

        self.assertComponentStopped("fingergw")

    def test_web_component(self):
        self.useFixture(
            ZuulWebFixture(
                self.changes, self.config, self.additional_event_queues,
                self.upstream_root, self.rpcclient, self.poller_events,
                self.git_url_with_auth, self.addCleanup, self.test_root
            )
        )

        self.assertComponentState("web", BaseComponent.RUNNING)
