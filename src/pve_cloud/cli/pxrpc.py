import os
import threading
import time

import rpyc
from proxmoxer import ProxmoxAPI


# rpyc doesnt have a clean shutdown methodology
# this is the cleanest i found without triggerin eof on the clients side
def shutdown():
    time.sleep(0.5)
    os._exit(0)


class InitService(rpyc.Service):

    def on_connect(self, conn):
        self.proxmox = ProxmoxAPI("127.0.0.1", user="root", backend="ssh_paramiko")

    def on_disconnect(self, conn):
        pass

    def exposed_get_pve_cluster_name(self):
        # try get the cluster name
        cluster_name = None
        status_resp = self.proxmox.cluster.status.get()
        for entry in status_resp:
            if entry["id"] == "cluster":
                cluster_name = entry["name"]
                break

        return cluster_name

    def exposed_get_nodes(self):
        return self.proxmox.nodes.get()

    def exposed_get_node_network(self, node_name):
        return self.proxmox.nodes(node_name).network.get()

    def exposed_shutdown(self):
        shutdown_thread = threading.Thread(target=shutdown)
        shutdown_thread.start()


def main():
    from rpyc.utils.server import ThreadedServer

    t = ThreadedServer(InitService, port=10080)
    t.start()
