import socket
from threading import Thread

from utils.utils import ip_from_bytes, ip_to_bytes

ip = "1.2.3.4"
btes = ip_to_bytes(ip)
print(btes)
print(ip_from_bytes(btes))
