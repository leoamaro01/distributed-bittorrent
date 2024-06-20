class TorrentFileInfo:
    def __init__(self, file_name: str, file_size: int, piece_hashes: list[bytes]):
        self.file_name = file_name
        self.file_size = file_size
        self.piece_hashes = piece_hashes

class TorrentPeer:
    def __init__(self, ip: str, pieces: dict[int, list[int]]) -> None:
        self.ip = ip
        self.pieces = pieces

class TorrentInfo:
    def __init__(self, id: str, peers: list[TorrentPeer], files: list[TorrentFileInfo], piece_size: int) -> None:
        self.id = id
        self.peers = peers
        self.files = files
        self.piece_size = piece_size
