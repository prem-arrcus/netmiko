"""Arrcus - Arcos SSH driver for Netmiko.

This driver connects to a Linux host via SSH, then enters the ConfD cli shell.
It supports configuration mode via 'configure terminal'.
"""

import time

from typing import Iterator, Sequence, TextIO

from netmiko import log
from netmiko.cisco_base_connection import CiscoSSHConnection
from netmiko.docker import DockerExecBaseSession
from netmiko.exceptions import ReadTimeout
from netmiko.no_enable import NoEnable


class ArcosConnection(NoEnable, CiscoSSHConnection):
    """Implements methods for interacting with Arrcus devices.

    Arrcus nodes have a debian OS and ConfD CLI interface. This driver:
    1. Connects to the Linux host via SSH
    2. Enters the ConfD CLI shell
    3. Provides Cisco-like CLI interaction (since ConfD CLI is Cisco-like)
    """

    def _modify_connection_params(self) -> None:
        """Modify connection parameters prior to SSH connection."""
        self.wait_for_bootup = False

    def session_preparation(self) -> None:
        """Prepare the session after the connection has been established."""
        self.last_seen_prompt = ""

        # We would now be in Linux shell
        self._test_channel_read(pattern=r"[$#>]")
        time.sleep(0.3 * self.global_delay_factor)

        # Wait for system bootup
        if self.wait_for_bootup:
            self.wait_for_system_bootup(120)

        # Check confd status
        log.info('Checking if Confd is in active state')
        self._send_command_timing_str("systemctl is-active confd")
        for _ in range(20):
            out = self._send_command_timing_str("echo 'show confd-state daemon-status' | cli")
            if "confd-state daemon-status started" in out:
                break
            time.sleep(3)
        else:
            raise ValueError(f"Confd not ready even after waiting for {20*3}s")
        log.info('Confd is in active state')

        # Enter confd cli shell
        self.enter_confd_cli()
        log.info('In confd')

        # Now in confd cli shell, set up the session
        self.set_base_prompt()
        self.disable_paging(command="screen-length 0")
        self.set_terminal_width(command="screen-width 512")

        # Clear the read buffer
        time.sleep(0.3 * self.global_delay_factor)
        log.info("Clearing buffer")
        self.clear_buffer()
        log.info("Done clearing buffer")

    def enter_confd_cli(self, confd_command: str = "cli") -> str:
        """Enter the ConfD CLI shell from Linux shell.

        Args:
            confd_command: Command to enter ConfD CLI shell (default: 'cli').

        Returns:
            Output from entering ConfD CLI shell.

        Raises:
            ValueError: when entering confd cli shell fails

        """
        if ':' not in self.find_prompt():
            # Probably in ConfD CLI shell
            return ""

        # We are in linux prompt; Run "cli" command to reach ConfD CLI shell
        log.debug('Entering ConfD CLI shell shell')
        try:
            output = self._send_command_str(confd_command, expect_string=r"[>#]")
            time.sleep(0.2 * self.global_delay_factor)
            return output
        except ReadTimeout as err:
            # Didn't manage to hit the prompt.
            raise ValueError(
                "Failed to see the expected prompt on trying to enter ConfD CLI shell"
            ) from err

    def exit_confd_cli(self) -> str:
        """Exit ConfD CLI shell and return to Linux shell.

        Returns:
            the console output

        """
        output = ""
        # First exit config mode if in it
        if self.check_config_mode():
            output += self.exit_config_mode()

        # Exit ConfD CLI shell
        output += self._send_command_timing_str(
            "exit", strip_prompt=False, strip_command=False
        )
        return output

    def check_config_mode(self, check_string: str = "(config") -> bool:
        """Check if the device is in configuration mode.

        Arrcuscontain '(config' like:
        - hostname(config)#
        - hostname(config-router)#
        - hostname(config-if)#

        Returns:
            True if in config mode, else False

        """
        log.info("in check_config_mode")
        return check_string in self.find_prompt()

    def strip_prompt(self, a_string: str) -> str:
        """Strip the trailing router prompt from the output.

        :param a_string: Returned string from device
        :type a_string: str

        Returns:
            the stripped prompt

        """
        self.last_seen_prompt = ""
        return super().strip_prompt(a_string)

    def find_prompt(
        self, delay_factor: float = 1.0, pattern: str | None = None
    ) -> str:
        """Finds the current network device prompt, last line only.

        :param delay_factor: See __init__: global_delay_factor
        :type delay_factor: int

        :param pattern: Regular expression pattern to read until (not used in
        most situations).

        Returns:
            the cached or found prompt

        """
        # Check if we had cached the prompt during the last strip_prompt() call
        if self.last_seen_prompt:
            log.info(f"In find_prompt: returning cached prompt: {self.last_seen_prompt}")
            return self.last_seen_prompt

        prompt = super().find_prompt(delay_factor=delay_factor, pattern=pattern)

        # find_prompt returns re.escape(prompt), which escapes '#' as '\\#`
        return prompt.replace('\\#', '#')

    def config_mode(
        self,
        config_command: str = "config terminal",
        pattern: str = "",
        re_flags: int = 0,
    ) -> str:
        """Enter configuration mode.

        Args:
            config_command: Command to enter config mode (default: 'config terminal')
            pattern: Pattern to match after entering config mode
            re_flags: Regex flags

        Returns:
            Output from entering config mode.

        """
        log.info("Entering config mode")
        self.config_changed = False
        self.last_seen_prompt = ""
        return super().config_mode(
            config_command=config_command, pattern=pattern or r'\(config\)# ', re_flags=re_flags
        )

    def exit_config_mode(self, exit_config: str = "end", pattern: str = r"#.*") -> str:
        """Exit from configuration mode.

        Args:
            exit_config: Command to exit config mode (default: 'end')
            pattern: Pattern to match after exiting

        Returns:
            Output from exiting config mode.

        """
        output = ""

        # Commit before exit
        log.info("Exiting config mode")
        if self.config_changed:
            output += self.commit()
            self.config_changed = False

        self.last_seen_prompt = ""
        output += super().exit_config_mode(exit_config=exit_config, pattern=pattern)
        return output

    def set_base_prompt(
        self,
        pri_prompt_terminator: str = "#",
        alt_prompt_terminator: str = "",
        delay_factor: float = 1.0,
        pattern: str | None = None,
    ) -> str:
        """Set the base prompt for the device.

        Typically:
        - hostname# (privileged mode)
        - hostname> (user mode, less common)

        Returns:
            the base prompt

        """
        return super().set_base_prompt(
            pri_prompt_terminator=pri_prompt_terminator,
            alt_prompt_terminator=alt_prompt_terminator,
            delay_factor=delay_factor,
            pattern=pattern,
        )

    # def send_command(self, command_string: str, **kwargs: dict) -> str:
    #     """Execute command_string on the SSH channel using a pattern-based mechanism.

    #     Generally used for show commands. By default this method will keep waiting to receive data
    #     until the network device prompt is detected. The current network device prompt will be
    #     determined automatically.

    #     :param command_string: The command to be executed on the remote device.

    #     Returns:
    #         the console output seen when sending the config commands

    #     """
    #     output = ""

    #     output += super().send_command(command_string, **kwargs)

    #     return output

    def send_config_set(
          self,
          config_commands: str | Sequence[str] | Iterator[str] | TextIO | None = None,
          *,
          exit_config_mode: bool = False,
          enter_config_mode: bool = False,
          commit_config: bool = False,
          **kwargs: dict
    ) -> str:
        """Send configuration commands down the SSH channel.

        config_commands is an iterable containing all of the configuration commands.
        The commands will be executed one after the other.

        Automatically enters configuration mode.

        :param config_commands: Multiple configuration commands to be sent to the device

        :param exit_config_mode: Determines whether or not to exit config mode after complete

        :param enter_config_mode: Do you enter config mode before sending config commands

        :param commit_config: Do you want to commit the config after sending config commands

        Returns:
            the console output seen when sending the config commands

        """
        output = ""

        if not self.check_config_mode():
            output += self.config_mode()

        output += super().send_config_set(
            config_commands,
            enter_config_mode=enter_config_mode,
            exit_config_mode=exit_config_mode,
            **kwargs
        )

        # Config has been sent
        if "commit" not in config_commands:
            self.config_changed = True

        if commit_config:
            output += self.commit()

        return output

    def commit(
        self,
        comment: str = "",
        read_timeout: float = 120.0,
        delay_factor: float | None = None,
    ) -> str:
        """Commit the candidate configuration.

        Commit the entered configuration. Raise an error and return the failure
        if the commit fails.

        default:
           command_string = commit
        comment:
           command_string = commit comment <comment>

        delay_factor: Deprecated in Netmiko 4.x. Will be eliminated in Netmiko 5.

        Returns:
            console output str

        Raises:
            ValueError: when commit fails

        """
        if delay_factor is not None:
            warnings.warn(DELAY_FACTOR_DEPR_SIMPLE_MSG, DeprecationWarning)

        error_marker = ["Failed to generate committed config", "Commit failed"]
        command_string = "commit"

        if comment:
            command_string += f' comment "{comment}"'

        output = ""
        if not self.check_config_mode():
            output += self.config_mode()
        output += self._send_command_str(
            command_string,
            strip_prompt=False,
            strip_command=False,
            read_timeout=read_timeout,
        )

        if any(x in output for x in error_marker):
            raise ValueError(f"Commit failed with following errors:\n\n{output}")

        # Config has been committed
        self.config_changed = False

        return output

    def save_config(
        self,
        cmd: str = "",
        cfg_file = "startup.cfg",
        confirm: bool = True,
        confirm_response: str = "yes"
    ) -> str:
        """Save the running configuration to startup configuration.

        Args:
            cmd: Command to save config (default: 'save <cfg_file>')
            cfg_file: Config file location(default: 'startup.cfg')
            confirm: Whether confirmation is needed

        Returns:
            Output from save command.

        """
        output = ""
        if not self.check_config_mode():
            output += self.config_mode()

        log.info(f'Saving config to {cfg_file}')
        cmd = cmd or f"save {cfg_file}"
        output += super().save_config(
            cmd=cmd, confirm=confirm, confirm_response=confirm_response
        )
        return output

    def cleanup(self, command: str = "exit") -> None:
        """Gracefully exit the SSH session."""
        try:
            # Exit config mode if in it
            if self.check_config_mode():
                self.exit_config_mode()
        except Exception as err:
            log.error(f"Seen error during cleanup. Error: {err.__class__.__name__}: {err}")

        try:
            # Exit confd shell
            self.exit_confd_cli()
        except Exception as err:
            log.error(f"Seen error during cleanup. Error: {err.__class__.__name__}: {err}")

        # Exit Linux shell
        if self.session_log:
            self.session_log.fin = True
        self.write_channel(command + self.RETURN)

class ArcosSSH(ArcosConnection):
    """Implement methods for interacting with FRR  devices over SSH."""

    def session_preparation(self) -> None:
        """Prepare the session after the connection has been established."""
        self._test_channel_read(pattern=r"[$#>]")
        time.sleep(0.3 * self.global_delay_factor)

        # Disable bracketed paste
        self.write_channel(" bind 'set enable-bracketed-paste off'\n")
        # Disable colors
        self.write_channel(" export TERM=dumb\n")
        # use simple prompt
        self.write_channel(r" export PS1='\u@\h:\w\$ '" + "\n")
        # disable window title changes
        self.write_channel(" unset PROMPT_COMMAND\n")

        super().session_preparation()


class ArcosDockerExecSession(ArcosSSH, DockerExecBaseSession):
    """Implement methods for interacting with FRR devices over DockerExec Session."""

    def __init__(self, host: str, collect_boot_logs: bool = False, **kwargs: dict) -> None:
        """Init method for this Docker Exec session."""
        DockerExecBaseSession.__init__(self, host, collect_boot_logs, **kwargs)


    def session_preparation(self) -> None:
        """Prepare the session after the connection has been established."""
        # Invoke Docker's session_preparation first
        self.wait_for_bootup = True
        DockerExecBaseSession.session_preparation(self)
        super().session_preparation()
