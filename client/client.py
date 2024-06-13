import socket
import os
import os.path
from threading import Lock
from concurrent.futures import ThreadPoolExecutor, as_completed
from utils.torrent_requests import *

CLIENT_CONNECTION_PORT = 7010

DOWNLOAD_THREADS = 10

downloads_from_hosts: dict[str, int] = {}
non_responding_peers: list[str] = []
dfh_lock: Lock = Lock()
nrp_lock: Lock = Lock()

def get_torrent_info(torrent_id: str):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    # TODO: server distribution in chord
    # How do I find the address of a running node in the chord?
    sock.connect(("bittorrent-tracker", 8080))

    sock.sendall(GetTorrentRequest(torrent_id).to_bytes())
    
    req, req_type = TorrentRequest.recv_request(sock)
    
    sock.close()
    
    if req_type != RT_TORRENT:
        raise RuntimeError(f"Unexpected Response from tracker, expected PeersRequest({RT_TORRENT}, received {req_type}).\nRequest:\n{json.dumps(req)}")
    
    req: TorrentInfoResponse
    
    return req.torrent_info
    
def download_torrent(torrent_id: str):
    info = get_torrent_info(torrent_id)
    
    if len(info.files) == 0:
        raise RuntimeError(f"Torrent {torrent_id} not found or empty.")

    downloaded: dict[int, list[int]] = [] # TODO: fill with existing data for resuming
    pending_download: dict[int, list[int]] = {}
    
    for file_index, file in enumerate(info.files):
        pieces = range(len(file.piece_hashes))
        for piece in pieces:
            if file_index in downloaded and piece in downloaded[file_index]:
                continue
            if file_index in pending_download:
                pending_download[file_index].append(piece)
            else:
                pending_download[file_index] = [piece]

    # ordering the file pieces by rarity
    
    # (file, piece): [peers]
    pieces: dict[tuple[int, int], list[str]] = {}
    
    for peer in info.peers:
        for file in peer.pieces:
            for piece in peer.pieces[file]:
                if file in downloaded and piece in downloaded[file]:
                    continue
                
                if (file, piece) in pieces:
                    pieces[(file, piece)].append(peer.ip)
                else:
                    pieces[(file, piece)] = [peer.ip]
    
    rarity_ordered = list(pieces.keys())
    rarity_ordered.sort(key=lambda file_piece: len(pieces[file_piece]))
    
    with ThreadPoolExecutor(max_workers=DOWNLOAD_THREADS) as executor:
        futures = [executor.submit(download_file_piece, info.files[file].file_name, file, piece, pieces[(file, piece)]) 
                   for file, piece in rarity_ordered]
        
        for future in as_completed(futures):
            result = future.result()
            
            if result == None:
                return
            
            file, piece = result

            pending_download[file].remove(piece)
            
            if len(pending_download[file]) == 0:
                del pending_download[file] 
                
                # reconstruct file
                file_name = f"downloads/{torrent_id}/{info.files[file].file_name}"
                os.makedirs(os.path.dirname(file_name), exist_ok=True)
                
                with open(file_name, 'wb') as f:
                    for piece in range(len(info.files[file].piece_hashes)):
                        file_hex = get_hex(file)
                        piece_hex = get_hex(piece)
                        with open(f"downloads/.partial/{torrent_id}/{file_hex}/{piece_hex}", 'rb') as piece_file:
                            f.write(piece_file.read())
                
                # remove partial files
                for piece in range(len(info.files[file].piece_hashes)):
                    file_hex = get_hex(file)
                    piece_hex = get_hex(piece)
                    os.remove(f"downloads/.partial/{torrent_id}/{file_hex}/{piece_hex}")            

def get_hex(n: int) -> str:
    return hex(n)[2:]

def download_file_piece(file_name: str, file_index: int, piece_index: int, peers: list[str]) -> tuple[int, int] | None:
    peers_copy = peers.copy()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with nrp_lock:
            peers_copy = [p for p in peers_copy if p not in non_responding_peers]
        
        with dfh_lock:
            peers_copy.sort(key=lambda peer: downloads_from_hosts[peer] if peer in downloads_from_hosts else 0)
        
        for peer in peers_copy:
            try:
                sock.connect((peer, CLIENT_CONNECTION_PORT))
            except Exception as e:
                print(f"Failed to download piece from {peer}. Error:")
                print(e)
                with nrp_lock:
                    if peer not in non_responding_peers:
                        non_responding_peers.append(peer)
                continue
            else:
                with dfh_lock:
                    if peer in downloads_from_hosts:
                        downloads_from_hosts[peer] += 1
                    else:
                        downloads_from_hosts[peer] = 1
                
                # TODO: Send file request, receive file, if it fails at any point, try with next peer (don't report as it is probably a temporary issue )
                break
        
        return None       
        