import asyncio
import atexit
import socket
import threading
from contextlib import asynccontextmanager, contextmanager

import asyncssh
import paramiko
from asyncssh.misc import ChannelOpenError

# jump host connection cache
_jump_hosts = {}
_jump_chan_lock = threading.Lock()


def _get_jump_host_chan(host: str, jump_host: str, jump_user: str = "root"):
    with _jump_chan_lock:
        if jump_host not in _jump_hosts:
            jumpbox = paramiko.SSHClient()
            jumpbox.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            jumpbox.connect(jump_host, username=jump_user)

            _jump_hosts[jump_host] = jumpbox

        return (
            _jump_hosts[jump_host]
            .get_transport()
            .open_channel("direct-tcpip", (host, 22), ("127.0.0.1", 0))
        )


# cleanup hook
def cleanup_jumphosts():
    for jumpbox in _jump_hosts.values():
        jumpbox.close()


atexit.register(cleanup_jumphosts)

# caches for open checks to avoid rate limits
_ssh_open_hosts_cache = {}
_open_cache_lock = threading.Lock()


def get_open_ssh_from_cache(host: str):
    with _open_cache_lock:
        if host in _ssh_open_hosts_cache:
            return _ssh_open_hosts_cache[host]

    return None


def set_open_ssh_cache(host: str, open: bool):
    with _open_cache_lock:
        _ssh_open_hosts_cache[host] = open


# generic connect to proxmox cluster through optional jump host
# assumes pve host and jump host to be ssh root accessible
@contextmanager
def connect_host(
    host: str, jump_host: str = None, user: str = "root", jump_user: str = "root"
):
    jumpbox_channel = (
        _get_jump_host_chan(host, jump_host, jump_user) if jump_host else None
    )

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    ssh.connect(host, username=user, sock=jumpbox_channel)

    try:
        yield ssh
    finally:
        ssh.close()

        if jumpbox_channel:
            jumpbox_channel.close()


def check_ssh_open(check_host: str, jump_host: str = None):
    cache_open = get_open_ssh_from_cache(check_host)
    if cache_open is not None:
        return cache_open

    if jump_host:
        jumpbox_channel = _get_jump_host_chan(check_host, jump_host)

        try:
            jumpbox_channel.settimeout(3)

            # Use makefile() instead of socket.SocketIO to safely create a read/write stream
            with jumpbox_channel.makefile("rwb") as sio:
                # read ssh server answer
                sio.readline()

                # send client hello
                sio.write(b"SSH-2.0-PxcOnlineCheck_1.0\r\n")
                sio.flush()

            set_open_ssh_cache(check_host, True)
            return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            set_open_ssh_cache(check_host, False)
            return False
        finally:
            jumpbox_channel.close()
    else:
        try:
            with socket.create_connection((check_host, 22), timeout=3) as s:
                with socket.SocketIO(s, "rwb") as sio:
                    # read ssh server answer
                    sio.readline()

                    # send client hello
                    sio.write(b"SSH-2.0-PxcOnlineCheck_1.0\r\n")
                    sio.flush()

                set_open_ssh_cache(check_host, True)
                return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            set_open_ssh_cache(check_host, False)
            return False


# online checking can get quite excessive with lots of vms/lxcs
# to prevent running into ratelimits from firewalls we reuse our jump hosts
_async_jumphosts = {}
_async_jump_lock = asyncio.Lock()


# atexit handler, atexit doesnt handle async functions, this is why
# we wrap it here
async def cleanup_jumphosts_async():
    for conn in _async_jumphosts.values():
        conn.close()
        await conn.wait_closed()

    _async_jumphosts.clear()


# this should only be called once and at the very top of the ansible module
@contextmanager
def get_ssh_asyncio_loop():
    # loop we use for executing async functions
    loop = asyncio.new_event_loop()

    # patch custom attribute for checks
    loop._pxc_ssh_managed = True

    try:
        yield loop
    finally:
        loop.run_until_complete(cleanup_jumphosts_async())
        loop.close()


async def get_jump_host_async(jump_host: str = None, jump_user: str = "root"):
    # make sure methods get invoked in the proper context for cleanup
    if not getattr(asyncio.get_running_loop(), "_pxc_ssh_managed", False):
        raise RuntimeError(
            "Async ssh functions from pve_cloud.lib.ssh should be called with the get_ssh_asyncio_loop context!"
        )

    async with _async_jump_lock:
        if jump_host not in _async_jumphosts:
            _async_jumphosts[jump_host] = await asyncssh.connect(
                jump_host, username=jump_user, known_hosts=None
            )

        return _async_jumphosts[jump_host]


async def check_ssh_open_async(check_host: str, jump_host: str = None):
    cache_open = get_open_ssh_from_cache(check_host)
    if cache_open is not None:
        return cache_open

    jump_host_conn = None
    if jump_host:
        jump_host_conn = await get_jump_host_async(jump_host)

    try:
        if jump_host_conn:
            reader, writer = await asyncio.wait_for(
                jump_host_conn.open_connection(check_host, 22), timeout=2
            )
        else:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(check_host, 22), timeout=2
            )

        await asyncio.wait_for(reader.readline(), timeout=3)

        writer.write(b"SSH-2.0-PxcOnlineCheck_1.0\r\n")
        await writer.drain()

        writer.close()
        await writer.wait_closed()

        set_open_ssh_cache(check_host, True)
        return True
    except (
        asyncio.TimeoutError,
        OSError,
        ConnectionRefusedError,
        ChannelOpenError,
    ):
        set_open_ssh_cache(check_host, False)
        return False


async def wait_for_ssh_open_async(ip, jump_host: str = None):
    jump_host_conn = None
    if jump_host:
        jump_host_conn = await get_jump_host_async(jump_host)

    retries = 0
    max_retries = 30
    ports = (2222, 22)  # test custom port first

    while retries < max_retries:
        for ssh_port in ports:
            try:
                if jump_host_conn:
                    reader, writer = await asyncio.wait_for(
                        jump_host_conn.open_connection(ip, ssh_port), timeout=1
                    )
                else:
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection(ip, ssh_port), timeout=1
                    )
                await asyncio.wait_for(reader.readline(), timeout=3)

                writer.write(b"SSH-2.0-PxcOnlineCheck_1.0\r\n")
                await writer.drain()

                writer.close()
                await writer.wait_closed()

                return ssh_port
            except (
                asyncio.TimeoutError,
                OSError,
                ConnectionRefusedError,
                ChannelOpenError,
            ):
                retries += 1
                await asyncio.sleep(1)

    return None


@asynccontextmanager
async def connect_host_async(
    host: str,
    jump_host: str = None,
    port: int = 22,
    user: str = "root",
    jump_user: str = "root",
):
    jc = None
    if jump_host:
        jc = await get_jump_host_async(jump_host, jump_user)

    async with asyncssh.connect(
        host,
        username=user,
        known_hosts=None,
        # optionally pass tunnel here => equivalent to ansible ProxyJump
        tunnel=jc,
        port=port,
    ) as conn:
        yield conn
