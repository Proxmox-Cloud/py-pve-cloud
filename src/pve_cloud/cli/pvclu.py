import argparse
import yaml 
import pprint
import paramiko
import dns.resolver
import base64
import re
from pve_cloud.lib.inventory import *


def get_cluster_vars(pve_host):
  ssh = paramiko.SSHClient()
  ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

  ssh.connect(pve_host, username="root")

  # since we need root we cant use sftp and root via ssh is disabled
  _, stdout, _ = ssh.exec_command("cat /etc/pve/cloud/cluster_vars.yaml")

  cluster_vars = yaml.safe_load(stdout.read().decode('utf-8'))

  return cluster_vars


def get_cloud_env(pve_host):
  ssh = paramiko.SSHClient()
  ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

  ssh.connect(pve_host, username="root")

  # since we need root we cant use sftp and root via ssh is disabled
  _, stdout, _ = ssh.exec_command("cat /etc/pve/cloud/cluster_vars.yaml")

  cluster_vars = yaml.safe_load(stdout.read().decode('utf-8'))

  _, stdout, _ = ssh.exec_command("cat /etc/pve/cloud/secrets/patroni.pass")

  patroni_pass = stdout.read().decode('utf-8').strip()

  # fetch bind update key for ingress dns validation
  _, stdout, _ = ssh.exec_command("sudo cat /etc/pve/cloud/secrets/internal.key")
  bind_key_file = stdout.read().decode('utf-8')

  bind_internal_key = re.search(r'secret\s+"([^"]+)";', bind_key_file).group(1)

  return cluster_vars, patroni_pass, bind_internal_key


def get_online_pve_host_prsr(args):
  print(f"export PVE_ANSIBLE_HOST='{get_online_pve_host(args.target_pve, suppress_warnings=True)}'")


def get_ssh_master_kubeconfig(cluster_vars, stack_name):
  resolver = dns.resolver.Resolver()
  resolver.nameservers = [cluster_vars['bind_master_ip'], cluster_vars['bind_slave_ip']]

  ddns_answer = resolver.resolve(f"masters-{stack_name}.{cluster_vars['pve_cloud_domain']}")
  ddns_ips = [rdata.to_text() for rdata in ddns_answer]

  if not ddns_ips:
    raise Exception("No master could be found via DNS!")

  ssh = paramiko.SSHClient()
  ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

  ssh.connect(ddns_ips[0], username="admin")

  # since we need root we cant use sftp and root via ssh is disabled
  _, stdout, _ = ssh.exec_command("sudo cat /etc/kubernetes/admin.conf")

  admin_conf = yaml.safe_load(stdout.read().decode('utf-8'))
  # rewrite variables for external access
  admin_conf["clusters"][0]["cluster"]["server"] = f"https://{ddns_ips[0]}:6443"
  admin_conf["clusters"][0]["name"] = stack_name

  admin_conf["contexts"][0]["context"]["cluster"] = stack_name
  admin_conf["contexts"][0]["name"] = f"kubernetes-admin@{stack_name}"

  admin_conf["current-context"] =  f"kubernetes-admin@{stack_name}"

  return yaml.safe_dump(admin_conf)


def export_envr(args):
  cloud_domain = get_cloud_domain(args.target_pve, suppress_warnings=True)
  pve_inventory = get_pve_inventory(cloud_domain, suppress_warnings=True)

  # get ansible ip for first host in target cluster
  ansible_host = None
  for cluster in pve_inventory:
    if args.target_pve.startswith(cluster):
      ansible_host = next(iter(pve_inventory[cluster].values()))["ansible_host"]

  if not ansible_host:
    raise RuntimeError(f"Could not find online host for {args.target_pve}!")

  cluster_vars, patroni_pass, bind_internal_key = get_cloud_env(ansible_host)
  print(f"export PVE_ANSIBLE_HOST='{ansible_host}'")
  print(f"export PVE_CLOUD_DOMAIN='{cloud_domain}'")

  # tf vars
  print(f"export PG_CONN_STR=\"postgres://postgres:{patroni_pass}@{cluster_vars['pve_haproxy_floating_ip_internal']}:5000/tf_states?sslmode=disable\"")
  print(f"export TF_VAR_pve_cloud_domain='{cloud_domain}'")
  print(f"export TF_VAR_pve_host='{ansible_host}'")
  print(f"export TF_VAR_cluster_proxy_ip='{cluster_vars['pve_haproxy_floating_ip_internal']}'")
  print(f"export TF_VAR_pve_cloud_pg_cstr=\"postgresql+psycopg2://postgres:{patroni_pass}@{cluster_vars['pve_haproxy_floating_ip_internal']}:5000/pve_cloud?sslmode=disable\"")
  print(f"export TF_VAR_master_b64_kubeconf='{base64.b64encode(get_ssh_master_kubeconfig(cluster_vars, args.stack_name).encode('utf-8')).decode('utf-8')}'")
  print(f"export TF_VAR_bind_master_ip='{cluster_vars['bind_master_ip']}'")
  print(f"export TF_VAR_bind_internal_key='{bind_internal_key}'")

  # pve inventory gets converted as b64 yaml string
  pve_64 = yaml.safe_dump(pve_inventory)
  print(f"export TF_VAR_pve_inventory_b64='{base64.b64encode(pve_64.encode('utf-8')).decode('utf-8')}'")

  
def main():
  parser = argparse.ArgumentParser(description="PVE Cloud utility cli. Should be called with bash eval.")

  base_parser = argparse.ArgumentParser(add_help=False)

  subparsers = parser.add_subparsers(dest="command", required=True)

  export_envr_parser = subparsers.add_parser("export-envrc", help="Export variables for k8s .envrc", parents=[base_parser])
  export_envr_parser.add_argument("--target-pve", type=str, help="The target pve cluster.", required=True)
  export_envr_parser.add_argument("--stack-name", type=str, help="Stack name of the deployment.", required=True)
  export_envr_parser.set_defaults(func=export_envr)

  get_online_pve_host_parser = subparsers.add_parser("get-online-host", help="Gets the ip for the first online proxmox host in the cluster.", parents=[base_parser])
  get_online_pve_host_parser.add_argument("--target-pve", type=str, help="The target pve cluster to get the first online ip of.", required=True)
  get_online_pve_host_parser.set_defaults(func=get_online_pve_host_prsr)

  args = parser.parse_args()
  args.func(args)


if __name__ == "__main__":
  main()