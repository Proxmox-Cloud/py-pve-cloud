import argparse
import os

import paramiko
import pve_cloud._version as pxc_version
import rpyc
import yaml
from fabric import Connection
from proxmoxer import ProxmoxAPI
from pve_cloud_schemas.validate import validate_cloud_dyn_inv

from pve_cloud.cli.pvclu import (get_ssh_master_kubeconfig,
                                 get_ssh_remote_master_kubeconfig)
from pve_cloud.cli.pxrpc import launch_pxrpc
from pve_cloud.lib.inventory import *

inv_path = os.path.expanduser("~/.pve-cloud-dyn-inv.yaml")


def init_dyn_inv():
    # try load current dynamic inventory
    if os.path.exists(inv_path):
        with open(inv_path, "r") as file:
            dynamic_inventory = yaml.safe_load(file)
        validate_cloud_dyn_inv(dynamic_inventory)
    else:
        # initialize empty
        dynamic_inventory = {}

    return dynamic_inventory


# todo: rpyc is probably overkill here and could instead use remove pvesh get command
# either remove in the future or other good usecases are found. terraform prov forwarding?
def connect_remote_cluster(args):
    dynamic_inventory = init_dyn_inv()

    # during setup we assume all jump hosts are online
    host = args.jump_hosts.split(",")[0]

    with launch_pxrpc(
        host, args.pve_host, init_venv=True, local_pypi_ip=args.local_pypi_ip
    ) as (pxrpc, pve_host):
        # first we check if the cluster is already part of a proxmox cloud
        result = pve_host.run("cat /etc/pve/cloud/cluster_vars.yaml")
        cluster_vars = yaml.safe_load(result.stdout.strip())

        if not cluster_vars:
            # cluster has not been yet initialized
            if not args.pve_cloud_domain:
                pve_cloud_domain = input(
                    "Cluster has not yet been fully initialized, assign the cluster a cloud domain and press ENTER:"
                )
            else:
                pve_cloud_domain = args.pve_cloud_domain
        else:
            pve_cloud_domain = cluster_vars["pve_cloud_domain"]

        # init cloud domain if not there
        if pve_cloud_domain not in dynamic_inventory:
            dynamic_inventory[pve_cloud_domain] = {}

        cluster_name = pxrpc.root.get_pve_cluster_name()
        print("pve cluster name", cluster_name)

        if cluster_name in dynamic_inventory[pve_cloud_domain] and not args.force:
            print(
                f"cluster {cluster_name} already in dynamic inventory, add --force to overwrite current local inv."
            )
            return

        # overwrite on force / create fresh
        dynamic_inventory[pve_cloud_domain][cluster_name] = {
            "pve_hosts": {},
            "jump_hosts": args.jump_hosts.split(","),
        }

        # not present => add and safe the dynamic inventory
        cluster_hosts = pxrpc.root.get_nodes()
        print("cluster_hosts", cluster_hosts)

        for node in cluster_hosts:
            node_name = node["node"]

            if node["status"] == "offline":
                print(f"skipping offline node {node_name}")
                continue

            # get the main ip
            ifaces = pxrpc.root.get_node_network(node_name)
            print("ifaces", ifaces)
            node_ip_address = None
            for iface in ifaces:
                # when specified only take the host ip from the special iface
                if args.host_iface:
                    if iface["iface"] == args.host_iface:
                        node_ip_address = iface["address"]
                        break
                else:
                    if (
                        "gateway" in iface
                    ):  # otherwise fallback to iface with default gw
                        if node_ip_address is not None:
                            raise Exception(
                                f"found multiple ifaces with gateways for node {node_name}"
                            )
                        node_ip_address = iface["address"]

            if node_ip_address is None:
                raise Exception(f"Could not find ip for node {node_name}")

            print(f"adding {node_name}")
            dynamic_inventory[pve_cloud_domain][cluster_name]["pve_hosts"][
                node_name
            ] = {
                "ansible_user": "root",
                "ansible_host": node_ip_address,
            }

        print(f"writing dyn inv to {inv_path}")
        validate_cloud_dyn_inv(dynamic_inventory)
        with open(inv_path, "w") as file:
            yaml.dump(dynamic_inventory, file)


def connect_cluster(args):
    dynamic_inventory = init_dyn_inv()

    # connect to the cluster via paramiko and check if cloud files are already there
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    ssh.connect(args.pve_host, username="root")

    # since we need root we cant use sftp and root via ssh is disabled
    _, stdout, _ = ssh.exec_command("cat /etc/pve/cloud/cluster_vars.yaml")
    cluster_vars = yaml.safe_load(stdout.read().decode("utf-8"))

    if not cluster_vars:
        # cluster has not been yet initialized
        if not args.pve_cloud_domain:
            pve_cloud_domain = input(
                "Cluster has not yet been fully initialized, assign the cluster a cloud domain and press ENTER:"
            )
        else:
            pve_cloud_domain = args.pve_cloud_domain
    else:
        pve_cloud_domain = cluster_vars["pve_cloud_domain"]

    # init cloud domain if not there
    if pve_cloud_domain not in dynamic_inventory:
        dynamic_inventory[pve_cloud_domain] = {}

    # connect to the passed host
    proxmox = ProxmoxAPI(args.pve_host, user="root", backend="ssh_paramiko")

    # try get the cluster name
    cluster_name = None
    status_resp = proxmox.cluster.status.get()
    for entry in status_resp:
        if entry["id"] == "cluster":
            cluster_name = entry["name"]
            break

    if cluster_name is None:
        raise Exception("Could not get cluster name")

    if cluster_name in dynamic_inventory[pve_cloud_domain] and not args.force:
        print(
            f"cluster {cluster_name} already in dynamic inventory, add --force to overwrite current local inv."
        )
        return

    # overwrite on force / create fresh
    dynamic_inventory[pve_cloud_domain][cluster_name] = {"pve_hosts": {}}

    # not present => add and safe the dynamic inventory
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
            # when specified only take the host ip from the special iface
            if args.host_iface:
                if iface["iface"] == args.host_iface:
                    node_ip_address = iface.get("address")
                    break
            else:
                if "gateway" in iface:  # otherwise fallback to iface with default gw
                    if node_ip_address is not None:
                        raise Exception(
                            f"found multiple ifaces with gateways for node {node_name}"
                        )
                    node_ip_address = iface.get("address")

        if node_ip_address is None:
            raise Exception(f"Could not find ip for node {node_name}")

        print(f"adding {node_name}")
        dynamic_inventory[pve_cloud_domain][cluster_name]["pve_hosts"][node_name] = {
            "ansible_user": "root",
            "ansible_host": node_ip_address,
        }

    print(f"writing dyn inv to {inv_path}")
    validate_cloud_dyn_inv(dynamic_inventory)
    with open(inv_path, "w") as file:
        yaml.dump(dynamic_inventory, file)


def print_kubeconfig(args):
    if not os.path.exists(args.inventory):
        print("The specified inventory file does not exist!")
        return

    with open(args.inventory, "r") as f:
        inventory = yaml.safe_load(f)

    target_pve = inventory["target_pve"]

    target_cloud_domain = get_cloud_domain(target_pve)
    pve_inventory = get_pve_inventory(target_cloud_domain)

    # find target cluster in loaded inventory
    target_cluster = None

    for cluster in pve_inventory:
        if target_pve.endswith((cluster + "." + target_cloud_domain)):
            target_cluster = cluster
            break

    if not target_cluster:
        print("could not find target cluster in pve inventory!")
        return

    first_host = list(pve_inventory[target_cluster]["pve_hosts"].keys())[0]

    online_jump_host = None
    if "jump_hosts" in pve_inventory[target_cluster]:
        # jump hosts for cluster configured => find an online one
        for jump_host in pve_inventory[target_cluster]["jump_hosts"]:
            if check_ssh_open(jump_host):
                online_jump_host = jump_host
                break

        if not online_jump_host:
            print("No jump host of target cluster is online / reachable!")
            return

    if (
        not "extra_control_plane_sans" in inventory
        or not inventory["extra_control_plane_sans"]
    ):
        print("kubernetes cluster is not publicly reachable!")
        return

    jumpbox_channel = None
    if online_jump_host:
        jumpbox = paramiko.SSHClient()
        jumpbox.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        jumpbox.connect(online_jump_host, username="root")

        jumpbox_transport = jumpbox.get_transport()
        src_addr = ("127.0.0.1", 0)
        dest_addr = (pve_inventory[target_cluster][first_host]["ansible_host"], 22)

        jumpbox_channel = jumpbox_transport.open_channel(
            "direct-tcpip", dest_addr, src_addr
        )

    # connect to the first pve host in the dyn inv, assumes they are all online
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        pve_inventory[target_cluster][first_host]["ansible_host"],
        username="root",
        sock=jumpbox_channel,
    )

    # since we need root we cant use sftp and root via ssh is disabled
    _, stdout, _ = ssh.exec_command("cat /etc/pve/cloud/cluster_vars.yaml")

    cluster_vars = yaml.safe_load(stdout.read().decode("utf-8"))

    if online_jump_host:
        print(
            get_ssh_remote_master_kubeconfig(
                cluster_vars,
                inventory["stack_name"],
                inventory["extra_control_plane_sans"][0],
                online_jump_host,
                pve_inventory[target_cluster][first_host]["ansible_host"],
            )
        )
    else:
        print(get_ssh_master_kubeconfig(cluster_vars, inventory["stack_name"]))


def get_parser():
    parser = argparse.ArgumentParser(
        description="PVE general purpose cli for setting up."
    )

    base_parser = argparse.ArgumentParser(add_help=False)

    subparsers = parser.add_subparsers(dest="command", required=True)

    connect_cluster_parser = subparsers.add_parser(
        "connect-cluster",
        help="Add a pve cluster to you local development machines ~/.pve-cloud-dyn-inv.yaml inventory file.",
        parents=[base_parser],
    )
    connect_cluster_parser.add_argument(
        "--pve-host",
        type=str,
        help="PVE Host to connect to and add the entire cluster for the local machine.",
        required=True,
    )
    connect_cluster_parser.add_argument(
        "--host-iface",
        type=str,
        help="Choose a special iface on the pve hosts from where to get their ip. This is useful for accessing the hosts via their vm data ip. Defaults to the iface that has the default gateway set.",
    )
    connect_cluster_parser.add_argument(
        "--force", action="store_true", help="Will read the cluster if set."
    )
    connect_cluster_parser.add_argument(
        "--pve-cloud-domain",
        type=str,
        help="This skips manual input querying for the domain if the cluster gets initialized the first time.",
    )
    connect_cluster_parser.set_defaults(func=connect_cluster)

    remote_cluster_parser = subparsers.add_parser(
        "connect-remote-cluster",
        help="Adds a proxmox cluster that is behind a jump host to your local ~/.pve-cloud-dyn-inv.yaml inventory file.",
        parents=[base_parser],
    )
    remote_cluster_parser.add_argument(
        "--jump-hosts",
        type=str,
        help="Comma seperated ips to remote jump hosts root users. They will be configured as jump hosts on the local machines inventory. They need to be apt compatible and support `apt install python3-venv`.",
        required=True,
    )
    remote_cluster_parser.add_argument(
        "--pve-host",
        type=str,
        help="PVE Host to connect to and add the entire cluster for the local machine.",
        required=True,
    )
    remote_cluster_parser.add_argument(
        "--host-iface",
        type=str,
        help="Choose a special iface on the pve hosts from where to get their ip. This is useful for accessing the hosts via their vm data ip. Defaults to the iface that has the default gateway set.",
    )
    remote_cluster_parser.add_argument(
        "--force", action="store_true", help="Will read the cluster if set."
    )
    remote_cluster_parser.add_argument(
        "--pve-cloud-domain",
        type=str,
        help="This skips manual input querying for the domain if the cluster gets initialized the first time.",
    )
    remote_cluster_parser.add_argument(
        "--local-pypi-ip",
        type=str,
        help="Local pypi registry ip for e2e testing.",
    )
    remote_cluster_parser.set_defaults(func=connect_remote_cluster)

    print_kconf_parser = subparsers.add_parser(
        "print-kubeconfig",
        help="Print the kubeconfig from a k8s cluster deployed with pve cloud.",
        parents=[base_parser],
    )
    print_kconf_parser.add_argument(
        "--inventory",
        type=str,
        help="PVE cloud kubespray inventory yaml file.",
        required=True,
    )
    print_kconf_parser.set_defaults(func=print_kubeconfig)

    return parser


def main():
    args = get_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
