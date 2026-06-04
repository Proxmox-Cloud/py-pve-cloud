import os
import socket
import sys
import threading
import time
from contextlib import contextmanager
from sqlalchemy.exc import IntegrityError

import dns.resolver
import pve_cloud._version as pxc_version
import rpyc
from fabric import Connection
from proxmoxer import ProxmoxAPI
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from pve_cloud.orm.alchemy import AcmeX509, ProxmoxCloudSecrets, VirtualMachineVars
import json

# rpyc doesnt have a clean shutdown methodology
# this is the cleanest i found without triggerin eof on the clients side
def shutdown():
    time.sleep(0.5)
    os._exit(0)


# initialized / launched by pvcli connect_remote_cluster
class PxRpcService(rpyc.Service):

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

    def exposed_resolve_k8s_master(self, nameservers, a_rs_hostname):
        resolver = dns.resolver.Resolver()
        resolver.nameservers = nameservers
        ddns_answer = resolver.resolve(a_rs_hostname)

        ddns_ips = [rdata.to_text() for rdata in ddns_answer]

        return ddns_ips

    def exposed_e2e_inject_cert(self, pg_conn_str_orm, stack_fqdn, record0):
        engine = create_engine(pg_conn_str_orm)

        # update certs and mirror pull secret
        with Session(engine) as session:
            copy_cert = AcmeX509(
                stack_fqdn=stack_fqdn,
                config={},
                ec_csr={},
                ec_crt={},
                k8s=json.loads(record0),
            )
            session.merge(copy_cert)
            session.commit()


    def exposed_inject_cloud_secret(self, pg_conn_str_orm, cloud_domain, secret_name, secret_data_json, secret_type):
        engine = create_engine(pg_conn_str_orm)

        with Session(engine) as session:
            try:
                session.add(
                    ProxmoxCloudSecrets(
                        cloud_domain=cloud_domain,
                        secret_name=secret_name,
                        secret_data=json.loads(secret_data_json),
                        secret_type=secret_type,
                    )
                )
                session.commit()
                return True

            except IntegrityError as e:
                session.rollback()
                return False

    
    def exposed_delete_cloud_secret(self, pg_conn_str_orm, cloud_domain, secret_name):

        engine = create_engine(pg_conn_str_orm)

        with Session(engine) as session:
            stmt = delete(ProxmoxCloudSecrets).where(
                ProxmoxCloudSecrets.cloud_domain == cloud_domain,
                ProxmoxCloudSecrets.secret_name == secret_name,
            )

            result = session.execute(stmt)
            session.commit()


    def exposed_get_cloud_secret(self, pg_conn_str_orm, cloud_domain, secret_name):

        engine = create_engine(pg_conn_str_orm)

        with Session(engine) as session:
            stmt = select(ProxmoxCloudSecrets).where(
                ProxmoxCloudSecrets.cloud_domain == cloud_domain,
                ProxmoxCloudSecrets.secret_name == secret_name,
            )
            record = session.scalars(stmt).first()

        if not record:
            return ""
        
        secret_data_json = json.dumps(record.secret_data)
        return secret_data_json
    

    def exposed_get_cloud_secrets(self, pg_conn_str_orm, cloud_domain, secret_type):
        engine = create_engine(pg_conn_str_orm)

        with Session(engine) as session:
            stmt = select(ProxmoxCloudSecrets).where(
                ProxmoxCloudSecrets.cloud_domain == cloud_domain,
                ProxmoxCloudSecrets.secret_type == secret_type,
            )
            records = session.scalars(stmt).all()

        secrets_json = json.dumps({record.secret_name: record.secret_data for record in records})
        return secrets_json
    

    def exposed_get_vm_vars_blake(self, pg_conn_str_orm, blake_ids_json, cloud_domain):
        engine = create_engine(pg_conn_str_orm)

        blake_ids = json.loads(blake_ids_json)

        with Session(engine) as session:
            stmt = select(VirtualMachineVars).where(
                VirtualMachineVars.blake_id.in_(blake_ids),
                VirtualMachineVars.cloud_domain == cloud_domain,
            )
            records = session.scalars(stmt).all()
        
        vars_json = json.dumps({ entry.blake_id: entry.vm_vars for entry in records })
        return vars_json


def main():
    from rpyc.utils.server import ThreadedServer

    print("launching on", sys.argv[1])
    t = ThreadedServer(PxRpcService, port=int(sys.argv[1]))
    t.start()


# client launch ctxmgr
@contextmanager
def launch_pxrpc(jump_host, pve_host, init_venv=False, local_pypi_ip=None):
    print("connecting to", jump_host, pve_host)
    with Connection(host=jump_host, user="root") as jump_host:
        with Connection(host=pve_host, user="root", gateway=jump_host) as pve_host:
            # we only init the venv conditionally, the pve_setup_clusters playbook does the install for all hosts
            # this is only for the initial connect-remote-cluster on a host that is completely fresh
            if init_venv:
                if (
                    pve_host.run(f"[ -d '/root/.pxc-venv' ]", warn=True, hide=True).exited
                    != 0
                ):

                    # install python venv
                    pve_host.run("apt install python3-venv -y", hide=False)

                    # create versionized venv - to avoid collision of multiple admins
                    pve_host.run(f"python3 -m venv /root/.pxc-venv", hide=False)

                # install latest py-pve-cloud into the venv
                if local_pypi_ip:
                    pve_host.run(
                        f"/root/.pxc-venv/bin/pip install --upgrade --index-url http://{local_pypi_ip}:8088/simple --trusted-host {local_pypi_ip} py-pve-cloud=={pxc_version.__version__}",
                        hide=False,
                    )
                else:
                    pve_host.run(
                        f"/root/.pxc-venv/bin/pip install --upgrade py-pve-cloud=={pxc_version.__version__}",
                        hide=False,
                    )

            # get an random open port for launching pxrpc server
            get_open_port_remote = pve_host.run(
                'python3 -c "import socket; s = socket.socket(socket.AF_INET, socket.SOCK_STREAM); s.bind((\\"\\", 0)); print(s.getsockname()[1]); s.close()"',
                hide=True,
            )

            open_port_remote = int(get_open_port_remote.stdout.strip())
            print("open port remote", open_port_remote)

            # run detached pxrpc server - we use this to execute python code remotely on the jump host
            # pkill -f pxrpc to cleanup
            pve_host.run(
                f"export PYTHONUNBUFFERED=1; /root/.pxc-venv/bin/pxrpc {open_port_remote} >> /var/log/pxrpc-{open_port_remote}.log 2>&1",
                disown=True,
            )

            # get local open port too for forwarding
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("", 0))
            local_open_port = s.getsockname()[1]
            s.close()

            print("local open port", local_open_port)

            # forward its port via fabric
            with pve_host.forward_local(
                local_port=local_open_port,
                remote_port=open_port_remote,
                remote_host="127.0.0.1",
            ):
                time.sleep(3) # time for server to start
                # launch rpyc client to the forwarded port
                pxrpc = rpyc.connect("localhost", local_open_port)

                yield pxrpc, pve_host  # return to do whatever the caller need

                # shut it down
                pxrpc.root.shutdown()
