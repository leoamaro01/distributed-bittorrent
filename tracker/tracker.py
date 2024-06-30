from concurrent.futures import ThreadPoolExecutor, as_completed
import socket
from threading import Lock, Thread
from utils.torrent_requests import *
import hashlib

CLIENT_COMMS_PORT = 7011

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
            
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.connect((ip, CLIENT_COMMS_PORT))
                
                sock.sendall(GetClientTorrentsRequest().to_bytes())
                
                res, res_type = TorrentRequest.recv_request(sock)
                
                if res_type != RT_CLIENT_TORRENTS_RESPONSE:
                    return
                
                res: ClientTorrentsResponse

                with user_torrents_lock:
                    user_torrents[ip] = res.torrents

                with torrents_lock:
                    for torrent in res.torrents:
                        if torrent in torrents and ip not in torrents[torrent].peers:
                            torrents[torrent].peers.append(ip)
        elif req_type == RT_LOGOUT:
            with users_online_lock:
                if ip in users_online:
                    users_online.remove(ip)
                    
            if ip in user_torrents:
                with user_torrents_lock:
                    with torrents_lock:
                        for torrent in user_torrents[ip]:
                            if torrent in torrents and ip in torrents[torrent].peers:
                                    torrents[torrent].peers.remove(ip)

                    user_torrents.pop(ip)                        
        elif req_type == RT_UPLOAD_TORRENT:
            req: UploadTorrentRequest
            
            torrent_id = hashlib.sha256(req.files[0].file_name.encode()).hexdigest()
            
            while torrent_id in torrents:
                torrent_id = hashlib.sha256(torrent_id.encode()).hexdigest()
            
            torrent = Torrent(torrent_id, [ip], req.files, req.piece_size)
            
            torrents[torrent_id] = torrent
            
            client.sendall(UploadTorrentResponse(torrent_id).to_bytes())

def main():
    socket.setdefaulttimeout(5)
    
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    sock.bind(('', 8080))
    sock.listen(5)

    while True:
        client, addr = sock.accept()
        
        client_thread = Thread(target=handle_client, args=[client, addr[0]])
        client_thread.start()
    
