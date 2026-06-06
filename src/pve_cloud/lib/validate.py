import os

import pve_cloud._version
from pve_cloud.lib.ssh import get_cluster_vars


def raise_on_py_cloud_missmatch(proxmox_host, jump_host=None):
    # dont raise in tdd
    if os.getenv("PYTEST_CURRENT_TEST") or os.getenv("TDDOG_LOCAL_IFACE"):
        return

    cluster_vars = get_cluster_vars(proxmox_host, jump_host)
   
    if cluster_vars["py_pve_cloud_version"] != pve_cloud._version.__version__:
        raise RuntimeError(
            f"Version missmatch! py_pve_cloud_version for cluster is {cluster_vars['py_pve_cloud_version']}, while you are using {pve_cloud._version.__version__}"
        )
