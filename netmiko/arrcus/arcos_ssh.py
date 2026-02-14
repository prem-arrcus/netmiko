"""Arrcus - Arcos SSH driver for Netmiko.

This driver connects to a Linux host via SSH, then enters the ConfD cli shell.
It supports configuration mode via 'configure terminal'.
"""

import time

from netmiko import log
from netmiko.cisco_base_connection import CiscoSSHConnection
from netmiko.exceptions import ReadTimeout
from netmiko.no_enable import NoEnable


class ArcosSSH(NoEnable, CiscoSSHConnection):
    """Implements methods for interacting with Arrcus devices.

    Arrcus nodes have a debian OS and ConfD CLI interface. This driver:
    1. Connects to the Linux host via SSH
    2. Enters the ConfD CLI shell
    3. Provides Cisco-like CLI interaction (since ConfD CLI is Cisco-like)
    """

    def session_preparation(self) -> None:
        """Prepare the session after the connection has been established."""
        self.last_seen_prompt = ""

        # First, we're in Linux shell - need to enter vtysh
        self._test_channel_read(pattern=r"[$#>]")
        time.sleep(0.3 * self.global_delay_factor)

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
            Exception `ValueError` when entering confd cli shell fails

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
        except ReadTimeout:
            # Didn't manage to hit the prompt.
            raise ValueError(
                "Failed to see the expected prompt on trying to enter ConfD CLI shell"
            )

    def exit_confd_cli(self) -> str:
        """Exit ConfD CLI shell and return to Linux shell."""
        output = ""
        # First exit config mode if in it
        if self.check_config_mode():
            output += self.exit_config_mode()

        # Exit ConfD CLI shell
        output += self._send_command_timing_str(
            "exit", strip_prompt=False, strip_command=False
        )
        return output

    def check_config_mode(
        self, check_string: str = "(config", pattern: str = "", force_regex: bool = False
    ) -> bool:
        """Check if the device is in configuration mode.

        Arrcuscontain '(config' like:
        - hostname(config)#
        - hostname(config-router)#
        - hostname(config-if)#
        """
        log.info("in check_config_mode")
        return check_string in self.find_prompt()

    def strip_prompt(self, a_string: str) -> str:
        """Strip the trailing router prompt from the output.

        :param a_string: Returned string from device
        :type a_string: str
        """
        self.last_seen_prompt = ""
        response_list = a_string.split(self.RESPONSE_RETURN)
        last_line = response_list[-1]

        if self.base_prompt in last_line:
            self.last_seen_prompt = last_line
            return self.RESPONSE_RETURN.join(response_list[:-1])
        return a_string

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
        self.last_seen_prompt = ""
        return super().config_mode(
            config_command=config_command, pattern=pattern, re_flags=re_flags
        )

    def exit_config_mode(self, exit_config: str = "end", pattern: str = r"#.*") -> str:
        """Exit from configuration mode.

        Args:
            exit_config: Command to exit config mode (default: 'end')
            pattern: Pattern to match after exiting

        Returns:
            Output from exiting config mode.

        """
        self.last_seen_prompt = ""
        return super().exit_config_mode(exit_config=exit_config, pattern=pattern)

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
        if self.check_config_mode():
            self.exit_config_mode()

        output = self._send_command_str(
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
        except Exception:
            pass

        try:
            # Exit vtysh first
            self.exit_vtysh()
        except Exception:
            pass

        # Exit Linux shell
        if self.session_log:
            self.session_log.fin = True
        self.write_channel(command + self.RETURN)
