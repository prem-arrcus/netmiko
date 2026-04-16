"""FRR (Free Range Routing) SSH driver for Netmiko.

This driver connects to a Linux host via SSH, then enters the FRR vtysh shell.
It supports configuration mode via 'configure terminal'.
"""

import time

from netmiko import log
from netmiko.cisco_base_connection import CiscoSSHConnection
from netmiko.docker import DockerExecBaseSession
from netmiko.exceptions import ReadTimeout
from netmiko.no_enable import NoEnable


class FrrConnection(NoEnable, CiscoSSHConnection):
    """Implement methods for interacting with FRR (Free Range Routing) devices.

    FRR runs on Linux and uses vtysh as its CLI interface. This driver:
    1. Connects to the Linux host via SSH
    2. Enters the vtysh shell
    3. Provides Cisco-like CLI interaction (since vtysh is Cisco-like)
    """

    def _modify_connection_params(self) -> None:
        """Modify connection parameters prior to SSH connection."""
        self.wait_for_bootup = False

    def session_preparation(self) -> None:
        """Prepare the session after the connection has been established."""
        self.vtysh_prompt = ""
        self.last_seen_prompt = ""

        # Wait for system bootup
        if self.wait_for_bootup:
            self.wait_for_system_bootup(10)

        # First, we're in Linux shell - need to enter vtysh
        self.enter_vtysh()
        log.info('In vty shell')

        # Now in vtysh, set up the session
        self.set_base_prompt()
        self.disable_paging(command="terminal length 0")

        # Clear the read buffer
        time.sleep(0.3 * self.global_delay_factor)
        log.info("Clearing buffer")
        self.clear_buffer()
        log.info("Done clearing buffer")

        self.vtysh_prompt = rf"{self.base_prompt}# "
        self.vtysh_cfg_prompt = rf"{self.base_prompt}\(config\)# "

    def enter_vtysh(self, vtysh_command: str = "vtysh") -> str:
        """Enter the vtysh shell from Linux shell.

        Args:
            vtysh_command: Command to enter vtysh (default: 'vtysh').
                          Can be 'sudo vtysh' if needed.

        Returns:
            Output from entering vtysh.

        """
        self.last_seen_prompt = ""
        if '@' not in self.find_prompt():
            # Probably in FRR's vty shell
            return ""

        # We are in linux prompt; Run "vtysh" command to reach FRR's vty shell
        log.debug('Entering vty shell')
        try:
            pattern = self.vtysh_prompt or r"[>#]"
            output = self._send_command_str(vtysh_command, expect_string=pattern)
            if 'try running me as a privileged user!' not in output:
                return output

            # We dont have the needed privileges. Let us try again with sudo
            vtysh_command = f"sudo {vtysh_command}"
        except ReadTimeout:
            # Didn't manage to hit the prompt.
            # Let us try again to check if we have hit the password prompt
            pass

        # Trying again, checking for password prompt
        output = self._send_command_timing_str(
            vtysh_command, strip_prompt=False, strip_command=False
        )

        # Handle sudo password prompt if present
        if "password: " in output.lower():
            if self.secret:
                output += self._send_command_timing_str(
                    self.secret, strip_prompt=False, strip_command=False
                )

        # Verify we're in vtysh (prompt should end with # or >)
        time.sleep(0.2 * self.global_delay_factor)
        return output

    def exit_vtysh(self) -> str:
        """Exit vtysh and return to Linux shell."""
        output = ""
        # First exit config mode if in it
        if self.check_config_mode():
            output += self.exit_config_mode(pattern=r"[#$]")

        # Exit vtysh
        self.last_seen_prompt = ""
        output += self._send_command_timing_str(
            "exit", strip_prompt=False, strip_command=False
        )
        return output

    def find_prompt(
        self, delay_factor: float = 1.0, pattern: str | None = None
    ) -> str:
        """Finds the current network device prompt, last line only.

        :param delay_factor: See __init__: global_delay_factor
        :type delay_factor: int

        :param pattern: Regular expression pattern to read until (not used in
        most situations).
        """
        # Check if we had cached the prompt during the last strip_prompt() call
        if self.last_seen_prompt and not pattern:
            log.info(f"In find_prompt: returning cached prompt: {self.last_seen_prompt}")
            return self.last_seen_prompt

        prompt = super().find_prompt(delay_factor=delay_factor, pattern=pattern)
        self.last_seen_prompt = prompt

        # find_prompt returns re.escape(prompt), which escapes '#' as '\\#`
        # return prompt.replace('\\#', '#')
        return prompt

    def check_config_mode(
        self, check_string: str = "(config", pattern: str = "", force_regex: bool = False
    ) -> bool:
        """Check if the device is in configuration mode.

        Arrcuscontain '(config' like:
        - hostname(config)#
        - hostname(config-router)#
        - hostname(config-if)#
        """
        prompt = self.last_seen_prompt
        log.info(f"in check_config_mode: last_seen_prompt={prompt}")
        out = check_string in self.find_prompt()
        self.last_seen_prompt = prompt
        return out

    def strip_prompt(self, a_string: str) -> str:
        """Strip the trailing router prompt from the output.

        :param a_string: Returned string from device
        :type a_string: str
        """
        # self.last_seen_prompt = ""
        return super().strip_prompt(a_string)

    def config_mode(
        self,
        config_command: str = "configure terminal",
        pattern: str = "",
        re_flags: int = 0,
    ) -> str:
        """Enter configuration mode.

        Args:
            config_command: Command to enter config mode (default: 'configure terminal')
            pattern: Pattern to match after entering config mode
            re_flags: Regex flags

        Returns:
            Output from entering config mode.
        """
        log.info("Entering config mode")
        self.last_seen_prompt = ""
        pattern = pattern or self.vtysh_cfg_prompt
        return super().config_mode(
            config_command=config_command, pattern=pattern, re_flags=re_flags
        )

    def exit_config_mode(self, exit_config: str = "end", pattern: str = "") -> str:
        """Exit from configuration mode.

        Args:
            exit_config: Command to exit config mode (default: 'end')
            pattern: Pattern to match after exiting

        Returns:
            Output from exiting config mode.
        """
        self.last_seen_prompt = ""
        pattern: str = pattern or self.vtysh_prompt
        return super().exit_config_mode(exit_config=exit_config, pattern=pattern)

    def set_base_prompt(
        self,
        pri_prompt_terminator: str = "#",
        alt_prompt_terminator: str = ">",
        delay_factor: float = 1.0,
        pattern: str | None = None,
    ) -> str:
        """Set the base prompt for the device.

        FRR vtysh prompts are typically:
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

    def save_config(
        self,
        cmd: str = "write memory",
        confirm: bool = False,
        confirm_response: str = "",
    ) -> str:
        """Save the running configuration to startup configuration.

        Args:
            cmd: Command to save config (default: 'write memory')
            confirm: Whether confirmation is needed
            confirm_response: Response to confirmation prompt

        Returns:
            Output from save command.

        """
        output = ""
        if self.check_config_mode():
            output += self.exit_config_mode()

        output += self._send_command_str(
            command_string=cmd,
            strip_prompt=False,
            strip_command=False,
            read_timeout=60.0,
        )
        return output

    def cleanup(self, command: str = "exit") -> None:
        """Gracefully exit the SSH session."""
        try:
            # Exit config mode if in it
            if self.check_config_mode():
                self.exit_config_mode()
        except Exception as err:
            log.warning(f'Seen error when trying to exit from vtysh config mode. Error: {err}')
            pass

        try:
            # Exit vtysh first
            self.exit_vtysh()
        except Exception as err:
            log.warning(f'Seen error when trying to exit from vtysh. Error: {err}')
            pass

        # Exit Linux shell
        if self.session_log:
            self.session_log.fin = True
        self.write_channel(command + self.RETURN)


class FrrSSH(FrrConnection):
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


class FrrDockerExecSession(FrrSSH, DockerExecBaseSession):
    """Implement methods for interacting with FRR devices over DockerExec Session."""

    def __init__(self, host: str, collect_boot_logs: bool = False, **kwargs: dict) -> None:
        """Init method for this Docker Exec session."""
        DockerExecBaseSession.__init__(self, host, collect_boot_logs, **kwargs)

    def session_preparation(self) -> None:
        """Prepare the session after the connection has been established."""
        # Invoke Docker's session_preparation first
        DockerExecBaseSession.session_preparation(self)
        super().session_preparation()
