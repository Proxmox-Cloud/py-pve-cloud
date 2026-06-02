import argparse
import os
import time

import paramiko
import pve_cloud._version as pxc_version
import rpyc
import yaml
from fabric import Connection
from proxmoxer import ProxmoxAPI

from pve_cloud.cli.pvclu import get_ssh_master_kubeconfig
from pve_cloud.lib.inventory import *

inv_path = os.path.expanduser("~/.pve-cloud-dyn-inv.yaml")


def init_dyn_inv():
    # try load current dynamic inventory
    if os.path.exists(inv_path):
        with open(inv_path, "r") as file:
            dynamic_inventory = yaml.safe_load(file)
    else:
        # initialize empty
        dynamic_inventory = {}

    return dynamic_inventory


# todo: rpyc is probably overkill here and could instead use remove pvesh get command
# either remove in the future or other good usecases are found. terraform prov forwarding?
def connect_remote_cluster(args):
    dynamic_inventory = init_dyn_inv()

    # during setup we assume all jump hosts are online
    host = args.pve_jump_hosts.split(",")[0]

    with Connection(host=host, user="root") as jump_pve_host:
        # install python venv
        jump_pve_host.run("apt install python3-venv -y", hide=False)

        # create versionized venv - to avoid collision of multiple admins
        jump_pve_host.run(f"python3 -m venv ~/.pxc-venv", hide=False)

        # install py-pve-cloud into the venv
        if args.local_pypi_ip:
            jump_pve_host.run(
                f"~/.pxc-venv/bin/pip install --upgrade --index-url http://{args.local_pypi_ip}:8088/simple --trusted-host {args.local_pypi_ip} py-pve-cloud=={pxc_version.__version__}",
                hide=False,
            )
        else:
            jump_pve_host.run(
                f"~/.pxc-venv/bin/pip install --upgrade py-pve-cloud=={pxc_version.__version__}",
                hide=False,
            )

        # run detached pxrpc server - we use this to execute python code remotely on the jump host
        # pkill -f pxrpc to cleanup
        jump_pve_host.run(
            f"export PYTHONUNBUFFERED=1; ~/.pxc-venv/bin/pxrpc >> /var/log/pxrpc.log 2>&1",
            disown=True,
        )
        time.sleep(3)

        # forward its port via fabric
        with jump_pve_host.forward_local(
            local_port=10080, remote_port=10080, remote_host="127.0.0.1"
        ):
            # launch rpyc client to the forwarded port
            pxrpc = rpyc.connect("localhost", 10080)
            try:

                # first we check if the cluster is already part of a proxmox cloud
                result = jump_pve_host.run("cat /etc/pve/cloud/cluster_vars.yaml")
                cluster_vars = yaml.safe_load(result.stdout.strip())

                if not cluster_vars:
                    # cluster has not been yet initialized
                    pve_cloud_domain = input(
                        "Cluster has not yet been fully initialized, assign the cluster a cloud domain and press ENTER:"
                    )
                else:
                    pve_cloud_domain = cluster_vars["pve_cloud_domain"]

                # init cloud domain if not there
                if pve_cloud_domain not in dynamic_inventory:
                    dynamic_inventory[pve_cloud_domain] = {}

                cluster_name = pxrpc.root.get_pve_cluster_name()
                print("pve cluster name", cluster_name)

                if (
                    cluster_name in dynamic_inventory[pve_cloud_domain]
                    and not args.force
                ):
                    print(
                        f"cluster {cluster_name} already in dynamic inventory, add --force to overwrite current local inv."
                    )
                    return

                # overwrite on force / create fresh
                dynamic_inventory[pve_cloud_domain][cluster_name] = {
                    "pve_hosts": {},
                    "pve_jump_hosts": args.pve_jump_hosts.split(","),
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
                with open(inv_path, "w") as file:
                    yaml.dump(dynamic_inventory, file)

            finally:
                # shut the rpyc server down
                pxrpc.root.shutdown()


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
        pve_cloud_domain = input(
            "Cluster has not yet been fully initialized, assign the cluster a cloud domain and press ENTER:"
        )
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

    first_host = list(pve_inventory[target_cluster].keys())[0]

    # connect to the first pve host in the dyn inv, assumes they are all online
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        pve_inventory[target_cluster][first_host]["ansible_host"], username="root"
    )

    # since we need root we cant use sftp and root via ssh is disabled
    _, stdout, _ = ssh.exec_command("cat /etc/pve/cloud/cluster_vars.yaml")

    cluster_vars = yaml.safe_load(stdout.read().decode("utf-8"))

    print(get_ssh_master_kubeconfig(cluster_vars, inventory["stack_name"]))


def main():
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
    connect_cluster_parser.set_defaults(func=connect_cluster)

    remote_cluster_parser = subparsers.add_parser(
        "connect-remote-cluster",
        help="Adds a proxmox cluster that is behind a jump host to your local ~/.pve-cloud-dyn-inv.yaml inventory file.",
        parents=[base_parser],
    )
    remote_cluster_parser.add_argument(
        "--pve-jump-hosts",
        type=str,
        help="Comma seperated ips to remote proxmox hosts of a cluster. They will be configured as jump hosts on the local machines inventory.",
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

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
