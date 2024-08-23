from calendar import c
from concurrent.futures import thread
from os import remove
import socket
import threading
import sys
import time
import hashlib
from typing import Callable
from utils.utils import recv_all, ip_from_bytes, ip_to_bytes

CHORD_NODES_PORT = 8081

# Operation codes
FIND_SUCCESSOR = 1
FIND_PREDECESSOR = 2
GET_SUCCESSOR = 3
GET_PREDECESSOR = 4
NOTIFY = 5
PING = 6
CLOSEST_PRECEDING_FINGER = 7
STORE_KEY = 8
STORE_KEY_REPLICA = 9
RETRIEVE_KEY = 10
GET_SUCCESSOR_LIST = 11
DELETE_FROM_REPLICA = 12
GET_START = 13
GET_PREDECESSOR_LIST = 14


def get_sha_repr(data: str):
    return int(hashlib.sha256(data.encode()).hexdigest(), 16)


def get_sha_for_bytes(data: bytes):
    return int(hashlib.sha256(data).hexdigest(), 16)


# Helper method to check if a value is in the range (start, end]
def inbetween(k: int, start: int, end: int) -> bool:
    if start < end:
        return start < k <= end
    else:  # The interval wraps around 0
        return start < k or k <= end


# Class to reference a Chord node
class ChordNodeReference:
    def __init__(
        self,
        ip: str,
        port: int = CHORD_NODES_PORT,
        id=None,
        log: Callable[[str, str], None] = None,
    ):
        self.id = get_sha_repr(ip) if not id else id
        self.ip = ip
        self.port = port
        self.log = log

    # Internal method to send data to the referenced node
    def _send_data(
        self, op: int, data: bytes = b"", expect_response: bool = True
    ) -> bytes:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                s.connect((self.ip, self.port))
                req = op.to_bytes() + len(data).to_bytes(4) + data
                s.sendall(req)
                if expect_response:
                    s.settimeout(20)
                    res_len = int.from_bytes(recv_all(s, 4))
                    return recv_all(s, res_len)
                else:
                    return b""
        except:  # here we can assume that node effing died
            return None

    # Method to find the successor of a given id
    def find_successor(self, id: int) -> "ChordNodeReference":
        return node_from_bytes(self._send_data(FIND_SUCCESSOR, id.to_bytes(32)))

    # Method to find the predecessor of a given id
    def find_predecessor(self, id: int) -> "ChordNodeReference":
        return node_from_bytes(self._send_data(FIND_PREDECESSOR, id.to_bytes(32)))

    def get_successor_list(self) -> list["ChordNodeReference"]:
        response = self._send_data(GET_SUCCESSOR_LIST)
        if response == None:
            return []

        response_index = 0

        def get_response(_bytes: int):
            nonlocal response_index
            ret = response[response_index : response_index + _bytes]
            response_index += _bytes
            return ret

        succ_count = int.from_bytes(get_response(4))

        succ_list = []
        for _ in range(succ_count):
            succ_list.append(
                node_from_bytes(get_response(36))
            )  # a node is 36 bytes, 32 for the key and 4 for the ip

        return succ_list

    def get_predecessor_list(self) -> list["ChordNodeReference"]:
        response = self._send_data(GET_PREDECESSOR_LIST)
        if response == None:
            return None

        response_index = 0

        def get_response(_bytes: int):
            nonlocal response_index
            ret = response[response_index : response_index + _bytes]
            response_index += _bytes
            return ret

        pred_count = int.from_bytes(get_response(4))

        pred_list = []
        for _ in range(pred_count):
            pred_list.append(
                node_from_bytes(get_response(36))
            )  # a node is 36 bytes, 32 for the key and 4 for the ip

        return pred_list

    # Property to get the successor of the current node
    @property
    def succ(self) -> "ChordNodeReference":
        return node_from_bytes(self._send_data(GET_SUCCESSOR))

    # Property to get the predecessor of the current node
    @property
    def pred(self) -> "ChordNodeReference":
        return node_from_bytes(self._send_data(GET_PREDECESSOR))

    @property
    def start(self) -> int:
        data = self._send_data(GET_START)
        if data == None:
            return None
        return int.from_bytes(data)

    # Method to notify the current node about another node
    def notify(self, node: "ChordNodeReference"):
        self._send_data(NOTIFY, node_to_bytes(node), False)

    def ping(self) -> bool:
        return self._send_data(PING, expect_response=False) == b""

    # Method to find the closest preceding finger of a given id
    def closest_preceding_finger(self, id: int) -> "ChordNodeReference":
        return node_from_bytes(
            self._send_data(CLOSEST_PRECEDING_FINGER, id.to_bytes(32))
        )

    # Method to store a key-value pair in the current node
    def store_key(self, key: bytes, key_options: bytes, value: bytes) -> bool:
        return (
            self._send_data(
                STORE_KEY, len(key).to_bytes(4) + key + key_options + value, False
            )
            != None
        )

    def store_key_replica(self, key: bytes, key_options: bytes, value: bytes):
        self._send_data(
            STORE_KEY_REPLICA, len(key).to_bytes(4) + key + key_options + value, False
        )

    def remove_replica(self, start: int, end: int):
        self._send_data(
            DELETE_FROM_REPLICA, start.to_bytes(32) + end.to_bytes(32), False
        )

    # Method to retrieve a value for a given key from the current node
    def retrieve_key(self, key: bytes, key_options: bytes) -> bytes | None:
        return self._send_data(RETRIEVE_KEY, len(key).to_bytes(4) + key + key_options)

    def __str__(self) -> str:
        return f"{self.ip},{self.port}"

    def __repr__(self) -> str:
        return str(self)

    def __eq__(self, value: object) -> bool:
        if not isinstance(value, ChordNodeReference):
            return False
        return self.id == value.id


def node_to_bytes(node: ChordNodeReference) -> bytes:
    if node == None:
        return b""
    return node.id.to_bytes(32) + ip_to_bytes(node.ip)


def node_from_bytes(node_bytes: bytes) -> ChordNodeReference:
    if node_bytes == None or node_bytes == b"":
        return None
    return ChordNodeReference(
        ip_from_bytes(node_bytes[32:]),
        CHORD_NODES_PORT,
        int.from_bytes(node_bytes[:32]),
    )


# Class representing a Chord node
class ChordNode:
    def __init__(
        self,
        ip: str,
        port: int = CHORD_NODES_PORT,
        m: int = 256,
        replication: int = 16,
        store_data_func: Callable[[bytes, bytes, bytes], None] = None,
        store_replica_func: Callable[[bytes, bytes, bytes], None] = None,
        # (key, key options)
        retrieve_data_func: Callable[[bytes, bytes], bytes] = None,
        # (start index, end index of data to be moved (every data with key lower than that is to be moved), node to move it to)
        transfer_data_func: Callable[[int, int, "ChordNodeReference"], None] = None,
        # (start index, end index of data to be moved from replica to own node)``
        own_replica_func: Callable[[int, int], None] = None,
        # (start index, end index of data to be deleted from replica)
        delete_replica_func: Callable[[int, int], None] = None,
        # (list of new successors, list of removed successors)
        update_succs_func: Callable[
            [list["ChordNodeReference"], list["ChordNodeReference"]], None
        ] = None,
        log: Callable[[str, str], None] = None,
    ):

        self.id = get_sha_repr(ip)
        self.ip = ip
        self.port = port
        self.ref = ChordNodeReference(self.ip, self.port, log=log)
        self.succ = self.ref  # Initial successor is itself
        self.succ_lock: threading.Lock = threading.Lock()
        self.successor_list = (
            []
        )  # Successor list used for replication and fault tolerance
        self.predecessor_list: list[ChordNodeReference] = (
            []
        )  # Used to update owned replicas and easily get a new predecessor when the current one dies
        self.predecessor_list_lock: threading.Lock = threading.Lock()
        self.successor_list_lock: threading.Lock = threading.Lock()
        self.replication = replication  # Number of replicas
        self.pred = None  # Initially no predecessor
        self.m = m  # Number of bits in the hash/key space
        self.finger = [self.ref] * self.m  # Finger table
        self.fingers_lock: threading.Lock = threading.Lock()
        self.next = 0  # Finger table index to fix next
        self.store_data_func = store_data_func  # Function to store data
        self.store_replica_func = store_replica_func  # Function to store data replicas
        self.retrieve_data_func = retrieve_data_func  # Function to retrieve data
        self.transfer_data_func = transfer_data_func  # Function to transfer data to
        self.own_replica_func = own_replica_func  # Function to own data replicas
        self.delete_replica_func = (
            delete_replica_func  # Function to delete data replicas
        )
        self.update_succs_func = update_succs_func  # Function to update successors
        self.log = log  # Function to log messages
        self.start_id = self.id  # Index of the lowest key in the node's data
        self.pred_start = None

        # Start background threads for stabilization, fixing fingers, and checking predecessor
        threading.Thread(
            target=self.stabilize, daemon=True
        ).start()  # Start stabilize thread
        threading.Thread(
            target=self.fix_fingers, daemon=True
        ).start()  # Start fix fingers thread
        threading.Thread(
            target=self.check_predecessor, daemon=True
        ).start()  # Start check predecessor thread
        threading.Thread(
            target=self.start_server, daemon=True
        ).start()  # Start server thread

    # Method to find the successor of a given id
    def find_succ(self, id: int) -> "ChordNodeReference":
        node = self.find_pred(id)  # Find predecessor of id
        successor = node.succ  # Return successor of that node
        if successor == None:
            return self.find_succ(id)  # Retry, `node` just died
        return successor

    # Method to find the predecessor of a given id
    def find_pred(self, id: int) -> "ChordNodeReference":
        node = self.ref
        last_node = self.ref
        # this loop advances 2 nodes at a time, so that if a fail occurs,
        # we can go back one step instead of starting from the beginning
        # (if both die, we just reset to the start)
        while True:
            # this makes sure node doesn't die between the last iteration and this one
            succ = node.succ
            if succ == None:
                node = last_node.closest_preceding_finger(id)

                if node == None:  # 2 dead nodes in a row? retry from the start
                    return self.find_pred(id)

                continue

            if inbetween(id, node.id, succ.id):
                break

            closest = node.closest_preceding_finger(id)
            if closest == None:  # tried to ping a dead node
                # try again from last_node
                node = last_node.closest_preceding_finger(id)

                if node == None:  # 2 dead nodes in a row? retry from the start
                    return self.find_pred(id)
            else:
                last_node = node
                node = closest
        return node

    # Method to find the closest preceding finger of a given id
    def closest_preceding_finger(self, id: int) -> "ChordNodeReference":
        with self.fingers_lock:
            for i in range(self.m - 1, -1, -1):
                if (
                    self.finger[i]
                    and inbetween(self.finger[i].id, self.id, id)
                    and self.finger[i].ping()
                ):
                    return self.finger[i]

        with self.successor_list_lock:
            succ_list = self.successor_list.copy()

        for s in succ_list:
            if inbetween(s.id, self.id, id):
                return s

        return self.ref

    # Method to join a Chord network using 'node' as an entry point
    def join(self, node: "ChordNodeReference"):
        self.pred = None
        if node:
            with self.succ_lock:
                self.succ = node.find_successor(self.id)
                # this means node died, so we can't join the network
                if self.succ == None:
                    raise RuntimeError(
                        f"Can't reach node {node.ip}. Can't join network."
                    )

            with self.succ_lock:
                with self.successor_list_lock:
                    succ_succs = self.succ.get_successor_list()
                    if len(succ_succs) > self.replication - 1:
                        succ_succs = succ_succs[: self.replication - 1]
                    self.successor_list = [self.succ] + succ_succs

                self.succ.notify(self.ref)
        else:
            with self.succ_lock:
                self.succ = self.ref

    # Method to fix the successor of the current node
    def fix_succ(self):
        with self.succ_lock:
            succ_pinged = self.succ.id != self.id and self.succ.ping()

        if not succ_pinged:
            fixed = False
            with self.successor_list_lock:
                succs = self.successor_list.copy()
            for succ in succs:
                if succ.ping():
                    with self.succ_lock:
                        self.succ = succ
                        fixed = True
                        break
                else:
                    with self.successor_list_lock:
                        if succ in self.successor_list:
                            self.successor_list.remove(succ)
            if not fixed:
                with self.succ_lock:
                    self.succ = self.ref

    # Stabilize method to periodically verify and update the successor
    def stabilize(self):
        while True:
            self.fix_succ()

            with self.succ_lock:
                x = self.succ.pred
                if x and x.id != self.id:
                    if inbetween(x.id, self.id, self.succ.id):
                        self.succ = x
                    self.succ.notify(self.ref)

            self.succ_lock.acquire()
            if self.succ.id != self.id:
                succ_succs_list = self.succ.get_successor_list()
                self.succ_lock.release()

                while self.ref in succ_succs_list:
                    succ_succs_list.remove(self.ref)

                if len(succ_succs_list) > self.replication - 1:
                    succ_succs_list = succ_succs_list[: self.replication - 1]

                removed = []
                added = []

                with self.successor_list_lock:
                    old = self.successor_list
                    new_list = [self.succ] + succ_succs_list
                    self.successor_list = new_list

                for s in new_list:
                    if s not in old:
                        added.append(s)
                for s in old:
                    if s not in new_list:
                        removed.append(s)

                self.update_succs_func(added, removed)
            else:
                self.succ_lock.release()

            time.sleep(1)

    # Notify method to inform the node about another node
    def notify(self, node: "ChordNodeReference"):
        if node.id == self.id:
            return

        if not self.pred or inbetween(node.id, self.pred.id, self.id):
            pred_start = node.start

            if pred_start == None:
                return

            pred_preds = node.get_predecessor_list()

            if pred_preds == None:
                return

            self.pred = node
            self.own_replica_func(self.pred.id, self.id)

            if len(self.successor_list) == self.replication:
                self.successor_list[self.replication - 1].remove_replica(
                    self.start_id, self.pred.id
                )

            self.transfer_data_func(self.start_id, self.pred.id, self.pred)

            self.start_id = self.pred.id

            if pred_start == self.pred.id:
                self.pred_start = None
            else:
                self.pred_start = pred_start

            new_preds = [self.pred]

            for i in range(min(self.replication - 1, len(pred_preds))):
                if pred_preds[i].id == self.id:
                    break

                new_preds.append(pred_preds[i])

            with self.predecessor_list_lock:
                self.predecessor_list = new_preds

    # Fix fingers method to periodically update the finger table
    def fix_fingers(self):
        while True:
            self.next += 1
            if self.next >= self.m:
                self.next = 0

            succ = self.find_succ((self.id + 2**self.next) % 2**self.m)
            with self.fingers_lock:
                self.finger[self.next] = succ
            time.sleep(1)

    # Check predecessor method to periodically verify if the predecessor is alive
    def check_predecessor(self):
        while True:
            if self.pred:
                if not self.pred.ping():
                    # own data that used to belong to pred
                    if self.pred_start != None:
                        self.own_replica_func(self.pred_start, self.pred.id)

                    self.pred = None

                    with self.predecessor_list_lock:
                        pred_list = self.predecessor_list.copy()

                    for pred in pred_list:
                        if pred.ping():
                            self.notify(pred)
                        else:
                            with self.predecessor_list_lock:
                                self.predecessor_list.remove(pred)

                    if self.pred == None:
                        potential_pred = self.find_pred(self.id)
                        if potential_pred.id == self.id:
                            # own all data, we (think) are the only node in the chord
                            self.own_replica_func(0, 2**self.m)
                else:
                    pred_start = self.pred.start

                    if pred_start == None:
                        continue

                    if pred_start == self.pred.id:
                        self.pred_start = None
                    else:
                        self.pred_start = pred_start

                    pred_preds = self.pred.get_predecessor_list()

                    if pred_preds == None:
                        continue

                    new_preds = [self.pred]

                    for i in range(min(self.replication - 1, len(pred_preds))):
                        if pred_preds[i].id == self.id:
                            break

                        new_preds.append(pred_preds[i])

                    with self.predecessor_list_lock:
                        self.predecessor_list = new_preds

            time.sleep(1)

    # Store key method to store a key-value pair and replicate to the successor
    def store_key(self, key: bytes, key_options: bytes, value: bytes):
        key_hash = get_sha_for_bytes(key)
        node = self.find_succ(key_hash)
        node.store_key(key, key_options, value)

    # Retrieve key method to get a value for a given key
    def retrieve_key(self, key: bytes, key_options: bytes) -> bytes | None:
        key_hash = get_sha_for_bytes(key)
        node = self.find_succ(key_hash)
        return node.retrieve_key(key, key_options)

    def handle_connection(self, conn: socket.socket):
        option = int.from_bytes(recv_all(conn, 1))
        data_len = int.from_bytes(recv_all(conn, 4))
        data = recv_all(conn, data_len)

        data_resp = None

        if option == FIND_SUCCESSOR:
            id = int.from_bytes(data)
            data_resp = self.find_succ(id)
        elif option == FIND_PREDECESSOR:
            id = int.from_bytes(data)
            data_resp = self.find_pred(id)
        elif option == GET_SUCCESSOR:
            self.fix_succ()
            with self.succ_lock:
                data_resp = node_to_bytes(self.succ)
        elif option == GET_PREDECESSOR:
            data_resp = self.pred if self.pred else self.ref
        elif option == GET_START:
            data_resp = self.start_id.to_bytes(32)
        elif option == NOTIFY:
            self.notify(node_from_bytes(data))
        elif option == PING:
            pass
        elif option == CLOSEST_PRECEDING_FINGER:
            id = int.from_bytes(data)
            data_resp = self.closest_preceding_finger(id)
        elif option == STORE_KEY:
            keylen = int.from_bytes(data[:4])
            key = data[4 : 4 + keylen]

            key_options = data[4 + keylen : 6 + keylen]

            value = data[6 + keylen :]

            self.store_data_func(key, key_options, value)

            with self.successor_list_lock:
                for succ in self.successor_list:
                    succ.store_key_replica(key, key_options, value)
        elif option == STORE_KEY_REPLICA:
            keylen = int.from_bytes(data[:4])
            key = data[4 : 4 + keylen]

            key_options = data[4 + keylen : 6 + keylen]

            value = data[6 + keylen :]

            self.store_replica_func(key, key_options, value)
        elif option == DELETE_FROM_REPLICA:
            start = int.from_bytes(data[:32])
            end = int.from_bytes(data[32:])
            self.delete_replica_func(start, end)
        elif option == RETRIEVE_KEY:
            keylen = int.from_bytes(data[:4])
            key = data[4 : 4 + keylen]
            key_options = data[4 + keylen :]
            data_resp = self.retrieve_data_func(key, key_options)
        elif option == GET_SUCCESSOR_LIST:
            with self.successor_list_lock:
                succs = self.successor_list.copy()
            data_resp = len(succs).to_bytes(4)
            for s in succs:
                data_resp += node_to_bytes(s)
        elif option == GET_PREDECESSOR_LIST:
            with self.predecessor_list_lock:
                preds = self.predecessor_list.copy()
            data_resp = len(preds).to_bytes(4)
            for s in preds:
                data_resp += node_to_bytes(s)

        if data_resp != None:
            if isinstance(data_resp, ChordNodeReference):
                data_resp = node_to_bytes(data_resp)
            conn.sendall(len(data_resp).to_bytes(4) + data_resp)

        conn.close()

    # Start server method to handle incoming requests
    def start_server(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((self.ip, self.port))
            s.listen(5)

            while True:
                conn, _ = s.accept()

                threading.Thread(
                    target=self.handle_connection, args=[conn], daemon=False
                ).start()

    def __str__(self) -> str:
        with self.successor_list_lock:
            succs = "\n".join(str(f) for f in self.successor_list)
        with self.predecessor_list_lock:
            preds = "\n".join(str(f) for f in self.predecessor_list)

        with self.succ_lock:
            return f"pred: {self.pred}\nsucc:\n{self.succ}\npredecssors: \n {preds}\nsuccessors: \n{succs}"


if __name__ == "__main__":
    ip = socket.gethostbyname(socket.gethostname())
    node = ChordNode(ip)

    if len(sys.argv) >= 2:
        other_ip = sys.argv[1]
        node.join(ChordNodeReference(other_ip, node.port))

    while True:
        pass
