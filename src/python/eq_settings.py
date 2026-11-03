"""Validated per-user EQ settings. Saving uses an atomic replace."""
import json
import math
import os
from pathlib import Path
import tempfile


def defaults():
    return {'enabled': False, 'preamp': 0.0, 'bands': [
        {'enabled': True, 'type': 'bell', 'frequency': f, 'gain': 0.0, 'q': 1.0}
        for f in (31, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)]}


def validate(value):
    def number(n, lo, hi):
        if isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) or not lo <= n <= hi:
            raise ValueError('EQ 参数超出范围')
        return float(n)
    if not isinstance(value, dict) or type(value.get('enabled')) is not bool:
        raise ValueError('EQ 设置无效')
    bands = value.get('bands')
    if not isinstance(bands, list) or len(bands) > 10:
        raise ValueError('最多支持 10 段 EQ')
    result = {'enabled': value['enabled'], 'preamp': number(value.get('preamp'), -30, 0), 'bands': []}
    for b in bands:
        if not isinstance(b, dict) or type(b.get('enabled')) is not bool or b.get('type') not in ('bell', 'low_shelf', 'high_shelf'):
            raise ValueError('EQ 频段无效')
        result['bands'].append({'enabled': b['enabled'], 'type': b['type'],
            'frequency': number(b.get('frequency'), 20, 20000),
            'gain': number(b.get('gain'), -18, 18), 'q': number(b.get('q'), .2, 20)})
    return result


def validate_request(value):
    """Control commands never carry draft filter parameters."""
    if not isinstance(value, dict):
        raise ValueError('EQ 请求无效')
    if 'action' not in value:
        return validate(value)
    if value.get('action') == 'enable' and set(value) == {'action', 'enabled'} and type(value['enabled']) is bool:
        return {'action': 'enable', 'enabled': value['enabled']}
    if value.get('action') == 'reset' and set(value) == {'action'}:
        return {'action': 'reset'}
    raise ValueError('EQ 控制请求无效')


def resolve_update(current, request):
    current = validate(current)
    request = validate_request(request)
    if request.get('action') == 'enable':
        current['enabled'] = request['enabled']
        return current
    if request.get('action') == 'reset':
        result = defaults()
        result['enabled'] = current['enabled']
        return result
    # Applying parameters must never toggle the effect as a side effect.
    request['enabled'] = current['enabled']
    return request


def settings_path():
    override = os.environ.get('NCM_WASAPI_DATA_DIR')
    base = Path(override) if override else Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local')) / 'NcmWasapiExtension'
    return base / 'equalizer.json'


def load(path=None):
    try:
        p = Path(path) if path else settings_path()
        if p.stat().st_size > 32768:
            return defaults()
        return validate(json.loads(p.read_text(encoding='utf-8')))
    except (OSError, ValueError, TypeError):
        return defaults()


def save(value, path=None):
    value = validate(value)
    p = Path(path) if path else settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=p.parent, delete=False) as f:
            temp = Path(f.name)
            json.dump(value, f, ensure_ascii=False, allow_nan=False)
        os.replace(temp, p)
    finally:
        if temp and temp.exists():
            temp.unlink()
    return value
