"""Misc content progress: every duty and open-world zone in the game against what the server has for it.

The lists come from ``content-index.json`` (``kawari-questdump content``, made from the server's own game install):
every ContentFinderCondition duty with its content type, short code, territory and expansion, and every open-world
zone (TerritoryType with intended use 1) with its background folder. No game text is kept here.

What the ACTIVE BUILD has for each:

* a duty is **scripted** when the build has its content director script, ``resources/scripts/content/<short
  code>.lua`` - the file the server loads when the duty starts;
* a duty or zone **has spawns** when one of the build's drop-ins (``resources/dropins/*.json``) adds battle NPCs to its
  level (the drop-in's ``appends`` LGB is in the territory's level folder). Quest battle layers (``QB_*``) don't count
  for the zone they borrow.

PvP maps listed as open world (Wolves' Den Pier: its Bg is under ``/pvp/``) have no monsters at all, so they're left
out of the zone list (Bob, 2026-10-25).

A duty with both is counted as done, with one of them as started.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

INDEX_FILE = "content-index.json"

# ContentType rows shown in this order (names come from the index): Dungeons, Trials, Raids, Ultimate Raids, Deep
# Dungeons, Variant & Criterion Dungeons, Guildhests. Every other type follows, under "Other duties".
FEATURED_TYPES = (2, 4, 5, 28, 21, 30, 3)
FALLBACK_TYPE_NAMES = {2: "Dungeons", 3: "Guildhests", 4: "Trials", 5: "Raids", 21: "Deep Dungeons",
                       28: "Ultimate Raids", 30: "V&C Dungeons"}

STATUS_LABELS = {
    "done": "Scripted, with spawns",
    "scripted": "Script only",
    "spawns": "Spawns only",
    "missing": "Nothing yet",
}
ZONE_STATUS_LABELS = {"spawns": "Monster spawns", "missing": "No spawns"}


@dataclass
class Duty:
    id: int
    name: str
    short_code: str
    content_type: int
    territory: int
    level: int
    item_level: int
    expansion: int
    scripted: bool
    spawns: int

    @property
    def status(self) -> str:
        if self.scripted and self.spawns:
            return "done"
        if self.scripted:
            return "scripted"
        if self.spawns:
            return "spawns"
        return "missing"


@dataclass
class Zone:
    id: int
    name: str
    region: str
    expansion: int
    spawns: int
    dropin: str

    @property
    def status(self) -> str:
        return "spawns" if self.spawns else "missing"


@dataclass
class Tally:
    total: int = 0
    done: int = 0
    started: int = 0

    def add(self, status: str) -> None:
        self.total += 1
        if status == "done":
            self.done += 1
        elif status in ("scripted", "spawns"):
            self.started += 1

    @property
    def percent(self) -> float:
        return 100.0 * self.done / self.total if self.total else 0.0

    @property
    def started_percent(self) -> float:
        return 100.0 * self.started / self.total if self.total else 0.0


@dataclass
class ContentProgress:
    duties: list[Duty] = field(default_factory=list)
    zones: list[Zone] = field(default_factory=list)
    expansion_names: dict[int, str] = field(default_factory=dict)
    type_names: dict[int, str] = field(default_factory=dict)
    loaded: bool = False

    def type_name(self, content_type: int) -> str:
        return (self.type_names.get(content_type) or FALLBACK_TYPE_NAMES.get(content_type)
                or f"Content type {content_type}")

    def groups(self) -> list[tuple[int | None, str]]:
        """The duty groups to show: the featured content types the index has, then everything else."""
        present = {d.content_type for d in self.duties}
        groups = [(t, self.type_name(t)) for t in FEATURED_TYPES if t in present]
        if present - set(FEATURED_TYPES):
            groups.append((None, "Other duties"))
        return groups

    def in_group(self, group: int | None) -> list[Duty]:
        if group is None:
            return [d for d in self.duties if d.content_type not in FEATURED_TYPES]
        return [d for d in self.duties if d.content_type == group]

    def duty_tally(self, group: int | None, expansion: int | None = None) -> Tally:
        tally = Tally()
        for duty in self.in_group(group):
            if expansion is None or duty.expansion == expansion:
                tally.add(duty.status)
        return tally

    def zone_tally(self, expansion: int | None = None) -> Tally:
        tally = Tally()
        for zone in self.zones:
            if expansion is None or zone.expansion == expansion:
                tally.add("done" if zone.spawns else "missing")
        return tally

    def expansions(self) -> list[int]:
        return sorted({d.expansion for d in self.duties} | {z.expansion for z in self.zones})


def build_content_scripts(build_dir: Path | None) -> set[str]:
    """Short codes with a content director script in the build (``resources/scripts/content/*.lua``)."""
    if build_dir is None:
        return set()
    root = Path(build_dir) / "resources" / "scripts" / "content"
    if not root.is_dir():
        return set()
    return {path.stem.lower() for path in root.glob("*.lua")}


def build_spawns(build_dir: Path | None) -> dict[str, tuple[int, str]]:
    """Battle NPCs the build's drop-ins add, by level folder (``bg/.../level``): (how many, drop-in file name)."""
    found: dict[str, tuple[int, str]] = {}
    if build_dir is None:
        return found
    root = Path(build_dir) / "resources" / "dropins"
    if not root.is_dir():
        return found
    counts: dict[str, int] = defaultdict(int)
    names: dict[str, str] = {}
    for path in sorted(root.glob("*.json")):
        try:
            dropin = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        appends = str(dropin.get("appends") or "")
        if not appends:
            continue
        folder = appends.rsplit("/", 1)[0].lower()
        number = 0
        for layer in dropin.get("layers") or []:
            if str(layer.get("name", "")).startswith("QB_"):
                continue
            for obj in layer.get("objects") or []:
                data = obj.get("data") or {}
                # Duties spawn theirs from the director (nonpop), so those count too.
                if data.get("type") == "battle_npc":
                    number += 1
        if number:
            counts[folder] += number
            names.setdefault(folder, path.name)
    return {folder: (counts[folder], names[folder]) for folder in counts}


def level_folder(bg: str) -> str:
    """A TerritoryType Bg (``ffxiv/wil_w1/fld/w1f2/level/w1f2``) as a drop-in's folder (``bg/ffxiv/.../level``)."""
    bg = bg.strip().strip("/")
    if not bg:
        return ""
    return ("bg/" + bg.rsplit("/", 1)[0]).lower()


def load(root: Path, build_dir: Path | None) -> ContentProgress:
    path = Path(root) / INDEX_FILE
    progress = ContentProgress()
    try:
        index = json.loads(path.read_text())
    except (OSError, ValueError):
        return progress
    progress.loaded = True
    progress.expansion_names = {int(k): v for k, v in (index.get("expansions") or {}).items() if v}
    progress.type_names = {int(k): v for k, v in (index.get("content_types") or {}).items() if v}
    scripts = build_content_scripts(build_dir)
    spawns = build_spawns(build_dir)

    for entry in index.get("duties") or []:
        short = str(entry.get("short_code") or "")
        progress.duties.append(Duty(
            id=int(entry.get("id", 0)),
            name=str(entry.get("name", "")),
            short_code=short,
            content_type=int(entry.get("content_type", 0)),
            territory=int(entry.get("territory", 0)),
            level=int(entry.get("level", 0)),
            item_level=int(entry.get("item_level", 0)),
            expansion=int(entry.get("ex_version", 0)),
            scripted=bool(short) and short.lower() in scripts,
            spawns=spawns.get(level_folder(str(entry.get("territory_bg") or "")), (0, ""))[0],
        ))
    progress.duties.sort(key=lambda d: (d.expansion, d.level, d.item_level, d.id))

    seen = set()
    for entry in index.get("zones") or []:
        folder = level_folder(str(entry.get("bg") or ""))
        # Several territories can share a map (event copies); count each map once. PvP maps have no monsters.
        if folder in seen or "/pvp/" in folder:
            continue
        seen.add(folder)
        count, dropin = spawns.get(folder, (0, ""))
        progress.zones.append(Zone(
            id=int(entry.get("id", 0)),
            name=str(entry.get("name", "")),
            region=str(entry.get("region", "")),
            expansion=int(entry.get("ex_version", 0)),
            spawns=count,
            dropin=dropin,
        ))
    progress.zones.sort(key=lambda z: (z.expansion, z.region, z.name))
    return progress
