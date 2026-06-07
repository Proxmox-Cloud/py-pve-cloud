import asyncio
import json
import os
import socket
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager, contextmanager

import dns.resolver
import pve_cloud._version as pxc_version
import rpyc
from fabric import Connection
from proxmoxer import ProxmoxAPI
from sqlalchemy import create_engine, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pve_cloud.orm.alchemy import (AcmeX509, ProxmoxCloudSecrets,
                                   VirtualMachineVars)
import subprocess
import yaml


# initialized / launched by pvcli connect_remote_cluster
# it is launched on a proxmox host
class PxrpcService(rpyc.Service):

    def __init__(self):
        super().__init__()
        self.proxmox = ProxmoxAPI("127.0.0.1", user="root", backend="ssh_paramiko")

    def on_connect(self, conn):
        pass

    def on_disconnect(self, conn):
        pass

    # rpyc doesnt have a clean shutdown methodology
    # this is the cleanest i found without triggerin eof on the clients side
    def shutdown(self):
        time.sleep(5)  # workers get more time to shut down
        os._exit(0)


    def get_pg_conn_str(self):
        # return from cache if present
        if self.patroni_cstr:
            return self.patroni_cstr
        
        # needs to be cat because of proxmox fs
        result_pass = subprocess.run(
            ["cat", "/etc/pve/cloud/secrets/patroni.pass"],
            capture_output=True,
            text=True,
            check=True,
        )
        patroni_pass = result_pass.stdout.rstrip()

        result_vars = subprocess.run(
            ["cat", "/etc/pve/cloud/cluster_vars.yaml"],
            capture_output=True,
            text=True,
            check=True,
        )
        cluster_vars = yaml.safe_load(result_vars.stdout)

        self.patroni_cstr = f"postgresql+psycopg2://postgres:{patroni_pass}@{cluster_vars['pve_haproxy_floating_ip_internal']}:5000/pve_cloud?sslmode=disable"

        return self.patroni_cstr

    def exposed_e2e_return(self):
        print(f"e2e return on pid {os.getpid()}")
        return self.get_pg_conn_str()

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
        shutdown_thread = threading.Thread(target=self.shutdown)
        shutdown_thread.start()

    def exposed_resolve_k8s_master(self, nameservers, a_rs_hostname):
        resolver = dns.resolver.Resolver()
        resolver.nameservers = nameservers
        ddns_answer = resolver.resolve(a_rs_hostname)

        ddns_ips = [rdata.to_text() for rdata in ddns_answer]

        return ddns_ips

    def exposed_e2e_inject_cert(self, stack_fqdn, record0):
        engine = create_engine(self.get_pg_conn_str())

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

    def exposed_inject_cloud_secret(
        self, cloud_domain, secret_name, secret_data_json, secret_type
    ):
        engine = create_engine(self.get_pg_conn_str())

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

    def exposed_delete_cloud_secret(self, cloud_domain, secret_name):

        engine = create_engine(self.get_pg_conn_str())

        with Session(engine) as session:
            stmt = delete(ProxmoxCloudSecrets).where(
                ProxmoxCloudSecrets.cloud_domain == cloud_domain,
                ProxmoxCloudSecrets.secret_name == secret_name,
            )

            result = session.execute(stmt)
            session.commit()

    def exposed_get_cloud_secret(self, cloud_domain, secret_name):

        engine = create_engine(self.get_pg_conn_str())

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

    def exposed_get_cloud_secrets(self, cloud_domain, secret_type):
        engine = create_engine(self.get_pg_conn_str())

        with Session(engine) as session:
            stmt = select(ProxmoxCloudSecrets).where(
                ProxmoxCloudSecrets.cloud_domain == cloud_domain,
                ProxmoxCloudSecrets.secret_type == secret_type,
            )
            records = session.scalars(stmt).all()

        secrets_json = json.dumps(
            {record.secret_name: record.secret_data for record in records}
        )
        return secrets_json

    def exposed_get_vm_vars_blake(self, blake_ids_json, cloud_domain):
        engine = create_engine(self.get_pg_conn_str())

        blake_ids = json.loads(blake_ids_json)

        with Session(engine) as session:
            stmt = select(VirtualMachineVars).where(
                VirtualMachineVars.blake_id.in_(blake_ids),
                VirtualMachineVars.cloud_domain == cloud_domain,
            )
            records = session.scalars(stmt).all()

        vars_json = json.dumps({entry.blake_id: entry.vm_vars for entry in records})
        return vars_json


# launch the rpc server
def main():
    from rpyc.utils.server import ForkingServer, ThreadedServer

    print("launching on", sys.argv[1])

    # launch it threaded for sync (easy tasks no paralellism)
    if sys.argv[2] == "THREADED":
        t = ThreadedServer(PxrpcService, port=int(sys.argv[1]))
        t.start()
    elif sys.argv[2] == "FORKING":  # forking launch for paralellism
        f = ForkingServer(PxrpcService, port=int(sys.argv[1]))
        f.start()
    else:
        raise RuntimeError("Unknown / missing 2 string arg THREADED/FORKING")


@contextmanager
def _launch_pxrpc_base(
    jump_host, pve_host, rpyc_server_type, init_venv=False, local_pypi_ip=None
):
    print("connecting to", jump_host, pve_host)
    with Connection(host=jump_host, user="root") as jump_host_conn:
        with Connection(
            host=pve_host, user="root", gateway=jump_host_conn
        ) as pve_host_conn:
            # we only init the venv conditionally, the pve_setup_clusters playbook does the install for all hosts
            # this is only for the initial connect-remote-cluster on a host that is completely fresh
            if init_venv:
                if (
                    pve_host_conn.run(
                        f"[ -d '/root/.pxc-venv' ]", warn=True, hide=True
                    ).exited
                    != 0
                ):

                    # install python venv
                    pve_host_conn.run("apt install python3-venv -y", hide=False)

                    # create versionized venv - to avoid collision of multiple admins
                    pve_host_conn.run(f"python3 -m venv /root/.pxc-venv", hide=False)

                # install latest py-pve-cloud into the venv
                if local_pypi_ip:
                    pve_host_conn.run(
                        f"/root/.pxc-venv/bin/pip install --upgrade --index-url http://{local_pypi_ip}:8088/simple --trusted-host {local_pypi_ip} py-pve-cloud=={pxc_version.__version__}",
                        hide=False,
                    )
                else:
                    pve_host_conn.run(
                        f"/root/.pxc-venv/bin/pip install --upgrade py-pve-cloud=={pxc_version.__version__}",
                        hide=False,
                    )

            # get an random open port for launching pxrpc server
            get_open_port_remote = pve_host_conn.run(
                'python3 -c "import socket; s = socket.socket(socket.AF_INET, socket.SOCK_STREAM); s.bind((\\"\\", 0)); print(s.getsockname()[1]); s.close()"',
                hide=True,
            )

            open_port_remote = int(get_open_port_remote.stdout.strip())
            print("open port remote", open_port_remote)

            # run detached pxrpc server - we use this to execute python code remotely on the jump host
            # pkill -f pxrpc to cleanup
            pve_host_conn.run(
                f"export PYTHONUNBUFFERED=1; /root/.pxc-venv/bin/pxrpc {open_port_remote} {rpyc_server_type} >> /var/log/pxrpc-{open_port_remote}.log 2>&1",
                disown=True,
            )

            # get local open port too for forwarding
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("", 0))
            local_open_port = s.getsockname()[1]
            s.close()

            print("local open port", local_open_port)

            # forward its port via fabric
            with pve_host_conn.forward_local(
                local_port=local_open_port,
                remote_port=open_port_remote,
                remote_host="127.0.0.1",
            ):

                time.sleep(3)  # time for server to start
                yield local_open_port, pve_host_conn  # handle conn from here different based on sync / async


# skip the .root for method invocation
class RPyCConnectionWrapper:

    def __init__(self, connection):
        self._connection = connection

    def __getattr__(self, method_name):
        return getattr(self._connection.root, method_name)


# client launch contextmanagers:
@contextmanager
def launch_pxrpc(jump_host, pve_host, init_venv=False, local_pypi_ip=None):
    with _launch_pxrpc_base(
        jump_host,
        pve_host,
        "THREADED",
        init_venv=init_venv,
        local_pypi_ip=local_pypi_ip,
    ) as (local_open_port, pve_host_conn):
        # launch rpyc client to the forwarded port
        pxrpc = RPyCConnectionWrapper(rpyc.connect("localhost", local_open_port))

        try:
            yield pxrpc, pve_host_conn  # return to do whatever the caller need
        finally:
            # shut it down
            print("invoking remote shutdown function")
            pxrpc.shutdown()


# async implementation with process pool executor underneath (asyncssh struggels with deeper tunneling)

# init rpyc connection once per worker in processpoolexecutor initializer
worker_rpyc_client = None


def exit_worker(exit_id):
    global worker_rpyc_client

    print("closing rpyc client connection", exit_id, worker_rpyc_client)
    worker_rpyc_client.close()


def init_rpyc_worker(port):
    global worker_rpyc_client

    worker_rpyc_client = rpyc.connect("localhost", port)


# generic method call on the rpyc connection
def _execute_rpyc_call(method_name, *args, **kwargs):
    global worker_rpyc_client

    print(f"executing rpyc call {method_name} on pid {os.getpid()}")
    try:
        remote_method = getattr(worker_rpyc_client.root, method_name)
        result = remote_method(*args, **kwargs)

        return result
    except Exception as e:
        return f"RPyC Execution Error ({method_name}): {e}"


# rpyc connection wrapper that passed calls to executor
class AsyncRPyCPoolWrapper:

    def __init__(self, pool: ProcessPoolExecutor):
        self.pool = pool
        self.loop = asyncio.get_running_loop()

    # lookup wrapper.funcXYZ triggers this and also calls the result by () automatically
    def __getattr__(self, method_name):

        def dispatcher(*args, **kwargs):

            return self.loop.run_in_executor(
                self.pool,
                _execute_rpyc_call,
                method_name,
                *args,
                **kwargs,
            )

        return dispatcher


@asynccontextmanager
async def launch_pxrpc_async(jump_host, pve_host, init_venv=False, local_pypi_ip=None):
    with _launch_pxrpc_base(
        jump_host, pve_host, "FORKING", init_venv=init_venv, local_pypi_ip=local_pypi_ip
    ) as (local_open_port, _):

        NUM_WORKERS = 4
        with ProcessPoolExecutor(
            max_workers=NUM_WORKERS,
            initializer=init_rpyc_worker,
            initargs=(local_open_port,),
        ) as pool:
            pxrpc = AsyncRPyCPoolWrapper(pool)

            try:
                yield pxrpc  # return to do whatever the caller need
            finally:
                print("invoking delayed remote shutdown function")
                await pxrpc.shutdown()

                # close all connection of the workers
                print("sending pool shutdown")
                list(
                    pool.map(exit_worker, range(NUM_WORKERS))
                )  # list/() is critical to yield the generator
