"""systemd notification from the actual progress loop, never a detached heartbeat."""
import os
import socket


def upgrade_unit(unit, previous_release, release, base):
    """Replace only execution health directives in the existing C3 unit."""
    if unit.count('WorkingDirectory='+previous_release+'\n') != 1 or '-m track_c.c3_runner --config '+base+'/config.json' not in unit:
        raise ValueError('unexpected C3 service command or working directory')
    settings = dict(Type='notify', NotifyAccess='main', WatchdogSec='20s',
                    TimeoutStartSec='120s', TimeoutAbortSec='5s',
                    ExecStopPost=base+'/.venv/bin/python -m track_c.recovery --config '+base+'/config.json --seconds 25')
    rows=[]; section=None
    for row in unit.splitlines():
        if row.startswith('['):
            section=row
            rows.append(row)
            if section=='[Service]': rows.extend(k+'='+v for k,v in settings.items())
        elif section=='[Service]' and row.partition('=')[0] in settings:
            continue
        else:
            rows.append('WorkingDirectory='+release if row=='WorkingDirectory='+previous_release else row)
    return '\n'.join(rows)+'\n'


def notify(message):
    address = os.environ.get('NOTIFY_SOCKET')
    if not address: return False
    if address.startswith('@'): address='\0'+address[1:]
    try:
        with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as channel:
            channel.connect(address)
            channel.sendall(message.encode())
        return True
    except OSError:
        return False
