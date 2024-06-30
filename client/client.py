import json
import socket
import os
import os.path as path
from threading import Lock, Thread
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
from typing import Callable
from utils.torrent_requests import *
import hashlib
import texts
from queue import SimpleQueue
from utils.utils import TorrentError

CLIENT_CONNECTION_PORT = 7010
SERVER_COMMS_PORT = 7011

DOWNLOAD_THREADS = 10

UPLOAD_THREADS = 10

open_files_locks: dict[str, Lock] = {}
open_files_locks_lock: Lock = Lock()

downloads_from_hosts: dict[str, int] = {}
dfh_lock: Lock = Lock()

# Torrent ID : [ Paths ]
available_seeds: dict[str, str] = []
seeds_lock: Lock = Lock()

def get_torrent_info(torrent_id: str, include_peers: bool = True) -> TorrentInfo:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    # TODO: server distribution in chord
    # How do I find the address of a running node in the chord?
    connect_socket_to_server(sock)

    sock.sendall(GetTorrentRequest(torrent_id, include_peers).to_bytes())
    
    sock.settimeout(None)
    
    req, req_type = TorrentRequest.recv_request(sock)
    
    sock.close()
    
    if req_type != RT_TORRENT:
        raise TorrentError(f"Unexpected Response from tracker, expected PeersRequest({RT_TORRENT}, received {req_type}).\nRequest:\n{json.dumps(req)}")
    
    req: TorrentInfoResponse
    
    return req.torrent_info

def open_file_with_lock(file_path):
    open_files_locks_lock.acquire()
    
    if file_path in open_files_locks:
        lock = open_files_locks[file_path]
        open_files_locks_lock.release()
        lock.acquire()
    else:
        lock = Lock()
        open_files_locks[file_path] = lock
        open_files_locks_lock.release()
        lock.acquire()

def close_file_with_lock(file_path):
    with open_files_locks_lock:
        if file_path not in open_files_locks:
            print("Tried to release file lock that was not acquired.")
            return
        
        open_files_locks[file_path].release()

def upload_torrent(torrent_path: str):
    if not path.exists(torrent_path):
        raise TorrentError(f"Specified path {torrent_path} does not exist")
    
    # [(file_name, file_size, [piece_hashes])]
    files: list[TorrentFileInfo] = []
    
    print("Uploading torrent...")
    print("Generating piece hashes...")
    
    for root, _, file_names in os.walk(torrent_path):
        for file_name in file_names:
            file_path = path.join(root, file_name)
            
            print(f"Generating piece hashes for file {file_path}")
            
            file_size = path.getsize(file_path)
            piece_hashes = []
            
            open_file_with_lock(file_path)
            
            try:
                with open(file_path, 'rb') as f:
                    while True:
                        piece = f.read(PIECE_SIZE)
                        
                        if len(piece) == 0:
                            break
                        
                        piece_hashes.append(calculate_piece_hash(piece))
            finally:
                close_file_with_lock(file_path)
            
            files.append(TorrentFileInfo(file_name, file_size, piece_hashes))
    
    print("Uploading torrent info...")
    
    sock = None
    
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        connect_socket_to_server(sock)
        
        try:
            sock.sendall(UploadTorrentRequest(files, PIECE_SIZE).to_bytes())
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
        print(f"Torrent {torrent_id} not found or empty.")

    downloaded: dict[int, list[int]] = []
    
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
                
                open_file_with_lock(piece_path)
                
                try:
                    f = open(piece_path, "rb")
                    piece_contents = f.read()
                    f.close()
                finally:
                    close_file_with_lock(piece_path)
                
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
        futures = [executor.submit(download_file_piece, info, file, piece, pieces[(file, piece)]) 
                   for file, piece in rarity_ordered]
    
        for future in as_completed(futures):            
            result = future.result()
            if result == None:
                continue
            
            print(f"Downloaded {result}!")
            
            file, piece = result

            pending_download[file].remove(piece)
            
            if len(pending_download[file]) == 0:
                pending_download.pop(file) 
                
                # reconstruct file
                file_name = path.join(f"downloads/{torrent_id}", info.files[file].file_name)
                os.makedirs(path.dirname(file_name), exist_ok=True)
                
                open_file_with_lock(file_name)
                
                try:
                    with open(file_name, 'wb') as f:
                        for piece in range(len(info.files[file].piece_hashes)):
                            file_hex = get_hex(file)
                            piece_hex = get_hex(piece)
                            piece_path = f"downloads/.partial/{torrent_id}/{file_hex}/{piece_hex}"
                            
                            open_file_with_lock(piece_path)
                            
                            try:
                                with open(piece_path, 'rb') as piece_file:
                                    f.write(piece_file.read())
                            finally:
                                close_file_with_lock(piece_path)
                finally:
                    close_file_with_lock(file_name)
                
                # remove partial files
                for piece in range(len(info.files[file].piece_hashes)):
                    file_hex = get_hex(file)
                    piece_hex = get_hex(piece)
                    os.remove(f"downloads/.partial/{torrent_id}/{file_hex}/{piece_hex}")            

def download_file_piece(torrent_info: TorrentInfo, file_index: int, piece_index: int, peers: list[str]) -> tuple[int, int] | None:
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
                    
                    if not check_piece_hash(file_bytes, torrent_info.files[file_index].piece_hashes[piece_index]):
                        raise TorrentError("Could not verify file received from peer.")
                    
                    piece_path = f"downloads/.partial/{torrent_info.id}/{get_hex(file_index)}/{get_hex(piece_index)}"
                    os.makedirs(path.dirname(piece_path), exist_ok=True)

                    open_file_with_lock(piece_path)
                    
                    try:
                        with open(piece_path, 'wb') as piece_file:
                            piece_file.write(file_bytes)
                    finally:
                        close_file_with_lock(piece_path)
                    
                    return file_index, piece_index
                except Exception as e:
                    print(f"Failed to download piece from peer {peer}. Error:")
                    print(e)        
        return None      
    
def get_hex(n: int) -> str:
    return hex(n)[2:]

def calculate_piece_hash(piece: bytes) -> bytes:
    return hashlib.sha256(piece).digest()

def check_piece_hash(piece: bytes, hash: bytes) -> bool:
    return calculate_piece_hash(piece) == hash

def check_file(torrent_info: TorrentInfo, file_index: int, file_path:str = None) -> bool:
    file_info: TorrentFileInfo = torrent_info.files[file_index]
    
    if file_path == None:
        file_path = path.join(f"downloads/{torrent_info.id}", file_info.file_name)
    
    if not path.exists(file_path):
        return False
    
    open_file_with_lock(file_path)
    
    try:
        with open(file_path, 'rb') as file:
            if len(file) != file_info.file_size:
                return False

            pieces = len(file_info.piece_hashes)
            
            for i in range(pieces):
                piece_size = get_piece_size(torrent_info, file_index, i)
                
                piece = file.read(piece_size)
                
                if not check_piece_hash(piece, file_info.piece_hashes[i]):
                    return False
    finally:
        close_file_with_lock(file_path)
            
    return True
    
def get_piece_size(torrent_info: TorrentInfo, file_index , piece_index: int):
    file_info: TorrentFileInfo = torrent_info.files[file_index]
    piece_size = torrent_info.piece_size
                    
    piece_count = len(file_info.piece_hashes)
    
    if piece_index == piece_count - 1:
        piece_size = file_info.file_size - (piece_count - 1) * piece_size
        
    return piece_size 

def load_seeds():
    available_seeds = {}
    
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
                connect_socket_to_server(torrent_exists_request)
                
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
                connect_socket_to_server(torrent_exists_request)
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

def handle_incoming_peer(sock: socket.socket):
    req, req_type = TorrentRequest.recv_request(sock)
    
    if req_type == RT_DOWNLOAD_PIECE:
        req: DownloadPieceRequest
        
        with seeds_lock:
            torrent_in_seeds = req.torrent_id in available_seeds
            
        if not torrent_in_seeds:
            sock.sendall(StatusResponse(ST_NOTFOUND).to_bytes())
            sock.close()
            return

        with seeds_lock:
            paths = available_seeds[req.torrent_id]
        
        torrent_info = get_torrent_info(req.torrent_id, False)
        
        if len(torrent_info.files) == 0:
            print("Invalid Torrent")
            sock.sendall(StatusResponse(ST_NOTFOUND).to_bytes())
            sock.close()
            return
        
        file = torrent_info.files[req.file_index]
        piece_hash = file.piece_hashes[req.piece_index]
        
        for p in paths:
            with seeds_lock:
                is_partial = available_seeds.startswith("downloads/.partial")

            if is_partial:
                file_hex = get_hex(req.file_index)
                piece_hex = get_hex(req.piece_index)
                
                piece_file_path = f"downloads/.partial/{req.torrent_id}/{file_hex}/{piece_hex}"
                
                open_file_with_lock(piece_file_path)
                try:
                    with open(piece_file_path, "rb") as f:
                        piece_bytes = f.read()
                finally:
                    close_file_with_lock(piece_file_path)
                
                if not check_piece_hash(piece_bytes, piece_hash):
                    continue
                
                try:
                    sock.sendall(piece_bytes)
                except:
                    continue
                else:
                    return
            else:
                file_path = path.join(p, file.file_name)
                
                if not path.exists(file_path) or not path.isfile(file_path):
                    continue
                
                if path.getsize(file_path) != torrent_info.files[req.file_index].file_size:
                    continue
                
                open_file_with_lock(file_path)  
                
                try:
                    with open(file_path, "rb") as f:
                        f.seek(req.piece_index * torrent_info.piece_size)
                        piece_bytes = f.read(torrent_info.piece_size)
                finally:
                    close_file_with_lock(file_path)
                
                if not check_piece_hash(piece_bytes, piece_hash):
                    continue
                
                try:
                    sock.sendall(piece_bytes)
                except:
                    continue
                else:
                    return            

def seeder_thread():
    seeder_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    
    seeder_socket.bind(('', CLIENT_CONNECTION_PORT))
    seeder_socket.listen()
    
    while True:
        client, _ = seeder_socket.accept()
        
        client_thread = Thread(target=handle_incoming_peer, args=[client])
        client_thread.start()
    
def handle_server_request(sock: socket.socket):
    req, req_type = TorrentRequest.recv_request(sock)
    
    if req_type == RT_AVAILABLE_PIECES:
        req: GetAvailablePiecesRequest
        
        if req.torrent_id not in available_seeds:
            sock.sendall(AvailablePiecesResponse({}).to_bytes())
            sock.close()
            return
        
        with seeds_lock:
            paths: list[str] = available_seeds[req.torrent_id].copy()
            
        file_pieces = {}
        
        torrent_info = get_torrent_info(req.torrent_id, False)
        
        for torrent_path in paths:
            if torrent_path.startswith("downloads/.partial"):
                for file in os.listdir(torrent_path):
                    try:
                        file_index = int(file, base=16)
                    except:
                        continue
                    
                    file_pieces[file_index] = []
                    
                    for piece in os.listdir(torrent_path):
                        try:
                            piece_index = int(piece, base=16)
                        except:
                            continue
                        
                        piece_path = path.join(torrent_path, file, piece)
                        open_file_with_lock(piece_path)
                        
                        with open(piece_path, "rb") as f:
                            piece_bytes = f.read()                        
                        close_file_with_lock(piece_path)
                        
                        if not check_piece_hash(piece_bytes, torrent_info.files[file_index].piece_hashes[piece_index]):
                            continue
                        
                        file_pieces[file_index].append(piece_index)
            else:
                for e, file in enumerate(torrent_info.files):
                    file_path = path.join(torrent_path, file.file_name)
                    
                    if check_file(torrent_info, e, file_path):
                        file_pieces[file] = [i for i in range(len(file.piece_hashes))]
        
        sock.sendall(AvailablePiecesResponse(file_pieces).to_bytes())
        sock.close()
        return
    
    if req_type == RT_GET_CLIENT_TORRENTS:
        req: GetClientTorrentsRequest
        
        with seeds_lock:
            torrents = list(available_seeds.keys())
        
        sock.sendall(ClientTorrentsResponse(torrents).to_bytes())
        sock.close()
        return
     
def server_communication_thread():
    comms_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    
    comms_socket.bind(('', SERVER_COMMS_PORT))
    comms_socket.listen()
    
    while True:
        server_sock, _ = server_sock.accept()
        
        server_thread = Thread(target=handle_server_request, args=[server_sock])
        server_thread.start()

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

def exit_command(args: list[str]):
    if len(args) > 0 and args[0] == "help":
        print("Exits the client.")
        return
    
    print("Logging out of server...")
    
    logout_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    
    connect_socket_to_server(logout_socket)
    
    logout_socket.sendall(LogoutRequest().to_bytes())
    
    print("Exiting...")
    exit(0)

commands: dict[str, Callable] = {
        "help": print_help,
        "download": download_command,
        "upload": upload_command,
        "exit": exit_command
    }

# endregion

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

def connect_socket_to_server(socket: socket.socket):
    try:
        socket.connect(("bittorrent-tracker", 8080))
    except:
        raise TorrentError("Failed to connect to tracker. Check your network connection.")
          
def main():
    print("Welcome to CDL-BitTorrent!")
    print("Starting Up...")
    
    load_seeds()
    
    print("Starting seeder...")
    
    socket.setdefaulttimeout(5)
    
    seeder = Thread(target=seeder_thread, name="seeder")
    seeder.start()
    
    print("Starting server communication thread...")
    
    server_comms = Thread(target=server_communication_thread, name="server_comms")
    server_comms.start()
    
    print("Logging in to server...")    
    
    login_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    
    connect_socket_to_server(login_socket)
    
    login_socket.sendall(LoginRequest().to_bytes())
    login_socket.close()
    
    print("Done!")
    
    while True:
        print("This is the CDL-BitTorrent CLI, use help to learn the available commands.")
        inp: str = input("$> ").strip()
        
        if len(inp) == 0:
            continue
        
        [command, *args] = split_command(inp)
        
        commands[command](args)