import argparse
import logging
import socket
import subprocess
import sys
import time
import yaml
import json
import psutil
import os
import paho.mqtt.client as mqtt 
import threading
import kubernetes
import datetime
from kubernetes.stream import stream
from kubernetes.stream.ws_client import ERROR_CHANNEL
import systemd_watchdog
import jwt
from statemachine import StateMachine, State
import ssl
from OpenSSL import crypto

INTERVAL = 5
ERRORS = 10000

class MetricsManager:
    def __init__(self):
        self.metrics = {
            "device_watchdog_checkping_failures": {
                "metricId": "device_watchdog_checkping_failures",
                "description": "Number of checkping failures",
                "labels": {},
                "value": 0
            },
            "device_watchdog_checknetwork_failures": {
                "metricId": "device_watchdog_checknetwork_failures",
                "description": "Number of checknetwork failures",
                "labels": {},
                "value": 0
            },
            "device_watchdog_checktoe_failures": {
                "metricId": "device_watchdog_checktoe_failures",
                "description": "Number of checktoe failures",
                "labels": {},
                "value": 0
            },
            "device_watchdog_checkkubernetesapi_failures": {
                "metricId": "device_watchdog_checkkubernetesapi_failures",
                "description": "Number of checkkubernetesapi failures",
                "labels": {},
                "value": 0
            },
            "device_watchdog_net_tx": {
                "metricId": "device_watchdog_net_tx",
                "description": "Network traffic sent",
                "labels": {},
                "value": 0
            },
            "device_watchdog_net_rx": {
                "metricId": "device_watchdog_net_rx",
                "description": "Network traffic received",
                "labels": {},
                "value": 0
            }
        }

    def increment(self, metric_id, value=1):
        if metric_id in self.metrics:
            self.metrics[metric_id]["value"] += value
        else:
            raise KeyError(f"Metric {metric_id} not found. Please set it first.")

    def set(self, metric_id, description, labels, value):
        key = (metric_id, frozenset(labels.items()))
        self.metrics[key] = {
            "metricId": metric_id,
            "description": description,
            "labels": labels,
            "value": value
        }

    def to_json(self):
        return json.dumps({
            "apiVersion": "teknoir.org/v1",
            "kind": "Metrics",
            "metadata": {
                "name": "DeviceMetrics"
            },
            "spec": {
                "metrics": list(self.metrics.values())
            }
        })

class MQTTClient:
    def __init__(self, broker, port, topic, logger, client_id=None, username=None, password=None):
        self.broker = broker
        self.port = port
        self.topic = topic
        self.logger = logger
        self.client = mqtt.Client(client_id=client_id)
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_publish = self.on_publish

        if username and password:
            self.client.username_pw_set(username, password)

    def on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            logger.info("Connected to MQTT Broker!")
        else:
            logger.info(f"Failed to connect, return code {rc}")

    def on_disconnect(self, client, userdata, rc):
        logger.info("Disconnected from MQTT Broker")

    def on_publish(self, client, userdata, mid):
        # logger.info(f"Message {mid} published")
        pass

    def connect(self):
        self.client.connect(self.broker, self.port)
        self.client.loop_start()

    def publish(self, message):
        result = self.client.publish(self.topic, message)
        status = result.rc
        if status == 0:
            pass
            logger.info(f"Sent succesfully to topic `{self.topic}`")
        else:
            logger.info(f"Failed to send message to topic {self.topic}")

class BackupTunnel:
    def __init__(self, tunnel_cmd, check_interval=60, logger=None):
        self.tunnel_cmd = tunnel_cmd
        self.check_interval = check_interval
        self.process = None
        self.logger = logger

    def start_tunnel(self):
        self.logger.info("Starting backup tunnel...")
        try:
            self.process = subprocess.Popen(
                self.tunnel_cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=os.setsid  # Group the process for easier termination
            )
            self.logger.info(f"Backup tunnel started with PID: {self.process.pid}")
            threading.Thread(target=self.log_output, args=(self.process.stderr, self.logger.error)).start()
        except Exception as e:
            self.logger.error(f"Failed to start tunnel: {e}")
    
    def log_output(self, pipe, log_func):
        with pipe:
            for line in iter(pipe.readline, b''):
                log_func(line.decode().strip())

    def is_tunnel_running(self):
        if not self.process:
            return False
        return psutil.pid_exists(self.process.pid) and self.process.poll() is None

    def monitor_tunnel(self):
        while True:
            if not self.is_tunnel_running():
                self.logger.warning("Backup tunnel is not running. Restarting...")
                self.start_tunnel()
            else:
                self.logger.info("Backup tunnel is running.")
            time.sleep(self.check_interval)

    def stop_tunnel(self):
        if self.process and self.is_tunnel_running():
            self.logger.info("Stopping backup tunnel...")
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
                self.process.wait(timeout=10)
                self.logger.info("Backup tunnel stopped.")
            except Exception as e:
                self.logger.error(f"Failed to stop tunnel: {e}")

    def run(self):
        try:
            self.start_tunnel()
            self.monitor_tunnel()
        finally:
            self.stop_tunnel()


class WatchdogSM(StateMachine):
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)
        super(WatchdogSM, self).__init__()
        self.wait_count = 0

    uninitialized = State('Uninitialized', initial=True)

    running = State('Idle')
    checkping = State('CheckPing')
    checknetwork = State('CheckNetwork')
    checktoe = State('CheckTOE')
    checkkubernetesapi = State('CheckK8sAPI')

    reboot = State('Reboot')

    progress = uninitialized.to(running)

    run = running.to.itself()

    healthcheck = running.to(checkping) | \
                  checkping.to(checknetwork) | \
                  checknetwork.to(checktoe) | \
                  checktoe.to(checkkubernetesapi) | \
                  checkkubernetesapi.to(running)

    degress = reboot.to.itself()

    error = checkping.to(checknetwork) | \
            checknetwork.to(checktoe) | \
            checktoe.to(checkkubernetesapi) | \
            checkkubernetesapi.to(running) | \
            uninitialized.to.itself() | \
            reboot.to.itself()

    escalate = reboot.from_(checkping, checknetwork, checktoe, checkkubernetesapi)

    def on_run(self):
        # self.logger.info(f'Healtcheck wait count: {self.wait_count}')
        self.wait_count += 1


class Watchdog(object):

    def __init__(self, mqtt_client, metrics_manager, publish_interval=30, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)
        self.sm = WatchdogSM(**kwargs)
        self.e_checkping = 0
        self.e_checktoe = 0
        self.e_checkkubernetesapi = 0
        self.e_checknetwork = 0
        self.mqtt_client = mqtt_client
        self.metrics_manager = metrics_manager
        self.publish_interval = publish_interval
        self.tick_counter = 0

    def get_network_activity(self, interval=1):
        net1 = psutil.net_io_counters(pernic=True)
        time.sleep(interval)
        net2 = psutil.net_io_counters(pernic=True)
        
        ext_tx_bytes = 0
        ext_rx_bytes = 0

        # Get the list of available network interfaces
        interfaces = psutil.net_if_stats().keys()

        for iface in interfaces:
            if iface in net1 and iface in net2:
                tx_bytes = net2[iface].bytes_sent - net1[iface].bytes_sent
                rx_bytes = net2[iface].bytes_recv - net1[iface].bytes_recv
                ext_tx_bytes += tx_bytes
                ext_rx_bytes += rx_bytes

                # Add metrics for each interface
                self.metrics_manager.set(
                    metric_id="device_watchdog_net_tx",
                    description="Transmitted bytes on interface",
                    labels={"interface": iface},
                    value=tx_bytes
                )
                self.metrics_manager.set(
                    metric_id="device_watchdog_net_rx",
                    description="Received bytes on interface",
                    labels={"interface": iface},
                    value=rx_bytes
                )

        # Add aggregated metrics
        self.metrics_manager.set(
            metric_id="device_watchdog_net_tx_total",
            description="Total transmitted bytes on external interfaces",
            labels={},
            value=ext_tx_bytes
        )
        self.metrics_manager.set(
            metric_id="device_watchdog_net_rx_total",
            description="Total received bytes on external interfaces",
            labels={},
            value=ext_rx_bytes
        )

        # self.logger.info(f"Network Speed - Sent: {ext_tx_bytes / 1024:.2f} KB/s, Received: {ext_rx_bytes / 1024:.2f} KB/s")
        self.network_speed = (ext_tx_bytes, ext_rx_bytes)

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
                self.metrics_manager.increment("device_watchdog_checkping_failures")
                self.sm.escalate() if self.e_checkping >= self.errors else self.sm.error()

        elif self.sm.current_state == self.sm.checknetwork:
            if self.wrap_action(self.checknetwork):
                self.sm.healthcheck()
                self.e_checknetwork = 0
            else:
                self.e_checknetwork += 1
                self.logger.warning(f'{self.sm.current_state.name} error no {self.e_checknetwork}')
                self.metrics_manager.increment("device_watchdog_checknetwork_failures")
                self.sm.escalate() if self.e_checknetwork >= self.errors else self.sm.error()

        elif self.sm.current_state == self.sm.checktoe:
            if self.wrap_action(self.checktoe):
                self.sm.healthcheck()
                self.e_checktoe = 0
            else:
                self.e_checktoe += 1
                self.logger.warning(f'{self.sm.current_state.name} error no {self.e_checktoe}')
                self.metrics_manager.increment("device_watchdog_checktoe_failures")
                self.sm.escalate() if self.e_checktoe >= self.errors else self.sm.error()

        elif self.sm.current_state == self.sm.checkkubernetesapi:
            if self.wrap_action(self.checkkubernetesapi):
                self.sm.healthcheck()
                self.e_checkkubernetesapi = 0
            else:
                self.e_checkkubernetesapi += 1
                self.logger.warning(f'{self.sm.current_state.name} error no {self.e_checkkubernetesapi}')
                self.metrics_manager.increment("device_watchdog_checkkubernetesapi_failures")
                self.sm.escalate() if self.e_checkkubernetesapi >= self.errors else self.sm.error()

        #######################################################################################################################

        elif self.sm.current_state == self.sm.reboot:
            if self.wrap_action(self.reboot):
                self.sm.degress()
            else:
                self.sm.error()
        
        
        self.tick_counter += 1
        if self.tick_counter >= self.publish_interval:
            self.get_network_activity()
            self.publish_metrics()
            self.tick_counter = 0

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

    def publish_metrics(self):
        metrics_message = self.metrics_manager.to_json()
        self.mqtt_client.publish(metrics_message)

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
    
    def checknetwork(self):
        try:
            connections = psutil.net_connections(kind='inet')
            if not connections:
                self.logger.info("No network connections found.")
            for conn in connections:
                self.logger.info(f"Connection: {conn.laddr} -> {conn.raddr}, Status: {conn.status}")
            return True
        except Exception as e:
            self.logger.error(f"Error retrieving network connections: {e}. Probably not running as root!")
        return False

def load_config(config_path=None):
    default_config_path = "/etc/teknoir/values.yaml"
    config_file_path = config_path or default_config_path

    default_config = {
        "toe": {
            "deviceID": "teknir_generic_device",
            "teamSpace": "demonstrations",
            "pullSecret": "",
            "gcpProject": "gke_teknoir-poc_us-central1-c_teknoir-dev-cluster",
            "configHostPath": "/etc/teknoir",
            "defaultNamespace": "teknoir"
        }
    }

    if os.path.exists(config_file_path):
        with open(config_file_path, 'r') as file:
            config = yaml.safe_load(file)
            return config
    else:
        return default_config

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
    parser.add_argument('--local', dest='local',
                        help='Use this if you are testing or running outside of systemd',
                        action='store_true')
    args = parser.parse_args()
    return args

def load_ca_cert(ca_cert_path):
    with open(ca_cert_path, 'r') as f:
        ca_cert = f.read()
    certpool = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH).load_verify_locations(cafile=ca_cert_path)
    return certpool

def create_tls_config(certpool):
    tls_config = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    tls_config.load_verify_locations(cafile=certpool)
    tls_config.check_hostname = False
    tls_config.verify_mode = ssl.CERT_NONE
    return tls_config

def generate_jwt_token(project_id, private_key_path, expiration_hours):
    with open(private_key_path, 'r') as f:
        private_key = f.read()
    token = jwt.encode(
        {
            'iat': datetime.datetime.now(),
            'exp': datetime.datetime.now() + datetime.timedelta(hours=expiration_hours),
            'aud': project_id
        },
        private_key,
        algorithm='RS256'
    )
    return token

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
    if not wd.is_enabled and not args.local:
        # Then it's probably not running in systemd with watchdog enabled or running locally
        raise Exception("Watchdog not enabled")

    config = load_config(os.getenv('CONFIG_PATH'))
    logger.info(f"Loaded config: {config}") 

    gcp_project = config['toe']['gcpProject']
    if gcp_project in ["gke_teknoir-poc_us-central1-c_teknoir-dev-cluster", "teknoir-poc"]:
        ssh_host = "deadend.teknoir.dev"
    else:
        ssh_host = "deadend.teknoir.cloud"

    private_key_path = os.path.join(config['toe']['configHostPath'], "rsa_private.pem")

    TUNNEL_CMD = (
        "ssh -v -o UserKnownHostsFile=/dev/null "
        "-o StrictHostKeyChecking=no -o ExitOnForwardFailure=yes "
        "-o ServerAliveInterval=60 -i {private_key_path} "
        f"-N -R 49322:localhost:22 teknoir@{ssh_host} -p 2222"
    )

    if args.local:
        mqtt_client = MQTTClient(
            broker="localhost",
            port=18831,
            topic="/devices/cristian/state",
            logger=logger,
            client_id="watchdog-cristian",
            username="emqx_operator_controller",
            password="mbaoiatkynes9b6ttgpoufw6k7sn5azmpvqpgrgjut0rwfxds8a9u7wylqqhk0lf"
        )
    
    mqtt_client.connect()

    metrics_manager = MetricsManager()

    
    tunnel_manager = BackupTunnel(tunnel_cmd=TUNNEL_CMD, check_interval=30, logger=logger)
    watchdog = Watchdog(mqtt_client=mqtt_client, metrics_manager=metrics_manager, logger=logger, wd=wd, **args.__dict__)

    # Start BackupTunnel in a separate thread
    # tunnel_thread = threading.Thread(target=tunnel_manager.run)
    # tunnel_thread.start()

    # Report that the program init is complete
    wd.ready()
    wd.status("Init is complete...")
    wd.notify()

    wd.status("Starting up watchdog...")
    while True: 
        watchdog.tick()
        time.sleep(1)
        wd.notify()
