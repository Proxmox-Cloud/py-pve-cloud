from sqlalchemy import Column, Integer, String, Boolean, SmallInteger, Text
from sqlalchemy.dialects.postgresql import MACADDR, INET, JSONB, insert, ENUM
from sqlalchemy import create_engine, MetaData, Table, select, delete, update
from sqlalchemy.orm import declarative_base
from alembic.config import Config
from alembic import command
import os

Base = declarative_base()

class BindDomains(Base):
  __tablename__ = "bind_domains"

  domain = Column(String(253), primary_key=True)
  stack_fqdn = Column(String(253), primary_key=True)


class AcmeX509(Base):
  __tablename__ = "acme_x509"

  stack_fqdn = Column(String(253), primary_key=True)
  config = Column(JSONB)
  ec_csr = Column(JSONB)
  ec_crt = Column(JSONB)
  k8s = Column(JSONB)


class KeaReservations(Base):
  __tablename__ = "kea_reservations"

  mac = Column(MACADDR, primary_key=True)
  ip = Column(INET) # nullable
  hostname = Column(String(253), nullable=False)
  client_classes = Column(String(1000)) # csv seperated client classes
  stack_fqdn = Column(String(253), nullable=False)
  machine_type = Column(String(50), nullable=False)


class KeaClientClassDefs(Base):
  __tablename__ = "kea_client_class_defs"

  stack_fqdn = Column(String(253), nullable=False)
  class_name = Column(String(253), primary_key=True)
  class_content = Column(JSONB, nullable=False)


# todo: rename just ingress rules
class K8SIngressRules(Base):
  __tablename__ = "k8s_ingress_rules"

  zone = Column(String(253), primary_key=True)
  name = Column(String(253), primary_key=True)
  stack_fqdn = Column(String(253), primary_key=True)
  proxy_stack_fqdn = Column(String(253), nullable=False)
  external = Column(Boolean, default=False)
  rule_len = Column(Integer, nullable=False)
  is_k8s = Column(Boolean, nullable=False)


# todo: rename to just tcp proxies
class K8STcpProxies(Base):
  __tablename__ = "k8s_tcp_proxies"

  proxy_name = Column(String(253), nullable=False)
  haproxy_port = Column(SmallInteger, primary_key=True)
  node_port = Column(SmallInteger, nullable=False)
  stack_fqdn = Column(String(253), nullable=False)
  # determines backend routing in haproxy, if false will go 
  is_k8s = Column(Boolean, nullable=False) 
  proxy_snippet = Column(Text)
  proxy_stack_fqdn = Column(String(253), primary_key=True)
  external = Column(Boolean, default=False)


class K8SExternalControlPlanes(Base):
  __tablename__ = "k8s_ext_control_planes"

  stack_fqdn = Column(String(253), primary_key=True)
  extra_sans = Column(String(2530), nullable=False) # csvs
  proxy_stack_fqdn = Column(String(253), nullable=False)


# vars are overwrite on exist and have no guarantee that they get cleaned up
# / that the vms still exist if a user just deletes via proxmox ui
# todo: built in some sort of cleanup mechanism
class VirtualMachineVars(Base):
  __tablename__ = "vm_vars_blake"

  cloud_domain = Column(String(253), primary_key=True)
  blake_id = Column(String(50), primary_key=True)
  vm_vars = Column(JSONB, nullable=False)


# is also used for configuration and dynamic discovery
class ProxmoxCloudSecrets(Base):
  __tablename__ = "px_cloud_secrets"

  cloud_domain = Column(String(253), primary_key=True)
  secret_name = Column(String(253), primary_key=True)
  secret_data = Column(JSONB, nullable=False)
  secret_type = Column(String(50), nullable=True)


# apply the migrations to the database
def migrate(conn_str):
  alembic_cfg = Config(os.path.join(os.path.dirname(__file__), 'alembic.ini'))

  alembic_cfg.set_main_option("sqlalchemy.url", conn_str)
  alembic_cfg.set_main_option("prepend_sys_path", os.path.dirname(__file__))

  command.upgrade(alembic_cfg, "head")


# generic read with simple where equal functionality
def alch_read(conn_str, table_name, where_equal_args):
  engine = create_engine(conn_str)
  metadata = MetaData()

  table = Table(table_name, metadata, autoload_with=engine)

  statement = select(table)
  for col_name, val in where_equal_args.items():
    statement = statement.where(table.c[col_name] == val)

  with engine.connect() as conn:
    result = conn.execute(statement)
    
  return [dict(row._mapping) for row in result]


def alch_write(conn_str, table_name, rows):
  engine = create_engine(conn_str)
  metadata = MetaData()

  table = Table(table_name, metadata, autoload_with=engine)

  stmt = insert(table)

  with engine.begin() as conn:
    conn.execute(stmt, rows)


def alch_update(conn_str, table_name, values, whereclause):
  engine = create_engine(conn_str)
  metadata = MetaData()
  table = Table(table_name, metadata, autoload_with=engine)

  stmt = update(table).values(**values)

  for col, val in whereclause.items():
    stmt = stmt.where(table.c[col] == val)

  with engine.begin() as conn:
    conn.execute(stmt)


def alch_upsert(conn_str, table_name, values, conflict_columns):
  engine = create_engine(conn_str)
  metadata = MetaData()
  table = Table(table_name, metadata, autoload_with=engine)

  stmt = insert(table).values(**values)

  # update every column except conflict columns that were passed in the values
  # this is the same as doing SET column_x = EXCLUDED.column_x
  update_dict = {c: getattr(stmt.excluded, c) for c in values if c not in conflict_columns}

  stmt = stmt.on_conflict_do_update(
    index_elements=[table.c[c] for c in conflict_columns],
    set_=update_dict
  )

  with engine.begin() as conn:
    conn.execute(stmt)


def alch_delete(conn_str, table_name, where_equal_args):
  engine = create_engine(conn_str)
  metadata = MetaData()

  table = Table(table_name, metadata, autoload_with=engine)

  stmt = delete(table)
  for col_name, val in where_equal_args.items():
    stmt = stmt.where(table.c[col_name] == val)

  with engine.begin() as conn: 
    result = conn.execute(stmt)
    return result.rowcount
  