#!/usr/bin/env python
import os
import sys
import socket
def get_ip_address():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 80))
    return s.getsockname()[0]
    
if __name__ == "__main__":
    #ip = get_ip_address()
    #with open("/home/cavenel/Dropbox/mahjong_compet/ip.txt",'w') as f:
    #    f.write(ip)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "apps.settings.dev")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)
