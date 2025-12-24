import argparse
import yaml
from proxmoxer import ProxmoxAPI
import os
import paramiko
from pve_cloud.cli.pvclu import get_ssh_master_kubeconfig
from pve_cloud.lib.inventory import *



def connect_cluster(args):
  # try load current dynamic inventory
  inv_path = os.path.expanduser("~/.pve-cloud-dyn-inv.yaml")
  if os.path.exists(inv_path):
    with open(inv_path, "r") as file:
      dynamic_inventory = yaml.safe_load(file)
  else:
    # initialize empty
    dynamic_inventory = {}

  # connect to the cluster via paramiko and check if cloud files are already there
  ssh = paramiko.SSHClient()
  ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

  ssh.connect(args.pve_host, username="root")

  # since we need root we cant use sftp and root via ssh is disabled
  _, stdout, _ = ssh.exec_command("cat /etc/pve/cloud/cluster_vars.yaml")
  cluster_vars = yaml.safe_load(stdout.read().decode('utf-8'))

  if not cluster_vars:
    # cluster has not been yet initialized
    pve_cloud_domain = input("Cluster has not yet been fully initialized, assign the cluster a cloud domain and press ENTER:")
  else:
    pve_cloud_domain = cluster_vars["pve_cloud_domain"]
  
  # init cloud domain if not there
  if pve_cloud_domain not in dynamic_inventory:
    dynamic_inventory[pve_cloud_domain] = {}

  # connect to the passed host
  proxmox = ProxmoxAPI(
    args.pve_host, user="root", backend='ssh_paramiko'
  )

  # try get the cluster name
  cluster_name = None
  status_resp = proxmox.cluster.status.get()
  for entry in status_resp:
    if entry['id'] == "cluster":
      cluster_name = entry['name']
      break

  if cluster_name is None:
    raise Exception("Could not get cluster name")
  
  if cluster_name in dynamic_inventory[pve_cloud_domain] and not args.force:
    print(f"cluster {cluster_name} already in dynamic inventory, add --force to overwrite current local inv.")
    return
  
  # overwrite on force / create fresh
  dynamic_inventory[pve_cloud_domain][cluster_name] = {}
  
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
      if 'gateway' in iface:
        if node_ip_address is not None:
          raise Exception(f"found multiple ifaces with gateways for node {node_name}")
        node_ip_address = iface.get("address")

    if node_ip_address is None:
      raise Exception(f"Could not find ip for node {node_name}")
    
    print(f"adding {node_name}")
    dynamic_inventory[pve_cloud_domain][cluster_name][node_name] = {
      "ansible_user": "root",
      "ansible_host": node_ip_address
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
  ssh.connect(pve_inventory[target_cluster][first_host]["ansible_host"], username="root")

  # since we need root we cant use sftp and root via ssh is disabled
  _, stdout, _ = ssh.exec_command("cat /etc/pve/cloud/cluster_vars.yaml")

  cluster_vars = yaml.safe_load(stdout.read().decode('utf-8'))

  print(get_ssh_master_kubeconfig(cluster_vars, inventory["stack_name"]))


def main():
  parser = argparse.ArgumentParser(description="PVE general purpose cli for setting up.")

  base_parser = argparse.ArgumentParser(add_help=False)

  subparsers = parser.add_subparsers(dest="command", required=True)

  connect_cluster_parser = subparsers.add_parser("connect-cluster", help="Add an entire pve cluster to this machine for use.", parents=[base_parser])
  connect_cluster_parser.add_argument("--pve-host", type=str, help="PVE Host to connect to and add the entire cluster for the local machine.", required=True)
  connect_cluster_parser.add_argument("--force", action="store_true", help="Will read the cluster if set.")
  connect_cluster_parser.set_defaults(func=connect_cluster)

  print_kconf_parser = subparsers.add_parser("print-kubeconfig", help="Print the kubeconfig from a k8s cluster deployed with pve cloud.", parents=[base_parser])
  print_kconf_parser.add_argument("--inventory", type=str, help="PVE cloud kubespray inventory yaml file.", required=True)
  print_kconf_parser.set_defaults(func=print_kubeconfig)

  args = parser.parse_args()
  args.func(args)


if __name__ == "__main__":
  main()