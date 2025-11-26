# Python pkg - py-pve-cloud

This is the core python library package that serves as a foundation for pve cloud.

## Alembic orm

This project uses sqlalchemy + alembic integrated into the collection for management of the patroni database schema.

Edit `src/orm/alchemy.py` database classes and run `alembic revision --autogenerate -m "revision description"` from the orm folder, to commit your changes into the general migrations. Before you need to do a `pip install .` to get the needed orm pypi packages.

get env var auth

```bash
PVE_HOST_IP= # ip for proxmox host of development system

PATRONI_PASS=$(ssh root@$PVE_HOST_IP cat /etc/pve/cloud/secrets/patroni.pass)
PROXY_IP=$(ssh root@$PVE_HOST_IP cat /etc/pve/cloud/cluster_vars.yaml | yq '.pve_haproxy_floating_ip_internal')
export PG_CONN_STR=postgresql+psycopg2://postgres:$PATRONI_PASS@$PROXY_IP:5000/pve_cloud?sslmode=disable
```

To create a new migration the database needs to be on the latest version, run `alembic upgrade head` to upgrade it.