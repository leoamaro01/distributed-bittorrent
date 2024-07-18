help_text = """Commands in the CDL-BitTorrent Client CLI:

    help             Displays this page.
    download         Attempts to download a torrent with a given ID.
    upload           Registers this client as a seeder for a folder in your file system so other clients can download it.
    list-seeds       Displays every torrent this client is currently seeding.
    join-download    Shows live logs of a download.
    cancel-download  Cancels a download in progress.
    seed             Registers this client as a seeder for an existing torrent.
    exit             Closes the client.
"""

seed_help = """Registers this client as a seeder for an existing torrent.

Usage:
    upload [PATH] [TORRENT ID]  Registers this client as a seeder for TORRENT_ID, for a seed located in PATH.
    upload help                 Displays this page.
"""

cancel_download_help = """Cancels a download in progress.

Usage:
    cancel-download [TORRENT ID]  Cancels the download of TORRENT ID.
    cancel-download help          Displays this page.
"""

join_download_help = """Shows live logs of a download.

Usage:
    join-download               Lets you select a download to join.
    join-download [TORRENT ID]  Shows live logs of the download of TORRENT ID.
    join-download help          Displays this page.
"""

list_seeds_help = """Displays every torrent this client is currently seeding.

Usage:
    list-seeds       Shows seeds with their location.
    list-seeds help  Displays this page
"""

download_help = """Attempts to download a torrent with a given ID.

Usage:
    download [TORRENT ID] [OPTIONS]  Attempts to download a torrent.
    download help                    Displays this page.

Options:
    --join  Automatically joins the download log after the download starts.
"""

upload_help = """Attempts to download a torrent with a given ID.

Usage:
    upload [PATH]  Registers a new torrent for the file or folder located in PATH.
    upload help    Displays this page.
"""