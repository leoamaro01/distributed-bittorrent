from socket import socket

class TorrentError(Exception):
    def __init__(self, message: str, *args: object) -> None:
        super().__init__(message, *args)
        self.message = message

def recv_all(socket: socket, expected: int):
    chunks = []
    bytes_recd = 0
    while bytes_recd < expected:
        chunk = socket.recv(expected - bytes_recd)
        if chunk == b'':
            raise RuntimeError("Socket connection broken before receiving expected bytes")
        chunks.append(chunk)
        bytes_recd += len(chunk)
    return b''.join(chunks)