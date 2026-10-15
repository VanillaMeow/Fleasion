# ruff: file-ignore[implicit-namespace-package]

from __future__ import annotations

import asyncio
import errno
from types import SimpleNamespace
from typing import TYPE_CHECKING, Never, cast

import pytest
from PyQt6.QtWidgets import QApplication, QCheckBox, QLineEdit, QPushButton, QRadioButton

from fleasion import linux_proxy_helper_daemon, macos_proxy_helper_daemon
from fleasion.gui import rando_stuff_tab
from fleasion.gui.subplace_joiner_tab import SubplaceJoinerTab
from fleasion.proxy import master as proxy_master, server as proxy_server
from fleasion.proxy.server import FleasionProxy
from fleasion.proxy.upstream import UpstreamConnectResult
from fleasion.utils.certs import generate_ca, generate_multi_host_cert

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from PyQt6.QtWidgets import QWidget


PROXY_PORT = 443


def _attribute(owner: object, name: str) -> object:
    return getattr(owner, name)


@pytest.fixture
def app() -> QApplication:
    application = QApplication.instance()
    return cast('QApplication', application) if application is not None else QApplication([])


def _make_proxy(tmp_path: Path, *, intercept_all: bool) -> FleasionProxy:
    ca_cert, ca_key = generate_ca(tmp_path)
    default_cert = generate_multi_host_cert(
        'default', {proxy_server.ASSET_DELIVERY_HOST}, ca_cert, ca_key, tmp_path
    )
    factory = cast('Callable[..., FleasionProxy]', FleasionProxy)
    return factory(
        texture_stripper=SimpleNamespace(),
        cache_scraper=SimpleNamespace(enabled=False),
        host_certs={},
        default_cert=default_cert,
        upstream_endpoints={},
        explicit_proxy=True,
        intercept_hosts={'gamejoin.roblox.com', proxy_server.ASSET_DELIVERY_HOST},
        intercept_all_hosts=intercept_all,
        ca_cert_path=ca_cert,
        ca_key_path=ca_key,
        cert_cache_dir=tmp_path,
        port=0,
    )


@pytest.mark.parametrize('intercept_all', [False, True])
def test_gamejoin_cannot_be_intercepted_or_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, intercept_all: bool
) -> None:
    proxy = _make_proxy(tmp_path, intercept_all=intercept_all)
    proxy.set_intercept_excluded_hosts(())
    should_intercept = cast(
        'Callable[[str, int], bool]', _attribute(proxy, '_should_intercept_explicit_host')
    )
    host_context = cast('Callable[[str], object]', _attribute(proxy, '_get_or_generate_host_ctx'))
    record = cast(
        'Callable[[str, int, str, str, bool], dict[str, object]]',
        _attribute(proxy, '_record_request'),
    )

    async def reject_network(_host: str) -> Never:
        await asyncio.sleep(0)
        pytest.fail('Gamejoin replay reached the upstream connector')

    monkeypatch.setattr(proxy, '_connect_upstream', reject_network)
    replay = cast(
        'Callable[[int, bytes, str], Awaitable[dict[str, object] | None]]',
        _attribute(proxy, 'replay_request'),
    )
    for host in ('gamejoin.roblox.com', 'GAMEJOIN.ROBLOX.COM.'):
        assert not should_intercept(host, 443)
        assert host_context(host) is None
        entry = record(host, 443, 'POST', '/v1/join-game', intercept_all)
        entry_id = entry['id']
        assert isinstance(entry_id, int)
        assert asyncio.run(replay(entry_id, b'POST /v1/join-game HTTP/1.1\r\n\r\n', host)) is None
    assert should_intercept(proxy_server.ASSET_DELIVERY_HOST, 443)
    asyncio.run(proxy.stop())


def _server_port(server: asyncio.Server) -> int:
    return cast('int', server.sockets[0].getsockname()[1])


def test_gamejoin_connect_preserves_opaque_bytes_with_intercept_all_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = _make_proxy(tmp_path, intercept_all=True)
    opaque = b'\x16\x03\x03opaque Roblox TLS payload'

    async def run_test() -> None:
        received: list[bytes] = []

        async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                payload = await reader.readexactly(len(opaque))
                received.append(payload)
                writer.write(payload)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        upstream = await asyncio.start_server(echo, '127.0.0.1', 0)

        async def connect(host: str, port: int) -> UpstreamConnectResult:
            assert host == 'gamejoin.roblox.com'
            assert port == PROXY_PORT
            reader, writer = await asyncio.open_connection('127.0.0.1', _server_port(upstream))
            return UpstreamConnectResult(
                reader=reader, writer=writer, method='direct', endpoint='test'
            )

        monkeypatch.setattr(proxy_server, '_open_explicit_proxy_tunnel', connect)
        await proxy.start()
        reader, writer = await asyncio.open_connection('127.0.0.1', proxy.port)
        try:
            writer.write(
                b'CONNECT gamejoin.roblox.com:443 HTTP/1.1\r\nHost: gamejoin.roblox.com:443\r\n\r\n'
            )
            await writer.drain()
            reply = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 2)
            assert reply.startswith(b'HTTP/1.1 200')
            writer.write(opaque)
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(len(opaque)), 2) == opaque
            assert received == [opaque]
            get_log = cast(
                'Callable[[], list[dict[str, object]]]', _attribute(proxy, 'get_request_log')
            )
            entries = get_log()
            assert len(entries) == 1
            assert entries[0]['intercepted'] is False
            assert not entries[0].get('request_raw')
        finally:
            writer.close()
            await writer.wait_closed()
            await proxy.stop()
            upstream.close()
            await upstream.wait_closed()

    asyncio.run(run_test())


@pytest.mark.parametrize('mode', ['full', 'a', 'b', 'c', 'd', 'e'])
def test_proxy_debug_modes_never_route_gamejoin(mode: str) -> None:
    proxy = object.__new__(proxy_master.ProxyMaster)
    vars(proxy)['config_manager'] = SimpleNamespace(
        settings={'_runtime_proxy_debug': True, '_runtime_proxy_debug_mode': mode}
    )
    desired = cast('Callable[[], set[str]]', _attribute(proxy, '_desired_intercept_hosts'))
    assert 'gamejoin.roblox.com' not in desired()


def test_hosts_cleanup_removes_unmarked_legacy_gamejoin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hosts_file = tmp_path / 'hosts'
    hosts_file.write_text('127.0.0.1 localhost\n::1 gamejoin.roblox.com\n', encoding='utf-8')
    monkeypatch.setattr(proxy_master, 'HOSTS_FILE', hosts_file)
    assert proxy_master.has_stale_hosts_entries(set(proxy_server.INTERCEPT_HOSTS))
    cleanup = cast('Callable[[set[str]], bool]', _attribute(proxy_master, '_remove_hosts_entries'))
    assert cleanup(set(proxy_server.INTERCEPT_HOSTS))
    assert hosts_file.read_text(encoding='utf-8') == '127.0.0.1 localhost\n'


def test_privileged_helpers_reject_gamejoin_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hosts_file = tmp_path / 'hosts'
    hosts_file.write_text(
        '127.0.0.1 localhost\n127.0.0.1 gamejoin.roblox.com\n::1 gamejoin.roblox.com\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(macos_proxy_helper_daemon, 'HOSTS_FILE', str(hosts_file))
    set_hosts = cast(
        'Callable[[set[str]], None]', _attribute(macos_proxy_helper_daemon, '_set_hosts')
    )
    set_hosts({'assetdelivery.roblox.com'})
    assert 'gamejoin.roblox.com' not in hosts_file.read_text(encoding='utf-8')
    with pytest.raises(ValueError, match='allowlist'):
        set_hosts({'gamejoin.roblox.com'})
    validate = cast(
        'Callable[[set[str]], set[str]]', _attribute(linux_proxy_helper_daemon, '_validate_hosts')
    )
    with pytest.raises(RuntimeError, match=r'unsupported hosts requested: gamejoin\.roblox\.com'):
        validate({'gamejoin.roblox.com'})


def test_read_only_hosts_cannot_keep_a_legacy_gamejoin_redirect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hosts_file = tmp_path / 'hosts'
    hosts_file.write_text(
        '127.0.0.1 assetdelivery.roblox.com\n::1 gamejoin.roblox.com\n', encoding='utf-8'
    )

    def fail_apply(_hosts: set[str]) -> Never:
        message = 'Read-only file system'
        raise OSError(errno.EROFS, message, str(hosts_file))

    def no_op() -> None:
        pass

    monkeypatch.setattr(linux_proxy_helper_daemon, 'HOSTS_FILE', hosts_file)
    monkeypatch.setattr(linux_proxy_helper_daemon, '_clear_hosts', no_op)
    monkeypatch.setattr(linux_proxy_helper_daemon, '_install_boot_guard', lambda: False)
    monkeypatch.setattr(linux_proxy_helper_daemon, '_apply_hosts', fail_apply)
    apply = cast(
        'Callable[[set[str]], bool]',
        _attribute(linux_proxy_helper_daemon, '_apply_hosts_or_use_existing_read_only'),
    )
    with pytest.raises(OSError, match='Read-only file system'):
        apply({'assetdelivery.roblox.com'})


def test_gamejoin_controls_stay_disabled_when_proxy_setting_changes(
    app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    def skip_resolution(_tab: rando_stuff_tab.RandoStuffTab) -> None:
        pass

    monkeypatch.setattr(rando_stuff_tab, '_load_accounts', list)
    monkeypatch.setattr(rando_stuff_tab.RandoStuffTab, '_check_cookies_on_boot', skip_resolution)
    monkeypatch.setattr(rando_stuff_tab.RandoStuffTab, '_resolve_current_user', skip_resolution)
    factory = cast('Callable[[], rando_stuff_tab.RandoStuffTab]', rando_stuff_tab.RandoStuffTab)
    tab = factory()
    try:
        toggle = cast('Callable[[bool], None]', _attribute(tab, 'set_proxy_features_enabled'))
        for enabled in (True, False, True):
            toggle(enabled)
            for name in ('_rejoin_section', '_subplace_blacklist_section'):
                section = _attribute(tab, name)
                content = cast('QWidget', _attribute(section, '_content'))
                controls = (
                    content.findChildren(QLineEdit)
                    + content.findChildren(QPushButton)
                    + content.findChildren(QRadioButton)
                )
                assert controls
                assert all(not control.isEnabled() for control in controls)
            subplace = cast('QLineEdit', _attribute(tab, '_subplace_id_input'))
            creator = cast('QCheckBox', _attribute(tab, '_username_self_game_creator_chk'))
            assert not subplace.isEnabled()
            assert not creator.isEnabled()
            assert not creator.isChecked()
        assert cast('QLineEdit', _attribute(tab, '_private_server_input')).isEnabled()
    finally:
        tab.close()
        tab.deleteLater()
        app.processEvents()


def test_subplace_root_join_does_not_open_a_network_session() -> None:
    join_root = cast('Callable[[int, str], bool]', _attribute(SubplaceJoinerTab, '_join_root'))
    assert not join_root(12345, 'cookie')
