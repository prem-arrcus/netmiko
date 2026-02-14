"""FRR (Free Range Routing) SSH driver for Netmiko.

This driver connects to a Linux host via SSH, then enters the FRR vtysh shell.
It supports configuration mode via 'configure terminal'.
"""

from typing import Any, Optional
import re
import time

from netmiko import log
from netmiko.cisco_base_connection import CiscoSSHConnection
from netmiko.no_enable import NoEnable
from netmiko.exceptions import ReadTimeout


class FrrSSH(NoEnable, CiscoSSHConnection):
    """
    Implement methods for interacting with FRR (Free Range Routing) devices.

    FRR runs on Linux and uses vtysh as its CLI interface. This driver:
    1. Connects to the Linux host via SSH
    2. Enters the vtysh shell
    3. Provides Cisco-like CLI interaction (since vtysh is Cisco-like)
    """

    def session_preparation(self) -> None:
        """Prepare the session after the connection has been established."""
        self.last_seen_prompt = ""

        # First, we're in Linux shell - need to enter vtysh
        self._test_channel_read(pattern=r"[$#>]")
        time.sleep(0.3 * self.global_delay_factor)

        # Enter vtysh
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

    def enter_vtysh(self, vtysh_command: str = "vtysh") -> str:
        """Enter the vtysh shell from Linux shell.

        Args:
            vtysh_command: Command to enter vtysh (default: 'vtysh').
                          Can be 'sudo vtysh' if needed.

        Returns:
            Output from entering vtysh.
        """
        if '@' not in self.find_prompt():
            # Probably in FRR's vty shell
            return ""

        # We are in linux prompt; Run "vtysh" command to reach FRR's vty shell
        log.debug('Entering vty shell')
        try:
            output = self._send_command_str(vtysh_command, expect_string=r"[>#]")
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
            output += self.exit_config_mode()

        # Exit vtysh
        output += self._send_command_timing_str(
            "exit", strip_prompt=False, strip_command=False
        )
        return output

    def check_config_mode(
        self, check_string: str = "(config", pattern: str = "", force_regex: bool = False
    ) -> bool:
        """Check if the device is in configuration mode.

        FRR config mode prompts contain '(config' like:
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
        else:
            return a_string

    def find_prompt(
        self, delay_factor: float = 1.0, pattern: Optional[str] = None
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

        return super().find_prompt(
            delay_factor=delay_factor, pattern=pattern 
        )

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
        alt_prompt_terminator: str = ">",
        delay_factor: float = 1.0,
        pattern: Optional[str] = None,
    ) -> str:
        """Set the base prompt for the device.

        FRR vtysh prompts are typically:
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


class FrrTelnet(FrrSSH):
    """FRR driver for Telnet connections."""

    pass
