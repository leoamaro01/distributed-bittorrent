from io import BufferedReader
import socket
import os
import os.path as path
from threading import Lock, Thread
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable
from utils.torrent_requests import *
import hashlib
import texts

from utils.utils import TorrentError

CLIENT_CONNECTION_PORT = 7010

DOWNLOAD_THREADS = 10

UPLOAD_THREADS = 10

downloads_from_hosts: dict[str, int] = {}
dfh_lock: Lock = Lock()

# Torrent ID : [ Paths ]
available_seeds: dict[str, str] = []
seeds_lock: Lock = Lock()


def get_torrent_info(torrent_id: str, include_peers: bool = True) -> TorrentInfo:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    # TODO: server distribution in chord
    # How do I find the address of a running node in the chord?
    sock.connect(("bittorrent-tracker", 8080))

    sock.sendall(GetTorrentRequest(torrent_id, include_peers).to_bytes())
    
    req, req_type = TorrentRequest.recv_request(sock)
    
    sock.close()
    
    if req_type != RT_TORRENT:
        raise TorrentError(f"Unexpected Response from tracker, expected PeersRequest({RT_TORRENT}, received {req_type}).\nRequest:\n{json.dumps(req)}")
    
    req: TorrentInfoResponse
    
    return req.torrent_info

def upload_torrent(torrent_path: str):
    if not path.exists(torrent_path):
        raise TorrentError(f"Specified path {torrent_path} does not exist")
    
    # [(file_name, file_size, [piece_hashes])]
    files: list[tuple[str, int, list[bytes]]] = []
    
    print("Uploading torrent...")
    print("Generating piece hashes...")
    
    for root, _, file_names in os.walk(torrent_path):
        for file_name in file_names:
            file_path = path.join(root, file_name)
            
            print(f"Generating piece hashes for file {file_path}")
            
            file_size = path.getsize(file_path)
            piece_hashes = []
            
            with open(file_path, 'rb') as f:
                while True:
                    piece = f.read(PIECE_SIZE)
                    
                    if len(piece) == 0:
                        break
                    
                    piece_hashes.append(calculate_piece_hash(piece))
            
            files.append((file_name, file_size, piece_hashes))
    
    print("Uploading torrent info...")
    
    sock = None
    
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.connect(("bittorrent-tracker", 8080))
        except:
            raise TorrentError("Failed to connect to tracker. Check your network connection.")    
        
        hostname = socket.gethostname()
        ip = socket.gethostbyname(hostname)
        
        try:
            sock.sendall(UploadTorrentRequest(ip, files).to_bytes())
        except:
            raise TorrentError("Failed to upload torrent info. Check your network connection.")
        
        req, req_type = TorrentRequest.recv_request(sock)
        
        if req_type != RT_UPLOAD_RESPONSE:
            raise RuntimeError(f"Unexpected Response from tracker, expected UploadTorrentResponse({RT_UPLOAD_RESPONSE}, received {req_type}).\nRequest:\n{json.dumps(req)}")
        
        req: UploadTorrentResponse
        
        print(f"Torrent uploaded successfully. Torrent ID: {req.torrent_id}")
        
        with seeds_lock:
            available_seeds[req.torrent_id] = [torrent_path]

def download_torrent(torrent_id: str):
    info = get_torrent_info(torrent_id)
    
    if len(info.files) == 0:
        raise TorrentError(f"Torrent {torrent_id} not found or empty.")

    downloaded: dict[int, list[int]] = [] # TODO: fill with existing data for resuming
    
    partial_torrent_path = f"downloads/.partial/{torrent_id}"
    
    if path.exists(partial_torrent_path):
        print("Loading existing parts...")
        
        files_folders = os.listdir(partial_torrent_path)
            
        for file_folder in files_folders:
            try:
                index = int(file_folder, base=16)
            except:
                continue
            
            file_folder_path = path.join(partial_torrent_path, file_folder)
            
            if not path.isdir(file_folder_path):
                continue
            
            downloaded[index] = []
            
            piece_files = os.listdir(file_folder_path)
            
            loaded = 0
            for piece_file in piece_files:
                try:
                    piece_index = int(piece_file, base=16)
                except:
                    continue
                
                piece_path = path.join(file_folder_path, piece_file)
                
                if not path.isfile(piece_path):
                    continue
                
                piece_hash = info.files[index].piece_hashes[piece_index]
                
                if path.getsize(piece_path) != info.piece_size:
                    os.remove(piece_path)
                        
                f = open(piece_path, "rb")
                piece_contents = f.read()
                f.close()
                
                if not check_piece_hash(piece_contents, piece_hash):
                    os.remove(piece_path)
                    continue
                
                downloaded[index].append(piece_index)

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
                os.makedirs(path.dirname(file_name), exist_ok=True)
                
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

def calculate_piece_hash(piece: bytes) -> bytes:
    return hashlib.sha256(piece).digest()

def check_piece_hash(piece: bytes, hash: bytes) -> bool:
    return calculate_piece_hash(piece) == hash

def check_file(torrent_info: TorrentInfo, file_index: int) -> bool:
    file_info: TorrentFileInfo = torrent_info.files[file_index]
    
    file_path = path.join(f"downloads/{torrent_info.id}", file_info.file_name)
    
    if not path.exists(file_path):
        return False
    
    with open(file_path, 'rb') as file:
        if len(file) != file_info.file_size:
            return False

        pieces = len(file_info.piece_hashes)
        
        for i in range(pieces):
            piece_size = get_piece_size(torrent_info, file_index, i)
            
            piece = file.read(piece_size)
            
            if not check_piece_hash(piece, file_info.piece_hashes[i]):
                return False
            
    return True
    
def get_piece_size(torrent_info: TorrentInfo, file_index , piece_index: int):
    file_info: TorrentFileInfo = torrent_info.files[file_index]
    piece_size = torrent_info.piece_size
                    
    piece_count = len(file_info.piece_hashes)
    
    if piece_index == piece_count - 1:
        piece_size = file_info.file_size - (piece_count - 1) * piece_size
        
    return piece_size

def download_file_piece(torrent_info: TorrentInfo, file_name: str, file_index: int, piece_index: int, peers: list[str]) -> tuple[int, int] | None:
    peers_copy = peers.copy()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:        
        with dfh_lock:
            peers_copy.sort(key=lambda peer: downloads_from_hosts[peer] if peer in downloads_from_hosts else 0)
        
        for peer in peers_copy:
            try:
                sock.connect((peer, CLIENT_CONNECTION_PORT))
            except Exception as e:
                print(f"Failed to connect to peer {peer}. Error:")
                print(e)
            else:
                with dfh_lock:
                    if peer in downloads_from_hosts:
                        downloads_from_hosts[peer] += 1
                    else:
                        downloads_from_hosts[peer] = 1
                
                try:
                    sock.sendall(DownloadPieceRequest(torrent_info.id, file_index, piece_index).to_bytes())

                    req, req_type = TorrentRequest.recv_request(sock)
                    
                    if req_type != RT_STATUS:
                        raise TorrentError(f"Unexpected Response from tracker, expected DownloadPieceResponse({RT_STATUS}, received {req_type}).\nRequest:\n{json.dumps(req)}")
                    
                    req: StatusResponse
                    
                    if req.status_code != ST_OK:
                        raise TorrentError(f"Failed to download piece, reason: {status_str(req.status_code)}.")
                    
                    piece_size = get_piece_size(torrent_info, file_index, piece_index)
                    
                    file_bytes = recv_all(sock, piece_size)
                    
                    with open(f"downloads/.partial/{torrent_info.id}/{get_hex(file_index)}/{get_hex(piece_index)}", 'wb') as piece_file:
                        piece_file.write(file_bytes)
                    
                    return file_index, piece_index
                except Exception as e:
                    print(f"Failed to download piece from peer {peer}. Error:")
                    print(e)        
        return None       

def load_seeds():
    print("Loading Seeds...")
    
    os.makedirs("downloads/.partial", exist_ok=True)
    
    contents = os.listdir("downloads")
    
    for item in contents:
        if item == ".partial":
            continue
        
        full_path = path.join("downloads", item)
        
        if not path.isdir(full_path):
            continue
        
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as torrent_exists_request:  
            try:
                torrent_exists_request.connect(("bittorrent-tracker", 8080))
                torrent_exists_request.sendall(TorrentExistsRequest(item).to_bytes())
                
                req, req_type = TorrentRequest.recv_request(torrent_exists_request)
                
                if req_type != RT_STATUS:
                    print(f"Unexpected Response from tracker, expected TorrentExistsResponse({RT_STATUS}, received {req_type}).\nRequest:\n{json.dumps(req)}")
                    continue
                
                req: StatusResponse
                
                if req.status_code != ST_OK:
                    print(f"Folder {item} does not appear to be a torrent, skipping...")
                    continue
            except Exception as e:
                print(f"Failed to check for {item} from tracker. Check your network connection.")
                continue
            
        # print(f"Loading available pieces for downloaded torrent {item}...")
        
        print(f"Loaded torrent {item}")
        
        with seeds_lock:
            if item in available_seeds and full_path not in available_seeds[item]:
                available_seeds[item].append(full_path)
            elif item not in available_seeds:
                available_seeds[item] = [full_path]
        
        # passed = 0
        # for i in range(len(torrent_info.files)):
        #     if check_file(torrent_info, i):
        #         print(f"File {i} passed check, collecting as seed.", end="\r")
        #         passed += 1
        #         with seeds_lock:
        #             available_seeds[item].extend([(i, j) for j in range(len(torrent_info.files[i].piece_hashes))])
        # print(f"{passed}/{len(torrent_info.files)} files passed verification for torrent {item}")
    
    print("Loaded seeds from downloaded torrents.")    
    
    print("Loading seeds from partial torrents...")
    
    partial_torrents = os.listdir("downloads/.partial")
    
    for item in partial_torrents:
        full_path = path.join("downloads/.partial", item)
        
        if not path.isdir(full_path):
            continue
        
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as torrent_exists_request:  
            try:
                torrent_exists_request.connect(("bittorrent-tracker", 8080))
                torrent_exists_request.sendall(TorrentExistsRequest(item).to_bytes())
                
                req, req_type = TorrentRequest.recv_request(torrent_exists_request)
                
                if req_type != RT_STATUS:
                    print(f"Unexpected Response from tracker, expected TorrentExistsResponse({RT_STATUS}, received {req_type}).\nRequest:\n{json.dumps(req)}")
                    continue
                
                req: StatusResponse
                
                if req.status_code != ST_OK:
                    print(f"Folder {item} does not appear to be a torrent, skipping...")
                    continue
            except Exception as e:
                print(f"Failed to check for {item} from tracker. Check your network connection.")
                continue
        
        print(f"Loaded partial torrent {item}")
        
        with seeds_lock:
            if item in available_seeds and full_path not in available_seeds[item]:
                available_seeds[item].append(full_path)
            elif item not in available_seeds:
                available_seeds[item] = [full_path]
        
        print("Finished loading Seeds.")        

def seeder_thread():
    pass

# region Commands
def print_help(args: list[str]):
    if len(args) != 0 and args[0] != "help" and args[0] in commands:
        commands[args[0]](["help"])
        return
    
    print(texts.help_text)

def download_command(args: list[str]):
    if len(args) == 0:
        print("Wrong number of arguments in 'download' command, use 'download help' for usage info.")
        return
    
    if args[0] == "help":
        print(texts.download_help)
        return
    
    try:
        download_torrent(args[0])
    except TorrentError as e:
        print(f"Failed to download torrent. Error:\n{e.message}")

def upload_command(args: list[str]):
    if len(args) == 0:
        print("Wrong number of arguments in 'upload' command, use 'upload help' for usage info.")
        return
    
    if args[0] == "help":
        print(texts.upload_help)
        return

    try:
        upload_torrent(args[0])
    except TorrentError as e:
        print(f"Failed to upload torrent. Error:\n{e.message}")

commands: dict[str, Callable] = {
        "help": print_help,
        "download": download_command,
        "upload": upload_command
    }

# endregion

def split_command(command: str) -> list[str]:
    result: list[str] = []
    current_start = 0
    
    scope_char = None
    
    for i in len(command):
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
            
def main():
    print("Welcome to CDL-BitTorrent!")
    print("Starting Up...")
    
    load_seeds()
    
    print("Starting seeder...")
    
    seeder = Thread(target=seeder_thread, name="seeder")
    seeder.start()
    
    print("Done!")
    
    while True:
        print("This is the CDL-BitTorrent CLI, use help to learn the available commands.")
        inp: str = input("$> ").strip()
        
        if len(inp) == 0:
            continue
        
        [command, *args] = split_command(inp)
        
        commands[command](args)