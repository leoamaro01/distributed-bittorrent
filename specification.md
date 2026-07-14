# Core Technologies

## Request system
Communication between clients and servers uses a set of objects with a native byte serialization/deserialization mechanism, which provides the convenience of object-oriented programming without sacrificing bandwidth or performance.

This same system carries both simple queries and entire torrent pieces.

## Piece-based downloads
When a user uploads a torrent, they send the torrent's directory structure and each file's data to the server they are connected to. That data is:
- The full file name, including the directories on the path to it
- The file size in bytes
- Verification hashes for each piece of the file (we use 256 KB pieces, but this is easily configurable without losing compatibility with existing torrents, since the piece size is included in the torrent information the user uploads to the server)

File pieces are downloaded in random order, to uniformly balance piece availability across the user swarm, and from random peers as well, to balance downloads across all users sharing the torrent.

Every downloaded piece has its bytes hashed and checked against the hash specified in the torrent information, ensuring that neither accidental nor malicious tampering with the torrent's files can occur. The piece data (bytes) is then stored as a file in a folder dedicated to partial downloads.

Once all pieces of a file are downloaded, they are verified again — if one or more pieces fail verification (this step is repeated to prevent tampering with pieces during the download), the downloaded pieces are deleted and re-added to the pending downloads list. If they pass verification, the file is reconstructed from the contents of the piece files, and once reconstruction completes the pieces are removed from the partial downloads folder.

## Chord
Chord is used as the data distribution system, thanks to its proven performance and robustness against failures.

The distributed data consists of torrent information and the users currently online, such that the node that locally stores the online users is responsible for checking which torrents each user has available.

### Fault-resistance strategies
Some replication strategies were added on top of the basic Chord protocol to make it more resistant to unexpected failures of ring nodes.

- #### Immediate successor set
    Every node maintains, in addition to its immediate successor, a successor set of length $r = 16$. The [Chord paper](https://pdos.csail.mit.edu/papers/chord:sigcomm01/chord_sigcomm.pdf) proves that for an initially stable network with $N$ nodes and a successor list of length $O(\log N)$, if every node then fails with probability $1/2$, then with high probability `find_successor` returns the closest live successor to the requested key — which, in this case, holds for up to $2^{16} = 65{,}536$ nodes. This successor set is also used for data replication.

- #### Data replication
    Each node is responsible not only for the torrents and users whose keys it succeeds, but also for a set of replicas of the data of its predecessor nodes.

    The replica lifecycle is as follows:
    1. When a piece of data is stored on a node, the node replicates it to every node in its successor list.
    2. When a node's successor list changes, all of the node's own data is replicated to the new successors, and the corresponding replicas are deleted from nodes removed from the successor list.
    3. When a node loses its predecessor (whether through failure or any other reason), it takes ownership of the data it was storing as replicas of its predecessor's data.
    4. When a node gains a new predecessor, it transfers part of its own data to the new predecessor, which then re-inserts it into this same node through (1), this time as replicas.

## Discovery
Both clients and servers must carry out discovery strategies to learn about new servers in case the ones they have connected to so far fail. We already covered one way servers achieve this — the successor list — and clients do something similar. Clients maintain a list of "known servers" and always try to connect to the first element of that list; if the connection ever fails, that server is removed from the known list and the next one is tried, and so on. But what happens when a client — or a server — doesn't know any servers at all?

### Multicast
Both clients and servers use multicast to find available servers on the local network. Every server keeps a multicast port open, ready to respond to any client or server that needs to join the network.

This may be enough for servers, which in any case use Chord's own mechanisms to continuously learn about new servers, but clients have no similar mechanism — if every server on their local network disappeared, they would have no way to reach remote servers. In response to this:

### Word of Mouth
When clients run low on known servers (fewer than 20 by default), they ask both servers and other users whether they know servers the client doesn't, thereby spreading knowledge of remote servers to local (or, in any case, less remote) clients.

# Prerequisites
## Windows
You must have Docker, WSL, and make installed to run the makefile commands (recommended). Make can be installed with Chocolatey using:
```
choco install make
```

## GNU/Linux
You must install Docker to use the project.

# Usage
## Initialization
To run the project on a Docker network, run:
```
make prerun
```
This creates a Docker network through which our clients and servers will communicate, and a Docker volume where you can keep files for easy torrent-upload testing without repeatedly copying files into containers.

### Server
To run the server, use:
```
make redeploy-tracker
```
This cleans up previous runs of the server, rebuilds the Docker image, and starts a container from that image named `bittorrent-tracker`.

To bring up more containers on the same machine (for testing on a Docker network), use:
```
make redeploy-tracker-cluster
```
This does the same as the previous command, but with 5 (customizable) trackers named `bittorrent-tracker-i`, where `i` is the server's index relative to the other four. You should wait 5 seconds after starting the first tracker before starting the rest so they can properly join the same Chord ring; but once the first tracker is up, all subsequent ones can be created without any delay.

### Client
To create a client that uses the Docker volume created by the `prerun` Make command, use:
```
make redeploy-holder-client
```
To create a regular client without access to that volume, use:
```
make redeploy-client
```
And (again, for testing on a Docker network — multiple clients or servers should not run on one machine in a local environment) to bring up several clients at once, you can use:
```
make redeploy-client-cluster
```

## Using the program
Both the server and the client provide a Command-Line Interface (CLI), which you can access using:
```
docker attach (container name)
```
Both offer a `help` command you can use to explore the functionality the CLI provides.
