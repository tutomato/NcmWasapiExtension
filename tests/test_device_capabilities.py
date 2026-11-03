"""Catalog cache/error tests using fake endpoints; no COM or audio operations."""
import copy
import pathlib
import sys
import unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'src' / 'python'))
from device_capabilities import DeviceCatalog, make_format, select_format


class FakeBackend:
    def __init__(self):
        self.devices = [{'deviceId': 'A', 'name': 'DAC', 'deviceFormat': 'mask1'}]
        self.default = 'A'
        self.enumerations = 0
        self.probes = []
        self.answer = self.supported()

    @staticmethod
    def supported():
        return {'formats': [make_format(44100, 2, 's24in32')[1]],
                'exclusiveSupported': True, 'probeState': 'complete',
                'formatMask': ['s24in32'], 'probeErrors': []}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def enumerate(self):
        self.enumerations += 1
        return copy.deepcopy(self.devices), self.default, []

    def probe(self, device_id, rates, channels):
        self.probes.append(device_id)
        return copy.deepcopy(self.answer)


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.now = 0
        self.catalog = DeviceCatalog(backend_factory=lambda: self.backend,
                                     clock=lambda: self.now)

    def test_poll_and_capability_cache_and_defensive_copy(self):
        first = self.catalog.refresh()
        first['devices'][0]['formats'].clear()
        self.now = 1
        self.assertEqual(len(self.catalog.refresh()['devices'][0]['formats']), 1)
        self.assertEqual(self.backend.enumerations, 1)
        self.now = 3
        second = self.catalog.refresh()
        self.assertEqual(self.backend.enumerations, 2)
        self.assertEqual(self.backend.probes, ['A'])
        self.assertEqual(second['generation'], first['generation'])
        self.now = 61
        self.catalog.refresh()
        self.assertEqual(self.backend.probes, ['A', 'A'])

    def test_default_switch_and_hot_replug(self):
        one = self.catalog.refresh()
        self.backend.default = None
        self.now = 2
        two = self.catalog.refresh()
        self.assertGreater(two['generation'], one['generation'])
        self.assertIsNone(two['defaultDeviceId'])
        self.backend.devices = []
        self.now = 4
        self.assertEqual(self.catalog.refresh()['devices'], [])
        self.backend.devices = [{'deviceId': 'A', 'name': 'DAC', 'deviceFormat': 'mask1'}]
        self.now = 6
        self.catalog.refresh()
        self.assertEqual(self.backend.probes, ['A', 'A'])

    def test_metadata_change_invalidates_capability_cache(self):
        self.catalog.refresh()
        self.backend.devices[0]['deviceFormat'] = 'mask2'
        self.now = 2
        self.catalog.refresh()
        self.assertEqual(self.backend.probes, ['A', 'A'])

    def test_busy_retains_prior_support_but_new_endpoint_stays_unknown(self):
        self.catalog.refresh()
        self.backend.answer = {'formats': [], 'exclusiveSupported': None,
            'probeState': 'error', 'formatMask': [],
            'probeErrors': [{'hr': '0x8889000a', 'operation': 'IsFormatSupported'}]}
        self.now = 61
        busy = self.catalog.refresh()['devices'][0]
        self.assertTrue(busy['exclusiveSupported'])
        self.assertEqual(busy['probeState'], 'busy')
        self.assertTrue(busy['stale'])
        self.assertEqual(len(busy['formats']), 1)
        self.backend.devices[0]['deviceId'] = 'B'
        self.now = 63
        unknown = self.catalog.refresh()['devices'][0]
        self.assertIsNone(unknown['exclusiveSupported'])
        self.assertFalse(unknown.get('stale', False))

    def test_confirmed_unsupported_replaces_old_support(self):
        self.catalog.refresh()
        self.backend.answer = {'formats': [], 'exclusiveSupported': False,
            'probeState': 'complete', 'formatMask': [], 'probeErrors': []}
        self.now = 61
        self.assertIs(self.catalog.refresh()['devices'][0]['exclusiveSupported'], False)

    def test_native_rate_selection_and_packed_format(self):
        device = {'formats': [make_format(44100, 2, 's16')[1],
                             make_format(44100, 2, 's24', False)[1]]}
        chosen = select_format(device, 44100)
        self.assertEqual(chosen['kind'], 's24')
        self.assertEqual(chosen['blockAlign'], 6)
        self.assertTrue(chosen['legacy'])
        self.assertIsNone(select_format(device, 48000))
        self.assertIsNone(select_format(device, 44100, 1))


if __name__ == '__main__':
    unittest.main()
