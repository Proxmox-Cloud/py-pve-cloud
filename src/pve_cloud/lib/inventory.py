import subprocess
from proxmoxer import ProxmoxAPI
from pve_cloud.lib.validate import raise_on_py_cloud_missmatch
import shutil
import yaml
import os
import socket


def get_cloud_domain(target_pve, suppress_warnings = False):
    if shutil.which("avahi-browse"):
        avahi_disc = subprocess.run(["avahi-browse", "-rpt", "_pxc._tcp"], stdout=subprocess.PIPE, text=True, check=True)
        services = avahi_disc.stdout.splitlines()

        # find cloud domain hosts and get first online per proxmox cluster
        for service in services:
            if service.startswith("="):
                # avahi service def
                svc_args = service.split(";")

                cloud_domain = None
                cluster_name = None

                for txt_arg in svc_args[9].split():
                    txt_arg = txt_arg.replace('"', '')
                    if txt_arg.startswith("cloud_domain"):
                        cloud_domain = txt_arg.split("=")[1]

                    if txt_arg.startswith("cluster_name"):
                        cluster_name = txt_arg.split("=")[1]

                if not cloud_domain or not cluster_name:
                    raise ValueError(f"Missconfigured proxmox cloud avahi service: {service}")
                
                if target_pve.endswith(cloud_domain):
                    return cloud_domain
                

        raise RuntimeError("Could not get cloud domain via avahi mdns!")
    else:
        if not suppress_warnings:
            print("avahi-browse not available, falling back to local inventory file from pvcli connect-cluster!")

        with open(os.path.expanduser("~/.pve-cloud-dyn-inv.yaml"), "r") as f:
            pve_inventory = yaml.safe_load(f)

        for pve_cloud in pve_inventory:
            for pve_cluster in pve_inventory[pve_cloud]:
                if pve_cluster + "." + pve_cloud == target_pve:
                    return pve_cloud
        
        raise Exception(f"Could not identify cloud domain for {target_pve}")


def get_online_pve_host(target_pve, suppress_warnings = False, skip_py_cloud_check=False):
    if shutil.which("avahi-browse"):
        avahi_disc = subprocess.run(["avahi-browse", "-rpt", "_pxc._tcp"], stdout=subprocess.PIPE, text=True, check=True)
        services = avahi_disc.stdout.splitlines()

        for service in services:
            if service.startswith("="):
                # avahi service def
                svc_args = service.split(";")
                host_ip = svc_args[7]

                cloud_domain = None
                cluster_name = None

                for txt_arg in svc_args[9].split():
                    txt_arg = txt_arg.replace('"', '')
                    if txt_arg.startswith("cloud_domain"):
                        cloud_domain = txt_arg.split("=")[1]

                    if txt_arg.startswith("cluster_name"):
                        cluster_name = txt_arg.split("=")[1]

                if not cloud_domain or not cluster_name:
                    raise ValueError(f"Missconfigured proxmox cloud avahi service: {service}")
                
                # main pve cloud inventory
                if f"{cluster_name}.{cloud_domain}" == target_pve:
                    if not skip_py_cloud_check:
                        raise_on_py_cloud_missmatch(host_ip) # validate that versions of dev machine and running on cluster match

                    return host_ip
        
        raise RuntimeError(f"No online host found for {target_pve}!")
    else:
        if not suppress_warnings:
            print("avahi-browse not available, falling back to local inventory file from pvcli connect-cluster!")

        with open(os.path.expanduser("~/.pve-cloud-dyn-inv.yaml"), "r") as f:
            pve_inventory = yaml.safe_load(f)

        for pve_cloud in pve_inventory:
            for pve_cluster in pve_inventory[pve_cloud]:
                if pve_cluster + "." + pve_cloud == target_pve:
                    for pve_host in pve_inventory[pve_cloud][pve_cluster]:
                        # check if host is available
                        pve_host_ip = pve_inventory[pve_cloud][pve_cluster][pve_host]["ansible_host"]
                        try:
                            with socket.create_connection((pve_host_ip, 22), timeout=3):
                                
                                if not skip_py_cloud_check:
                                    raise_on_py_cloud_missmatch(pve_host_ip) # validate that versions of dev machine and running on cluster match

                                return pve_host_ip
                        except Exception as e:
                            # debug
                            print(e, type(e))
                            pass
            
        raise RuntimeError(f"Could not find online pve host for {target_pve}")


def get_pve_inventory(pve_cloud_domain, suppress_warnings = False, skip_py_cloud_check=False):
    if shutil.which("avahi-browse"):
        # avahi is available

        # call avahi-browse -rpt _pxc._tcp and find online host matching pve cloud domain
        # connect via ssh and fetch all other hosts via proxmox api => build inventory
        avahi_disc = subprocess.run(["avahi-browse", "-rpt", "_pxc._tcp"], stdout=subprocess.PIPE, text=True, check=True)
        services = avahi_disc.stdout.splitlines()

        pve_inventory = {}

        py_pve_cloud_performed_version_checks = set()

        # find cloud domain hosts and get first online per proxmox cluster
        cloud_domain_first_hosts = {}
        for service in services:
            if service.startswith("="):
                # avahi service def
                svc_args = service.split(";")
                host_ip = svc_args[7]

                cloud_domain = None
                cluster_name = None

                for txt_arg in svc_args[9].split():
                    txt_arg = txt_arg.replace('"', '')
                    if txt_arg.startswith("cloud_domain"):
                        cloud_domain = txt_arg.split("=")[1]

                    if txt_arg.startswith("cluster_name"):
                        cluster_name = txt_arg.split("=")[1]

                if not cloud_domain or not cluster_name:
                    raise ValueError(f"Missconfigured proxmox cloud avahi service: {service}")
                
                # main pve cloud inventory
                if cloud_domain == pve_cloud_domain and cluster_name not in cloud_domain_first_hosts:
                    if not skip_py_cloud_check and f"{cluster_name}.{cloud_domain}" not in py_pve_cloud_performed_version_checks:
                        raise_on_py_cloud_missmatch(host_ip) # validate that versions of dev machine and running on cluster match
                        py_pve_cloud_performed_version_checks.add(f"{cluster_name}.{cloud_domain}") # perform version check only once per cluster

                    cloud_domain_first_hosts[cluster_name] = host_ip
                
        # iterate over hosts and build pve inv via proxmox api
        # todo: this needs to be hugely optimized it blocks the grpc server
        for cluster_first, first_host in cloud_domain_first_hosts.items():
            proxmox = ProxmoxAPI(
                first_host, user="root", backend='ssh_paramiko'
            )

            cluster_name = None
            status_resp = proxmox.cluster.status.get()
            for entry in status_resp:
                if entry['id'] == "cluster":
                    cluster_name = entry['name']
                break

            if cluster_name is None:
                raise RuntimeError("Could not get cluster name")
            
            if cluster_name != cluster_first:
                raise ValueError(f"Proxmox cluster name missconfigured in avahi service {cluster_name}/{cluster_first}")
            
            pve_inventory[cluster_name] = {}

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
                    if 'gateway' in iface:
                        if node_ip_address is not None:
                            raise RuntimeError(f"found multiple ifaces with gateways for node {node_name}")
                        node_ip_address = iface.get("address")

                if node_ip_address is None:
                    raise RuntimeError(f"Could not find ip for node {node_name}")
                
                pve_inventory[cluster_name][node_name] = {
                    "ansible_user": "root",
                    "ansible_host": node_ip_address
                }
                        

        return pve_inventory

    else:
        if not suppress_warnings:
            print("avahi-browse not available, falling back to local inventory file from pvcli connect-cluster!")
        # try load fallback manual inventory from disk
        inv_path = os.path.expanduser("~/.pve-cloud-dyn-inv.yaml")
        if not os.path.exists(inv_path):
            raise RuntimeError("Local pve inventory file missing (~/.pve-cloud-dyn-inv.yaml), execute `pvcli connect-cluster` or setup avahi mdns discovery!")

        with open(inv_path, "r") as file:
            dynamic_inventory = yaml.safe_load(file)

        if pve_cloud_domain not in dynamic_inventory:
            raise RuntimeError(f"{pve_cloud_domain} not in local dynamic inventory (~/.pve-cloud-dyn-inv.yaml created by `pvcli connect-cluster`)!")

        return dynamic_inventory[pve_cloud_domain]

   