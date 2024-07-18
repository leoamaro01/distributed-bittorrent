from utils.utils import *

class TorrentFileInfo:
    def __init__(self, file_name: str, file_size: int, piece_hashes: list[bytes]):
        self.file_name = file_name
        self.file_size = file_size
        self.piece_hashes = piece_hashes
    
    def __str__(self) -> str:
        return f"File Name: {self.file_name}, File Size: {self.file_size}, Pieces : {len(self.piece_hashes)}"

class TorrentInfo:
    def __init__(self, id: str, dirs: list[str], files: list[TorrentFileInfo], piece_size: int) -> None:
        self.id = id
        self.files = files
        self.dirs = dirs
        self.piece_size = piece_size
    
    @staticmethod
    def invalid(id: str) -> "TorrentInfo":
        return TorrentInfo(id, [], [], 0)
    
    def is_invalid(self) -> bool:
        return self.files == [] and self.dirs == [] and self.piece_size == 0
    
    def __str__(self) -> str:
        files = "\n".join("\t" + str(f) for f in self.files)

        return f"""ID: {self.id}
Files:
{files}
Piece Size: {self.piece_size}"""
