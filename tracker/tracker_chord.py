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
                s.connect((self.ip, self.port))
                req = op.to_bytes() + len(data).to_bytes(4) + data
                s.sendall(req)
                if expect_response:
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

    # Property to get the successor of the current node
    @property
    def succ(self) -> "ChordNodeReference":
        return node_from_bytes(self._send_data(GET_SUCCESSOR))

    # Property to get the predecessor of the current node
    @property
    def pred(self) -> "ChordNodeReference":
        return node_from_bytes(self._send_data(GET_PREDECESSOR))

    # Method to notify the current node about another node
    def notify(self, node: "ChordNodeReference"):
        self._send_data(NOTIFY, node_to_bytes(node), False)

    # Method to check if the predecessor is alive
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
        return f"{self.id},{self.ip},{self.port}"

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
        self.successor_list = (
            []
        )  # Successor list used for replication and fault tolerance
        self.replication = replication  # Number of replicas
        self.pred = None  # Initially no predecessor
        self.m = m  # Number of bits in the hash/key space
        self.finger = [self.ref] * self.m  # Finger table
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
        node = self
        last_node = self
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

            # this doesn't enter a loop since if node == node.closest_preceding_finger, then
            # node == node.succ (if it wasn't then node != noed.closest_preceding_finger, see function below),
            # then the _inbetween call below will succeed.
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
        successor_list_index = len(self.successor_list) - 1

        # this loop goes down the finger list and the successor list at the same time,
        # making sure that if low-index fingers are down, the successor list can make up for it
        # keeping up the eficiency of the Chord algorithm
        for i in range(self.m - 1, -1, -1):
            if not self.finger[i] or not self.finger[i].ping():
                continue

            while (
                successor_list_index >= 0
                and self.finger[i].id < self.successor_list[successor_list_index].id
            ):
                suc = self.successor_list[successor_list_index]
                if inbetween(suc.id, self.id, id) and suc.ping():
                    return suc
                successor_list_index -= 1

            if (
                inbetween(self.finger[i].id, self.id, id)
                and self.finger[i]
                and self.finger[i].ping()
            ):
                return self.finger[i]

        return self.ref

    def get_successor_list(self) -> list["ChordNodeReference"]:
        return self.successor_list

    # Method to join a Chord network using 'node' as an entry point
    def join(self, node: "ChordNodeReference"):
        self.pred = None
        if node:
            self.succ = node.find_successor(self.id)
            # this means node died, so we can't join the network
            if self.succ == None:
                raise RuntimeError(f"Can't reach node {node.ip}. Can't join network.")

            succ_succs = self.succ.get_successor_list()

            if len(succ_succs) > self.replication - 1:
                self.successor_list = succ_succs[: self.replication - 1]

            self.successor_list = [self.succ] + self.successor_list

            self.succ.notify(self.ref)
        else:
            self.succ = self.ref

    # Stabilize method to periodically verify and update the successor and predecessor
    def stabilize(self):
        while True:
            succs = self.successor_list.copy()
            for s in succs:
                if not s.ping():
                    self.successor_list.remove(s)

            succ_list_index = 0
            while succ_list_index < len(self.successor_list) and (
                not self.succ.ping() or self.succ.id == self.id
            ):
                self.succ = self.successor_list[succ_list_index]
                succ_list_index += 1

            if succ_list_index == -1 and not self.succ.ping():
                self.succ = self.ref

            x = self.succ.pred
            if x == None:
                if self.succ.id != self.id:
                    if (
                        not self.succ.ping()
                    ):  # it died, go through the successor list again
                        continue
                    self.succ.notify(self.ref)
            elif x.id != self.id:
                if inbetween(x.id, self.id, self.succ.id):
                    self.succ = x
                self.succ.notify(self.ref)

            if self.succ.id != self.id:
                succ_succs_list = self.succ.get_successor_list()

                if len(succ_succs_list) > self.replication - 1:
                    succ_succs_list = succ_succs_list[: self.replication - 1]

                old = self.successor_list
                self.successor_list = [self.succ] + succ_succs_list

                removed = []
                added = []

                for s in self.successor_list:
                    if s not in old:
                        added.append(s)
                for s in old:
                    if s not in self.successor_list:
                        removed.append(s)

                self.update_succs_func(added, removed)

            time.sleep(10)

    # Notify method to inform the node about another node
    def notify(self, node: "ChordNodeReference"):
        if node.id == self.id:
            pass
        if not self.pred or inbetween(node.id, self.pred.id, self.id):
            self.pred = node
            self.own_replica_func(self.pred.id, self.id)
            if len(self.successor_list) == self.replication:
                self.successor_list[self.replication - 1].remove_replica(
                    self.start_id, self.pred.id
                )
            self.transfer_data_func(self.start_id, self.pred.id, self.pred)

    # Fix fingers method to periodically update the finger table
    def fix_fingers(self):
        while True:
            self.next += 1
            if self.next >= self.m:
                self.next = 0
            self.finger[self.next] = self.find_succ(
                (self.id + 2**self.next) % 2**self.m
            )
            time.sleep(10)

    # Check predecessor method to periodically verify if the predecessor is alive
    def check_predecessor(self):
        while True:
            if self.pred:
                if not self.pred.ping():
                    self.pred = None
            time.sleep(10)

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
        self.log("Received connection", "IN CHORD")
        option = int.from_bytes(recv_all(conn, 1))
        data_len = int.from_bytes(recv_all(conn, 4))
        self.log(f"Option {option}, Data Length {data_len}", "IN CHORD")
        data = recv_all(conn, data_len)

        data_resp = None

        if option == FIND_SUCCESSOR:
            id = int.from_bytes(data)
            data_resp = self.find_succ(id)
        elif option == FIND_PREDECESSOR:
            id = int.from_bytes(data)
            data_resp = self.find_pred(id)
        elif option == GET_SUCCESSOR:
            data_resp = self.succ if self.succ else self.ref
        elif option == GET_PREDECESSOR:
            data_resp = self.pred if self.pred else self.ref
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
            succs = self.get_successor_list()
            data_resp = len(succs).to_bytes(4)
            for s in succs:
                data_resp += node_to_bytes(s)
        else:
            self.log("Could not find any operation with that ID", "IN CHORD")

        if data_resp:
            self.log("Has response", "IN CHORD")
            if isinstance(data_resp, ChordNodeReference):
                self.log("Is a node", "IN CHORD")
                data_resp = node_to_bytes(data_resp)
            self.log("Sending response", "IN CHORD")
            conn.sendall(len(data_resp).to_bytes(4) + data_resp)
            self.log("Response sent", "IN CHORD")

        conn.close()

    # Start server method to handle incoming requests
    def start_server(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.log(f"Starting chord server at {self.ip}:{self.port}", "IN CHORD")
            s.bind((self.ip, self.port))
            s.listen(5)

            while True:
                conn, _ = s.accept()

                threading.Thread(
                    target=self.handle_connection, args=[conn], daemon=False
                ).start()


if __name__ == "__main__":
    ip = socket.gethostbyname(socket.gethostname())
    node = ChordNode(ip)

    if len(sys.argv) >= 2:
        other_ip = sys.argv[1]
        node.join(ChordNodeReference(other_ip, node.port))

    while True:
        pass
