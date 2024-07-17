from concurrent.futures import ThreadPoolExecutor, as_completed
import socket
from threading import Event, Lock, Thread
from typing import Callable
from utils.torrent_requests import *
from utils.utils import *
import hashlib
import texts
import time
import random
from queue import SimpleQueue

# region Constants

MAX_PEERS_TO_SEND = 50

CHECKUP_TIME = 5 * 60

# endregion

class Torrent:
    def __init__(self, torrent_id: str, peers: list[str], dirs: list[str], files: list[TorrentFileInfo], piece_size: int) -> None:
        self.torrent_id = torrent_id
        self.peers = peers
        self.dirs = dirs
        self.files = files
        self.piece_size = piece_size

# region Data

torrents: dict[str, Torrent] = {}
torrents_lock: Lock = Lock()

users_online: list[str] = []
users_online_lock: Lock = Lock()

user_torrents: dict[str, list[str]] = {}
user_torrents_lock: Lock = Lock()

exit_event: Event = Event()

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

def login_user_if_not_online(ip: str) -> bool:
    users_online_lock.acquire()
    if ip not in users_online:
        users_online.append(ip)
        users_online_lock.release()
        check_client()
        return True
    else:
        users_online_lock.release()
        return False

def get_torrent_info(torrent_id: str):
    with torrents_lock:
        torrent = torrents.get(torrent_id, None)
    
    if torrent == None:
        return None        
    
    info = TorrentInfo(torrent.torrent_id, torrent.dirs, torrent.files, torrent.piece_size)            
    
    return info

def erase_client_data(user: str):
    with user_torrents_lock:
        if user in user_torrents:
            with torrents_lock:
                for torrent in user_torrents[user]:
                    if torrent in torrents and user in torrents[torrent].peers:
                        torrents[torrent].peers.remove(user)
            
            user_torrents.pop(user)

# endregion

# region Client Checking

def check_client(ip: str) -> tuple[bool, str]:    
    erase_client_data(ip)
    
    log("Starting client checkup", f"CHECKUP {ip}")
    
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
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
        user_torrents[ip] = res.torrents

    with torrents_lock:
        for torrent in res.torrents:
            if torrent in torrents and ip not in torrents[torrent].peers:
                torrents[torrent].peers.append(ip)
    
    log("Client checkup finished", f"CHECKUP {ip}")
                
def client_check_thread():
    while not exit_event.is_set():
        time.sleep(CHECKUP_TIME)
        
        log("Starting client checkup...", "CHECKUP")
        
        with users_online_lock:
            users = users_online.copy()
        
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
                    with users_online_lock:
                        if user in users_online:
                            users_online.remove(user)
                
                if not exited and exit_event.is_set():
                    exited = True
                    executor.shutdown(wait=False, cancel_futures=True)
 
# endregion
            
# region Requests

def handle_client(client: socket.socket, ip: str):
    with client:
        try:
            req, req_type = TorrentRequest.recv_request(client)
        except TorrentError as e:
            log(f"Failed to receive request from client. Error: {e.message}", f"REQUEST {ip}")
            return
        
        if req_type == RT_GET_TORRENT:
            req: GetTorrentRequest
            
            log(f"Client requested torrent info for {req.torrent_id}", f"REQUEST {ip}")
            
            info = get_torrent_info(req.torrent_id)
            
            if info == None:
                log(f"Torrent {req.torrent_id} not found", f"REQUEST {ip}")
                info = TorrentInfo(req.torrent_id, [], [], 0)
            
            try:
                client.sendall(TorrentInfoResponse(info).to_bytes())
            except BaseException as e:
                log(f"Failed to send response to client. Error: {e}", f"REQUEST {ip}")
            
            login_user_if_not_online(ip)
        elif req_type == RT_PING:
            return
        elif req_type == RT_LOGIN:
            log(f"Client logged in", f"REQUEST {ip}")
            with users_online_lock:
                if ip not in users_online:
                    users_online.append(ip)
            
            check_client(ip)
        elif req_type == RT_LOGOUT:
            log(f"Client logged out", f"REQUEST {ip}")
            with users_online_lock:
                if ip in users_online:
                    users_online.remove(ip)
                    
            erase_client_data(ip)                      
        elif req_type == RT_UPLOAD_TORRENT:
            log(f"Client uploaded a torrent", f"REQUEST {ip}")
            req: UploadTorrentRequest
            
            torrent_id = hashlib.sha256(req.files[0].file_name.encode()).hexdigest()
            
            with torrents_lock:
                while torrent_id in torrents:
                    torrent_id = hashlib.sha256(torrent_id.encode()).hexdigest()
            
            log(f"Torrent {torrent_id} saved", f"REQUEST {ip}")
            
            try:
                client.sendall(UploadTorrentResponse(torrent_id).to_bytes())
            except BaseException as e:
                log(f"Failed to send response to client. Error: {e}", f"REQUEST {ip}")
            else:
                torrent = Torrent(torrent_id, [ip], req.dirs, req.files, req.piece_size)            
                
                with torrents_lock:
                    torrents[torrent_id] = torrent
                
                if not login_user_if_not_online(ip):
                    with user_torrents_lock:
                        if ip in user_torrents:
                            user_torrents[ip].append(torrent_id)
                        else:
                            user_torrents[ip] = [torrent_id]
        elif req_type == RT_REGISTER_AS_PEER:
            log(f"Client registered as peer", f"REQUEST {ip}")
            req: RegisterAsPeerRequest
            
            with torrents_lock:
                if req.torrent_id not in torrents:
                    return
                
                if ip not in torrents[req.torrent_id].peers:
                    torrents[req.torrent_id].peers.append(ip)
            
            if not login_user_if_not_online(ip):
                with user_torrents_lock:
                    if ip in user_torrents:
                        if req.torrent_id not in user_torrents[ip]:
                            user_torrents[ip].append(req.torrent_id)
                    else:
                        user_torrents[ip] = [req.torrent_id]
        elif req_type == RT_GET_PEERS:
            log(f"Client requested peers", f"REQUEST {ip}")
            req: GetPeersRequest
            
            with torrents_lock:
                if req.torrent_id not in torrents:
                    peers = []
                
                peers = torrents[req.torrent_id].peers
            
            if ip in peers:
                peers.remove(ip)
            
            random.shuffle(peers)
            
            if len(peers) > MAX_PEERS_TO_SEND:
                peers = peers[:MAX_PEERS_TO_SEND]
            
            try:
                client.sendall(GetPeersResponse(peers).to_bytes())
            except BaseException as e:
                log(f"Failed to send response to client. Error: {e}", f"REQUEST {ip}")
            
            login_user_if_not_online(ip)
            
    log(f"Client disconnected", f"REQUEST {ip}")

def server_requests_tread():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    sock.bind(('', SERVER_PORT))
    sock.listen(5)

    sock.settimeout(5)

    while not exit_event.is_set():
        try:
            client, addr = sock.accept()
        except TimeoutError:
            continue
        
        client_thread = Thread(target=handle_client, args=[client, addr[0]], daemon=False)
        client_thread.start()

# endregion

# region Commands

def print_help(args: list[str]):
    if len(args) != 0 and args[0] != "help" and args[0] in commands:
        commands[args[0]](["help"])
        return
    
    print(texts.help_text)

def delete_torrent(args: list[str]):
    if len(args) == 0:
        print("Wrong number of arguments in 'delete' command, use 'delete help' for usage info.")
        return
    
    if args[0] == "help":
        print(texts.delete_help)
        return
    
    with torrents_lock:
        if args[0] not in torrents:
            print("Torrent not found.")
            return        
        torrents.pop(args[0])
    
    with user_torrents_lock:
        for user in user_torrents:
            if args[0] in user_torrents[user]:
                user_torrents[user].remove(args[0])
    
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
    
    with users_online_lock:
        for i, peer in enumerate(users_online):
            print(peer, end="\n" if (i + 1) % 5 == 0 else "\t")
            
    print("")

def list_torrents(args: list[str]):
    if len(args) > 0 and args[0] == "help":
        print(texts.list_torrents_help)
        return
    
    with torrents_lock:
        print("\n".join(torrents.keys()))

def info_command(args: list[str]):
    if len(args) == 0:
        print("Wrong number of arguments in 'info' command, use 'info help' for usage info.")
        return
    
    if args[0] == "help":
        print(texts.info_help)
        return
    
    if args[0] not in torrents:
        print(f"Torrent {args[0]} not found. Try 'list-torrents' for available torrents.")
        return
    
    print(str(get_torrent_info(args[0])))
    with torrents_lock:
        peers_str = "\n".join(torrents[args[0]].peers)
    
    print(f"Peers:\n{peers_str}")
    

commands: dict[str, Callable] = {
    "help": print_help,
    'delete': delete_torrent,
    "exit": exit_command,
    "list-peers": list_peers,
    "list-torrents": list_torrents,
    "info": info_command,
}

# endregion

def main():
    print("Welcome to CDL-BitTorrent Server!")
    print("Starting up...")
    
    logger_thread = Thread(target=logger, daemon=True)
    logger_thread.start()
    
    check_thread = Thread(target=client_check_thread, daemon=True)
    check_thread.start()
    
    requests_thread = Thread(target=server_requests_tread, daemon=True)
    requests_thread.start()
    
    print("Done!")
    
    print("This is the CDL-BitTorrent Server CLI, use help to learn the available commands.")
    
    while True:
        inp: str = input("$> ").strip()
        
        if len(inp) == 0:
            continue
        
        [command, *args] = split_command(inp)
        
        if command not in commands:
            print(f"Unknown command '{command}', use 'help' to see the available commands.")
            continue
        
        commands[command](args)

if __name__ == "__main__":
    main()
