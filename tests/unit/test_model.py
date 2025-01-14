# Copyright 2015 Red Hat, Inc.
# Copyright 2023 Acme Gating, LLC
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


import configparser
import collections
import os
import random
import types
import uuid
from unittest import mock

import fixtures
import testtools

from zuul import model
from zuul import configloader
from zuul.lib import encryption
from zuul.lib import yamlutil as yaml
import zuul.lib.connections

from tests.base import BaseTestCase, FIXTURE_DIR
from zuul.lib.ansible import AnsibleManager
from zuul.lib import tracing
from zuul.lib.re2util import ZuulRegex
from zuul.model_api import MODEL_API
from zuul.zk.zkobject import LocalZKContext
from zuul.zk.components import COMPONENT_REGISTRY
from zuul import change_matcher


class Dummy(object):
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class TestJob(BaseTestCase):
    def setUp(self):
        COMPONENT_REGISTRY.registry = Dummy()
        COMPONENT_REGISTRY.registry.model_api = MODEL_API
        self._env_fixture = self.useFixture(
            fixtures.EnvironmentVariable('HISTTIMEFORMAT', '%Y-%m-%dT%T%z '))
        super(TestJob, self).setUp()
        # Toss in % in env vars to trigger the configparser issue
        self.connections = zuul.lib.connections.ConnectionRegistry()
        self.addCleanup(self.connections.stop)
        self.connection = Dummy(connection_name='dummy_connection')
        self.source = Dummy(canonical_hostname='git.example.com',
                            connection=self.connection)
        self.abide = model.Abide()
        self.tenant = model.Tenant('tenant')
        self.tenant.default_ansible_version = AnsibleManager().default_version
        self.tenant.semaphore_handler = Dummy(abide=self.abide)
        self.layout = model.Layout(self.tenant)
        self.tenant.layout = self.layout
        self.project = model.Project('project', self.source)
        self.context = model.SourceContext(
            self.project.canonical_name, self.project.name,
            self.project.connection_name, 'master', 'test', True)
        self.untrusted_context = model.SourceContext(
            self.project.canonical_name, self.project.name,
            self.project.connection_name, 'master', 'test', False)
        self.tpc = model.TenantProjectConfig(self.project)
        self.tenant.addUntrustedProject(self.tpc)
        self.pipeline = model.Pipeline('gate', self.tenant)
        self.pipeline.source_context = self.context
        self.pipeline.manager = mock.Mock()
        self.pipeline.tenant = self.tenant
        self.zk_context = LocalZKContext(self.log)
        self.pipeline.manager.current_context = self.zk_context
        self.pipeline.state = model.PipelineState()
        self.pipeline.state._set(pipeline=self.pipeline)
        self.layout.addPipeline(self.pipeline)
        with self.zk_context as ctx:
            self.queue = model.ChangeQueue.new(
                ctx, pipeline=self.pipeline)
        self.pcontext = configloader.ParseContext(
            self.connections, None, self.tenant, AnsibleManager())

        private_key_file = os.path.join(FIXTURE_DIR, 'private.pem')
        with open(private_key_file, "rb") as f:
            priv, pub = encryption.deserialize_rsa_keypair(f.read())
            self.project.private_secrets_key = priv
            self.project.public_secrets_key = pub
        m = yaml.Mark('name', 0, 0, 0, '', 0)
        self.start_mark = model.ZuulMark(m, m, '')
        config = configparser.ConfigParser()
        self.tracing = tracing.Tracing(config)

    @property
    def job(self):
        job = self.pcontext.job_parser.fromYaml({
            '_source_context': self.context,
            '_start_mark': self.start_mark,
            'name': 'job',
            'parent': None,
            'irrelevant-files': [
                '^docs/.*$'
            ]}, None)
        return job

    def test_change_matches_returns_false_for_matched_skip_if(self):
        change = model.Change('project')
        change.files = ['/COMMIT_MSG', 'docs/foo']
        self.assertFalse(self.job.changeMatchesFiles(change))

    def test_change_matches_returns_false_for_single_matched_skip_if(self):
        change = model.Change('project')
        change.files = ['docs/foo']
        self.assertFalse(self.job.changeMatchesFiles(change))

    def test_change_matches_returns_true_for_unmatched_skip_if(self):
        change = model.Change('project')
        change.files = ['/COMMIT_MSG', 'foo']
        self.assertTrue(self.job.changeMatchesFiles(change))

    def test_change_matches_returns_true_for_single_unmatched_skip_if(self):
        change = model.Change('project')
        change.files = ['foo']
        self.assertTrue(self.job.changeMatchesFiles(change))

    def test_job_sets_defaults_for_boolean_attributes(self):
        self.assertIsNotNone(self.job.voting)

    def test_job_variants(self):
        # This simulates freezing a job.

        secrets = ['foo']
        py27_pre = model.PlaybookContext(
            self.context, 'py27-pre', [], secrets, [])
        py27_run = model.PlaybookContext(
            self.context, 'py27-run', [], secrets, [])
        py27_post = model.PlaybookContext(
            self.context, 'py27-post', [], secrets, [])

        py27 = model.Job('py27')
        py27.timeout = 30
        py27.pre_run = (py27_pre,)
        py27.run = (py27_run,)
        py27.post_run = (py27_post,)

        job = py27.copy()
        self.assertEqual(30, job.timeout)

        # Apply the diablo variant
        diablo = model.Job('py27')
        diablo.timeout = 40
        job.applyVariant(diablo, self.layout, None)

        self.assertEqual(40, job.timeout)
        self.assertEqual(['py27-pre'],
                         [x.path for x in job.pre_run])
        self.assertEqual(['py27-run'],
                         [x.path for x in job.run])
        self.assertEqual(['py27-post'],
                         [x.path for x in job.post_run])
        self.assertEqual(secrets, job.pre_run[0].secrets)
        self.assertEqual(secrets, job.run[0].secrets)
        self.assertEqual(secrets, job.post_run[0].secrets)

        # Set the job to final for the following checks
        job.final = True
        self.assertTrue(job.voting)

        good_final = model.Job('py27')
        good_final.voting = False
        job.applyVariant(good_final, self.layout, None)
        self.assertFalse(job.voting)

        bad_final = model.Job('py27')
        bad_final.timeout = 600
        with testtools.ExpectedException(
                Exception,
                "Unable to modify final job"):
            job.applyVariant(bad_final, self.layout, None)

    @mock.patch("zuul.model.zkobject.ZKObject._save")
    def test_job_inheritance_job_tree(self, save_mock):
        base = self.pcontext.job_parser.fromYaml({
            '_source_context': self.context,
            '_start_mark': self.start_mark,
            'name': 'base',
            'parent': None,
            'timeout': 30,
        }, None)
        self.layout.addJob(base)
        python27 = self.pcontext.job_parser.fromYaml({
            '_source_context': self.context,
            '_start_mark': self.start_mark,
            'name': 'python27',
            'parent': 'base',
            'timeout': 40,
        }, None)
        self.layout.addJob(python27)
        python27diablo = self.pcontext.job_parser.fromYaml({
            '_source_context': self.context,
            '_start_mark': self.start_mark,
            'name': 'python27',
            'branches': [
                'stable/diablo'
            ],
            'timeout': 50,
        }, None)
        self.layout.addJob(python27diablo)

        project_config = self.pcontext.project_parser.fromYaml({
            '_source_context': self.context,
            '_start_mark': self.start_mark,
            'name': 'project',
            'gate': {
                'jobs': [
                    {'python27': {'timeout': 70,
                                  'run': 'playbooks/python27.yaml'}}
                ]
            }
        })
        self.layout.addProjectConfig(project_config)

        change = model.Change(self.project)
        change.branch = 'master'
        change.cache_stat = Dummy(key=Dummy(reference=uuid.uuid4().hex))
        item = self.queue.enqueueChanges([change], None)

        self.assertTrue(base.changeMatchesBranch(change))
        self.assertTrue(python27.changeMatchesBranch(change))
        self.assertFalse(python27diablo.changeMatchesBranch(change))

        with self.zk_context as ctx:
            item.freezeJobGraph(self.layout, ctx,
                                skip_file_matcher=False,
                                redact_secrets_and_keys=False)
        self.assertEqual(len(item.getJobs()), 1)
        job = item.getJobs()[0]
        self.assertEqual(job.name, 'python27')
        self.assertEqual(job.timeout, 70)

        change.branch = 'stable/diablo'
        change.cache_stat = Dummy(key=Dummy(reference=uuid.uuid4().hex))
        item = self.queue.enqueueChanges([change], None)

        self.assertTrue(base.changeMatchesBranch(change))
        self.assertTrue(python27.changeMatchesBranch(change))
        self.assertTrue(python27diablo.changeMatchesBranch(change))

        with self.zk_context as ctx:
            item.freezeJobGraph(self.layout, ctx,
                                skip_file_matcher=False,
                                redact_secrets_and_keys=False)
        self.assertEqual(len(item.getJobs()), 1)
        job = item.getJobs()[0]
        self.assertEqual(job.name, 'python27')
        self.assertEqual(job.timeout, 70)

    @mock.patch("zuul.model.zkobject.ZKObject._save")
    def test_inheritance_keeps_matchers(self, save_mock):
        base = self.pcontext.job_parser.fromYaml({
            '_source_context': self.context,
            '_start_mark': self.start_mark,
            'name': 'base',
            'parent': None,
            'timeout': 30,
        }, None)
        self.layout.addJob(base)
        python27 = self.pcontext.job_parser.fromYaml({
            '_source_context': self.context,
            '_start_mark': self.start_mark,
            'name': 'python27',
            'parent': 'base',
            'timeout': 40,
            'irrelevant-files': ['^ignored-file$'],
        }, None)
        self.layout.addJob(python27)

        project_config = self.pcontext.project_parser.fromYaml({
            '_source_context': self.context,
            '_start_mark': self.start_mark,
            'name': 'project',
            'gate': {
                'jobs': [
                    'python27',
                ]
            }
        })
        self.layout.addProjectConfig(project_config)

        change = model.Change(self.project)
        change.branch = 'master'
        change.cache_stat = Dummy(key=Dummy(reference=uuid.uuid4().hex))
        change.files = ['/COMMIT_MSG', 'ignored-file']
        item = self.queue.enqueueChanges([change], None)

        self.assertTrue(base.changeMatchesFiles(change))
        self.assertFalse(python27.changeMatchesFiles(change))

        self.pipeline.manager.getFallbackLayout = mock.Mock(return_value=None)
        with self.zk_context as ctx:
            item.freezeJobGraph(self.layout, ctx,
                                skip_file_matcher=False,
                                redact_secrets_and_keys=False)
        self.assertEqual([], item.getJobs())

    def test_job_source_project(self):
        base_project = model.Project('base_project', self.source)
        base_context = model.SourceContext(
            base_project.canonical_name, base_project.name,
            base_project.connection_name, 'master', 'test', True)
        tpc = model.TenantProjectConfig(base_project)
        self.tenant.addUntrustedProject(tpc)

        base = self.pcontext.job_parser.fromYaml({
            '_source_context': base_context,
            '_start_mark': self.start_mark,
            'parent': None,
            'name': 'base',
        }, None)
        self.layout.addJob(base)

        other_project = model.Project('other_project', self.source)
        other_context = model.SourceContext(
            other_project.canonical_name, other_project.name,
            other_project.connection_name, 'master', 'test', True)
        tpc = model.TenantProjectConfig(other_project)
        self.tenant.addUntrustedProject(tpc)
        base2 = self.pcontext.job_parser.fromYaml({
            '_source_context': other_context,
            '_start_mark': self.start_mark,
            'name': 'base',
        }, None)
        with testtools.ExpectedException(
                Exception,
                "Job base in other_project is not permitted "
                "to shadow job base in base_project"):
            self.layout.addJob(base2)

    @mock.patch("zuul.model.zkobject.ZKObject._save")
    def test_job_pipeline_allow_untrusted_secrets(self, save_mock):
        self.pipeline.post_review = False
        job = self.pcontext.job_parser.fromYaml({
            '_source_context': self.context,
            '_start_mark': self.start_mark,
            'name': 'job',
            'parent': None,
            'post-review': True
        }, None)

        self.layout.addJob(job)

        project_config = self.pcontext.project_parser.fromYaml({
            '_source_context': self.context,
            '_start_mark': self.start_mark,
            'name': 'project',
            'gate': {
                'jobs': [
                    'job'
                ]
            }
        })

        self.layout.addProjectConfig(project_config)

        change = model.Change(self.project)
        # Test master
        change.branch = 'master'
        change.cache_stat = Dummy(key=Dummy(reference=uuid.uuid4().hex))
        item = self.queue.enqueueChanges([change], None)
        with testtools.ExpectedException(
                Exception,
                "Pre-review pipeline gate does not allow post-review job"):
            with self.zk_context as ctx:
                item.freezeJobGraph(self.layout, ctx,
                                    skip_file_matcher=False,
                                    redact_secrets_and_keys=False)

    def test_job_deduplicate_secrets(self):
        # Verify that in a job with two secrets with the same name on
        # different playbooks (achieved by inheritance) the secrets
        # are not deduplicated.  Also verify that the same secret used
        # twice is deduplicated.

        secret1_data = {'text': 'secret1 data'}
        secret2_data = {'text': 'secret2 data'}
        secret1 = self.pcontext.secret_parser.fromYaml({
            '_source_context': self.context,
            '_start_mark': self.start_mark,
            'name': 'secret1',
            'data': secret1_data,
        })
        self.layout.addSecret(secret1)

        secret2 = self.pcontext.secret_parser.fromYaml({
            '_source_context': self.context,
            '_start_mark': self.start_mark,
            'name': 'secret2',
            'data': secret2_data,
        })
        self.layout.addSecret(secret2)

        # In the first job, we test deduplication.
        base = self.pcontext.job_parser.fromYaml({
            '_source_context': self.context,
            '_start_mark': self.start_mark,
            'name': 'base',
            'parent': None,
            'secrets': [
                {'name': 'mysecret',
                 'secret': 'secret1'},
                {'name': 'othersecret',
                 'secret': 'secret1'},
            ],
            'pre-run': 'playbooks/pre.yaml',
        }, None)
        self.layout.addJob(base)

        # The second job should have a secret with the same name as
        # the first job, but with a different value, to make sure it
        # is not deduplicated.
        python27 = self.pcontext.job_parser.fromYaml({
            '_source_context': self.context,
            '_start_mark': self.start_mark,
            'name': 'python27',
            'parent': 'base',
            'secrets': [
                {'name': 'mysecret',
                 'secret': 'secret2'},
            ],
            'run': 'playbooks/python27.yaml',
        }, None)
        self.layout.addJob(python27)

        project_config = self.pcontext.project_parser.fromYaml({
            '_source_context': self.context,
            '_start_mark': self.start_mark,
            'name': 'project',
            'gate': {
                'jobs': ['python27'],
            }
        })
        self.layout.addProjectConfig(project_config)

        change = model.Change(self.project)
        change.branch = 'master'
        change.cache_stat = Dummy(key=Dummy(reference=uuid.uuid4().hex))
        item = self.queue.enqueueChanges([change], None)

        self.assertTrue(base.changeMatchesBranch(change))
        self.assertTrue(python27.changeMatchesBranch(change))

        with self.zk_context as ctx:
            item.freezeJobGraph(self.layout, ctx,
                                skip_file_matcher=False,
                                redact_secrets_and_keys=False)
        self.assertEqual(len(item.getJobs()), 1)
        job = item.getJobs()[0]
        self.assertEqual(job.name, 'python27')

        pre_idx = job.pre_run[0]['secrets']['mysecret']
        pre_secret = yaml.encrypted_load(
            job.secrets[pre_idx]['encrypted_data'])
        self.assertEqual(pre_secret, secret1_data)

        # Verify that they were deduplicated
        pre2_idx = job.pre_run[0]['secrets']['othersecret']
        self.assertEqual(pre_idx, pre2_idx)

        # Verify that the second secret is distinct
        run_idx = job.run[0]['secrets']['mysecret']
        run_secret = yaml.encrypted_load(
            job.secrets[run_idx]['encrypted_data'])
        self.assertEqual(run_secret, secret2_data)


class FakeFrozenJob(model.Job):

    def __init__(self, name):
        super().__init__(name)
        self.uuid = uuid.uuid4().hex
        self.ref = 'fake reference'
        self.all_refs = [self.ref]


class TestGraph(BaseTestCase):
    def setUp(self):
        COMPONENT_REGISTRY.registry = Dummy()
        COMPONENT_REGISTRY.registry.model_api = MODEL_API
        super().setUp()

    def test_job_graph_disallows_circular_dependencies(self):
        jobs = [FakeFrozenJob('job%d' % i) for i in range(0, 10)]

        def setup_graph():
            graph = model.JobGraph({})
            prevjob = None
            for j in jobs[:3]:
                if prevjob:
                    j.dependencies = frozenset([
                        model.JobDependency(prevjob.name)])
                graph.addJob(j)
                prevjob = j
            # 0 triggers 1 triggers 2 triggers 3...
            return graph

        # Cannot depend on itself
        graph = setup_graph()
        j = FakeFrozenJob('jobX')
        j.dependencies = frozenset([model.JobDependency(j.name)])
        graph.addJob(j)
        with testtools.ExpectedException(
                Exception,
                "Dependency cycle detected in job jobX"):
            graph.freezeDependencies()

        # Disallow circular dependencies
        graph = setup_graph()
        jobs[3].dependencies = frozenset([model.JobDependency(jobs[4].name)])
        graph.addJob(jobs[3])
        jobs[4].dependencies = frozenset([model.JobDependency(jobs[3].name)])
        graph.addJob(jobs[4])
        with testtools.ExpectedException(
                Exception,
                "Dependency cycle detected in job job3"):
            graph.freezeDependencies()

        graph = setup_graph()
        jobs[3].dependencies = frozenset([model.JobDependency(jobs[5].name)])
        graph.addJob(jobs[3])
        jobs[4].dependencies = frozenset([model.JobDependency(jobs[3].name)])
        graph.addJob(jobs[4])
        jobs[5].dependencies = frozenset([model.JobDependency(jobs[4].name)])
        graph.addJob(jobs[5])

        with testtools.ExpectedException(
                Exception,
                "Dependency cycle detected in job job3"):
            graph.freezeDependencies()

        graph = setup_graph()
        jobs[3].dependencies = frozenset([model.JobDependency(jobs[2].name)])
        graph.addJob(jobs[3])
        jobs[4].dependencies = frozenset([model.JobDependency(jobs[3].name)])
        graph.addJob(jobs[4])
        jobs[5].dependencies = frozenset([model.JobDependency(jobs[4].name)])
        graph.addJob(jobs[5])
        jobs[6].dependencies = frozenset([model.JobDependency(jobs[2].name)])
        graph.addJob(jobs[6])
        graph.freezeDependencies()

    def test_job_graph_allows_soft_dependencies(self):
        parent = FakeFrozenJob('parent')
        child = FakeFrozenJob('child')
        child.dependencies = frozenset([
            model.JobDependency(parent.name, True)])

        # With the parent
        graph = model.JobGraph({})
        graph.addJob(parent)
        graph.addJob(child)
        graph.freezeDependencies()
        self.assertEqual(graph.getParentJobsRecursively(child),
                         [parent])

        # Skip the parent
        graph = model.JobGraph({})
        graph.addJob(child)
        graph.freezeDependencies()
        self.assertEqual(graph.getParentJobsRecursively(child), [])

    def test_job_graph_allows_soft_dependencies4(self):
        # A more complex scenario with multiple parents at each level
        parents = [FakeFrozenJob('parent%i' % i) for i in range(6)]
        child = FakeFrozenJob('child')
        child.dependencies = frozenset([
            model.JobDependency(parents[0].name, True),
            model.JobDependency(parents[1].name)])
        parents[0].dependencies = frozenset([
            model.JobDependency(parents[2].name),
            model.JobDependency(parents[3].name, True)])
        parents[1].dependencies = frozenset([
            model.JobDependency(parents[4].name),
            model.JobDependency(parents[5].name)])
        # Run them all
        graph = model.JobGraph({})
        for j in parents:
            graph.addJob(j)
        graph.addJob(child)
        graph.freezeDependencies()
        self.assertEqual(set(graph.getParentJobsRecursively(child)),
                         set(parents))

        # Skip first parent, therefore its recursive dependencies don't appear
        graph = model.JobGraph({})
        for j in parents:
            if j is not parents[0]:
                graph.addJob(j)
        graph.addJob(child)
        graph.freezeDependencies()
        self.assertEqual(set(graph.getParentJobsRecursively(child)),
                         set(parents) -
                         set([parents[0], parents[2], parents[3]]))

        # Skip a leaf node
        graph = model.JobGraph({})
        for j in parents:
            if j is not parents[3]:
                graph.addJob(j)
        graph.addJob(child)
        graph.freezeDependencies()
        self.assertEqual(set(graph.getParentJobsRecursively(child)),
                         set(parents) - set([parents[3]]))


class TestTenant(BaseTestCase):
    def test_add_project(self):
        tenant = model.Tenant('tenant')
        connection1 = Dummy(connection_name='dummy_connection1')
        source1 = Dummy(canonical_hostname='git1.example.com',
                        name='dummy',  # TODOv3(jeblair): remove
                        connection=connection1)

        source1_project1 = model.Project('project1', source1)
        source1_project1_tpc = model.TenantProjectConfig(source1_project1)
        tenant.addConfigProject(source1_project1_tpc)
        d = {'project1':
             {'git1.example.com': source1_project1}}
        self.assertEqual(d, tenant.projects)
        self.assertEqual((True, source1_project1),
                         tenant.getProject('project1'))
        self.assertEqual((True, source1_project1),
                         tenant.getProject('git1.example.com/project1'))

        source1_project2 = model.Project('project2', source1)
        tpc = model.TenantProjectConfig(source1_project2)
        tenant.addUntrustedProject(tpc)
        d = {'project1':
             {'git1.example.com': source1_project1},
             'project2':
             {'git1.example.com': source1_project2}}
        self.assertEqual(d, tenant.projects)
        self.assertEqual((False, source1_project2),
                         tenant.getProject('project2'))
        self.assertEqual((False, source1_project2),
                         tenant.getProject('git1.example.com/project2'))

        connection2 = Dummy(connection_name='dummy_connection2')
        source2 = Dummy(canonical_hostname='git2.example.com',
                        name='dummy',  # TODOv3(jeblair): remove
                        connection=connection2)

        source2_project1 = model.Project('project1', source2)
        tpc = model.TenantProjectConfig(source2_project1)
        tenant.addUntrustedProject(tpc)
        d = {'project1':
             {'git1.example.com': source1_project1,
              'git2.example.com': source2_project1},
             'project2':
             {'git1.example.com': source1_project2}}
        self.assertEqual(d, tenant.projects)
        with testtools.ExpectedException(
                Exception,
                "Project name 'project1' is ambiguous"):
            tenant.getProject('project1')
        self.assertEqual((False, source1_project2),
                         tenant.getProject('project2'))
        self.assertEqual((True, source1_project1),
                         tenant.getProject('git1.example.com/project1'))
        self.assertEqual((False, source2_project1),
                         tenant.getProject('git2.example.com/project1'))

        source2_project2 = model.Project('project2', source2)
        tpc = model.TenantProjectConfig(source2_project2)
        tenant.addConfigProject(tpc)
        d = {'project1':
             {'git1.example.com': source1_project1,
              'git2.example.com': source2_project1},
             'project2':
             {'git1.example.com': source1_project2,
              'git2.example.com': source2_project2}}
        self.assertEqual(d, tenant.projects)
        with testtools.ExpectedException(
                Exception,
                "Project name 'project1' is ambiguous"):
            tenant.getProject('project1')
        with testtools.ExpectedException(
                Exception,
                "Project name 'project2' is ambiguous"):
            tenant.getProject('project2')
        self.assertEqual((True, source1_project1),
                         tenant.getProject('git1.example.com/project1'))
        self.assertEqual((False, source2_project1),
                         tenant.getProject('git2.example.com/project1'))
        self.assertEqual((False, source1_project2),
                         tenant.getProject('git1.example.com/project2'))
        self.assertEqual((True, source2_project2),
                         tenant.getProject('git2.example.com/project2'))

        source1_project2b = model.Project('subpath/project2', source1)
        tpc = model.TenantProjectConfig(source1_project2b)
        tenant.addConfigProject(tpc)
        d = {'project1':
             {'git1.example.com': source1_project1,
              'git2.example.com': source2_project1},
             'project2':
             {'git1.example.com': source1_project2,
              'git2.example.com': source2_project2},
             'subpath/project2':
             {'git1.example.com': source1_project2b}}
        self.assertEqual(d, tenant.projects)
        self.assertEqual((False, source1_project2),
                         tenant.getProject('git1.example.com/project2'))
        self.assertEqual((True, source2_project2),
                         tenant.getProject('git2.example.com/project2'))
        self.assertEqual((True, source1_project2b),
                         tenant.getProject('subpath/project2'))
        self.assertEqual(
            (True, source1_project2b),
            tenant.getProject('git1.example.com/subpath/project2'))

        source2_project2b = model.Project('subpath/project2', source2)
        tpc = model.TenantProjectConfig(source2_project2b)
        tenant.addConfigProject(tpc)
        d = {'project1':
             {'git1.example.com': source1_project1,
              'git2.example.com': source2_project1},
             'project2':
             {'git1.example.com': source1_project2,
              'git2.example.com': source2_project2},
             'subpath/project2':
             {'git1.example.com': source1_project2b,
              'git2.example.com': source2_project2b}}
        self.assertEqual(d, tenant.projects)
        self.assertEqual((False, source1_project2),
                         tenant.getProject('git1.example.com/project2'))
        self.assertEqual((True, source2_project2),
                         tenant.getProject('git2.example.com/project2'))
        with testtools.ExpectedException(
                Exception,
                "Project name 'subpath/project2' is ambiguous"):
            tenant.getProject('subpath/project2')
        self.assertEqual(
            (True, source1_project2b),
            tenant.getProject('git1.example.com/subpath/project2'))
        self.assertEqual(
            (True, source2_project2b),
            tenant.getProject('git2.example.com/subpath/project2'))

        with testtools.ExpectedException(
                Exception,
                "Project project1 is already in project index"):
            tenant._addProject(source1_project1_tpc)


class TestFreezable(BaseTestCase):
    def test_freezable_object(self):

        o = model.Freezable()
        o.foo = 1
        o.list = []
        o.dict = {}
        o.odict = collections.OrderedDict()
        o.odict2 = collections.OrderedDict()

        o1 = model.Freezable()
        o1.foo = 1
        l1 = [1]
        d1 = {'foo': 1}
        od1 = {'foo': 1}
        o.list.append(o1)
        o.list.append(l1)
        o.list.append(d1)
        o.list.append(od1)

        o2 = model.Freezable()
        o2.foo = 1
        l2 = [1]
        d2 = {'foo': 1}
        od2 = {'foo': 1}
        o.dict['o'] = o2
        o.dict['l'] = l2
        o.dict['d'] = d2
        o.dict['od'] = od2

        o3 = model.Freezable()
        o3.foo = 1
        l3 = [1]
        d3 = {'foo': 1}
        od3 = {'foo': 1}
        o.odict['o'] = o3
        o.odict['l'] = l3
        o.odict['d'] = d3
        o.odict['od'] = od3

        seq = list(range(1000))
        random.shuffle(seq)
        for x in seq:
            o.odict2[x] = x

        o.freeze()

        with testtools.ExpectedException(Exception, "Unable to modify frozen"):
            o.bar = 2
        with testtools.ExpectedException(AttributeError, "'tuple' object"):
            o.list.append(2)
        with testtools.ExpectedException(TypeError, "'mappingproxy' object"):
            o.dict['bar'] = 2
        with testtools.ExpectedException(TypeError, "'mappingproxy' object"):
            o.odict['bar'] = 2

        with testtools.ExpectedException(Exception, "Unable to modify frozen"):
            o1.bar = 2
        with testtools.ExpectedException(Exception, "Unable to modify frozen"):
            o.list[0].bar = 2
        with testtools.ExpectedException(AttributeError, "'tuple' object"):
            o.list[1].append(2)
        with testtools.ExpectedException(TypeError, "'mappingproxy' object"):
            o.list[2]['bar'] = 2
        with testtools.ExpectedException(TypeError, "'mappingproxy' object"):
            o.list[3]['bar'] = 2

        with testtools.ExpectedException(Exception, "Unable to modify frozen"):
            o2.bar = 2
        with testtools.ExpectedException(Exception, "Unable to modify frozen"):
            o.dict['o'].bar = 2
        with testtools.ExpectedException(AttributeError, "'tuple' object"):
            o.dict['l'].append(2)
        with testtools.ExpectedException(TypeError, "'mappingproxy' object"):
            o.dict['d']['bar'] = 2
        with testtools.ExpectedException(TypeError, "'mappingproxy' object"):
            o.dict['od']['bar'] = 2

        with testtools.ExpectedException(Exception, "Unable to modify frozen"):
            o3.bar = 2
        with testtools.ExpectedException(Exception, "Unable to modify frozen"):
            o.odict['o'].bar = 2
        with testtools.ExpectedException(AttributeError, "'tuple' object"):
            o.odict['l'].append(2)
        with testtools.ExpectedException(TypeError, "'mappingproxy' object"):
            o.odict['d']['bar'] = 2
        with testtools.ExpectedException(TypeError, "'mappingproxy' object"):
            o.odict['od']['bar'] = 2

        # Make sure that mapping proxy applied to an ordered dict
        # still shows the ordered behavior.
        self.assertTrue(isinstance(o.odict2, types.MappingProxyType))
        self.assertEqual(list(o.odict2.keys()), seq)


class TestRef(BaseTestCase):
    def test_ref_equality(self):
        change1 = model.Change('project1')
        change1.ref = '/change1'
        change1b = model.Change('project1')
        change1b.ref = '/change1'
        change2 = model.Change('project2')
        change2.ref = '/change2'
        self.assertFalse(change1.equals(change2))
        self.assertTrue(change1.equals(change1b))

        tag1 = model.Tag('project1')
        tag1.ref = '/tag1'
        tag1b = model.Tag('project1')
        tag1b.ref = '/tag1'
        tag2 = model.Tag('project2')
        tag2.ref = '/tag2'
        self.assertFalse(tag1.equals(tag2))
        self.assertTrue(tag1.equals(tag1b))

        self.assertFalse(tag1.equals(change1))

        branch1 = model.Branch('project1')
        branch1.ref = '/branch1'
        branch1b = model.Branch('project1')
        branch1b.ref = '/branch1'
        branch2 = model.Branch('project2')
        branch2.ref = '/branch2'
        self.assertFalse(branch1.equals(branch2))
        self.assertTrue(branch1.equals(branch1b))

        self.assertFalse(branch1.equals(change1))
        self.assertFalse(branch1.equals(tag1))


class TestSourceContext(BaseTestCase):
    def setUp(self):
        super().setUp()
        COMPONENT_REGISTRY.registry = Dummy()
        COMPONENT_REGISTRY.registry.model_api = MODEL_API
        self.connection = Dummy(connection_name='dummy_connection')
        self.source = Dummy(canonical_hostname='git.example.com',
                            connection=self.connection)
        self.project = model.Project('project', self.source)
        self.context = model.SourceContext(
            self.project.canonical_name, self.project.name,
            self.project.connection_name, 'master', 'test', True)
        self.context.implied_branches = [
            change_matcher.BranchMatcher(ZuulRegex('foo')),
            change_matcher.ImpliedBranchMatcher(ZuulRegex('foo')),
        ]

    def test_serialize(self):
        self.context.deserialize(self.context.serialize())
