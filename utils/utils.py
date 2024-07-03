import socket

class TorrentError(Exception):
    def __init__(self, message: str, *args: object) -> None:
        super().__init__(message, *args)
        self.message = message

def recv_all(sock: socket.socket, expected: int):
    chunks = []
    bytes_recd = 0
    while bytes_recd < expected:
        chunk = sock.recv(expected - bytes_recd)
        if chunk == b'':
            raise RuntimeError("Socket connection broken before receiving expected bytes")
        chunks.append(chunk)
        bytes_recd += len(chunk)
    return b''.join(chunks)

def split_command(command: str) -> list[str]:
    result: list[str] = []
    current_start = 0
    
    scope_char = None
    
    for i in range(len(command)):
        if scope_char != None:
            if command[i] == scope_char:
                scope_char = None

                if i == current_start:
                    current_start += 1
                    continue
                
                result.append(command[current_start:i])
                current_start = i + 1
            continue
            
        if command[i].isspace():
            if i == current_start:
                current_start += 1
                continue
            
            result.apend(command[current_start:i])
            current_start = i + 1
            continue
        
        if command[i] in ["'", '"']:
            scope_char = command[i]
            current_start = i + 1
            continue
    
    if current_start != len(command):
        result.append(command[current_start:])
    
    return result

def get_hex(n: int) -> str:
    return hex(n)[2:]
