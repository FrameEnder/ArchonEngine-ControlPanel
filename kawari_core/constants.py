"""Constants mirrored from the Kawari source tree (redstrate/Kawari @ 538e289).

Everything here was read out of core/src/config.rs, resources/scripts/Global.lua
and resources/data/constants.yml rather than guessed, so when upstream moves these
are the values to re-check.
"""

from __future__ import annotations

APP_NAME = "Kawari Manager"
APP_VERSION = "1.6.0"
# Fallback ONLY - shown when no versioned build is active yet. The real value
# comes from the active build (its bundled README.md, plus upstream's
# SUPPORTED_* constants at that commit). See Context.patch_text().
KAWARI_PATCH = "unknown"
KAWARI_PATCH_FULL = ""
UPSTREAM_COMMIT = "49c945011"

BUILD_URL = "https://xiv.zone/distrib/kawari/kawari-linux-x86_64.zip"
BUILD_URL_ARM = "https://xiv.zone/distrib/kawari/kawari-linux-aarch64.zip"
OODLE_URL = "https://github.com/WorkingRobot/OodleUE/releases/latest/download/gcc-x64-release.zip"
DOCS_URL = "https://docs.xiv.zone/kawari/setup/overview.html"
REPO_URL = "https://github.com/redstrate/Kawari"

# ── Services ────────────────────────────────────────────────────────────────
# (key, label, default port, protocol, exposed to clients?)
# healthcheck (21064) is deliberately absent: the admin server reaches the world
# server over it inside the container, so publishing it gains nothing.
SERVICES = [
    ("admin",    "Admin",    21057, "http", False),
    ("frontier", "Frontier", 21058, "http", True),
    ("lobby",    "Lobby",    21059, "game", True),
    ("login",    "Login",    21060, "http", True),
    ("patch",    "Patch",    21061, "http", True),
    ("web",      "Web",      21062, "http", True),
    ("world",    "World",    21063, "game", True),
]
HTTP_PORTS = [p for k, l, p, proto, e in SERVICES if proto == "http"]
GAME_PORTS = [p for k, l, p, proto, e in SERVICES if proto == "game"]
ALL_PORTS = [p for k, l, p, proto, e in SERVICES]
HEALTHCHECK_PORT = 21064  # world.healthcheck_port, container-internal only

# ── Hosts entries ───────────────────────────────────────────────────────────
# Only the first four appear as defaults in config.rs; the rest are carried by
# the docs. 'launcher' is vestigial -- servers/launcher was deleted 2026-09-04.
HOSTS_NAMES = [
    ("ffxiv.localhost",              "web.server_name default"),
    ("frontier.ffxiv.localhost",     "frontier.server_name default"),
    ("patch.ffxiv.localhost",        "patch.server_name default"),
    ("ffxiv-login.square.localhost", "login.server_name default"),
    ("admin.ffxiv.localhost",        "admin panel (convenience only)"),
    ("config-dl.ffxiv.localhost",    "documented, unused by config defaults"),
    ("dctravel.ffxiv.localhost",     "documented, unused by config defaults"),
    ("launcher.ffxiv.localhost",     "VESTIGIAL - launcher server removed Sep 2026"),
]

# ── config.yaml schema, straight from core/src/config.rs ────────────────────
# (key, label, kind, default, tooltip)  kind: int|str|bool|list|port
CONFIG_SCHEMA = {
    "admin": [
        ("port", "Port", "port", 21057, "Admin panel HTTP port."),
        ("listen_address", "Listen address", "str", "0.0.0.0",
         "Bind address inside the container. Leave at 0.0.0.0 and control exposure with port publishing."),
    ],
    "frontier": [
        ("port", "Port", "port", 21058, "Frontier HTTP port."),
        ("listen_address", "Listen address", "str", "0.0.0.0", "Bind address inside the container."),
        ("server_name", "Server name", "str", "http://frontier.ffxiv.localhost:21058",
         "URL the client is told to use. Must be reachable FROM THE CLIENT, not from the server."),
        ("worlds_open", "Worlds open", "bool", True, "Whether worlds accept logins. Also toggleable from the admin panel."),
        ("login_open", "Login open", "bool", True, "Whether the login gate is open."),
    ],
    "lobby": [
        ("port", "Port", "port", 21059, "Lobby TCP port (game protocol, not HTTP)."),
        ("listen_address", "Listen address", "str", "0.0.0.0", "Bind address inside the container."),
        ("server_name", "Server name", "str", "127.0.0.1",
         "Bare host or IP -- no scheme. This is what the client connects to for the character list."),
    ],
    "login": [
        ("port", "Port", "port", 21060, "Login HTTP port."),
        ("listen_address", "Listen address", "str", "0.0.0.0", "Bind address inside the container."),
        ("server_name", "Server name", "str", "http://ffxiv-login.square.localhost:21060",
         "URL the client is told to use for login."),
        ("enable_registration", "Enable registration", "bool", True,
         "Allow new accounts via the web UI. Turn off once your accounts exist if the port is reachable by others."),
    ],
    "patch": [
        ("port", "Port", "port", 21061, "Patch HTTP port."),
        ("listen_address", "Listen address", "str", "0.0.0.0", "Bind address inside the container."),
        ("patches_location", "Patches location", "str", "patches",
         "Folder holding boot/ and game/ patch files, relative to the working directory."),
        ("server_name", "Server name", "str", "http://patch.ffxiv.localhost:21061",
         "URL the client is told to use for patching."),
        ("supported_platforms", "Supported platforms", "list", ["win32"],
         "Only the Windows client is supported upstream. Astra runs it under Wine, which counts as win32."),
    ],
    "web": [
        ("port", "Port", "port", 21062, "Web UI / account management port. Also serves /.well-known/xiv for launcher auto-config."),
        ("listen_address", "Listen address", "str", "0.0.0.0", "Bind address inside the container."),
        ("server_name", "Server name", "str", "http://ffxiv.localhost:21062", "URL the client and launchers are told to use."),
    ],
    "world": [
        ("port", "Port", "port", 21063, "World TCP port (game protocol)."),
        ("healthcheck_port", "Healthcheck port", "port", 21064,
         "Container-internal only. The admin server uses it to reach the world server; do not publish it."),
        ("listen_address", "Listen address", "str", "0.0.0.0", "Bind address inside the container."),
        ("server_name", "Server name", "str", "127.0.0.1", "Bare host or IP -- no scheme."),
        ("world_id", "World ID", "int", 63, "63 is Gilgamesh. Any valid World sheet row works."),
        ("enable_packet_obsfucation", "Packet obfuscation", "bool", False,
         "Upstream spells it 'obsfucation'; the key must match exactly. Off by default."),
        ("enable_packet_compression", "Packet compression", "bool", True, "On by default."),
        ("login_message", "Login message", "str", "Welcome to Kawari!", "Motd shown on entering the world."),
        ("generate_navmesh", "Generate navmesh", "bool", True,
         "Generates navigation meshes on demand. Slow on first visit to a zone, needed for NPC pathing."),
        ("accept_new_characters", "Accept new characters", "bool", True, "Allow character creation."),
        ("exp_bonus", "EXP bonus", "bool", False, "Applies an experience bonus."),
        ("language", "Language", "str", "en", "Game data language: en, ja, de, fr."),
        ("active_festivals", "Active festivals", "intlist", [0, 0, 0, 0, 0, 0, 0, 0],
         "Eight festival id slots ([u16; 8]). 174 is Moonfire Faire (2026); the full "
         "list is resources/data/festivals.yml. Applied at zone init, so re-enter the "
         "zone after changing. The !festival command only reaches the first four."),
    ],
    "filesystem": [
        ("game_path", "Game path", "str", "",
         "REQUIRED. The folder containing sqpack/ and ffxiv_dx11.exe. Inside the container this is /game."),
        ("additional_search_paths", "Additional search paths", "list", [], "Extra game data search paths."),
        ("unpack_path", "Unpack path", "str", "", "Optional path to unpacked game files; navmesh generation can read these."),
        ("navimesh_path", "Navmesh path", "str", "navimesh", "Where generated navmeshes are cached, relative to the working directory."),
        ("additional_resource_paths", "Additional resource paths", "list", [],
         "Overlay directories for custom Lua scripts and resources. The data/ and web/ folders cannot be overlaid."),
    ],
    "tweaks": [
        ("always_allow_skipping", "Always allow skipping", "bool", False, "Lets you skip any cutscene."),
        ("enforce_validity_checks", "Enforce validity checks", "bool", True,
         "Server-side sanity checks on client input. Turning this off makes debugging easier and cheating trivial."),
    ],
}

CONFIG_SECTION_HELP = {
    "admin":      "The admin panel server. It has NO authentication of any kind -- every route is open to anyone who can reach the port.",
    "frontier":   "Serves the launcher's news/banner endpoints and the worlds-open gate.",
    "lobby":      "Character select. server_name here is a bare host, not a URL.",
    "login":      "Account login and session issuing.",
    "patch":      "Serves boot and game patches. Point patches_location at a folder with boot/ and game/ subfolders.",
    "web":        "Account management UI, Auracite import, and /.well-known/xiv auto-config for launchers.",
    "world":      "The actual game server.",
    "filesystem": "Where Kawari finds game data and where it writes generated files.",
    "tweaks":     "Behavioural switches that have no retail equivalent.",
}

# ── Territories (resources/scripts/Global.lua) ──────────────────────────────
TERRITORIES = [
    (132, "New Gridania"), (133, "Old Gridania"), (128, "Limsa Lominsa Upper Decks"),
    (129, "Limsa Lominsa Lower Decks"), (130, "Ul'dah - Steps of Nald"),
    (131, "Ul'dah - Steps of Thal"), (148, "Central Shroud"), (152, "East Shroud"),
    (153, "South Shroud"), (154, "North Shroud"), (140, "Western Thanalan"),
    (141, "Central Thanalan"), (145, "Eastern Thanalan"), (146, "Southern Thanalan"),
    (147, "Northern Thanalan"), (134, "Middle La Noscea"), (135, "Lower La Noscea"),
    (137, "Eastern La Noscea"), (138, "Western La Noscea"), (139, "Upper La Noscea"),
    (180, "Outer La Noscea"), (155, "Coerthas Central Highlands"), (156, "Mor Dhona"),
    (418, "Foundation"), (419, "The Pillars"), (478, "Idyllshire"),
    (635, "Rhalgr's Reach"), (628, "Kugane"), (819, "The Crystarium"),
    (820, "Eulmore"), (962, "Old Sharlayan"), (963, "Radz-at-Han"),
    (1185, "Tuliyollal"), (1186, "Solution Nine"),
    # Gold Saucer (PlaceNameZone 503) -- instanced ones need a working director
    (144, "The Gold Saucer"), (388, "Chocobo Square"),
    (1197, "Blunderville Square"), (1360, "Keybound Square"),
    (390, "Chocobo Race: Sagolii Road"), (389, "Chocobo Race: Costa del Sol"),
    (391, "Chocobo Race: Tranquil Paths"), (506, "LoVM: Stages / Master Battle"),
    (831, "The Manderville Tables"), (579, "The Triple Triad Battlehall"),
    (792, "Leap of Faith: The Fall of Belah'dia"), (1165, "Blunderville"),
    # Field operations and deep dungeons (instanced)
    (732, "Eureka Anemos"), (763, "Eureka Pagos"), (795, "Eureka Pyros"),
    (827, "Eureka Hydatos"), (570, "Palace of the Dead - entrance"),
]

# ── GM ranks (resources/scripts/Global.lua) ─────────────────────────────────
GM_RANKS = [(0, "Normal player"), (90, "Debug (full GM commands)")]

# Commands worth surfacing in the UI. The //gm pos unit quirk is genuinely
# surprising, so it is called out here rather than left to be discovered.
QUICK_COMMANDS = [
    ("!inspect",              "Print zone, position, rotation, gil, equipment and inventory"),
    ("//gm teri_info",        "Print zone id, weather, internal/region/place name, intended use"),
    ("//gm teri <id>",        "Change territory -- the way out of a broken instance"),
    ("//gm pos <x> <y> <z>",  "Teleport within the zone. COORDINATES ARE x100: multiply !inspect values by 100"),
    ("!nudge <dist> [up|down]", "Move relative, in yalms. No unit conversion"),
    ("!unlock 1 / !unlock 4", "Unlock Return / Teleport actions"),
    ("!finishevent",          "Break out of a stuck event script"),
    ("!fateinfo / !fatecomplete", "Inspect or complete the FATE you are standing in"),
    ("!skipintro",            "Teleport to Limsa, unlock everything, complete all quests"),
]

# ── Modes ───────────────────────────────────────────────────────────────────
MODE_LOCAL = "local"
MODE_NETWORK = "network"
MODE_PROXY = "proxy"
MODE_LABELS = {
    MODE_LOCAL:   "Local only (same machine)",
    MODE_NETWORK: "LAN / Tailscale (other machines connect directly)",
    MODE_PROXY:   "Behind a reverse proxy",
}
MODE_HELP = {
    MODE_LOCAL: (
        "HTTP services bind to 127.0.0.1; the two game ports still bind 0.0.0.0 because "
        "the client needs them. config.yaml keeps Kawari's *.ffxiv.localhost defaults, "
        "which your hosts file resolves."
    ),
    MODE_NETWORK: (
        "Everything binds 0.0.0.0 and all six server_name values are rewritten to the "
        "address below. Note this exposes the unauthenticated admin panel to anything "
        "that can reach the host -- see the Admin section."
    ),
    MODE_PROXY: (
        "Everything binds 0.0.0.0 and server_name values point at the proxy. Hosts file "
        "entries are not needed; the proxy resolves the names instead."
    ),
}
