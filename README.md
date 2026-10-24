# Kawari Web

A browser front end for a self-hosted [Kawari](https://github.com/redstrate/Kawari)
FFXIV server, built to run in a rootless Podman container. Version 2.22.0.

How its features work, and how they depend on the FrameEnder fork of Kawari, is documented in the fork's
`docs/src/fork/kawari-web.md`; that page is updated with every change here.

It shares its logic with the desktop manager: `kawari_core/` is the same
database, Auracite, schema and version code, with the Qt dependency removed.

## Run it

```bash
cd ~/Documents/Server/FFXIV/kawari-web
./run.sh
```

Then open `http://127.0.0.1:7301`. Use the literal address, not `localhost`:
`localhost` resolves to the IPv6 loopback `::1` first on most systems, and
rootless Podman publishes on IPv4 only, so the browser's first attempt is refused
and it does not fall back. From another machine, use the server's LAN or
Tailscale address and open the port in firewalld.

`run.sh` builds the image, mounts your
`~/.kawari` directory, and mounts the rootless Podman API socket if it exists.

To let the dashboard start and stop the game server, enable the socket once:

```bash
systemctl --user enable --now podman.socket
```

Without it the site still works; only the container controls and log view are
disabled, and the dashboard says so rather than failing silently.

## Live character sync

**Settings → Character sync** issues a token for the Kawari Companion plugin,
which uploads the character from the game client on a schedule. The endpoint is
`POST /api/sync` with `Authorization: Bearer <token>`, taking an Auracite
character document or zip, and `GET /api/sync/ping` reports which characters the
token may write.

Every rule is enforced here rather than in the plugin, which runs on a machine
this server does not control:

* a token is bound to one account; only characters on that account can be written
* a character logged in on a running server is refused, since the world server
  would overwrite the sync on logout
* a snapshot is taken before every applied sync, so it is undoable
* nothing is created and nothing is renamed - an unknown name is refused
* rate limited to one sync per 30 seconds, payload capped at 6 MB
* which sections a sync may touch is yours to choose; appearance is opt-in

## Running it as a service

`run.sh` is for trying things out: the container restarts if it crashes, but
nothing starts it after a reboot. For a permanent install:

```bash
./install-service.sh
```

That builds the image, enables linger (without which a rootless user service
stops when you log out), writes a systemd unit with your paths, starts it and
checks it answers. `Restart=always` is systemd's equivalent of
`--restart unless-stopped`: it comes back whenever it exits, and
`systemctl --user stop kawari-web` keeps it stopped until you start it again.

It uses a quadlet unit where podman supports it (4.4 and later) and a plain
systemd unit otherwise. `kawari-web.container` is the quadlet it writes, if you
would rather place it by hand.

```bash
systemctl --user status kawari-web      # how it is doing
systemctl --user restart kawari-web
systemctl --user stop kawari-web        # stays stopped
journalctl --user -u kawari-web -f      # follow the log
./install-service.sh --remove           # undo it
```

The installer refuses to go ahead if the data directory is not writable or your
user has no `/etc/subuid` entry, since both produce failures that look like
something else later.

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `KAWARI_DATA_DIR` | `~/.kawari` | Kawari config directory: databases, config.yaml, backups |
| `KAWARI_WEB_DIR` | `<data>/web` | Web app state: `webapp.db` and uploads |
| `PODMAN_SOCKET` | *(unset)* | Path to the mounted Podman API socket |
| `KAWARI_HOST_DATA_DIR` | same as data dir | The data directory **as the host sees it** |
| `KAWARI_WEB_HOST` / `KAWARI_WEB_PORT` | `0.0.0.0` / `7301` | Bind address inside the container |

The default is 7301 rather than 8080, which is taken on most machines running
anything else. To use a different one:

```bash
PORT=9000 ./run.sh          # publishes 9000 on the host
```

`PORT` only changes the host side of the mapping. The app inside the container
always listens on `KAWARI_WEB_PORT`, and `run.sh` maps `PORT` to it, so the two
never have to agree. If you change the published port, remember the value shown
on the How to Connect page is Kawari's own web port (21062), not this one — they
are different services.

## Moving the server to another machine

`settings.conf` records absolute **host** paths (`DATA_DIR`, `GAME_PATH`) from
wherever the server was first set up, so copying the data directory to a
different host leaves them pointing at nothing - a different username, or
`/var/home` on an ostree system versus `/home` elsewhere, is enough.

The app no longer trusts those paths blindly: it tries the configured location,
then the conventional `<data dir>/data`, and uses whichever actually holds the
databases. When the configured path is wrong it says so on the Overview, Database
and Setup pages, with a **Repair paths for this machine** button that rewrites
`DATA_DIR`. That matters beyond display, because the same value builds the game
container's bind mount.

The game path is separate and cannot be guessed - set it on the Setup page if the
game files moved too.

## If it will not start

```bash
./diagnose.sh
```

That prints which image and container actually exist, when each was built, the
user namespace mode in effect, what the container sees at `/data`, and whether
the running image contains the current code. Since 2.1.3 the app also prints a
banner before anything can fail:

```
Kawari Web 2.1.3 starting
  uid=1000 gid=1000
  data dir : /data
  data dir owner uid=1000 gid=1000, writable=True
```

If the version in that banner is not the one you just built, an old container is
still running - `run.sh` now stops a quadlet-managed unit first, and prints the
image and container IDs after starting.

## First run

Until an administrator exists, every URL redirects to `/setup`, which offers two
routes:

- **Import the accounts already on this server.** Reads `login.db`, gives every
  account a profile, and you choose which one is the administrator. Passwords are
  untouched — people sign in with their game password.
- **Create the first account.** Creates a real Kawari account (a `user` plus its
  `service_account`), so it works in the game client too. This also builds
  `login.db` from scratch if the server has never run, recording the Diesel
  migration the way the server does so it is not applied twice.

## Pages

**Home** — server status and population, service reachability dots, the message of
the day, a three-step "how to start playing" strip (with the autoconfig URL and a copy
button), the latest three community posts (with a composer when signed in), the three
most recent announcements, the connection details, the player list (marked *In game*
while one of their characters is online and the server is running) and the
configurable links.

**How to Connect** — a walkthrough for Astra, with this server's live values
filled in: the autoconfig URL to paste, and the individual field values in case
they are needed by hand. It warns when the server is in local-only mode, because
then no remote client can reach it.

**Community and profiles** — the top bar calls the feed **Community**. Posts with text (up to 2000 characters) and up to four
images, comments, and threaded replies. A single image is shown whole rather
than cropped; two, three or four tile into a fixed frame. Clicking one opens a
viewer in the same tab, with arrow keys, Escape and a counter for multi-image
posts. The markup is plain links, so with JavaScript off an image still opens. Authors can edit or delete their own
posts and comments; deleting a comment removes its replies with it.
Administrators can remove anyone's. Every profile shows that player's posts; the
Community page shows everyone's, can be narrowed to posts with screenshots, and has
a *Who's online* card and the latest announcements beside it.

**Shop** — the FINAL FANTASY XIV Online Store's catalogue (829 listings in 16
categories: mounts, minions, emotes, Tales of Adventure, Fantasia, outfits, gear,
dyes, barding, orchestrion rolls, housing...), for everyone to browse and search,
and to buy with **gil** or the site's own **play coins**. See [Shop](#shop).

**Notifications** — a bell in the top bar with an unread badge and a dropdown.
Delivered for comments on your posts, replies to your comments, the game server
starting or stopping, administrator announcements and shop deliveries. The
Notifications page groups them into Today and Earlier and filters them by kind or
to the unread ones (`?kind=`). Turning a kind off in
Settings stops delivery entirely rather than hiding it after the fact. Server
state is noticed on requests, rate-limited to one check every 20 seconds, since
there is no scheduler.

**Settings** — categorised: Profile, Password & account, Notifications, Character
sync and Character visibility. Password & account changes the password for this site **and** the game launcher, because
there is only one account; it verifies the current password first.

**Top bar** — brand and navigation (Home, Community, Shop, How to connect) on the
left, bell and account menu on the right. Signed out it shows Sign in and Register
instead. The menus are `<details>` elements, so none of it needs JavaScript. On a
phone the brand and account stay on one row and the navigation scrolls under them.

**Look** — a dark slate theme (with the light theme still available in Branding),
headings in Sora, text in IBM Plex Sans and IDs, ports and logs in IBM Plex Mono. The
fonts are bundled in `kawari_web/static/fonts` (SIL Open Font License, licence files
alongside), so nothing is fetched from outside and the site still works offline in
its container. Icons are inline SVG.

**Profile** — banner, avatar, bio, and two tabs: **Posts** (the default) and
**Characters**, each with a count. Each character is a slim card (name, title and
Grand Company, current class and level, playtime, gil, classes levelled, duties
unlocked, the top classes, online or where it was last seen) with a **Full stats**
button that opens a pop-up holding everything: every class and job grouped by role
(shaded by level, with an EXP bar; hover for the exact EXP and soul crystals), every
unlock group as progress meters at once, travel, duties, the whole purse, and the
owner's show/hide switch. The pop-up works without JavaScript (it's a `:target`
overlay); with it, Escape and a click outside close it and focus returns to the
button. The owner also sees their sync status and latest purchases beside the
cards. The tab is a query parameter rather than
client-side state, so a link to someone's characters stays one, and the character
stats are only computed when that tab is actually open. Characters are
**private by default**: a visibility row exists only once the owner publishes one,
so absence means hidden.

**Dashboard** (administrators only) — server start/stop/restart, service and
client-version checks (Overview's **Client compatibility** compares your client's
`.ver` files with what the active build supports — the same *Supports* as Updates. In
the container Kawari Web can't see the game folder, so it reads them through a
short-lived container from the server's image with the install mounted read-only, and
keeps the answer for ten minutes; **Check again** reads them now), the full `config.yaml` editor, database health and
migration, backups, account creation and admin promotion, Auracite import and
export, branding, quest progress, marketboard prices, and container logs. Non-admins get a 403 on every dashboard
route, including the POST actions, not just a hidden nav link.

**Announcements** (Dashboard → Announcements) sends a notification to every account
that hasn't turned announcements off, with a live preview of how it will look, and
keeps a list of everything sent (when, by whom, how many accounts got it). Overview
keeps a quick form for it too.

It has its own layout: a sidebar grouped by job — **Server** (Overview, Logs,
Diagnostics), **Build & config** (Setup, Updates & branches, Server config), **Game
data** (Database & accounts, Marketboard), **Content** (Quest lab, Quest progress,
Duties & zones) and **Community** (Shop, Announcements, Branding) — and a header with
the page's group and title. Shop management and listing editing live in the dashboard
too. On a phone the sidebar folds behind a **Menu** button, which is a checkbox, so it
needs no JavaScript.

**Storage limits** (Dashboard → Database) — every update and build switch backs up both
databases and snapshots every character, and every edit, sync and import snapshots that
character, so without a limit they fill the disk a little more with each update. Kawari
Web keeps the newest 20 backups and the newest 30 snapshots per character (change them
there; 0 keeps everything) and deletes the oldest whenever a new one is made. A backup
that a build still on disk would restore when you switch back to it is never deleted
automatically. Old builds beyond **Builds to keep** (Updates → Update settings) are
removed at the end of every install. Backups and a character's snapshots can also be
ticked and deleted together.

**Branding** — server name, tagline, emoji favicon, logo, accent colour, light or
dark theme, footer text, and the Home page links. The default accent is `#5aa9f0`; an
accent already saved is kept. Text on accent-coloured buttons is picked automatically
(dark or white, whichever reads better), so any accent stays legible.

## Quest Lab

**Dashboard → Quests** wraps the quest-scripting loop around the fork's
`kawari-questdump` (FrameEnder/Kawari, `tools/questdump`): dump a quest, read
what the client does, script it, build, put a character at the right point,
test.

* **Running the tool.** It runs in a throwaway container made from the
  *active* server image, with the server's game folder and config mounted
  read-only, so it always matches the running build and the server needn't be
  up. Output lands in `<data>/quest-dumps/`. Story chains, specific quests (by
  ID, script or name), raw game files, and `index`. Only a build of the fork
  from source includes the tool; the page says so otherwise.
* **The quest index** (`kawari-questdump index`, added in fork commit 4556bf1)
  is every quest's name, level, prerequisites, issuer and area. It powers
  search across all quests and prerequisite completion. Without it both still
  work, over the dumped quests only.
* **Script check.** Each dumped quest's script is read from the active build
  (what the server runs now) and compared with its dump. **Needs a look**
  means it names a quest variable the Quest sheet doesn't have (silently `nil`
  in Lua: a typo or version drift) or plays a scene the client script doesn't
  contain. Scenes are counted as played through `SEQ_*` variables, literal
  numbers and the `SCENE_*` constants the Sapphire-derived scripts use; client
  scenes the script never names are listed. A `-- NOTE:` mentioning
  "unverified" marks the script **unverified**.
* **Quest page.** Variables (the ones the script uses in bold), objectives,
  listeners and rewards; the fork script beside the client listing; the
  quests before and after; downloads of each dump file.
* **Testing.** Copyable `//gm quest …` commands work live while logged in.
  "Set up a character" edits the database instead (logged out, snapshot
  first): it completes the prerequisites, raises the current class to the
  quest's level, and leaves the quest ready to accept, in progress at a chosen
  sequence, completed, or never started. "Any one of" prerequisites take one
  branch — one already done, else the quest's own city — so a Gridania tester
  doesn't get Limsa's and Ul'dah's story too. Quest completion is Kawari's
  most-significant-bit-first quest bitmask.
* **Test this quest.** One test session from setup to bundle: **Start the test** sets up the character as
  above, turns packet capture on (restarting the server if you tick it, since the server only reads that at
  startup) and notes the time. Play the quest, then **Finish the test and send it**: a diagnostics bundle
  covering exactly the test, with the quest in its `meta.json` (`quest_test`) and README, so the fork's
  `questkit triage` follows that quest and `questkit compare` knows what to check. It can turn capture back
  off if the test turned it on. **Cancel** sends nothing. One test runs at a time; Diagnostics shows it too.
* **Bundle.** One zip of the selected dumps, the scripts the server runs for
  them, `Global.lua`, optionally the index, and a `MANIFEST.md` of the checks:
  the attachment for whoever writes the next quests. Dumps are Square Enix's
  files; keep them private and out of git.

## Quest progress

**Dashboard → Progress** tracks how much of the game's story the server can play:
every quest from A Realm Reborn to Dawntrail, against the quest scripts in the
**active build**.

* **Where the quests come from.** The quest index (**Build the quest index**, here
  or in Quest Lab), which `kawari-questdump index` makes from the server's own
  game install. So the list is the game's own, up to whatever patch is installed
  (7.57 today); build it again after a game update to pick up new quests. No game
  text is kept in this repository.
* **Main Scenario or side quest.** A quest is Main Scenario when it is in the
  game's ScenarioTree sheet, the same list the Scenario Guide uses. Everything else
  the journal lists is a side quest, filed under its journal section (Sidequests,
  Class & Job Quests, Feature Quests...) - which the page can filter by. Quests the
  journal never shows (unused or internal rows) aren't counted.
* **Implemented / verified.** Implemented means the build has the quest's script
  (`resources/scripts/events/quest/<id / 100>/<script>.lua`); verified means the
  script's header says it was checked against a retail recording.
* Totals per expansion for Main Scenario and side quests, then the full list,
  filterable by expansion, type, journal section, status and a search by ID,
  script or name. Each quest links to its Quest Lab page.

**Misc Content** (the second tab of Progress) does the same for everything that
isn't a quest: every dungeon, trial, raid, ultimate raid, deep dungeon, variant
dungeon, guildhest and other duty, and every open-world zone, per expansion.

* **Where they come from.** The content index (**Build the content index** on the
  tab), which `kawari-questdump content` makes from the game install: every duty
  (ContentFinderCondition) with its content type, short code, territory, levels and
  expansion, and every open-world zone. It needs a fork build from 2026-10-18 on.
* **Scripted** means the active build has the duty's content script
  (`resources/scripts/content/<short code>.lua`, what the server runs when it
  starts); **has spawns** means one of the build's drop-ins adds battle NPCs to its
  map. A duty is counted done with both, started with one. A zone is counted when
  it has monster spawns; without them it's empty on Kawari.
* It shows what the server has, not how well it plays.

The quest index needs a fork build whose `kawari-questdump` writes format 2 (main
scenario flag, expansion and journal section). With an older index the page still
works, but says so and counts every quest as a side quest.

## Shop

The **Shop** tab sells the Online Store's catalogue on your server. It's seeded
from `kawari_core/data/shop_seed.json` (gathered from the Console Games Wiki,
Square Enix's Tales of Adventure guide and Eorzean Tavern; the retail price is
kept for reference) and lives in the web app's own database from then on.

* **Two prices per listing**, 1 by default:
  * **Gil** comes out of the character the buyer picks at the top of the shop.
    The world server keeps a logged-in character in memory and saves it over any
    change, so gil can only be spent while that character is logged out.
  * **Play coins** belong to the web account. They're earned from playtime, every
    character on the account counting, at 25 an hour by default. The fork's
    server saves playtime with every autosave, so coins add up while people play.
* **Delivery** is how retail delivers store purchases: a reward letter by moogle
  mail, from the World, with the items attached (five to a letter). Take them from
  any mailbox in game. This works for a logged-in character too, because the
  server reads mail when the mailbox opens.
* **Linked to game items.** A listing's contents are game items by name and
  quantity. **Manage the shop → Read the item list from the game** runs
  `kawari-questdump items` (a fork build's), which writes every item's ID, name
  and description from your game install; contents are matched against it. A
  listing whose items the game doesn't know, or with several items of the same
  name, can't be bought until it's fixed: give the right one's ID (`Name #12345`).
  162 costume sets came without their pieces listed; those need contents first.
* **Administrators** edit any listing (name, category, description, contents,
  both prices, whether each currency is accepted, hidden or shown), add and delete
  listings, set prices for a whole category at once, rename the play currency,
  change its rate, adjust anyone's balance, change the letter that comes with a
  purchase, and can reset the listings to the original catalogue.
* **Pictures.** Every listing has a thumbnail. By default it's the game's own icon
  for the listing's first item: **Manage the shop → Fetch item icons** runs
  `kawari-questdump extract` for each listing's icon (the high-resolution copy when
  the game has one) and turns the textures into PNGs in `<data>/web/item-icons/`,
  so they come from your game install and never sit in this repository. It needs the
  item list read first, fetches only icons it doesn't have yet (tick *Fetch them all
  again* to refresh), and Pillow decodes the game's texture formats. Administrators
  can also upload up to 8 preview images per listing (Edit listing → Pictures, 4 MB
  each); the first is the cover, which replaces the icon as the thumbnail, and the
  listing's page shows them all as a gallery that opens in the image viewer.
* Buyers get a notification when an order is delivered, and **Your purchases**
  lists their orders.

## Marketboard

**Dashboard → Marketboard** sets what everything on the in-game marketboard
costs (the fork's server lists every tradable item there, 50 listings each by
default). It's laid out like the board: the four tabs (Weapons, Armor, Items,
Housing), each tab's search categories, each category's items.

* **An item's price** is *gil per level × its level + its rarity's bonus*. The
  level is the equip level or the item level (a setting per tab: equip level for
  weapons and armor, item level for the rest). HQ multiplies the rarity bonus
  (×2 by default; a white item's bonus is 0, so its HQ costs the same).
* **Built-in rates**: Weapons and Armor 100 gil a level with 0 / 2,000 / 4,000 /
  8,000 / 20,000 for white / pink / green / blue / purple; Items (crafting
  supplies and everything else) 3 a level with those bonuses divided by 100;
  Housing 50 a level with the full bonuses.
* **Three levels of settings**: each tab's rates; a category can change any of
  them (empty fields keep the tab's); any item can have its own NQ and HQ price
  (empty keeps the computed one, shown greyed). **Find an item** searches by name.
* **Listings**: how many each item has, how many of those are HQ (for items that
  can be), the largest stack, and the tax buyers pay on top (5 %, as retail).
* **Latest sales** lists what players bought.

Changes are written to the server's `world.db` (the `marketboard_*` tables) and
the server reads them whenever the board is used, so they apply at once, logged
in or not. The items and categories come from your game install: **Read the
item list from the game** runs `kawari-questdump items` (the fork's, from a build
with the marketboard), which also writes `marketboard-categories.json`.

## Bot glamours

**Dashboard → Bot glamours** holds the outfits the fork's party bots wear over
their gear: `glamours.txt` in the server's data folder (beside `world.db`, which
the container sees as `/kawari/data`). The world server reads it again whenever
it changes, so a new list applies to the next bot that joins, with no restart.

* **The Kawari Glamour Collector** (a Chrome extension, in the Kawari
  repository's `tools/glamour-collector`) collects glamours from Eorzea
  Collection and sends the list here. In its popup, under **Kawari Web**, give
  this site's address as your browser reaches it (on your LAN, e.g.
  `http://192.168.1.20:8080`, or your tailnet, e.g.
  `http://myserver.tail1234.ts.net:8080`) and your token from **Settings →
  Character sync**; it must be an administrator's. **Connect** asks Chrome to let
  the extension reach that address, then **Send to Kawari Web** replaces the list
  and **Load from Kawari Web** adds the server's glamours to the extension's.
* **The class list**: the extension's Class dropdown is filled from your game
  install's classes and jobs. **Read the class list from the game** runs
  `kawari-questdump classes` (the fork's), which writes `class-index.json`.
* **Earlier lists**: each time a list is sent, the one it replaces is kept (the
  newest 20, in the web folder's `glamour-backups/`) and can be restored.

The API, for the extension (the sync token, an administrator's):
`GET /api/glamours/ping`, `GET /api/glamours/classes`, `GET /api/glamours` (the
list's text) and `POST /api/glamours` (`{"text": "..."}`, replaces it).

## Auracite backups

**Dashboard → Database → Import an Auracite backup** applies a backup to an existing character (the world server's
own importer only creates new ones); **Export** on a character's row writes one. A snapshot is taken before an import,
and a character who is logged in on a running server is refused, since the world server would save over the import
when they log out. **Inspect only** shows what a backup holds without importing anything.

Backups from the **extended Auracite plugin** (Auracite for Kawari, in the kawari-diagnostics repository) carry a
`kawari_extended` section on top of Auracite's own. Kawari Web imports what Kawari has a place for:

* **Inventory**: crystals, key items, the chocobo saddlebag, and the glamour dresser, glamour plates and armoire;
* **Quest progress**: the quests in progress (sequence and variables), completed levequests and legacy quests, map
  markers and the gathering log;
* **Chocobo companion** (its own section, also offered to Character sync): name, rank, EXP, skill points and lines,
  stance, favourite feed, colour and barding.

Some of it only exists in the game once its window has been opened (the glamour dresser, the armoire, the saddlebag,
achievements, a retainer's items): the plugin marks those "not loaded" when they weren't, and the import leaves what
the character has alone rather than clearing it. Everything else in the section (gear sets, hotbars, macros,
achievements, retainers, logs and records) is kept whole with the character, in `world.db`'s `auracite_extended`
table (the fork's migration of 2026-10-24), and an export writes it back, with Kawari's current data for the parts it
stores: a Kawari → Auracite → Kawari round trip loses nothing.

## Diagnostics

**Dashboard → Diagnostics** gathers what someone debugging the server remotely
needs, and commits it as one bundle to a private GitHub repository:

* the world server's recent log;
* packet captures - turn capture on here (it sets `world.packet_capture` in
  config.yaml and writes to `<data>/captures`, which survives rebuilds; needs a
  fork build from ed897d8 on). Turn it off when you're done, the files grow fast;
* the chosen characters' saved quest state and position, read from world.db;
* snapshots from the **Kawari Probe** Dalamud plugin, which reports what the
  game client itself believes (its quest list and variables, key items, zone,
  position, conditions). It posts to `POST /api/diag/snapshot` with the same
  token as character sync;
* which build is running, and the server config with secrets removed.

**Battles from Kawari Recorder.** The Recorder (2.6+) can send each retail battle it records straight here:
in its Battles tab, under *Send to Kawari Web*, enter this site's address and your character sync token
(the same as the Probe's), then *Test*. Each battle arrives at `POST /api/diag/battle` as one gzipped file,
is kept in `<web>/diag/battles`, and is filed in the diagnostics repository under `battles/` straight away
(with `battles/index.json` listing every battle and its label). The Diagnostics page lists them; *File now*
retries one that couldn't be filed. A bundle's README names the battles received during its window.

It needs the repository (`owner/name`) and a fine-grained GitHub token limited
to that one repository with *Contents: Read and write*. Game files never go in a
bundle.

The page can also point `filesystem.navimesh_path` at `<data>/navimesh`, so
navmeshes are kept between builds instead of regenerated (slowly) after every
container rebuild.

## Tracking a fork

Everything that reaches out to the Kawari project goes through one place
(`kawari_core/upstream.py`), so **Dashboard → Updates → Source** can point the
whole installation at any repository on any of the supported forges. Three
things move together, and they have to:

* the **API**, for the changelog, the commit list and how far behind the
  active build is
* the **source files at a commit**, which the database schema map is *derived*
  from. A fork that changes a Diesel migration must be read from the fork, or
  the generated migration would reconcile the database against the wrong column
  list
* the **build**: an archive to download, or the checkout a source build compiles

### Forges

Paste the repository's web address; the forge, owner, repository and any branch
in the URL are worked out from it.

| Forge | Example | Notes |
| --- | --- | --- |
| **Codeberg** (Forgejo) | `https://codeberg.org/redstrate/Kawari` | Upstream's home since it left GitHub, and the default. |
| **Gitea / Forgejo**, self-hosted | `http://100.79.235.78:3769/ProtoPropski/Kawari` | Plain http and a Tailscale address are fine; this machine must be on the tailnet. |
| **GitHub** | `https://github.com/FrameEnder/Kawari` | GitHub Enterprise through the API override. |

Codeberg runs Forgejo, a fork of Gitea, so Codeberg and a self-hosted Gitea or
Forgejo are one code path. It was tested against a real Gitea 1.24 with a
**private** repository. The differences from GitHub are handled: Gitea's compare
lists commits newest first and counts `total_commits` (GitHub: oldest first,
`ahead_by`); Gitea authenticates API and git with `token <t>`; its raw-file
endpoint serves private files directly; a private release asset downloads from
the web host. Where a Gitea or Forgejo lacks `/compare`, the history is walked
instead.

**Tokens are per forge**: a GitHub token is useless on Codeberg. A token is
sent only to the forge's own API and web hosts, never to a third-party archive
URL, and it is what a source build uses to clone a private repository.

**Upstream moved.** A source saved by 2.6–2.8 meant GitHub and stays on GitHub,
so a saved fork there keeps working. One still tracking upstream's old GitHub
repository gets a one-click **Track codeberg.org/redstrate/Kawari**.

### Builds

Upstream publishes the newest build to xiv.zone, which a fork obviously does
not, so the build can come from any of:

| Build source | What it does | Specific commit? |
| --- | --- | --- |
| Download: direct archive URL | Downloads a fixed `.zip`. How upstream publishes, so for a fork it installs **upstream's** code. The Source panel says so in red. | No: the URL serves what it serves |
| Download: latest CI artifact | Newest green CI run on the branch. GitHub; Gitea 1.23+. Forgejo has no artifact API yet. | Yes, by the run's commit |
| Download: latest release asset | Newest release's asset. | Yes, by tag |
| **Build from source: clone the branch** | Fetches and compiles here. | **Yes: any commit, short commit, tag or branch** |
| **Build from source: a folder on this server** | Compiles the checkout you are editing, uncommitted changes included. | Check it out in the folder |

Upstream's CI moved to Forgejo Actions on Codeberg. Whether its deploy job still
publishes to xiv.zone could not be checked from here; if the URL stops
updating, build from source.

### Installing a specific commit

**Updates → Install a specific commit** takes any commit, short commit, tag or
branch, and "Check for updates" lists the branch's newest 30 commits with an
Install button each (the active one is marked; one already installed points at
the Version history, where switching is a retag). An install goes through the
same steps as an update: backup, character snapshot, schema map *from that
commit*, image, activate, recreate. A short commit cannot be fetched by name,
so the builder falls back to fetching every branch and tag and resolving it.

### Building from source

For working on the server itself. A builder image (`localhost/kawari-builder`:
the Rust image plus cmake, clang/libclang and rsync, which recastnavigation's
bindgen needs) is built once; each build runs a throwaway container through the
Podman socket and packages exactly the file list CI's upload step does. From
there it goes through the normal pipeline: backup, schema, image, activate,
migrate, recreate.

* **Folder mode** takes the checkout's path **on the host**. It is mounted
  read-only and synced into a separate build area, honouring `.gitignore`, so
  your `target/`, `config.yaml` and databases are untouched. Committed and
  clean, a build is versioned by its commit. With uncommitted changes it is
  `<commit>-dev<yymmddHHMMSS>` and tagged *dev* in the history, so it can never
  pass for the commit it started from — and each rebuild gets its own image tag.
* **Dependency mirrors** (Updates → Dependency mirrors): Kawari's `Cargo.toml`
  fetches physis, Icarus and recastnavigation-rs-sys from GitHub, and the last
  pulls the recastnavigation C++ library in as a git submodule. Give one line per
  repository, `<original URL> <your mirror's URL>`, and the builder turns them into
  `git config url.<mirror>.insteadOf <original>` with cargo fetching through git
  (`CARGO_NET_GIT_FETCH_WITH_CLI`), so cargo's fetches and the submodule come from
  your Gitea or Forgejo while `Cargo.toml` and `Cargo.lock` stay as upstream has
  them. Each mirror must have the commits the lock file asks for (a pull mirror
  that keeps syncing) and be readable without signing in; for Icarus, mirror every
  branch, since each game patch gets its own `ver/...` branch. The build log lists
  the mirrors it used.
* **The schema comes from the tree that was compiled**, not GitHub: an
  unpushed migration change is exactly what the database step must see.
* **Kept between builds** under `<data>/source/`: the cargo registry, the git
  checkout and the target dirs. Measured on a 2-core machine: the first build
  of the workspace took 4.5 minutes in fast mode, and an edit to the world
  server rebuilt in under a second. *Clear build cache* starts over.
* **Fast incremental build** (default) turns off the workspace's LTO and single
  codegen unit. Untick it to build exactly as CI does, which is much slower and
  has no incremental rebuilds.
* "Build and install" is always offered in these modes, because only the build
  knows whether anything changed. If it compiles to the active build, nothing is
  installed.
* Deleting or pruning a build also removes its image tag, since a dev loop
  makes a new one every time.

With several artifacts or assets per build, *"name contains"* picks between them;
left blank it takes the one that looks like a Linux x86-64 zip and avoids
`aarch64`, `debug` and `windows`. A fork's CI artifact is commonly a zip inside
GitHub's own zip, or one folder deep — both are unwrapped.

**Private forks work.** `raw.githubusercontent.com` cannot serve a private
repository, so per-commit file reads fall back to the Contents API, which the
token can open. The token is sent to the configured API host and nowhere else,
so a third-party archive URL never sees it.

**Test this source** does the four things an update needs — reads the branch
head, one migration file and the README, then resolves the build archive — and
reports each separately, because they fail separately. A source that does not
pass is still saved, so a typo can be corrected rather than retyped.

Builds are versioned by commit, so installed builds are unaffected by changing
the source and the history records where each came from. Switching source can
leave the ledger holding a commit the new repository has never seen; the version
check notices that, says so, and skips the changelog rather than failing.
`Reset to upstream` restores `redstrate/Kawari` and keeps the token.

## Accounts

Identity lives in Kawari's own `user` table, so there is no second set of
credentials. The web app's `webapp.db` only hangs extra data off a user id:
admin flag, display name, bio, avatar, banner, and which characters are public.
Kawari's databases are never modified by the account system.

Kawari stores and compares passwords **in plain text**
(`selected_user.password == for_password`), so this front end cannot be more
secure than that. Do not reuse a password from anywhere else, and do not expose
this site to the internet without putting a TLS-terminating reverse proxy and
your own access control in front of it.

### Why `--userns=keep-id` matters

In rootless Podman, container uid 0 maps to your host user and every other uid
maps into your subuid range. So a container running as uid 1000 sees a directory
you own as root-owned, and cannot write to it — which shows up as
`PermissionError: [Errno 13] Permission denied: '/data/web'` on startup, repeated
forever by the restart policy.

`--userns=keep-id` maps your host uid to the same uid inside the container, so
the mounted data directory is writable and the Podman socket stays reachable.
`run.sh` and the quadlet unit both set it. If you run the container by hand, you
need it too.

### Reaching the game server from inside the container

The address the server advertises to clients (your LAN or Tailscale IP) is
usually **not** reachable from inside this container: rootless Podman gives it
its own network namespace, and the host's published ports are not on the
container's view of that address. Probing it directly makes every service look
down while clients connect perfectly well.

So the app probes `host.containers.internal` first, then the advertised address,
then loopback, and uses whichever answers — while still checking the *contents*
of `/.well-known/xiv` against the advertised address, because that is what
clients are handed. The address actually used is shown on the dashboard.

If the game server runs on a different machine from this web app, set
`KAWARI_PROBE_HOST` to where it is reachable.

### Why `KAWARI_HOST_DATA_DIR` matters

Podman runs on the host, so when this app asks it to create the Kawari container,
the bind-mount paths in that request are resolved **by the host**. Inside this
container the data directory is `/data`, which the host knows nothing about, so
the app translates paths before building a container spec. `run.sh` and the
quadlet unit set this automatically; set it yourself only if you mount things
differently.

## Starting the server

The Overview's primary button adapts to what exists: **Create and start** when
there is an image but no container, plain **Start** when the container exists,
and a pointer to Setup when neither does. Creating builds the container spec from
the current ports, mounts and advertised address, so it also picks up changes
made on the Setup page.

## Long-running operations

Installing, updating and switching builds take minutes, so they run as background
jobs on their own thread with a live progress page: each step reports its own
result and the log streams into the browser. Only one job runs at a time - they
all touch the same container, image and databases, and letting two overlap is how
a database gets corrupted.

Job state is deliberately in-memory. If the app restarts mid-update, the job is
gone rather than looking resumable when the process running it no longer exists.

## Feature parity

Everything from the desktop app is here:

| Area | Where |
|---|---|
| Install Kawari, Oodle, build the image, create the container | Dashboard → Setup |
| Networking mode, port preview, autoconfig verification, hosts snippet | Dashboard → Setup |
| Version check, changelog, schema preview, update, rollback, pinning, pruning | Dashboard → Updates |
| Tracking a fork on GitHub, Codeberg or Gitea/Forgejo; installing a specific commit (web only) | Dashboard → Updates |
| Every `config.yaml` key | Dashboard → Server config |
| Schema health, migration, accounts, Auracite import/export | Dashboard → Database |
| Backups: create, download, restore, delete (one or ticked), prune to the newest N, a kept-backups limit applied automatically | Dashboard → Database |
| Builds: switch, pin, delete, prune | Dashboard → Updates |
| Character snapshots: diff, restore, delete (one or ticked), a per-character limit applied automatically | Dashboard → a character |
| Full character editor: identity, location, classes, currency, Grand Company, unlocks, chocobo, social, snapshots, delete | Dashboard → Database → a character |
| Branding, logs | Dashboard → Branding / Logs |

The desktop app remains usable against the same data directory, but the web app
is now the complete one.

## Layout

```
kawari_core/     shared, Qt-free: dbtools, chardata, auracite, schemagen,
                 versions, upstream, questlab, progress, shop, marketboard,
                 gamedata, constants, core
kawari_web/      Flask app: config, webdb, auth, podman_api, stats, app,
                 dashboard, upstreamcfg, srcbuild, questlab, progress, shop,
                 marketboard, templates/, static/ (style.css, app.js, fonts/)
Containerfile    rootless-friendly image, runs as a non-root user
run.sh           build + run with the right mounts
kawari-web.container  systemd quadlet unit
```

`kawari_core` is currently a copy of the desktop app's logic modules. Making the
desktop app import the same package, rather than carrying its own copy, is the
obvious next step.
