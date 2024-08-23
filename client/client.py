from queue import SimpleQueue
import socket
import os
import math
import os.path as path
from ssl import SOCK_STREAM
from threading import Event, Lock, Thread
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
from typing import Callable
from utils.torrent_requests import *
import hashlib
import texts
from utils.utils import *
import time
import random
from sys import argv

# region Download Status


class FileDownloadStatus:
    def __init__(self):
        self.downloaded_parts: int = 0
        self.downloading_parts: list[tuple[int, float, str | None]] = []


class PartDownloadStatus:
    def __init__(self, file_index: int, part_index: int):
        self.file_index: int = file_index
        self.part_index: int = part_index
        self.source: str | None = None
        self.progress = 0


class TorrentDownloadStatus:
    def __init__(self, torrent_info: TorrentInfo) -> None:
        self.torrent_info: TorrentInfo = torrent_info
        self.finished: bool = False
        self.cancelled: bool = False
        self.exception: str | None = None
        self.status: str | None = None
        self.total_parts: int | None = None
        self.cancel_event: Event = Event()

        if torrent_info != None:
            self.total_parts = 0
            for file in torrent_info.files:
                self.total_parts += len(file.piece_hashes)

        # [(file index, part index, progress)]
        self.downloading_parts_progress: list[PartDownloadStatus] = []
        self.downloaded: list[PartDownloadStatus] = []

    def submit_download(self, file_index: int, part_index: int):
        self.downloading_parts_progress.append(
            PartDownloadStatus(file_index, part_index)
        )

    def __find_part(
        self, file_index: int, part_index: int, search_in_downloaded: bool = False
    ):
        for part in self.downloading_parts_progress:
            if part.file_index == file_index and part.part_index == part_index:
                return part
        if search_in_downloaded:
            for part in self.downloaded:
                if part.file_index == file_index and part.part_index == part_index:
                    return part
        return None

    def update_progress(self, file_index: int, part_index: int, progress: float):
        part = self.__find_part(file_index, part_index)

        if part == None:
            part = PartDownloadStatus(file_index, part_index)
            part.progress = progress
            self.downloading_parts_progress.append(part)
        else:
            part.progress = progress

    def set_download_source(self, file_index: int, part_index: int, source: str):
        part = self.__find_part(file_index, part_index)
        if part == None:
            part = PartDownloadStatus(file_index, part_index)
            part.source = source
            self.downloading_parts_progress.append(part)
        else:
            part.source = source

    def finish_download(self, file_index: int, part_index: int):
        part = self.__find_part(file_index, part_index)
        if part == None:
            part = PartDownloadStatus(file_index, part_index)
            part.progress = 1
            self.downloaded.append(part)
        else:
            part.progress = 1
            self.downloaded.append(part)
            self.downloading_parts_progress.remove(part)

    def cancel_download(self, file_index: int, part_index: int):
        part = self.__find_part(file_index, part_index, True)
        if part == None:
            return

        if part in self.downloading_parts_progress:
            self.downloading_parts_progress.remove(part)
        else:
            self.downloaded.remove(part)

    def set_error(self, error_message: str):
        self.exception = error_message

    def end(self):
        self.finished = True

    def cancel(self):
        self.cancelled = True

    def set_status(self, status: str | None):
        self.status = status

    def download_progress(self) -> float:
        if self.total_parts == None:
            if self.torrent_info == None:
                return 0
            else:
                self.total_parts = 0
                for file in self.torrent_info.files:
                    self.total_parts += len(file.piece_hashes)

        return float(len(self.downloaded)) / self.total_parts

    def __str__(self) -> str:
        if self.cancelled:
            text = f"Cancelled"

            if self.exception != None:
                text += f" with error:\n{self.exception}"

            return text

        if self.finished:
            return f"Download of {self.torrent_info.id} finished successfully."

        if self.exception != None:
            return f"Download of {self.torrent_info.id} failed with exception:\n{self.exception}"

        result = ""

        if self.cancel_event.is_set():
            result += "Cancelling...\n\n"

        if self.status != None:
            result += self.status + "\n\n"

        if self.torrent_info == None:
            return result

        total_download_progress = self.download_progress()

        file_statuses: dict[int, FileDownloadStatus] = {}

        for d in self.downloading_parts_progress:
            if d.file_index not in file_statuses:
                file_statuses[d.file_index] = FileDownloadStatus()

            file_statuses[d.file_index].downloading_parts.append(
                (d.part_index, d.progress, d.source)
            )

        # for d in self.downloaded:
        #     if d.file_index not in file_statuses:
        #         file_statuses[d.file_index] = FileDownloadStatus()

        #     file_statuses[d.file_index].downloaded_parts += 1

        def get_progress_bar(progress: float):
            full_segments = math.floor(progress * 10)
            percentage = math.floor(progress * 1000) / float(10)

            return (
                "["
                + "●" * full_segments
                + "◯" * (10 - full_segments)
                + f"] ({percentage}%)"
            )

        result += f"Downloading {self.torrent_info.id} {get_progress_bar(total_download_progress)}\n"

        for f in file_statuses:
            info = self.torrent_info.files[f]

            if file_statuses[f].downloaded_parts == len(info.piece_hashes):
                continue

            progress = float(file_statuses[f].downloaded_parts) / len(info.piece_hashes)
            downloaded_mb = (
                file_statuses[f].downloaded_parts
                * self.torrent_info.piece_size
                / float(1000000)
            )
            total_mb = info.file_size / float(1000000)

            result += f"\nFile {info.file_name} {get_progress_bar(progress)} {downloaded_mb}/{total_mb}MB\n"

            for part, part_progress, source in file_statuses[f].downloading_parts:
                result += (
                    f"\tPart {part} {get_progress_bar(part_progress)}"
                    + (f" from {source}" if source != None else "")
                    + "\n"
                )

        return result


# endregion

# region Constants

CLIENT_CONNECTION_PORT = 7010

PIECE_TRANSFER_TIMEOUT = 15

DOWNLOAD_LOG_TIMESTEP = 0.5

MIN_KNOWN_SERVERS = 20

DISCOVERY_TIMEOUT = 5

DOWNLOAD_CHUNKS = 10

UPLOAD_MAX = 40

# endregion

# region Data

open_files_locks: dict[str, Lock] = {}
open_files_locks_lock: Lock = Lock()

# Torrent ID : [ Paths ]
available_seeds: dict[str, list[str]] = {}
seeds_lock: Lock = Lock()

last_checkup = None
last_checkup_lock: Lock = Lock()

exit_event: Event = Event()

download_statuses: dict[str, TorrentDownloadStatus] = {}
download_statuses_lock: Lock = Lock()

uploading_count: int = 0
uploading_count_lock: Lock = Lock()

leave_download_event: Event = Event()

torrent_infos: dict[str, TorrentInfo] = {}
torrent_infos_lock: Lock = Lock()

connected_to_server: Event = Event()

known_servers: list[str] = []
known_servers_lock: Lock = Lock()

# endregion

# region Logging
log_queue: SimpleQueue = SimpleQueue()


def log(msg: str, category: str | None = None):
    time_str = time.strftime("%Y-%m-%d %H:%M:%S")
    category_str = f"({category}) " if category != None else ""
    log_queue.put(f"[{time_str}] {category_str}{msg}\n")


def logger():
    os.makedirs("logs", exist_ok=True)
    time_str = time.strftime("%Y-%m-%dT%H-%M-%S")
    log_file = path.join("logs", f"{time_str}.log")
    while True:
        item = log_queue.get()

        with open(log_file, "a") as f:
            f.write(item)


# endregion

# region Torrent Upload


def upload_torrent(torrent_path: str) -> bool:
    if not path.exists(torrent_path):
        print(f"Path {torrent_path} does not exist")
        return False

    log(f"Starting upload of torrent in {torrent_path}", "UPLOAD TORRENT")

    # [(file_name, file_size, [piece_hashes])]
    files: list[TorrentFileInfo] = []
    dirs: list[str] = []

    print("Uploading torrent...")
    print("Generating piece hashes...")

    if path.isfile(torrent_path):
        open_file_with_lock(torrent_path)

        piece_hashes = []

        try:
            with open(file_path, "rb") as f:
                while True:
                    piece = f.read(PIECE_SIZE)

                    if len(piece) == 0:
                        break

                    piece_hashes.append(calculate_piece_hash(piece))
        except:
            log(f"Failed to read file {torrent_path}", "UPLOAD TORRENT")
            print(f"Failed to read file {torrent_path}")
            return False
        finally:
            close_file_with_lock(torrent_path)

        files.append(
            TorrentFileInfo(
                path.basename(torrent_path), path.getsize(torrent_path), piece_hashes
            )
        )
    else:
        for root, _, file_names in os.walk(torrent_path, topdown=False):
            # Gets the directory name relative to the torrent path.
            # e.g. if torrent path is /app/data/MyTorrent and the root is /app/data/MyTorrent/MyFolder,
            # then the result will be MyTorrent/MyFolder, which is the format expected from TorrentInfo
            dirname = path.join(
                path.basename(torrent_path),
                root[(len(path.normpath(torrent_path)) + 1) :],
            )

            # This makes sure parent directories aren't added if one of their childs is already in the list.
            # Children are always added first because of the topdown=False parameter in os.walk
            found_child = False
            for dir in dirs:
                if dir.startswith(dirname):
                    found_child = True
                    break
            if not found_child:
                dirs.append(dirname)

            for file_name in file_names:
                file_path = path.join(root, file_name)

                # Gets file name relative to the torrent path.
                # e.g. if torrent path is /app/data/MyTorrent and the file path is /app/data/MyTorrent/MyFolder/MyFile.txt,
                # then the result will be MyTorrent/MyFolder/MyFile.txt, which is the format expected from TorrentInfo
                local_torrent_file_name = path.join(
                    path.basename(torrent_path),
                    file_path[(len(path.normpath(torrent_path)) + 1) :],
                )

                log(
                    f"Generating piece hashes for file {local_torrent_file_name}",
                    "UPLOAD TORRENT",
                )
                print(f"Generating piece hashes for file {local_torrent_file_name}")

                file_size = path.getsize(file_path)

                open_file_with_lock(file_path)

                piece_hashes = []

                try:
                    with open(file_path, "rb") as f:
                        while True:
                            piece = f.read(PIECE_SIZE)

                            if len(piece) == 0:
                                break

                            piece_hashes.append(calculate_piece_hash(piece))
                except:
                    log(f"Failed to read file {file_path}", "UPLOAD TORRENT")
                    print(f"Failed to read file {file_path}")
                    return False
                finally:
                    close_file_with_lock(file_path)

                files.append(
                    TorrentFileInfo(local_torrent_file_name, file_size, piece_hashes)
                )

    log(
        "Finished generating piece hashes, uploading torrent info to tracker...",
        "UPLOAD TORRENT",
    )
    print("Uploading torrent info...")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if not connect_socket_to_server(sock):
            log("Failed to connect to the server", "UPLOAD TORRENT")
            print("Failed to connect to the server. Check your network connection.")
            return False

        sock.settimeout(PIECE_TRANSFER_TIMEOUT)

        try:
            sock.sendall(UploadTorrentRequest(dirs, files, PIECE_SIZE).to_bytes())
        except:
            log("Failed to upload torrent info to the server", "UPLOAD TORRENT")
            print(
                "Failed to upload torrent info to the server. Check your network connection."
            )
            return False

        sock.settimeout(socket.getdefaulttimeout())

        try:
            req, req_type = TorrentRequest.recv_request(sock)
        except:
            log("Failed to get response from the server", "UPLOAD TORRENT")
            print(
                "Failed to get response from the server. Check your network connection."
            )
            return False

        if req_type != RT_UPLOAD_RESPONSE:
            log(
                f"Unexpected Response from tracker, expected {RT_UPLOAD_RESPONSE}, received {req_type}",
                "UPLOAD TORRENT",
            )
            print(
                f"Unexpected Response from tracker, expected {RT_UPLOAD_RESPONSE}, received {req_type}."
            )
            return False

        req: UploadTorrentResponse

        log(
            f"Torrent uploaded successfully. Torrent ID: {req.torrent_id}",
            "UPLOAD TORRENT",
        )
        print(f"Torrent uploaded successfully. Torrent ID: {req.torrent_id}")

        add_path_to_seeds(req.torrent_id, path.dirname(torrent_path))

        with torrent_infos_lock:
            torrent_infos[req.torrent_id] = TorrentInfo(
                req.torrent_id, dirs, files, PIECE_SIZE
            )
            update_config_file("torrent_infos_cache.bin", torrent_infos)


# endregion

# region Torrent Download


def download_torrent(torrent_id: str):
    # region Download preparations
    log("Starting download", f"DOWNLOAD {torrent_id}")

    with download_statuses_lock:
        download_statuses[torrent_id] = TorrentDownloadStatus(None)
        download_statuses[torrent_id].set_status("Getting torrent info...")

    try:
        info = get_torrent_info(torrent_id, True)
    except TorrentError as e:
        log("Failed to get torrent info. Exiting...", f"DOWNLOAD {torrent_id}")
        with download_statuses_lock:
            download_statuses[torrent_id].set_error(
                f"Error while getting torrent info on {torrent_id}.\nError:\n\t{e.message}"
            )
            download_statuses[torrent_id].cancel()
        return

    if info.is_invalid():
        log("Could not find torrent info. Exiting...", f"DOWNLOAD {torrent_id}")
        with download_statuses_lock:
            download_statuses[torrent_id].set_error(
                f"Error while downloading torrent {torrent_id}. Not found."
            )
            download_statuses[torrent_id].cancel()
        return

    with download_statuses_lock:
        download_statuses[torrent_id].torrent_info = info
        download_statuses[torrent_id].set_status("Creating directory structure...")

    log("Creating torrent directory structure...", f"DOWNLOAD {torrent_id}")

    os.makedirs(path.join("downloads", torrent_id), exist_ok=True)

    for dir in info.dirs:
        os.makedirs(path.join("downloads", torrent_id, dir), exist_ok=True)

    downloaded: dict[int, list[int]] = {}

    partial_torrent_path = f"downloads/.partial/{torrent_id}"

    if path.exists(partial_torrent_path):
        log("Loading existing partial files...", f"DOWNLOAD {torrent_id}")
        with download_statuses_lock:
            download_statuses[torrent_id].set_status(
                "Loading existing partial files..."
            )

        try:
            files_folders = os.listdir(partial_torrent_path)
        except BaseException as e:
            log(
                f"Failed to load existing partial files. Error: {e}. Exiting...",
                f"DOWNLOAD {torrent_id}",
            )
            with download_statuses_lock:
                download_statuses[torrent_id].set_error(
                    f"Failed to load existing partial files. Error: {e}"
                )
                download_statuses[torrent_id].cancel()
            return

        for file_folder in files_folders:
            try:
                index = int(file_folder, base=16)
            except:
                continue

            if index >= len(info.files) or index < 0:
                continue

            file_folder_path = path.join(partial_torrent_path, file_folder)

            if not path.isdir(file_folder_path):
                continue

            try:
                piece_files = os.listdir(file_folder_path)
            except:
                continue

            downloaded[index] = []

            for piece_file in piece_files:
                try:
                    piece_index = int(piece_file, base=16)
                except:
                    continue

                if (
                    piece_index >= len(info.files[index].piece_hashes)
                    or piece_index < 0
                ):
                    continue

                piece_path = path.join(file_folder_path, piece_file)

                if not path.isfile(piece_path):
                    continue

                if path.getsize(piece_path) != get_piece_size(info, index, piece_index):
                    try:
                        os.remove(piece_path)
                    except:
                        log(
                            f"Error trying to remove invalid piece file {piece_path}",
                            f"DOWNLOAD {torrent_id}",
                        )
                    continue

                piece_hash = info.files[index].piece_hashes[piece_index]

                open_file_with_lock(piece_path)

                try:
                    with open(piece_path, "rb") as f:
                        piece_contents = f.read()
                except:
                    log(
                        f"Error trying to read piece file {piece_path}",
                        f"DOWNLOAD {torrent_id}",
                    )
                    try:
                        os.remove(piece_path)
                    except:
                        log(
                            f"Error trying to remove invalid piece file {piece_path}",
                            f"DOWNLOAD {torrent_id}",
                        )
                    continue
                finally:
                    close_file_with_lock(piece_path)

                if not check_piece_hash(piece_contents, piece_hash):
                    try:
                        os.remove(piece_path)
                    except:
                        log(
                            f"Error trying to remove invalid piece file {piece_path}",
                            f"DOWNLOAD {torrent_id}",
                        )
                    continue

                downloaded[index].append(piece_index)

                with download_statuses_lock:
                    download_statuses[torrent_id].finish_download(index, piece_index)
    else:
        os.makedirs(partial_torrent_path, exist_ok=True)

    downloaded_torrent_path = f"downloads/{torrent_id}"

    log("Checking existing files...", f"DOWNLOAD {torrent_id}")
    with download_statuses_lock:
        download_statuses[torrent_id].set_status("Checking existing files...")

    for i in range(len(info.files)):
        if check_file(info, i):
            downloaded[i] = [j for j in range(len(info.files[i].piece_hashes))]
            with download_statuses_lock:
                for j in range(len(info.files[i].piece_hashes)):
                    download_statuses[torrent_id].finish_download(i, j)

    log("Registering as seeder...", f"DOWNLOAD {torrent_id}")
    with download_statuses_lock:
        download_statuses[torrent_id].set_status("Registering as seeder...")

    add_path_to_seeds(torrent_id, partial_torrent_path)
    add_path_to_seeds(torrent_id, downloaded_torrent_path)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if not connect_socket_to_server(
            sock, True
        ):  # this only returns false if the exit event was set
            return
        try:
            sock.sendall(RegisterAsPeerRequest(torrent_id).to_bytes())
        except BaseException as e:
            log(
                f"Failed to register as seeder. Error: {e}. Exiting...",
                f"DOWNLOAD {torrent_id}",
            )
            with download_statuses_lock:
                download_statuses[torrent_id].set_error(
                    f"Failed to register as seeder. Error: {e}"
                )
                download_statuses[torrent_id].cancel()
            return

    log("Gathering pending files...", f"DOWNLOAD {torrent_id}")
    with download_statuses_lock:
        download_statuses[torrent_id].set_status("Gathering pending files...")

    pending_download_files: dict[int, list[int]] = {}
    pending_download_pieces: list[tuple[int, int]] = []

    for file_index, file in enumerate(info.files):
        pieces = range(len(file.piece_hashes))
        for piece in pieces:
            if file_index in downloaded and piece in downloaded[file_index]:
                continue

            pending_download_pieces.append((file_index, piece))

            if file_index in pending_download_files:
                pending_download_files[file_index].append(piece)
            else:
                pending_download_files[file_index] = [piece]
    # endregion

    # Download Loop
    while not exit_event.is_set():
        with download_statuses_lock:
            if download_statuses[torrent_id].cancel_event.is_set():
                download_statuses[torrent_id].cancel()
                break

        with download_statuses_lock:
            download_statuses[torrent_id].set_status("Getting peers from tracker...")

        peers = get_torrent_peers(torrent_id)

        if len(peers) == 0:
            with download_statuses_lock:
                download_statuses[torrent_id].set_status(
                    f"No peers found. Retrying in 5 seconds..."
                )
            time.sleep(5)
            continue

        with known_servers_lock:
            low_servers = len(known_servers) < MIN_KNOWN_SERVERS

        for p in peers:
            with known_servers_lock:
                if len(known_servers) >= MIN_KNOWN_SERVERS:
                    break
            learn_servers_from_peer(p)

        random.shuffle(pending_download_pieces)

        if len(pending_download_pieces) > DOWNLOAD_CHUNKS:
            downloads = pending_download_pieces[:DOWNLOAD_CHUNKS]

        with download_statuses_lock:
            download_statuses[torrent_id].set_status("Downloading...")

        exited = False

        with ThreadPoolExecutor() as executor:
            futures = [
                executor.submit(download_file_piece, info, file, piece, peers)
                for file, piece in downloads
            ]

            for future in as_completed(futures):
                if not exited:
                    with download_statuses_lock:
                        cancelled = download_statuses[torrent_id].cancel_event.is_set()
                    if cancelled or exit_event.is_set():
                        exited = True
                        executor.shutdown(wait=False, cancel_futures=True)

                if future.cancelled():
                    continue

                try:
                    result = future.result()
                except BaseException as e:
                    log(
                        f"Error downloading a piece. Error: {e}",
                        f"DOWNLOAD {torrent_id}",
                    )
                    with download_statuses_lock:
                        download_statuses[torrent_id].set_error(
                            f"Error downloading a piece. Error: {e}"
                        )
                        download_statuses[torrent_id].cancel_event.set()
                    executor.shutdown(wait=False, cancel_futures=True)
                    exited = True
                    continue

                if result == None:
                    continue

                file, piece = result

                pending_download_pieces.remove((file, piece))
                pending_download_files[file].remove(piece)

                if file in downloaded:
                    downloaded[file].append(piece)
                else:
                    downloaded[file] = [piece]

        if len(pending_download_files) == 0:
            with download_statuses_lock:
                download_statuses[torrent_id].set_status(
                    "Download finished. Reconstructing files..."
                )

            log(
                f"Download finished, reconstructing files...",
                category=f"DOWNLOAD {torrent_id}",
            )

            any_failed = False

            # region File reconstruction
            for file in range(len(info.files)):
                if exited:
                    any_failed = True
                    break

                pending_download_files.pop(file)

                log(
                    f"Reconstructing {file}...",
                    category=f"DOWNLOAD {torrent_id}",
                )

                file_name = path.join(
                    f"downloads/{torrent_id}", info.files[file].file_name
                )

                os.makedirs(path.dirname(file_name), exist_ok=True)

                failed = False
                closed = False

                open_file_with_lock(file_name)

                try:
                    fl = open(file_name, "wb")
                except BaseException as e:
                    log(
                        f"Failed to open file, cancelling reconstruction... Error: {e}",
                        f"DOWNLOAD {torrent_id}",
                    )
                    with download_statuses_lock:
                        download_statuses[torrent_id].set_error(
                            f"Failed to open file {file_name}. Error: {e}"
                        )
                        download_statuses[torrent_id].cancel_event.set()
                    exited = True
                    close_file_with_lock(file_name)
                    continue

                file_hex = get_hex(file)

                def fail_func(piece_index: int):
                    pending_download_pieces.append((file, piece_index))
                    if file in pending_download_files:
                        pending_download_files[file].append(piece_index)
                    else:
                        pending_download_files[file] = [piece_index]
                    downloaded[file].remove(piece_index)
                    with download_statuses_lock:
                        download_statuses[torrent_id].cancel_download(file, piece_index)

                for piece_index in range(len(info.files[file].piece_hashes)):
                    piece_hex = get_hex(piece_index)
                    piece_path = (
                        f"downloads/.partial/{torrent_id}/{file_hex}/{piece_hex}"
                    )

                    if not path.isfile(piece_path):
                        log(
                            f"Found missing piece file ({file}, {piece_index}), should be at {piece_path}. Resubmitting to download queue...",
                            f"DOWNLOAD {torrent_id}",
                        )
                        failed = True
                        fail_func(piece_index)
                        continue

                    if path.getsize(piece_path) > PIECE_SIZE:
                        log(
                            f"Found piece file ({file}, {piece_index}) at {piece_path} with invalid size. Resubmitting to download queue...",
                            f"DOWNLOAD {torrent_id}",
                        )
                        try:
                            os.remove(piece_path)
                        except:
                            log(
                                f"Failed to remove piece file {piece_path}.",
                                f"DOWNLOAD {torrent_id}",
                            )
                        failed = True
                        fail_func(piece_index)
                        continue

                    open_file_with_lock(piece_path)
                    try:
                        with open(piece_path, "rb") as f:
                            piece_data = f.read()
                    except:
                        log(
                            f"Failed to read piece file {piece_path}. Resubmitting to download queue...",
                            f"DOWNLOAD {torrent_id}",
                        )
                        try:
                            os.remove(piece_path)
                        except:
                            log(
                                f"Failed to remove piece file {piece_path}.",
                                f"DOWNLOAD {torrent_id}",
                            )
                        failed = True
                        fail_func(piece_index)
                        continue
                    finally:
                        close_file_with_lock(piece_path)

                    if not check_piece_hash(
                        piece_data, info.files[file].piece_hashes[piece_index]
                    ):
                        log(
                            f"Piece file {piece_path} has invalid hash. Resubmitting to download queue...",
                            f"DOWNLOAD {torrent_id}",
                        )
                        try:
                            os.remove(piece_path)
                        except:
                            log(
                                f"Failed to remove piece file {piece_path}.",
                                f"DOWNLOAD {torrent_id}",
                            )
                        failed = True
                        fail_func(piece_index)
                        continue

                    if not failed:
                        fl.write(piece_data)
                    elif not closed:
                        closed = True
                        fl.close()
                        close_file_with_lock(file_name)

                if not closed:
                    fl.close()
                    close_file_with_lock(file_name)

                if failed:
                    log(
                        f"Some parts of file {file} couldn't be veryfied to be correct and will be downloaded again. File was not reconstructed.",
                        f"DOWNLOAD {torrent_id}",
                    )
                    any_failed = True

                    continue

                if any_failed:
                    continue

                log(
                    f"File {file} reconstructed. Removing partial files.",
                    f"DOWNLOAD {torrent_id}",
                )

                # remove partial piece files
                for piece_index in range(len(info.files[file].piece_hashes)):
                    piece_hex = get_hex(piece_index)
                    p_path = f"downloads/.partial/{torrent_id}/{file_hex}/{piece_hex}"
                    open_file_with_lock(p_path)
                    try:
                        os.remove(p_path)
                    except:
                        log(
                            f"Failed to remove piece file {p_path}. Continuing...",
                            f"DOWNLOAD {torrent_id}",
                        )
                        continue
                    finally:
                        close_file_with_lock(p_path)

                log(
                    f"Partial files for file {file} removed.",
                    f"DOWNLOAD {torrent_id}",
                )

                # remove partial file folder
                try:
                    fldr = f"downloads/.partial/{torrent_id}/{file_hex}"
                    os.rmdir(fldr)
                except:
                    log(
                        f"Failed to remove partial files folder {fldr}. Continuing...",
                        f"DOWNLOAD {torrent_id}",
                    )
            # endregion

            if any_failed:
                log(
                    f"Reconstruction of some files failed. Restarting download of failed files...",
                    f"DOWNLOAD {torrent_id}",
                )
                continue

            with download_statuses_lock:
                download_statuses[torrent_id].end()

            remove_path_from_seeds(torrent_id, partial_torrent_path)

            try:
                os.rmdir(f"downloads/.partial/{torrent_id}")
            except OSError:
                log(
                    f"Failed to remove partial download folder downloads/.partial/{torrent_id}",
                    f"DOWNLOAD {torrent_id}",
                )
            break

        if exited:
            with download_statuses_lock:
                download_statuses[torrent_id].cancel()
            break


def download_file_piece(
    torrent_info: TorrentInfo, file_index: int, piece_index: int, peers: list[str]
) -> tuple[int, int] | None:
    with download_statuses_lock:
        download_statuses[torrent_info.id].submit_download(file_index, piece_index)

    randomized_peers = peers.copy()
    random.shuffle(randomized_peers)

    for peer in randomized_peers:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.connect((peer, CLIENT_CONNECTION_PORT))
            except:
                continue

            with download_statuses_lock:
                download_statuses[torrent_info.id].set_download_source(
                    file_index, piece_index, peer
                )

            try:
                sock.sendall(
                    DownloadPieceRequest(
                        torrent_info.id, file_index, piece_index
                    ).to_bytes()
                )
            except BaseException as e:
                with download_statuses_lock:
                    download_statuses[torrent_info.id].set_download_source(
                        file_index, piece_index, None
                    )
                continue

            sock.settimeout(PIECE_TRANSFER_TIMEOUT)

            try:
                req, req_type = TorrentRequest.recv_request(sock)
            except BaseException as e:
                with download_statuses_lock:
                    download_statuses[torrent_info.id].set_download_source(
                        file_index, piece_index, None
                    )
                continue

        if req_type != RT_PIECE_DATA:
            with download_statuses_lock:
                download_statuses[torrent_info.id].set_download_source(
                    file_index, piece_index, None
                )
            continue

        req: PieceDataResponse

        if req.request_status != ST_OK or req.piece_bytes == b"":
            with download_statuses_lock:
                download_statuses[torrent_info.id].set_download_source(
                    file_index, piece_index, None
                )
            continue

        if not check_piece_hash(
            req.piece_bytes, torrent_info.files[file_index].piece_hashes[piece_index]
        ):
            with download_statuses_lock:
                download_statuses[torrent_info.id].set_download_source(
                    file_index, piece_index, None
                )
            continue

        piece_path = f"downloads/.partial/{torrent_info.id}/{get_hex(file_index)}/{get_hex(piece_index)}"
        os.makedirs(path.dirname(piece_path), exist_ok=True)

        open_file_with_lock(piece_path)

        try:
            with open(piece_path, "wb") as piece_file:
                piece_file.write(req.piece_bytes)
        except BaseException as e:
            with download_statuses_lock:
                download_statuses[torrent_info.id].cancel_download(
                    file_index, piece_index
                )
            return None
        finally:
            close_file_with_lock(piece_path)

        with download_statuses_lock:
            download_statuses[torrent_info.id].finish_download(file_index, piece_index)

        return file_index, piece_index

    with download_statuses_lock:
        download_statuses[torrent_info.id].cancel_download(file_index, piece_index)

    return None


# endregion

# region Utilities


def learn_servers_from_peer(peer: str):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.connect((peer, CLIENT_CONNECTION_PORT))

            sock.sendall(GetServersRequest().to_bytes())
            req, req_type = TorrentRequest.recv_request(sock)

            if req_type == RT_SERVERS_RESPONSE:
                req: ServersResponse

                with known_servers_lock:
                    for ip in req.server_ips:
                        if ip not in known_servers:
                            known_servers.append(ip)
        except:
            pass


def connect_socket_to_server(
    socket: socket.socket, block_on_disconnect: bool = False
) -> bool:
    if exit_event.is_set():
        return False

    if not connected_to_server.is_set():
        if block_on_disconnect:
            while not exit_event.is_set():
                if connected_to_server.wait(5):
                    break
            if exit_event.is_set():
                return False
        else:
            return False

    with known_servers_lock:
        servers = known_servers.copy()

    socket.settimeout(1)

    for server in servers:
        try:
            socket.connect((server, SERVER_PORT))
        except:
            with known_servers_lock:
                if server in known_servers:
                    known_servers.remove(server)
            continue
        else:
            return True

    if not connected_to_server.is_set():
        if block_on_disconnect:
            while not exit_event.is_set():
                if connected_to_server.wait(5):
                    break
            return connect_socket_to_server(socket, True)
        else:
            return False

    if not block_on_disconnect:
        return False

    connected_to_server.clear()

    while not exit_event.is_set():
        servers = discover_servers()

        if len(servers) > 0:
            servers_cp = servers.copy()

            connected = False
            for server in servers_cp:
                try:
                    log(f"Trying to connect to server {server}...", "SERVER CONNECTION")
                    socket.connect((server, SERVER_PORT))
                    log("Connected to server", "SERVER CONNECTION")
                    with known_servers_lock:
                        known_servers.extend(servers)
                    connected = True
                    break
                except:
                    servers.remove(server)

            if connected:
                break

        log(
            "Failed to connect to servers. Trying again in 5 seconds...",
            "SERVER CONNECTION",
        )
        time.sleep(5)
        continue

    if exit_event.is_set():
        return False

    connected_to_server.set()

    return True


def update_seeds_validity():
    with seeds_lock:
        changed = False
        torrents = available_seeds.keys()

        for torrent in torrents:
            paths = available_seeds[torrent].copy()
            for p in paths:
                if not path.exists(p):
                    available_seeds[torrent].remove(p)
                    changed = True
            if available_seeds[torrent] == []:
                changed = True
                available_seeds.pop(torrent)

        if changed:
            update_config_file("available_seeds.bin", available_seeds)


def get_piece_size(torrent_info: TorrentInfo, file_index, piece_index: int):
    file_info: TorrentFileInfo = torrent_info.files[file_index]
    piece_size = torrent_info.piece_size

    piece_count = len(file_info.piece_hashes)

    if piece_index == piece_count - 1:
        piece_size = file_info.file_size - (piece_count - 1) * piece_size

    return piece_size


def calculate_piece_hash(piece: bytes) -> bytes:
    return hashlib.sha256(piece).digest()


def check_piece_hash(piece: bytes, hash: bytes) -> bool:
    return calculate_piece_hash(piece) == hash


def check_file(
    torrent_info: TorrentInfo, file_index: int, file_path: str = None
) -> bool:
    file_info: TorrentFileInfo = torrent_info.files[file_index]

    if file_path == None:
        file_path = path.join(f"downloads/{torrent_info.id}", file_info.file_name)

    if not path.isfile(file_path):
        return False

    if path.getsize(file_path) != file_info.file_size:
        return False

    open_file_with_lock(file_path)

    try:
        with open(file_path, "rb") as file:
            pieces = len(file_info.piece_hashes)

            for i in range(pieces):
                piece_size = get_piece_size(torrent_info, file_index, i)

                piece = file.read(piece_size)

                if not check_piece_hash(piece, file_info.piece_hashes[i]):
                    return False
    except BaseException as e:
        log(
            f"Error while checking file integrity for {file_path}. Error: {e}",
            "CHECK FILE",
        )
        return False
    finally:
        close_file_with_lock(file_path)

    return True


def update_download_progress(
    torrent_id: str, file_index: int, piece_index: int, progress: float
):
    with download_statuses_lock:
        download_statuses[torrent_id].update_progress(file_index, piece_index, progress)


def get_torrent_info(torrent_id: str, block_on_disconnect: bool = False) -> TorrentInfo:
    if exit_event.is_set():
        return TorrentInfo.invalid(torrent_id)

    with torrent_infos_lock:
        if torrent_id in torrent_infos:
            return torrent_infos[torrent_id]

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if not connect_socket_to_server(sock, block_on_disconnect):
            return TorrentInfo.invalid(torrent_id)

        try:
            sock.sendall(GetTorrentRequest(torrent_id).to_bytes())
        except:
            if block_on_disconnect:
                log(
                    "Failed to send torrent request to the server after a successful connection. Retrying...",
                    "GET TORRENT INFO",
                )
                return get_torrent_info(torrent_id, True)
            else:
                return TorrentInfo.invalid(torrent_id)

        sock.settimeout(PIECE_TRANSFER_TIMEOUT)

        try:
            req, req_type = TorrentRequest.recv_request(sock)
        except BaseException as e:
            if block_on_disconnect:
                log(
                    f"Failed to get torrent info after a successful connection to the server. Error: {e}. Retrying...",
                    "GET TORRENT INFO",
                )
                return get_torrent_info(torrent_id, True)
            else:
                return TorrentInfo.invalid(torrent_id)

    if req_type != RT_TORRENT:
        if block_on_disconnect:
            log(
                f"Unexpected Response from tracker, expected {RT_TORRENT}, received {req_type}. Retrying...",
                "GET TORRENT INFO",
            )
            return get_torrent_info(torrent_id, True)
        else:
            return TorrentInfo.invalid(torrent_id)

    req: TorrentInfoResponse

    with torrent_infos_lock:
        torrent_infos[torrent_id] = req.torrent_info

        update_config_file("torrent_infos_cache.bin", torrent_infos)

    return req.torrent_info


def get_torrent_peers(torrent_id: str, block_on_disconnect: bool = False) -> list[str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if not connect_socket_to_server(sock, block_on_disconnect):
            return []

        try:
            sock.sendall(GetPeersRequest(torrent_id).to_bytes())
        except:
            if block_on_disconnect:
                log(
                    "Failed to send request to the server after a successful connection. Retrying...",
                    "GET PEERS",
                )
                return get_torrent_peers(torrent_id, True)
            else:
                return []

        try:
            req, req_type = TorrentRequest.recv_request(sock)
        except:
            if block_on_disconnect:
                log(
                    "Failed to get peers after a successful connection to the server. Retrying...",
                    "GET PEERS",
                )
                return get_torrent_peers(torrent_id, True)
            else:
                return []

    if req_type != RT_GET_PEERS_RESPONSE:
        if block_on_disconnect:
            log(
                f"Unexpected Response from tracker, expected {RT_GET_PEERS_RESPONSE}, received {req_type}. Retrying...",
                "GET PEERS",
            )
            return get_torrent_peers(torrent_id, True)
        else:
            return []

    req: GetPeersResponse

    return req.peers


def add_path_to_seeds(torrent_id: str, path: str):
    with seeds_lock:
        if torrent_id in available_seeds:
            if path not in available_seeds[torrent_id]:
                available_seeds[torrent_id].append(path)
        else:
            available_seeds[torrent_id] = [path]

        update_config_file("available_seeds.bin", available_seeds)


def remove_path_from_seeds(torrent_id: str, path: str):
    with seeds_lock:
        if torrent_id in available_seeds:
            if path in available_seeds[torrent_id]:
                available_seeds[torrent_id].remove(path)
                if available_seeds[torrent_id] == []:
                    available_seeds.pop(torrent_id)

        update_config_file("available_seeds.bin", available_seeds)


def open_file_with_lock(file_path):
    with open_files_locks_lock:
        if file_path in open_files_locks:
            lock = open_files_locks[file_path]
        else:
            lock = Lock()
            open_files_locks[file_path] = lock

    lock.acquire()


def close_file_with_lock(file_path):
    with open_files_locks_lock:
        if file_path not in open_files_locks:
            raise TorrentError(f"File {file_path} is not locked")

        lock = open_files_locks[file_path]

    lock.release()


def load_config():
    available_seeds = load_config_file("available_seeds.bin")
    torrent_infos = load_config_file("torrent_infos_cache.bin")


# endregion

# region Seeding


def seeder_thread():
    seeder_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    seeder_socket.bind(("", CLIENT_CONNECTION_PORT))
    seeder_socket.listen()

    while not exit_event.is_set():
        try:
            client, addr = seeder_socket.accept()
        except TimeoutError:
            continue

        ip = addr[0]

        client_thread = Thread(
            target=handle_incoming_peer, args=[client, ip], daemon=False
        )
        client_thread.start()


def handle_incoming_peer(sock: socket.socket, ip: str):
    global uploading_count

    with sock:
        sock.settimeout(None)

        try:
            req, req_type = TorrentRequest.recv_request(sock)
        except BaseException as e:
            log(f"Failed to get request. Error: {e}", f"PEER {ip}")
            return

        sock.settimeout(socket.getdefaulttimeout())

        if req_type == RT_GET_SERVERS:
            with known_servers_lock:
                servers = known_servers.copy()

            try:
                sock.sendall(ServersResponse(servers).to_bytes())
            except BaseException as e:
                log(f"Failed to send response to peer. Error: {e}", f"PEER {ip}")
        elif req_type == RT_DOWNLOAD_PIECE:
            req: DownloadPieceRequest

            with uploading_count_lock:
                if uploading_count >= UPLOAD_MAX:
                    try:
                        sock.sendall(PieceDataResponse(ST_BUSY, b"").to_bytes())
                    except BaseException as e:
                        log(
                            f"Failed to send response to peer. Error: {e}", f"PEER {ip}"
                        )
                    return

                uploading_count += 1

            with seeds_lock:
                torrent_in_seeds = req.torrent_id in available_seeds

            if not torrent_in_seeds:
                try:
                    sock.sendall(PieceDataResponse(ST_NOTFOUND, b"").to_bytes())
                except BaseException as e:
                    log(f"Failed to send response to peer. Error: {e}", f"PEER {ip}")
                return

            with seeds_lock:
                paths = available_seeds[req.torrent_id]

            torrent_info = get_torrent_info(req.torrent_id)

            if torrent_info.is_invalid():
                try:
                    sock.sendall(PieceDataResponse(ST_NOTFOUND, b"").to_bytes())
                except BaseException as e:
                    log(f"Failed to send response to peer. Error: {e}", f"PEER {ip}")
                return

            if len(torrent_info.files) <= req.file_index or req.file_index < 0:
                try:
                    sock.sendall(PieceDataResponse(ST_NOTFOUND, b"").to_bytes())
                except BaseException as e:
                    log(f"Failed to send response to peer. Error: {e}", f"PEER {ip}")
                return

            file = torrent_info.files[req.file_index]

            if len(file.piece_hashes) <= req.piece_index or req.piece_index < 0:
                try:
                    sock.sendall(PieceDataResponse(ST_NOTFOUND, b"").to_bytes())
                except BaseException as e:
                    log(f"Failed to send response to peer. Error: {e}", f"PEER {ip}")
                return

            piece_hash = file.piece_hashes[req.piece_index]

            for p in paths:
                if p == f"downloads/.partial/{req.torrent_id}":
                    file_hex = get_hex(req.file_index)
                    piece_hex = get_hex(req.piece_index)

                    piece_file_path = (
                        f"downloads/.partial/{req.torrent_id}/{file_hex}/{piece_hex}"
                    )

                    if not path.isfile(piece_file_path):
                        log(f"Piece file {piece_file_path} not found", f"PEER {ip}")
                        continue

                    if path.getsize(piece_file_path) > PIECE_SIZE:
                        log(
                            f"Piece file {piece_file_path} has an invalid size",
                            f"PEER {ip}",
                        )
                        continue

                    open_file_with_lock(piece_file_path)
                    try:
                        with open(piece_file_path, "rb") as f:
                            piece_bytes = f.read()
                    except BaseException as e:
                        log(
                            f"Failed to read piece file {piece_file_path}. Error: {e}",
                            f"PEER {ip}",
                        )
                        continue
                    finally:
                        close_file_with_lock(piece_file_path)

                    if not check_piece_hash(piece_bytes, piece_hash):
                        continue

                    sock.settimeout(PIECE_TRANSFER_TIMEOUT)

                    try:
                        sock.sendall(PieceDataResponse(ST_OK, piece_bytes).to_bytes())
                    except BaseException as e:
                        log(
                            f"Failed to send piece data to peer. Closing the connection. Error: {e}",
                            f"PEER {ip}",
                        )
                        return
                    finally:
                        with uploading_count_lock:
                            uploading_count -= 1
                    return
                else:
                    file_path = path.join(p, file.file_name)

                    if not path.isfile(file_path):
                        log(f"File {file_path} not found", f"PEER {ip}")
                        continue

                    if (
                        path.getsize(file_path)
                        != torrent_info.files[req.file_index].file_size
                    ):
                        log(f"File {file_path} has an invalid size", f"PEER {ip}")
                        continue

                    open_file_with_lock(file_path)

                    try:
                        with open(file_path, "rb") as f:
                            f.seek(req.piece_index * torrent_info.piece_size)
                            piece_bytes = f.read(PIECE_SIZE)
                    except BaseException as e:
                        log(
                            f"Failed to read piece from file {file_path}. Error: {e}",
                            f"PEER {ip}",
                        )
                        continue
                    finally:
                        close_file_with_lock(file_path)

                    if not check_piece_hash(piece_bytes, piece_hash):
                        log(
                            f"Piece from file {file_path} has invalid hash",
                            f"PEER {ip}",
                        )
                        continue

                    sock.settimeout(PIECE_TRANSFER_TIMEOUT)

                    try:
                        sock.sendall(PieceDataResponse(ST_OK, piece_bytes).to_bytes())
                    except BaseException as e:
                        log(
                            f"Failed to send piece data to peer. Closing the connection. Error: {e}",
                            f"PEER {ip}",
                        )
                        return
                    finally:
                        with uploading_count_lock:
                            uploading_count -= 1

                    return

            with uploading_count_lock:
                uploading_count -= 1

            try:
                sock.sendall(PieceDataResponse(ST_NOTFOUND, b"").to_bytes())
            except BaseException as e:
                log(f"Failed to send response to peer. Error: {e}", f"PEER {ip}")


# endregion

# region Server Comms


def server_communication_thread():
    comms_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    comms_socket.bind(("", SERVER_COMMS_PORT))
    comms_socket.listen()

    while not exit_event.is_set():
        try:
            server_sock, _ = comms_socket.accept()
        except TimeoutError:
            continue

        server_thread = Thread(
            target=handle_server_request, args=[server_sock], daemon=True
        )
        server_thread.start()


def handle_server_request(sock: socket.socket):
    global last_checkup

    log("Handling server request...", "SERVER CONNECTION")

    with sock:
        sock.settimeout(None)

        try:
            req, req_type = TorrentRequest.recv_request(sock)
        except BaseException as e:
            log(f"Failed to get request from server. Error: {e}", "SERVER CONNECTION")
            return

        if req_type == RT_GET_CLIENT_TORRENTS:
            log(
                "Server is requesting torrents currently being seeded",
                "SERVER CONNECTION",
            )

            with last_checkup_lock:
                last_checkup = time.time()

            update_seeds_validity()

            with seeds_lock:
                torrents = available_seeds.keys()

            try:
                sock.sendall(ClientTorrentsResponse(torrents).to_bytes())
            except BaseException as e:
                log(
                    f"Failed to send response to server. Error: {e}",
                    "SERVER CONNECTION",
                )
                return


# endregion

# region Commands


def print_help(args: list[str]):
    if len(args) != 0 and args[0] != "help" and args[0] in commands:
        commands[args[0]](["help"])
        return

    print(texts.help_text)


def download_command(args: list[str]):
    if len(args) == 0:
        print(
            "Wrong number of arguments in 'download' command, use 'download help' for usage info."
        )
        return

    if args[0] == "help":
        print(texts.download_help)
        return

    download_thread = Thread(
        target=download_torrent, args=[args[0]], name="download", daemon=False
    )
    download_thread.start()

    if "--join" not in args:
        print("Download started, use join-download to view the download progress.")
    else:
        join_download_command([args[0]])


def upload_command(args: list[str]):
    if len(args) == 0:
        print(
            "Wrong number of arguments in 'upload' command, use 'upload help' for usage info."
        )
        return

    if args[0] == "help":
        print(texts.upload_help)
        return

    upload_torrent(args[0])


def exit_command(args: list[str]):
    if len(args) > 0 and args[0] == "help":
        print("Exits the client.")
        return

    print("Logging out of server...")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if connect_socket_to_server(sock):
            try:
                sock.sendall(LogoutRequest().to_bytes())
            except:
                pass

    exit_event.set()

    print("Exiting...")
    exit(0)


def list_seeds(args: list[str]):
    if len(args) > 0 and args[0] == "help":
        print(texts.list_seeds_help)
        return

    with seeds_lock:
        if len(available_seeds) == 0:
            print("No torrents being seeded.")
            return

        print(
            "\n".join(
                map(lambda s: f"{s}: {available_seeds[s]}", available_seeds.keys())
            )
        )


def show_download_log(torrent_id: str):
    while not leave_download_event.is_set() and not exit_event.is_set():
        clear_console()
        text = "Press Enter at any time to leave the download.\n\n"

        with download_statuses_lock:
            if torrent_id not in download_statuses:
                print("Download not found.")
                return

            print(text + str(download_statuses[torrent_id]), flush=True)

            if (
                download_statuses[torrent_id].finished
                or download_statuses[torrent_id].cancelled
            ):
                return

        time.sleep(DOWNLOAD_LOG_TIMESTEP)


def join_download_command(args: list[str]):
    if len(args) > 0 and args[0] == "help":
        print(texts.join_download_help)
        return

    with download_statuses_lock:
        torrents = list(download_statuses.keys())
        downloads = list(download_statuses.items())

    if len(torrents) == 0:
        print("No downloads in progress.")
        return

    torrent_id = None

    if len(args) > 0 and args[0] in torrents:
        torrent_id = args[0]
    else:
        print("Select the download to join from the following list:")
        print()
        for i, (torrent_id, status) in enumerate(downloads):
            st = ""
            if status.finished:
                st = "Finished"
            elif status.exception != None and not status.cancelled:
                st = "Error. Cancelling..."
            elif status.exception != None and status.cancelled:
                st = "Error. Cancelled."
            elif status.cancelled:
                st = "Cancelled"
            elif status.cancel_event.is_set():
                st = "Cancelling..."
            else:
                st = f"{math.floor(status.download_progress() * 1000) / 10}%"

            print(f"{i + 1}: {torrent_id} ({st})")

        while True:
            print()
            try:
                inp = (
                    input(
                        f"Enter the index of the download to join [1 - {len(torrents)}] write 'clear' to remove finished or cancelled downloads or 'exit' to leave: "
                    )
                    .lower()
                    .strip()
                )

                if inp == "exit":
                    return

                if inp == "clear":
                    with download_statuses_lock:
                        for torrent_id, status in downloads:
                            if status.finished or status.cancelled:
                                download_statuses.pop(torrent_id)
                    return

                index = int(inp)
            except:
                print("Invalid index. Please enter a valid number.")
                continue

            if index < 1 or index > len(torrents):
                print(
                    f"Invalid index. Please enter a number between 1 and {len(torrents)}."
                )
                continue

            torrent_id = torrents[index - 1]
            break

    if torrent_id == None:
        print("No download selected.")
        return

    log_thread = Thread(
        target=show_download_log, args=[torrent_id], name="download_log", daemon=True
    )
    log_thread.start()

    try:
        input()
    except:  # input() can raise errors, such as EOFError, and we don't care about that.
        pass

    leave_download_event.set()

    try:
        log_thread.join()
    except:
        pass

    leave_download_event.clear()


def cancel_download_command(args: list[str]):
    if len(args) == 0:
        print(
            "Wrong number of arguments in 'cancel-download' command, use 'cancel-download help' for usage info."
        )
        return

    if args[0] == "help":
        print(texts.cancel_download_help)
        return

    with download_statuses_lock:
        if args[0] not in download_statuses:
            print(f"No torrent with id {args[0]} is being downloaded.")
            return

        download_statuses[args[0]].cancel_event.set()

    print("Cancelled.")


def seed_command(args: list[str]):
    if len(args) == 0 or (len(args) < 2 and args[0] != "help"):
        print(
            "Wrong number of arguments in 'seed' command, use 'seed help' for usage info."
        )
        return

    if args[0] == "help":
        print(texts.seed_help)
        return

    if not path.exists(path.dirname(args[0])):
        print(f"Path {args[0]} does not exist.")
        return

    with seeds_lock:
        if (
            args[1] in available_seeds
            and path.dirname(args[0]) in available_seeds[args[1]]
        ):
            print("Torrent already being seeded.")
            return

    log(f"Attempting to seed torrent {args[1]}. Getting torrent info...", "SEED")

    torrent_info = get_torrent_info(args[1])

    if torrent_info.is_invalid():
        log(f"Failed to get torrent info for torrent {args[1]}.", "SEED")
        print(f"Torrent {args[1]} not found. May be a connection issue.")
        return

    for i, file in enumerate(torrent_info.files):
        file_path = path.join(path.dirname(args[0]), file.file_name)

        if not path.exists(file_path):
            log(f"File {file_path} not found.", "SEED")
            print(f"File {file_path} not found.")
            return

        if path.getsize(file_path) != file.file_size:
            log(f"File {file_path} has incorrect size.", "SEED")
            print(f"File {file_path} has incorrect size.")
            return

        if not check_file(torrent_info, i, file_path):
            log(f"File {file_path} is incorrect.", "SEED")
            print(f"File {file_path} is incorrect.")
            return

    add_path_to_seeds(args[1], path.dirname(args[0]))

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        log("Connecting to server...", "SEED")
        if not connect_socket_to_server(sock):
            log("Failed to connect to server", "SEED")
            print("Failed to connect to server")
            return

        try:
            sock.sendall(RegisterAsPeerRequest(args[1]).to_bytes())
        except BaseException as e:
            log(f"Failed to send request to server. Error: {e}", "SEED")
            print("Failed to send request to server.")
            return

    print("Seeding!")


commands: dict[str, Callable] = {
    "help": print_help,
    "download": download_command,
    "upload": upload_command,
    "exit": exit_command,
    "list-seeds": list_seeds,
    "join-download": join_download_command,
    "cancel-download": cancel_download_command,
    "seed": seed_command,
}

# endregion

# region Reconnection


def attempt_reconnection_thread():
    global last_checkup
    while not exit_event.is_set():
        time.sleep(5)

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if not connect_socket_to_server(sock):
                log("Lost connection to server, reconnecting...", "RECONNECTION THREAD")
                if not connect_socket_to_server(sock, True):
                    continue

            with last_checkup_lock:
                not_passed = last_checkup == None or time.time() - last_checkup < 2 * 60
            if not_passed:
                with known_servers_lock:
                    low_servers = len(known_servers) < MIN_KNOWN_SERVERS

                if low_servers:
                    try:
                        sock.sendall(GetServersRequest().to_bytes())
                        req, req_type = TorrentRequest.recv_request(sock)

                        if req_type == RT_SERVERS_RESPONSE:
                            req: ServersResponse

                            with known_servers_lock:
                                for ip in req.server_ips:
                                    if ip not in known_servers:
                                        known_servers.append(ip)
                    except:
                        pass
                    continue

                sock.sendall(PingRequest().to_bytes())
                continue

            with last_checkup_lock:
                last_checkup = None

            sock.sendall(LoginRequest().to_bytes())

            log("Reconnected to server", "RECONNECTION THREAD")


def discover_servers() -> list[str]:
    servers = []
    ip = socket.gethostbyname(socket.gethostname())

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        multicast_group = "224.0.0.10"
        port = SERVER_MULTICAST_PORT

        discovery_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        discovery_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        discovery_socket.bind(("", CLIENT_DISCOVERY_PORT))
        discovery_socket.settimeout(3)

        discovery_socket.listen(10)

        # send discovery message
        sock.sendto(b"client" + ip_to_bytes(ip), (multicast_group, port))
        sock.close()

        # wait for other servers to respond
        start_wait_time = time.time()

        while time.time() - start_wait_time < DISCOVERY_TIMEOUT:
            try:
                conn, addr = discovery_socket.accept()
            except TimeoutError:
                continue

            try:
                # should receive "discovery" + the ip, so 13 bytes
                data = conn.recv(13)

                if len(data) < 13:
                    continue

                if data[:9].decode() != "discovery":
                    continue

                if ip_from_bytes(data[9:13]) != addr[0]:
                    continue

                servers.append(addr[0])

                conn.close()
            except:
                continue
    except BaseException as e:
        log(f"Error at discovery: {e}", "DISCOVERY")

    if len(servers) == 0:
        log("Failed to find any servers through discovery", "DISCOVERY")

    return servers


# endregion


def main():
    print("Welcome to CDL-BitTorrent Client!")
    print("Starting Up...")

    log("Starting up. Loading configuration...", "MAIN")

    connected_to_server.set()

    load_config()

    print("Finding servers...")
    log("Finding servers...", "DISCOVERY")

    if "--nodiscovery" not in argv:
        servers = discover_servers()

        with known_servers_lock:
            known_servers.extend(servers)

    logging_thread = Thread(target=logger, name="logging", daemon=True)
    logging_thread.start()

    server_comms = Thread(
        target=server_communication_thread, name="server_comms", daemon=True
    )
    server_comms.start()

    seeder = Thread(target=seeder_thread, name="seeder", daemon=True)
    seeder.start()

    reconnection_thread = Thread(
        target=attempt_reconnection_thread, name="reconnection", daemon=True
    )
    reconnection_thread.start()

    print("Logging in to server...")
    log("Logging in to server...", "MAIN")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as login_socket:
        if not connect_socket_to_server(login_socket):
            print(
                f"Failed to connect to server, the server may be unavailable or your internet connection may be down. Exiting..."
            )
            log("Failed to connect to server. Exiting...", "MAIN")
            exit_event.set()
            exit(1)

        try:
            login_socket.sendall(LoginRequest().to_bytes())
        except:
            print("Failed to send login request to server. Exiting...")
            log("Failed to send login request to server. Exiting...", "MAIN")
            exit_event.set()
            exit(1)

    socket.setdefaulttimeout(5)

    print("Done!\n")

    print(
        "This is the CDL-BitTorrent Client CLI, use help to learn the available commands."
    )

    while True:
        inp: str = input("$> ").strip()

        if len(inp) == 0:
            continue

        [command, *args] = split_command(inp)

        if command not in commands:
            print(
                f"Unknown command '{command}', use 'help' to see the available commands."
            )
            continue

        commands[command](args)


if __name__ == "__main__":
    main()
