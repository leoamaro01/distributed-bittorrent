import json
import socket
from utils.torrent_info import TorrentFileInfo, TorrentInfo, TorrentPeer
from utils.utils import recv_all
import hashlib

# region Request IDs
RT_INVALID = 0
RT_ACK = 1
RT_GET_TORRENT = 2
RT_FILE_START = 3
RT_DOWNLOAD_START = 4
RT_TORRENT = 5
# endregion

# --- Standards ---
# standard piece size: 256KB
# max file size: ~1PB
# peers count: max 2^32 (4 bytes)
PEERS_B = 4
# piece count per file: max 2^32 (4 bytes)
PIECE_COUNT_B = 4
# file count: max 2^64 (8 bytes)
FILE_COUNT_B = 8

class TorrentRequest:
    # returns (Request, type of request)
    @staticmethod
    def recv_request(sock: socket.socket) -> tuple["TorrentRequest", int] | None:
        recv = sock.recv(1)
        
        if len(recv) == 0: 
            return None
        
        request_type = int.from_bytes(recv)
        
        if request_type == RT_INVALID:
            raise TypeError("Received invalid request")
        elif request_type == RT_ACK:
            return AckRequest(), RT_ACK
        elif request_type == RT_TORRENT:
            return TorrentInfoResponse.recv_request(sock)
        elif request_type == RT_GET_TORRENT:
            return GetTorrentRequest.recv_request(sock)
    
    def to_bytes(self) -> bytes:
        raise TypeError()

class GetTorrentRequest(TorrentRequest):
    def __init__(self, torrent_id: str) -> None:
        self.torrent_id = torrent_id

    @staticmethod
    def recv_request(sock: socket.socket) -> tuple["GetTorrentRequest", int]:
        id_length = int.from_bytes(recv_all(sock, 4))
        id = recv_all(sock, id_length).decode()
        
        return GetTorrentRequest(id), RT_GET_TORRENT

    def to_bytes(self) -> bytes:
        id = self.torrent_id.encode()
        id_len = len(id).to_bytes(4)
        
        # Torrent ID Length - Torrent ID
        return RT_GET_TORRENT.to_bytes() + id_len + id 

class TorrentInfoResponse(TorrentRequest):
    def __init__(self, torrent_info: TorrentInfo) -> None:
        self.torrent_info = torrent_info

    @staticmethod
    def recv_request(sock: socket.socket) -> tuple["TorrentInfoResponse", int]:
        peers_count = int.from_bytes(recv_all(sock, 4))
        
        peers = []
        for _ in range(peers_count):
            ip = '.'.join(str(b) for b in recv_all(sock, 4))
            
            peer_files_count = int.from_bytes(recv_all(sock, FILE_COUNT_B))
            
            peer_file_pieces = {}
            for _ in peer_files_count:
                file = int.from_bytes(recv_all(sock, FILE_COUNT_B))
                
                peer_file_pieces[file] = []
                
                pieces_count = int.from_bytes(recv_all(sock, PIECE_COUNT_B))
                
                for _ in pieces_count:
                    peer_file_pieces[file].append(int.from_bytes(recv_all(sock, PIECE_COUNT_B)))
            
            peers.append(TorrentPeer(ip, peer_file_pieces))
            
        piece_size = int.from_bytes(recv_all(sock, 4))
        
        files_count = int.from_bytes(recv_all(sock, FILE_COUNT_B))
        
        files = []
        if files_count != 0:
            for _ in range(files_count):
                name_len = int.from_bytes(recv_all(sock, 4))
                file_name = recv_all(sock, name_len).decode()
                
                file_size = int.from_bytes(recv_all(sock, 8))
                pieces_count = int.from_bytes(recv_all(sock, PIECE_COUNT_B))
                
                hashes = []
                for _ in range(pieces_count):
                    hashes.append(recv_all(sock, 32))
                
                files.append(TorrentFileInfo(file_name, file_size, hashes))
        
        return TorrentInfoResponse(TorrentInfo(peers, files, piece_size)), RT_TORRENT

    def to_bytes(self) -> bytes:
        peers_count = len(self.torrent_info.peers).to_bytes(PEERS_B)
        peers = b''
        
        for peer in self.torrent_info.peers:
            ip = b''.join(int(i).to_bytes() for i in peer.ip.split('.'))
            peer_files_count = len(peer.pieces.keys()).to_bytes(FILE_COUNT_B)
            
            peer_file_pieces = b''
            
            for file in peer.pieces:
                peer_file_pieces += file.to_bytes(FILE_COUNT_B)
                peer_file_pieces += len(peer.pieces[file]).to_bytes(PIECE_COUNT_B)
                
                for piece in peer.pieces[file]:
                    peer_file_pieces += piece.to_bytes(PIECE_COUNT_B)
            
            peers += ip + peer_files_count + peer_file_pieces
        
        files_count = len(self.torrent_info.files).to_bytes(FILE_COUNT_B)
        files = b''
        
        for file in self.torrent_info.files:
            name_bytes = file.file_name.encode()
            name_length = len(name_bytes).to_bytes(4)
            file_size = file.file_size.to_bytes(8)
            pieces_count = len(file.piece_hashes).to_bytes(4)
            hashes = b''.join(file.piece_hashes)
            
            files += name_length + name_bytes + file_size + pieces_count + hashes
        
        piece_size = self.torrent_info.piece_size.to_bytes(4)
        
        # Peers Count - [Peer IP - Peer Files Count - [File Index - Piece Count - [Piece Index]]] - Piece Size - Files Count - [File Name Size - File Name - File Size - Pieces Count - Piece Hashes]
        return RT_TORRENT.to_bytes() + peers_count + peers + piece_size + files_count + files

class AckRequest(TorrentRequest):
    pass