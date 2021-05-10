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

import base64
import collections
from contextlib import contextmanager
import copy
import itertools
import os
import logging
import textwrap
import io
import re
import subprocess

import voluptuous as vs

from zuul.driver.sql.sqlconnection import SQLConnection
from zuul import model
from zuul.lib import yamlutil as yaml
import zuul.manager.dependent
import zuul.manager.independent
import zuul.manager.supercedent
import zuul.manager.serial
from zuul.lib import encryption
from zuul.lib.logutil import get_annotated_logger
from zuul.lib.re2util import filter_allowed_disallowed
from zuul.zk.semaphore import SemaphoreHandler


# Several forms accept either a single item or a list, this makes
# specifying that in the schema easy (and explicit).
def to_list(x):
    return vs.Any([x], x)


def as_list(item):
    if not item:
        return []
    if isinstance(item, list):
        return item
    return [item]


class ConfigurationSyntaxError(Exception):
    pass


class NodeFromGroupNotFoundError(Exception):
    def __init__(self, nodeset, node, group):
        message = textwrap.dedent("""\
        In nodeset "{nodeset}" the group "{group}" contains a
        node named "{node}" which is not defined in the nodeset.""")
        message = textwrap.fill(message.format(nodeset=nodeset,
                                               node=node, group=group))
        super(NodeFromGroupNotFoundError, self).__init__(message)


class DuplicateNodeError(Exception):
    def __init__(self, nodeset, node):
        message = textwrap.dedent("""\
        In nodeset "{nodeset}" the node "{node}" appears multiple times.
        Node names must be unique within a nodeset.""")
        message = textwrap.fill(message.format(nodeset=nodeset,
                                               node=node))
        super(DuplicateNodeError, self).__init__(message)


class UnknownConnection(Exception):
    def __init__(self, connection_name):
        message = textwrap.dedent("""\
        Unknown connection named "{connection}".""")
        message = textwrap.fill(message.format(connection=connection_name))
        super(UnknownConnection, self).__init__(message)


class LabelForbiddenError(Exception):
    def __init__(self, label, allowed_labels, disallowed_labels):
        message = textwrap.dedent("""\
        Label named "{label}" is not part of the allowed
        labels ({allowed_labels}) for this tenant.""")
        # Make a string that looks like "a, b and not c, d" if we have
        # both allowed and disallowed labels.
        labels = ", ".join(allowed_labels or [])
        if allowed_labels and disallowed_labels:
            labels += ' and '
        if disallowed_labels:
            labels += 'not '
            labels += ", ".join(disallowed_labels)
        message = textwrap.fill(message.format(
            label=label,
            allowed_labels=labels))
        super(LabelForbiddenError, self).__init__(message)


class MaxTimeoutError(Exception):
    def __init__(self, job, tenant):
        message = textwrap.dedent("""\
        The job "{job}" exceeds tenant max-job-timeout {maxtimeout}.""")
        message = textwrap.fill(message.format(
            job=job.name, maxtimeout=tenant.max_job_timeout))
        super(MaxTimeoutError, self).__init__(message)


class DuplicateGroupError(Exception):
    def __init__(self, nodeset, group):
        message = textwrap.dedent("""\
        In nodeset "{nodeset}" the group "{group}" appears multiple times.
        Group names must be unique within a nodeset.""")
        message = textwrap.fill(message.format(nodeset=nodeset,
                                               group=group))
        super(DuplicateGroupError, self).__init__(message)


class ProjectNotFoundError(Exception):
    def __init__(self, project):
        message = textwrap.dedent("""\
        The project "{project}" was not found.  All projects
        referenced within a Zuul configuration must first be
        added to the main configuration file by the Zuul
        administrator.""")
        message = textwrap.fill(message.format(project=project))
        super(ProjectNotFoundError, self).__init__(message)


class TemplateNotFoundError(Exception):
    def __init__(self, template):
        message = textwrap.dedent("""\
        The project template "{template}" was not found.
        """)
        message = textwrap.fill(message.format(template=template))
        super(TemplateNotFoundError, self).__init__(message)


class NodesetNotFoundError(Exception):
    def __init__(self, nodeset):
        message = textwrap.dedent("""\
        The nodeset "{nodeset}" was not found.
        """)
        message = textwrap.fill(message.format(nodeset=nodeset))
        super(NodesetNotFoundError, self).__init__(message)


class PipelineNotPermittedError(Exception):
    def __init__(self):
        message = textwrap.dedent("""\
        Pipelines may not be defined in untrusted repos,
        they may only be defined in config repos.""")
        message = textwrap.fill(message)
        super(PipelineNotPermittedError, self).__init__(message)


class ProjectNotPermittedError(Exception):
    def __init__(self):
        message = textwrap.dedent("""\
        Within an untrusted project, the only project definition
        permitted is that of the project itself.""")
        message = textwrap.fill(message)
        super(ProjectNotPermittedError, self).__init__(message)


class YAMLDuplicateKeyError(ConfigurationSyntaxError):
    def __init__(self, key, node, context, start_mark):
        intro = textwrap.fill(textwrap.dedent("""\
        Zuul encountered a syntax error while parsing its configuration in the
        repo {repo} on branch {branch}.  The error was:""".format(
            repo=context.project.name,
            branch=context.branch,
        )))

        e = textwrap.fill(textwrap.dedent("""\
        The key "{key}" appears more than once; duplicate keys are not
        permitted.
        """.format(
            key=key,
        )))

        m = textwrap.dedent("""\
        {intro}

        {error}

        The error appears in the following stanza:

        {content}

        {start_mark}""")

        m = m.format(intro=intro,
                     error=indent(str(e)),
                     content=indent(start_mark.snippet.rstrip()),
                     start_mark=str(start_mark))
        super(YAMLDuplicateKeyError, self).__init__(m)


def indent(s):
    return '\n'.join(['  ' + x for x in s.split('\n')])


@contextmanager
def project_configuration_exceptions(context, accumulator):
    try:
        yield
    except ConfigurationSyntaxError:
        raise
    except Exception as e:
        intro = textwrap.fill(textwrap.dedent("""\
        Zuul encountered an error while accessing the repo {repo}.  The error
        was:""".format(
            repo=context.project.name,
        )))

        m = textwrap.dedent("""\
        {intro}

        {error}""")

        m = m.format(intro=intro,
                     error=indent(str(e)))
        accumulator.addError(context, None, m)


@contextmanager
def early_configuration_exceptions(context):
    try:
        yield
    except ConfigurationSyntaxError:
        raise
    except Exception as e:
        intro = textwrap.fill(textwrap.dedent("""\
        Zuul encountered a syntax error while parsing its configuration in the
        repo {repo} on branch {branch}.  The error was:""".format(
            repo=context.project.name,
            branch=context.branch,
        )))

        m = textwrap.dedent("""\
        {intro}

        {error}""")

        m = m.format(intro=intro,
                     error=indent(str(e)))
        raise ConfigurationSyntaxError(m)


@contextmanager
def configuration_exceptions(stanza, conf, accumulator):
    try:
        yield
    except ConfigurationSyntaxError:
        raise
    except Exception as e:
        conf = copy.deepcopy(conf)
        context = conf.pop('_source_context')
        start_mark = conf.pop('_start_mark')
        intro = textwrap.fill(textwrap.dedent("""\
        Zuul encountered a syntax error while parsing its configuration in the
        repo {repo} on branch {branch}.  The error was:""".format(
            repo=context.project.name,
            branch=context.branch,
        )))

        m = textwrap.dedent("""\
        {intro}

        {error}

        The error appears in the following {stanza} stanza:

        {content}

        {start_mark}""")

        m = m.format(intro=intro,
                     error=indent(str(e)),
                     stanza=stanza,
                     content=indent(start_mark.snippet.rstrip()),
                     start_mark=str(start_mark))

        accumulator.addError(context, start_mark, m, str(e))


@contextmanager
def reference_exceptions(stanza, obj, accumulator):
    try:
        yield
    except ConfigurationSyntaxError:
        raise
    except Exception as e:
        context = obj.source_context
        start_mark = obj.start_mark
        intro = textwrap.fill(textwrap.dedent("""\
        Zuul encountered a syntax error while parsing its configuration in the
        repo {repo} on branch {branch}.  The error was:""".format(
            repo=context.project.name,
            branch=context.branch,
        )))

        m = textwrap.dedent("""\
        {intro}

        {error}

        The error appears in the following {stanza} stanza:

        {content}

        {start_mark}""")

        m = m.format(intro=intro,
                     error=indent(str(e)),
                     stanza=stanza,
                     content=indent(start_mark.snippet.rstrip()),
                     start_mark=str(start_mark))

        accumulator.addError(context, start_mark, m, str(e))


class ZuulMark(object):
    # The yaml mark class differs between the C and python versions.
    # The C version does not provide a snippet, and also appears to
    # lose data under some circumstances.
    def __init__(self, start_mark, end_mark, stream):
        self.name = start_mark.name
        self.index = start_mark.index
        self.line = start_mark.line
        self.end_line = end_mark.line
        self.end_index = end_mark.index
        self.column = start_mark.column
        self.end_column = end_mark.column
        self.snippet = stream[start_mark.index:end_mark.index]

    def __str__(self):
        return '  in "{name}", line {line}, column {column}'.format(
            name=self.name,
            line=self.line + 1,
            column=self.column + 1,
        )

    def __eq__(self, other):
        return (self.line == other.line and
                self.snippet == other.snippet)


class ZuulSafeLoader(yaml.SafeLoader):
    zuul_node_types = frozenset(('job', 'nodeset', 'secret', 'pipeline',
                                 'project', 'project-template',
                                 'semaphore', 'queue', 'pragma'))

    def __init__(self, stream, context):
        wrapped_stream = io.StringIO(stream)
        wrapped_stream.name = str(context)
        super(ZuulSafeLoader, self).__init__(wrapped_stream)
        self.name = str(context)
        self.zuul_context = context
        self.zuul_stream = stream

    def construct_mapping(self, node, deep=False):
        keys = set()
        for k, v in node.value:
            # The key << needs to be treated special since that will merge
            # the anchor into the mapping and not create a key on its own.
            if k.value == '<<':
                continue

            if k.value in keys:
                mark = ZuulMark(node.start_mark, node.end_mark,
                                self.zuul_stream)
                raise YAMLDuplicateKeyError(k.value, node, self.zuul_context,
                                            mark)
            keys.add(k.value)
        r = super(ZuulSafeLoader, self).construct_mapping(node, deep)
        keys = frozenset(r.keys())
        if len(keys) == 1 and keys.intersection(self.zuul_node_types):
            d = list(r.values())[0]
            if isinstance(d, dict):
                d['_start_mark'] = ZuulMark(node.start_mark, node.end_mark,
                                            self.zuul_stream)
                d['_source_context'] = self.zuul_context
        return r


def safe_load_yaml(stream, context):
    loader = ZuulSafeLoader(stream, context)
    try:
        return loader.get_single_data()
    except yaml.YAMLError as e:
        m = """
Zuul encountered a syntax error while parsing its configuration in the
repo {repo} on branch {branch}.  The error was:

  {error}
"""
        m = m.format(repo=context.project.name,
                     branch=context.branch,
                     error=str(e))
        raise ConfigurationSyntaxError(m)
    finally:
        loader.dispose()


class EncryptedPKCS1_OAEP(yaml.YAMLObject):
    yaml_tag = u'!encrypted/pkcs1-oaep'
    yaml_loader = yaml.SafeLoader
    yaml_dumper = yaml.SafeDumper

    def __init__(self, ciphertext):
        if isinstance(ciphertext, list):
            self.ciphertext = [base64.b64decode(x.value)
                               for x in ciphertext]
        else:
            self.ciphertext = base64.b64decode(ciphertext)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __eq__(self, other):
        if not isinstance(other, EncryptedPKCS1_OAEP):
            return False
        return (self.ciphertext == other.ciphertext)

    @classmethod
    def from_yaml(cls, loader, node):
        return cls(node.value)

    @classmethod
    def to_yaml(cls, dumper, data):
        ciphertext = data.ciphertext
        if isinstance(ciphertext, list):
            ciphertext = [yaml.ScalarNode(tag='tag:yaml.org,2002:str',
                                          value=base64.b64encode(x))
                          for x in ciphertext]
            return yaml.SequenceNode(tag=cls.yaml_tag,
                                     value=ciphertext)
        ciphertext = base64.b64encode(ciphertext)
        return yaml.ScalarNode(tag=cls.yaml_tag, value=ciphertext)

    def decrypt(self, private_key):
        if isinstance(self.ciphertext, list):
            return ''.join([
                encryption.decrypt_pkcs1_oaep(chunk, private_key).
                decode('utf8')
                for chunk in self.ciphertext])
        else:
            return encryption.decrypt_pkcs1_oaep(self.ciphertext,
                                                 private_key).decode('utf8')


def ansible_var_name(value):
    vs.Schema(str)(value)
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]*", value):
        raise vs.Invalid("Invalid Ansible variable name '{}'".format(value))


def ansible_vars_dict(value):
    vs.Schema(dict)(value)
    for key in value:
        ansible_var_name(key)


class PragmaParser(object):
    pragma = {
        'implied-branch-matchers': bool,
        'implied-branches': to_list(str),
        '_source_context': model.SourceContext,
        '_start_mark': ZuulMark,
    }

    schema = vs.Schema(pragma)

    def __init__(self, pcontext):
        self.log = logging.getLogger("zuul.PragmaParser")
        self.pcontext = pcontext

    def fromYaml(self, conf):
        self.schema(conf)

        bm = conf.get('implied-branch-matchers')

        source_context = conf['_source_context']
        if bm is not None:
            source_context.implied_branch_matchers = bm

        branches = conf.get('implied-branches')
        if branches is not None:
            source_context.implied_branches = as_list(branches)


class NodeSetParser(object):
    def __init__(self, pcontext):
        self.log = logging.getLogger("zuul.NodeSetParser")
        self.pcontext = pcontext
        self.schema = self.getSchema(False)
        self.anon_schema = self.getSchema(True)

    def getSchema(self, anonymous=False):
        node = {vs.Required('name'): to_list(str),
                vs.Required('label'): str,
                }

        group = {vs.Required('name'): str,
                 vs.Required('nodes'): to_list(str),
                 }

        nodeset = {vs.Required('nodes'): to_list(node),
                   'groups': to_list(group),
                   '_source_context': model.SourceContext,
                   '_start_mark': ZuulMark,
                   }

        if not anonymous:
            nodeset[vs.Required('name')] = str
        return vs.Schema(nodeset)

    def fromYaml(self, conf, anonymous=False):
        if anonymous:
            self.anon_schema(conf)
        else:
            self.schema(conf)
        ns = model.NodeSet(conf.get('name'))
        ns.source_context = conf.get('_source_context')
        ns.start_mark = conf.get('_start_mark')
        node_names = set()
        group_names = set()
        allowed_labels = self.pcontext.tenant.allowed_labels
        disallowed_labels = self.pcontext.tenant.disallowed_labels

        requested_labels = [n['label'] for n in as_list(conf['nodes'])]
        filtered_labels = filter_allowed_disallowed(
            requested_labels, allowed_labels, disallowed_labels)
        rejected_labels = set(requested_labels) - set(filtered_labels)
        for name in rejected_labels:
            raise LabelForbiddenError(
                label=name,
                allowed_labels=allowed_labels,
                disallowed_labels=disallowed_labels)
        for conf_node in as_list(conf['nodes']):
            for name in as_list(conf_node['name']):
                if name in node_names:
                    raise DuplicateNodeError(name, conf_node['name'])
            node = model.Node(as_list(conf_node['name']), conf_node['label'])
            ns.addNode(node)
            for name in as_list(conf_node['name']):
                node_names.add(name)
        for conf_group in as_list(conf.get('groups', [])):
            for node_name in as_list(conf_group['nodes']):
                if node_name not in node_names:
                    raise NodeFromGroupNotFoundError(conf['name'], node_name,
                                                     conf_group['name'])
            if conf_group['name'] in group_names:
                raise DuplicateGroupError(conf['name'], conf_group['name'])
            group = model.Group(conf_group['name'],
                                as_list(conf_group['nodes']))
            ns.addGroup(group)
            group_names.add(conf_group['name'])
        ns.freeze()
        return ns


class SecretParser(object):
    def __init__(self, pcontext):
        self.log = logging.getLogger("zuul.SecretParser")
        self.pcontext = pcontext
        self.schema = self.getSchema()

    def getSchema(self):
        secret = {vs.Required('name'): str,
                  vs.Required('data'): dict,
                  '_source_context': model.SourceContext,
                  '_start_mark': ZuulMark,
                  }

        return vs.Schema(secret)

    def fromYaml(self, conf):
        self.schema(conf)
        s = model.Secret(conf['name'], conf['_source_context'])
        s.source_context = conf['_source_context']
        s.start_mark = conf['_start_mark']
        s.secret_data = conf['data']
        s.freeze()
        return s


class JobParser(object):
    ANSIBLE_ROLE_RE = re.compile(r'^(ansible[-_.+]*)*(role[-_.+]*)*')

    zuul_role = {vs.Required('zuul'): str,
                 'name': str}

    galaxy_role = {vs.Required('galaxy'): str,
                   'name': str}

    role = vs.Any(zuul_role, galaxy_role)

    job_project = {vs.Required('name'): str,
                   'override-branch': str,
                   'override-checkout': str}

    job_dependency = {vs.Required('name'): str,
                      'soft': bool}

    secret = {vs.Required('name'): ansible_var_name,
              vs.Required('secret'): str,
              'pass-to-parent': bool}

    semaphore = {vs.Required('name'): str,
                 'resources-first': bool}

    # Attributes of a job that can also be used in Project and ProjectTemplate
    job_attributes = {'parent': vs.Any(str, None),
                      'final': bool,
                      'abstract': bool,
                      'protected': bool,
                      'intermediate': bool,
                      'requires': to_list(str),
                      'provides': to_list(str),
                      'failure-message': str,
                      'success-message': str,
                      'failure-url': str,
                      'success-url': str,
                      'hold-following-changes': bool,
                      'voting': bool,
                      'semaphore': vs.Any(semaphore, str),
                      'tags': to_list(str),
                      'branches': to_list(str),
                      'files': to_list(str),
                      'secrets': to_list(vs.Any(secret, str)),
                      'irrelevant-files': to_list(str),
                      # validation happens in NodeSetParser
                      'nodeset': vs.Any(dict, str),
                      'timeout': int,
                      'post-timeout': int,
                      'attempts': int,
                      'pre-run': to_list(str),
                      'post-run': to_list(str),
                      'run': to_list(str),
                      'cleanup-run': to_list(str),
                      'ansible-version': vs.Any(str, float),
                      '_source_context': model.SourceContext,
                      '_start_mark': ZuulMark,
                      'roles': to_list(role),
                      'required-projects': to_list(vs.Any(job_project, str)),
                      'vars': ansible_vars_dict,
                      'extra-vars': ansible_vars_dict,
                      'host-vars': {str: ansible_vars_dict},
                      'group-vars': {str: ansible_vars_dict},
                      'dependencies': to_list(vs.Any(job_dependency, str)),
                      'allowed-projects': to_list(str),
                      'override-branch': str,
                      'override-checkout': str,
                      'description': str,
                      'variant-description': str,
                      'post-review': bool,
                      'match-on-config-updates': bool,
                      'workspace-scheme': vs.Any('golang', 'flat'),
    }

    job_name = {vs.Required('name'): str}

    job = dict(collections.ChainMap(job_name, job_attributes))

    schema = vs.Schema(job)

    simple_attributes = [
        'final',
        'abstract',
        'protected',
        'intermediate',
        'timeout',
        'post-timeout',
        'workspace',
        'voting',
        'hold-following-changes',
        'attempts',
        'failure-message',
        'success-message',
        'failure-url',
        'success-url',
        'override-branch',
        'override-checkout',
        'match-on-config-updates',
        'workspace-scheme',
    ]

    def __init__(self, pcontext):
        self.log = logging.getLogger("zuul.JobParser")
        self.pcontext = pcontext

    def fromYaml(self, conf, project_pipeline=False, name=None,
                 validate=True):
        if validate:
            self.schema(conf)

        if name is None:
            name = conf['name']

        # NB: The default detection system in the Job class requires
        # that we always assign values directly rather than modifying
        # them (e.g., "job.run = ..." rather than
        # "job.run.append(...)").

        job = model.Job(name)
        job.description = conf.get('description')
        job.source_context = conf['_source_context']
        job.start_mark = conf['_start_mark']
        job.variant_description = conf.get(
            'variant-description', " ".join(as_list(conf.get('branches'))))

        if project_pipeline and conf['_source_context'].trusted:
            # A config project has attached this job to a
            # project-pipeline.  In this case, we can ignore
            # allowed-projects -- the superuser has stated they want
            # it to run.  This can be useful to allow untrusted jobs
            # with secrets to be run in other untrusted projects.
            job.ignore_allowed_projects = True

        if 'parent' in conf:
            if conf['parent'] is not None:
                # Parent job is explicitly specified, so inherit from it.
                job.parent = conf['parent']
            else:
                # Parent is explicitly set as None, so user intends
                # this to be a base job.  That's only okay if we're in
                # a config project.
                if not conf['_source_context'].trusted:
                    raise Exception(
                        "Base jobs must be defined in config projects")
                job.parent = job.BASE_JOB_MARKER

        # Secrets are part of the playbook context so we must establish
        # them earlier than playbooks.
        secrets = []
        for secret_config in as_list(conf.get('secrets', [])):
            if isinstance(secret_config, str):
                secret_name = secret_config
                secret_alias = secret_config
                secret_ptp = False
            else:
                secret_name = secret_config['secret']
                secret_alias = secret_config['name']
                secret_ptp = secret_config.get('pass-to-parent', False)
            secret_use = model.SecretUse(secret_name, secret_alias)
            secret_use.pass_to_parent = secret_ptp
            secrets.append(secret_use)
        job.secrets = tuple(secrets)

        # A job in an untrusted repo that uses secrets requires
        # special care.  We must note this, and carry this flag
        # through inheritance to ensure that we don't run this job in
        # an unsafe check pipeline.  We must also set allowed-projects
        # to only the current project, as otherwise, other projects
        # might be able to cause something to happen with the secret
        # by using a depends-on header.
        if secrets and not conf['_source_context'].trusted:
            job.post_review = True
            job.allowed_projects = frozenset((
                conf['_source_context'].project.name,))

        if (conf.get('timeout') and
            self.pcontext.tenant.max_job_timeout != -1 and
            int(conf['timeout']) > self.pcontext.tenant.max_job_timeout):
            raise MaxTimeoutError(job, self.pcontext.tenant)

        if (conf.get('post-timeout') and
            self.pcontext.tenant.max_job_timeout != -1 and
            int(conf['post-timeout']) > self.pcontext.tenant.max_job_timeout):
            raise MaxTimeoutError(job, self.pcontext.tenant)

        if 'post-review' in conf:
            if conf['post-review']:
                job.post_review = True
            else:
                raise Exception("Once set, the post-review attribute "
                                "may not be unset")

        # Configure and validate ansible version
        if 'ansible-version' in conf:
            # The ansible-version can be treated by yaml as a float so convert
            # it to a string.
            ansible_version = str(conf['ansible-version'])
            self.pcontext.ansible_manager.requestVersion(ansible_version)
            job.ansible_version = ansible_version

        # Roles are part of the playbook context so we must establish
        # them earlier than playbooks.
        roles = []
        if 'roles' in conf:
            for role in conf.get('roles', []):
                if 'zuul' in role:
                    r = self._makeZuulRole(job, role)
                    if r:
                        roles.append(r)
        # A job's repo should be an implicit role source for that job,
        # but not in a project-pipeline variant.
        if not project_pipeline:
            r = self._makeImplicitRole(job)
            roles.insert(0, r)
        job.addRoles(roles)

        for pre_run_name in as_list(conf.get('pre-run')):
            pre_run = model.PlaybookContext(job.source_context,
                                            pre_run_name, job.roles,
                                            secrets)
            job.pre_run = job.pre_run + (pre_run,)
        # NOTE(pabelanger): Reverse the order of our post-run list. We prepend
        # post-runs for inherits however, we want to execute post-runs in the
        # order they are listed within the job.
        for post_run_name in reversed(as_list(conf.get('post-run'))):
            post_run = model.PlaybookContext(job.source_context,
                                             post_run_name, job.roles,
                                             secrets)
            job.post_run = (post_run,) + job.post_run
        for cleanup_run_name in reversed(as_list(conf.get('cleanup-run'))):
            cleanup_run = model.PlaybookContext(job.source_context,
                                                cleanup_run_name, job.roles,
                                                secrets)
            job.cleanup_run = (cleanup_run,) + job.cleanup_run

        if 'run' in conf:
            for run_name in as_list(conf.get('run')):
                run = model.PlaybookContext(job.source_context, run_name,
                                            job.roles, secrets)
                job.run = job.run + (run,)

        if conf.get('intermediate', False) and not conf.get('abstract', False):
            raise Exception("An intermediate job must also be abstract")

        for k in self.simple_attributes:
            a = k.replace('-', '_')
            if k in conf:
                setattr(job, a, conf[k])
        if 'nodeset' in conf:
            conf_nodeset = conf['nodeset']
            if isinstance(conf_nodeset, str):
                # This references an existing named nodeset in the
                # layout; it will be validated later.
                ns = conf_nodeset
            else:
                ns = self.pcontext.nodeset_parser.fromYaml(
                    conf_nodeset, anonymous=True)
            job.nodeset = ns

        if 'required-projects' in conf:
            new_projects = {}
            projects = as_list(conf.get('required-projects', []))
            unknown_projects = []
            for project in projects:
                if isinstance(project, dict):
                    project_name = project['name']
                    project_override_branch = project.get('override-branch')
                    project_override_checkout = project.get(
                        'override-checkout')
                else:
                    project_name = project
                    project_override_branch = None
                    project_override_checkout = None
                (trusted, project) = self.pcontext.tenant.getProject(
                    project_name)
                if project is None:
                    unknown_projects.append(project_name)
                    continue
                job_project = model.JobProject(project.canonical_name,
                                               project_override_branch,
                                               project_override_checkout)
                new_projects[project.canonical_name] = job_project

            # NOTE(mnaser): We accumulate all unknown projects and throw an
            #               exception only once to capture all of them in the
            #               error message.
            if unknown_projects:
                names = ", ".join(unknown_projects)
                raise Exception("Unknown projects: %s" % (names,))

            job.required_projects = new_projects

        if 'dependencies' in conf:
            new_dependencies = []
            dependencies = as_list(conf.get('dependencies', []))
            for dep in dependencies:
                if isinstance(dep, dict):
                    dep_name = dep['name']
                    dep_soft = dep.get('soft', False)
                else:
                    dep_name = dep
                    dep_soft = False
                job_dependency = model.JobDependency(dep_name, dep_soft)
                new_dependencies.append(job_dependency)
            job.dependencies = new_dependencies

        if 'semaphore' in conf:
            semaphore = conf.get('semaphore')
            if isinstance(semaphore, str):
                job.semaphore = model.JobSemaphore(semaphore)
            else:
                job.semaphore = model.JobSemaphore(
                    semaphore.get('name'),
                    semaphore.get('resources-first', False))

        for k in ('tags', 'requires', 'provides'):
            v = frozenset(as_list(conf.get(k)))
            if v:
                setattr(job, k, v)

        variables = conf.get('vars', None)
        if variables:
            if 'zuul' in variables or 'nodepool' in variables:
                raise Exception("Variables named 'zuul' or 'nodepool' "
                                "are not allowed.")
            job.variables = variables
        extra_variables = conf.get('extra-vars', None)
        if extra_variables:
            if 'zuul' in extra_variables or 'nodepool' in extra_variables:
                raise Exception("Variables named 'zuul' or 'nodepool' "
                                "are not allowed.")
            job.extra_variables = extra_variables
        host_variables = conf.get('host-vars', None)
        if host_variables:
            for host, hvars in host_variables.items():
                if 'zuul' in hvars or 'nodepool' in hvars:
                    raise Exception("Variables named 'zuul' or 'nodepool' "
                                    "are not allowed.")
            job.host_variables = host_variables
        group_variables = conf.get('group-vars', None)
        if group_variables:
            for group, gvars in group_variables.items():
                if 'zuul' in group_variables or 'nodepool' in gvars:
                    raise Exception("Variables named 'zuul' or 'nodepool' "
                                    "are not allowed.")
            job.group_variables = group_variables

        allowed_projects = conf.get('allowed-projects', None)
        # See note above at "post-review".
        if allowed_projects and not job.allowed_projects:
            allowed = []
            for p in as_list(allowed_projects):
                (trusted, project) = self.pcontext.tenant.getProject(p)
                if project is None:
                    raise Exception("Unknown project %s" % (p,))
                allowed.append(project.name)
            job.allowed_projects = frozenset(allowed)

        branches = None
        implied = False
        if 'branches' in conf:
            branches = as_list(conf['branches'])
        elif not project_pipeline:
            branches = self.pcontext.getImpliedBranches(job.source_context)
            implied = True
        if branches:
            job.setBranchMatcher(branches, implied=implied)
        if 'files' in conf:
            job.setFileMatcher(as_list(conf['files']))
        if 'irrelevant-files' in conf:
            job.setIrrelevantFileMatcher(as_list(conf['irrelevant-files']))
        job.freeze()
        return job

    def _makeZuulRole(self, job, role):
        name = role['zuul'].split('/')[-1]

        (trusted, project) = self.pcontext.tenant.getProject(role['zuul'])
        if project is None:
            return None

        return model.ZuulRole(role.get('name', name),
                              project.canonical_name)

    def _makeImplicitRole(self, job):
        project = job.source_context.project
        name = project.name.split('/')[-1]
        name = JobParser.ANSIBLE_ROLE_RE.sub('', name) or name
        return model.ZuulRole(name,
                              project.canonical_name,
                              implicit=True)


class ProjectTemplateParser(object):
    def __init__(self, pcontext):
        self.log = logging.getLogger("zuul.ProjectTemplateParser")
        self.pcontext = pcontext
        self.schema = self.getSchema()
        self.not_pipelines = ['name', 'description', 'templates',
                              'merge-mode', 'default-branch', 'vars',
                              'queue', '_source_context', '_start_mark']

    def getSchema(self):
        job = {str: vs.Any(str, JobParser.job_attributes)}
        job_list = [vs.Any(str, job)]

        pipeline_contents = {
            # TODO(tobiash): Remove pipeline specific queue after deprecation
            'queue': str,
            'debug': bool,
            'fail-fast': bool,
            'jobs': job_list
        }

        project = {
            'name': str,
            'description': str,
            'queue': str,
            'vars': ansible_vars_dict,
            str: pipeline_contents,
            '_source_context': model.SourceContext,
            '_start_mark': ZuulMark,
        }

        return vs.Schema(project)

    def fromYaml(self, conf, validate=True, freeze=True):
        if validate:
            self.schema(conf)
        source_context = conf['_source_context']
        start_mark = conf['_start_mark']
        project_template = model.ProjectConfig(conf.get('name'))
        project_template.source_context = conf['_source_context']
        project_template.start_mark = conf['_start_mark']
        project_template.queue_name = conf.get('queue')
        for pipeline_name, conf_pipeline in conf.items():
            if pipeline_name in self.not_pipelines:
                continue
            project_pipeline = model.ProjectPipelineConfig()
            project_template.pipelines[pipeline_name] = project_pipeline
            # TODO(tobiash): Remove pipeline specific queue after deprecation
            project_pipeline.queue_name = conf_pipeline.get('queue')
            project_pipeline.debug = conf_pipeline.get('debug')
            project_pipeline.fail_fast = conf_pipeline.get(
                'fail-fast')
            self.parseJobList(
                conf_pipeline.get('jobs', []),
                source_context, start_mark, project_pipeline.job_list)

        # If this project definition is in a place where it
        # should get implied branch matchers, set it.
        branches = self.pcontext.getImpliedBranches(source_context)
        if branches:
            project_template.setImpliedBranchMatchers(branches)

        variables = conf.get('vars', {})
        if variables:
            if 'zuul' in variables or 'nodepool' in variables:
                raise Exception("Variables named 'zuul' or 'nodepool' "
                                "are not allowed.")
            project_template.variables = variables

        if freeze:
            project_template.freeze()
        return project_template

    def parseJobList(self, conf, source_context, start_mark, job_list):
        for conf_job in conf:
            if isinstance(conf_job, str):
                jobname = conf_job
                attrs = {}
            elif isinstance(conf_job, dict):
                # A dictionary in a job tree may override params
                jobname, attrs = list(conf_job.items())[0]
            else:
                raise Exception("Job must be a string or dictionary")
            attrs['_source_context'] = source_context
            attrs['_start_mark'] = start_mark

            job_list.addJob(self.pcontext.job_parser.fromYaml(
                attrs, project_pipeline=True,
                name=jobname, validate=False))


class ProjectParser(object):
    def __init__(self, pcontext):
        self.log = logging.getLogger("zuul.ProjectParser")
        self.pcontext = pcontext
        self.schema = self.getSchema()

    def getSchema(self):
        job = {str: vs.Any(str, JobParser.job_attributes)}
        job_list = [vs.Any(str, job)]

        pipeline_contents = {
            # TODO(tobiash): Remove pipeline specific queue after deprecation
            'queue': str,
            'debug': bool,
            'fail-fast': bool,
            'jobs': job_list
        }

        project = {
            'name': str,
            'description': str,
            'vars': ansible_vars_dict,
            'templates': [str],
            'merge-mode': vs.Any('merge', 'merge-resolve',
                                 'cherry-pick', 'squash-merge'),
            'default-branch': str,
            'queue': str,
            str: pipeline_contents,
            '_source_context': model.SourceContext,
            '_start_mark': ZuulMark,
        }

        return vs.Schema(project)

    def fromYaml(self, conf):
        self.schema(conf)

        project_name = conf.get('name')
        if not project_name:
            # There is no name defined so implicitly add the name
            # of the project where it is defined.
            project_name = (conf['_source_context'].project.canonical_name)

        if project_name.startswith('^'):
            # regex matching is designed to match other projects so disallow
            # in untrusted contexts
            if not conf['_source_context'].trusted:
                raise ProjectNotPermittedError()

            # Parse the project as a template since they're mostly the
            # same.
            project_config = self.pcontext.project_template_parser. \
                fromYaml(conf, validate=False, freeze=False)

            project_config.name = project_name
        else:
            (trusted, project) = self.pcontext.tenant.getProject(project_name)
            if project is None:
                raise ProjectNotFoundError(project_name)

            if not conf['_source_context'].trusted:
                if project != conf['_source_context'].project:
                    raise ProjectNotPermittedError()

            # Parse the project as a template since they're mostly the
            # same.
            project_config = self.pcontext.project_template_parser.\
                fromYaml(conf, validate=False, freeze=False)

            project_config.name = project.canonical_name

            # Pragmas can cause templates to end up with implied
            # branch matchers for arbitrary branches, but project
            # stanzas should not.  They should either have the current
            # branch or no branch matcher.
            if conf['_source_context'].trusted:
                project_config.setImpliedBranchMatchers([])
            else:
                project_config.setImpliedBranchMatchers(
                    [conf['_source_context'].branch])

        # Add templates
        for name in conf.get('templates', []):
            if name not in project_config.templates:
                project_config.templates.append(name)

        mode = conf.get('merge-mode', 'merge-resolve')
        project_config.merge_mode = model.MERGER_MAP[mode]

        default_branch = conf.get('default-branch', 'master')
        project_config.default_branch = default_branch

        project_config.queue_name = conf.get('queue', None)

        variables = conf.get('vars', {})
        if variables:
            if 'zuul' in variables or 'nodepool' in variables:
                raise Exception("Variables named 'zuul' or 'nodepool' "
                                "are not allowed.")
            project_config.variables = variables

        project_config.freeze()
        return project_config


class PipelineParser(object):
    # A set of reporter configuration keys to action mapping
    reporter_actions = {
        'enqueue': 'enqueue_actions',
        'start': 'start_actions',
        'success': 'success_actions',
        'failure': 'failure_actions',
        'merge-failure': 'merge_failure_actions',
        'no-jobs': 'no_jobs_actions',
        'disabled': 'disabled_actions',
        'dequeue': 'dequeue_actions',
    }

    def __init__(self, pcontext):
        self.log = logging.getLogger("zuul.PipelineParser")
        self.pcontext = pcontext
        self.schema = self.getSchema()

    def getDriverSchema(self, dtype):
        methods = {
            'trigger': 'getTriggerSchema',
            'reporter': 'getReporterSchema',
            'require': 'getRequireSchema',
            'reject': 'getRejectSchema',
        }

        schema = {}
        # Add the configured connections as available layout options
        for connection_name, connection in \
            self.pcontext.connections.connections.items():
            method = getattr(connection.driver, methods[dtype], None)
            if method:
                schema[connection_name] = to_list(method())

        return schema

    def getSchema(self):
        manager = vs.Any('independent',
                         'dependent',
                         'serial',
                         'supercedent')

        precedence = vs.Any('normal', 'low', 'high')

        window = vs.All(int, vs.Range(min=0))
        window_floor = vs.All(int, vs.Range(min=1))
        window_type = vs.Any('linear', 'exponential')
        window_factor = vs.All(int, vs.Range(min=1))

        pipeline = {vs.Required('name'): str,
                    vs.Required('manager'): manager,
                    'precedence': precedence,
                    'supercedes': to_list(str),
                    'description': str,
                    'success-message': str,
                    'failure-message': str,
                    'start-message': str,
                    'merge-failure-message': str,
                    'no-jobs-message': str,
                    'footer-message': str,
                    'dequeue-message': str,
                    'dequeue-on-new-patchset': bool,
                    'ignore-dependencies': bool,
                    'post-review': bool,
                    'disable-after-consecutive-failures':
                        vs.All(int, vs.Range(min=1)),
                    'window': window,
                    'window-floor': window_floor,
                    'window-increase-type': window_type,
                    'window-increase-factor': window_factor,
                    'window-decrease-type': window_type,
                    'window-decrease-factor': window_factor,
                    '_source_context': model.SourceContext,
                    '_start_mark': ZuulMark,
                    }
        pipeline['require'] = self.getDriverSchema('require')
        pipeline['reject'] = self.getDriverSchema('reject')
        pipeline['trigger'] = vs.Required(self.getDriverSchema('trigger'))
        for action in ['enqueue', 'start', 'success', 'failure',
                       'merge-failure', 'no-jobs', 'disabled', 'dequeue']:
            pipeline[action] = self.getDriverSchema('reporter')
        return vs.Schema(pipeline)

    def fromYaml(self, conf):
        self.schema(conf)
        pipeline = model.Pipeline(conf['name'], self.pcontext.tenant)
        pipeline.source_context = conf['_source_context']
        pipeline.start_mark = conf['_start_mark']
        pipeline.description = conf.get('description')
        pipeline.supercedes = as_list(conf.get('supercedes', []))

        precedence = model.PRECEDENCE_MAP[conf.get('precedence')]
        pipeline.precedence = precedence
        pipeline.failure_message = conf.get('failure-message',
                                            "Build failed.")
        pipeline.merge_failure_message = conf.get(
            'merge-failure-message', "Merge Failed.\n\nThis change or one "
            "of its cross-repo dependencies was unable to be "
            "automatically merged with the current state of its "
            "repository. Please rebase the change and upload a new "
            "patchset.")
        pipeline.success_message = conf.get('success-message',
                                            "Build succeeded.")
        pipeline.footer_message = conf.get('footer-message', "")
        pipeline.start_message = conf.get('start-message',
                                          "Starting {pipeline.name} jobs.")
        pipeline.enqueue_message = conf.get('enqueue-message', "")
        pipeline.no_jobs_message = conf.get('no-jobs-message', "")
        pipeline.dequeue_message = conf.get(
            "dequeue-message", "Build canceled."
        )
        pipeline.dequeue_on_new_patchset = conf.get(
            'dequeue-on-new-patchset', True)
        pipeline.ignore_dependencies = conf.get(
            'ignore-dependencies', False)
        pipeline.post_review = conf.get(
            'post-review', False)

        for conf_key, action in self.reporter_actions.items():
            reporter_set = []
            allowed_reporters = self.pcontext.tenant.allowed_reporters
            if conf.get(conf_key):
                if conf_key in ['success', 'failure', 'merge-failure']:
                    # SQL reporters are required (an implied SQL reporter is
                    # added to every pipeline, ...(1)
                    sql_reporter = self.pcontext.connections\
                        .getSqlReporter(pipeline)
                    sql_reporter.setAction(conf_key)
                    reporter_set.append(sql_reporter)
                for reporter_name, params \
                    in conf.get(conf_key).items():
                    if allowed_reporters is not None and \
                       reporter_name not in allowed_reporters:
                        raise UnknownConnection(reporter_name)
                    if type(self.pcontext.connections
                                .connections[reporter_name]) == SQLConnection:
                        # (1)... explicit SQL reporters are ignored)
                        self.log.warning("Ignoring SQL reporter configured in"
                                         " pipeline %s" % pipeline.name)
                        continue
                    reporter = self.pcontext.connections.getReporter(
                        reporter_name, pipeline, params)
                    reporter.setAction(conf_key)
                    reporter_set.append(reporter)
            setattr(pipeline, action, reporter_set)

        # If merge-failure actions aren't explicit, use the failure actions
        if not pipeline.merge_failure_actions:
            pipeline.merge_failure_actions = pipeline.failure_actions

        for conf_key, action in self.reporter_actions.items():
            if conf_key in ['success', 'failure', 'merge-failure']\
                    and not getattr(pipeline, action):
                # SQL reporters are required ... add SQL reporters to the
                # rest of actions.
                sql_reporter = self.pcontext.connections\
                    .getSqlReporter(pipeline)
                sql_reporter.setAction(conf_key)
                setattr(pipeline, action, [sql_reporter])

        pipeline.disable_at = conf.get(
            'disable-after-consecutive-failures', None)

        pipeline.window = conf.get('window', 20)
        pipeline.window_floor = conf.get('window-floor', 3)
        pipeline.window_increase_type = conf.get(
            'window-increase-type', 'linear')
        pipeline.window_increase_factor = conf.get(
            'window-increase-factor', 1)
        pipeline.window_decrease_type = conf.get(
            'window-decrease-type', 'exponential')
        pipeline.window_decrease_factor = conf.get(
            'window-decrease-factor', 2)

        manager_name = conf['manager']
        if manager_name == 'dependent':
            manager = zuul.manager.dependent.DependentPipelineManager(
                self.pcontext.scheduler, pipeline)
        elif manager_name == 'independent':
            manager = zuul.manager.independent.IndependentPipelineManager(
                self.pcontext.scheduler, pipeline)
        elif manager_name == 'serial':
            manager = zuul.manager.serial.SerialPipelineManager(
                self.pcontext.scheduler, pipeline)
        elif manager_name == 'supercedent':
            manager = zuul.manager.supercedent.SupercedentPipelineManager(
                self.pcontext.scheduler, pipeline)

        pipeline.setManager(manager)

        for source_name, require_config in conf.get('require', {}).items():
            source = self.pcontext.connections.getSource(source_name)
            manager.ref_filters.extend(
                source.getRequireFilters(require_config))

        for source_name, reject_config in conf.get('reject', {}).items():
            source = self.pcontext.connections.getSource(source_name)
            manager.ref_filters.extend(
                source.getRejectFilters(reject_config))

        for connection_name, trigger_config in conf.get('trigger').items():
            if self.pcontext.tenant.allowed_triggers is not None and \
               connection_name not in self.pcontext.tenant.allowed_triggers:
                raise UnknownConnection(connection_name)
            trigger = self.pcontext.connections.getTrigger(
                connection_name, trigger_config)
            pipeline.triggers.append(trigger)
            manager.event_filters.extend(
                trigger.getEventFilters(connection_name,
                                        conf['trigger'][connection_name]))

        # Pipelines don't get frozen
        return pipeline


class SemaphoreParser(object):
    def __init__(self, pcontext):
        self.log = logging.getLogger("zuul.SemaphoreParser")
        self.pcontext = pcontext
        self.schema = self.getSchema()

    def getSchema(self):
        semaphore = {vs.Required('name'): str,
                     'max': int,
                     '_source_context': model.SourceContext,
                     '_start_mark': ZuulMark,
                     }

        return vs.Schema(semaphore)

    def fromYaml(self, conf):
        self.schema(conf)
        semaphore = model.Semaphore(conf['name'], conf.get('max', 1))
        semaphore.source_context = conf.get('_source_context')
        semaphore.start_mark = conf.get('_start_mark')
        semaphore.freeze()
        return semaphore


class QueueParser:
    def __init__(self, pcontext):
        self.log = logging.getLogger("zuul.QueueParser")
        self.pcontext = pcontext
        self.schema = self.getSchema()

    def getSchema(self):
        queue = {vs.Required('name'): str,
                 'per-branch': bool,
                 'allow-circular-dependencies': bool,
                 '_source_context': model.SourceContext,
                 '_start_mark': ZuulMark,
                 }
        return vs.Schema(queue)

    def fromYaml(self, conf):
        self.schema(conf)
        queue = model.Queue(
            conf['name'],
            conf.get('per-branch', False),
            conf.get('allow-circular-dependencies', False),
        )
        queue.source_context = conf.get('_source_context')
        queue.start_mark = conf.get('_start_mark')
        queue.freeze()
        return queue


class AuthorizationRuleParser(object):
    def __init__(self):
        self.log = logging.getLogger("zuul.AuthorizationRuleParser")
        self.schema = self.getSchema()

    def getSchema(self):

        authRule = {vs.Required('name'): str,
                    vs.Required('conditions'): to_list(dict)
                   }

        return vs.Schema(authRule)

    def fromYaml(self, conf):
        self.schema(conf)
        a = model.AuthZRuleTree(conf['name'])

        def parse_tree(node):
            if isinstance(node, list):
                return model.OrRule(parse_tree(x) for x in node)
            elif isinstance(node, dict):
                subrules = []
                for claim, value in node.items():
                    if claim == 'zuul_uid':
                        claim = '__zuul_uid_claim'
                    subrules.append(model.ClaimRule(claim, value))
                return model.AndRule(subrules)
            else:
                raise Exception('Invalid claim declaration %r' % node)

        a.ruletree = parse_tree(conf['conditions'])
        return a


class ParseContext(object):
    """Hold information about a particular run of the parser"""

    def __init__(self, connections, scheduler, tenant, ansible_manager):
        self.connections = connections
        self.scheduler = scheduler
        self.tenant = tenant
        self.ansible_manager = ansible_manager
        self.pragma_parser = PragmaParser(self)
        self.pipeline_parser = PipelineParser(self)
        self.nodeset_parser = NodeSetParser(self)
        self.secret_parser = SecretParser(self)
        self.job_parser = JobParser(self)
        self.semaphore_parser = SemaphoreParser(self)
        self.queue_parser = QueueParser(self)
        self.project_template_parser = ProjectTemplateParser(self)
        self.project_parser = ProjectParser(self)

    def getImpliedBranches(self, source_context):
        # If the user has set a pragma directive for this, use the
        # value (if unset, the value is None).
        if source_context.implied_branch_matchers is True:
            if source_context.implied_branches is not None:
                return source_context.implied_branches
            return [source_context.branch]
        elif source_context.implied_branch_matchers is False:
            return None

        # If this is a trusted project, don't create implied branch
        # matchers.
        if source_context.trusted:
            return None

        # If this project only has one branch, don't create implied
        # branch matchers.  This way central job repos can work.
        branches = self.tenant.getProjectBranches(
            source_context.project)
        if len(branches) == 1:
            return None

        if source_context.implied_branches is not None:
            return source_context.implied_branches
        return [source_context.branch]


class TenantParser(object):
    def __init__(self, connections, scheduler, merger, keystorage):
        self.log = logging.getLogger("zuul.TenantParser")
        self.connections = connections
        self.scheduler = scheduler
        self.merger = merger
        self.keystorage = keystorage

    classes = vs.Any('pipeline', 'job', 'semaphore', 'project',
                     'project-template', 'nodeset', 'secret', 'queue')

    project_dict = {str: {
        'include': to_list(classes),
        'exclude': to_list(classes),
        'shadow': to_list(str),
        'exclude-unprotected-branches': bool,
        'extra-config-paths': to_list(str),
        'load-branch': str,
        'allow-circular-dependencies': bool,
    }}

    project = vs.Any(str, project_dict)

    group = {
        'include': to_list(classes),
        'exclude': to_list(classes),
        vs.Required('projects'): to_list(project),
    }

    project_or_group = vs.Any(project, group)

    tenant_source = vs.Schema({
        'config-projects': to_list(project_or_group),
        'untrusted-projects': to_list(project_or_group),
    })

    def validateTenantSources(self):
        def v(value, path=[]):
            if isinstance(value, dict):
                for k, val in value.items():
                    self.connections.getSource(k)
                    self.validateTenantSource(val, path + [k])
            else:
                raise vs.Invalid("Invalid tenant source", path)
        return v

    def validateTenantSource(self, value, path=[]):
        self.tenant_source(value)

    def getSchema(self):
        tenant = {vs.Required('name'): str,
                  'max-nodes-per-job': int,
                  'max-job-timeout': int,
                  'source': self.validateTenantSources(),
                  'exclude-unprotected-branches': bool,
                  'allowed-triggers': to_list(str),
                  'allowed-reporters': to_list(str),
                  'allowed-labels': to_list(str),
                  'disallowed-labels': to_list(str),
                  'allow-circular-dependencies': bool,
                  'default-parent': str,
                  'default-ansible-version': vs.Any(str, float),
                  'admin-rules': to_list(str),
                  'report-build-page': bool,
                  'web-root': str,
                  }
        return vs.Schema(tenant)

    def fromYaml(self, abide, conf, ansible_manager):
        self.getSchema()(conf)
        tenant = model.Tenant(conf['name'])
        pcontext = ParseContext(self.connections, self.scheduler,
                                tenant, ansible_manager)
        if conf.get('max-nodes-per-job') is not None:
            tenant.max_nodes_per_job = conf['max-nodes-per-job']
        if conf.get('max-job-timeout') is not None:
            tenant.max_job_timeout = int(conf['max-job-timeout'])
        if conf.get('exclude-unprotected-branches') is not None:
            tenant.exclude_unprotected_branches = \
                conf['exclude-unprotected-branches']
        if conf.get('admin-rules') is not None:
            tenant.authorization_rules = conf['admin-rules']
        if conf.get('report-build-page') is not None:
            tenant.report_build_page = conf['report-build-page']
        tenant.web_root = conf.get('web-root', self.scheduler.web_root)
        if tenant.web_root and not tenant.web_root.endswith('/'):
            tenant.web_root += '/'
        tenant.allowed_triggers = conf.get('allowed-triggers')
        tenant.allowed_reporters = conf.get('allowed-reporters')
        tenant.allowed_labels = conf.get('allowed-labels')
        tenant.disallowed_labels = conf.get('disallowed-labels')
        tenant.default_base_job = conf.get('default-parent', 'base')

        tenant.unparsed_config = conf
        # tpcs is TenantProjectConfigs
        config_tpcs, untrusted_tpcs = self._loadTenantProjects(conf)
        for tpc in config_tpcs:
            tenant.addConfigProject(tpc)
        for tpc in untrusted_tpcs:
            tenant.addUntrustedProject(tpc)

        # We prepare a stack to store config loading issues
        loading_errors = model.LoadingErrors()

        for tpc in config_tpcs + untrusted_tpcs:
            source_context = model.ProjectContext(tpc.project)
            with project_configuration_exceptions(source_context,
                                                  loading_errors):
                self._getProjectBranches(tenant, tpc)
                self._resolveShadowProjects(tenant, tpc)

        # Set default ansible version
        default_ansible_version = conf.get('default-ansible-version')
        if default_ansible_version is not None:
            # The ansible version can be interpreted as float by yaml so make
            # sure it's a string.
            default_ansible_version = str(default_ansible_version)
            ansible_manager.requestVersion(default_ansible_version)
        else:
            default_ansible_version = ansible_manager.default_version
        tenant.default_ansible_version = default_ansible_version

        # Start by fetching any YAML needed by this tenant which isn't
        # already cached.  Full reconfigurations start with an empty
        # cache.
        self._cacheTenantYAML(abide, tenant, loading_errors)

        # Then collect the appropriate YAML based on this tenant
        # config.
        config_projects_config, untrusted_projects_config = \
            self._loadTenantYAML(abide, tenant, loading_errors)

        # Then convert the YAML to configuration objects which we
        # cache on the tenant.
        tenant.config_projects_config = self.parseConfig(
            tenant, config_projects_config, loading_errors, pcontext)
        tenant.untrusted_projects_config = self.parseConfig(
            tenant, untrusted_projects_config, loading_errors, pcontext)

        # Combine the trusted and untrusted config objects
        parsed_config = model.ParsedConfig()
        parsed_config.extend(tenant.config_projects_config)
        parsed_config.extend(tenant.untrusted_projects_config)

        # Cache all of the objects on the individual project-branches
        # for later use during dynamic reconfigurations.
        self.cacheConfig(tenant, parsed_config)

        tenant.layout = self._parseLayout(
            tenant, parsed_config, loading_errors)
        tenant.semaphore_handler = SemaphoreHandler(
            self.scheduler.zk_client, tenant.name, tenant.layout
        )

        return tenant

    def _resolveShadowProjects(self, tenant, tpc):
        shadow_projects = []
        for sp in tpc.shadow_projects:
            _, project = tenant.getProject(sp)
            if project is None:
                raise ProjectNotFoundError(sp)
            shadow_projects.append(project)
        tpc.shadow_projects = frozenset(shadow_projects)

    def _getProjectBranches(self, tenant, tpc):
        branches = sorted(tpc.project.source.getProjectBranches(
            tpc.project, tenant))
        if 'master' in branches:
            branches.remove('master')
            branches = ['master'] + branches
        tpc.branches = branches

    def _loadProjectKeys(self, connection_name, project):
        project.private_secrets_key, project.public_secrets_key = (
            self.keystorage.getProjectSecretsKeys(
                connection_name, project.name
            )
        )

        project.private_ssh_key, project.public_ssh_key = (
            self.keystorage.getProjectSSHKeys(connection_name, project.name)
        )

    @staticmethod
    def _getProject(source, conf, current_include):
        extra_config_files = ()
        extra_config_dirs = ()

        if isinstance(conf, str):
            # Return a project object whether conf is a dict or a str
            project = source.getProject(conf)
            project_include = current_include
            shadow_projects = []
            project_exclude_unprotected_branches = None
            project_load_branch = None
        else:
            project_name = list(conf.keys())[0]
            project = source.getProject(project_name)
            shadow_projects = as_list(conf[project_name].get('shadow', []))

            # We check for None since the user may set include to an empty list
            if conf[project_name].get("include") is None:
                project_include = current_include
            else:
                project_include = frozenset(
                    as_list(conf[project_name]['include']))
            project_exclude = frozenset(
                as_list(conf[project_name].get('exclude', [])))
            if project_exclude:
                project_include = frozenset(project_include - project_exclude)
            project_exclude_unprotected_branches = conf[project_name].get(
                'exclude-unprotected-branches', None)
            if conf[project_name].get('extra-config-paths') is not None:
                extra_config_paths = as_list(
                    conf[project_name]['extra-config-paths'])
                extra_config_files = tuple([x for x in extra_config_paths
                                            if not x.endswith('/')])
                extra_config_dirs = tuple([x[:-1] for x in extra_config_paths
                                           if x.endswith('/')])
            project_load_branch = conf[project_name].get(
                'load-branch', None)

        tenant_project_config = model.TenantProjectConfig(project)
        tenant_project_config.load_classes = frozenset(project_include)
        tenant_project_config.shadow_projects = shadow_projects
        tenant_project_config.exclude_unprotected_branches = \
            project_exclude_unprotected_branches
        tenant_project_config.extra_config_files = extra_config_files
        tenant_project_config.extra_config_dirs = extra_config_dirs
        tenant_project_config.load_branch = project_load_branch

        return tenant_project_config

    def _getProjects(self, source, conf, current_include):
        # Return a project object whether conf is a dict or a str
        projects = []
        if isinstance(conf, str):
            # A simple project name string
            projects.append(self._getProject(source, conf, current_include))
        elif len(conf.keys()) > 1 and 'projects' in conf:
            # This is a project group
            if 'include' in conf:
                current_include = set(as_list(conf['include']))
            else:
                current_include = current_include.copy()
            if 'exclude' in conf:
                exclude = set(as_list(conf['exclude']))
                current_include = current_include - exclude
            for project in conf['projects']:
                sub_projects = self._getProjects(
                    source, project, current_include)
                projects.extend(sub_projects)
        elif len(conf.keys()) == 1:
            # A project with overrides
            projects.append(self._getProject(
                source, conf, current_include))
        else:
            raise Exception("Unable to parse project %s", conf)
        return projects

    def _loadTenantProjects(self, conf_tenant):
        config_projects = []
        untrusted_projects = []

        default_include = frozenset(['pipeline', 'job', 'semaphore', 'project',
                                     'secret', 'project-template', 'nodeset',
                                     'queue'])

        for source_name, conf_source in conf_tenant.get('source', {}).items():
            source = self.connections.getSource(source_name)

            current_include = default_include
            for conf_repo in conf_source.get('config-projects', []):
                # tpcs = TenantProjectConfigs
                tpcs = self._getProjects(source, conf_repo, current_include)
                for tpc in tpcs:
                    self._loadProjectKeys(source_name, tpc.project)
                    config_projects.append(tpc)

            current_include = frozenset(default_include - set(['pipeline']))
            for conf_repo in conf_source.get('untrusted-projects', []):
                tpcs = self._getProjects(source, conf_repo,
                                         current_include)
                for tpc in tpcs:
                    self._loadProjectKeys(source_name, tpc.project)
                    untrusted_projects.append(tpc)

        return config_projects, untrusted_projects

    def _cacheTenantYAML(self, abide, tenant, loading_errors):
        jobs = []
        for project in itertools.chain(
                tenant.config_projects, tenant.untrusted_projects):
            tpc = tenant.project_configs[project.canonical_name]
            # For each branch in the repo, get the zuul.yaml for that
            # branch.  Remember the branch and then implicitly add a
            # branch selector to each job there.  This makes the
            # in-repo configuration apply only to that branch.
            branches = tenant.getProjectBranches(project)
            for branch in branches:
                branch_cache = abide.getUnparsedBranchCache(
                    project.canonical_name, branch)
                if branch_cache.isValidFor(tpc):
                    # We already have this branch cached.
                    continue
                if not tpc.load_classes:
                    # If all config classes are excluded then do not
                    # request any getFiles jobs.
                    continue
                job = self.merger.getFiles(
                    project.source.connection.connection_name,
                    project.name, branch,
                    files=(['zuul.yaml', '.zuul.yaml'] +
                           list(tpc.extra_config_files)),
                    dirs=['zuul.d', '.zuul.d'] + list(tpc.extra_config_dirs))
                self.log.debug("Submitting cat job %s for %s %s %s" % (
                    job, project.source.connection.connection_name,
                    project.name, branch))
                job.source_context = model.SourceContext(
                    project, branch, '', False)
                jobs.append(job)
                branch_cache.setValidFor(tpc)

        for job in jobs:
            self.log.debug("Waiting for cat job %s" % (job,))
            job.wait(self.merger.git_timeout)
            if not job.updated:
                raise Exception("Cat job %s failed" % (job,))
            self.log.debug("Cat job %s got files %s" %
                           (job, job.files.keys()))
            loaded = False
            files = sorted(job.files.keys())
            unparsed_config = model.UnparsedConfig()
            tpc = tenant.project_configs[
                job.source_context.project.canonical_name]
            for conf_root in (
                    ('zuul.yaml', 'zuul.d', '.zuul.yaml', '.zuul.d') +
                    tpc.extra_config_files + tpc.extra_config_dirs):
                for fn in files:
                    fn_root = fn.split('/')[0]
                    if fn_root != conf_root or not job.files.get(fn):
                        continue
                    # Don't load from more than one configuration in a
                    # project-branch (unless an "extra" file/dir).
                    if (conf_root not in tpc.extra_config_files and
                        conf_root not in tpc.extra_config_dirs):
                        if (loaded and loaded != conf_root):
                            self.log.warning(
                                "Multiple configuration files in %s" %
                                (job.source_context,))
                            continue
                        loaded = conf_root
                    # Create a new source_context so we have unique filenames.
                    source_context = job.source_context.copy()
                    source_context.path = fn
                    self.log.info(
                        "Loading configuration from %s" %
                        (source_context,))
                    incdata = self.loadProjectYAML(
                        job.files[fn], source_context, loading_errors)
                    branch_cache = abide.getUnparsedBranchCache(
                        source_context.project.canonical_name,
                        source_context.branch)
                    branch_cache.put(source_context.path, incdata)
                    unparsed_config.extend(incdata)

    def _loadTenantYAML(self, abide, tenant, loading_errors):
        config_projects_config = model.UnparsedConfig()
        untrusted_projects_config = model.UnparsedConfig()

        for project in tenant.config_projects:
            tpc = tenant.project_configs.get(project.canonical_name)
            branch = tpc.load_branch if tpc.load_branch else 'master'
            branch_cache = abide.getUnparsedBranchCache(
                project.canonical_name, branch)
            tpc = tenant.project_configs[project.canonical_name]
            unparsed_branch_config = branch_cache.get(tpc)

            if unparsed_branch_config:
                unparsed_branch_config = self.filterConfigProjectYAML(
                    unparsed_branch_config)

                config_projects_config.extend(unparsed_branch_config)

        for project in tenant.untrusted_projects:
            branches = tenant.getProjectBranches(project)
            for branch in branches:
                branch_cache = abide.getUnparsedBranchCache(
                    project.canonical_name, branch)
                tpc = tenant.project_configs[project.canonical_name]
                unparsed_branch_config = branch_cache.get(tpc)
                if unparsed_branch_config:
                    unparsed_branch_config = self.filterUntrustedProjectYAML(
                        unparsed_branch_config, loading_errors)

                    untrusted_projects_config.extend(unparsed_branch_config)
        return config_projects_config, untrusted_projects_config

    def loadProjectYAML(self, data, source_context, loading_errors):
        config = model.UnparsedConfig()
        try:
            with early_configuration_exceptions(source_context):
                r = safe_load_yaml(data, source_context)
                config.extend(r)
        except ConfigurationSyntaxError as e:
            loading_errors.addError(source_context, None, e)
        return config

    def filterConfigProjectYAML(self, data):
        # Any config object may appear in a config project.
        return data.copy(trusted=True)

    def filterUntrustedProjectYAML(self, data, loading_errors):
        if data and data.pipelines:
            with configuration_exceptions(
                    'pipeline', data.pipelines[0], loading_errors):
                raise PipelineNotPermittedError()
        return data.copy(trusted=False)

    def _getLoadClasses(self, tenant, conf_object):
        project = conf_object.get('_source_context').project
        tpc = tenant.project_configs[project.canonical_name]
        return tpc.load_classes

    def parseConfig(self, tenant, unparsed_config, loading_errors, pcontext):
        parsed_config = model.ParsedConfig()

        # Handle pragma items first since they modify the source context
        # used by other classes.
        for config_pragma in unparsed_config.pragmas:
            try:
                pcontext.pragma_parser.fromYaml(config_pragma)
            except ConfigurationSyntaxError as e:
                loading_errors.addError(
                    config_pragma['_source_context'],
                    config_pragma['_start_mark'], e)

        for config_pipeline in unparsed_config.pipelines:
            classes = self._getLoadClasses(tenant, config_pipeline)
            if 'pipeline' not in classes:
                continue
            with configuration_exceptions('pipeline',
                                          config_pipeline, loading_errors):
                parsed_config.pipelines.append(
                    pcontext.pipeline_parser.fromYaml(config_pipeline))

        for config_nodeset in unparsed_config.nodesets:
            classes = self._getLoadClasses(tenant, config_nodeset)
            if 'nodeset' not in classes:
                continue
            with configuration_exceptions('nodeset',
                                          config_nodeset, loading_errors):
                parsed_config.nodesets.append(
                    pcontext.nodeset_parser.fromYaml(config_nodeset))

        for config_secret in unparsed_config.secrets:
            classes = self._getLoadClasses(tenant, config_secret)
            if 'secret' not in classes:
                continue
            with configuration_exceptions('secret',
                                          config_secret, loading_errors):
                parsed_config.secrets.append(
                    pcontext.secret_parser.fromYaml(config_secret))

        for config_job in unparsed_config.jobs:
            classes = self._getLoadClasses(tenant, config_job)
            if 'job' not in classes:
                continue
            with configuration_exceptions('job',
                                          config_job, loading_errors):
                parsed_config.jobs.append(
                    pcontext.job_parser.fromYaml(config_job))

        for config_semaphore in unparsed_config.semaphores:
            classes = self._getLoadClasses(tenant, config_semaphore)
            if 'semaphore' not in classes:
                continue
            with configuration_exceptions('semaphore',
                                          config_semaphore, loading_errors):
                parsed_config.semaphores.append(
                    pcontext.semaphore_parser.fromYaml(config_semaphore))

        for config_queue in unparsed_config.queues:
            classes = self._getLoadClasses(tenant, config_queue)
            if 'queue' not in classes:
                continue
            with configuration_exceptions('queue',
                                          config_queue, loading_errors):
                parsed_config.queues.append(
                    pcontext.queue_parser.fromYaml(config_queue))

        for config_template in unparsed_config.project_templates:
            classes = self._getLoadClasses(tenant, config_template)
            if 'project-template' not in classes:
                continue
            with configuration_exceptions(
                    'project-template', config_template, loading_errors):
                parsed_config.project_templates.append(
                    pcontext.project_template_parser.fromYaml(
                        config_template))

        for config_project in unparsed_config.projects:
            classes = self._getLoadClasses(tenant, config_project)
            if 'project' not in classes:
                continue
            with configuration_exceptions('project', config_project,
                                          loading_errors):
                # we need to separate the regex projects as they are
                # processed differently later
                name = config_project.get('name')
                parsed_project = pcontext.project_parser.fromYaml(
                    config_project)
                if name and name.startswith('^'):
                    parsed_config.projects_by_regex.setdefault(
                        name, []).append(parsed_project)
                else:
                    parsed_config.projects.append(parsed_project)

        return parsed_config

    def cacheConfig(self, tenant, parsed_config):
        def _cache(attr, obj):
            tpc = tenant.project_configs[
                obj.source_context.project.canonical_name]
            branch_cache = tpc.parsed_branch_config.get(
                obj.source_context.branch)
            if branch_cache is None:
                branch_cache = tpc.parsed_branch_config.setdefault(
                    obj.source_context.branch,
                    model.ParsedConfig())
            lst = getattr(branch_cache, attr)
            lst.append(obj)

        # We don't cache pragma objects as they are acted on when
        # parsed.

        for pipeline in parsed_config.pipelines:
            _cache('pipelines', pipeline)

        for nodeset in parsed_config.nodesets:
            _cache('nodesets', nodeset)

        for secret in parsed_config.secrets:
            _cache('secrets', secret)

        for job in parsed_config.jobs:
            _cache('jobs', job)

        for queue in parsed_config.queues:
            _cache('queues', queue)

        for semaphore in parsed_config.semaphores:
            _cache('semaphores', semaphore)

        for template in parsed_config.project_templates:
            _cache('project_templates', template)

        for project_config in parsed_config.projects:
            _cache('projects', project_config)

    def _addLayoutItems(self, layout, tenant, parsed_config,
                        skip_pipelines=False, skip_semaphores=False):
        # TODO(jeblair): make sure everything needing
        # reference_exceptions has it; add tests if needed.
        if not skip_pipelines:
            for pipeline in parsed_config.pipelines:
                with reference_exceptions(
                        'pipeline', pipeline, layout.loading_errors):
                    layout.addPipeline(pipeline)

        for nodeset in parsed_config.nodesets:
            with reference_exceptions(
                    'nodeset', nodeset, layout.loading_errors):
                layout.addNodeSet(nodeset)

        for secret in parsed_config.secrets:
            with reference_exceptions('secret', secret, layout.loading_errors):
                layout.addSecret(secret)

        for job in parsed_config.jobs:
            with reference_exceptions('job', job, layout.loading_errors):
                added = layout.addJob(job)
            if not added:
                self.log.debug(
                    "Skipped adding job %s which shadows an existing job" %
                    (job,))

        # Now that all the jobs are loaded, verify references to other
        # config objects.
        for jobs in layout.jobs.values():
            for job in jobs:
                with reference_exceptions('job', job, layout.loading_errors):
                    job.validateReferences(layout)
        for pipeline in layout.pipelines.values():
            with reference_exceptions(
                    'pipeline', pipeline, layout.loading_errors):
                pipeline.validateReferences(layout)

        if skip_semaphores:
            # We should not actually update the layout with new
            # semaphores, but so that we can validate that the config
            # is correct, create a shadow layout here to which we add
            # new semaphores so validation is complete.
            semaphore_layout = model.Layout(tenant)
        else:
            semaphore_layout = layout
        for semaphore in parsed_config.semaphores:
            with reference_exceptions(
                    'semaphore', semaphore, layout.loading_errors):
                semaphore_layout.addSemaphore(semaphore)

        for queue in parsed_config.queues:
            with reference_exceptions('queue', queue, layout.loading_errors):
                layout.addQueue(queue)

        for template in parsed_config.project_templates:
            with reference_exceptions(
                    'project-template', template, layout.loading_errors):
                layout.addProjectTemplate(template)

        # The project stanzas containing a regex are separated from the normal
        # project stanzas and organized by regex. We need to loop over each
        # regex and copy each stanza below the regex for each matching project.
        for regex, config_projects in parsed_config.projects_by_regex.items():
            projects_matching_regex = tenant.getProjectsByRegex(regex)

            for trusted, project in projects_matching_regex:
                for config_project in config_projects:
                    # we just override the project name here so a simple copy
                    # should be enough
                    conf = config_project.copy()
                    name = project.canonical_name
                    conf.name = name
                    conf.freeze()
                    parsed_config.projects.append(conf)

        for project in parsed_config.projects:
            layout.addProjectConfig(project)

        # Now that all the project pipelines are loaded, fixup and
        # verify references to other config objects.
        self._validateProjectPipelineConfigs(layout)

    def _validateProjectPipelineConfigs(self, layout):
        # Validate references to other config objects
        def inner_validate_ppcs(ppc):
            for jobs in ppc.job_list.jobs.values():
                for job in jobs:
                    # validate that the job exists on its own (an
                    # additional requirement for project-pipeline
                    # jobs)
                    layout.getJob(job.name)
                    job.validateReferences(layout)

        for project_name in layout.project_configs:
            for project_config in layout.project_configs[project_name]:
                with reference_exceptions(
                        'project', project_config, layout.loading_errors):
                    for template_name in project_config.templates:
                        if template_name not in layout.project_templates:
                            raise TemplateNotFoundError(template_name)
                        project_templates = layout.getProjectTemplates(
                            template_name)
                        for p_tmpl in project_templates:
                            with reference_exceptions(
                                    'project-template', p_tmpl,
                                    layout.loading_errors):
                                for ppc in p_tmpl.pipelines.values():
                                    inner_validate_ppcs(ppc)
                    for ppc in project_config.pipelines.values():
                        inner_validate_ppcs(ppc)

    def _parseLayout(self, tenant, data, loading_errors):
        # Don't call this method from dynamic reconfiguration because
        # it interacts with drivers and connections.
        layout = model.Layout(tenant)
        layout.loading_errors = loading_errors
        self.log.debug("Created layout id %s", layout.uuid)

        self._addLayoutItems(layout, tenant, data)

        for pipeline in layout.pipelines.values():
            pipeline.manager._postConfig(layout)

        return layout


class ConfigLoader(object):
    log = logging.getLogger("zuul.ConfigLoader")

    def __init__(self, connections, scheduler, merger, keystorage):
        self.connections = connections
        self.scheduler = scheduler
        self.merger = merger
        self.keystorage = keystorage
        self.tenant_parser = TenantParser(connections, scheduler,
                                          merger, self.keystorage)
        self.admin_rule_parser = AuthorizationRuleParser()

    def expandConfigPath(self, config_path):
        if config_path:
            config_path = os.path.expanduser(config_path)
        if not os.path.exists(config_path):
            raise Exception("Unable to read tenant config file at %s" %
                            config_path)
        return config_path

    def readConfig(self, config_path, from_script=False):
        config_path = self.expandConfigPath(config_path)
        if not from_script:
            with open(config_path) as config_file:
                self.log.info("Loading configuration from %s" % (config_path,))
                data = yaml.safe_load(config_file)
        else:
            if not os.access(config_path, os.X_OK):
                self.log.error(
                    "Unable to read tenant configuration from a non "
                    "executable script (%s)" % config_path)
                data = []
            else:
                self.log.info(
                    "Loading configuration from script %s" % config_path)
                ret = subprocess.run(
                    [config_path], stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE)
                try:
                    ret.check_returncode()
                    data = yaml.safe_load(ret.stdout)
                except subprocess.CalledProcessError as error:
                    self.log.error(
                        "Tenant config script exec failed: %s (%s)" % (
                            str(error), str(ret.stderr)))
                    data = []
        unparsed_abide = model.UnparsedAbideConfig()
        unparsed_abide.extend(data)
        return unparsed_abide

    def loadConfig(self, unparsed_abide, ansible_manager, tenants=None):
        abide = model.Abide()
        for conf_admin_rule in unparsed_abide.admin_rules:
            admin_rule = self.admin_rule_parser.fromYaml(conf_admin_rule)
            abide.admin_rules[admin_rule.name] = admin_rule

        if tenants:
            tenants_to_load = [t for t in unparsed_abide.tenants
                               if t.get('name') in tenants]
        else:
            tenants_to_load = unparsed_abide.tenants

        for conf_tenant in tenants_to_load:
            # When performing a full reload, do not use cached data.
            tenant = self.tenant_parser.fromYaml(
                abide, conf_tenant, ansible_manager)
            abide.tenants[tenant.name] = tenant
            if len(tenant.layout.loading_errors):
                self.log.warning(
                    "%s errors detected during %s tenant "
                    "configuration loading" % (
                        len(tenant.layout.loading_errors), tenant.name))
                # Log accumulated errors
                for err in tenant.layout.loading_errors.errors[:10]:
                    self.log.warning(err.error)
        return abide

    def reloadTenant(self, abide, tenant, ansible_manager,
                     unparsed_abide=None):
        new_abide = model.Abide()
        new_abide.tenants = abide.tenants.copy()
        new_abide.admin_rules = abide.admin_rules.copy()
        new_abide.unparsed_project_branch_cache = \
            abide.unparsed_project_branch_cache

        if unparsed_abide:
            # We got a new unparsed abide so re-load the tenant completely.
            # First check if the tenant is still existing and if not remove
            # from the abide.
            if tenant.name not in unparsed_abide.known_tenants:
                del new_abide.tenants[tenant.name]
                return new_abide

            unparsed_config = next(t for t in unparsed_abide.tenants
                                   if t['name'] == tenant.name)
        else:
            unparsed_config = tenant.unparsed_config

        # When reloading a tenant only, use cached data if available.
        new_tenant = self.tenant_parser.fromYaml(
            new_abide, unparsed_config, ansible_manager)
        new_abide.tenants[tenant.name] = new_tenant
        if len(new_tenant.layout.loading_errors):
            self.log.warning(
                "%s errors detected during %s tenant "
                "configuration re-loading" % (
                    len(new_tenant.layout.loading_errors), tenant.name))
            # Log accumulated errors
            for err in new_tenant.layout.loading_errors.errors[:10]:
                self.log.warning(err.error)
        return new_abide

    def _loadDynamicProjectData(self, config, project,
                                files, trusted, item, loading_errors,
                                pcontext):
        tenant = item.pipeline.tenant
        tpc = tenant.project_configs[project.canonical_name]
        if trusted:
            branches = [tpc.load_branch if tpc.load_branch else 'master']
        else:
            # Use the cached branch list; since this is a dynamic
            # reconfiguration there should not be any branch changes.
            branches = tenant.getProjectBranches(project)

        for branch in branches:
            fns1 = []
            fns2 = []
            fns3 = []
            fns4 = []
            files_entry = files.connections.get(
                project.source.connection.connection_name, {}).get(
                    project.name, {}).get(branch)
            # If there is no files entry at all for this
            # project-branch, then use the cached config.
            if files_entry is None:
                incdata = tpc.parsed_branch_config.get(branch)
                if incdata:
                    config.extend(incdata)
                continue
            # Otherwise, do not use the cached config (even if the
            # files are empty as that likely means they were deleted).
            files_list = files_entry.keys()
            for fn in files_list:
                if fn.startswith("zuul.d/"):
                    fns1.append(fn)
                if fn.startswith(".zuul.d/"):
                    fns2.append(fn)
                for ef in tpc.extra_config_files:
                    if fn.startswith(ef):
                        fns3.append(fn)
                for ed in tpc.extra_config_dirs:
                    if fn.startswith(ed):
                        fns4.append(fn)
            fns = (["zuul.yaml"] + sorted(fns1) + [".zuul.yaml"] +
                   sorted(fns2) + fns3 + sorted(fns4))
            incdata = None
            loaded = None
            for fn in fns:
                data = files.getFile(project.source.connection.connection_name,
                                     project.name, branch, fn)
                if data:
                    source_context = model.SourceContext(project, branch,
                                                         fn, trusted)
                    # Prevent mixing configuration source
                    conf_root = fn.split('/')[0]

                    # Don't load from more than one configuration in a
                    # project-branch (unless an "extra" file/dir).
                    if (conf_root not in tpc.extra_config_files and
                        conf_root not in tpc.extra_config_dirs):
                        if loaded and loaded != conf_root:
                            self.log.warning(
                                "Configuration in %s ignored because "
                                "project-branch is already configured",
                                source_context)
                            item.warning(
                                "Configuration in %s ignored because "
                                "project-branch is already configured" %
                                source_context)
                            continue
                        loaded = conf_root

                    incdata = self.tenant_parser.loadProjectYAML(
                        data, source_context, loading_errors)

                    if trusted:
                        incdata = self.tenant_parser.filterConfigProjectYAML(
                            incdata)
                    else:
                        incdata = self.tenant_parser.\
                            filterUntrustedProjectYAML(incdata, loading_errors)

                    config.extend(self.tenant_parser.parseConfig(
                        tenant, incdata, loading_errors, pcontext))

    def createDynamicLayout(self, item, files, ansible_manager,
                            include_config_projects=False,
                            zuul_event_id=None):
        tenant = item.pipeline.tenant
        log = get_annotated_logger(self.log, zuul_event_id)
        pcontext = ParseContext(self.connections, self.scheduler,
                                tenant, ansible_manager)
        loading_errors = model.LoadingErrors()
        if include_config_projects:
            config = model.ParsedConfig()
            for project in tenant.config_projects:
                self._loadDynamicProjectData(config, project, files, True,
                                             item, loading_errors, pcontext)
        else:
            config = tenant.config_projects_config.copy()

        for project in tenant.untrusted_projects:
            self._loadDynamicProjectData(config, project, files, False, item,
                                         loading_errors, pcontext)

        layout = model.Layout(tenant)
        layout.loading_errors = loading_errors
        log.debug("Created layout id %s", layout.uuid)
        if not include_config_projects:
            # NOTE: the actual pipeline objects (complete with queues
            # and enqueued items) are copied by reference here.  This
            # allows our shadow dynamic configuration to continue to
            # interact with all the other changes, each of which may
            # have their own version of reality.  We do not support
            # creating, updating, or deleting pipelines in dynamic
            # layout changes.
            layout.pipelines = tenant.layout.pipelines

            # NOTE: the semaphore definitions are copied from the
            # static layout here. For semaphores there should be no
            # per patch max value but exactly one value at any
            # time. So we do not support dynamic semaphore
            # configuration changes.
            layout.semaphores = tenant.layout.semaphores
            skip_pipelines = skip_semaphores = True
        else:
            skip_pipelines = skip_semaphores = False

        self.tenant_parser._addLayoutItems(layout, tenant, config,
                                           skip_pipelines=skip_pipelines,
                                           skip_semaphores=skip_semaphores)
        return layout
