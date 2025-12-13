import argparse
import yaml
from proxmoxer import ProxmoxAPI
import os
import paramiko
from pve_cloud.cli.pvclu import get_ssh_master_kubeconfig
from pve_cloud.lib.inventory import *


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

  print_kconf_parser = subparsers.add_parser("print-kubeconfig", help="Print the kubeconfig from a k8s cluster deployed with pve cloud.", parents=[base_parser])
  print_kconf_parser.add_argument("--inventory", type=str, help="PVE cloud kubespray inventory yaml file.", required=True)
  print_kconf_parser.set_defaults(func=print_kubeconfig)

  args = parser.parse_args()
  args.func(args)


if __name__ == "__main__":
  main()