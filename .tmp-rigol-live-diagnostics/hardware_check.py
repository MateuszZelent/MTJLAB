from pathlib import Path
from time import perf_counter, sleep
import pyvisa
from PIL import Image
from ruamel.yaml import YAML
from app.settings import StationSettings
from app.devices.visa import _ManagedVisaSession
from app.devices.rigol_dg1000z.adapter import RigolAdapter, RigolChannelConfig
p=Path('.tmp-rigol-live-diagnostics'); traffic=[]; timings=[]
rm=pyvisa.ResourceManager()
class ExclusiveFactory:
 def open(self, resource, backend, timeout_ms):
  s=rm.open_resource(resource,access_mode=pyvisa.constants.AccessModes.exclusive_lock,open_timeout=1000); s.timeout=timeout_ms
  assert 'DG1032Z,DG1ZA172902039' in s.query('*IDN?')
  assert s.query(':OUTP1?').strip()==s.query(':OUTP2?').strip()=='OFF'
  return _ManagedVisaSession(s,None,traffic.append)
settings=StationSettings.model_validate(YAML(typ='safe').load(Path('.config/settings.yml')))
a=RigolAdapter(settings,session_factory=ExclusiveFactory())
def screen(name):
 sleep(.3); raw_session=a._require_session()._session
 raw_session.write(':DISP:DATA?'); header=raw_session.read_bytes(2); assert header[:1]==b'#'; count=int(raw_session.read_bytes(int(header[1:2]))); bmp=raw_session.read_bytes(count); f=p/(name+'.bmp'); f.write_bytes(bmp); Image.open(f).save(f.with_suffix('.png'))
try:
 a.connect(); s=a._require_session(); other=s.query(':SOUR2:APPL?').strip()
 a.configure_channel(RigolChannelConfig(1,'SIN',1000.,.01,-.01))
 traffic.clear()
 for offset in [0.,.01,.02,.08]:
  start=perf_counter(); result=a.update_offset(1,offset); timings.append(('SIN offset',offset,(perf_counter()-start)*1000)); assert abs(result-offset)<1e-6
 for amp in [.01,.02,.04,.02]:
  start=perf_counter(); result=a.update_amplitude_vpp(1,amp); timings.append(('SIN amplitude',amp,(perf_counter()-start)*1000)); assert abs(result-amp)<1e-6
 assert not any('TX WRITE' in c and (':FUNC ' in c or ':OUTP' in c or ':APPL' in c) for c in traffic)
 screen('sin-direct-final')
 a.configure_channel(RigolChannelConfig(1,'DC',1.,.08,.08))
 start_traffic=len(traffic)
 for mv in [0,10,20,80]:
  start=perf_counter(); a.update_offset(1,mv/1000); timings.append(('DC offset',mv/1000,(perf_counter()-start)*1000)); screen(f'dc-direct-{mv}')
 for mv in range(81):
  start=perf_counter(); a.update_offset(1,mv/1000); timings.append(('DC continuous',mv/1000,(perf_counter()-start)*1000))
 writes=[c for c in traffic[start_traffic:] if 'TX WRITE' in c]
 assert len(writes)==85 and all(':SOUR1:VOLT:OFFS ' in c for c in writes),writes
 assert s.query(':SOUR2:APPL?').strip()==other
 assert s.query(':OUTP1?').strip()==s.query(':OUTP2?').strip()=='OFF'
 screen('dc-continuous-final')
 print('PASS: SIN 4 offsets + 4 amplitudes; DC 4 steps + 81 consecutive offsets; no waveform/output writes during updates; CH2 unchanged; both OFF.')
 for kind in ['SIN offset','SIN amplitude','DC offset','DC continuous']:
  ts=[t for k,v,t in timings if k==kind]; print(kind, 'count',len(ts),'min/mean/max ms',round(min(ts),1),round(sum(ts)/len(ts),1),round(max(ts),1))
 print(a.dc_readback_limitation(1))
finally:
 a.disconnect(); rm.close(); (p/'adapter-direct-traffic.txt').write_text('\n'.join(traffic)); (p/'adapter-direct-timings.txt').write_text('\n'.join(map(str,timings)))
