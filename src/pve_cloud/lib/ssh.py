import asyncio
import socket
from contextlib import asynccontextmanager, contextmanager

import asyncssh
import paramiko
import atexit
from asyncssh.misc import ChannelOpenError
from contextlib import contextmanager


# jump host connection cache
_jump_hosts = {}

def get_jump_host_chan(host:str, jump_host: str, jump_user: str = "root"):
    if jump_host not in _jump_hosts:
        jumpbox = paramiko.SSHClient()
        jumpbox.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        jumpbox.connect(jump_host, username=jump_user)

        _jump_hosts[jump_host] = jumpbox

    return _jump_hosts[jump_host].get_transport().open_channel(
        "direct-tcpip",
        (host, 22),
        ("127.0.0.1", 0)
    )


# cleanup hook
def cleanup_jumphosts():
    for jumpbox in _jump_hosts.values():
        jumpbox.close()


atexit.register(cleanup_jumphosts)

# generic connect to proxmox cluster through optional jump host
# assumes pve host and jump host to be ssh root accessible
@contextmanager
def connect_host(
    host: str, jump_host: str = None, user: str = "root", jump_user: str = "root"
):
    jumpbox_channel = get_jump_host_chan(host, jump_host, jump_user) if jump_host else None

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    ssh.connect(host, username=user, sock=jumpbox_channel)

    try:
        yield ssh
    finally:
        ssh.close()

        if jumpbox_channel:
            jumpbox_channel.close()


def check_ssh_open_tun(tun: paramiko.Channel):
    try:
        tun.settimeout(3)

        # Use makefile() instead of socket.SocketIO to safely create a read/write stream
        with tun.makefile("rwb") as sio:
            # read ssh server answer
            sio.readline()

            # send client hello
            sio.write(b"SSH-2.0-PxcOnlineCheck_1.0\r\n")
            sio.flush()

        return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


def check_ssh_open(check_host: str, jump_host: str = None):
    if jump_host:
        jumpbox_channel = get_jump_host_chan(check_host, jump_host)

        try:
            return check_ssh_open_tun(jumpbox_channel)
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

                return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            return False


# online checking can get quite excessive with lots of vms/lxcs
# to prevent running into ratelimits from firewalls we reuse our jump hosts
_async_jumphosts = {}

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
    if not getattr("_pxc_ssh_managed", asyncio.get_running_loop(), False):
        raise RuntimeError("Async ssh functions from pve_cloud.lib.ssh should be called with the get_ssh_asyncio_loop context!")
    
    if jump_host not in _async_jumphosts:
        print("initting jump host", jump_host)
        _async_jumphosts[jump_host] = await asyncssh.connect(
            jump_host, username=jump_user, known_hosts=None
        )
    
    return _async_jumphosts[jump_host]


async def check_ssh_open_async(check_host: str, jump_host: str = None):
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

        return True
    except (
        asyncio.TimeoutError,
        OSError,
        ConnectionRefusedError,
        ChannelOpenError,
    ):
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
    host: str, jump_host: str = None, port: int = 22, user: str = "root", jump_user: str = "root"
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
        port=port
    ) as conn:
        yield conn



