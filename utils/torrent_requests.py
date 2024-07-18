import socket
from utils.torrent_info import TorrentFileInfo, TorrentInfo
from utils.utils import *

# region Response Status
ST_OK = 20
ST_NOTFOUND = 44
ST_BUSY = 45

def status_str(status: int):
    if status == ST_OK:
        return "OK"
    if status == ST_NOTFOUND:
        return "Not Found"
    if status == ST_BUSY:
        return "Busy"
# endregion

class TorrentRequest:
    # returns (Request, type of request)
    @staticmethod
    def recv_request(sock: socket.socket) -> tuple["TorrentRequest", int] | None:
        recv = sock.recv(1)
        
        if len(recv) == 0: 
            raise TorrentError(f"No request received.")
        
        request_type = int.from_bytes(recv)
        
        if request_type not in requests:
            raise TorrentError(f"Received invalid request {request_type}. Expected one of {requests.keys()}")
        
        try:
            return requests[request_type](sock)
        except BaseException as e:
            raise TorrentError(f"Failed to parse request {request_type}. Error: {e}")
        
    def to_bytes(self) -> bytes:
        raise TypeError()

class TorrentRequestWithID(TorrentRequest):
    def __init__(self, torrent_id: str) -> None:
        self.torrent_id = torrent_id
    
    @staticmethod
    def recv_request(sock: socket.socket, request_id: int) -> tuple["TorrentRequestWithID", int]:
        id_length = int.from_bytes(recv_all(sock, 4))
        id = recv_all(sock, id_length).decode()
        
        return TorrentRequestWithID(id), request_id
    
    def _to_bytes_internal(self, request_id: int) -> bytes:
        id = self.torrent_id.encode()
        id_len = len(id).to_bytes(4)
        
        return request_id.to_bytes() + id_len + id

class TorrentExistsRequest(TorrentRequestWithID):
    def __init__(self, torrent_id: str) -> None:
        super().__init__(torrent_id)
    
    @staticmethod
    def recv_request(sock: socket.socket) -> tuple["TorrentExistsRequest", int]:
        return TorrentRequestWithID.recv_request(sock, RT_TORRENT_EXISTS)
    
    def to_bytes(self) -> bytes:
        return self._to_bytes_internal(RT_TORRENT_EXISTS)

class GetTorrentRequest(TorrentRequestWithID):
    def __init__(self, torrent_id: str) -> None:
        super().__init__(torrent_id)
    
    @staticmethod
    def recv_request(sock: socket.socket) -> tuple["GetTorrentRequest", int]:
        return TorrentRequestWithID.recv_request(sock, RT_GET_TORRENT)
    
    def to_bytes(self) -> bytes:
        return self._to_bytes_internal(RT_GET_TORRENT)

class TorrentInfoResponse(TorrentRequest):
    def __init__(self, torrent_info: TorrentInfo) -> None:
        self.torrent_info = torrent_info

    @staticmethod
    def recv_request(sock: socket.socket) -> tuple["TorrentInfoResponse", int]:
        id_len = int.from_bytes(recv_all(sock, 4))
        
        id = recv_all(sock, id_len).decode()
        
        piece_size = int.from_bytes(recv_all(sock, 4))
        
        dirs_count = int.from_bytes(recv_all(sock, FILE_COUNT_B))
        
        dirs = []
        for _ in range(dirs_count):
            dir_len = int.from_bytes(recv_all(sock, 4))
            dirs.append(recv_all(sock, dir_len).decode())
        
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
        
        return TorrentInfoResponse(TorrentInfo(id, dirs, files, piece_size)), RT_TORRENT

    def to_bytes(self) -> bytes:
        id = self.torrent_info.id.encode()
        id_len = len(id).to_bytes(4)
        
        dirs_count = len(self.torrent_info.dirs).to_bytes(FILE_COUNT_B)
        dirs = b''.join(len(d).to_bytes(4) + d.encode() for d in self.torrent_info.dirs)
        
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
        
        return RT_TORRENT.to_bytes() + id_len + id + piece_size + dirs_count + dirs + files_count + files

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
    def __init__(self, dirs: list[str], files: list[TorrentFileInfo], piece_size: int):
        self.dirs = dirs
        self.files = files
        self.piece_size = piece_size
    
    @staticmethod
    def recv_request(sock: socket.socket) -> tuple["UploadTorrentRequest", int]:
        dirs_count = int.from_bytes(recv_all(sock, FILE_COUNT_B))
        dirs: list[str] = []
        
        for _ in range(dirs_count):
            dir_len = int.from_bytes(recv_all(sock, 4))
            dirs.append(recv_all(sock, dir_len).decode())
        
        files_count = int.from_bytes(recv_all(sock, FILE_COUNT_B))
        files: list[TorrentFileInfo] = []
        
        for _ in range(files_count):
            name_len = int.from_bytes(recv_all(sock, 4))
            name = recv_all(sock, name_len).decode()
            size = int.from_bytes(recv_all(sock, 8))
            pieces_count = int.from_bytes(recv_all(sock, PIECE_COUNT_B))
            
            pieces = []
            for _ in range(pieces_count):
                pieces.append(recv_all(sock, 32))
            
            files.append(TorrentFileInfo(name, size, pieces))
        
        piece_size = int.from_bytes(recv_all(sock, 4))
        
        return UploadTorrentRequest(dirs, files, piece_size), RT_UPLOAD_TORRENT
    
    def to_bytes(self) -> bytes:
        dirs_count = len(self.dirs).to_bytes(FILE_COUNT_B)
        dirs = b''.join(len(d).to_bytes(4) + d.encode() for d in self.dirs)
        
        files_count = len(self.files).to_bytes(FILE_COUNT_B)
        files = b''
        
        for file in self.files:
            name = file.file_name.encode()
            name_len = len(name).to_bytes(4)
            size = file.file_size.to_bytes(8)
            pieces_count = len(file.piece_hashes).to_bytes(PIECE_COUNT_B)
            pieces = b''.join(file.piece_hashes)
            
            files += name_len + name + size + pieces_count + pieces
        
        piece_size = self.piece_size.to_bytes(4)
        
        return RT_UPLOAD_TORRENT.to_bytes() + dirs_count + dirs + files_count + files + piece_size
    
class UploadTorrentResponse(TorrentRequestWithID):
    def __init__(self, torrent_id: str) -> None:
        super().__init__(torrent_id)
    
    @staticmethod
    def recv_request(sock: socket.socket) -> tuple["UploadTorrentResponse", int]:
        return TorrentRequestWithID.recv_request(sock, RT_UPLOAD_RESPONSE)
    
    def to_bytes(self) -> bytes:
        return self._to_bytes_internal(RT_UPLOAD_RESPONSE)

class StatusResponse(TorrentRequest):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
    
    @staticmethod
    def recv_request(sock: socket.socket) -> tuple["StatusResponse", int] | None:
        status = int.from_bytes(recv_all(sock, 1))
        return StatusResponse(status), RT_STATUS
    
    def to_bytes(self) -> bytes:
        return RT_STATUS.to_bytes() + self.status_code.to_bytes(1)

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

class RegisterAsPeerRequest(TorrentRequestWithID):
    def __init__(self, torrent_id: str) -> None:
        super().__init__(torrent_id)
    
    @staticmethod
    def recv_request(sock: socket.socket) -> tuple["RegisterAsPeerRequest", int]:
        return TorrentRequestWithID.recv_request(sock, RT_REGISTER_AS_PEER)
    
    def to_bytes(self) -> bytes:
        return self._to_bytes_internal(RT_REGISTER_AS_PEER)
    
class GetPeersRequest(TorrentRequestWithID):
    def __init__(self, torrent_id: str) -> None:
        super().__init__(torrent_id)
    
    @staticmethod
    def recv_request(sock: socket.socket) -> tuple["GetPeersRequest", int]:
        return TorrentRequestWithID.recv_request(sock, RT_GET_PEERS)
    
    def to_bytes(self) -> bytes:
        return self._to_bytes_internal(RT_GET_PEERS)
    
class GetPeersResponse(TorrentRequest):
    def __init__(self, peers: list[str]) -> None:
        self.peers = peers
    
    @staticmethod
    def recv_request(sock: socket.socket) -> tuple["GetPeersResponse", int] | None:
        peers_count = int.from_bytes(recv_all(sock, 4))
        peers = []
                
        for _ in range(peers_count):
            peers.append('.'.join(str(b) for b in recv_all(sock, 4)))
            
        return GetPeersResponse(peers), RT_GET_PEERS_RESPONSE
    
    def to_bytes(self) -> bytes:
        peers_count = len(self.peers).to_bytes(4)
        peers = b''.join(b''.join(int(section).to_bytes() for section in p.split('.')) for p in self.peers)
        
        return RT_GET_PEERS_RESPONSE.to_bytes() + peers_count + peers

class PieceDataResponse(TorrentRequest):
    def __init__(self, request_status: int, piece_bytes: bytes) -> None:
        self.request_status = request_status
        self.piece_bytes = piece_bytes
    
    @staticmethod
    def recv_request(sock: socket.socket) -> tuple["PieceDataResponse", int]:
        status = int.from_bytes(recv_all(sock, 1))
        piece_size = int.from_bytes(recv_all(sock, 4))
        if piece_size == 0:
            return PieceDataResponse(status, b''), RT_PIECE_DATA
        return PieceDataResponse(status, recv_all(sock, piece_size)), RT_PIECE_DATA
    
    def to_bytes(self) -> bytes:
        return RT_PIECE_DATA.to_bytes() + self.request_status.to_bytes() + len(self.piece_bytes).to_bytes(4) + self.piece_bytes

class PingRequest(TorrentRequest):
    @staticmethod
    def recv_request(sock: socket.socket) -> tuple["PingRequest", int] | None:
        return PingRequest(), RT_PING
    
    def to_bytes(self) -> bytes:
        return RT_PING.to_bytes()

# region Request IDs
RT_PING = 0
RT_STATUS = 1
RT_GET_TORRENT = 2
RT_TORRENT = 3
RT_DOWNLOAD_PIECE = 4
RT_UPLOAD_TORRENT = 5
RT_UPLOAD_RESPONSE = 6
RT_TORRENT_EXISTS = 7
RT_GET_PEERS = 8
RT_GET_PEERS_RESPONSE = 9
RT_LOGIN = 10
RT_LOGOUT = 11
RT_GET_CLIENT_TORRENTS = 12
RT_CLIENT_TORRENTS_RESPONSE = 13
RT_REGISTER_AS_PEER = 14
RT_PIECE_DATA = 15

requests = {
    RT_PING: PingRequest.recv_request,
    RT_STATUS: StatusResponse.recv_request,
    RT_GET_TORRENT: GetTorrentRequest.recv_request,
    RT_TORRENT: TorrentInfoResponse.recv_request,
    RT_DOWNLOAD_PIECE: DownloadPieceRequest.recv_request,
    RT_UPLOAD_TORRENT: UploadTorrentRequest.recv_request,
    RT_UPLOAD_RESPONSE: UploadTorrentResponse.recv_request,
    RT_TORRENT_EXISTS: TorrentExistsRequest.recv_request,
    RT_LOGIN: LoginRequest.recv_request,
    RT_LOGOUT: LogoutRequest.recv_request,
    RT_GET_CLIENT_TORRENTS: GetClientTorrentsRequest.recv_request,
    RT_CLIENT_TORRENTS_RESPONSE: ClientTorrentsResponse.recv_request,
    RT_REGISTER_AS_PEER: RegisterAsPeerRequest.recv_request,
    RT_GET_PEERS: GetPeersRequest.recv_request,
    RT_GET_PEERS_RESPONSE: GetPeersResponse.recv_request,
    RT_PIECE_DATA: PieceDataResponse.recv_request
}        
# endregion