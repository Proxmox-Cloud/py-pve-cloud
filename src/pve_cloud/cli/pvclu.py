import argparse
import os
import urllib.parse

import dns.resolver
import paramiko
from pve_cloud_schemas.validate import validate_cloud_dyn_inv
import yaml
from fabric import Connection

from pve_cloud.cli.pxrpc import launch_pxrpc
from pve_cloud.lib.inventory import (get_cloud_domain, get_online_pve_host,
                                     get_online_pve_host_from_target_pve,
                                     get_pve_inventory, get_target_cluster)
from pve_cloud.lib.ssh import connect_host
from pve_cloud_schemas.validate import validate_cluster_vars


def get_online_pve_host_prsr(args):
    pve_host, jump_host = get_online_pve_host_from_target_pve(args.target_pve)
    if jump_host:
        raise NotImplemented(
            f"Online pve host {pve_host} is not reachable directly! Only via {jump_host}"
        )
    print(f"export PVE_ANSIBLE_HOST='{pve_host}'")


# works only for cluster with an external exposed control plane
def get_ssh_remote_master_kubeconfig(stack_name, external_san, jump_host, pve_host):
    # launch remote pxrpc service
    with launch_pxrpc(jump_host, pve_host) as (pxrpc, pve_host_conn):

        result = pve_host_conn.run("cat /etc/pve/cloud/cluster_vars.yaml")
        cluster_vars = yaml.safe_load(result.stdout.strip())
        validate_cluster_vars(cluster_vars)

        ddns_ips = pxrpc.resolve_k8s_master(
            [cluster_vars["bind_master_ip"], cluster_vars["bind_slave_ip"]],
            f"masters-{stack_name}.{cluster_vars['pve_cloud_domain']}",
        )

        if not ddns_ips:
            raise Exception("No master could be found via remote DNS!")

        # use tunneled pve host to open connection to master node
        with Connection(
            host=ddns_ips[0], user="admin", gateway=pve_host_conn
        ) as master_node:
            result = master_node.run("sudo cat /etc/kubernetes/admin.conf")
            admin_conf = yaml.safe_load(result.stdout.strip())

            admin_conf["clusters"][0]["cluster"][
                "server"
            ] = f"https://{external_san}:6443"
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


# todo: rename function and implement rsync terraform cache mirroring like in terraform e2e suite
def export_pg_conn_str(args):
    if not args.target_pve and not args.cloud_domain:
        raise RuntimeError("Neither --target-pve nor --cloud-domain was specified.")

    if args.cloud_domain:
        cloud_domain = args.cloud_domain
    else:
        cloud_domain = get_cloud_domain(args.target_pve)

    pve_inventory = get_pve_inventory(cloud_domain)

    if not args.target_pve:
        target_cluster = list(pve_inventory.keys())[0]  # take random cluster
    else:
        # find specific target cluster based on target_pve arg
        target_cluster = get_target_cluster(
            pve_inventory, args.target_pve, target_cloud_domain=cloud_domain
        )

    pve_host, jump_host = get_online_pve_host(pve_inventory, target_cluster)

    with connect_host(pve_host, jump_host=jump_host) as ssh:
        _, stdout, _ = ssh.exec_command("cat /etc/pve/cloud/cluster_vars.yaml")
        cluster_vars = yaml.safe_load(stdout.read().decode("utf-8"))
        validate_cluster_vars(cluster_vars)

        _, stdout, _ = ssh.exec_command("cat /etc/pve/cloud/secrets/patroni.pass")
        patroni_pass = stdout.read().decode("utf-8").strip()

    if jump_host:
        # if a jumphost is defined, additionally to returning the pg connstr we create a local unix socket file forward

        # pkill existing forwards and cleanup socket
        print(
            f"pkill -f '{os.getcwd()}/.s.PGSQL.5432:{cluster_vars['pve_haproxy_floating_ip_internal']}:5000' && rm -f {os.getcwd()}/.s.PGSQL.5432 && sleep 2"
        )
        # create forward socket; background, no command exection, keepalive options and forward
        print(
            f"ssh -f -N -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -L {os.getcwd()}/.s.PGSQL.5432:{cluster_vars['pve_haproxy_floating_ip_internal']}:5000 root@{jump_host}"
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
