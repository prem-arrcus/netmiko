"""Netmiko module to handle docker exec sessions."""

import select
import socket
import time

import docker

from netmiko import log
from netmiko.base_connection import BaseConnection


class DockerChannel:
    """Low level transport based on PTY socket from docker exec."""

    def __init__(self, container_name: str, cmd: str = "bash") -> None:
        """Initialize the Docker Exec Channel."""
        self.client = docker.from_env()
        self.container = self.client.containers.get(container_name)

        exec_id = self.client.api.exec_create(
            container=self.container.id,
            cmd=cmd,
            stdin=True,
            tty=True,
        )["Id"]

        sock = self.client.api.exec_start(
            exec_id,
            tty=True,
            stream=False,
            socket=True,
        )

        self.sock = sock._sock
        self.sock.setblocking(False)

    def write(self, data: bytes):
        self.sock.send(data)

    def read(self) -> bytes:
        try:
            return self.sock.recv(65535)
        except BlockingIOError:
            return b""

    def is_data_available(self):
        r, _, _ = select.select([self.sock], [], [], 0)
        return bool(r)

    def close(self):
        self.sock.close()


# Netmiko Channel Adapter
class NetmikoChannelAdapter:
    """Adapter Channel that emulates netmiko's SSHChannel.

    Provides read_channel() and write_channel() apis
    """

    def __init__(self, docker_channel):
        self.transport = docker_channel
        self._buffer = b""

    def write_channel(self, data: str) -> None:
        if isinstance(data, str):
            data = data.encode()
        self.transport.write(data)

    def read_channel(self) -> str:
        output = b""

        # Non-blocking gather
        start = time.time()
        while time.time() - start < 0.1:
            if self.transport.is_data_available():
                output += self.transport.read()
            else:
                break

        return output.decode(errors="ignore")

    def read_channel_timing(self, delay_factor=1.0, max_loops=150):
        output = ""
        loops = 0

        while loops < max_loops:
            time.sleep(0.1 * delay_factor)
            data = self.read_channel()

            if data:
                output += data
                loops = 0
            else:
                loops += 1

        return output

    def close(self):
        self.transport.sock.shutdown(socket.SHUT_RDWR);
        self.transport.close()


class DockerExecBaseSession(BaseConnection):
    """Netmiko Driver for Docker.

    Uses docker's socket connection to its containers (instead of SSH)

    """

    def __init__(self, host, collect_boot_logs: bool = False, **kwargs):
        self.container = host
        self.last_seen_prompt = ""
        self.collect_boot_logs = collect_boot_logs
        super().__init__(host="docker", **kwargs)

    def establish_connection(self) -> None:
        """Override SSH connection completely."""
        # Create docker TTY
        self.remote_conn = DockerChannel(self.container)

        # Create Netmiko-compatible channel
        self.channel = NetmikoChannelAdapter(self.remote_conn)

        # These attributes are referenced internally
        self.remote_conn_pre = self.channel

        # Collect boot logs
        if self.collect_boot_logs and self.session_log:
            self.session_log.write(self.remote_conn.container.logs().decode())

        # Wait till channel is readable
        self._test_channel_read()

    def session_preparation(self) -> None:
        """Prepare the session after the connection has been established."""
        self.wait_for_bootup = True

        # Let Netmiko do its normal CLI preparation
        self.set_base_prompt()
        self.last_seen_prompt = self.base_prompt
        self.disable_paging(command=" stty rows 0")
        self.set_terminal_width(command=" stty cols 511")

    def wait_for_system_bootup(self, timeout: int = 10) -> None:
        """Wait till system boots up fully."""
        log.info('Checking if system jas fully booted up')
        cmd = "systemctl is-system-running --wait"
        out = self._send_command_str(
                f"timeout {timeout} {cmd} || echo Fail",
                expect_string=r"[#\$]",
                read_timeout=timeout + 5,
        )
        if out == "Fail":
            raise ValueError(
                 "Docker container not fully up and running after bootup "
                f"even after waiting for {timeout}s"
        )
        log.info('System is up and running')

    def close(self):
        self.channel.close()

class DockerExecSession(DockerExecBaseSession):
    """Netmiko Connection Handler for Docker Nodes over Exec session."""

    def session_preparation(self) -> None:
        """Prepare the session after the connection has been established."""
        self.setup_terminal()
        super().session_preparation()

    def setup_terminal(self) -> None:
        # disable window title changes and systemd OSC context sequences
        self.write_channel(" unset PROMPT_COMMAND PS0\n")
        # disable bracketed paste
        self.write_channel(" bind 'set enable-bracketed-paste off'\n")
        # Disable colors
        self.write_channel(" export TERM=dumb\n")
        # use simple prompt
        self.write_channel(r" export PS1='\u@\h:\w\$ '" + "\n")

    def find_prompt(
        self, delay_factor: float = 1.0, pattern: str | None = None
    ) -> str:
        """Find the current network device prompt.

        Use the cached prompt info, if it exists. Otherwise find the current prompt.

        Returns:
            Cached or found prompt

        """
        # Check if we had cached the prompt during the last strip_prompt() call
        if self.last_seen_prompt and not pattern:
            return self.last_seen_prompt

        prompt = super().find_prompt(delay_factor=delay_factor, pattern=pattern)

        # find_prompt returns re.escape(prompt), which escapes '#' as '\\#`
        return prompt.replace('\\#', '#')
