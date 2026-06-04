import argparse
import re

import dns.resolver
import paramiko
import yaml
import os
from pve_cloud.cli.pxrpc import launch_pxrpc

from pve_cloud.lib.inventory import *
import urllib.parse
from fabric import Connection


def get_cluster_vars(pve_host, jump_host=None):
    
    jumpbox_channel = None
    if jump_host:
        jumpbox = paramiko.SSHClient()
        jumpbox.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        jumpbox.connect(jump_host, username="root")

        jumpbox_transport = jumpbox.get_transport()
        src_addr = ("127.0.0.1", 0)
        dest_addr = (pve_host, 22)

        jumpbox_channel = jumpbox_transport.open_channel(
            "direct-tcpip", dest_addr, src_addr
        )

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    ssh.connect(pve_host, username="root", sock=jumpbox_channel)

    # since we need root we cant use sftp and root via ssh is disabled
    _, stdout, _ = ssh.exec_command("cat /etc/pve/cloud/cluster_vars.yaml")

    cluster_vars = yaml.safe_load(stdout.read().decode("utf-8"))

    return cluster_vars


def get_cloud_env(pve_host):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    ssh.connect(pve_host, username="root")

    # since we need root we cant use sftp and root via ssh is disabled
    _, stdout, _ = ssh.exec_command("cat /etc/pve/cloud/cluster_vars.yaml")

    cluster_vars = yaml.safe_load(stdout.read().decode("utf-8"))

    _, stdout, _ = ssh.exec_command("cat /etc/pve/cloud/secrets/patroni.pass")

    patroni_pass = stdout.read().decode("utf-8").strip()

    # fetch bind update key for ingress dns validation
    _, stdout, _ = ssh.exec_command("sudo cat /etc/pve/cloud/secrets/internal.key")
    bind_key_file = stdout.read().decode("utf-8")

    bind_internal_key = re.search(r'secret\s+"([^"]+)";', bind_key_file).group(1)

    return cluster_vars, patroni_pass, bind_internal_key


def get_online_pve_host_prsr(args):
    pve_host, jump_host = get_online_pve_host(args.target_pve, suppress_warnings=True)
    if jump_host:
        raise NotImplemented("Jump host functionality not implemented for get-online-host yet!")
    print(
        f"export PVE_ANSIBLE_HOST='{pve_host}'"
    )

# works only for cluster with an external exposed control plane
def get_ssh_remote_master_kubeconfig(cluster_vars, stack_name, external_san, jump_host):
    # launch remote pxrpc service
    with launch_pxrpc(jump_host) as (pxrpc, jump_host):
        ddns_ips = pxrpc.root.resolve_k8s_master(
            [cluster_vars["bind_master_ip"], cluster_vars["bind_slave_ip"]], 
            f"masters-{stack_name}.{cluster_vars['pve_cloud_domain']}"
        )
        
        if not ddns_ips:
            raise Exception("No master could be found via remote DNS!")
        
        # use jump host to open tunneled ssh to master node now
        with Connection(host=ddns_ips[0], user="admin", gateway=jump_host) as master_node:
            result = master_node.run("sudo cat /etc/kubernetes/admin.conf")
            admin_conf = yaml.safe_load(result.stdout.strip())

            admin_conf["clusters"][0]["cluster"]["server"] = f"https://{external_san}:6443"
            admin_conf["clusters"][0]["name"] = stack_name

            admin_conf["contexts"][0]["context"]["cluster"] = stack_name
            admin_conf["contexts"][0]["name"] = stack_name

            admin_conf["current-context"] = stack_name

            return yaml.safe_dump(admin_conf)
    


def get_ssh_master_kubeconfig(cluster_vars, stack_name):
    resolver = dns.resolver.Resolver()
    resolver.nameservers = [
        cluster_vars["bind_master_ip"],
        cluster_vars["bind_slave_ip"],
    ]

    ddns_answer = resolver.resolve(
        f"masters-{stack_name}.{cluster_vars['pve_cloud_domain']}"
    )
    ddns_ips = [rdata.to_text() for rdata in ddns_answer]

    if not ddns_ips:
        raise Exception("No master could be found via DNS!")

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    ssh.connect(ddns_ips[0], username="admin")

    # since we need root we cant use sftp and root via ssh is disabled
    _, stdout, _ = ssh.exec_command("sudo cat /etc/kubernetes/admin.conf")

    admin_conf = yaml.safe_load(stdout.read().decode("utf-8"))
    # rewrite variables for external access
    admin_conf["clusters"][0]["cluster"]["server"] = f"https://{ddns_ips[0]}:6443"
    admin_conf["clusters"][0]["name"] = stack_name

    admin_conf["contexts"][0]["context"]["cluster"] = stack_name
    admin_conf["contexts"][0]["name"] = stack_name

    admin_conf["current-context"] = stack_name

    return yaml.safe_dump(admin_conf)


def export_pg_conn_str(args):
    if args.target_pve:
        cloud_domain = get_cloud_domain(args.target_pve, suppress_warnings=True)
    elif args.cloud_domain:
        cloud_domain = args.cloud_domain
    else:
        raise RuntimeError("Neither --target-pve nor --cloud-domain was specified.")

    pve_inventory = get_pve_inventory(cloud_domain, suppress_warnings=True)

    # get ansible ip for first host in target cluster
    ansible_host = None
    jump_hosts = None
    for cluster in pve_inventory:
        if args.cloud_domain:
            ansible_host = next(iter(pve_inventory[cluster]["pve_hosts"].values()))["ansible_host"]
            if "jump_hosts" in pve_inventory[cluster]:
                jump_hosts = pve_inventory[cluster]["jump_hosts"]

            break
        elif args.target_pve.startswith(cluster):
            ansible_host = next(iter(pve_inventory[cluster]["pve_hosts"].values()))["ansible_host"]
            if "jump_hosts" in pve_inventory[cluster]:
                jump_hosts = pve_inventory[cluster]["jump_hosts"]
            break

    if not ansible_host:
        raise RuntimeError(f"Could not find online host for {args.target_pve}!")

    cluster_vars, patroni_pass, bind_internal_key = get_cloud_env(ansible_host)

    if jump_hosts:
        # for jump hosts we do local tunneling via a unix socket
        # first we check if jump host is defined
        online_jump_host = None

        # jump hosts for cluster configured => find an online one
        for jump_host in jump_hosts:
            if check_ssh_open(jump_host):
                online_jump_host = jump_host
                break
        
        if not online_jump_host:
            raise RuntimeError(f"Jump hosts for cluster defined but none online / reachable!")

        # pkill existing forwards and cleanup socket
        print(
            f"pkill -f '{os.getcwd()}/.s.PGSQL.5432:{cluster_vars['pve_haproxy_floating_ip_internal']}:5000' && rm {os.getcwd()}/.s.PGSQL.5432 && sleep 2"
        )
        # create forward socket
        print(
            f"ssh -f -N -L {os.getcwd()}/.s.PGSQL.5432:{cluster_vars['pve_haproxy_floating_ip_internal']}:5000 root@{online_jump_host}"
        )
        print(
            f"export PG_CONN_STR=\"postgres://postgres:{patroni_pass}@/tf_states?host={urllib.parse.quote(os.getcwd(), safe='')}&sslmode=disable\""
        )
    else:
        # return the direct connection string
        print(
            f"export PG_CONN_STR=\"postgres://postgres:{patroni_pass}@{cluster_vars['pve_haproxy_floating_ip_internal']}:5000/tf_states?sslmode=disable\""
        )


def main():
    parser = argparse.ArgumentParser(
        description="PVE Cloud utility cli. Should be called with bash eval."
    )

    base_parser = argparse.ArgumentParser(add_help=False)

    subparsers = parser.add_subparsers(dest="command", required=True)

    export_envr_parser = subparsers.add_parser(
        "export-psql", help="Export variables for k8s .envrc", parents=[base_parser]
    )
    export_envr_parser.add_argument(
        "--target-pve",
        type=str,
        help="The target pve cluster, specify this or cloud domain directly.",
    )
    export_envr_parser.add_argument(
        "--cloud-domain", type=str, help="Cloud domain instead of target pve."
    )
    export_envr_parser.set_defaults(func=export_pg_conn_str)

    get_online_pve_host_parser = subparsers.add_parser(
        "get-online-host",
        help="Gets the ip for the first online proxmox host in the cluster.",
        parents=[base_parser],
    )
    get_online_pve_host_parser.add_argument(
        "--target-pve",
        type=str,
        help="The target pve cluster to get the first online ip of.",
        required=True,
    )
    get_online_pve_host_parser.set_defaults(func=get_online_pve_host_prsr)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
