import json
import socket
from utils.torrent_info import TorrentFileInfo, TorrentInfo, TorrentPeer
from utils.utils import TorrentError, recv_all
import hashlib



# region Response Status
ST_OK = 20
ST_NOTFOUND = 44

def status_str(status: int):
    if status == ST_OK:
        return "OK"
    if status == ST_NOTFOUND:
        return "Not Found"
# endregion

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

class TorrentRequest:
    # returns (Request, type of request)
    @staticmethod
    def recv_request(sock: socket.socket) -> tuple["TorrentRequest", int] | None:
        recv = sock.recv(1)
        
        if len(recv) == 0: 
            return None
        
        request_type = int.from_bytes(recv)
        
        if request_type not in requests:
            raise TorrentError(f"Received invalid request {request_type}. Expected one of {requests.keys()}")
        
        return requests[request_type](sock)
        
    def to_bytes(self) -> bytes:
        raise TypeError()

class TorrentExistsRequest(TorrentRequest):
    def __init__(self, torrent_id: str) -> None:
        self.torrent_id = torrent_id
    
    @staticmethod
    def recv_request(sock: socket.socket) -> tuple["TorrentExistsRequest", int]:
        id_length = int.from_bytes(recv_all(sock, 4))
        id = recv_all(sock, id_length).decode()
        
        return TorrentExistsRequest(id), RT_GET_TORRENT
    
    def to_bytes(self) -> bytes:
        id = self.torrent_id.encode()
        id_len = len(id).to_bytes(4)
        
        return RT_TORRENT_EXISTS.to_bytes() + id_len + id

class GetTorrentRequest(TorrentRequest):
    def __init__(self, torrent_id: str, include_peers: bool) -> None:
        self.torrent_id = torrent_id
        self.include_peers = include_peers

    @staticmethod
    def recv_request(sock: socket.socket) -> tuple["GetTorrentRequest", int]:
        id_length = int.from_bytes(recv_all(sock, 4))
        id = recv_all(sock, id_length).decode()
        include_peers = bool(int.from_bytes(recv_all(sock, 1)))
        
        return GetTorrentRequest(id, include_peers), RT_GET_TORRENT

    def to_bytes(self) -> bytes:
        id = self.torrent_id.encode()
        id_len = len(id).to_bytes(4)
        include_peers = int(self.include_peers).to_bytes(1)
        
        # Torrent ID Length - Torrent ID
        return RT_GET_TORRENT.to_bytes() + id_len + id + include_peers

class TorrentInfoResponse(TorrentRequest):
    def __init__(self, torrent_info: TorrentInfo) -> None:
        self.torrent_info = torrent_info

    @staticmethod
    def recv_request(sock: socket.socket) -> tuple["TorrentInfoResponse", int]:
        id_len = int.from_bytes(recv_all(sock, 4))
        
        id = recv_all(sock, id_len).decode()
        
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
        
        return TorrentInfoResponse(TorrentInfo(id, peers, files, piece_size)), RT_TORRENT

    def to_bytes(self) -> bytes:
        id = self.torrent_info.id.encode()
        id_len = len(id).to_bytes(4)
        
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
        
        # Torrent ID Length - Torrent ID - Peers Count - [Peer IP - Peer Files Count - [File Index - Piece Count - [Piece Index]]] - Piece Size - Files Count - [File Name Size - File Name - File Size - Pieces Count - Piece Hashes]
        return RT_TORRENT.to_bytes() + id_len + id + peers_count + peers + piece_size + files_count + files

class DownloadPieceRequest(TorrentRequest):
    def __init__(self, torrent_id: str, file_index: int, piece_index: int) -> None:
        self.torrent_id = torrent_id
        self.file_index = file_index
        self.piece_index = piece_index
    
    @staticmethod
    def recv_request(sock: socket.socket) -> tuple["DownloadPieceRequest", int]:
        id_length = int.from_bytes(recv_all(sock, 4))
        id = recv_all(sock, id_length).decode()
        
        file_index = int.from_bytes(recv_all(sock, FILE_COUNT_B))
        piece_index = int.from_bytes(recv_all(sock, PIECE_COUNT_B))
        
        return DownloadPieceRequest(id, file_index, piece_index), RT_DOWNLOAD_PIECE
    
    def to_bytes(self) -> bytes:
        id = self.torrent_id.encode()
        id_len = len(id).to_bytes(4)
        
        # Torrent ID Length - Torrent ID - File Index - Piece Index
        return RT_DOWNLOAD_PIECE.to_bytes() + id_len + id + self.file_index.to_bytes(FILE_COUNT_B) + self.piece_index.to_bytes(PIECE_COUNT_B)

class UploadTorrentRequest(TorrentRequest):
    def __init__(self, files: list[tuple[str, int, list[bytes]]]):
        self.files = files
    
    @staticmethod
    def recv_request(sock: socket.socket) -> tuple["UploadTorrentRequest", int]:
        files_count = int.from_bytes(recv_all(sock, FILE_COUNT_B))
        files = []
        
        for _ in range(files_count):
            name_len = int.from_bytes(recv_all(sock, 4))
            name = recv_all(sock, name_len).decode()
            size = int.from_bytes(recv_all(sock, 8))
            pieces_count = int.from_bytes(recv_all(sock, PIECE_COUNT_B))
            
            pieces = []
            for _ in range(pieces_count):
                pieces.append(recv_all(sock, 32))
            
            files.append((name, size, pieces))
        
        return UploadTorrentRequest(files), RT_UPLOAD_TORRENT
    
    def to_bytes(self) -> bytes:
        files_count = len(self.files).to_bytes(FILE_COUNT_B)
        files = b''
        
        for file in self.files:
            name = file[0].encode()
            name_len = len(name).to_bytes(4)
            size = file[1].to_bytes(8)
            pieces_count = len(file[2]).to_bytes(PIECE_COUNT_B)
            pieces = b''.join(file[2])
            
            files += name_len + name + size + pieces_count + pieces
        
        return RT_UPLOAD_TORRENT.to_bytes() + files_count + files
    
class UploadTorrentResponse(TorrentRequest):
    def __init__(self, torrent_id: str):
        self.torrent_id = torrent_id
        
    @staticmethod
    def recv_request(sock: socket.socket) -> tuple["UploadTorrentResponse", int] | None:
        id_len = int.from_bytes(recv_all(sock, 4))
        id = recv_all(sock, id_len).decode()
        
        return UploadTorrentResponse(id), RT_UPLOAD_RESPONSE
    
    def to_bytes(self) -> bytes:
        id = self.torrent_id.encode()
        id_len = len(id).to_bytes(4)
        
        return RT_UPLOAD_RESPONSE.to_bytes() + id_len + id

class StatusResponse(TorrentRequest):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
    
    @staticmethod
    def recv_request(sock: socket.socket) -> tuple["StatusResponse", int] | None:
        status = int.from_bytes(recv_all(sock, 1))
        return StatusResponse(status), RT_STATUS
    
    def to_bytes(self) -> bytes:
        return RT_STATUS.to_bytes() + self.status_code.to_bytes(1)

class GetAvailablePiecesRequest(TorrentRequest):
    def __init__(self, torrent_id: str) -> None:
        self.torrent_id = torrent_id
    
    @staticmethod
    def recv_request(sock: socket.socket) -> tuple["GetAvailablePiecesRequest", int]:
        id_length = int.from_bytes(recv_all(sock, 4))
        id = recv_all(sock, id_length).decode()
        
        return GetAvailablePiecesRequest(id), RT_AVAILABLE_PIECES
    
    def to_bytes(self) -> bytes:
        id = self.torrent_id.encode()
        id_len = len(id).to_bytes(4)
        
        return RT_AVAILABLE_PIECES.to_bytes() + id_len + id

class AvailablePiecesResponse(TorrentRequest):
    def __init__(self, file_pieces: dict[int, list[int]]) -> None:
        self.file_pieces = file_pieces
        
    @staticmethod
    def recv_request(sock: socket.socket) -> tuple["AvailablePiecesResponse", int]:
        file_count = int.from_bytes(recv_all(sock, FILE_COUNT_B))
        
        file_pieces = {}
        
        for _ in range(file_count):
            file_index = int.from_bytes(recv_all(sock, FILE_COUNT_B))
            pieces_count = int.from_bytes(recv_all(sock, PIECE_COUNT_B))
        
            pieces = []
            for _ in range(pieces_count):
                pieces.append(int.from_bytes(recv_all(sock, PIECE_COUNT_B)))
            
            file_pieces[file_index] = pieces
        
        return AvailablePiecesResponse(file_pieces), RT_PIECES_RESPONSE
    
    def to_bytes(self) -> bytes:
        file_count = len(self.file_pieces).to_bytes(FILE_COUNT_B)
        pieces = b''
        
        for file in self.file_pieces:
            pieces_count = len(self.file_pieces[file]).to_bytes(PIECE_COUNT_B)
            pieces += file.to_bytes(FILE_COUNT_B) + pieces_count + b''.join(piece.to_bytes(PIECE_COUNT_B) for piece in self.file_pieces[file])
        
        return RT_PIECES_RESPONSE.to_bytes() + file_count + pieces

class LoginRequest(TorrentRequest):
    @staticmethod
    def recv_request(sock: socket.socket) -> tuple[TorrentRequest, int] | None:
        return LoginRequest(), RT_LOGIN
    
    def to_bytes(self) -> bytes:
        return RT_LOGIN.to_bytes()

class LogoutRequest(TorrentRequest):
    @staticmethod
    def recv_request(sock: socket.socket) -> tuple[TorrentRequest, int] | None:
        return LogoutRequest(), RT_LOGOUT
    
    def to_bytes(self) -> bytes:
        return RT_LOGOUT.to_bytes()
    
class GetClientTorrentsRequest(TorrentRequest):
    @staticmethod
    def recv_request(sock: socket.socket) -> tuple[TorrentRequest, int] | None:
        return GetClientTorrentsRequest(), RT_GET_CLIENT_TORRENTS
    
    def to_bytes(self) -> bytes:
        return RT_GET_CLIENT_TORRENTS.to_bytes()

class ClientTorrentsResponse(TorrentRequest):
    def __init__(self, torrents: list[str]) -> None:
        self.torrents = torrents
        
    @staticmethod
    def recv_request(sock: socket.socket) -> tuple["ClientTorrentsResponse", int]:
        torrents_count = int.from_bytes(recv_all(sock, 4))
        torrents = []
        
        for _ in range(torrents_count):
            id_len = int.from_bytes(recv_all(sock, 4))
            id = recv_all(sock, id_len).decode()
            
            torrents.append(id)
        
        return ClientTorrentsResponse(torrents), RT_CLIENT_TORRENTS_RESPONSE
    
    def to_bytes(self) -> bytes:
        torrents_count = len(self.torrents).to_bytes(4)
        torrents = b''.join(len(t).to_bytes(4) + t.encode() for t in self.torrents)
        
        return RT_CLIENT_TORRENTS_RESPONSE.to_bytes() + torrents_count + torrents

# region Request IDs
RT_STATUS = 1
RT_GET_TORRENT = 2
RT_TORRENT = 3
RT_DOWNLOAD_PIECE = 4
RT_UPLOAD_TORRENT = 5
RT_UPLOAD_RESPONSE = 6
RT_TORRENT_EXISTS = 7
RT_AVAILABLE_PIECES = 8
RT_PIECES_RESPONSE = 9
RT_LOGIN = 10
RT_LOGOUT = 11
RT_GET_CLIENT_TORRENTS = 12
RT_CLIENT_TORRENTS_RESPONSE = 13

requests = {
    RT_STATUS: StatusResponse.recv_request,
    RT_GET_TORRENT: GetTorrentRequest.recv_request,
    RT_TORRENT: TorrentInfoResponse.recv_request,
    RT_DOWNLOAD_PIECE: DownloadPieceRequest.recv_request,
    RT_UPLOAD_TORRENT: UploadTorrentRequest.recv_request,
    RT_UPLOAD_RESPONSE: UploadTorrentResponse.recv_request,
    RT_TORRENT_EXISTS: TorrentExistsRequest.recv_request,
    RT_AVAILABLE_PIECES: GetAvailablePiecesRequest.recv_request,
    RT_PIECES_RESPONSE: AvailablePiecesResponse.recv_request,
    RT_LOGIN: LoginRequest.recv_request,
    RT_LOGOUT: LogoutRequest.recv_request,
    RT_GET_CLIENT_TORRENTS: GetClientTorrentsRequest.recv_request,
    RT_CLIENT_TORRENTS_RESPONSE: ClientTorrentsResponse.recv_request
}        
# endregion