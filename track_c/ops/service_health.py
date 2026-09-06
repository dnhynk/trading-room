"""systemd notification from the actual progress loop, never a detached heartbeat."""
import os
import socket




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
