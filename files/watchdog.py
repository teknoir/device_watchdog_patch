import argparse
import logging
import socket
import subprocess
import sys
import time
import yaml
import json

import kubernetes
from kubernetes.stream import stream
from kubernetes.stream.ws_client import ERROR_CHANNEL
import systemd_watchdog
from statemachine import StateMachine, State

INTERVAL = 60
ERRORS = 20

class WatchdogSM(StateMachine):

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)
        super(WatchdogSM, self).__init__()
        self.wait_count = 0

    uninitialized = State('Uninitialized', initial=True)

    running = State('Idle')
    checkping = State('CheckPing')
    checktoe = State('CheckTOE')
    checkkubernetesapi = State('CheckK8sAPI')

    reboot = State('Reboot')

    progress = uninitialized.to(running)

    run = running.to.itself()

    healthcheck = running.to(checkping) | \
                  checkping.to(checktoe) | \
                  checktoe.to(checkkubernetesapi) | \
                  checkkubernetesapi.to(running)

    degress = reboot.to.itself()

    error = checkping.to(checktoe) | \
            checktoe.to(checkkubernetesapi) | \
            checkkubernetesapi.to(running) | \
            uninitialized.to.itself() | \
            reboot.to.itself()

    escalate = reboot.from_(checkping, checktoe, checkkubernetesapi)

    def on_run(self):
        # self.logger.info(f'Healtcheck wait count: {self.wait_count}')
        self.wait_count += 1


class Watchdog(object):

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)
        self.sm = WatchdogSM(**kwargs)
        self.e_checkping = 0
        self.e_checktoe = 0
        self.e_checkkubernetesapi = 0

    def tick(self):
        self.logger.info(self.sm.current_state.name)

        if self.sm.current_state == self.sm.uninitialized:
            self.sm.progress()

        #######################################################################################################################

        elif self.sm.current_state == self.sm.running:
            if self.sm.wait_count >= self.interval:
                self.sm.wait_count = 0
                self.sm.healthcheck()
            else:
                self.sm.run()

        elif self.sm.current_state == self.sm.checkping:
            if self.wrap_action(self.checkping):
                self.sm.healthcheck()
                self.e_checkping = 0
            else:
                self.e_checkping += 1
                self.logger.warning(f'{self.sm.current_state.name} error no {self.e_checkping}')
                self.sm.escalate() if self.e_checkping >= self.errors else self.sm.error()

        elif self.sm.current_state == self.sm.checktoe:
            if self.wrap_action(self.checktoe):
                self.sm.healthcheck()
                self.e_checktoe = 0
            else:
                self.e_checktoe += 1
                self.logger.warning(f'{self.sm.current_state.name} error no {self.e_checktoe}')
                self.sm.escalate() if self.e_checktoe >= self.errors else self.sm.error()

        elif self.sm.current_state == self.sm.checkkubernetesapi:
            if self.wrap_action(self.checkkubernetesapi):
                self.sm.healthcheck()
                self.e_checkkubernetesapi = 0
            else:
                self.e_checkkubernetesapi += 1
                self.logger.warning(f'{self.sm.current_state.name} error no {self.e_checkkubernetesapi}')
                self.sm.escalate() if self.e_checkkubernetesapi >= self.errors else self.sm.error()

        #######################################################################################################################

        elif self.sm.current_state == self.sm.reboot:
            if self.wrap_action(self.reboot):
                self.sm.degress()
            else:
                self.sm.error()

    def wrap_action(self, action):
        try:
            return action()
        except:
            e = sys.exc_info()[0]
            self.logger.warning("Exception: %s" % e)
            return False

    def reboot(self):
        self.logger.warning(f'Rebooting the device')
        proc = subprocess.run(['reboot'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            self.logger.warning(f'The exit code was: {proc.returncode}')
            return False
        return True

    def checkping(self):
        try:
            socket.create_connection(("1.1.1.1", 53))
            return True
        except OSError:
            pass
        self.logger.warning('Cannot ping IP: 1.1.1.1 on port 53')
        return False

    def checktoe(self):
        kubernetes.config.load_kube_config("/etc/rancher/k3s/k3s.yaml")
        try:
            api_instance = kubernetes.client.CoreV1Api()
            label_selector = "app=toe"
            api_response = api_instance.list_pod_for_all_namespaces(watch=False, label_selector=label_selector)
            toe = next(iter(api_response.items), None)
            if toe == None:
                self.logger.warning(f'There is no TOE POD')
                return False
            toe_container_status = next(iter(toe.status.container_statuses), None)
            if toe_container_status == None:
                self.logger.warning(f'There is no container status for TOE POD')
                return False
            self.logger.info(f'TOE POD NAME: {toe_container_status.name}, Ready: {str(toe_container_status.ready)}')
            return toe_container_status.ready
        except kubernetes.client.rest.ApiException as e:
            self.logger.warning('Found exception in reading TOE pod')
        return False

    def checkkubernetesapi(self):
        kubernetes.config.load_kube_config("/etc/rancher/k3s/k3s.yaml")
        try:
            api_instance = kubernetes.client.CoreV1Api()
            name = 'healtcheck'
            namespace = 'kube-system'
            resp = None
            try:
                resp = api_instance.read_namespaced_pod(name=name, namespace=namespace)
            except kubernetes.client.rest.ApiException as e:
                if e.status != 404:
                    logger.info(f"Unknown error: {e}")
                    return False
            if not resp:
                logger.info(f"Pod {name} does not exist. Creating it...")
                pod_manifest = {
                    'apiVersion': 'v1',
                    'kind': 'Pod',
                    'metadata': {
                        'name': name
                    },
                    'spec': {
                        'containers': [{
                            'image': 'busybox',
                            'name': 'sleep',
                            "args": [
                                "/bin/sh",
                                "-c",
                                "while true;do date;sleep 5; done"
                            ]
                        }]
                    }
                }
                resp = api_instance.create_namespaced_pod(body=pod_manifest, namespace=namespace)
                while True:
                    resp = api_instance.read_namespaced_pod(name=name, namespace=namespace)
                    if resp.status.phase != 'Pending':
                        break
                    time.sleep(1)
                logger.info(f"Pod {name} created")

            # Calling exec and waiting for response
            exec_command = [
                'nc', '-zvn', '-w', '2', 'kubernetes.default', '443']
            client = stream(api_instance.connect_get_namespaced_pod_exec,
                            name,
                            namespace,
                            command=exec_command,
                            stderr=True, stdin=False,
                            stdout=True, tty=False, _preload_content=False)

            client.run_forever(timeout=10)
            err = client.read_channel(ERROR_CHANNEL)
            result = yaml.safe_load(err)
            if result['status'] != "Success":
                logger.error(f'Failed to run command "{" ".join(exec_command)}"')
                logger.error('Reason: ' + result['reason'])
                logger.error('Message: ' + result['message'])
                logger.error('Details: ' + ';'.join(map(lambda x: json.dumps(x), result['details']['causes'])))
                return False

        except kubernetes.client.rest.ApiException as e:
            logger.warning(f'Exception during healthcheck of kubernetes API: {e}')
            return False

        return True

def parse_args():
    # Parse input arguments
    desc = 'Teknoir Device Watchdog'
    parser = argparse.ArgumentParser(description=desc)
    parser.add_argument('--interval', dest='interval',
                        help='Set interval in seconds between check',
                        default=INTERVAL, type=int)
    parser.add_argument('--errors', dest='errors',
                        help='Set number of errors to reboot',
                        default=ERRORS, type=int)
    args = parser.parse_args()
    return args


if __name__ == '__main__':
    args = parse_args()
    logger = logging.getLogger('tnw')
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    formatter = logging.Formatter('[%(name)s] [%(levelname)s] %(message)s')
    ch.setFormatter(formatter)
    logger.addHandler(ch)
    logger.setLevel(logging.INFO)

    logger.info("TΞꓘN01R")
    logger.info("TΞꓘN01R")
    logger.info("TΞꓘN01R")
    logger.info("TΞꓘN01R")

    wd = systemd_watchdog.watchdog()
    if not wd.is_enabled:
        # Then it's probably not running is systemd with watchdog enabled
        raise Exception("Watchdog not enabled")

    watchdog = Watchdog(logger=logger, wd=wd, **args.__dict__)

    # Report that the program init is complete
    wd.ready()
    wd.status("Init is complete...")
    wd.notify()

    wd.status("Starting up watchdog...")
    while True:
        watchdog.tick()
        time.sleep(1)
        wd.notify()
