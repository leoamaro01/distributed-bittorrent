from concurrent.futures import ThreadPoolExecutor, as_completed, wait
import socket
from threading import Event, Lock, Thread
import threading
from typing import Callable
from tracker_chord import (
    ChordNode,
    ChordNodeReference,
    get_sha_for_bytes,
    inbetween,
)
from utils.torrent_requests import *
from utils.utils import *
import hashlib
import texts
import time
import random
from queue import SimpleQueue
from sys import argv


class Torrent:
    def __init__(
        self,
        torrent_id: str,
        peers: list[str],
        dirs: list[str],
        files: list[TorrentFileInfo],
        piece_size: int,
    ) -> None:
        self.torrent_id = torrent_id
        self.peers = peers
        self.dirs = dirs
        self.files = files
        self.piece_size = piece_size

    def to_bytes(self) -> bytes:
        id = self.torrent_id.encode()
        id_len = len(id).to_bytes(4)

        dirs_count = len(self.dirs).to_bytes(FILE_COUNT_B)
        dirs = b"".join(len(d).to_bytes(4) + d.encode() for d in self.dirs)

        files_count = len(self.files).to_bytes(FILE_COUNT_B)
        files = b""

        for file in self.files:
            name_bytes = file.file_name.encode()
            name_length = len(name_bytes).to_bytes(4)
            file_size = file.file_size.to_bytes(8)
            pieces_count = len(file.piece_hashes).to_bytes(4)
            hashes = b"".join(file.piece_hashes)

            files += name_length + name_bytes + file_size + pieces_count + hashes

        piece_size = self.piece_size.to_bytes(4)

        peers_count = len(self.peers).to_bytes(4)
        peers = b""

        for peer in self.peers:
            peers += ip_to_bytes(peer)

        return (
            id_len
            + id
            + peers_count
            + peers
            + piece_size
            + dirs_count
            + dirs
            + files_count
            + files
        )

    @staticmethod
    def from_bytes(data: bytes) -> "Torrent":
        if data == b"":
            return None

        data_index = 0

        def get_data(_bytes: int) -> bytes:
            nonlocal data_index
            ret = data[data_index : data_index + _bytes]
            data_index += _bytes
            return ret

        id_len = int.from_bytes(get_data(4))

        id = get_data(id_len).decode()

        peers_count = int.from_bytes(get_data(4))
        peers = []

        for _ in range(peers_count):
            peers.append(ip_from_bytes(get_data(4)))

        piece_size = int.from_bytes(get_data(4))

        dirs_count = int.from_bytes(get_data(FILE_COUNT_B))

        dirs = []
        for _ in range(dirs_count):
            dir_len = int.from_bytes(get_data(4))
            dirs.append(get_data(dir_len).decode())

        files_count = int.from_bytes(get_data(FILE_COUNT_B))

        files = []
        if files_count != 0:
            for _ in range(files_count):
                name_len = int.from_bytes(get_data(4))
                file_name = get_data(name_len).decode()

                file_size = int.from_bytes(get_data(8))
                pieces_count = int.from_bytes(get_data(PIECE_COUNT_B))

                hashes = []
                for _ in range(pieces_count):
                    hashes.append(get_data(32))

                files.append(TorrentFileInfo(file_name, file_size, hashes))

        return Torrent(id, peers, dirs, files, piece_size)


# region Constants

CLIENT_CHECKUP_TIMEOUT = 2
MAX_PEERS_TO_SEND = 50
DISCOVERY_TIMEOUT = 2
CHECKUP_TIME = 5

# endregion

# region Data

torrents: dict[str, Torrent] = {}
torrents_lock: Lock = Lock()

torrents_replica: dict[str, Torrent] = {}
torrents_replica_lock: Lock = Lock()

users_online: list[str] = []
users_online_lock: Lock = Lock()

users_online_replica: list[str] = []
users_online_replica_lock: Lock = Lock()

user_torrents: dict[str, list[str]] = {}
user_torrents_lock: Lock = Lock()

exit_event: Event = Event()

chord_node: ChordNode
chord_node_lock: Lock = Lock()

# endregion

# region Logging
log_queue: SimpleQueue = SimpleQueue()


def log(msg: str, category: str | None = None):
    time_str = time.strftime("%Y-%m-%d %H:%M:%S")
    category_str = f"({category}) " if category != None else ""
    log_queue.put(f"[{time_str}] {category_str}{msg}\n")


def logger():
    os.makedirs("logs", exist_ok=True)
    time_str = time.strftime("%Y-%m-%dT%H-%M-%S")
    log_file = path.join("logs", f"{time_str}.log")
    while True:
        item = log_queue.get()

        with open(log_file, "a") as f:
            f.write(item)


# endregion

# region Utilities


def to_torrent_info(torrent: Torrent):
    if torrent == None:
        return None

    info = TorrentInfo(
        torrent.torrent_id, torrent.dirs, torrent.files, torrent.piece_size
    )

    return info


# endregion

# region Client Checking


def check_client(ip: str) -> tuple[bool, str]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(CLIENT_CHECKUP_TIMEOUT)

        try:
            sock.connect((ip, SERVER_COMMS_PORT))
        except BaseException as e:
            log(f"Failed to connect to client. Error: {e}", f"CHECKUP {ip}")
            return False, ip

        try:
            sock.sendall(GetClientTorrentsRequest().to_bytes())
        except BaseException as e:
            log(f"Failed to send request to client. Error: {e}", f"CHECKUP {ip}")
            return False, ip

        try:
            res, res_type = TorrentRequest.recv_request(sock)
        except BaseException as e:
            log(f"Failed to receive response from client. Error: {e}", f"CHECKUP {ip}")
            return False, ip

    if res_type != RT_CLIENT_TORRENTS_RESPONSE:
        log(f"Client sent an unexpected response", f"CHECKUP")
        return False, ip

    res: ClientTorrentsResponse

    with user_torrents_lock:
        added_torrents: list[str] = []
        removed_torrents: list[str] = []

        if ip not in user_torrents:
            user_torrents[ip] = res.torrents
        else:
            old_torrents = user_torrents[ip]
            user_torrents[ip] = res.torrents

            for t in old_torrents:
                if t not in user_torrents[ip]:
                    removed_torrents.append(t)
            for t in user_torrents[ip]:
                if t not in old_torrents:
                    added_torrents.append(t)

    for t in added_torrents:
        store_key_on_chord(
            t.encode(),
            DT_TORRENT.to_bytes() + VT_ADD_PEER.to_bytes(),
            ip_to_bytes(ip),
        )
    for t in removed_torrents:
        store_key_on_chord(
            t.encode(),
            DT_TORRENT.to_bytes() + VT_REMOVE_PEER.to_bytes(),
            ip_to_bytes(ip),
        )

    return True, ip


def client_check_thread():
    while not exit_event.is_set():
        with users_online_lock:
            users = users_online.copy()

        if len(users) == 0:
            time.sleep(CHECKUP_TIME)
            continue

        exited = False

        with ThreadPoolExecutor() as executor:
            futures = [executor.submit(check_client, user) for user in users]

            for future in as_completed(futures):
                if future.cancelled():
                    continue

                try:
                    result = future.result()
                except BaseException as e:
                    log(f"Failed to check a client. Error: {e}", "CHECKUP")
                    continue

                is_online, user = result

                if not is_online:
                    store_key_on_chord(
                        ip_to_bytes(user),
                        DT_USER.to_bytes() + VT_USER_LOGOUT.to_bytes(),
                        b"",
                    )

                if not exited and exit_event.is_set():
                    exited = True
                    executor.shutdown(wait=False, cancel_futures=True)

        if exited:
            return

        time.sleep(CHECKUP_TIME)


# endregion

# region Requests


def handle_client(client: socket.socket, ip: str):
    with client:
        try:
            req, req_type = TorrentRequest.recv_request(client)
        except TorrentError as e:
            log(
                f"Failed to receive request from client. Error: {e.message}",
                f"REQUEST {ip}",
            )
            return

        if req_type == RT_GET_TORRENT:
            req: GetTorrentRequest

            log(f"Client requested torrent info for {req.torrent_id}", f"REQUEST {ip}")

            torrent = Torrent.from_bytes(
                retrieve_key_from_chord(
                    req.torrent_id.encode(),
                    DT_TORRENT.to_bytes() + VT_GET_TORRENT_INFO.to_bytes(),
                )
            )

            info = to_torrent_info(torrent)

            if info == None:
                log(f"Torrent {req.torrent_id} not found", f"REQUEST {ip}")
                info = TorrentInfo.invalid(req.torrent_id)

            try:
                client.sendall(TorrentInfoResponse(info).to_bytes())
            except BaseException as e:
                log(f"Failed to send response to client. Error: {e}", f"REQUEST {ip}")

            store_key_on_chord(
                ip_to_bytes(ip),
                DT_USER.to_bytes() + VT_USER_LOGIN.to_bytes(),
                b"",
            )
        elif req_type == RT_PING:
            return
        elif req_type == RT_LOGIN:
            log(f"Client logged in", f"REQUEST {ip}")
            store_key_on_chord(
                ip_to_bytes(ip),
                DT_USER.to_bytes() + VT_USER_LOGIN.to_bytes(),
                b"",
            )
        elif req_type == RT_LOGOUT:
            log(f"Client logged out", f"REQUEST {ip}")
            store_key_on_chord(
                ip_to_bytes(ip),
                DT_USER.to_bytes() + VT_USER_LOGOUT.to_bytes(),
                b"",
            )
        elif req_type == RT_UPLOAD_TORRENT:
            log(f"Client uploaded a torrent", f"REQUEST {ip}")
            req: UploadTorrentRequest

            if len(req.files) > 0:
                torrent_id = hashlib.sha256(
                    req.files[0].file_name.encode() + random.randbytes(32)
                ).hexdigest()
            else:
                torrent_id = hashlib.sha256(
                    req.dirs[0].encode() + random.randbytes(32)
                ).hexdigest()

            log(f"Torrent {torrent_id} saved", f"REQUEST {ip}")

            try:
                client.sendall(UploadTorrentResponse(torrent_id).to_bytes())
            except BaseException as e:
                log(f"Failed to send response to client. Error: {e}", f"REQUEST {ip}")
            else:
                torrent = Torrent(torrent_id, [ip], req.dirs, req.files, req.piece_size)

                store_key_on_chord(
                    torrent_id.encode(),
                    DT_TORRENT.to_bytes() + VT_NEW_TORRENT.to_bytes(),
                    torrent.to_bytes(),
                )

                store_key_on_chord(
                    ip_to_bytes(ip),
                    DT_USER.to_bytes() + VT_USER_LOGIN.to_bytes(),
                    b"",
                )
        elif req_type == RT_REGISTER_AS_PEER:
            log(f"Client registered as peer", f"REQUEST {ip}")
            req: RegisterAsPeerRequest

            store_key_on_chord(
                req.torrent_id.encode(),
                DT_TORRENT.to_bytes() + VT_ADD_PEER.to_bytes(),
                ip_to_bytes(ip),
            )

            store_key_on_chord(
                ip_to_bytes(ip),
                DT_USER.to_bytes() + VT_USER_LOGIN.to_bytes(),
                b"",
            )
        elif req_type == RT_GET_SERVERS:
            servers = []
            with chord_node_lock:
                if chord_node.pred != None:
                    servers.append(chord_node.pred.ip)
                with chord_node.successor_list_lock:
                    succs = chord_node.successor_list.copy()
                with chord_node.fingers_lock:
                    fingers = chord_node.finger.copy()

            for succ in succs:
                if succ.ip not in servers:
                    servers.append(succ.ip)

            for f in fingers:
                if f.ip not in servers:
                    servers.append(f.ip)
            try:
                client.sendall(ServersResponse(servers).to_bytes())
            except BaseException as e:
                log(f"Failed to send response to client. Error: {e}", f"REQUEST {ip}")
        elif req_type == RT_GET_PEERS:
            log(f"Client requested peers", f"REQUEST {ip}")
            req: GetPeersRequest

            peers_bytes = retrieve_key_from_chord(
                req.torrent_id.encode(), DT_TORRENT.to_bytes() + VT_GET_PEERS.to_bytes()
            )

            bytes_index = 0

            log(
                f"Requested peers for torrent {req.torrent_id}, received bytes {peers_bytes} from chord.",
                f"REQUEST {ip}",
            )

            def get_bytes(_bytes: int) -> bytes:
                nonlocal bytes_index
                ret = peers_bytes[bytes_index : bytes_index + _bytes]
                bytes_index += _bytes
                return ret

            if peers_bytes == b"":
                log(
                    "Its empty",
                    f"REQUEST {ip}",
                )
                peers = []
            else:
                peers_count = int.from_bytes(get_bytes(4))

                log(
                    f"Its {peers_count} peers.",
                    f"REQUEST {ip}",
                )

                peers = []
                for _ in range(peers_count):
                    p = ip_from_bytes(get_bytes(4))
                    peers.append(p)

            if ip in peers:
                log("Removing peer from ips", f"REQUEST {ip}")
                peers.remove(ip)

            try:
                log("Sending to peer", f"REQUEST {ip}")
                client.sendall(GetPeersResponse(peers).to_bytes())
            except BaseException as e:
                log(f"Failed to send response to client. Error: {e}", f"REQUEST {ip}")

            store_key_on_chord(
                ip_to_bytes(ip), DT_USER.to_bytes() + VT_USER_LOGIN.to_bytes(), b""
            )
        elif req_type == RT_GET_ALL_TORRENTS:
            log("Client requested all torrents", f"REQUEST {ip}")

            with chord_node_lock:
                log(
                    "Starting chain request on chord for all torrents...",
                    f"REQUEST {ip}",
                )
                all_torrents_bytes = chord_node.get_all_data(DT_TORRENT.to_bytes(1))

            bytes_read = 0

            def read_data(count: int) -> bytes:
                nonlocal bytes_read
                data = all_torrents_bytes[bytes_read : bytes_read + count]
                bytes_read += count
                return data

            count = int.from_bytes(read_data(4))

            torrent_ids_names: list[tuple[str, str]] = []

            for _ in range(count):
                id_len = int.from_bytes(read_data(4))
                id = read_data(id_len).decode()
                name_len = int.from_bytes(read_data(4))
                name = read_data(name_len).decode()

                if (id, name) not in torrent_ids_names:
                    torrent_ids_names.append((id, name))

            try:
                log("Sending to peer", f"REQUEST {ip}")
                client.sendall(AllTorrentsResponse(torrent_ids_names).to_bytes())
            except BaseException as e:
                log(f"Failed to send response to client. Error: {e}", f"REQUEST {ip}")

            store_key_on_chord(
                ip_to_bytes(ip), DT_USER.to_bytes() + VT_USER_LOGIN.to_bytes(), b""
            )

    log(f"Client disconnected", f"REQUEST {ip}")


def server_requests_thread():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    sock.bind(("", SERVER_PORT))
    sock.listen(5)

    sock.settimeout(5)

    while not exit_event.is_set():
        try:
            client, addr = sock.accept()
        except TimeoutError:
            continue

        client_thread = Thread(
            target=handle_client, args=[client, addr[0]], daemon=False
        )
        client_thread.start()


# endregion

# region Commands


def print_help(args: list[str]):
    if len(args) != 0 and args[0] != "help" and args[0] in commands:
        commands[args[0]](["help"])
        return

    print(texts.help_text)


def chord_data(args: list[str]):
    with chord_node_lock:
        print(str(chord_node))


def delete_torrent(args: list[str]):
    if len(args) == 0:
        print(
            "Wrong number of arguments in 'delete' command, use 'delete help' for usage info."
        )
        return

    if args[0] == "help":
        print(texts.delete_help)
        return

    store_key_on_chord(
        args[0].encode(), DT_TORRENT.to_bytes() + VT_REMOVE_TORRENT.to_bytes(), b""
    )

    print("Torrent deleted.")


def exit_command(args: list[str]):
    if len(args) > 0 and args[0] == "help":
        print("Exits the server.")
        return

    exit_event.set()

    print("Exiting...")
    exit(0)


def list_peers(args: list[str]):
    if len(args) > 0 and args[0] == "help":
        print(texts.list_peers_help)
        return

    print("Own users:")
    with users_online_lock:
        for i, peer in enumerate(users_online):
            print(peer, end="\n" if (i + 1) % 5 == 0 else "\t")

    print()

    print("Replica users:")
    with users_online_replica_lock:
        for i, peer in enumerate(users_online_replica):
            print(peer, end="\n" if (i + 1) % 5 == 0 else "\t")

    print("")


def list_torrents(args: list[str]):
    if len(args) > 0 and args[0] == "help":
        print(texts.list_torrents_help)
        return

    print("Own torrents:")
    with torrents_lock:
        print("\n".join(torrents.keys()))

    print()

    print("Replica torrents:")
    with torrents_replica_lock:
        print("\n".join(torrents_replica.keys()))


def info_command(args: list[str]):
    if len(args) == 0:
        print(
            "Wrong number of arguments in 'info' command, use 'info help' for usage info."
        )
        return

    if args[0] == "help":
        print(texts.info_help)
        return

    torrent: Torrent = Torrent.from_bytes(
        retrieve_key_from_chord(
            args[0].encode(), DT_TORRENT.to_bytes() + VT_GET_TORRENT_INFO.to_bytes()
        )
    )

    if torrent == None:
        print(
            f"Torrent {args[0]} not found in chord. Try 'list-torrents' for available (local) torrents."
        )
        return

    print(str(to_torrent_info(torrent)))
    with torrents_lock:
        peers_str = "\n".join(torrent.peers)

    print(f"Peers:\n{peers_str}")


commands: dict[str, Callable] = {
    "help": print_help,
    "delete": delete_torrent,
    "exit": exit_command,
    "list-peers": list_peers,
    "list-torrents": list_torrents,
    "info": info_command,
    "chord": chord_data,
}

# endregion

# region Chord Handling

# Data types:
DT_TORRENT = 0
DT_USER = 1

# Store Value types:
VT_ADD_PEER = 0
VT_REMOVE_PEER = 1
VT_NEW_TORRENT = 2
VT_REMOVE_TORRENT = 3
VT_USER_LOGOUT = 4
VT_USER_LOGIN = 5
VT_USER_TRANSFER = 6

# Retrieve Value types:
VT_GET_TORRENT_INFO = 0
VT_GET_PEERS = 1


def get_all_data(key_options: bytes) -> bytes:
    data_type = int.from_bytes(key_options[:1])

    if data_type == DT_TORRENT:
        with torrents_lock:
            torrents_cp = torrents.copy()

        count = len(torrents_cp)

        torr_data = count.to_bytes(4)

        for torr in torrents_cp.values():
            torr_data += len(torr.torrent_id).to_bytes(4)
            torr_data += torr.torrent_id.encode()

            name = (
                torr.dirs[0].split("/")[0]
                if len(torr.dirs) > 0
                else torr.files[0].file_name.split("/")[0]
            )

            torr_data += len(name).to_bytes(4)
            torr_data += name.encode()

        log(f"All data bytes: {torr_data}", "ALL DATA")

        return torr_data
    elif data_type == DT_USER:
        with users_online_lock:
            users = users_online.copy()

        count = len(users)

        users_data = count.to_bytes(4)

        for user in users:
            users_data += ip_to_bytes(user)

        return users_data


def store_key_on_chord(
    key_bytes: bytes, key_options: bytes, value_bytes: bytes
) -> None:
    log("Sent to store key on chord", "CHORD STORE")
    with chord_node_lock:
        succ: ChordNodeReference = chord_node.find_succ(get_sha_for_bytes(key_bytes))

    log(f"Found successor at {succ.ip}", "CHORD STORE")

    while not succ.store_key(key_bytes, key_options, value_bytes):
        log(f"Failed to store, trying again", "CHORD STORE")
        succ = chord_node.find_succ(get_sha_for_bytes(key_bytes))
        log(f"Found successor at {succ.ip}", "CHORD STORE")


def retrieve_key_from_chord(key_bytes: bytes, key_options: bytes) -> bytes:
    log("Retrieving a key", "CHORD RETRIEVE")
    with chord_node_lock:
        succ: ChordNodeReference = chord_node.find_succ(get_sha_for_bytes(key_bytes))

    log(f"Found successor at {succ.ip}", "CHORD RETRIEVE")

    value = succ.retrieve_key(key_bytes, key_options)
    while value == None:
        log(f"Failed to retrieve, trying again", "CHORD RETRIEVE")
        succ: ChordNodeReference = chord_node.find_succ(get_sha_for_bytes(key_bytes))
        log(f"Found successor at {succ.ip}", "CHORD RETRIEVE")
        value = succ.retrieve_key(key_bytes, key_options)
    return value


def store_data_internal(
    key_bytes: bytes,
    key_options: bytes,
    value_bytes: bytes,
    torrents: dict[str, Torrent],
    torrents_lock: Lock,
    users_online: list[str],
    users_online_lock: Lock,
) -> None:
    log("Told to store data", "CHORD")
    data_type = int.from_bytes(key_options[:1])
    value_type = int.from_bytes(key_options[1:2])

    if data_type == DT_TORRENT:
        log("Storing a torrent", "CHORD")
        torrent_id = key_bytes.decode()

        if value_type == VT_ADD_PEER:
            log("Adding a peer", "CHORD")
            peer = ip_from_bytes(value_bytes)

            with torrents_lock:
                if torrent_id in torrents and peer not in torrents[torrent_id].peers:
                    torrents[torrent_id].peers.append(peer)
        elif value_type == VT_REMOVE_PEER:
            log("Removing a peer", "CHORD")
            peer = ip_from_bytes(value_bytes)

            with torrents_lock:
                if torrent_id in torrents and peer in torrents[torrent_id].peers:
                    torrents[torrent_id].peers.remove(peer)
        elif value_type == VT_NEW_TORRENT:
            log("Adding a torrent", "CHORD")
            torrent = Torrent.from_bytes(value_bytes)

            with torrents_lock:
                torrents[torrent_id] = Torrent(
                    torrent_id,
                    torrent.peers,
                    torrent.dirs,
                    torrent.files,
                    torrent.piece_size,
                )
        elif value_type == VT_REMOVE_TORRENT:
            log("Removing a torrent", "CHORD")
            with torrents_lock:
                if torrent_id in torrents:
                    torrents.pop(torrent_id)
    elif data_type == DT_USER:
        log("Storing a user", "CHORD")
        user = ip_from_bytes(key_bytes)

        if value_type == VT_USER_LOGIN:
            log("User login", "CHORD")
            added = False
            with users_online_lock:
                if user not in users_online:
                    users_online.append(user)
                    added = True
            if added:
                check_client(user)
        elif value_type == VT_USER_LOGOUT:
            log("User logout", "CHORD")
            with users_online_lock:
                if user in users_online:
                    users_online.remove(user)

            with user_torrents_lock:
                torrents_from_user = (
                    user_torrents[user] if user in user_torrents else []
                )

            for torrent in torrents_from_user:
                store_key_on_chord(
                    torrent.encode(),
                    DT_TORRENT.to_bytes() + VT_REMOVE_PEER.to_bytes(),
                    key_bytes,
                )
        elif value_type == VT_USER_TRANSFER:
            log("User transferred", "CHORD")
            with user_torrents_lock:
                with users_online_lock:
                    bytes_read = 0

                    def read_value(_bytes: int):
                        nonlocal bytes_read
                        data = value_bytes[bytes_read : bytes_read + _bytes]
                        bytes_read += _bytes
                        return data

                    torrents_count = int.from_bytes(read_value(4))

                    torrs = []

                    for _ in range(torrents_count):
                        length = read_value(1)
                        torrs.append(read_value(length).decode())

                    if user not in users_online:
                        users_online.append(user)

                    if user in user_torrents:
                        user_torrents[user].extend(torrs)
                    else:
                        user_torrents[user] = torrs

            check_client(user)


def store_data(key_bytes: bytes, key_options: bytes, value_bytes: bytes) -> None:
    store_data_internal(
        key_bytes,
        key_options,
        value_bytes,
        torrents,
        torrents_lock,
        users_online,
        users_online_lock,
    )


def store_replica(key_bytes: bytes, key_options: bytes, value_bytes: bytes) -> None:
    store_data_internal(
        key_bytes,
        key_options,
        value_bytes,
        torrents_replica,
        torrents_replica_lock,
        users_online_replica,
        users_online_replica_lock,
    )


def try_retrieve_data_internal(
    key_bytes: bytes,
    key_options: bytes,
    torrents: dict[str, Torrent],
    torrents_lock: Lock,
    users_online: list[str],
    users_online_lock: Lock,
) -> bytes | None:
    data_type = int.from_bytes(key_options[:1])
    value_type = int.from_bytes(key_options[1:2])

    if data_type == DT_TORRENT:
        log("Retrieving a torrent", "CHORD")
        torrent_id = key_bytes.decode()

        if value_type == VT_GET_TORRENT_INFO:
            log("Retrieving a torrent info", "CHORD")
            with torrents_lock:
                torrent = torrents.get(torrent_id, None)
                if torrent == None:
                    return None
                return torrent.to_bytes()
        elif value_type == VT_GET_PEERS:
            log("Retrieving torrent peers", "CHORD")
            with torrents_lock:
                torrent = torrents.get(torrent_id, None)
                if torrent == None:
                    return None

                if len(torrent.peers) == 0:
                    return b""

                peers = torrent.peers.copy()
                if len(peers) > MAX_PEERS_TO_SEND + 1:
                    peers = peers[: MAX_PEERS_TO_SEND + 1]

                random.shuffle(peers)

                return len(peers).to_bytes(4) + b"".join(
                    ip_to_bytes(ip) for ip in peers
                )


def retrieve_data(key_bytes: bytes, key_options: bytes) -> bytes:
    data = try_retrieve_data_internal(
        key_bytes, key_options, torrents, torrents_lock, users_online, users_online_lock
    )

    if data != None:
        return data

    data = try_retrieve_data_internal(
        key_bytes,
        key_options,
        torrents_replica,
        torrents_replica_lock,
        users_online_replica,
        users_online_replica_lock,
    )

    return data if data != None else b""


def own_replica_and_forward(key: str, is_torrent: bool):
    if is_torrent:
        with torrents_lock:
            with torrents_replica_lock:
                torrents[key] = torrents_replica[key]
                torrent_bytes = torrents[key].to_bytes()
                torrents_replica.pop(key)
        with chord_node_lock:
            with chord_node.successor_list_lock:
                successors = chord_node.successor_list.copy()
        for succ in successors:
            replicate_torrent(key, torrent_bytes, succ)
    else:
        with users_online_lock:
            with users_online_replica_lock:
                users_online.append(key)
                users_online_replica.remove(key)
        with chord_node_lock:
            with chord_node.successor_list_lock:
                successors = chord_node.successor_list.copy()
        for succ in successors:
            replicate_user(key, succ)


def own_replica(data_start: int, data_end: int):
    with torrents_replica_lock:
        replicas = torrents_replica.copy()
    for torrent in replicas:
        key_hash = get_sha_for_bytes(torrent.encode())
        if inbetween(key_hash, data_start, data_end):
            own_replica_and_forward(torrent, True)
    with users_online_replica_lock:
        replicas = users_online_replica.copy()
    for user in replicas:
        ip_hash = get_sha_for_bytes(ip_to_bytes(user))
        if inbetween(ip_hash, data_start, data_end):
            own_replica_and_forward(user, False)


def delete_replica(data_start: int, data_end: int):
    with torrents_replica_lock:
        replicas = torrents_replica.copy()
        for torrent in replicas:
            key_hash = get_sha_for_bytes(torrent.encode())
            if inbetween(key_hash, data_start, data_end):
                torrents_replica.pop(torrent)
    with users_online_replica_lock:
        replicas = users_online_replica.copy()
        for user in replicas:
            ip_hash = get_sha_for_bytes(ip_to_bytes(user))
            if inbetween(ip_hash, data_start, data_end):
                users_online_replica.remove(user)


def transfer_torrent(
    torrent_id: str, torrent_bytes, target: ChordNodeReference
) -> tuple[int, str] | None:
    if target.store_key(
        torrent_id.encode(),
        DT_TORRENT.to_bytes() + VT_NEW_TORRENT.to_bytes(),
        torrent_bytes,
    ):
        return DT_TORRENT, torrent_id
    return None


def transfer_user(user_ip: str, target: ChordNodeReference) -> tuple[int, str] | None:
    user_torrents_bytes = b""

    with user_torrents_lock:
        torr_count = len(user_torrents[user_ip]) if user_ip in user_torrents else 0

        user_torrents_bytes += torr_count.to_bytes()

        if torr_count > 0:
            for torr in user_torrents[user_ip]:
                user_torrents_bytes += len(torr).to_bytes(1)
                user_torrents_bytes += torr.encode()

    if target.store_key(
        ip_to_bytes(user_ip),
        DT_USER.to_bytes() + VT_USER_TRANSFER.to_bytes(),
        user_torrents_bytes,
    ):
        return DT_USER, user_ip
    return None


def transfer_data(data_start: int, data_end: int, target: ChordNodeReference):
    with ThreadPoolExecutor() as executor:
        futures = []
        with torrents_lock:
            torrents_cp = torrents.copy()

        for torrent in torrents_cp:
            key_hash = get_sha_for_bytes(torrent.encode())
            if inbetween(key_hash, data_start, data_end):
                futures.append(
                    executor.submit(
                        transfer_torrent,
                        torrent,
                        torrents[torrent].to_bytes(),
                        target,
                    )
                )

        with users_online_lock:
            users = users_online.copy()

        for user in users:
            ip_hash = get_sha_for_bytes(ip_to_bytes(user))
            if inbetween(ip_hash, data_start, data_end):
                futures.append(executor.submit(transfer_user, user, target))

        for future in as_completed(futures):
            if future.cancelled():
                continue

            result = future.result()

            if result == None:
                continue

            data_type, key = result

            if data_type == DT_TORRENT:
                with torrents_lock:
                    if key in torrents:
                        torrents.pop(key)
            elif data_type == DT_USER:
                with users_online_lock:
                    if key in users_online:
                        users_online.remove(key)


def replicate_torrent(
    torrent_id: str, torrent_bytes: bytes, successor: ChordNodeReference
):
    successor.store_key_replica(
        torrent_id.encode(),
        DT_TORRENT.to_bytes() + VT_NEW_TORRENT.to_bytes(),
        torrent_bytes,
    )


def replicate_user(user_ip: str, successor: ChordNodeReference):
    successor.store_key_replica(
        ip_to_bytes(user_ip),
        DT_USER.to_bytes() + VT_USER_LOGIN.to_bytes(),
        b"",
    )


def replicate(successor: ChordNodeReference):
    with ThreadPoolExecutor() as executor:
        futures = []

        with torrents_lock:
            for torrent in torrents:
                futures.append(
                    executor.submit(
                        replicate_torrent,
                        torrent,
                        torrents[torrent].to_bytes(),
                        successor,
                    )
                )

        with users_online_lock:
            for user in users_online:
                futures.append(executor.submit(replicate_user, user, successor))

        wait(futures)


def update_successors(
    new_successors: list[ChordNodeReference],
    removed_successors: list[ChordNodeReference],
):
    with chord_node_lock:
        for s in removed_successors:
            s.remove_replica(chord_node.start_id, chord_node.id)

    with ThreadPoolExecutor() as executor:
        futures = []

        for s in new_successors:
            futures.append(executor.submit(replicate, s))

        wait(futures)


# endregion

# region Multicast Receiving


def receive_multicast_request(data: bytes, ip: str):
    if len(data) < 10:
        return

    id = data[:6].decode()

    if id not in ["client", "server"]:
        return

    if ip_from_bytes(data[6:10]) != ip:
        return

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        my_ip = socket.gethostbyname(socket.gethostname())

        try:
            sock.connect(
                (ip, SERVER_DISCOVERY_PORT if id == "server" else CLIENT_DISCOVERY_PORT)
            )

            sock.sendall(b"discovery" + ip_to_bytes(my_ip))
        except:
            return


def multicast_receiver():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # Set the socket options to enable multicast
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    # Specify the multicast group and port
    multicast_group = "224.0.0.10"
    port = SERVER_MULTICAST_PORT

    # Bind the socket to the multicast group and port
    sock.bind(("", port))

    # Join the multicast group
    sock.setsockopt(
        socket.IPPROTO_IP,
        socket.IP_ADD_MEMBERSHIP,
        socket.inet_aton(multicast_group) + socket.inet_aton("0.0.0.0"),
    )

    # Receive and print incoming multicast messages
    while True:
        data, address = sock.recvfrom(10)

        threading.Thread(
            target=receive_multicast_request, args=[data, address[0]]
        ).start()


# endregion


def main():
    global chord_node
    print("Welcome to CDL-BitTorrent Server!")
    print("Starting up...")

    logger_thread = Thread(target=logger, daemon=True)
    logger_thread.start()

    ip = socket.gethostbyname(socket.gethostname())

    chord_node = ChordNode(
        ip=ip,
        store_data_func=store_data,
        delete_replica_func=delete_replica,
        own_replica_func=own_replica,
        retrieve_data_func=retrieve_data,
        store_replica_func=store_replica,
        transfer_data_func=transfer_data,
        update_succs_func=update_successors,
        get_all_data_func=get_all_data,
        log=log,
    )

    direct = False

    if "--direct" in argv:
        server_index = argv.index("--direct") + 1
        if len(argv) <= server_index:
            print(
                "If you are using the --direct option you must also specify a server address."
            )
        else:
            server_ip = argv[server_index]

            try:
                chord_node.join(ChordNodeReference(server_ip))
            except RuntimeError:
                print(
                    "Failed to connect to server specified with the --direct option. Will attempt discovery next..."
                )
            else:
                direct = True

    if "--nodiscovery" not in argv and not direct:
        servers = []

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            multicast_group = "224.0.0.10"
            port = SERVER_MULTICAST_PORT

            discovery_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            discovery_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            discovery_socket.bind(("", SERVER_DISCOVERY_PORT))
            discovery_socket.settimeout(3)

            discovery_socket.listen(10)

            # send discovery message
            sock.sendto(b"server" + ip_to_bytes(ip), (multicast_group, port))
            sock.close()

            # wait for other servers to respond
            start_wait_time = time.time()

            while time.time() - start_wait_time < DISCOVERY_TIMEOUT:
                try:
                    conn, addr = discovery_socket.accept()
                except TimeoutError:
                    continue

                try:
                    # should receive "discovery" + the ip, so 13 bytes
                    data = conn.recv(13)

                    if len(data) < 13:
                        continue

                    if data[:9].decode() != "discovery":
                        continue

                    if ip_from_bytes(data[9:13]) != addr[0]:
                        continue

                    servers.append(addr[0])

                    conn.close()
                except:
                    continue
        except BaseException as e:
            print(f"Error at discovery: {e}")

        if len(servers) == 0:
            log("Failed to find any servers through discovery", "DISCOVERY")
            print("Failed to find any servers through discovery")
        else:
            joined = False
            for server in servers:
                try:
                    chord_node.join(ChordNodeReference(server))
                except RuntimeError:
                    continue
                else:
                    joined = True
                    break
            if not joined:
                log("Failed to join any server through discovery", "DISCOVERY")
                print("Failed to join any server through discovery")

    multicast_thread = Thread(target=multicast_receiver, daemon=True)
    multicast_thread.start()

    check_thread = Thread(target=client_check_thread, daemon=True)
    check_thread.start()

    requests_thread = Thread(target=server_requests_thread, daemon=True)
    requests_thread.start()

    print("Done!")

    print(
        "This is the CDL-BitTorrent Server CLI, use help to learn the available commands."
    )

    while True:
        inp: str = input("$> ").strip()

        if len(inp) == 0:
            continue

        [command, *args] = split_command(inp)

        if command not in commands:
            print(
                f"Unknown command '{command}', use 'help' to see the available commands."
            )
            continue

        commands[command](args)


if __name__ == "__main__":
    main()
