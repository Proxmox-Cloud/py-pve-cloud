import asyncio
import socket
from contextlib import asynccontextmanager, contextmanager

import asyncssh
import paramiko
import yaml
from asyncssh.misc import ChannelOpenError


# generic connect to proxmox cluster through optional jump host
# assumes pve host and jump host to be ssh root accessible
@contextmanager
def connect_host(
    host: str, jump_host: str = None, user: str = "root", jump_user: str = "root"
):
    jumpbox_channel = None
    jumpbox = None
    if jump_host:
        jumpbox = paramiko.SSHClient()
        jumpbox.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        jumpbox.connect(jump_host, username=jump_user)

        jumpbox_transport = jumpbox.get_transport()
        src_addr = ("127.0.0.1", 0)
        dest_addr = (host, 22)

        jumpbox_channel = jumpbox_transport.open_channel(
            "direct-tcpip", dest_addr, src_addr
        )

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    ssh.connect(host, username=user, sock=jumpbox_channel)

    try:
        yield ssh
    finally:
        ssh.close()

        if jump_host:
            jumpbox_channel.close()
            jumpbox.close()


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
        jumpbox = paramiko.SSHClient()
        jumpbox.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        jumpbox.connect(jump_host, username="root")
        jumpbox_channel = None

        try:
            jumpbox_transport = jumpbox.get_transport()
            src_addr = ("127.0.0.1", 0)
            dest_addr = (check_host, 22)

            jumpbox_channel = jumpbox_transport.open_channel(
                "direct-tcpip", dest_addr, src_addr
            )

            return check_ssh_open_tun(jumpbox_channel)

        finally:
            if jumpbox_channel is not None:
                jumpbox_channel.close()

            jumpbox.close()
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


async def check_ssh_open_async(check_host: str, jump_host: str = None):
    jump_host_conn = None
    if jump_host:
        jump_host_conn = await asyncssh.connect(
            jump_host, username="root", known_hosts=None
        )

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
    finally:
        if jump_host_conn:
            jump_host_conn.close()
            await jump_host_conn.wait_closed()


async def wait_for_ssh_open_async(ip, jump_host: str = None):
    jump_host_conn = None
    if jump_host:
        jump_host_conn = await asyncssh.connect(
            jump_host, username="root", known_hosts=None
        )

    try:
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
    finally:
        if jump_host_conn:
            jump_host_conn.close()
            await jump_host_conn.wait_closed()

    return None


@asynccontextmanager
async def connect_host_async(
    host: str, jump_host: str = None, port: int = 22, user: str = "root", jump_user: str = "root"
):
    jc = None
    if jump_host:
        jc = await asyncssh.connect(jump_host, username=jump_user, known_hosts=None)

    try:
        async with asyncssh.connect(
            host,
            username=user,
            known_hosts=None,
            # optionally pass tunnel here => equivalent to ansible ProxyJump
            tunnel=jc,
            port=port
        ) as conn:
            yield conn

    finally:
        if jc:
            jc.close()
            await jc.wait_closed()
