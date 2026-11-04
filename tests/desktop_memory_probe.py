"""Measure resize memory in a disposable desktop with bounded resources."""

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time

from web_e2e import SOURCE, run
from raven_web_host import Raven

CYCLES = 100
MEMORY_STOP = 1536 * 1024 * 1024
READ_MEMORY = """
import json
from pathlib import Path
rows = []
for path in Path('/proc').glob('[0-9]*/status'):
    try:
        fields = dict(row.split(':', 1) for row in path.read_text().splitlines() if ':' in row)
        if fields['Name'].strip() in ('Xvnc', 'xwallpaper', 'xmonad-x86_64-l'):
            rows.append({'pid': path.parent.name, 'name': fields['Name'].strip(),
                         'rss_kib': int(fields.get('VmRSS', '0 kB').split()[0])})
    except (FileNotFoundError, ProcessLookupError):
        pass
root = Path('/sys/fs/cgroup')
print(json.dumps({'processes': rows, 'memory': int((root/'memory.current').read_text()),
                  'events': (root/'memory.events').read_text()}))
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wallpaper-session", type=int,
                        help="read the image wallpaper from this owned session for the image comparison")
    options = parser.parse_args()
    scratch = Path(tempfile.mkdtemp(prefix="raven-resize-memory-"))
    environment = dict(os.environ)
    environment.pop("DISPLAY", None)
    for key in ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR"):
        path = scratch / key.lower()
        path.mkdir(mode=0o700)
        environment[key] = str(path)
    owner = Raven(SOURCE / "raven").user
    had_hostd = f"@raven-host-{os.getuid()}" in Path('/proc/net/unix').read_text()
    session_id = None
    generation = None
    measurements = []
    try:
        result = run([str(SOURCE/'raven'), 'new', '-d', '-s', 'resize-probe-'+secrets.token_hex(4),
                      '-g', '800x600', '-w', 'xmonad', 'xterm'], environment)
        session_id = re.search(rb'raven: created session ([0-9]+)', result.stderr)[1].decode()
        container = f'raven-{owner}-{session_id}'
        item = json.loads(run(['docker','inspect',container],environment).stdout)[0]
        generation = item['Id']
        assert item['HostConfig']['Memory'] == 2 * 1024**3
        display = item['Config']['Labels']['raven.display']

        def execute(*args):
            return run(['docker','exec','-e',f'DISPLAY=:{display}',
                        '-e','XAUTHORITY=/home/raven/.Xauthority',container,*args],environment)

        def sample(phase, cycle):
            data = json.loads(execute('python3','-c',READ_MEMORY).stdout)
            data.update(phase=phase, cycle=cycle)
            measurements.append(data)
            (scratch/'metrics.json').write_text(json.dumps(measurements, indent=2))
            print(json.dumps(data),flush=True)
            assert data['memory'] < MEMORY_STOP, 'Probe memory safety threshold exceeded'

        print(execute('dpkg-query','-W','tigervnc-standalone-server','xwallpaper','libxcb-randr0').stdout.decode(),flush=True)
        for width,height in ((800,600),(1920,1080)):
            name=f'probe-{width}x{height}'
            execute('xrandr','--newmode',name,'60',*[str(width)]*4,*[str(height)]*4)
            execute('xrandr','--addmode','VNC-0',name)
        phases = ('solid','image') if options.wallpaper_session is not None else ('solid',)
        for phase in phases:
            if phase == 'image':
                image = scratch/'wallpaper.img'
                run(['docker','cp',f'raven-{owner}-{options.wallpaper_session}:/home/raven/.config/raven/wallpaper.img',str(image)],environment)
                run([str(SOURCE/'raven'),'wallpaper','set','-t',session_id,str(image)],environment)
            sample(phase,0)
            for cycle in range(1,CYCLES+1):
                mode='probe-1920x1080' if cycle%2 else 'probe-800x600'
                execute('xrandr','--output','VNC-0','--mode',mode)
                time.sleep(0.15)
                if cycle%10 == 0:
                    sample(phase,cycle)
            time.sleep(3)
            sample(phase,'settled')
        print(f'Probe completed; artifacts: {scratch}',flush=True)
    finally:
        if session_id is not None:
            current = subprocess.run(['docker','inspect',f'raven-{owner}-{session_id}'],
                                     env=environment,capture_output=True,timeout=30)
            if current.returncode == 0 and json.loads(current.stdout)[0]['Id'] == generation:
                run([str(SOURCE/'raven'),'kill','-t',session_id],environment)
        if not had_hostd:
            run([str(SOURCE/'raven'),'hostd','stop'],environment)


if __name__ == '__main__':
    main()
