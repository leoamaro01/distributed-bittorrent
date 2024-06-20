import socket
from threading import Thread
from utils.torrent_requests import *

class Torrent:
    def __init__(self, torrent_id: str, peers: list[str], files: list[str]) -> None:
        self.torrent_id = torrent_id
        self.peers = peers
        self.files = files

torrents: Torrent = [
    Torrent("torrent0", [
        "192.168.1.100",
        "2.3.4.5",
        "1.0.1.0",
        "255.255.255.255"
    ], [
        "folder1/file1.txt",
        "file2.txt",
        "folder1/folder2/file3.txt"
    ]),
    Torrent("torrent1", [
        "192.168.1.101",
        "10.0.0.1",
        "172.16.0.1"
    ], [
        "folder3/file4.txt",
        "file5.txt",
        "folder4/folder5/file6.txt"
    ]),
    Torrent("torrent3", [], []),
    Torrent("torrent2", [], [
        "folder6/file7.txt",
        "file8.txt",
        "folder7/folder8/file9.txt"
    ])
]

def handle_client(client: socket.socket):
    req, req_type = TorrentRequest.recv_request(client)
    
    if req_type == RT_GET_TORRENT:
        req: GetTorrentRequest
        
        matching_torrents = list(filter(lambda torrent: torrent.torrent_id == req.torrent_id, torrents))
        
        peers = []
        files = []
        
        if len(matching_torrents) != 0:
            peers = matching_torrents[0].peers
            files = matching_torrents[0].files
        
        client.sendall(TorrentInfoResponse(req.torrent_id, peers, files).to_bytes())
        
    client.close()    

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

sock.bind(('', 8080))
sock.listen(5)

while True:
    client, addr = sock.accept()
    
    print("got a connection")
    
    client_thread = Thread(target=handle_client, args=[client])
    client_thread.start()
    
