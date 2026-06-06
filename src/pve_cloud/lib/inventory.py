import os
import shutil
import socket
import subprocess

import paramiko
import yaml
from proxmoxer import ProxmoxAPI
from pve_cloud_schemas.validate import validate_cloud_dyn_inv

from pve_cloud.lib.validate import raise_on_py_cloud_missmatch

from pve_cloud.lib.ssh import check_ssh_open, check_ssh_open_tun
from types import SimpleNamespace


def get_avahi_iterator():
    avahi_disc = subprocess.run(
        ["avahi-browse", "-rpt", "_pxc._tcp"],
        stdout=subprocess.PIPE,
        text=True,
        check=True,
    )
    services = avahi_disc.stdout.splitlines()

    for service in services:
        if service.startswith("="):
            # avahi service def
            svc_args = service.split(";")
            host_name = svc_args[6].removesuffix(".local")
            host_ip = svc_args[7]

            cloud_domain = None
            cluster_name = None

            for txt_arg in svc_args[9].split():
                txt_arg = txt_arg.replace('"', "")
                if txt_arg.startswith("cloud_domain"):
                    cloud_domain = txt_arg.split("=")[1]

                if txt_arg.startswith("cluster_name"):
                    cluster_name = txt_arg.split("=")[1]

            if not cloud_domain or not cluster_name:
                raise ValueError(
                    f"Missconfigured proxmox cloud avahi service: {service}"
                )
            
            yield SimpleNamespace(host_name=host_name, host_ip=host_ip, cloud_domain=cloud_domain, cluster_name=cluster_name)
                

def get_cloud_domain(target_pve):
    inv_path = os.path.expanduser("~/.pve-cloud-dyn-inv.yaml")
    if os.path.exists(inv_path):
        with open(os.path.expanduser("~/.pve-cloud-dyn-inv.yaml"), "r") as f:
            pve_inventory = yaml.safe_load(f)

        validate_cloud_dyn_inv(pve_inventory)

        for pve_cloud in pve_inventory:
            for pve_cluster in pve_inventory[pve_cloud]:
                if pve_cluster + "." + pve_cloud == target_pve:
                    return pve_cloud

    if shutil.which("avahi-browse"):
        for service in get_avahi_iterator():
            if target_pve.endswith(service.cloud_domain):
                return service.cloud_domain

    raise Exception(f"Could not identify cloud domain for {target_pve}")


# returns the inventory containing all hosts of a pve cloud domain
# either from local inv file or via avahi
def get_pve_inventory(
    pve_cloud_domain,
    skip_py_cloud_check=False,
    fetch_other_pve_hosts=False,
):
    # first we try to load the manually created inventory via pvcli connect
    # as it takes precedence over avahi
    inv_path = os.path.expanduser("~/.pve-cloud-dyn-inv.yaml")
    if os.path.exists(inv_path):
        with open(inv_path, "r") as file:
            dynamic_inventory = yaml.safe_load(file)

        validate_cloud_dyn_inv(dynamic_inventory)

        if pve_cloud_domain in dynamic_inventory:
            # return the cloud domains inventory from here if we found it
            return dynamic_inventory[pve_cloud_domain]


    if shutil.which("avahi-browse"):
        pve_inventory = {}

        py_pve_cloud_performed_version_checks = set()

        # find cloud domain hosts and get first online per proxmox cluster
        cloud_domain_first_hosts = {}

        for service in get_avahi_iterator():
            # build inventory only for the current domain
            if service.cloud_domain == pve_cloud_domain:
                if service.cluster_name not in pve_inventory:
                    pve_inventory[service.cluster_name] = {
                        "pve_hosts": {}
                    }

                pve_inventory[service.cluster_name]["pve_hosts"][service.host_name] = {
                    "ansible_user": "root",
                    "ansible_host": service.host_ip,
                }

            # main pve cloud inventory
            if (
                service.cloud_domain == pve_cloud_domain
                and service.cluster_name not in cloud_domain_first_hosts
            ):
                if (
                    not skip_py_cloud_check
                    and f"{service.cluster_name}.{service.cloud_domain}"
                    not in py_pve_cloud_performed_version_checks
                ):
                    raise_on_py_cloud_missmatch(
                        service.host_ip
                    )  # validate that versions of dev machine and running on cluster match
                    py_pve_cloud_performed_version_checks.add(
                        f"{service.cluster_name}.{service.cloud_domain}"
                    )  # perform version check only once per cluster

                cloud_domain_first_hosts[service.cluster_name] = service.host_ip

        if not fetch_other_pve_hosts:
            return pve_inventory  # return without doing inter api call resolution

        # iterate over hosts and build pve inv via proxmox api
        # todo: this needs to be hugely optimized it blocks the grpc server
        # todo: this could maybe be refactored with pvcli init dyn inv functionality
        for cluster_first, first_host in cloud_domain_first_hosts.items():
            proxmox = ProxmoxAPI(first_host, user="root", backend="ssh_paramiko")

            cluster_name = None
            status_resp = proxmox.cluster.status.get()
            for entry in status_resp:
                if entry["id"] == "cluster":
                    cluster_name = entry["name"]
                break

            if cluster_name is None:
                raise RuntimeError("Could not get cluster name")

            if cluster_name != cluster_first:
                raise ValueError(
                    f"Proxmox cluster name missconfigured in avahi service {cluster_name}/{cluster_first}"
                )

            # fetch other hosts via api
            cluster_hosts = proxmox.nodes.get()

            for node in cluster_hosts:
                node_name = node["node"]

                if node["status"] == "offline":
                    print(f"skipping offline node {node_name}")
                    continue

                # get the main ip
                ifaces = proxmox.nodes(node_name).network.get()
                node_ip_address = None
                for iface in ifaces:
                    if "gateway" in iface:
                        if node_ip_address is not None:
                            raise RuntimeError(
                                f"found multiple ifaces with gateways for node {node_name}"
                            )
                        node_ip_address = iface.get("address")

                if node_ip_address is None:
                    raise RuntimeError(f"Could not find ip for node {node_name}")

                pve_inventory[cluster_name]["pve_hosts"][node_name] = {
                    "ansible_user": "root",
                    "ansible_host": node_ip_address,
                }

        return pve_inventory

    raise RuntimeError("Local pve inventory file missing (~/.pve-cloud-dyn-inv.yaml), execute `pvcli connect-cluster` or setup avahi mdns discovery!")
  

# find target cluster in loaded inventory
def get_target_cluster(pve_inventory, target_pve, target_cloud_domain=None):
    if not target_cloud_domain:
        target_cloud_domain = get_cloud_domain(target_pve)

    target_cluster = None

    for cluster in pve_inventory:
        if target_pve.endswith((cluster + "." + target_cloud_domain)):
            target_cluster = cluster
            break

    if not target_cluster:
        raise RecursionError(f"could not find target cluster for target pve {target_pve} in pve inventory!")


    return target_cluster
    

def get_online_jump_host(pve_inventory, target_cluster):
    online_jump_host = None
    if "jump_hosts" in pve_inventory[target_cluster]:
        # jump hosts for cluster configured => find an online one
        for jump_host in pve_inventory[target_cluster]["jump_hosts"]:
            if check_ssh_open(jump_host):
                online_jump_host = jump_host
                break

        if not online_jump_host:
            raise RuntimeError(f"No jump host of target cluster {target_cluster} is online / reachable!")
  
    return online_jump_host


def get_online_pve_host(pve_inventory, target_cluster):
    online_jump_host = get_online_jump_host(pve_inventory, target_cluster)
  
    online_pve_host = None

    jumpbox = None
    if online_jump_host:
        jumpbox = paramiko.SSHClient()
        jumpbox.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        jumpbox.connect(online_jump_host, username="root")

    for pve_host in pve_inventory[target_cluster]["pve_hosts"]:
        pve_host_ip = pve_inventory[target_cluster]["pve_hosts"][pve_host]["ansible_host"]

        if jumpbox:
            jumpbox_transport = jumpbox.get_transport()
            src_addr = ("127.0.0.1", 0)
            dest_addr = (pve_inventory[target_cluster]["pve_hosts"][pve_host]["ansible_host"], 22)
            jumpbox_channel = jumpbox_transport.open_channel(
                "direct-tcpip", dest_addr, src_addr
            )

            if check_ssh_open_tun(jumpbox_channel):
                online_pve_host = pve_host_ip
                break
        else:
            # direct connect
            if check_ssh_open(pve_host_ip):
                online_pve_host = pve_host_ip
                break

    
    if not online_pve_host:
        raise RuntimeError("Could not find online pve host for {target_cluster}")

    return online_pve_host, online_jump_host