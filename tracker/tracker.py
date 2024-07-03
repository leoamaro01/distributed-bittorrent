from concurrent.futures import ThreadPoolExecutor, as_completed
import socket
from threading import Lock, Thread
from typing import Callable
from utils.torrent_requests import *
from utils.utils import *
import hashlib
import texts
import time

CLIENT_COMMS_PORT = 7011

CHECKUP_TIME = 5 * 60

class Torrent:
    def __init__(self, torrent_id: str, peers: list[str], files: list[TorrentFileInfo], piece_size: int) -> None:
        self.torrent_id = torrent_id
        self.peers = peers
        self.files = files
        self.piece_size = piece_size

torrents: dict[str, Torrent] = []
torrents_lock: Lock = Lock()

users_online: list[str] = []
users_online_lock: Lock = Lock()

user_torrents: dict[str, list[str]] = []
user_torrents_lock: Lock = Lock()

def get_client_available_pieces(ip: str, torrent_id: str) -> tuple[str, dict[int, list[int]]] | None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.timeout
            sock.connect((ip, CLIENT_COMMS_PORT))

            sock.sendall(GetAvailablePiecesRequest(torrent_id).to_bytes())
            
            res, res_type = TorrentRequest.recv_request(sock)
            
            if res_type != RT_PIECES_RESPONSE:
                return None
            
            res: AvailablePiecesResponse
            
            return ip, res.file_pieces
        except:
            return None
            
def handle_client(client: socket.socket, ip: str):
    with client:
        req, req_type = TorrentRequest.recv_request(client)
        
        if req_type == RT_GET_TORRENT:
            req: GetTorrentRequest
            
            with torrents_lock:
                torrent = torrents.get(req.torrent_id, None)
            
            if torrent == None:
                client.sendall(TorrentInfoResponse(req.torrent_id, [], []).to_bytes())
                return            
            
            info = TorrentInfo(torrent.torrent_id, [], torrent.files, torrent.piece_size)            
            
            if req.include_peers:
                with ThreadPoolExecutor() as executor:
                    with users_online_lock:
                        futures = [executor.submit(get_client_available_pieces, peer, req.torrent_id) for peer in torrent.peers if peer in users_online]
                
                    for future in as_completed(futures):            
                        result = future.result()
                        
                        if result == None:
                            continue
                        
                        peer, file_pieces = result
                        
                        if len(file_pieces) == 0:
                            continue
                    
                        info.peers.append(TorrentPeer(peer, file_pieces))
            
            client.sendall(TorrentInfoResponse(info).to_bytes())
        elif req_type == RT_LOGIN:
            with users_online_lock:
                if ip not in users_online:
                    users_online.append(ip)
            
            check_client(ip)
        elif req_type == RT_LOGOUT:
            with users_online_lock:
                if ip in users_online:
                    users_online.remove(ip)
                    
            erase_client_data(ip)                      
        elif req_type == RT_UPLOAD_TORRENT:
            req: UploadTorrentRequest
            
            torrent_id = hashlib.sha256(req.files[0].file_name.encode()).hexdigest()
            
            while torrent_id in torrents:
                torrent_id = hashlib.sha256(torrent_id.encode()).hexdigest()
            
            torrent = Torrent(torrent_id, [ip], req.files, req.piece_size)
            
            torrents[torrent_id] = torrent
            
            client.sendall(UploadTorrentResponse(torrent_id).to_bytes())

def client_check_thread():
    while True:
        time.sleep(CHECKUP_TIME)
        
        users = None
        with users_online_lock:
            users = users_online.copy()
        
        with ThreadPoolExecutor() as executor:
            futures = [executor.submit(check_client, user) for user in users]
            
            for future in as_completed(futures):
                result = future.result()
                
                is_online, user = result
                
                if not is_online:
                    with users_online_lock:
                        users_online.remove(user)
                        
                    erase_client_data(user)

def erase_client_data(user: str):
    with user_torrents_lock:
        if user in user_torrents:
            with torrents_lock:
                for torrent in user_torrents[user]:
                    if torrent in torrents and user in torrents[torrent].peers:
                        torrents[torrent].peers.remove(user)
            
            user_torrents.pop(user)

def check_client(ip: str) -> tuple[bool, str]:
    """
    Check if the client is still online
    
    Params:
        ip: The IP of the client
    
    Returns:
        A tuple with the first element being a boolean indicating if the client is still online and the second element being the IP of the client
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    
    try:
        sock.connect((ip, CLIENT_COMMS_PORT))
        
        sock.sendall(GetClientTorrentsRequest().to_bytes())
        
        res, res_type = TorrentRequest.recv_request(sock)
        
        if res_type != RT_CLIENT_TORRENTS_RESPONSE:
            return False, ip
        
        res: ClientTorrentsResponse
        
        erase_client_data(ip)
        
        with user_torrents_lock:
            user_torrents[ip] = res.torrents

        with torrents_lock:
            for torrent in res.torrents:
                if torrent in torrents and ip not in torrents[torrent].peers:
                    torrents[torrent].peers.append(ip)
        
        return True, ip
    except:
        erase_client_data(ip)
        return False, ip
    finally:
        sock.close()    

def server_requests_tread():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    sock.bind(('', 8080))
    sock.listen(5)

    while True:
        client, addr = sock.accept()
        
        client_thread = Thread(target=handle_client, args=[client, addr[0]])
        client_thread.start()

def main():
    print("Welcome to CDL-BitTorrent Server!")
    print("Starting up...")
    
    socket.setdefaulttimeout(5)
    
    check_thread = Thread(target=client_check_thread)
    check_thread.start()
    
    requests_thread = Thread(target=server_requests_tread)
    requests_thread.start()
    
    print("Done!")
    
    while True:
        print("This is the CDL-BitTorrent Server CLI, use help to learn the available commands.")
        inp: str = input("$> ").strip()
        
        if len(inp) == 0:
            continue
        
        [command, *args] = split_command(inp)
        
        commands[command](args)
    
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
    
    print("Exiting...")
    exit(0)

commands: dict[str, Callable] = {
    "help": print_help,
    'delete': delete_torrent,
    "exit": exit_command
}

# endregion
