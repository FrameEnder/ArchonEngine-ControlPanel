"""Quest progress: every quest in the game against the quest scripts the server has.

The quests come from ``quest-index.json`` (``kawari-questdump index``, made from
the server's own game install), so the list is always the game's own, up to
whatever patch is installed, and no game text is kept here. Format 2 of the
index says, for each quest, its expansion, whether it is main scenario (it is in
the ScenarioTree sheet) and where the journal files it (section, category and
genre).

A quest counts as implemented when the ACTIVE BUILD has its script,
``resources/scripts/events/quest/<id // 100>/<script>.lua`` - what the running
server actually has. A script whose header says it was checked against a live
recording is shown as verified.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import questlab as Q

# The expansions the tracker covers, by ExVersion row. Names come from the index
# (the game's own); these are only for an index made before format 2.
EXPANSIONS = (0, 1, 2, 3, 4, 5)
FALLBACK_EXPANSION_NAMES = {
    0: "A Realm Reborn", 1: "Heavensward", 2: "Stormblood",
    3: "Shadowbringers", 4: "Endwalker", 5: "Dawntrail",
}
MSQ = "msq"
SIDE = "side"
TYPE_LABELS = {MSQ: "Main Scenario", SIDE: "Side quests"}
STATUS_LABELS = {
    "verified": "Verified",
    "implemented": "Implemented",
    "missing": "Not implemented",
}

# How a script's header says it was checked against retail.
_VERIFIED = re.compile(r"\b(recording|recorded on retail|retail recording)\b", re.I)
_HEADER_LINES = 30


@dataclass
class QuestRow:
    id: int
    script: str
    name: str
    level: int
    expansion: int
    kind: str  # MSQ or SIDE
    section: str
    category: str
    genre: str
    place: str
    status: str  # a STATUS_LABELS key

    @property
    def implemented(self) -> bool:
        return self.status != "missing"


@dataclass
class Tally:
    total: int = 0
    implemented: int = 0
    verified: int = 0

    def add(self, row: QuestRow) -> None:
        self.total += 1
        if row.implemented:
            self.implemented += 1
        if row.status == "verified":
            self.verified += 1

    @property
    def percent(self) -> float:
        return 100.0 * self.implemented / self.total if self.total else 0.0


@dataclass
class Progress:
    rows: list[QuestRow] = field(default_factory=list)
    expansion_names: dict[int, str] = field(default_factory=dict)
    # Quests the journal never shows (no section): unused or internal rows.
    hidden: int = 0
    # Scripts in the build that match no quest in the index.
    unknown_scripts: list[str] = field(default_factory=list)
    index_format: int = 0

    @property
    def has_journal_data(self) -> bool:
        return self.index_format >= 2

    def tally(self, expansion: int | None = None, kind: str | None = None) -> Tally:
        tally = Tally()
        for row in self.rows:
            if expansion is not None and row.expansion != expansion:
                continue
            if kind is not None and row.kind != kind:
                continue
            tally.add(row)
        return tally

    def sections(self) -> list[str]:
        return sorted({row.section for row in self.rows if row.section})

    def filter(self, expansion: int | None = None, kind: str | None = None,
               status: str | None = None, section: str | None = None,
               query: str = "") -> list[QuestRow]:
        query = query.strip().lower()
        number = None
        if query.isdigit():
            number = int(query)
            number = number - 65536 if number >= 65536 else number
        out = []
        for row in self.rows:
            if expansion is not None and row.expansion != expansion:
                continue
            if kind and row.kind != kind:
                continue
            if status == "implemented" and not row.implemented:
                continue
            if status in ("verified", "missing") and row.status != status:
                continue
            if section and row.section != section:
                continue
            if query:
                if number is not None:
                    if row.id != number:
                        continue
                elif query not in row.name.lower() and query not in row.script.lower():
                    continue
            out.append(row)
        return out


def build_scripts(build_dir: Path | None) -> dict[str, Path]:
    """Every quest script in the build, by script name (e.g. SubFst038_00175)."""
    if build_dir is None:
        return {}
    root = Path(build_dir) / "resources" / "scripts" / "events" / "quest"
    if not root.is_dir():
        return {}
    return {path.stem: path for path in root.glob("*/*.lua")}


def script_status(path: Path | None) -> str:
    if path is None:
        return "missing"
    try:
        with path.open(errors="replace") as handle:
            header = "".join(next(handle, "") for _ in range(_HEADER_LINES))
    except OSError:
        return "implemented"
    return "verified" if _VERIFIED.search(header) else "implemented"


def load(root: Path, build_dir: Path | None) -> Progress:
    """Reads the quest index (in the dumps folder) and the build's scripts."""
    progress = Progress()
    index = Q.load_index(root)
    if not index:
        return progress
    progress.index_format = _index_format(root)
    scripts = build_scripts(build_dir)
    seen_scripts = set()

    for quest in index.values():
        script = quest.get("script") or ""
        name = quest.get("name") or ""
        expansion = int(quest.get("expansion") or 0)
        if not script or not name or expansion not in EXPANSIONS:
            continue
        if expansion not in progress.expansion_names:
            progress.expansion_names[expansion] = (
                quest.get("expansion_name") or FALLBACK_EXPANSION_NAMES[expansion])

        section = quest.get("section_name") or ""
        if progress.has_journal_data and not section:
            # Not in the journal anywhere: unused or internal, not something to script.
            progress.hidden += 1
            continue

        path = scripts.get(script)
        if path is not None:
            seen_scripts.add(script)
        progress.rows.append(QuestRow(
            id=int(quest["id"]),
            script=script,
            name=name,
            level=int(quest.get("level") or 0),
            expansion=expansion,
            kind=MSQ if quest.get("main_scenario") else SIDE,
            section=section,
            category=quest.get("category_name") or "",
            genre=quest.get("genre_name") or "",
            place=quest.get("place_name") or "",
            status=script_status(path),
        ))

    for expansion in EXPANSIONS:
        progress.expansion_names.setdefault(expansion, FALLBACK_EXPANSION_NAMES[expansion])
    progress.rows.sort(key=lambda row: (row.expansion, row.kind != MSQ, row.level, row.id))
    progress.unknown_scripts = sorted(set(scripts) - seen_scripts)
    return progress


def _index_format(root: Path) -> int:
    try:
        return int(json.loads((root / "quest-index.json").read_text()).get("format", 1))
    except (OSError, ValueError, TypeError):
        return 0
