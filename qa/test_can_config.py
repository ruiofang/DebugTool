"""Configuration planning and authorization tests; no network link is changed."""

import io
import json
import subprocess
import unittest
from unittest.mock import Mock, patch

import can_config as config


def physical_link(up=False, bitrate=None, fd=False, data_bitrate=None):
    data = {}
    if bitrate is not None:
        data['bittiming'] = {'bitrate': bitrate}
    if data_bitrate is not None:
        data['data_bittiming'] = {'bitrate': data_bitrate}
    return {'ifname': 'can_qa', 'flags': ['UP'] if up else [],
            'mtu': 72 if fd else 16,
            'linkinfo': {'info_kind': 'can', 'info_data': data}}


class CanConfigurationTests(unittest.TestCase):
    def test_down_classic_link_sets_bitrate_then_up_without_unsupported_fd_option(self):
        commands = config.configuration_commands(physical_link(), 'can_qa', 500000, False, 2000000)
        self.assertEqual(commands, [
            ['link', 'set', 'dev', 'can_qa', 'type', 'can', 'bitrate', '500000'],
            ['link', 'set', 'dev', 'can_qa', 'up']])

    def test_up_link_with_different_bitrate_is_stopped_before_reconfiguration(self):
        commands = config.configuration_commands(physical_link(True, 125000), 'can_qa',
                                                  250000, False, 2000000)
        self.assertEqual(commands[0], ['link', 'set', 'dev', 'can_qa', 'down'])
        self.assertEqual(commands[1][-2:], ['bitrate', '250000'])

    def test_matching_active_link_needs_no_changes(self):
        self.assertEqual(config.configuration_commands(physical_link(True, 500000),
                         'can_qa', 500000, False, 2000000), [])
        self.assertEqual(config.configuration_commands(physical_link(True, 500000, True, 2000000),
                         'can_qa', 500000, True, 2000000), [])

    def test_fd_and_data_rate_changes_are_applied(self):
        commands = config.configuration_commands(physical_link(True, 500000, True, 1000000),
                                                 'can_qa', 500000, True, 2000000)
        self.assertEqual(commands[1][-4:], ['dbitrate', '2000000', 'fd', 'on'])

    def test_fd_to_classic_explicitly_disables_fd(self):
        commands = config.configuration_commands(physical_link(False, 500000, True),
                                                 'can_qa', 500000, False, 2000000)
        self.assertEqual(commands[0][-2:], ['fd', 'off'])

    def test_virtual_link_has_no_bitrate_commands(self):
        link = {'flags': [], 'mtu': 72, 'linkinfo': {'info_kind': 'vcan'}}
        self.assertEqual(config.configuration_commands(link, 'vcan_qa', 500000, True, 2000000),
                         [['link', 'set', 'dev', 'vcan_qa', 'up']])
        link['flags'] = ['UP']
        self.assertEqual(config.configuration_commands(link, 'vcan_qa', 125000, True, 2000000), [])

    def test_virtual_link_fd_requires_sufficient_mtu(self):
        link = {'flags': [], 'mtu': 16, 'linkinfo': {'info_kind': 'vcan'}}
        with self.assertRaisesRegex(config.ConfigError, 'MTU'):
            config.configuration_commands(link, 'vcan_qa', 500000, True, 2000000)

    def test_parameters_are_validated_before_any_commands(self):
        for interface in ('', '../can0', 'can 0', 'can0\x00', 'x' * 16):
            with self.subTest(interface=interface), self.assertRaises(config.ConfigError):
                config.validate_request(interface, 500000, False, 2000000)
        for value in (0, -1, True, 1.5, '500000', 100000001):
            with self.subTest(value=value), self.assertRaises(config.ConfigError):
                config.validate_request('can0', value, False, 2000000)

    def test_non_can_link_is_rejected_before_ip(self):
        with patch('can_config.Path.read_text', return_value='1'), patch('can_config._run_ip') as run:
            with self.assertRaisesRegex(config.ConfigError, '不是 CAN'):
                config.read_link('/bin/ip', 'eth0')
            run.assert_not_called()

    def test_read_link_requires_matching_structured_json(self):
        with patch('can_config.Path.read_text', return_value='280'), patch('can_config._run_ip') as run:
            for output in ('invalid', '[]', '[{"ifname":"other"}]',
                           '[{"ifname":"can_qa","linkinfo":{}}]',
                           '[{"ifname":"can_qa","linkinfo":{},"flags":"UP"}]'):
                run.return_value = output
                with self.subTest(output=output), self.assertRaises(config.ConfigError):
                    config.read_link('/bin/ip', 'can_qa')
            link = physical_link()
            run.return_value = json.dumps([link])
            self.assertEqual(config.read_link('/bin/ip', 'can_qa'), link)

    def test_capability_check_uses_effective_bit_not_root_uid(self):
        with patch('can_config.Path.read_text', return_value='Name: test\nCapEff:\t0000000000001000\n'):
            self.assertTrue(config.has_net_admin())
        with patch('can_config.Path.read_text', return_value='CapEff:\t0000000000000000\n'):
            self.assertFalse(config.has_net_admin())

    def test_no_capability_never_mutates_link(self):
        with patch('can_config.find_ip', return_value='/bin/ip'), \
                patch('can_config.read_link', return_value=physical_link()), \
                patch('can_config.has_net_admin', return_value=False), \
                patch('can_config._run_ip') as run:
            with self.assertRaises(config.AuthorizationRequired):
                config.configure('can_qa', 500000)
            run.assert_not_called()

    def test_matching_link_needs_no_capability(self):
        with patch('can_config.find_ip', return_value='/bin/ip'), \
                patch('can_config.read_link', return_value=physical_link(True, 500000)), \
                patch('can_config.has_net_admin', return_value=False), \
                patch('can_config._run_ip') as run:
            self.assertIn('一致', config.configure('can_qa', 500000))
            run.assert_not_called()

    def test_partial_failure_is_explicit_and_does_not_continue_up(self):
        with patch('can_config.find_ip', return_value='/bin/ip'), \
                patch('can_config.read_link', return_value=physical_link(True, 125000)), \
                patch('can_config.has_net_admin', return_value=True), \
                patch('can_config._run_ip', side_effect=['', config.ConfigError('bad bitrate')]) as run:
            with self.assertRaisesRegex(config.ConfigError, '接口配置未完成'):
                config.configure('can_qa', 500000)
            self.assertEqual(run.call_count, 2)

    def test_system_command_has_fixed_argv_timeout_and_no_shell(self):
        with patch('can_config.subprocess.run', return_value=Mock(returncode=0, stdout='ok')) as run:
            self.assertEqual(config._run_ip('/bin/ip', ['link', 'show']), 'ok')
            self.assertEqual(run.call_args.args, (['/bin/ip', 'link', 'show'],))
            self.assertEqual(run.call_args.kwargs['timeout'], 5)
            self.assertNotIn('shell', run.call_args.kwargs)
        with patch('can_config.subprocess.run', side_effect=subprocess.TimeoutExpired('ip', 5)):
            with self.assertRaisesRegex(config.ConfigError, '超时'):
                config._run_ip('/bin/ip', [])

    def test_permission_error_is_distinct(self):
        result = Mock(returncode=2, stdout='', stderr='RTNETLINK answers: Operation not permitted')
        with patch('can_config.subprocess.run', return_value=result):
            with self.assertRaises(config.AuthorizationRequired):
                config._run_ip('/bin/ip', ['link', 'set'], mutation=True)

    def test_system_environment_removes_frozen_library_and_python_injection(self):
        with patch.dict('can_config.os.environ', {'LD_LIBRARY_PATH': '/bundle',
                         'LD_LIBRARY_PATH_ORIG': '/system', '_PYI_APPLICATION_HOME_DIR': '/bundle',
                         'PYTHONPATH': '/injected', 'PYTHONHOME': '/injected'}, clear=True):
            self.assertEqual(config.system_environment(), {'LD_LIBRARY_PATH': '/system'})

    def test_helper_exit_codes_are_stable(self):
        args = ['--interface', 'can_qa', '--bitrate', '500000']
        with patch.dict('can_config.os.environ'), patch('sys.stdout', new_callable=io.StringIO), \
                patch('can_config.configure', side_effect=config.AuthorizationRequired('authorize')):
            self.assertEqual(config.main(args), 77)
        with patch.dict('can_config.os.environ'), patch('sys.stdout', new_callable=io.StringIO), \
                patch('can_config.configure', side_effect=config.ConfigError('invalid')):
            self.assertEqual(config.main(args), 1)


class CanDisableTests(unittest.TestCase):
    def setUp(self):
        self.ip_patch = patch('can_config.find_ip', return_value='/bin/ip')
        self.ip_patch.start()
        self.addCleanup(self.ip_patch.stop)

    def test_down_interface_is_idempotent_and_needs_no_privilege(self):
        with patch('can_config.read_link', return_value=physical_link()), \
                patch('can_config.has_net_admin') as capability, \
                patch('can_config._run_ip') as run:
            self.assertIn('已处于 DOWN', config.disable('can_qa'))
            capability.assert_not_called()
            run.assert_not_called()

    def test_up_interface_needs_authorization_before_any_mutation(self):
        with patch('can_config.read_link', return_value=physical_link(True, 500000)), \
                patch('can_config.has_net_admin', return_value=False), \
                patch('can_config._run_ip') as run:
            with self.assertRaisesRegex(config.AuthorizationRequired, '停用'):
                config.disable('can_qa')
            run.assert_not_called()

    def test_success_mutates_only_selected_interface_then_checks_down(self):
        for kind in ('can', 'vcan', 'vxcan'):
            up, down = physical_link(True, 500000), physical_link(False, 500000)
            up['linkinfo']['info_kind'] = down['linkinfo']['info_kind'] = kind
            with self.subTest(kind=kind), \
                    patch('can_config.read_link', side_effect=[up, down]) as read, \
                    patch('can_config.has_net_admin', return_value=True), \
                    patch('can_config._run_ip') as run:
                self.assertIn('已停用（DOWN）', config.disable('can_qa'))
                self.assertEqual(read.call_count, 2)
                self.assertEqual(read.call_args_list[0].args, ('/bin/ip', 'can_qa'))
                self.assertEqual(read.call_args_list[1].args, ('/bin/ip', 'can_qa'))
                run.assert_called_once_with('/bin/ip', ['link', 'set', 'dev', 'can_qa', 'down'],
                                            mutation=True)

    def test_command_success_is_not_accepted_if_interface_is_still_up(self):
        with patch('can_config.read_link', return_value=physical_link(True, 500000)), \
                patch('can_config.has_net_admin', return_value=True), \
                patch('can_config._run_ip'):
            with self.assertRaisesRegex(config.ConfigError, '仍处于 UP'):
                config.disable('can_qa')

    def test_unreadable_state_after_down_is_not_reported_as_success(self):
        with patch('can_config.read_link', side_effect=[physical_link(True, 500000),
                                                       config.ConfigError('disconnected')]), \
                patch('can_config.has_net_admin', return_value=True), \
                patch('can_config._run_ip'):
            with self.assertRaisesRegex(config.ConfigError, '无法确认 DOWN'):
                config.disable('can_qa')

    def test_failed_down_command_is_reported_without_claiming_success(self):
        with patch('can_config.read_link', return_value=physical_link(True, 500000)) as read, \
                patch('can_config.has_net_admin', return_value=True), \
                patch('can_config._run_ip', side_effect=config.ConfigError('busy')):
            with self.assertRaisesRegex(config.ConfigError, '停用 can_qa 失败.*busy'):
                config.disable('can_qa')
            self.assertEqual(read.call_count, 1)

    def test_down_permission_race_remains_authorization_failure(self):
        with patch('can_config.read_link', return_value=physical_link(True, 500000)), \
                patch('can_config.has_net_admin', return_value=True), \
                patch('can_config._run_ip', side_effect=config.AuthorizationRequired('denied')):
            with self.assertRaisesRegex(config.AuthorizationRequired, '停用'):
                config.disable('can_qa')

    def test_non_can_and_invalid_interfaces_are_never_mutated(self):
        with patch('can_config._run_ip') as run:
            with self.assertRaises(config.ConfigError):
                config.disable('../eth0')
            with patch('can_config.Path.read_text', return_value='1'):
                with self.assertRaisesRegex(config.ConfigError, '不是 CAN'):
                    config.disable('eth0')
            run.assert_not_called()
        unsupported = physical_link()
        unsupported['linkinfo']['info_kind'] = 'other'
        with patch('can_config.read_link', return_value=unsupported), \
                patch('can_config._run_ip') as run:
            with self.assertRaisesRegex(config.ConfigError, '不是支持停用'):
                config.disable('can_qa')
            run.assert_not_called()

    def test_down_cli_needs_no_bitrate_and_has_stable_exit_codes(self):
        args = ['--interface', 'can_qa', '--down']
        with patch.dict('can_config.os.environ'), patch('sys.stdout', new_callable=io.StringIO), \
                patch('can_config.disable', return_value='DOWN') as disable, \
                patch('can_config.configure') as configure:
            self.assertEqual(config.main(args), 0)
            disable.assert_called_once_with('can_qa')
            configure.assert_not_called()
        for error, code in ((config.AuthorizationRequired('authorize'), 77),
                            (config.ConfigError('failed'), 1)):
            with self.subTest(code=code), patch.dict('can_config.os.environ'), \
                    patch('sys.stdout', new_callable=io.StringIO), \
                    patch('can_config.disable', side_effect=error):
                self.assertEqual(config.main(args), code)

    def test_down_cli_rejects_conflicting_configuration_flags(self):
        for extra in (['--bitrate', '500000'], ['--fd'], ['--data-bitrate', '2000000']):
            with self.subTest(extra=extra), patch('sys.stderr', new_callable=io.StringIO), \
                    self.assertRaises(SystemExit) as result:
                config.main(['--interface', 'can_qa', '--down', *extra])
            self.assertEqual(result.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
