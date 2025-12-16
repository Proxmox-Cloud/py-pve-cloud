import paramiko
import yaml
import pve_cloud._version


def raise_on_py_cloud_missmatch(proxmox_host):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(proxmox_host, username="root")

    # since we need root we cant use sftp and root via ssh is disabled
    _, stdout, _ = ssh.exec_command("cat /etc/pve/cloud/cluster_vars.yaml")

    cluster_vars = yaml.safe_load(stdout.read().decode('utf-8'))

    if pve_cloud._version.__version__.startswith("0.0."): # tddog development version, skip raising
        return
  
    if cluster_vars["py_pve_cloud_version"] != pve_cloud._version.__version__:
        raise RuntimeError(f"Version missmatch! py_pve_cloud_version for cluster is {cluster_vars['py_pve_cloud_version']}, while you are using {pve_cloud._version.__version__}")