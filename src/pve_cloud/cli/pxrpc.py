import asyncio
import json
import multiprocessing
import os
import re
import signal
import socket
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from enum import StrEnum

import asyncssh
import dns.rcode
import dns.resolver
import dns.tsigkeyring
import dns.update
import pve_cloud._version as pxc_version
import rpyc
import yaml
from fabric import Connection
from proxmoxer import ProxmoxAPI
from pve_cloud_schemas.validate import validate_cluster_vars
from sqlalchemy import create_engine, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pve_cloud.orm.alchemy import (AcmeX509, ProxmoxCloudSecrets,
                                   VirtualMachineVars)


# services need to implement the shutdown function
class PxServiceEnum(StrEnum):
    PXRPC = "PXRPC"
    PROXMOXER = "PROXMOXER"


# initialized / launched by pvcli connect_remote_cluster
# it is launched on a proxmox host

# todo: it would be nice to have some generic exception handeling / wrapping and passing down to the
# rpyc clients for printing. like a wrapping RPycException class and general implcit try catch + handeling
# on terraform-provider-pxc unpacking

# we once get the master pid, this is needed for the forkingserver
# todo: probably not the best way for ipc shutdown, maybe use pipes?
MASTER_PID = os.getpid()


def shutdown_handler(signum, frame):
    # wait a few seconds so clients can gracefully disconnect
    time.sleep(5)

    # kill the entire process and all forks
    os.killpg(os.getpgrp(), signal.SIGKILL)


@rpyc.service
class RemoteProxmoxApi(rpyc.Service):

    def __init__(self):
        super().__init__()

        # init services required by most functions
        self.proxmox = ProxmoxAPI("127.0.0.1", user="root", backend="ssh_paramiko")

    # rpyc doesnt have a clean shutdown methodology
    # this is the cleanest i found without triggerin eof on the clients side
    @rpyc.exposed
    def shutdown(self):
        os.kill(MASTER_PID, signal.SIGTERM)  # this triggers the shutdown handler

    @rpyc.exposed
    def get_pve_cluster_name(self):
        # try get the cluster name
        cluster_name = None
        status_resp = self.proxmox.cluster.status.get()
        for entry in status_resp:
            if entry["id"] == "cluster":
                cluster_name = entry["name"]
                break

        return cluster_name

    @rpyc.exposed
    def get_nodes(self):
        return self.proxmox.nodes.get()

    @rpyc.exposed
    def get_node_network(self, node_name):
        return self.proxmox.nodes(node_name).network.get()


@rpyc.service
class PxrpcService(rpyc.Service):

    # functions for constructor, only for when the service is launched
    # as a real remote service on a proxmox host!
    def _get_cluster_vars(self):
        result_vars = subprocess.run(
            ["cat", "/etc/pve/cloud/cluster_vars.yaml"],
            capture_output=True,
            text=True,
            check=True,
        )
        cluster_vars = yaml.safe_load(result_vars.stdout)
        validate_cluster_vars(cluster_vars)

        return cluster_vars

    def _get_pg_conn_str(self, cluster_vars):
        # needs to be cat because of proxmox fs
        result_pass = subprocess.run(
            ["cat", "/etc/pve/cloud/secrets/patroni.pass"],
            capture_output=True,
            text=True,
            check=True,
        )
        patroni_pass = result_pass.stdout.rstrip()

        return f"postgresql+psycopg2://postgres:{patroni_pass}@{cluster_vars['pve_haproxy_floating_ip_internal']}:5000/pve_cloud?sslmode=disable"

    def _get_internal_bind_key(self):
        result_key = subprocess.run(
            ["cat", "/etc/pve/cloud/secrets/internal.key"],
            capture_output=True,
            text=True,
            check=True,
        )

        return re.search(r'secret\s+"([^"]+)";', result_key.stdout).group(1)

    # rpyc doesnt have a clean shutdown methodology
    # this is the cleanest i found without triggerin eof on the clients side
    @rpyc.exposed
    def shutdown(self):
        os.kill(MASTER_PID, signal.SIGTERM)  # this triggers the shutdown handler


    def __init__(self, cluster_vars=None, patroni_cstr=None, internal_bind_key=None):
        super().__init__()

        if cluster_vars:
            self.cluster_vars = cluster_vars
        else:
            self.cluster_vars = self._get_cluster_vars()

        if internal_bind_key:
            self.internal_bind_key = internal_bind_key
        else:
            self.internal_bind_key = self._get_internal_bind_key()

        if patroni_cstr:
            self.patroni_cstr = patroni_cstr
        else:
            self.patroni_cstr = self._get_pg_conn_str(
                self.cluster_vars
            )  # we need postgres for almost anything


    # return initted cluster vars
    @rpyc.exposed
    def get_cluster_vars(self):
        return self.cluster_vars

    # after initialization these functions can be called from anywhere
    # either via the local wrapper on cloud connecitons that are not tunneled
    # via a jump host, and also for true remote launches.
    @rpyc.exposed
    def e2e_return(self):
        print(f"e2e return on pid {os.getpid()}")
        return self.patroni_cstr

    @rpyc.exposed
    def resolve_k8s_master(self, nameservers, a_rs_hostname):
        resolver = dns.resolver.Resolver()
        resolver.nameservers = nameservers
        ddns_answer = resolver.resolve(a_rs_hostname)

        ddns_ips = [rdata.to_text() for rdata in ddns_answer]

        return ddns_ips

    @rpyc.exposed
    def e2e_inject_cert(self, stack_fqdn, record0):
        engine = create_engine(self.patroni_cstr)

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

    @rpyc.exposed
    def inject_cloud_secret(
        self, cloud_domain, secret_name, secret_data_json, secret_type
    ):
        engine = create_engine(self.patroni_cstr)

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

    @rpyc.exposed
    def merge_cloud_secret(
        self, cloud_domain, secret_name, secret_data_json, secret_type
    ):
        engine = create_engine(self.patroni_cstr)

        with Session(engine) as session:
            try:
                session.merge(
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

    @rpyc.exposed
    def delete_cloud_secret(self, cloud_domain, secret_name):

        engine = create_engine(self.patroni_cstr)

        with Session(engine) as session:
            stmt = delete(ProxmoxCloudSecrets).where(
                ProxmoxCloudSecrets.cloud_domain == cloud_domain,
                ProxmoxCloudSecrets.secret_name == secret_name,
            )

            result = session.execute(stmt)
            session.commit()

    @rpyc.exposed
    def get_cloud_secret(self, cloud_domain, secret_name):

        engine = create_engine(self.patroni_cstr)

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

    @rpyc.exposed
    def get_cloud_secrets(self, cloud_domain, secret_type):
        engine = create_engine(self.patroni_cstr)

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

    @rpyc.exposed
    def get_vm_vars_blake(self, blake_ids_json, cloud_domain):
        engine = create_engine(self.patroni_cstr)

        blake_ids = json.loads(blake_ids_json)

        with Session(engine) as session:
            stmt = select(VirtualMachineVars).where(
                VirtualMachineVars.blake_id.in_(blake_ids),
                VirtualMachineVars.cloud_domain == cloud_domain,
            )
            records = session.scalars(stmt).all()

        vars_json = json.dumps({entry.blake_id: entry.vm_vars for entry in records})
        return vars_json

    @rpyc.exposed
    def get_dns_a_record(self, host):
        # get nameservers of the cloud (global in all cluster vars defined)
        resolver = dns.resolver.Resolver()
        resolver.nameservers = [
            self.cluster_vars["bind_master_ip"],
            self.cluster_vars["bind_slave_ip"],
        ]

        try:
            answers = resolver.resolve(host, "A")
            return json.dumps([rdata.address for rdata in answers])
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            return "[]"

    @rpyc.exposed
    def create_cname_record(self, zone, name, cname, ttl):
        keyring = dns.tsigkeyring.from_text({"internal.": self.internal_bind_key})

        update = dns.update.Update(zone, keyring=keyring)
        update.add(name, ttl, "CNAME", cname)

        try:
            response = dns.query.tcp(update, self.cluster_vars["bind_master_ip"])

            if response.rcode() != dns.rcode.NOERROR:
                return False, dns.rcode.to_text(response.rcode())

            return True, None
        except Exception as e:
            return False, str(e)

    @rpyc.exposed
    def delete_cname_record(self, zone, name):
        keyring = dns.tsigkeyring.from_text({"internal.": self.internal_bind_key})

        update = dns.update.Update(zone, keyring=keyring)
        update.delete(name, "CNAME")

        try:
            response = dns.query.tcp(update, self.cluster_vars["bind_master_ip"])

            if response.rcode() != dns.rcode.NOERROR:
                return False, dns.rcode.to_text(response.rcode())

            return True, None
        except Exception as e:
            return False, str(e)

    @rpyc.exposed
    def create_external_acme_tls(self, stack_fqdn, cert_config_json, ec_csr_json):
        engine = create_engine(self.patroni_cstr)
        with Session(engine) as session:

            session.add(
                AcmeX509(
                    stack_fqdn=stack_fqdn,
                    config=json.loads(cert_config_json),
                    ec_csr=json.loads(ec_csr_json),
                )
            )
            session.commit()

    @rpyc.exposed
    def delete_external_acme_tls(self, stack_fqdn):
        engine = create_engine(self.patroni_cstr)
        with Session(engine) as session:
            stmt = delete(AcmeX509).where(
                AcmeX509.stack_fqdn == stack_fqdn,
            )
            result = session.execute(stmt)
            session.commit()


# launch the rpc server
def main():
    from rpyc.utils.server import ForkingServer, ThreadedServer

    print("launching on", sys.argv[1], sys.argv[2], sys.argv[3])

    # register handler in master thread / process
    signal.signal(signal.SIGTERM, shutdown_handler)

    service_to_launch = None
    match PxServiceEnum(sys.argv[3]):
        # here custom inits could take place (using classpartial / constructor)
        case PxServiceEnum.PXRPC:
            service_to_launch = PxrpcService
        case PxServiceEnum.PROXMOXER:
            service_to_launch = RemoteProxmoxApi

    # launch it threaded for sync (easy tasks no paralellism)
    if sys.argv[2] == "THREADED":
        pxrpc_server = ThreadedServer(service_to_launch, port=int(sys.argv[1]))
        pxrpc_server.start()
    elif sys.argv[2] == "FORKING":  # forking launch for paralellism
        pxrpc_server = ForkingServer(service_to_launch, port=int(sys.argv[1]))
        pxrpc_server.start()
    else:
        raise RuntimeError("Unknown / missing 2 string arg THREADED/FORKING")


@contextmanager
def _launch_pxrpc_base(
    jump_host: str,
    pve_host: str,
    rpyc_server_type: str,
    rpyc_service: PxServiceEnum,
    init_venv: bool = False,
    local_pypi_ip: str = None,
):
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
            pxrpc_cmd = f"export PYTHONUNBUFFERED=1; /root/.pxc-venv/bin/pxrpc {open_port_remote} {rpyc_server_type} {rpyc_service} >> /var/log/pxrpc-{open_port_remote}.log 2>&1"
            print("pxrpc cmd", pxrpc_cmd)
            pve_host_conn.run(
                pxrpc_cmd,
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
def launch_pxrpc(
    jump_host: str,
    pve_host: str,
    init_venv: bool = False,
    local_pypi_ip: str = None,
    service: PxServiceEnum = PxServiceEnum.PXRPC,
):
    with _launch_pxrpc_base(
        jump_host,
        pve_host,
        "THREADED",
        service,
        init_venv=init_venv,
        local_pypi_ip=local_pypi_ip,
    ) as (local_open_port, pve_host_conn):
        # launch rpyc client to the forwarded port
        pxrpc = RPyCConnectionWrapper(rpyc.connect("127.0.0.1", local_open_port))

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

    worker_rpyc_client = rpyc.connect("127.0.0.1", port)


# generic method call on the rpyc connection
def _execute_rpyc_call(method_name, *args, **kwargs):
    global worker_rpyc_client
    print(f"executing rpyc call {method_name} on pid {os.getpid()}")

    remote_method = getattr(worker_rpyc_client.root, method_name)
    result = remote_method(*args, **kwargs)

    return result


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
async def launch_pxrpc_async(
    jump_host: str,
    pve_host: str,
    init_venv: bool = False,
    local_pypi_ip: str = None,
    service: PxServiceEnum = PxServiceEnum.PXRPC,
):
    with _launch_pxrpc_base(
        jump_host,
        pve_host,
        "FORKING",
        service,
        init_venv=init_venv,
        local_pypi_ip=local_pypi_ip,
    ) as (local_open_port, _):

        NUM_WORKERS = 4
        with ProcessPoolExecutor(
            max_workers=NUM_WORKERS,
            initializer=init_rpyc_worker,
            initargs=(local_open_port,),
            # without this rpyc clients might fail in some environments
            mp_context=multiprocessing.get_context("spawn"),
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


# make methods async callable for generic invoke
# this is so we can call pxrpc methods generically for a locally
# initialized instance
class PxrpcAsyncWrapper:

    def __init__(self, pxservice):
        self.pxservice = pxservice

    def __getattr__(self, method_name):
        async def async_wrapper(*args, **kwargs):
            return getattr(self.pxservice, method_name)(*args, **kwargs)

        return async_wrapper


async def get_cstr_cvars(online_pve_host):
    async with asyncssh.connect(
        online_pve_host, username="root", known_hosts=None
    ) as conn:
        cmd = await conn.run("cat /etc/pve/cloud/secrets/patroni.pass", check=True)
        patroni_pass = cmd.stdout.rstrip()

        # fetch cluster vars to get internal proxy ip
        cmd = await conn.run("cat /etc/pve/cloud/cluster_vars.yaml", check=True)
        cluster_vars = yaml.safe_load(cmd.stdout)
        validate_cluster_vars(cluster_vars)

        cmd = await conn.run("cat /etc/pve/cloud/secrets/internal.key", check=True)
        internal_key = re.search(r'secret\s+"([^"]+)";', cmd.stdout).group(1)

    # build the connection string
    patroni_cstr = f"postgresql+psycopg2://postgres:{patroni_pass}@{cluster_vars['pve_haproxy_floating_ip_internal']}:5000/pve_cloud?sslmode=disable"

    return patroni_cstr, cluster_vars, internal_key


# pve cloud context aware async get function for the pxrpc functions.
# this will either return a local wrapper or a remotely launched instance
# of our pxrpc service. This serves as a generic function launch endpoint
# to be able to interact with a proxmox cloud, regardless of configuration
# (local access / remote via jump host). This currently only exists as an async
# implementation.
@asynccontextmanager
async def get_simple_pxrpc(online_pve_host, jump_host):
    if not jump_host:
        cstr, cluster_vars, internal_key = await get_cstr_cvars(online_pve_host)
        yield PxrpcAsyncWrapper(PxrpcService(cluster_vars, cstr, internal_key))
        return

    async with launch_pxrpc_async(jump_host, online_pve_host) as pxrpc:
        yield pxrpc
