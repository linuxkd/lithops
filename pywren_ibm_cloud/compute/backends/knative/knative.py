import os
import sys
import json
import time
import yaml
import logging
import http.client
from urllib.parse import urlparse
from kubernetes import client, config, watch
from pywren_ibm_cloud.utils import version_str
from pywren_ibm_cloud.version import __version__
from . import config as kconfig

import urllib3
urllib3.disable_warnings()

#Monkey patch for issue: https://github.com/kubernetes-client/python/issues/895
from kubernetes.client.models.v1_container_image import V1ContainerImage
def names(self, names):
    self._names = names
V1ContainerImage.names = V1ContainerImage.names.setter(names)

logger = logging.getLogger(__name__)


class KnativeServingBackend:
    """
    A wrap-up around Knative Serving APIs.
    """

    def __init__(self, knative_config):
        self.log_level = os.getenv('PYWREN_LOG_LEVEL')
        self.name = 'knative'
        self.knative_config = knative_config
        self.endpoint = self.knative_config.get('endpoint')
        self.service_hosts = {}

        # k8s config must be in ~/.kube/config or generate kube-config.yml file and
        # set env variable KUBECONFIG=<path-to-kube-confg>
        config.load_kube_config()
        self.api = client.CustomObjectsApi()
        self.v1 = client.CoreV1Api()

        self.headers = {'content-type': 'application/json'}

        log_msg = 'PyWren v{} init for Knative Serving - Endpoint: {}'.format(__version__, self.endpoint)
        logger.info(log_msg)
        if not self.log_level:
            print(log_msg)
        logger.debug('Knative Serving init for endpoint: {}'.format(self.endpoint))

    def _format_service_name(self, runtime_name, runtime_memory):
        runtime_name = runtime_name.replace('/', '--').replace(':', '--')
        return '{}--{}mb'.format(runtime_name, runtime_memory)

    def _unformat_service_name(self, action_name):
        runtime_name, memory = action_name.rsplit('--', 1)
        image_name = runtime_name.replace('--', '/', 1)
        image_name = image_name.replace('--', ':', -1)
        return image_name, int(memory.replace('mb', ''))

    def _get_default_runtime_image_name(self):
        docker_user = self.knative_config['docker_user']
        this_version_str = version_str(sys.version_info)
        if this_version_str == '3.5':
            image_name = kconfig.RUNTIME_DEFAULT_35
        elif this_version_str == '3.6':
            image_name = kconfig.RUNTIME_DEFAULT_36
        elif this_version_str == '3.7':
            image_name = kconfig.RUNTIME_DEFAULT_37
        return image_name.replace('<USER>', docker_user)

    def _get_service_host(self, service_name):
        """
        gets the service host needed for the invocation
        """
        # Check local cache
        if service_name in self.service_hosts:
            return self.service_hosts[service_name]
        else:
            try:
                svc = self.api.get_namespaced_custom_object(
                            group="serving.knative.dev",
                            version="v1alpha1",
                            name=service_name,
                            namespace="default",
                            plural="services"
                    )
                if svc is not None:
                    service_host = svc['status']['url'][7:]
                else:
                    raise Exception('Unable to get service details from {}'.format(service_name))
            except Exception as e:
                if json.loads(e.body)['code'] == 404:
                    log_msg = 'Knative service: resource "{}" Not Found'.format(service_name)
                    raise(log_msg)
                else:
                    raise(e)

            self.service_hosts[service_name] = service_host

            return service_host

    def _create_account_resources(self):
        """
        Creates the secret to access to the docker hub and the ServiceAcount
        """
        logger.debug("Creating Account resources: Secret and ServiceAccount")
        string_data = {'username': self.knative_config['docker_user'],
                       'password': self.knative_config['docker_token']}
        secret_res = yaml.safe_load(kconfig.secret_res)
        secret_res['stringData'] = string_data

        if self.knative_config['docker_repo'] != kconfig.DOCKER_REPO_DEFAULT:
            secret_res['metadata']['annotations']['tekton.dev/docker-0'] = self.knative_config['docker_repo']

        account_res = yaml.safe_load(kconfig.account_res)
        secret_res_name = secret_res['metadata']['name']
        account_res_name = account_res['metadata']['name']

        try:
            self.v1.delete_namespaced_secret(secret_res_name, 'default')
            self.v1.delete_namespaced_service_account(account_res_name, 'default')
        except Exception as e:
            if json.loads(e.body)['code'] == 404:
                log_msg = 'account resource Not Found - Not deleted'
                logger.debug(log_msg)

        self.v1.create_namespaced_secret('default', secret_res)
        self.v1.create_namespaced_service_account('default', account_res)

    def _create_build_resources(self):
        logger.debug("Creating Build resources: PipelineResource and Task")
        git_res = yaml.safe_load(kconfig.git_res)
        git_res_name = git_res['metadata']['name']

        task_def = yaml.safe_load(kconfig.task_def)
        task_name = task_def['metadata']['name']

        git_url_param = {'name': 'url', 'value': kconfig.GIT_URL_DEFAULT}
        git_rev_param = {'name': 'revision', 'value': kconfig.GIT_REV_DEFAULT}
        params = [git_url_param, git_rev_param]

        git_res['spec']['params'] = params

        try:
            self.api.delete_namespaced_custom_object(
                    group="tekton.dev",
                    version="v1alpha1",
                    name=task_name,
                    namespace="default",
                    plural="tasks",
                    body=client.V1DeleteOptions()
                )
        except Exception as e:
            if json.loads(e.body)['code'] == 404:
                log_msg = 'ksvc resource: {} Not Found'.format(task_name)
                logger.debug(log_msg)

        try:
            self.api.delete_namespaced_custom_object(
                    group="tekton.dev",
                    version="v1alpha1",
                    name=git_res_name,
                    namespace="default",
                    plural="pipelineresources",
                    body=client.V1DeleteOptions()
                )
        except Exception as e:
            if json.loads(e.body)['code'] == 404:
                log_msg = 'ksvc resource: {} Not Found'.format(git_res_name)
                logger.debug(log_msg)

        self.api.create_namespaced_custom_object(
                group="tekton.dev",
                version="v1alpha1",
                namespace="default",
                plural="pipelineresources",
                body=git_res
            )

        self.api.create_namespaced_custom_object(
                group="tekton.dev",
                version="v1alpha1",
                namespace="default",
                plural="tasks",
                body=task_def
            )

    def _build_docker_image_from_git(self, docker_image_name):
        """
        Builds the docker image and pushes it to the docker container registry
        """
        # TODO: Test if the image already exists

        self._create_account_resources()
        self._create_build_resources()

        task_run = yaml.safe_load(kconfig.task_run)
        image_url = {'name': 'imageUrl', 'value': '/'.join([self.knative_config['docker_repo'], docker_image_name])}
        task_run['spec']['inputs']['params'].append(image_url)
        #image_tag = {'name': 'imageTag', 'value':  __version__}
        #task_run['spec']['inputs']['params'].append(image_tag)

        task_run_name = task_run['metadata']['name']
        try:
            self.api.delete_namespaced_custom_object(
                    group="tekton.dev",
                    version="v1alpha1",
                    name=task_run_name,
                    namespace="default",
                    plural="taskruns",
                    body=client.V1DeleteOptions()
                )
        except Exception as e:
            if json.loads(e.body)['code'] == 404:
                log_msg = 'ksvc resource: {} Not Found'.format(task_run_name)
                logger.debug(log_msg)

        self.api.create_namespaced_custom_object(
                    group="tekton.dev",
                    version="v1alpha1",
                    namespace="default",
                    plural="taskruns",
                    body=task_run
                )

        pod_name = None
        w = watch.Watch()
        for event in w.stream(self.api.list_namespaced_custom_object, namespace='default',
                              group="tekton.dev", version="v1alpha1", plural="taskruns",
                              field_selector="metadata.name={0}".format(task_run_name), _request_timeout=10):
            if event['object'].get('status') is not None:
                pod_name = event['object']['status']['podName']
                w.stop()

        w = watch.Watch()
        for event in w.stream(self.v1.list_namespaced_pod, namespace='default',
                              field_selector="metadata.name={0}".format(pod_name), _request_timeout=120):
            if event['object'].status.phase == "Succeeded":
                w.stop()
            if event['object'].status.phase == "Failed":
                w.stop()
                raise Exception('Unable to create the Docker image from the git repository')

        self.api.delete_namespaced_custom_object(
                    group="tekton.dev",
                    version="v1alpha1",
                    name=task_run_name,
                    namespace="default",
                    plural="taskruns",
                    body=client.V1DeleteOptions()
                )

        logger.debug('Docker image created from git and uploaded to Dockerhub')

    def _create_service(self, docker_image_name, runtime_memory, timeout):
        """
        Creates a service in knative based on the docker_image_name and the memory provided
        """
        svc_res = yaml.safe_load(kconfig.service_res)

        service_name = self._format_service_name(docker_image_name, runtime_memory)
        svc_res['metadata']['name'] = service_name

        svc_res['spec']['template']['spec']['timeoutSeconds'] = timeout

        docker_image = '/'.join([self.knative_config['docker_repo'], docker_image_name])
        svc_res['spec']['template']['spec']['container']['image'] = docker_image

        svc_res['spec']['template']['spec']['container']['resources']['limits']['memory'] = '{}Mi'.format(runtime_memory)

        try:
            # delete the service resource if exists
            self.api.delete_namespaced_custom_object(
                    group="serving.knative.dev",
                    version="v1alpha1",
                    name=service_name,
                    namespace="default",
                    plural="services",
                    body=client.V1DeleteOptions()
                )
        except Exception as e:
            if json.loads(e.body)['code'] == 404:
                log_msg = 'Knative service: resource "{}" Not Found'.format(service_name)
                logger.debug(log_msg)

        # create the service resource
        self.api.create_namespaced_custom_object(
                group="serving.knative.dev",
                version="v1alpha1",
                namespace="default",
                plural="services",
                body=svc_res
            )

        w = watch.Watch()
        for event in w.stream(self.api.list_namespaced_custom_object,
                              namespace='default', group="serving.knative.dev",
                              version="v1alpha1", plural="services",
                              field_selector="metadata.name={0}".format(service_name),
                              _request_timeout=120):
            conditions = None
            if event['object'].get('status') is not None:
                conditions = event['object']['status']['conditions']
                if event['object']['status'].get('url') is not None:
                    service_url = event['object']['status']['url']
            if conditions and conditions[0]['status'] == 'True' and \
               conditions[1]['status'] == 'True' and conditions[2]['status'] == 'True':
                w.stop()

        log_msg = 'Runtime Service resource created - URL: {}'.format(service_url)
        logger.debug(service_url)

        self.service_hosts[service_name] = service_url[7:]

        return service_url

    def create_runtime(self, docker_image_name, memory, timeout=kconfig.RUNTIME_TIMEOUT_DEFAULT):
        """
        Creates a new runtime into the knative default namespace from an already built Docker image.
        As knative does not have a default image already published in a docker registry, pywren
        has to build it in the docker hub account provided by the user. So when the runtime docker
        image name is not provided by the user in the config, pywren will build the default from git.
        """
        default_runtime_img_name = self._get_default_runtime_image_name()
        if docker_image_name in ['default', default_runtime_img_name]:
            # We only build default image. rest of images must already exist
            # in the docker registry.
            self._build_docker_image_from_git(default_runtime_img_name)

        service_url = self._create_service(docker_image_name, memory, timeout)

        if self.endpoint is None:
            self.endpoint = service_url

        runtime_meta = self._generate_runtime_meta(docker_image_name, memory)

        return runtime_meta

    def build_runtime(self, docker_image_name, dockerfile):
        """
        Builds a new runtime from a Docker file and pushes it to the Docker hub
        """
        logger.info('Creating a new docker image from Dockerfile')
        logger.info('Docker image name: {}'.format(docker_image_name))

        if dockerfile:
            cmd = 'docker build -t {} -f {} .'.format(docker_image_name, dockerfile)
        else:
            cmd = 'docker build -t {} .'.format(docker_image_name)

        res = os.system(cmd)
        if res != 0:
            exit()

        cmd = 'docker push {}'.format(docker_image_name)
        res = os.system(cmd)
        if res != 0:
            exit()

    def delete_runtime(self, docker_image_name, memory):
        service_name = self._format_service_name(docker_image_name, memory)
        logger.info('Deleting runtime: {}'.format(service_name))
        try:
            self.api.delete_namespaced_custom_object(
                    group="serving.knative.dev",
                    version="v1alpha1",
                    name=service_name,
                    namespace="default",
                    plural="services",
                    body=client.V1DeleteOptions()
                )
        except Exception as e:
            if json.loads(e.body)['code'] == 404:
                log_msg = 'Knative service: resource "{}" Not Found'.format(service_name)
                logger.debug(log_msg)

    def delete_all_runtimes(self):
        #TODO
        pass

    def list_runtimes(self, docker_image_name='all'):
        """
        List all the runtimes deployed in the IBM CF service
        return: list of tuples [docker_image_name, memory]
        """
        #TODO
        runtimes = [[docker_image_name, 0]]
        return runtimes

    def invoke(self, docker_image_name, memory, payload):
        """
        Invoke -- return information about this invocation
        """
        service_name = self._format_service_name(docker_image_name, memory)

        self.headers['Host'] = self._get_service_host(service_name)

        exec_id = payload.get('executor_id', '')
        call_id = payload.get('call_id', '')
        job_id = payload.get('job_id', '')
        route = payload.get("service_route", '/')

        try:
            start = time.time()
            parsed_url = urlparse(self.endpoint)
            conn = http.client.HTTPConnection(parsed_url.netloc, timeout=600)
            conn.request("POST", route,
                         body=json.dumps(payload),
                         headers=self.headers)
            resp = conn.getresponse()
            resp_status = resp.status
            resp_data = resp.read()
            conn.close()
            roundtrip = time.time() - start
            resp_time = format(round(roundtrip, 3), '.3f')

            try:
                data = json.loads(resp_data.decode("utf-8"))
            except Exception:
                raise Exception('Response from invocation is not a dict: {}'.format(resp_data))

            if resp_status in [200, 202]:
                log_msg = ('ExecutorID {} - Function {} invocation done! ({}s) '
                           .format(exec_id, call_id, resp_time))
                logger.debug(log_msg)
                return exec_id + job_id + call_id, data
            else:
                logger.debug(data)
                if resp_status == 404:
                    raise Exception('Service Not Found')
                else:
                    raise Exception(resp_status, resp_data)

        except Exception as e:
            raise e
            conn.close()
            log_msg = ('ExecutorID {} - Function {} invocation failed: {}'.format(exec_id, call_id, str(e)))
            logger.debug(log_msg)

    def invoke_with_result(self, docker_image_name, memory, payload={}):
        """
        Invoke waiting for a result -- return information about this invocation
        """
        return self.invoke(docker_image_name, memory, payload)

    def get_runtime_key(self, docker_image_name, runtime_memory):
        """
        Method that creates and returns the runtime key.
        Runtime keys are used to uniquely identify runtimes within the storage,
        in order to know which runtimes are installed and which not.
        """
        service_name = self._format_service_name(docker_image_name, runtime_memory)
        parsed_url = urlparse(self.endpoint)
        runtime_key = os.path.join(parsed_url.netloc, service_name)

        return runtime_key

    def _generate_runtime_meta(self, docker_image_name, memory):
        """
        Extract installed Python modules from docker image
        """
        payload = {}

        payload['service_route'] = "/preinstalls"
        logger.debug("Extracting Python modules list from: {}".format(docker_image_name))
        try:
            _, runtime_meta = self.invoke_with_result(docker_image_name, memory, payload)
        except Exception as e:
            raise Exception("Unable to invoke 'modules' action {}".format(e))

        if not runtime_meta or 'preinstalls' not in runtime_meta:
            raise Exception(runtime_meta)

        return runtime_meta