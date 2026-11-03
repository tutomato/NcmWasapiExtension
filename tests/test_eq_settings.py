import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'python'))
import eq_settings as eq


class EqSettingsTests(unittest.TestCase):
    def test_default_gain_is_zero(self):
        c=eq.defaults()
        self.assertEqual(c['preamp'],0)
        self.assertTrue(all(b['gain']==0 for b in c['bands']))
        self.assertTrue(all(b['q']>0 and b['frequency']>0 for b in c['bands']))

    def test_persistence_and_corrupt_recovery(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'nested' / 'eq.json'
            c = eq.defaults()
            c['enabled'] = True
            c['bands'][0].update(frequency=73, gain=-4, q=3)
            eq.save(c, p)
            self.assertEqual(eq.load(p), c)
            p.write_text('{bad', encoding='utf-8')
            self.assertEqual(eq.load(p), eq.defaults())
            self.assertEqual(list(p.parent.iterdir()), [p])

    def test_rejects_nonfinite_boolean_and_ranges(self):
        for key, value in [('preamp', 1), ('preamp', float('nan')), ('preamp', True), ('enabled', 1)]:
            c = eq.defaults(); c[key] = value
            with self.assertRaises(ValueError): eq.validate(c)
        for key, value in [('q', 0), ('q', 21), ('gain', 19), ('frequency', 0), ('frequency', float('inf')), ('enabled', 1), ('type', 'vst')]:
            c = eq.defaults(); c['bands'][0][key] = value
            with self.assertRaises(ValueError): eq.validate(c)
        c = eq.defaults(); c['bands'] *= 2
        with self.assertRaises(ValueError): eq.validate(c)

    def test_invalid_save_preserves_existing(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'eq.json'; eq.save(eq.defaults(),p)
            before=p.read_bytes()
            c=eq.defaults(); c['preamp']=10
            with self.assertRaises(ValueError): eq.save(c,p)
            self.assertEqual(p.read_bytes(),before)


if __name__ == '__main__': unittest.main()
