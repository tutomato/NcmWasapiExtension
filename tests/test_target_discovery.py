"""Pure target-selection regressions; no socket, renderer, or player operations."""
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest import mock

source=Path(__file__).resolve().parents[1]/'src'/'python'/'cdp_bridge.py'
if not source.is_file(): source=Path(__file__).with_name('cdp_bridge.py')
spec=importlib.util.spec_from_file_location('adaptive_cdp_bridge',source)
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

def page(identifier, url, websocket=None):
    return {'id':identifier,'type':'page','url':url,
        'webSocketDebuggerUrl':websocket or 'ws://127.0.0.1:48763/devtools/page/'+identifier}

class TargetTests(unittest.TestCase):
    def select(self,pages):
        with mock.patch.object(m.urllib.request,'urlopen',return_value=io.StringIO(json.dumps(pages))):
            return m._target(48763)

    def test_original_main_path_has_priority(self):
        expected=page('main','orpheus://orpheus/pub/app.html')
        self.assertEqual(self.select([page('other','orpheus://orpheus/settings'),expected]),expected)

    def test_sole_changed_path_is_eligible_for_contract_probe(self):
        expected=page('main','orpheus://new-layout/main-v2.html')
        self.assertEqual(self.select([page('blank','about:blank'),
            page('devtools','devtools://devtools/bundled/inspector.html'),expected]),expected)

    def test_multiple_changed_pages_are_not_guessed(self):
        with self.assertRaisesRegex(m.CdpError,'ambiguous'):
            self.select([page('main','orpheus://new-layout/main-v2.html'),
                page('other','orpheus://new-layout/settings.html')])

    def test_multiple_preferred_pages_are_not_guessed(self):
        with self.assertRaisesRegex(m.CdpError,'ambiguous'):
            self.select([page('one','orpheus://orpheus/pub/app.html'),
                page('two','orpheus://orpheus/pub/app.html')])

    def test_other_port_or_remote_endpoint_is_rejected(self):
        for endpoint in ('ws://127.0.0.1:9222/page/a','ws://example.com:48763/page/a',
                         'wss://127.0.0.1:48763/page/a','ws://user@127.0.0.1:48763/page/a'):
            with self.subTest(endpoint=endpoint),self.assertRaisesRegex(m.CdpError,'Unexpected'):
                self.select([page('main','orpheus://new-layout/main.html',endpoint)])

    def test_blank_or_nonpage_targets_return_none(self):
        self.assertIsNone(self.select([page('blank','about:blank'),
            {'type':'worker','url':'orpheus://worker','id':'w'}]))

if __name__=='__main__': unittest.main()
