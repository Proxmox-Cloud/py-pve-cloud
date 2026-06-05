# Python pkg - py-pve-cloud

This is the core python library package that serves as a foundation for pve cloud.

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