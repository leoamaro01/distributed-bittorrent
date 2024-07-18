import socket
import os
import pickle
from os import path
from typing import Any, Callable

SERVER_PORT = 8080
SERVER_COMMS_PORT = 7011

# region Standards
# standard piece size: 256kB
PIECE_SIZE = 256 * 1024
# max file size: ~1PB
# peers count: max 2^32 (4 bytes)
PEERS_B = 4
# piece count per file: max 2^32 (4 bytes)
PIECE_COUNT_B = 4
# file count: max 2^64 (8 bytes)
FILE_COUNT_B = 8
# endregion


class TorrentError(Exception):
    def __init__(self, message: str, *args: object) -> None:
        super().__init__(message, *args)
        self.message = message


def recv_all(
    sock: socket.socket,
    expected: int,
    on_progress: Callable = None,
    on_progres_args: list = None,
):
    chunks = []
    bytes_recd = 0
    while bytes_recd < expected:
        if on_progress != None:
            if on_progres_args != None:
                on_progress(*on_progres_args, float(bytes_recd) / expected)
            else:
                on_progress(float(bytes_recd) / expected)

        chunk = sock.recv(expected - bytes_recd)

        if chunk == b"":
            raise RuntimeError(
                "Socket connection broken before receiving expected bytes"
            )

        chunks.append(chunk)
        bytes_recd += len(chunk)
    return b"".join(chunks)


def ip_to_bytes(ip: str) -> bytes:
    return b"".join(int(section).to_bytes() for section in ip.split("."))


def ip_from_bytes(ip_bytes: bytes) -> str:
    return ".".join(str(b) for b in ip_bytes)


def load_config_file(file_name: str) -> Any | None:
    config_path = path.join("config", file_name)

    try:
        with open(config_path, "rb") as f:
            return pickle.load(f)
    except:
        return None


def update_config_file(file_name: str, config_obj: Any):
    os.makedirs("config", exist_ok=True)

    with open(path.join("config", file_name), "wb") as f:
        pickle.dump(config_obj, f)


def clear_console():
    if os.name == "nt":
        os.system("cls")
    else:
        if "TERM" not in os.environ:
            os.environ["TERM"] = "xterm"
        os.system("clear")


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

            result.append(command[current_start:i])
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
