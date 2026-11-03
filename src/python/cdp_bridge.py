"""Keep the NCM output choices installed and acknowledge mode before reopening audio.

Controller integration::

    bridge = start(
        lambda mode, device: script.exports_sync.command({
            'action': 'select-mode', 'value': mode,
            'deviceId': device['deviceId'], 'deviceName': device['name'],
            'id': device['id']}),
        get_catalog=lambda: catalog.snapshot(),
        set_volume=lambda value: script.exports_sync.command(
            {'action': 'gain', 'value': value}))
    # bridge.status(), bridge.refreshDevices(), bridge.wait_ready(timeout)
    bridge.stop()  # before tearing down the player/native adapter

The callback is serialized on a worker thread. It must finish only after the
native adapter has accepted the mode; raise an exception or return False on
failure. The JavaScript setter waits for that acknowledgement. No HTTP command
server is opened. CDP Runtime.addBinding carries requests and replies.

Put output-option-inject.js and install_output_options.js next to this file.
Only these colocated generic-device sources are loaded.
Requires websocket-client, which is also used by cdp_eval.py.
"""
from __future__ import annotations

import json
import math
import pathlib
import queue
import secrets
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Callable

import websocket


ROOT = pathlib.Path(__file__).resolve().parent
DEFAULT_PORT = 48763
BINDING_NAME = '__codexNcmOutputMode'
BRIDGE_GLOBAL = '__codexOutputCdpBridge'


class CdpError(RuntimeError):
    pass


class _Pending:
    def __init__(self):
        self.done = threading.Event()
        self.result: dict[str, Any] | None = None
        self.error: Exception | None = None


class _Connection:
    """One CDP connection; receive independently of calls and binding callbacks."""

    def __init__(self, url: str, on_event: Callable):
        self.ws = websocket.create_connection(
            url, timeout=3, suppress_origin=True, enable_multithread=True)
        self.ws.settimeout(0.5)
        self.closed = threading.Event()
        self._send_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, _Pending] = {}
        self._next_id = 0
        self._on_event = on_event
        self._thread = threading.Thread(
            target=self._receive, name='ncm-cdp-receive', daemon=True)
        self._thread.start()

    def _receive(self):
        error: Exception = CdpError('NCM debugging connection closed')
        try:
            while not self.closed.is_set():
                try:
                    raw = self.ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not raw:
                    break
                message = json.loads(raw)
                if 'id' in message:
                    with self._pending_lock:
                        pending = self._pending.get(message['id'])
                    if pending:
                        pending.result = message
                        pending.done.set()
                elif message.get('method'):
                    self._on_event(self, message)
        except Exception as exc:
            error = CdpError(str(exc) or 'NCM debugging connection closed')
        finally:
            self.closed.set()
            with self._pending_lock:
                for pending in self._pending.values():
                    if not pending.done.is_set():
                        pending.error = error
                        pending.done.set()

    def call(self, method: str, params: dict | None = None, timeout: float = 8):
        if self.closed.is_set():
            raise CdpError('NCM debugging connection is not available')
        pending = _Pending()
        with self._pending_lock:
            self._next_id += 1
            request_id = self._next_id
            self._pending[request_id] = pending
        try:
            with self._send_lock:
                self.ws.send(json.dumps({
                    'id': request_id, 'method': method, 'params': params or {}}))
            if not pending.done.wait(timeout):
                raise CdpError(f'{method} timed out')
            if pending.error:
                raise pending.error
            response = pending.result or {}
            if 'error' in response:
                raise CdpError(str(response['error']))
            return response.get('result', {})
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def evaluate(self, expression: str, timeout: float = 8, context_id=None):
        params = {'expression': expression, 'awaitPromise': True,
                  'returnByValue': True}
        if context_id is not None:
            params['contextId'] = context_id
        result = self.call('Runtime.evaluate', params, timeout)
        if 'exceptionDetails' in result:
            detail = result['exceptionDetails']
            description = detail.get('exception', {}).get('description')
            raise CdpError(description or detail.get('text') or str(detail))
        value = result.get('result', {})
        return value.get('value')

    def close(self):
        self.closed.set()
        try:
            self.ws.close(timeout=0.5)
        except Exception:
            pass
        if threading.current_thread() is not self._thread:
            self._thread.join(1)


def _source_path(name: str) -> pathlib.Path:
    return ROOT / name


def _target(port: int) -> dict | None:
    with urllib.request.urlopen(
            f'http://127.0.0.1:{port}/json/list', timeout=2) as response:
        pages = json.load(response)
    candidates = []
    if not isinstance(pages, list):
        raise CdpError('Invalid NCM debugging target list')
    for page in pages:
        if not isinstance(page, dict) or page.get('type') != 'page':
            continue
        url = page.get('url')
        if (not isinstance(url, str) or not url.strip() or
                urllib.parse.urlparse(url).scheme.casefold() in (
                    'about', 'devtools', 'chrome-devtools')):
            continue
        try:
            endpoint = urllib.parse.urlparse(page.get('webSocketDebuggerUrl', ''))
            if (endpoint.scheme != 'ws' or
                    endpoint.hostname not in ('127.0.0.1', 'localhost', '::1') or
                    endpoint.port != port or endpoint.username or endpoint.password):
                raise ValueError('Endpoint is not this controller\'s loopback debug port')
        except (ValueError, TypeError, AttributeError) as error:
            raise CdpError('Unexpected NCM debugging endpoint') from error
        candidates.append(page)
    preferred = [page for page in candidates if 'orpheus/pub/app.html' in page['url']]
    if len(preferred) == 1:
        return preferred[0]
    if len(preferred) > 1 or len(candidates) > 1:
        raise CdpError('NCM app page is ambiguous; output options were not installed')
    # The port belongs to the NCM process started by this controller. A renamed
    # main page is eligible only when it is the sole page target; installation
    # still verifies its native/store/DTO contracts before patching any methods.
    return candidates[0] if candidates else None


def _install_expression(injection: str, installation: str,
                        token: str, owner: str, ack_timeout: float,
                        volume_enabled: bool = False, eq_source: str = '') -> str:
    # Both script files are trusted local bundle sources. They are embedded as
    # JavaScript, not evaluated as strings, so no browser eval/CSP dependency.
    injection = injection.rstrip().removesuffix(';')
    template = r'''(async function () {
  const token = __TOKEN__;
  const owner = __OWNER__;
  const globalName = __GLOBAL__;
  const bindingName = __BINDING__;
  const previous = window[globalName];
  if (previous && previous.dispose) previous.dispose('Output bridge replaced');
  if (window.__codexOutputOptions) {
    window.__codexOutputOptions.uninstall();
    delete window.__codexOutputOptions;
  }
  const pending = new Map();
  let sequence = 0;
  const transport = {
    owner, installed: false, restore: null, lastMode: null, lastDevice: null, volumeError: null,
    restoreVolume: null, disposeEqualizer: null,
    request(kind, value) {
      if (kind === 'mode' && (!value ||
          (value.mode !== 'shared' && value.mode !== 'exclusive') ||
          !value.device || typeof value.device !== 'object'))
        return Promise.reject(new Error('Invalid output mode'));
      if (kind === 'volume' && (typeof value !== 'number' || !Number.isFinite(value)))
        return Promise.reject(new Error('Invalid output volume'));
      if (kind === 'catalog' && !Array.isArray(value))
        return Promise.reject(new Error('Invalid output device list'));
      if (!['mode', 'volume', 'catalog', 'eq'].includes(kind))
        return Promise.reject(new Error('Invalid output request'));
      if (window[globalName] !== transport)
        return Promise.reject(new Error('Output bridge was replaced'));
      return new Promise((resolve, reject) => {
        const id = String(++sequence);
        const timer = setTimeout(() => {
          pending.delete(id);
          reject(new Error('Output mode acknowledgement timed out'));
        }, __ACK_TIMEOUT__);
        pending.set(id, {resolve, reject, timer, kind, value});
        try {
          window[bindingName](JSON.stringify({token, owner, id, kind, value}));
        } catch (error) {
          clearTimeout(timer); pending.delete(id); reject(error);
        }
      });
    },
    settle(id, ok, message) {
      const request = pending.get(id);
      if (!request) return false;
      pending.delete(id); clearTimeout(request.timer);
      if (ok) {
        if (request.kind === 'mode') {
          transport.lastMode = request.value.mode;
          transport.lastDevice = request.value.device;
        }
        else if (request.kind === 'volume') transport.volumeError = null;
        request.resolve(message);
      }
      else request.reject(new Error(message || 'Output mode was rejected'));
      return true;
    },
    dispose(message) {
      transport.installed = false;
      if (transport.restoreVolume) transport.restoreVolume();
      if (transport.disposeEqualizer) transport.disposeEqualizer();
      for (const request of pending.values()) {
        clearTimeout(request.timer);
        request.reject(new Error(message || 'Output bridge stopped'));
      }
      pending.clear();
    },
    snapshot() {
      let volume = null;
      try {
        const api = window.__codexOutputOptions;
        const value = api && typeof api.readVolume === 'function' ? api.readVolume() : null;
        if (typeof value === 'number' && Number.isFinite(value)) volume = value;
      } catch (_) {}
      return {owner, installed: transport.installed,
        lastMode: transport.lastMode, lastDevice: transport.lastDevice,
        restore: transport.restore, volume,
        volumeError: transport.volumeError};
    }
  };
  window[globalName] = transport;
  try {
    await (__INJECTION__);
    if (window[globalName] !== transport) throw new Error('Installation superseded');
    const api = window.__codexOutputOptions;
    if (__VOLUME_ENABLED__) {
      if (typeof api.requireVolumeSupport !== 'function')
        throw new Error('Output option bundle lacks adaptive volume detection');
      api.requireVolumeSupport();
    }
    const originalInstall = api.install;
    api.install = function (config) {
      return originalInstall.call(api, Object.assign({}, config, {
        getCapabilities: devices => transport.request('catalog', devices),
        selectMode: async (mode, device) => {
          // A final slider read belongs to this exact reopen, even if a drag
          // event and mode click reach the controller on different ticks.
          if (__VOLUME_ENABLED__) {
            const volume = transport.snapshot().volume;
            if (volume !== null)
              await transport.request('volume', Math.max(0, Math.min(1, volume)));
          }
          return transport.request('mode', {mode, device});
        }
      }));
    };
    try { __INSTALLATION__ }
    finally { api.install = originalInstall; }
    if (typeof api.restoreMode !== 'function')
      throw new Error('Output option bundle lacks restoreMode');
    if (__VOLUME_ENABLED__) {
      const audio = api.native.AudioPlayer;
      const originalSetVolume = audio.setVolume;
      if (typeof originalSetVolume !== 'function')
        throw new Error('Output option bundle lacks native volume setter');
      const wrappedSetVolume = function (value) {
        const volume = Number(value);
        if (Number.isFinite(volume)) {
          transport.request('volume', Math.max(0, Math.min(1, volume)))
            .catch(error => { transport.volumeError = String(error.message || error); });
        }
        return originalSetVolume.apply(this, arguments);
      };
      audio.setVolume = wrappedSetVolume;
      transport.restoreVolume = function () {
        if (audio.setVolume === wrappedSetVolume) audio.setVolume = originalSetVolume;
      };
      const initialVolume = transport.snapshot().volume;
      if (initialVolume !== null)
        await transport.request('volume', Math.max(0, Math.min(1, initialVolume)));
    }
    transport.restore = await api.restoreMode();
    if (window[globalName] !== transport) throw new Error('Installation superseded');
    transport.installed = true;
    __EQ_UI__
    return transport.snapshot();
  } catch (error) {
    transport.dispose('Output bridge installation failed');
    if (window[globalName] === transport) {
      if (window.__codexOutputOptions) window.__codexOutputOptions.uninstall();
      delete window[globalName];
    }
    throw error;
  }
})()'''
    substitutions = {
        '__TOKEN__': json.dumps(token), '__OWNER__': json.dumps(owner),
        '__GLOBAL__': json.dumps(BRIDGE_GLOBAL),
        '__BINDING__': json.dumps(BINDING_NAME),
        '__ACK_TIMEOUT__': str(round(ack_timeout * 1000)),
        '__VOLUME_ENABLED__': json.dumps(volume_enabled),
        '__INJECTION__': injection, '__INSTALLATION__': installation, '__EQ_UI__': eq_source}
    for key, value in substitutions.items():
        template = template.replace(key, value)
    return template


def _device_dto(value):
    """Validate the transport shape without assuming a fixed device index."""
    if not isinstance(value, dict):
        return None
    result = {}
    for key, limit in (('deviceId', 32768), ('name', 2048), ('type', 64)):
        field = value.get(key, '')
        if not isinstance(field, str) or len(field) > limit or '\0' in field:
            return None
        result[key] = field
    identifier = value.get('id')
    if isinstance(identifier, bool):
        return None
    if isinstance(identifier, int):
        if abs(identifier) > 2 ** 53 - 1:
            return None
    elif isinstance(identifier, str):
        if len(identifier) > 2048 or '\0' in identifier:
            return None
    else:
        return None
    result['id'] = identifier
    return result


class OutputOptionsBridge:
    def __init__(self, select_mode: Callable[[str, dict], Any], *,
                 get_catalog: Callable[[], dict],
                 set_volume: Callable[[float], Any] | None = None,
                 set_equalizer: Callable | None = None,
                 port: int = DEFAULT_PORT, injection_path=None,
                 installation_path=None, poll_interval: float = 1,
                 ack_timeout: float = 10, on_status: Callable | None = None):
        if not callable(select_mode):
            raise TypeError('select_mode must be callable')
        if not callable(get_catalog):
            raise TypeError('get_catalog must be callable')
        if set_volume is not None and not callable(set_volume):
            raise TypeError('set_volume must be callable or None')
        if not 1 <= int(port) <= 65535:
            raise ValueError('Invalid debug port')
        self.select_mode = select_mode
        self.get_catalog = get_catalog
        self.set_volume = set_volume
        self.set_equalizer = set_equalizer
        self._eq_clients = {}
        self.port = int(port)
        self.injection_path = pathlib.Path(injection_path) if injection_path else \
            _source_path('output-option-inject.js')
        self.installation_path = pathlib.Path(installation_path) if installation_path else \
            _source_path('install_output_options.js')
        self.poll_interval = max(0.1, float(poll_interval))
        self.ack_timeout = max(1, float(ack_timeout))
        self.on_status = on_status
        self.stop_event = threading.Event()
        self.ready_event = threading.Event()
        self._lock = threading.RLock()
        self._client: _Connection | None = None
        self._owner: str | None = None
        self._token = secrets.token_urlsafe(24)
        self._requests = queue.Queue(maxsize=32)
        self._volume_requests: list[tuple] = []
        self._catalog_requests: list[tuple] = []
        self._refresh_requested = threading.Event()
        self._state = {'state': 'waiting', 'connected': False, 'installed': False,
                       'port': self.port, 'last_error': None, 'mode': None,
                       'volume': None, 'restore': None}
        self._thread = threading.Thread(
            target=self._run, name='ncm-output-options', daemon=True)
        self._callback_thread = threading.Thread(
            target=self._dispatch, name='ncm-output-mode', daemon=True)

    def start(self):
        self._callback_thread.start()
        self._thread.start()
        return self

    def status(self) -> dict:
        with self._lock:
            return dict(self._state)

    def wait_ready(self, timeout: float | None = None) -> bool:
        return self.ready_event.wait(timeout)

    def refreshDevices(self):
        """Queue a native list refresh; safe inside any controller callback."""
        self._refresh_requested.set()

    def _update(self, **changes):
        with self._lock:
            changed = any(self._state.get(k) != v for k, v in changes.items())
            self._state.update(changes)
            snapshot = dict(self._state)
        if changed and self.on_status:
            try:
                self.on_status(snapshot)
            except Exception:
                pass  # A reporting callback must not break audio control.

    def _on_event(self, client, message):
        if message.get('method') != 'Runtime.bindingCalled':
            return
        event = message.get('params', {})
        if event.get('name') != BINDING_NAME:
            return
        try:
            payload = json.loads(event.get('payload', ''))
            if (not isinstance(payload, dict) or
                    payload.get('token') != self._token or
                    payload.get('owner') != self._owner or
                    not isinstance(payload.get('id'), str)):
                return
            kind = payload.get('kind')
            if client is not self._client and (client not in list(self._eq_clients.values()) or kind != 'eq'):
                return
            value = payload.get('value')
            request = (client, event.get('executionContextId'), payload)
            if (kind == 'mode' and isinstance(value, dict) and
                    value.get('mode') in ('shared', 'exclusive')):
                device = _device_dto(value.get('device'))
                if device is None:
                    return
                payload['value'] = {'mode': value['mode'], 'device': device}
                self._requests.put_nowait(request)
            elif kind == 'eq' and self.set_equalizer is not None:
                if value is not None:
                    from eq_settings import validate
                    payload['value'] = validate(value)
                self._requests.put_nowait(request)
            elif (kind == 'volume' and self.set_volume is not None and
                    isinstance(value, (int, float)) and not isinstance(value, bool) and
                    math.isfinite(value)):
                payload['value'] = max(0.0, min(1.0, float(value)))
                with self._lock:
                    self._volume_requests.append(request)
            elif kind == 'catalog' and isinstance(value, list) and len(value) <= 512:
                devices = [_device_dto(device) for device in value]
                if any(device is None for device in devices):
                    return
                payload['value'] = devices
                with self._lock:
                    self._catalog_requests.append(request)
        except (ValueError, TypeError, queue.Full):
            pass

    def _current_request(self, request):
        client, _, payload = request
        return ((client is self._client or client in list(self._eq_clients.values())) and not client.closed.is_set() and
                payload['owner'] == self._owner and not self.stop_event.is_set())

    def _acknowledge(self, requests, ok, message):
        groups = {}
        for client, context_id, payload in requests:
            key = (client, context_id, payload['owner'])
            groups.setdefault(key, []).append(payload['id'])
        for (client, context_id, owner), ids in groups.items():
            expression = (
                '(function(){const b=window[' + json.dumps(BRIDGE_GLOBAL) + '];'
                'return !!b&&b.owner===' + json.dumps(owner) + '&&' +
                json.dumps(ids) + '.map(id=>b.settle(id,' +
                json.dumps(ok) + ',' + json.dumps(message) + '));})()')
            try:
                client.evaluate(expression, timeout=3, context_id=context_id)
            except Exception:
                pass  # Reload may destroy the requesting execution context.

    def _apply(self, kind, requests):
        requests = [request for request in requests if self._current_request(request)]
        if not requests:
            return
        value = requests[-1][2]['value']
        ok, message = True, 'Output setting accepted'
        changes = {}
        try:
            if kind == 'catalog':
                result = self.get_catalog()
                if not isinstance(result, dict) or not isinstance(result.get('devices'), list):
                    raise CdpError('Device catalog returned an invalid snapshot')
                encoded = json.dumps(result, allow_nan=False)
                if len(encoded) > 4 * 1024 * 1024:
                    raise CdpError('Device catalog snapshot is too large')
                message = json.loads(encoded)
                changes = {'catalog_generation': result.get('generation')}
                result = True
            elif kind == 'eq':
                message = self.set_equalizer(value)
                if not isinstance(message, dict):
                    raise CdpError('EQ returned invalid status')
                json.dumps(message, allow_nan=False)
                result = True
            elif kind == 'mode':
                result = self.select_mode(value['mode'], value['device'])
                changes = {'mode': value['mode'], 'device': value['device']}
            else:
                result = self.set_volume(value)
                changes = {'gain': value}
            if result is False:
                raise CdpError('Native adapter rejected the output setting')
            self._update(**changes)
        except Exception as exc:
            ok, message = False, str(exc) or 'Native output setting failed'
            self._update(last_error=message)
        self._acknowledge(requests, ok, message)

    def _dispatch(self):
        while not self.stop_event.is_set():
            # Coalesce slider drags to the latest value, but settle every
            # renderer Promise. A volume burst cannot queue ahead of mode acks.
            with self._lock:
                volumes, self._volume_requests = self._volume_requests, []
            if volumes:
                self._apply('volume', volumes)
            try:
                request = self._requests.get_nowait()
            except queue.Empty:
                request = None
            if request is not None:
                self._apply(request[2]['kind'], [request])
                continue
            with self._lock:
                catalogs, self._catalog_requests = self._catalog_requests, []
            if catalogs:
                self._apply('catalog', catalogs)
            else:
                self.stop_event.wait(0.02)

    def _install(self, client):
        injection = self.injection_path.read_text(encoding='utf-8-sig')
        installation = self.installation_path.read_text(encoding='utf-8-sig')
        self._close_eq_clients()
        self._owner = secrets.token_urlsafe(18)
        return client.evaluate(_install_expression(
            injection, installation, self._token, self._owner, self.ack_timeout,
            volume_enabled=self.set_volume is not None),
            timeout=self.ack_timeout + 10)

    def _close_eq_clients(self):
        clients=list(self._eq_clients.values())
        self._eq_clients.clear()
        for client in clients:
            try:
                client.evaluate('(function(){const b=window['+json.dumps(BRIDGE_GLOBAL)+'];if(b&&b.dispose)b.dispose();})()',timeout=1)
            except Exception: pass
            client.close()

    def _sync_eq_panels(self):
        if self.set_equalizer is None or self.stop_event.is_set():return
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{self.port}/json/list',timeout=2) as response:
                pages=json.load(response)
            if not isinstance(pages,list) or len(pages)>64:raise CdpError('Invalid EQ target list')
            wanted={}
            for page in pages:
                if not isinstance(page,dict) or page.get('type')!='page':continue
                u=urllib.parse.urlparse(page.get('url',''))
                if u.scheme!='orpheus' or u.netloc!='orpheus' or u.path!='/pub/subApp.html':continue
                if urllib.parse.parse_qs(u.query).get('route')!=['audioEffect']:continue
                endpoint=urllib.parse.urlparse(page.get('webSocketDebuggerUrl',''))
                if endpoint.scheme!='ws' or endpoint.hostname not in ('127.0.0.1','localhost','::1') or endpoint.port!=self.port or endpoint.username or endpoint.password:
                    raise CdpError('Unexpected EQ debug endpoint')
                if isinstance(page.get('id'),str):wanted[page['id']]=page
            for target,client in list(self._eq_clients.items()):
                if target not in wanted or client.closed.is_set():
                    self._eq_clients.pop(target,None);client.close()
            for target,page in wanted.items():
                if self.stop_event.is_set():return
                client=self._eq_clients.get(target)
                try:
                    if client is None:
                        client=_Connection(page['webSocketDebuggerUrl'],self._on_event)
                        self._eq_clients[target]=client
                        client.call('Runtime.enable')
                        client.call('Runtime.addBinding',{'name':BINDING_NAME})
                    current=client.evaluate('(function(){const b=window['+json.dumps(BRIDGE_GLOBAL)+'];return b&&b.installed&&b.owner==='+json.dumps(self._owner)+';})()',timeout=2)
                    if not current:
                        expression=_source_path('equalizer-transport.js').read_text(encoding='utf-8')
                        values={'__TOKEN__':json.dumps(self._token),'__OWNER__':json.dumps(self._owner),
                            '__GLOBAL__':json.dumps(BRIDGE_GLOBAL),'__BINDING__':json.dumps(BINDING_NAME),
                            '__UI__':_source_path('equalizer-panel.js').read_text(encoding='utf-8')}
                        for key,value in values.items():expression=expression.replace(key,value)
                        client.evaluate(expression,timeout=5)
                    self._update(eq_entry='native-equalizer',eq_error=None)
                except Exception:
                    self._eq_clients.pop(target,None);client.close();raise
        except Exception as exc:
            # UI integration is independent of the audio stream and main bridge.
            self._update(eq_error=str(exc))

    def _run(self):
        while not self.stop_event.is_set():
            client = None
            try:
                page = _target(self.port)
                if page is None:
                    self._update(state='waiting', connected=False, installed=False,
                                 last_error='Waiting for NCM main app page')
                    self.stop_event.wait(self.poll_interval)
                    continue
                client = _Connection(page['webSocketDebuggerUrl'], self._on_event)
                self._client = client
                client.call('Runtime.enable')
                client.call('Runtime.addBinding', {'name': BINDING_NAME})
                self._update(state='connecting', connected=True, installed=False,
                             target_id=page.get('id'), target_url=page.get('url'))
                while not self.stop_event.is_set() and not client.closed.is_set():
                    snapshot = client.evaluate(
                        '(function(){const b=window[' + json.dumps(BRIDGE_GLOBAL) +
                        '];return b&&typeof b.snapshot==="function"?b.snapshot():null;})()',
                        timeout=4)
                    if (not snapshot or snapshot.get('owner') != self._owner or
                            not snapshot.get('installed')):
                        self.ready_event.clear()
                        snapshot = self._install(client)
                    if not snapshot or not snapshot.get('installed'):
                        raise CdpError('Output options were not installed')
                    self._update(state='ready', connected=True, installed=True,
                                 last_error=snapshot.get('volumeError'), mode=snapshot.get('lastMode'),
                                 device=snapshot.get('lastDevice'),
                                 volume=snapshot.get('volume'), restore=snapshot.get('restore'))
                    self.ready_event.set()
                    self._sync_eq_panels()
                    if self._refresh_requested.is_set():
                        self._refresh_requested.clear()
                        client.evaluate(
                            '(function(){const a=window.__codexOutputOptions;'
                            'if(a&&a.refreshDevices)a.refreshDevices();})()', timeout=3)
                    self.stop_event.wait(self.poll_interval)
            except Exception as exc:
                if not self.stop_event.is_set():
                    self.ready_event.clear()
                    self._update(state='waiting', connected=False, installed=False,
                                 last_error=str(exc))
            finally:
                self._close_eq_clients()
                if client:
                    client.close()
                if self._client is client:
                    self._client = None
                self.ready_event.clear()
                if client and not self.stop_event.is_set():
                    self._update(state='waiting', connected=False, installed=False)
            self.stop_event.wait(self.poll_interval)
        self.ready_event.clear()
        self._update(state='stopped', connected=False, installed=False)

    def stop(self, timeout: float = 3):
        self.stop_event.set()
        client = self._client
        if client and not client.closed.is_set():
            try:
                client.evaluate(
                    '(function(){const b=window[' + json.dumps(BRIDGE_GLOBAL) + '];'
                    'if(!b||b.owner!==' + json.dumps(self._owner) + ')return;'
                    'b.dispose("Output bridge stopped");'
                    'if(window.__codexOutputOptions)window.__codexOutputOptions.uninstall();'
                    'delete window[' + json.dumps(BRIDGE_GLOBAL) + '];})()', timeout=1)
            except Exception:
                pass
            client.close()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout)
        if (self._callback_thread.is_alive() and
                threading.current_thread() is not self._callback_thread):
            self._callback_thread.join(min(timeout, 1))


def start(select_mode: Callable[[str, dict], Any], **options) -> OutputOptionsBridge:
    """Start a daemon watcher. The returned object exposes stop/status/events."""
    return OutputOptionsBridge(select_mode, **options).start()
