help_text = """Commands in the CDL-BitTorrent Server CLI:

    help           Displays this page.
    delete         Deletes a torrent from the server.
    list-torrents  Displays the IDs of every torrent currently available.
    list-peers     Displays the IP address of every peer online.
    info           Displays info on a particular torrent, including seeders.
    exit           Closes the server.
"""

list_torrents_help = """Displays the IDs of every torrent currently available.

Usage:
    list-torrents       Lists every available torrent.
    list-torrents help  Displays this page.
"""

list_peers_help = """Displays the IP address of every peer currently online.

Usage:
    list-peers       Lists online peers.
    list-peers help  Displays this page.
"""

info_help = """Displays info on a particular torrent, including seeders.

Usage:
    info [TORRENT ID]  Displays torrent info.
    info help          Displays this page.
"""

delete_help = """Deletes a torrent from the server.

Usage:
    delete [TORRENT ID]  Deletes a torrent from the server.
    delete help          Displays this page.
"""