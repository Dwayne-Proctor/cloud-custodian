"""
Lambda Support
--------------

Lambda provides for powerful realtime and near realtime compliance
execution when integrated in with a number of different amazon
services as event sources.

AWS Cloud Config
################

One event source is using AWS Cloud Config, which provides versioned
json representation of objects into s3 with rules execution against
them.  Config Rules allow for lambda execution against these resource
states to determine compliance along with a nice dashboard targeted
towards visualizing compliance information over time. At the moment
config rules execute after the resource is already active, based on the
underlying resource poll and config snapshot delivery. 

Underlying the hood aws config and config rules, appear to be just
productizing a poll of some resources into json files in s3 with
lambda and versioned metrics reports. At the moment config rules
only support items managed under the ec2 api (ec2, ebs, network) which
means they have significant coverage gap when looking at the totality
of aws services since they only cover a single api. As a result atm,
they are best suited to orgs that are using a small subset of aws that
requires audit (ie. just ec2) and prefer a pre-configured dashboard on
that subset. Of course overtime the config service will evolve.

However for capabilities and reporting around compliance Netflix
security monkey would be a better choice atm imo. Maid distinguishes
in its configurable policy engine, as opposed to hard coded, ability
to run serverless, better integration with current best aws practices
and provides remediation and enforcement capabilities.

Open question on config rules, Its unclear if rules execute against
only on a delta to a resource or against each config snapshot..

For a wider range of coverage and functionality we turn to

Cloud Watch Events
##################

Cloud watchs events is a general event bus for aws infrastructure, atm
it covers two major sources of information, real time instance status events
and cloud trail api calls over a poll period on cloud trail delivery.

Cloud trail provides a much richer data source over the entire range
of aws services exposed via the audit trail to enable defining
compliance policy effectively against any aws product.

Additionally for ec2 instances we can provide mandatory policy
compliance, that effectively means the non compliant resource never
became available.

Cloud Maid Integration
######################

Maid provides for policy level execution against any lambda subscribable
event stream.

We reconstitue current state for the given resource and execute
the policy against it, matching against the policy filters, and applying
the policy actions.

Mu is the letter after lambda, lambda is a keyword in python.

Configuration
#############


Examples

.. code-block:: yaml

   policies:

     # Cloud Watch Events over CloudTrail api calls (1-15m trailing)
     - name: ec2-tag-compliance
       mode: 
         type: cloudtrail
         sources: 
          - ec2.amazonaws.com
         events: 
          - RunInstances
         # For cloud trail events we need to reference the resources id
         resources: "detail.responseElements.instancesSet.items[].instanceId"
       filters:
         - or:
           - tag:required1: absent
           - tag:required2: absent
       actions:
         - stop

     # On EC2 Instance state events (real time, seconds)
     - name: ec2-require-encrypted-volumes
       mode:
         type: ec2-instance-state
         events:
         - pending
       filters:
         - type: ebs
           key: Encrypted
           value: False
       actions:
         - mark
         # TODO delete instance volumes that
         # are not set to delete on terminate
         # currently we have a poll policy that
         # handles this.
         - terminate

     # Periodic Function
     # Syntax for scheduler per http://goo.gl/x3oMQ4
     # Supports both rate per unit time and cron expressions
     - name: s3-bucket-check
       resource: s3
       mode:
         type: periodic
         schedule: "rate(1 day)"
"""

import abc
import base64
import inspect
from cStringIO import StringIO
import fnmatch
import hashlib
import json
import logging
import os
import pprint
import sys
import tempfile
import uuid
import zipfile

from boto3.s3.transfer import S3Transfer, TransferConfig
from botocore.exceptions import ClientError


import janitor

from janitor.utils import parse_s3

logging.root.setLevel(logging.INFO)
logging.getLogger('botocore').setLevel(logging.WARNING)
log = logging.getLogger('maid.lambda')


__notes__ = """
Architecture implementation notes

We need to load policies for lambda functions a bit differently so that
they can create the resources needed.


For full lifecycle management we need to be able to determine

 - all resources associated to a given policy
 - all resources created by maid
 - diff of current resources to goal state of resources
 - remove previous policy lambdas and their event sources
   - we need either the previous config file or we need
     to assume only one maid running lambdas in a given
     account.

 
Sample interactions

  $ cloud-maid resources -c config.yml

   lambda:
     - function: name
       sources:
        - source info


Given an event that comes in from one of a number of sources,
per event source we need to be able to extract the rseource
identities and then query state for them and before processing
filters and actions.

TODO:

- Resource Manager Abstraction for all policies (or just policy
  collection).

- Lambda Manager Update Func Configuration

-  Cli tools for listing maid provisioned resources

# S3 Uploads

 - Zip Files idempotency is a bit hard to define, we can't currently
   tag the lambda with git revisions, and zip files track mod times.
 - We're actually uploading policy specific lambda functions, as we 
   bake the policy into the function code. So we need to track two
   separate versions, the policy version and the maid code version.
 - With s3 for the function code, we can track this information better
   both via metadata and/or versioning.

Todo
----

Maid additionally could use lambda execution for resource intensive policy
actions, using dynamodb for results aggregation, and a periodic result checker,
alternatively sqs with periodic aggregator, or when lambda is vpc accessible
elasticache.
"""

# TODO move me / we should load config options directly from policy config   
class Config(dict):

    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            raise AttributeError(k)
        
    @classmethod
    def empty(cls, **kw):
        d = {}
        d.update({
            'region': "us-east-1",
            'cache': '',
            'profile': None,
            'assume_role': None,
            'log_group': None,
            'metrics_enabled': False,
            'output_dir': '/tmp/',
            'cache_period': 0,
            'dryrun': False})
        d.update(kw)
        return cls(d)


def resource_handle(event, lambda_context):
    """
    Generic resource handler dispatch
    """
    from janitor.policy import load
    policies = load(Config.empty(), 'config.json', format='json')
    resources = None
    for p in policies:
        p.push(event, lambda_context, resources)

        
def format_event(evt):
    io = StringIO()
    pprint.pprint(evt, io)
    return io.getvalue()


def periodic_handle(event, context):
    log.info("Processing scheduled event\n %s", format_event(event))
    return resource_handle(event, context)

    
def ec2_instance_state_handle(event, context):
    log.info("Processing instance event \n %s", format_event(event))
    return resource_handle(event, context)

    
def cloudtrail_handle(event, context):
    log.info("Processing trail event \n %s", format_event(event))
    return resource_handle(event, context)


class PythonPackageArchive(object):
    """Creates a zip file for python lambda functions

    Packages up a virtualenv and a source package directory per lambda's
    directory structure.
    """
    
    def __init__(self,
                 src_path, virtualenv_dir=None, skip=None,
                 lib_filter=None, src_filter=None):
        self.src_path = src_path
        if virtualenv_dir is None:
            virtualenv_dir = os.path.abspath(
                os.path.join(os.path.dirname(sys.executable), '..'))
        self.virtualenv_dir = virtualenv_dir
        self._temp_archive_file = None
        self._zip_file = None
        self._closed = False
        self.lib_filter = lib_filter
        self.src_filter = src_filter
        self.skip = skip

    @property
    def path(self):
        return self._temp_archive_file.name

    @property
    def size(self):
        if not self._closed:
            raise ValueError("Archive not closed, size not accurate")
        return os.stat(self._temp_archive_file.name).st_size

    def filter_files(self, files):
        if not self.skip:
            return files
        skip_files = set(fnmatch.filter(files, self.skip))
        return [f for f in files if not f in skip_files]
    
    def create(self):
        assert not self._temp_archive_file, "Archive already created"
        self._temp_archive_file = tempfile.NamedTemporaryFile()
        self._zip_file = zipfile.ZipFile(
            self._temp_archive_file, mode='w',
            compression=zipfile.ZIP_DEFLATED)

        prefix = os.path.dirname(self.src_path)
        if os.path.isfile(self.src_path):
            # Module Source
            self._zip_file.write(
                os.path.join(self.src_path), os.path.basename(self.src_path))
        elif os.path.isdir(self.src_path):
            # Package Source
            for root, dirs, files in os.walk(self.src_path):
                arc_prefix = os.path.relpath(root, os.path.dirname(self.src_path))
                if self.src_filter:
                    self.src_filter(root, dirs, files)
                files = self.filter_files(files)
                for f in files:
                    self._zip_file.write(
                        os.path.join(root, f),
                        os.path.join(arc_prefix, f))
            
        # Library Source
        venv_lib_path = os.path.join(
            self.virtualenv_dir, 'lib', 'python2.7', 'site-packages')
                    
        for root, dirs, files in os.walk(venv_lib_path):
            if self.lib_filter:
                dirs, files = self.lib_filter(root, dirs, files)
            arc_prefix = os.path.relpath(root, venv_lib_path)
            files = self.filter_files(files)
            for f in files:
                self._zip_file.write(
                    os.path.join(root, f),
                    os.path.join(arc_prefix, f))

    def add_contents(self, dest, contents):
        # see zinfo function for some caveats
        assert not self._closed, "Archive closed"
        self._zip_file.writestr(dest, contents)

    def close(self):
        # Note underlying tempfile is removed when archive is garbage collected
        self._closed = True
        self._zip_file.close()
        log.debug("Created maid lambda archive size: %0.2fmb",
                  (os.path.getsize(self._temp_archive_file.name) / (1024.0 * 1024.0)))
        return self

    def remove(self):
        # dispose of the temp file for garbag collection
        if self._temp_archive_file:
            self._temp_archive_file = None

    def get_checksum(self):
        """Return the b64 encoded sha256 checksum."""
        assert self._closed, "Archive not closed"
        with open(self._temp_archive_file.name) as fh:
            return base64.b64encode(checksum(fh, hashlib.sha256()))

    def get_bytes(self):
        # return the entire zip file as byte string.
        assert self._closed, "Archive not closed"
        return open(self._temp_archive_file.name, 'rb').read()


def checksum(fh, hasher, blocksize=65536):
    buf = fh.read(blocksize)
    while len(buf) > 0:
        hasher.update(buf)
        buf = fh.read(blocksize)
    return hasher.digest()


def maid_archive(skip=None):
    """Create a lambda code archive for running maid."""

    # Some aggressive shrinking
    required = ["concurrent", "yaml", "pkg_resources"]
    
    def lib_filter(root, dirs, files):
        if os.path.basename(root) == 'site-packages':
            for n in tuple(dirs):
                if n not in required:
                    dirs.remove(n)
        return dirs, files
   
    return PythonPackageArchive(
        os.path.dirname(inspect.getabsfile(janitor)),
        os.path.abspath(os.path.join(
            os.path.dirname(sys.executable), '..')),
        skip=skip,
        lib_filter=lib_filter
    )
    
        
class LambdaManager(object):
    """ Provides CRUD operations around lambda functions
    """
    def __init__(self, session_factory, s3_asset_path=None):
        self.session_factory = session_factory
        self.client = self.session_factory().client('lambda')
        self.s3_asset_path = s3_asset_path

    def list_functions(self, prefix=None):
        p = self.client.get_paginator('list_functions')
        for rp in p.paginate():
            for f in rp.get('Functions', []):
                if not prefix:
                    yield f
                if f['FunctionName'].startswith(prefix):
                    yield f
    
    def publish(self, func, alias=None, role=None, s3_uri=None):
        #log.info('Publishing maid policy lambda function %s', func.name)

        result = self._create_or_update(func, role, s3_uri, qualifier=alias)
        if alias:
            func.alias = self.publish_alias(result, alias)

        for e in func.get_events(self.session_factory):
            if e.add(func):
                log.debug(
                    "Added event source: %s to function: %s",
                    e, func.alias)
        return result

    def remove(self, func, alias=None):
        log.info("Removing maid policy lambda function %s", func.name)
        for e in func.get_events(self.session_factory):
            e.remove(func)
        self.client.delete_function(FunctionName=func.name)

    def logs(self, func):
        logs = self.session_factory().client('logs')
        group_name = "/aws/lambda/%s" % func.name
        try:
            log_groups = logs.describe_log_groups(
                logGroupNamePrefix=group_name)
        except ClientError as e:
            return
        log_streams = logs.describe_log_streams(
            logGroupName=group_name,
            orderBy="LastEventTime", limit=3, descending=True)
        for s in reversed(log_streams['logStreams']):
            result = logs.get_log_events(
                logGroupName=group_name, logStreamName=s['logStreamName'])
            for e in result['events']:
                yield e
        
    @staticmethod
    def delta_function(lambda_func, func):
        conf = func.get_config()
        for k in conf:
            if conf[k] != lambda_func['Configuration'][k]:
                return True
            
    def _create_or_update(self, func, role=None, s3_uri=None, qualifier=None):
        role = func.role or role
        assert role, "Lambda function role must be specified"
        archive = func.get_archive()
        lfunc = self.get(func.name, qualifier)

        if s3_uri:
            # Todo support versioned buckets
            bucket, key = self._upload_func(s3_uri, func, archive)
            code_ref = {'S3Bucket': bucket, 'S3Key': key}
        else:
            code_ref = {'ZipFile': archive.get_bytes()}

        if lfunc:
            result = lfunc['Configuration']
            if archive.get_checksum() != lfunc['Configuration']['CodeSha256']:
                log.debug("Updating function %s code", func.name)
                params = dict(FunctionName=func.name, Publish=True)
                params.update(code_ref)
                result = self.client.update_function_code(**params)
            #else:
            #    log.debug("Code unchanged")
            # TODO/Consider also set publish above to false, and publish
            # after configuration change?
            if self.delta_function(lfunc, func):
                log.debug("Updating function: %s config" % func.name)
                result = self.client.update_function_configuration(**func.get_config())
            #else:
            #    log.debug("Config unchanged")
        else:
            params = func.get_config()
            params.update({'Publish': True, 'Code': code_ref})
            result = self.client.create_function(**params)

        return result

    def _upload_func(self, s3_uri, func, archive):
        _, bucket, key_prefix = parse_s3(s3_uri)
        key = "%s/%s" % (key_prefix, func.name)
        transfer = S3Transfer(
            self.session_factory().client('s3'),
            config=TransferConfig(
                multipart_threshold=1024*1024*4))
        transfer.upload_file(
            archive.path,
            bucket=bucket,
            key=key,
            extra_args={
                'ServerSideEncryption': 'AES256'})
        return bucket, key
    
    def publish_alias(self, func_data, alias):
        """Create or update an alias for the given function.
        """
        if not alias:
            return func_data['FunctionArn']
        func_name = func_data['FunctionName']
        func_version = func_data['Version']

        exists = resource_exists(
            self.client.get_alias, FunctionName=func_name, Name=alias)

        if not exists:
            log.debug("Publishing maid lambda alias %s", alias)            
            alias_result = self.client.create_alias(
                FunctionName=func_name,
                Name=alias,
                FunctionVersion=func_version)
        else:
            if (exists['FunctionVersion'] == func_version and
                exists['Name'] == alias):
                #log.debug("Alias unchanged")
                return exists['AliasArn']
            log.debug('Updating maid lambda alias %s', alias)
            alias_result = self.client.update_alias(
                FunctionName=func_name,
                Name=alias,
                FunctionVersion=func_version)
        return alias_result['AliasArn']
                        
    def get(self, func_name, qualifier=None):
        params = {'FunctionName': func_name}
        if qualifier:
            params['Qualifier'] = qualifier
        return resource_exists(
            self.client.get_function, **params)

    
def resource_exists(op, *args, **kw):
    try:
        return op(*args, **kw)
    except ClientError, e:
        if e.response['Error']['Code'] == "ResourceNotFoundException":
            return False
        raise
    
    
class LambdaFunction:

    __metaclass__ = abc.ABCMeta

    alias = None
    
    @abc.abstractproperty
    def name(self):
        """Name for the lambda function"""

    @abc.abstractproperty
    def runtime(self):
        """ """

    @abc.abstractproperty
    def description(self):
        """ """

    @abc.abstractproperty
    def handler(self):
        """ """

    @abc.abstractproperty
    def memory_size(self):
        """ """

    @abc.abstractproperty
    def timeout(self):
        """ """        

    @abc.abstractproperty
    def role(self):
        """ """                

    @abc.abstractmethod
    def get_events(self):
        """event sources that should be bound to this lambda."""
    
    @abc.abstractmethod
    def get_archive(self):
        """Return the lambda distribution archive object."""
        
    def get_config(self):
        return {
            'FunctionName': self.name,
            'MemorySize': self.memory_size,
            'Role': self.role,
            'Description': self.description,
            'Runtime': self.runtime,
            'Handler': self.handler,
            'Timeout': self.timeout}
        
        
PolicyHandlerTemplate = """\
from janitor import mu

def run(event, context):
    return mu.%s_handle(event, context)

"""


class PolicyLambda(LambdaFunction):

    handler = "maid_policy.run"
    runtime = "python2.7"
    timeout = 60
    
    def __init__(self, policy):
        self.policy = policy
        self.archive = maid_archive('*pyc')

    @property
    def name(self):
        return "maid-%s" % self.policy.name

    @property
    def description(self):
        return self.policy.data.get(
            'description', 'cloud-maid lambda policy')

    @property
    def role(self):
        return self.policy.data['mode'].get('role', '')

    @property
    def memory_size(self):
        return self.policy.data['mode'].get('memory', 512)

    def get_events(self, session_factory):
        events = []
        events.append(CloudWatchEventSource(
            self.policy.data['mode'], session_factory))
        return events
            
    def get_archive(self):
        self.archive.create()
        self.archive.add_contents(
            zinfo('config.json'),
            json.dumps({'policies': [self.policy.data]}, indent=2))
        self.archive.add_contents(
            zinfo('maid_policy.py'),
            PolicyHandlerTemplate % (
                self.policy.data['mode']['type'].replace('-', '_')))
        self.archive.close()
        return self.archive


def zinfo(fname):
    """Amazon lambda exec environment setup can break itself
    if zip files aren't constructed a particular way.

    ie. It respects file perm attributes including
    those that prevent lambda from working. Namely lambda
    extracts code as one user, and executes code as a different
    user without permissions for the executing user to read
    the file the lambda function is defacto broken. 

    Python's default zipfile.writestr does a 0600 perm which
    we modify here as a workaround.
    """
    info = zipfile.ZipInfo(fname)
    # Grant other users permissions to read
    info.external_attr = 0o644 << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


class CloudWatchEventSource(object):
    """
    Modeled loosely after kappa event source, such that it can be contributed
    post cloudwatch events ga or public beta.

    Event Pattern for Instance State

    .. code-block:: json

       { 
         "source": ["aws.ec2"],
         "detail-type": ["EC2 Instance State-change Notification"],
         "detail": { "state": ["pending"]}
       }

    Event Pattern for Cloud Trail API

    .. code-block:: json

       {
         "detail-type": ["AWS API Call via CloudTrail"],
         "detail": {
            "eventSource": ["s3.amazonaws.com"],
            "eventName": ["CreateBucket", "DeleteBucket"]
         }
       }
    """
    ASG_EVENT_MAPPING = {
        'launch-success': 'EC2 Instance Launch Successful',
        'launch-failure': 'EC2 Instance Launch Unsuccessful',
        'terminate-success': 'EC2 Instance Terminate Successful',
        'terminate-failure': 'EC2 Instance Terminate Unsuccessful'}
    
    def __init__(self, data, session_factory, prefix="maid-"):
        self.session_factory = session_factory
        self.client = self.session_factory().client('events')
        self.data = data
        self.prefix = prefix
        
    def _make_notification_id(self, function_name):
        if not function_name.startswith(self.prefix):
            return "%s%s" % (self.prefix, function_name)
        return function_name

    def get(self, rule_name):
        return resource_exists(
            self.client.describe_rule,
            Name=self._make_notification_id(rule_name))

    def delta(self, src, tgt):
        """Given two cwe rules determine if the configuration is the same.

        Name is already implied.
        """
        for k in ['State', 'EventPattern', 'ScheduleExpression']:
            if src.get(k) != tgt.get(k):
                return True
        return False

    def __repr__(self):
        return "<CWEvent Type:%s Sources:%s Events:%s>" % (
            self.data.get('type'),
            ', '.join(self.data.get('sources', [])),
            ', '.join(self.data.get('events', [])))
    
    def render_event_pattern(self):
        event_type = self.data.get('type')
        payload = {}
        if event_type == 'cloudtrail':
            payload['detail-type'] = ['AWS API Call via CloudTrail']
            payload['detail'] = {
                'eventSource': self.data.get('sources', []),
                'eventName': self.data.get('events', [])}
        elif event_type == "ec2-instance-state":
            payload['source'] = ['aws.ec2']
            payload['detail-type'] = [
                "EC2 Instance State-change Notifications"]
            # Technically could let empty be all events, but likely misconfig
            payload['detail'] = {"state": self.data.get('events', [])}
        elif event_type == "asg-instance-state":
            payload['source'] = ['aws.autoscaling']
            events = []
            for e in self.data.get('events', []):
                events.append(self.ASG_EVENT_MAPPING.get(e, e))
            payload['detail-type'] = events
        elif event_type == 'periodic':
            pass
        else:
            raise ValueError(
                "Unknown lambda event source type: %s" % event_type)
        if not payload:
            return None
        return json.dumps(payload, indent=2)
        
    def add(self, func):
        params = dict(
            Name=func.name,
            State='ENABLED')

        pattern = self.render_event_pattern()
        if pattern:
            params['EventPattern'] = pattern
            #log.debug("%s Event Pattern: %s", self, pattern)
        schedule = self.data.get('schedule')        
        if schedule:
            params['ScheduleExpression'] = schedule
        
        rule = self.get(func.name)
        if rule and self.delta(rule, params):
            log.debug("Updating cwe rule for %s" % self)            
            response = self.client.put_rule(**params)
        elif not rule:
            log.debug("Creating cwe rule for %s" % self)
            response = self.client.put_rule(**params)
            
        found = False
        response = self.client.list_targets_by_rule(Rule=func.name)
        for t in response['Targets']:
            if func.alias in t['Arn']:
                found = True

        if found:
            return

        log.debug('Creating cwe rule target found for %s' % self)
        self.client.put_targets(
            Rule=func.name,
            Targets=[
                {"Id": str(uuid.uuid4()),
                 "Arn": func.alias}]
            )
        return True
        
    def update(self, func):
        self.add(func)

    def pause(self, func):
        try:
            self.client.disable_rule(Rule=func.name)
        except ClientError as e:
            pass

    def resume(self, func):
        try:
            self.client.enable_rule(Rule=func.name)
        except ClientError as e:
            pass
        
    def remove(self, func):
        if self.get(func.name):
            self.client.delete_rule(
                Name=func.name,
            )
    