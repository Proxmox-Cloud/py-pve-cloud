import os

import paramiko
import pve_cloud._version
import yaml


def raise_on_py_cloud_missmatch(proxmox_host, jump_host=None):
    # dont raise in tdd
    if os.getenv("PYTEST_CURRENT_TEST"):
        return

    jumpbox_channel = None
    if jump_host:
        jumpbox = paramiko.SSHClient()
        jumpbox.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        jumpbox.connect(jump_host, username="root")

        jumpbox_transport = jumpbox.get_transport()
        src_addr = ("127.0.0.1", 0)
        dest_addr = (proxmox_host, 22)

        jumpbox_channel = jumpbox_transport.open_channel(
            "direct-tcpip", dest_addr, src_addr
        )

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        proxmox_host, username="root", sock=jumpbox_channel
    )  # tunnel if present

    # since we need root we cant use sftp and root via ssh is disabled
    _, stdout, _ = ssh.exec_command("cat /etc/pve/cloud/cluster_vars.yaml")

    cluster_vars = yaml.safe_load(stdout.read().decode("utf-8"))

    if cluster_vars["py_pve_cloud_version"] != pve_cloud._version.__version__:
        raise RuntimeError(
            f"Version missmatch! py_pve_cloud_version for cluster is {cluster_vars['py_pve_cloud_version']}, while you are using {pve_cloud._version.__version__}"
        )
