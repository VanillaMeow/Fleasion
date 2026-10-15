import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path


def _noop_log(*_args: object, **_kwargs: object) -> None:
    return None


def _no_cookie() -> None:
    return None


def _load_username_spoofer():
    root = Path(__file__).resolve().parents[1]
    module_name = 'fleasion.proxy.addons.username_spoofer'
    stubbed_names = (
        'fleasion',
        'fleasion.proxy',
        'fleasion.proxy.addons',
        'fleasion.utils',
        'fleasion.utils.roblox_auth',
        module_name,
    )
    previous_modules = {name: sys.modules.get(name) for name in stubbed_names}

    try:
        for package_name in ('fleasion', 'fleasion.proxy', 'fleasion.proxy.addons'):
            package = sys.modules.setdefault(package_name, types.ModuleType(package_name))
            package.__path__ = []

        utils = types.ModuleType('fleasion.utils')
        utils.__path__ = [str(root / 'src' / 'fleasion' / 'utils')]
        setattr(utils, 'log_buffer', types.SimpleNamespace(log=_noop_log))
        sys.modules['fleasion.utils'] = utils

        roblox_auth = types.ModuleType('fleasion.utils.roblox_auth')
        setattr(roblox_auth, 'get_roblosecurity', _no_cookie)
        sys.modules['fleasion.utils.roblox_auth'] = roblox_auth

        spec = importlib.util.spec_from_file_location(
            module_name,
            root / 'src' / 'fleasion' / 'proxy' / 'addons' / 'username_spoofer.py',
        )
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module.UsernameSpoofer
    finally:
        for name, module in previous_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


UsernameSpoofer = _load_username_spoofer()


class _Request:
    def __init__(self, url: str) -> None:
        self.pretty_url = url


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.content = json.dumps(payload).encode('utf-8')


class _Flow:
    def __init__(self, url: str, payload: dict[str, object]) -> None:
        self.request = _Request(url)
        self.response = _Response(payload)


class UsernameSpooferTests(unittest.TestCase):
    def test_gamejoin_responses_are_unchanged_with_saved_creator_spoof_enabled(self) -> None:
        spoofer = UsernameSpoofer()
        spoofer.set_runtime_state({'save_settings': True, 'self_game_creator': True})
        for endpoint in (
            'join-game',
            'join-game-instance',
            'join-reserved-game',
            'join-private-game',
        ):
            with self.subTest(endpoint=endpoint):
                flow = _Flow(
                    f'https://gamejoin.roblox.com/v1/{endpoint}',
                    {'joinScript': {'CreatorId': 123, 'CreatorType': 'Group'}},
                )
                original = flow.response.content
                spoofer.request(flow)
                spoofer.response(flow)
                self.assertEqual(flow.response.content, original)

    def test_game_creator_spoof_does_not_enable_interception(self) -> None:
        spoofer = UsernameSpoofer()
        spoofer.set_runtime_state({'self_game_creator': True})
        self.assertFalse(spoofer.is_enabled())

    def test_profile_spoof_still_works_with_saved_creator_spoof(self) -> None:
        spoofer = UsernameSpoofer()
        spoofer.set_runtime_state(
            {'self_game_creator': True, 'others_apply_ingame': True, 'others_name': 'Example'}
        )
        flow = _Flow(
            'https://apis.roblox.com/v1/user/profiles/get-profiles',
            {'profileDetails': [{'userId': 123, 'names': {'username': 'Original'}}]},
        )
        spoofer.response(flow)
        payload = json.loads(flow.response.content)
        self.assertEqual(payload['profileDetails'][0]['names']['username'], 'Example')
        self.assertTrue(spoofer.is_enabled())


if __name__ == '__main__':
    unittest.main()
