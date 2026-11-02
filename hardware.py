"""CPU/GPU info for any vendor via Windows APIs: PDH counters (Task Manager's source) + DXGI.

The GPU shown is the one VRChat renders on, so an iGPU or a second card busy with
AI workloads doesn't hijack the reading. Usage is the whole card's 3D load, not just VRChat's.
"""
import ctypes
import ctypes.wintypes as wt
import re
import winreg
from collections import defaultdict

_BRAND = re.compile(r'\((R|TM)\)|\b(AMD|Intel|NVIDIA)\b|\s+\d+-Core Processor|\s+CPU @.*', re.I)


def clean_name(name):
  return ' '.join(_BRAND.sub('', name).split())


def cpu_name():
  try:
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'HARDWARE\DESCRIPTION\System\CentralProcessor\0') as k:
      return clean_name(winreg.QueryValueEx(k, 'ProcessorNameString')[0])
  except OSError:
    return ''


# --- DXGI: adapter list with LUID, name, VRAM, whether it drives a display ---

class _LUID(ctypes.Structure):
  _fields_ = [('Low', wt.DWORD), ('High', wt.LONG)]


class _DESC1(ctypes.Structure):
  _fields_ = [('Description', wt.WCHAR * 128), ('VendorId', wt.UINT), ('DeviceId', wt.UINT),
              ('SubSysId', wt.UINT), ('Revision', wt.UINT), ('DedicatedVideoMemory', ctypes.c_size_t),
              ('DedicatedSystemMemory', ctypes.c_size_t), ('SharedSystemMemory', ctypes.c_size_t),
              ('AdapterLuid', _LUID), ('Flags', wt.UINT)]


class _GUID(ctypes.Structure):
  _fields_ = [('a', wt.DWORD), ('b', wt.WORD), ('c', wt.WORD), ('d', ctypes.c_ubyte * 8)]


_IID_FACTORY1 = _GUID(0x770aae78, 0xf26f, 0x4dba, (ctypes.c_ubyte * 8)(0xa8, 0x29, 0x25, 0x3c, 0x83, 0xd1, 0xb3, 0x87))


def _com(obj, index, *argtypes):
  vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
  return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtbl[index])


def list_adapters():
  factory = ctypes.c_void_p()
  if ctypes.windll.dxgi.CreateDXGIFactory1(ctypes.byref(_IID_FACTORY1), ctypes.byref(factory)) != 0:
    return []
  adapters = []
  try:
    i = 0
    while True:
      adapter = ctypes.c_void_p()
      if _com(factory, 12, wt.UINT, ctypes.POINTER(ctypes.c_void_p))(factory, i, ctypes.byref(adapter)) != 0:
        break  # DXGI_ERROR_NOT_FOUND
      i += 1
      desc = _DESC1()
      _com(adapter, 10, ctypes.POINTER(_DESC1))(adapter, ctypes.byref(desc))
      output = ctypes.c_void_p()
      display = _com(adapter, 7, wt.UINT, ctypes.POINTER(ctypes.c_void_p))(adapter, 0, ctypes.byref(output)) == 0
      if display:
        _com(output, 2)(output)
      _com(adapter, 2)(adapter)
      if desc.Flags & 2:  # software adapter (Microsoft Basic Render Driver)
        continue
      luid = 'luid_0x%08x_0x%08x' % (desc.AdapterLuid.High & 0xffffffff, desc.AdapterLuid.Low)
      if any(a['luid'] == luid for a in adapters):
        continue
      adapters.append({'luid': luid, 'name': clean_name(desc.Description), 'vram': desc.DedicatedVideoMemory, 'display': display})
  finally:
    _com(factory, 2)(factory)
  return adapters


# --- PDH: per-process/per-adapter 3D engine load and dedicated VRAM use ---

class _FMT(ctypes.Structure):
  _fields_ = [('CStatus', wt.DWORD), ('doubleValue', ctypes.c_double)]


class _ITEM(ctypes.Structure):
  _fields_ = [('szName', wt.LPWSTR), ('FmtValue', _FMT)]


_PDH_FMT_DOUBLE = 0x200
_INSTANCE = re.compile(r'(?:pid_(\d+)_)?(luid_0x[0-9a-f]+_0x[0-9a-f]+)')


class GpuMonitor:
  def __init__(self):
    self.pdh = ctypes.windll.pdh
    self.query = wt.HANDLE()
    self.engine = wt.HANDLE()
    self.memory = wt.HANDLE()
    self.pdh.PdhOpenQueryW(None, None, ctypes.byref(self.query))
    if self.pdh.PdhAddEnglishCounterW(self.query, r'\GPU Engine(*engtype_3D)\Utilization Percentage', None, ctypes.byref(self.engine)) != 0:
      raise OSError('GPU Engine counters unavailable')
    self.pdh.PdhAddEnglishCounterW(self.query, r'\GPU Adapter Memory(*)\Dedicated Usage', None, ctypes.byref(self.memory))
    self.pdh.PdhCollectQueryData(self.query)
    self.adapters = list_adapters()
    self.last_vrchat_luid = None

  def _array(self, counter):
    size, count = wt.DWORD(0), wt.DWORD(0)
    self.pdh.PdhGetFormattedCounterArrayW(counter, _PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), None)
    if not size.value:
      return []
    buf = (ctypes.c_byte * size.value)()
    if self.pdh.PdhGetFormattedCounterArrayW(counter, _PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), buf) != 0:
      return []
    items = ctypes.cast(buf, ctypes.POINTER(_ITEM))
    out = []
    for i in range(count.value):
      m = _INSTANCE.search(items[i].szName.lower())
      if m and items[i].FmtValue.CStatus in (0, 1):
        out.append((m.group(1), m.group(2), items[i].FmtValue.doubleValue))
    return out

  def read(self, vrchat_pid=None, choice='auto'):
    """Returns gpu_percent, gpu_name, vram_* for the chosen adapter."""
    self.pdh.PdhCollectQueryData(self.query)
    load = defaultdict(float)
    vrchat = defaultdict(float)
    for pid, luid, value in self._array(self.engine):
      load[luid] += value
      if vrchat_pid and pid == str(vrchat_pid):
        vrchat[luid] += value
    used = defaultdict(float)
    for _, luid, value in self._array(self.memory):
      used[luid] += value

    known = {a['luid']: a for a in self.adapters}
    if choice in known:
      luid = choice
    elif vrchat:
      luid = self.last_vrchat_luid = max(vrchat, key=vrchat.get)
    elif self.last_vrchat_luid:
      luid = self.last_vrchat_luid
    else:
      # No VRChat yet: the biggest card that drives a monitor (AI cards usually don't)
      pool = [a for a in self.adapters if a['display']] or self.adapters
      luid = max(pool, key=lambda a: a['vram'])['luid'] if pool else max(load, key=load.get, default=None)

    adapter = known.get(luid, {'name': '', 'vram': 0})
    total = adapter['vram']
    return {
      'gpu_percent': min(100, round(load.get(luid, 0))),
      'gpu_name': adapter['name'],
      'vram_percent': round(used.get(luid, 0) / total * 100) if total else 0,
      'vram_used': round(used.get(luid, 0) / 1073741824, 1),
      'vram_total': round(total / 1073741824, 1),
      'luid': luid,
    }


if __name__ == '__main__':
  import time
  print('CPU:', cpu_name())
  for a in list_adapters():
    print('GPU:', a)
  mon = GpuMonitor()
  time.sleep(1)
  r = mon.read()
  print(r)
  assert r['gpu_name'] and 0 <= r['gpu_percent'] <= 100
  assert clean_name('Intel(R) Core(TM) i7-8700K CPU @ 3.70GHz') == 'Core i7-8700K'
  assert clean_name('AMD Ryzen 7 9800X3D 8-Core Processor') == 'Ryzen 7 9800X3D'
  assert clean_name('NVIDIA GeForce RTX 4090') == 'GeForce RTX 4090'
  print('ok')
