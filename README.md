# Python pkg - py-pve-cloud

This is the core python library package that serves as a foundation for pve cloud.

## CLI Entry Points

The package provides three command-line interfaces installed as entry points:

### pvclu - PVE Cloud Utility

Main CLI tool for PVE Cloud operations including cluster management, inventory queries, and administrative tasks.

```bash
# View available commands
pvclu --help

# Connect to a PVE cluster
pvclu connect-cluster --pve-host <host> --pve-cloud-domain <domain>

# Connect to a remote cluster via jump host
pvclu connect-remote-cluster --jump-host <jump-host> --pve-host <host> --pve-cloud-domain <domain>
```

### pvcli - PVE Cloud CLI

Secondary CLI interface for specialized PVE Cloud operations.

```bash
# View available commands
pvcli --help
```

### pxrpc - PVE Remote Procedure Call

RPC client for executing commands remotely on PVE Cloud infrastructure. Supports both synchronous and asynchronous execution.

```bash
# View available commands
pxrpc --help

# Launch remote procedure calls through jump hosts
pxrpc <jump-host> <target-host> [options]
```

**Usage in Tests**: The PXRPC functionality is extensively tested in e2e tests (`pve_cloud/tests/e2e/test_cloud.py::test_pxrpc_tunnel`), validating both sync and async execution patterns, parallelism, and jump host tunneling.

## Alembic orm

This project uses sqlalchemy + alembic integrated into the collection for management of the patroni database schema.

Edit `src/pve_cloud/orm/alchemy.py` database classes and run `alembic revision --autogenerate -m "revision description"` from the orm folder, to commit your changes into the general migrations. Before you need to do a `pip install .` to get the needed orm pypi packages.

get env var auth

```bash
PVE_HOST_IP= # ip for proxmox host of development system

PATRONI_PASS=$(ssh root@$PVE_HOST_IP cat /etc/pve/cloud/secrets/patroni.pass)
PROXY_IP=$(ssh root@$PVE_HOST_IP cat /etc/pve/cloud/cluster_vars.yaml | yq '.pve_haproxy_floating_ip_internal')
export PG_CONN_STR=postgresql+psycopg2://postgres:$PATRONI_PASS@$PROXY_IP:5000/pve_cloud?sslmode=disable
```

To create a new migration the database needs to be on the latest version, run `alembic upgrade head` to upgrade it.

## SSH

This project heavily relies on ssh connections and tunnels to accomplish its goals.

* paramiko / fabric: this is used for tunneling and forwarding through these tunnels
* asyncssh: this is used for simple tunneling and executing commands, where speed is needed

TODO: in the future asyncssh could be refactored out in favour of a processpoolexecutor + async context implementation

We also use rpyc to launch a remote server