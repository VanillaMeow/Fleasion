from __future__ import annotations

import json
import unittest
from typing import TYPE_CHECKING, cast

from fleasion.proxy.addons.username_spoofer import UsernameSpoofer

if TYPE_CHECKING:
    from fleasion.proxy.server import ProxyFlow


class _Request:
    def __init__(self, url: str):
        self.pretty_url = url


class _Response:
    def __init__(self, payload: dict):
        self.content = json.dumps(payload).encode('utf-8')


class _Flow:
    def __init__(self, url: str, payload: dict):
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
                spoofer.request(cast('ProxyFlow', flow))
                spoofer.response(cast('ProxyFlow', flow))
                self.assertEqual(flow.response.content, original)

    @staticmethod
    def test_game_creator_spoof_does_not_enable_interception() -> None:
        spoofer = UsernameSpoofer()
        spoofer.set_runtime_state({'self_game_creator': True})
        assert not spoofer.is_enabled()

    def test_profile_spoof_still_works_with_saved_creator_spoof(self) -> None:
        spoofer = UsernameSpoofer()
        spoofer.set_runtime_state(
            {'self_game_creator': True, 'others_apply_ingame': True, 'others_name': 'Example'}
        )
        flow = _Flow(
            'https://apis.roblox.com/v1/user/profiles/get-profiles',
            {'profileDetails': [{'userId': 123, 'names': {'username': 'Original'}}]},
        )
        spoofer.response(cast('ProxyFlow', flow))
        payload = json.loads(flow.response.content)
        self.assertEqual(payload['profileDetails'][0]['names']['username'], 'Example')
        self.assertTrue(spoofer.is_enabled())


if __name__ == '__main__':
    unittest.main()
