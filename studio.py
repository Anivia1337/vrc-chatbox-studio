"""VRC Chatbox Studio - a live VRChat chatbox over OSC, with a web UI in a native window.

One sender loop builds and sends the chatbox message; small pollers keep live data
(media, Spotify, heart rate, hardware, VRChat process) fresh in `live`. The UI talks to
a local Flask API guarded by a per-run key.
"""
import asyncio
import base64
import ctypes
import hashlib
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
import webbrowser
from collections import defaultdict, deque
from datetime import datetime

import psutil
import requests
from flask import Flask, abort, jsonify, request, send_from_directory
from pythonosc.dispatcher import Dispatcher
from pythonosc.udp_client import SimpleUDPClient
from werkzeug.serving import make_server

import hardware

VERSION = '0.1.0'
FROZEN = getattr(sys, 'frozen', False)
BASE = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
UI_DIR = os.path.join(getattr(sys, '_MEIPASS', BASE), 'ui')
CONFIG_PATH = os.environ.get('STUDIO_CONFIG') or os.path.join(BASE, 'chatbox-studio.json')
LOG_PATH = os.path.join(BASE, 'chatbox-studio.log')
SPOTIFY_REDIRECT = 'http://127.0.0.1:8000/callback'  # loopback redirect, allowed by Spotify

DEFAULTS = {
  'layout': [],
  'messageString': '',
  'message_delay': 1.5,
  'scrollText': False,
  'FileToRead': '',
  'sendBlank': True,
  'suppressDuplicates': False,
  'sendASAP': False,
  'timeDisplayAM': '{hour}:{minute} AM',
  'timeDisplayPM': '{hour}:{minute} PM',
  'useTimeParameters': False,
  'useMediaManager': True,
  'useSpotifyApi': False,
  'songDisplay': '♪ {title} — {artist}',
  'spotifySongDisplay': '♪ {title} — {artist} [{song_progress}/{song_length}]',
  'spotify_client_id': '',
  'spotifyAccessToken': '',
  'spotifyRefreshToken': '',
  'showPaused': True,
  'hideSong': False,
  'removeParenthesis': False,
  'showOnChange': False,
  'songChangeTicks': 1,
  'cpuDisplay': 'CPU {cpu_percent}%',
  'ramDisplay': 'RAM {ram_used}/{ram_total} GB',
  'gpuDisplay': 'GPU {gpu_percent}%',
  'gpuChoice': 'auto',
  'hrDisplay': '♥ {hr} bpm',
  'usePulsoid': True,
  'useHypeRate': False,
  'pulsoidToken': '',
  'hypeRateKey': '',
  'hypeRateSessionId': '',
  'avatarHR': False,
  'toggleBeat': True,
  'blinkOverride': False,
  'blinkSpeed': 0.5,
  'playTimeDisplay': 'In VRChat {hours}:{remainder_minutes}',
  'mutedDisplay': 'mic off',
  'unmutedDisplay': '',
  'topBar': '┌────────────┐',
  'middleBar': '──────────────',
  'bottomBar': '└────────────┘',
  'verticalDivider': '|',
  'hideOutside': True,
  'timerDisplay': '{hours}:{minutes}:{seconds}',
  'timerEndStamp': 0,
  'keybind_run': '`',
  'keybind_afk': 'end',
  'useRunKeybind': True,
  'useAfkKeybind': False,
  'oscListen': False,
  'oscListenAddress': '127.0.0.1',
  'oscListenPort': 9001,
  'oscSendAddress': '127.0.0.1',
  'oscSendPort': 9000,
  'oscForeword': False,
  'oscForewordAddress': '127.0.0.1',
  'oscForewordPort': 9002,
  'theme': 'auto',
  'minimizeOnStart': False,
  'updatePrompt': True,
  'showSongInfo': True,
  'logOutput': False,
}
ELEMENTS = ('text', 'time', 'song', 'cpu', 'ram', 'gpu', 'hr', 'mute', 'playtime', 'div', 'timer')

# Field order of OSC Chat Tools' please-do-not-delete.txt (config 1.5.69.42 - 1.5.72), read for migration only
OLD_KEYS = ['confVersion', 'message_delay', 'messageString', 'FileToRead', 'scrollText', 'hideSong', 'hideOutside', 'showPaused', 'songDisplay', 'showOnChange', 'songChangeTicks', 'minimizeOnStart', 'keybind_run', 'keybind_afk', 'topBar', 'middleBar', 'bottomBar', 'pulsoidToken', 'avatarHR', 'blinkOverride', 'blinkSpeed', 'useAfkKeybind', 'toggleBeat', 'updatePrompt', 'oscListenAddress', 'oscListenPort', 'oscSendAddress', 'oscSendPort', 'oscForewordAddress', 'oscForeword', 'oscListen', 'oscForeword', 'logOutput', 'layoutString', 'verticalDivider', 'cpuDisplay', 'ramDisplay', 'gpuDisplay', 'hrDisplay', 'playTimeDisplay', 'mutedDisplay', 'unmutedDisplay', 'darkMode', 'sendBlank', 'suppressDuplicates', 'sendASAP', 'useMediaManager', 'useSpotifyApi', 'spotifySongDisplay', 'spotifyAccessToken', 'spotifyRefreshToken', 'usePulsoid', 'useHypeRate', 'hypeRateKey', 'hypeRateSessionId', 'timeDisplayPM', 'timeDisplayAM', 'showSongInfo', 'spotify_client_id', 'useTimeParameters', 'removeParenthesis', 'timerDisplay', 'timerEndStamp']

cfg = dict(DEFAULTS)
cfg_lock = threading.Lock()
state = {'run': True, 'afk': False, 'preview': '', 'next_send': 0.0, 'spotify': 'Unlinked', 'latest': '', 'binding': False}
live = defaultdict(str, {'hr': 0, 'isAfk': False, 'isMute': False, 'vrchat_pid': None, 'media': {}})
log_lines = deque(maxlen=500)
log_id = 0
log_lock = threading.Lock()
wake = threading.Event()  # interrupts the sender's wait (settings changed, run toggled, ...)


def log(text):
  global log_id
  stamp = datetime.now().strftime('%H:%M:%S')
  with log_lock:
    log_id += 1
    log_lines.append((log_id, stamp, text))
  try:
    print(stamp, text, flush=True)
  except (UnicodeEncodeError, OSError, AttributeError):
    pass  # cp1252 consoles / no console in the windowed exe
  if cfg['logOutput']:
    try:
      with open(LOG_PATH, 'a', encoding='utf-8') as f:
        f.write(f'{datetime.now()} {text}\n')
    except OSError:
      pass


# ---------------------------------------------------------------- config

def coerce(key, value):
  default = DEFAULTS[key]
  if isinstance(default, bool):
    return value if isinstance(value, bool) else str(value).lower() in ('1', 'true', 'yes', 'on')
  if isinstance(default, int):
    return int(float(value))
  if isinstance(default, float):
    return float(value)
  if isinstance(default, list):
    return [e for e in value if isinstance(e, dict) and e.get('t') in ELEMENTS]
  return str(value)


def parse_old_layout(layout_string):
  out = []
  for name, flag in re.findall(r'\{(\w+)\((\d)\)\}', layout_string or ''):
    if name in ELEMENTS:
      out.append({'t': name, 'div': flag in '13', 'nl': flag in '23'})
  return out


def import_old_config():
  """One-time import from an existing OSC Chat Tools install (same folder or its installer folder)."""
  import ast
  candidates = [os.path.join(BASE, 'please-do-not-delete.txt'),
                os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs', 'OSC Chat Tools', 'please-do-not-delete.txt')]
  for path in candidates:
    try:
      with open(path, encoding='utf-8') as f:
        values = ast.literal_eval(f.read())
    except (OSError, ValueError, SyntaxError):
      continue
    if len(values) != len(OLD_KEYS):
      log(f'Old config at {path} has an unknown format, skipped')
      continue
    old = dict(zip(OLD_KEYS, values))
    for key, value in old.items():
      if key in DEFAULTS:
        try:
          cfg[key] = coerce(key, value)
        except (ValueError, TypeError):
          pass
    cfg['layout'] = parse_old_layout(old.get('layoutString'))
    cfg['theme'] = 'dark' if old.get('darkMode') else 'auto'
    log(f'Imported settings from {path}')
    return True
  return False


def load_config():
  path = CONFIG_PATH
  if not os.path.exists(path) and os.path.exists(os.path.join(BASE, 'oct-config.json')):
    path = os.path.join(BASE, 'oct-config.json')  # preview builds used this name
  try:
    with open(path, encoding='utf-8') as f:
      data = json.load(f)
    for key, value in data.items():
      if key in DEFAULTS:
        try:
          cfg[key] = coerce(key, value)
        except (ValueError, TypeError):
          pass
    if path != CONFIG_PATH:
      save_config()
  except FileNotFoundError:
    if import_old_config():
      save_config()
  except (OSError, ValueError) as e:
    log(f'Config could not be read, using defaults: {e}')


def save_config():
  with cfg_lock:
    data = json.dumps(cfg, ensure_ascii=False, indent=1)
  tmp = CONFIG_PATH + '.tmp'
  with open(tmp, 'w', encoding='utf-8') as f:
    f.write(data)
  os.replace(tmp, CONFIG_PATH)


def update_config(changes):
  restart_osc = restart_keys = False
  with cfg_lock:
    for key, value in changes.items():
      if key not in DEFAULTS:
        continue
      cfg[key] = coerce(key, value)
      restart_osc |= key.startswith('osc')
      restart_keys |= key in ('keybind_run', 'keybind_afk', 'useRunKeybind', 'useAfkKeybind')
  save_config()
  if restart_osc:
    osc.restart()
  if restart_keys:
    register_hotkeys()
  wake.set()


# ---------------------------------------------------------------- formatting helpers

def fmt(template, **values):
  try:
    return template.format_map(defaultdict(str, values))
  except (ValueError, IndexError, AttributeError, KeyError):
    return template


def mmss(seconds):
  seconds = int(seconds or 0)
  return f'{seconds // 60}:{seconds % 60:02}'


def short_tz():
  name = datetime.now().astimezone().tzname() or ''
  return ''.join(w[0] for w in name.split()) if ' ' in name else name


# ---------------------------------------------------------------- OSC

class Osc:
  """Sender client plus one UDP listener that parses VRChat params and optionally forwards raw packets."""

  def __init__(self):
    self.client = None
    self.sock = None
    self.dispatcher = Dispatcher(strict_timing=False)  # never sleep on bundle timetags, handle immediately
    self.dispatcher.map('/avatar/parameters/AFK', lambda _a, v: self._set('isAfk', bool(v)))
    self.dispatcher.map('/avatar/parameters/MuteSelf', lambda _a, v: self._set('isMute', bool(v)))

  @staticmethod
  def _set(key, value):
    if live[key] != value:
      live[key] = value
      log(f'{key} {value}')
      wake.set()

  def send(self, address, value):
    try:
      self.client.send_message(address, value)
    except (OSError, AttributeError) as e:
      log(f'OSC send failed: {e}')

  def restart(self):
    self.client = SimpleUDPClient(cfg['oscSendAddress'], int(cfg['oscSendPort']))
    if self.sock:
      self.sock.close()
      self.sock = None
    if cfg['oscListen'] or cfg['oscForeword']:
      try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((cfg['oscListenAddress'], int(cfg['oscListenPort'])))
        self.sock = sock
        threading.Thread(target=self._listen, args=(sock,), daemon=True, name='osc-listen').start()
        log(f"OSC listening on {cfg['oscListenAddress']}:{cfg['oscListenPort']}"
            + (f", forwarding to {cfg['oscForewordAddress']}:{cfg['oscForewordPort']}" if cfg['oscForeword'] else ''))
      except OSError as e:
        log(f"OSC listen failed on port {cfg['oscListenPort']} - is another program using it? ({e})")

  def _listen(self, sock):
    forward = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    first = True
    while True:
      try:
        data, addr = sock.recvfrom(65535)
      except ConnectionResetError:
        continue  # Windows reports ICMP errors from earlier sends here; not fatal
      except OSError:
        return  # socket closed by restart()
      if first:
        first = False
        log(f'OSC: receiving data from {addr[0]}:{addr[1]}')
      if cfg['oscForeword']:
        try:
          forward.sendto(data, (cfg['oscForewordAddress'], int(cfg['oscForewordPort'])))
        except OSError:
          pass
      if cfg['oscListen']:
        try:
          self.dispatcher.call_handlers_for_packet(data, addr)
        except Exception as e:
          log(f'OSC packet error: {e}')


osc = Osc()


# ---------------------------------------------------------------- live data pollers

def poll_hardware():
  try:
    gpu = hardware.GpuMonitor()
  except OSError as e:
    gpu = None
    log(f'GPU counters unavailable: {e}')
  live['cpu_name'] = hardware.cpu_name()
  live['gpus'] = gpu.adapters if gpu else []
  psutil.cpu_percent()
  time.sleep(0.5)  # first readings need a short baseline
  while True:
    live['cpu_percent'] = psutil.cpu_percent()
    mem = psutil.virtual_memory()
    live['ram'] = {'ram_percent': int(mem.percent), 'ram_used': round((mem.total - mem.available) / 1073741824, 1),
                   'ram_available': round(mem.available / 1073741824, 1), 'ram_total': round(mem.total / 1073741824, 1)}
    if gpu:
      try:
        live['gpu'] = gpu.read(live['vrchat_pid'], cfg['gpuChoice'])
      except OSError:
        pass
    time.sleep(1)


def poll_vrchat():
  while True:
    pid = live['vrchat_pid']
    if not (pid and psutil.pid_exists(pid)):
      live['vrchat_pid'] = None
      for proc in psutil.process_iter(['name', 'create_time']):
        if proc.info['name'] == 'VRChat.exe':
          live['vrchat_pid'] = proc.pid
          live['vrchat_start'] = proc.info['create_time']
          log('VRChat detected')
          break
    time.sleep(3)


def song_needed():
  return state['run'] and (any(e['t'] == 'song' for e in cfg['layout']) or cfg['showSongInfo'])


def poll_windows_media():
  try:
    from winsdk.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as MediaManager
  except ImportError:
    log('Windows media controls unavailable')
    return

  async def read():
    session = (await MediaManager.request_async()).get_current_session()
    if not session:
      return {}
    props = await session.try_get_media_properties_async()
    timeline = session.get_timeline_properties()
    return {'title': props.title or '', 'artist': props.artist or '', 'album_title': props.album_title or '',
            'album_artist': props.album_artist or '', 'playing': session.get_playback_info().playback_status == 4,
            'song_progress': mmss(timeline.position.total_seconds()), 'song_length': mmss(timeline.end_time.total_seconds()),
            'url': ''}

  loop = asyncio.new_event_loop()
  last_error = ''
  while True:
    if cfg['useMediaManager'] and song_needed():
      try:
        # timeout: on some modified Windows builds the media API hangs forever (#20)
        live['media'] = loop.run_until_complete(asyncio.wait_for(read(), 3))
        last_error = ''
      except Exception as e:
        live['media'] = {}
        if str(e) != last_error:
          last_error = str(e)
          log(f'Windows media error: {e or "timed out"}')
    time.sleep(1)


class Spotify:
  API = 'https://api.spotify.com/v1'
  link_server = None  # the pending /callback server; a new link attempt replaces it

  def refresh(self):
    r = requests.post('https://accounts.spotify.com/api/token', timeout=10, data={
      'grant_type': 'refresh_token', 'refresh_token': cfg['spotifyRefreshToken'], 'client_id': cfg['spotify_client_id']})
    if r.status_code != 200:
      raise RuntimeError(f'token refresh failed: {r.text}')
    tokens = r.json()
    # Only the tokens are saved; pending UI edits are never applied behind the user's back
    update_config({'spotifyAccessToken': tokens['access_token'],
                   'spotifyRefreshToken': tokens.get('refresh_token', cfg['spotifyRefreshToken'])})

  def get(self, path):
    for attempt in range(2):
      r = requests.get(self.API + path, timeout=10, headers={'Authorization': 'Bearer ' + cfg['spotifyAccessToken']})
      if r.status_code == 401 and attempt == 0:
        self.refresh()
        continue
      if r.status_code == 204:
        return {}
      r.raise_for_status()
      return r.json()

  def check_link(self):
    if not cfg['spotifyRefreshToken']:
      state['spotify'] = 'Unlinked'
      return
    try:
      state['spotify'] = 'Linked to ' + (self.get('/me').get('display_name') or '?')
    except requests.RequestException as e:
      state['spotify'] = 'Status unknown (offline?)'
      log(f'Spotify check failed: {e}')
    except RuntimeError as e:
      state['spotify'] = 'Error - please relink'
      log(f'Spotify: {e}')

  def poll(self):
    self.check_link()
    while True:
      if cfg['useSpotifyApi'] and cfg['spotifyRefreshToken'] and song_needed():
        try:
          p = self.get('/me/player') or {}
          item = p.get('item') or {}
          artists = item.get('artists') or [{}]
          live['media'] = {'title': item.get('name', ''), 'artist': artists[0].get('name', ''),
                           'album_title': (item.get('album') or {}).get('name', ''), 'album_artist': artists[0].get('name', ''),
                           'playing': p.get('is_playing', False), 'song_progress': mmss((p.get('progress_ms') or 0) / 1000),
                           'song_length': mmss((item.get('duration_ms') or 0) / 1000),
                           'volume': str((p.get('device') or {}).get('volume_percent', '')), 'song_id': item.get('id', ''),
                           'url': (item.get('external_urls') or {}).get('spotify', '')}
        except (requests.RequestException, RuntimeError) as e:
          log(f'Spotify error: {e}')
          time.sleep(5)
      time.sleep(3)

  def link(self):
    if not cfg['spotify_client_id']:
      state['spotify'] = 'Enter your Client ID first'
      return
    verifier = base64.urlsafe_b64encode(os.urandom(40)).decode().rstrip('=')
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    url = requests.Request('GET', 'https://accounts.spotify.com/authorize', params={
      'client_id': cfg['spotify_client_id'], 'response_type': 'code', 'redirect_uri': SPOTIFY_REDIRECT,
      'scope': 'user-read-playback-state user-read-currently-playing', 'code_challenge_method': 'S256',
      'code_challenge': challenge}).prepare().url
    app = Flask('spotify-link')
    result = {}

    @app.route('/callback')
    def callback():
      try:
        if 'error' in request.args:
          raise RuntimeError(request.args['error'])
        r = requests.post('https://accounts.spotify.com/api/token', timeout=10, data={
          'grant_type': 'authorization_code', 'code': request.args.get('code', ''), 'redirect_uri': SPOTIFY_REDIRECT,
          'client_id': cfg['spotify_client_id'], 'code_verifier': verifier})
        if r.status_code != 200:
          raise RuntimeError(r.text)
        update_config({'spotifyAccessToken': r.json()['access_token'], 'spotifyRefreshToken': r.json()['refresh_token'],
                       'useSpotifyApi': True, 'useMediaManager': False})
        self.check_link()
        result['ok'] = True
        return _callback_page('Spotify linked', 'You can close this tab and return to VRC Chatbox Studio.')
      except Exception as e:
        log(f'Spotify link failed: {e}')
        state['spotify'] = 'Link failed'
        return _callback_page('Linking failed', str(e))
      finally:
        threading.Thread(target=server.shutdown, daemon=True).start()

    # Windows lets a second server bind the same port, and Spotify's redirect would then reach the
    # old one with the old code_verifier ("code_verifier was incorrect"). Close it first.
    old, self.link_server = self.link_server, None
    if old:
      old.shutdown()
      old.server_close()
    try:
      server = make_server('127.0.0.1', 8000, app)
    except OSError:
      state['spotify'] = 'Port 8000 is busy'
      log('Spotify link needs port 8000, but another program uses it')
      return
    self.link_server = server
    state['spotify'] = 'Waiting for browser...'
    webbrowser.open(url)
    timeout = threading.Timer(300, server.shutdown)  # give up after 5 minutes
    timeout.start()
    server.serve_forever()
    timeout.cancel()
    server.server_close()
    if self.link_server is server:
      self.link_server = None
      if not result:
        self.check_link()


def _callback_page(title, text):
  from html import escape
  return (f'<!doctype html><meta charset=utf-8><title>{escape(title)}</title>'
          '<body style="font:16px system-ui;display:grid;place-items:center;height:90vh;background:#14161c;color:#e8eaf0">'
          f'<div style="text-align:center"><h1>{escape(title)}</h1><p>{escape(text)}</p></div>')


spotify = Spotify()


def heart_rate():
  from websocket import WebSocketTimeoutException, create_connection
  beat_stop = threading.Event()

  def beat():
    while not beat_stop.is_set():
      bpm = int(live['hr'] or 0)
      if cfg['toggleBeat'] and bpm > 0:
        on = cfg['blinkSpeed'] if cfg['blinkOverride'] else 0.1
        osc.send('/avatar/parameters/isHRBeat', True)
        time.sleep(on)
        osc.send('/avatar/parameters/isHRBeat', False)
        time.sleep(max(0.05, 60 / bpm - on))
      else:
        time.sleep(1)

  while True:
    wanted = (state['run'] and any(e['t'] == 'hr' for e in cfg['layout'])) or cfg['avatarHR']
    if not wanted:
      time.sleep(1)
      continue
    hyperate = cfg['useHypeRate']
    try:
      if hyperate:
        ws = create_connection('wss://app.hyperate.io/socket/websocket?token=' + cfg['hypeRateKey'], timeout=10)
        ws.send(json.dumps({'topic': 'hr:' + cfg['hypeRateSessionId'], 'event': 'phx_join', 'payload': {}, 'ref': 0}))
      else:
        ws = create_connection('wss://dev.pulsoid.net/api/v1/data/real_time?access_token=' + cfg['pulsoidToken']
                               + '&response_mode=text_plain_only_heart_rate', timeout=10)
      ws.settimeout(1)
      log('Heart rate connected (' + ('HypeRate' if hyperate else 'Pulsoid') + ')')
      beat_stop.clear()
      threading.Thread(target=beat, daemon=True, name='hr-beat').start()
      last_ping = time.time()
      while ((state['run'] and any(e['t'] == 'hr' for e in cfg['layout'])) or cfg['avatarHR']) and cfg['useHypeRate'] == hyperate:
        try:
          msg = ws.recv()
        except WebSocketTimeoutException:
          if hyperate and time.time() - last_ping > 25:  # Phoenix sockets drop idle clients
            ws.send(json.dumps({'topic': 'phoenix', 'event': 'heartbeat', 'payload': {}, 'ref': 0}))
            last_ping = time.time()
          continue
        bpm = (json.loads(msg).get('payload') or {}).get('hr') if hyperate else msg
        if bpm is None:
          continue
        live['hr'] = int(float(bpm))
        osc.send('/avatar/parameters/isHRActive', True)
        osc.send('/avatar/parameters/isHRConnected', True)
        osc.send('/avatar/parameters/HR', live['hr'])
      ws.close()
      log('Heart rate disconnected')
    except Exception as e:
      log(f'Heart rate error: {e} - retrying in 10 s')
      time.sleep(10)
    finally:
      beat_stop.set()
      live['hr'] = 0


def time_parameters():
  while True:
    if cfg['useTimeParameters']:
      now = datetime.now()
      osc.send('/avatar/parameters/Hours', now.hour % 12 or 12)
      osc.send('/avatar/parameters/Minutes', now.minute)
      osc.send('/avatar/parameters/Seconds', now.second)
      osc.send('/avatar/parameters/Period', now.hour >= 12)
    time.sleep(1)


def check_update():
  try:
    tag = requests.get('https://api.github.com/repos/Anivia1337/vrc-chatbox-studio/releases/latest', timeout=10).json().get('tag_name')
    if not tag:
      return  # no release published yet
    state['latest'] = tag.lstrip('vV')
    log(f'Latest release: {state["latest"]}')
  except Exception as e:
    log(f'Update check failed: {e}')


# ---------------------------------------------------------------- message building

class Builder:
  def __init__(self):
    self.frame = 0
    self.file_pos = 0
    self.last_song = ''
    self.song_ticks = 0
    self.afk_blink = False

  def frames(self):
    lines = cfg['messageString'].split('\n') if cfg['messageString'] else ['']
    return lines

  def element(self, t, text):
    if t == 'text':
      return text
    if t == 'time':
      now = datetime.now()
      template = cfg['timeDisplayPM'] if now.hour >= 12 else cfg['timeDisplayAM']
      return fmt(template, hour=now.hour % 12 or 12, minute=f'{now.minute:02}', hour24=f'{now.hour:02}', time_zone=short_tz())
    if t == 'song':
      return self.song()
    if t == 'cpu':
      return fmt(cfg['cpuDisplay'], cpu_percent=live['cpu_percent'], cpu_name=live['cpu_name'])
    if t == 'ram':
      return fmt(cfg['ramDisplay'], **(live['ram'] or {}))
    if t == 'gpu':
      return fmt(cfg['gpuDisplay'], **(live['gpu'] or {}))
    if t == 'hr':
      return fmt(cfg['hrDisplay'], hr=live['hr'] if int(live['hr'] or 0) > 1 else '-')
    if t == 'mute':
      return cfg['mutedDisplay'] if live['isMute'] else cfg['unmutedDisplay']
    if t == 'playtime':
      minutes = int((time.time() - live['vrchat_start']) / 60) if live['vrchat_pid'] else 0
      return fmt(cfg['playTimeDisplay'], hours=f'{minutes // 60:02}', remainder_minutes=f'{minutes % 60:02}', minutes=f'{minutes:02}')
    if t == 'div':
      return cfg['middleBar']
    if t == 'timer':
      left = max(0, int(cfg['timerEndStamp'] / 1000 - time.time()))
      return fmt(cfg['timerDisplay'], hours=f'{left // 3600:02}', minutes=f'{left % 3600 // 60:02}', seconds=f'{left % 60:02}')
    return ''

  def song(self):
    m = live['media'] or {}
    title = m.get('title', '')
    if cfg['removeParenthesis']:
      title = re.sub(r' ?\([^)]*\)', '', title)
    if not title or (cfg['hideSong'] and not m.get('playing')):
      return ''
    template = cfg['spotifySongDisplay'] if cfg['useSpotifyApi'] else cfg['songDisplay']
    out = fmt(template, **{**m, 'title': title})
    if cfg['showPaused'] and not m.get('playing'):
      out += ' ⏸️'
    if cfg['showOnChange']:
      if out != self.last_song:
        self.last_song = out
        self.song_ticks = cfg['songChangeTicks']
      if self.song_ticks <= 0:
        return ''
      self.song_ticks -= 1
    return out

  def layout(self, text):
    parts = []
    for el in cfg['layout']:
      s = self.element(el['t'], text)
      if not s:
        continue
      if el.get('div'):
        s += f" {cfg['verticalDivider']} "
      if el.get('nl'):
        s += '\v'
      parts.append(s)
    msg = ''.join(parts)
    while True:  # drop dangling dividers/newlines at the end
      trimmed = msg.rstrip(' \v')
      for suffix in (cfg['verticalDivider'], cfg['middleBar']):
        if suffix and trimmed.endswith(suffix):
          trimmed = trimmed[:-len(suffix)]
      if trimmed == msg:
        break
      msg = trimmed
    if not cfg['hideOutside']:
      msg = f"{cfg['topBar']} {msg} {cfg['bottomBar']}"
    return msg.replace('\\n', '\v').replace('\\v', '\v')

  def next(self, advance=True):
    """The next chatbox message; advance=False builds a preview without moving frames."""
    if state['afk']:
      if advance:
        self.afk_blink = not self.afk_blink
      return cfg['topBar'] + ('\vAFK\v' if self.afk_blink or not advance else '\vㅤ\v') + cfg['bottomBar']
    if cfg['scrollText']:
      try:
        with open(cfg['FileToRead'], encoding='utf-8') as f:
          content = f.read()
      except OSError as e:
        return f'File error: {e}'
      if self.file_pos >= len(content):
        self.file_pos = 0
      chunk = content[self.file_pos:self.file_pos + 144]
      if advance:
        self.file_pos += 144
      return chunk
    frames = self.frames()
    line = frames[self.frame % len(frames)]
    if advance:
      self.frame += 1
    text = ' ㅤ' if line == '*' else ' ' + line if line else ''
    return self.layout(text)


builder = Builder()


def sender():
  time.sleep(1)  # let pollers fill `live` so the first message isn't half empty
  last_sent, last_time, was_running = None, 0.0, True
  while True:
    if not state['run']:
      if was_running and cfg['sendBlank']:
        osc.send('/chatbox/input', ['', True, False])
      was_running = False
      state['preview'] = builder.next(advance=False)
      state['next_send'] = 0
      wake.wait(0.5)
      wake.clear()
      continue
    was_running = True
    msg = builder.next()
    state['preview'] = msg
    skipped = cfg['suppressDuplicates'] and msg == last_sent and time.time() - last_time < 25
    if not skipped:
      osc.send('/chatbox/input', [msg, True, False])
      last_sent, last_time = msg, time.time()
    delay = 0.25 if skipped and cfg['sendASAP'] else max(0.5, cfg['message_delay'])
    state['next_send'] = time.time() + delay
    wake.clear()
    wake.wait(delay)


# ---------------------------------------------------------------- hotkeys

def toggle_run():
  state['run'] = not state['run']
  log('Running' if state['run'] else 'Paused')
  wake.set()


def toggle_afk():
  state['afk'] = not state['afk']
  wake.set()


hotkeys = []


def register_hotkeys():
  try:
    import keyboard
    while hotkeys:
      keyboard.remove_hotkey(hotkeys.pop())
    if cfg['useRunKeybind'] and cfg['keybind_run']:
      hotkeys.append(keyboard.add_hotkey(cfg['keybind_run'], toggle_run))
    if cfg['useAfkKeybind'] and cfg['keybind_afk']:
      hotkeys.append(keyboard.add_hotkey(cfg['keybind_afk'], toggle_afk))
  except Exception as e:
    log(f'Keybind error: {e}')


def afk_from_osc():
  """Without an AFK keybind, VRChat's own AFK parameter drives the AFK message."""
  last = False
  while True:
    if not cfg['useAfkKeybind'] and live['isAfk'] != last:
      last = live['isAfk']
      state['afk'] = last
      wake.set()
    time.sleep(0.5)


# ---------------------------------------------------------------- API

REPO = 'https://github.com/Anivia1337/vrc-chatbox-studio'
LINKS = {
  'github': REPO,
  'issues': REPO + '/issues',
  'releases': REPO + '/releases/latest',
  'guide': REPO + '#readme',
  'spotifyGuide': REPO + '#spotify',
  'spotifyDashboard': 'https://developer.spotify.com/dashboard',
  'pulsoidToken': 'https://pulsoid.net/ui/keys',
  'hypeRate': 'https://www.hyperate.io/',
}

KEY = secrets.token_urlsafe(16)
api = Flask(__name__, static_folder=None)
window = None  # pywebview window, for native file dialogs


@api.before_request
def guard():
  if request.path.startswith('/api/') and request.headers.get('X-Studio-Key') != KEY:
    abort(403)


@api.route('/')
def index():
  return send_from_directory(UI_DIR, 'index.html')


@api.route('/ui/<path:name>')
def static_ui(name):
  return send_from_directory(UI_DIR, name)


@api.get('/api/config')
def get_config():
  with cfg_lock:
    data = dict(cfg)
  data['spotifyAccessToken'] = data['spotifyRefreshToken'] = ''  # never needed by the UI
  return jsonify(config=data, defaults=DEFAULTS, version=VERSION, links=list(LINKS))


@api.post('/api/config')
def post_config():
  try:
    update_config(request.get_json(force=True) or {})
  except (ValueError, TypeError) as e:
    return jsonify(error=str(e)), 400
  return jsonify(ok=True)


@api.get('/api/state')
def get_state():
  since = int(request.args.get('log', 0))
  with log_lock:
    lines = [l for l in log_lines if l[0] > since]
  return jsonify(
    run=state['run'], afk=state['afk'], preview=state['preview'],
    nextIn=max(0, round(state['next_send'] - time.time(), 1)) if state['run'] else None,
    media=live['media'] or {}, hr=live['hr'], gpu=live['gpu'] or {}, gpus=live['gpus'] or [],
    cpu={'name': live['cpu_name'], 'percent': live['cpu_percent']}, ram=live['ram'] or {},
    vrchat=bool(live['vrchat_pid']), spotify=state['spotify'], latest=state['latest'], version=VERSION,
    mute=live['isMute'], log=lines, timerLeft=max(0, int(cfg['timerEndStamp'] / 1000 - time.time())))


@api.post('/api/action/<name>')
def action(name):
  body = request.get_json(silent=True) or {}
  if name == 'run':
    toggle_run()
  elif name == 'afk':
    toggle_afk()
  elif name == 'open' and body.get('link') in LINKS:
    webbrowser.open(LINKS[body['link']])
  elif name == 'spotifyLink':
    threading.Thread(target=spotify.link, daemon=True).start()
  elif name == 'spotifyUnlink':
    update_config({'spotifyAccessToken': '', 'spotifyRefreshToken': '', 'useSpotifyApi': False, 'useMediaManager': True})
    state['spotify'] = 'Unlinked'
  elif name == 'timerAdd':
    start = max(cfg['timerEndStamp'], time.time() * 1000)
    update_config({'timerEndStamp': int(start + float(body.get('seconds', 0)) * 1000)})
  elif name == 'timerReset':
    update_config({'timerEndStamp': 0})
  elif name == 'bindKey':
    import keyboard
    state['binding'] = True
    try:
      key = keyboard.read_key()
    finally:
      state['binding'] = False
    return jsonify(key=key)
  elif name == 'debugSend':
    raw, kind = str(body.get('value', '')), body.get('type', 'int')
    try:
      value = {'int': int, 'float': float, 'bool': lambda v: v.lower() in ('1', 'true'), 'str': str}[kind](raw)
    except (KeyError, ValueError):
      return jsonify(error='Invalid value for type ' + str(kind)), 400
    osc.send(str(body.get('path', '')), value)
    log(f"Debug: {body.get('path')} => {value!r}")
  elif name == 'checkUpdate':
    check_update()
  elif name == 'reset':
    tokens = {k: cfg[k] for k in ('spotifyAccessToken', 'spotifyRefreshToken')}
    update_config({**DEFAULTS, **tokens})
  elif name == 'openFolder':
    os.startfile(BASE)
  elif name == 'pickFile':
    if not window:
      return jsonify(path='')
    import webview
    picked = window.create_file_dialog(webview.OPEN_DIALOG, file_types=('Text files (*.txt)', 'All files (*.*)'))
    return jsonify(path=picked[0] if picked else '')
  else:
    abort(404)
  return jsonify(ok=True)


# ---------------------------------------------------------------- startup

def single_instance():
  ctypes.windll.kernel32.CreateMutexW(None, False, 'VRCChatboxStudio.SingleInstance')
  if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
    ctypes.windll.user32.MessageBoxW(None, 'VRC Chatbox Studio is already running.', 'VRC Chatbox Studio', 0x40)
    sys.exit(0)


def shutdown():
  if cfg['sendBlank']:
    osc.send('/chatbox/input', ['', True, False])
  os._exit(0)  # daemon threads, websockets and hotkey hooks all die with the process (#23)


def main():
  import logging
  logging.getLogger('werkzeug').setLevel(logging.ERROR)
  if '--serve' not in sys.argv:
    single_instance()
  load_config()
  log(f'VRC Chatbox Studio {VERSION} starting')
  osc.restart()
  register_hotkeys()
  for target in (poll_hardware, poll_vrchat, poll_windows_media, spotify.poll, heart_rate, time_parameters, afk_from_osc, sender, check_update):
    threading.Thread(target=target, daemon=True, name=target.__name__).start()

  port = int(sys.argv[sys.argv.index('--port') + 1]) if '--port' in sys.argv else 0
  server = make_server('127.0.0.1', port, api, threaded=True)
  threading.Thread(target=server.serve_forever, daemon=True, name='api').start()
  url = f'http://127.0.0.1:{server.server_port}/#k={KEY}'

  if '--browser' in sys.argv or '--serve' in sys.argv:  # dev: UI in a browser / headless
    print('UI:', url, flush=True)
    if '--browser' in sys.argv:
      webbrowser.open(url)
    try:
      while True:
        time.sleep(1)
    except KeyboardInterrupt:
      shutdown()
  try:
    import webview
  except ImportError:
    webbrowser.open(url)
    threading.Event().wait()
  global window
  dark = cfg['theme'] == 'dark'
  window = webview.create_window('VRC Chatbox Studio', url, width=1180, height=820, min_size=(380, 560),
                                 minimized=cfg['minimizeOnStart'], background_color='#111318' if dark else '#f4f5f8')
  window.events.closed += shutdown
  webview.start()
  shutdown()


if __name__ == '__main__':
  main()
