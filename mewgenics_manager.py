#!/usr/bin/env python3
"""
Mewgenics Breeding Manager
External viewer for cat stats, room locations, and breeding pairs.
Parsing logic based on pzx521521/mewgenics-save-editor.

Requirements: pip install PySide6 lz4
"""

import sys
import struct
import sqlite3
import lz4.block
import os
from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTableView, QPushButton, QLabel, QFileDialog, QHeaderView,
    QAbstractItemView, QSplitter, QFrame,
)
from PySide6.QtCore import (
    Qt, QAbstractTableModel, QModelIndex, QSortFilterProxyModel,
    QFileSystemWatcher,
)
from PySide6.QtGui import QColor, QBrush, QAction, QPalette

# ── Helpers ───────────────────────────────────────────────────────────────────

_JUNK_STRINGS = frozenset({"none", "null", "", "defaultmove", "default_move"})

def _valid_str(s) -> bool:
    """Reject None, empty, and game filler strings like 'none' or 'defaultmove'."""
    return bool(s) and s.strip().lower() not in _JUNK_STRINGS

# ── Constants ─────────────────────────────────────────────────────────────────

STAT_NAMES = ["STR", "DEX", "CON", "INT", "SPD", "CHA", "LCK"]

APPDATA_SAVE_DIR = os.path.join(
    os.environ.get("APPDATA", ""),
    "Glaiel Games", "Mewgenics",
)

STAT_COLORS = {
    1: QColor(170, 40,  40),
    2: QColor(195, 85,  40),
    3: QColor(190, 145, 40),
    4: QColor(100, 100, 115),
    5: QColor(80,  160, 70),
    6: QColor(50,  195, 80),
    7: QColor(30,  215, 100),
}

ROOM_DISPLAY = {
    "Floor1_Large":   "Ground Floor",
    "Floor1_Small":   "Ground Floor (S)",
    "Floor2_Large":   "Second Floor",
    "Floor2_Small":   "Second Floor (S)",
    "Attic":          "Attic",
    "Attic_Large":    "Attic",
    "Basement":       "Basement",
    "Basement_Large": "Basement",
}

# Full status → abbreviated display in table cell
STATUS_ABBREV = {
    "In House":  "House",
    "Adventure": "Away",
    "Gone":      "Gone",
}
STATUS_COLOR = {
    "In House":  QColor(50,  170, 110),
    "Adventure": QColor(70,  120, 200),
    "Gone":      QColor(80,   80,  90),
}


# ── Binary reader ─────────────────────────────────────────────────────────────

class BinaryReader:
    def __init__(self, data, pos=0):
        self.data = data
        self.pos  = pos

    def u32(self):
        v = struct.unpack_from('<I', self.data, self.pos)[0]
        self.pos += 4
        return v

    def i32(self):
        v = struct.unpack_from('<i', self.data, self.pos)[0]
        self.pos += 4
        return v

    def u64(self):
        lo, hi = struct.unpack_from('<II', self.data, self.pos)
        self.pos += 8
        return lo + hi * 4_294_967_296

    def f64(self):
        v = struct.unpack_from('<d', self.data, self.pos)[0]
        self.pos += 8
        return v

    def str(self):
        start = self.pos
        try:
            length = self.u64()
            if length < 0 or length > 10_000:
                self.pos = start
                return None
            s = self.data[self.pos:self.pos + int(length)].decode('utf-8', errors='ignore')
            self.pos += int(length)
            return s
        except Exception:
            self.pos = start
            return None

    def utf16str(self):
        char_count = self.u64()
        byte_len   = int(char_count * 2)
        s = self.data[self.pos:self.pos + byte_len].decode('utf-16le', errors='ignore')
        self.pos += byte_len
        return s

    def skip(self, n):
        self.pos += n

    def seek(self, n):
        self.pos = n

    def remaining(self):
        return len(self.data) - self.pos


# ── Cat ───────────────────────────────────────────────────────────────────────

class Cat:
    # parent_a / parent_b are resolved after the full save is loaded
    parent_a: Optional['Cat'] = None
    parent_b: Optional['Cat'] = None

    def __init__(self, blob: bytes, cat_key: int, house_info: dict, adventure_keys: set):
        uncomp_size = struct.unpack('<I', blob[:4])[0]
        raw = lz4.block.decompress(blob[4:], uncompressed_size=uncomp_size)
        r   = BinaryReader(raw)

        self.db_key = cat_key

        # Location / status
        if cat_key in adventure_keys:
            self.status = "Adventure"
            self.room   = "Adventure"
        elif cat_key in house_info:
            self.status = "In House"
            self.room   = house_info[cat_key]
        else:
            self.status = "Gone"
            self.room   = ""

        # Blob fields
        self.breed_id = r.u32()
        self._uid_int = r.u64()            # store as int for ancestry lookup
        self.unique_id = hex(self._uid_int)
        self.name = r.utf16str()

        r.str()  # unknown string

        # The 16 bytes here are likely two parent uniqueIds (2 × u64).
        # If they match another cat's uniqueId, parent links are resolved later.
        self._parent_uid_a = r.u64()
        self._parent_uid_b = r.u64()

        self.collar = r.str() or ""
        r.u32()

        r.skip(64)
        T = [r.u32() for _ in range(72)]
        self.body_parts = {"texture": T[0], "bodyShape": T[3], "headShape": T[8]}
        self.visual_mutation_ids = [T[i] for i in range(14, 29) if i < 72 and T[i] != 0]

        r.skip(12)
        self.gender = r.str() or "?"
        r.f64()

        self.stat_base = [r.u32() for _ in range(7)]
        self.stat_mod  = [r.i32() for _ in range(7)]
        self.stat_sec  = [r.i32() for _ in range(7)]

        self.base_stats  = {n: self.stat_base[i] for i, n in enumerate(STAT_NAMES)}
        self.total_stats = {n: self.stat_base[i] + self.stat_mod[i] + self.stat_sec[i]
                            for i, n in enumerate(STAT_NAMES)}

        # Abilities (heuristic scan)
        curr  = r.pos
        found = -1
        for i in range(curr, min(curr + 500, len(raw) - 9)):
            length = struct.unpack_from('<I', raw, i)[0]
            if (0 < length < 64
                    and struct.unpack_from('<I', raw, i + 4)[0] == 0
                    and 65 <= raw[i + 8] <= 90):
                found = i
                break
        if found != -1:
            r.seek(found)

        self.abilities = [a for a in [r.str() for _ in range(6)] if _valid_str(a)]
        self.equipment = [s for s in [r.str() for _ in range(4)] if _valid_str(s)]

        # Mutations (up to 14 slots)
        self.mutations = []
        first = r.str()
        if _valid_str(first):
            self.mutations.append(first)
        for _ in range(13):
            if r.remaining() < 12:
                break
            flag = r.u32()
            if flag == 0:
                break
            p = r.str()
            if _valid_str(p):
                self.mutations.append(p)

    # ── Display helpers ────────────────────────────────────────────────────

    @property
    def room_display(self) -> str:
        if not self.room or self.room == "Adventure":
            return self.room or ""
        return ROOM_DISPLAY.get(self.room, self.room)

    @property
    def gender_display(self) -> str:
        g = (self.gender or "").strip().lower()
        if g.startswith("male"):   return "M"
        if g.startswith("female"): return "F"
        if g in ("ditto", "?"):    return "?"
        return g[:1].upper() if g else "?"

    @property
    def can_move(self) -> bool:
        return self.status == "In House"

    @property
    def short_name(self) -> str:
        """First word of name for compact displays."""
        return self.name.split()[0] if self.name else "?"


# ── Ancestry helpers ──────────────────────────────────────────────────────────

def get_all_ancestors(cat: Optional[Cat], depth: int = 6, _seen: set = None) -> set:
    """Return all ancestor Cat objects up to `depth` generations."""
    if cat is None or depth == 0:
        return set()
    if _seen is None:
        _seen = set()
    ancestors: set[Cat] = set()
    for parent in (cat.parent_a, cat.parent_b):
        if parent is not None and id(parent) not in _seen:
            _seen.add(id(parent))
            ancestors.add(parent)
            ancestors |= get_all_ancestors(parent, depth - 1, _seen)
    return ancestors


def find_common_ancestors(a: Cat, b: Cat) -> list[Cat]:
    """Return cats that appear in both ancestry trees."""
    return list(get_all_ancestors(a) & get_all_ancestors(b))


def get_parents(cat: Cat) -> list[Cat]:
    return [p for p in (cat.parent_a, cat.parent_b) if p is not None]


def get_grandparents(cat: Cat) -> list[Cat]:
    gp = []
    for p in get_parents(cat):
        gp.extend(get_parents(p))
    return gp


def can_breed(a: Cat, b: Cat) -> tuple[bool, str]:
    """Return (ok, reason). reason is non-empty only when ok is False."""
    if a is b:
        return False, "Cannot pair a cat with itself"
    ga, gb = a.gender_display, b.gender_display
    # Ditto ("?") can pair with anything
    if ga == "?" or gb == "?":
        return True, ""
    if ga == "M" and gb == "F":
        return True, ""
    if ga == "F" and gb == "M":
        return True, ""
    # Same sex
    label = "female" if ga == "F" else "male"
    return False, f"Both cats are {label} — cannot produce offspring"


# ── Save-file helpers ─────────────────────────────────────────────────────────

def _get_house_info(conn) -> dict:
    row = conn.execute("SELECT data FROM files WHERE key = 'house_state'").fetchone()
    if not row or len(row[0]) < 8:
        return {}
    data  = row[0]
    count = struct.unpack_from('<I', data, 4)[0]
    pos   = 8
    result = {}
    for _ in range(count):
        if pos + 8 > len(data):
            break
        cat_key  = struct.unpack_from('<I', data, pos)[0]
        pos += 8
        room_len = struct.unpack_from('<I', data, pos)[0]
        pos += 8
        room_name = ""
        if room_len > 0:
            room_name = data[pos:pos + room_len].decode('ascii', errors='ignore')
            pos += room_len
        pos += 24
        result[cat_key] = room_name
    return result


def _get_adventure_keys(conn) -> set:
    keys = set()
    try:
        row = conn.execute("SELECT data FROM files WHERE key = 'adventure_state'").fetchone()
        if not row or len(row[0]) < 8:
            return keys
        data  = row[0]
        count = struct.unpack_from('<I', data, 4)[0]
        pos   = 8
        for _ in range(count):
            if pos + 8 > len(data):
                break
            val = struct.unpack_from('<Q', data, pos)[0]
            pos += 8
            cat_key = (val >> 32) & 0xFFFF_FFFF
            if cat_key:
                keys.add(cat_key)
    except Exception:
        pass
    return keys


def parse_save(path: str) -> tuple[list, list]:
    conn  = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    house = _get_house_info(conn)
    adv   = _get_adventure_keys(conn)
    rows  = conn.execute("SELECT key, data FROM cats").fetchall()
    conn.close()

    cats, errors = [], []
    for key, blob in rows:
        try:
            cats.append(Cat(blob, key, house, adv))
        except Exception as e:
            errors.append((key, str(e)))

    # Resolve parent references by uniqueId
    uid_map = {c._uid_int: c for c in cats}
    for cat in cats:
        cat.parent_a = uid_map.get(cat._parent_uid_a) if cat._parent_uid_a else None
        cat.parent_b = uid_map.get(cat._parent_uid_b) if cat._parent_uid_b else None

    return cats, errors


def find_save_files() -> list[str]:
    saves = []
    base  = Path(APPDATA_SAVE_DIR)
    if not base.is_dir():
        return saves
    for profile in base.iterdir():
        saves_dir = profile / "saves"
        if saves_dir.is_dir():
            saves.extend(str(p) for p in saves_dir.glob("*.sav"))
    saves.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return saves


# ── Qt table model ────────────────────────────────────────────────────────────

COLUMNS  = ["Name", "♀/♂", "Room", "Status"] + STAT_NAMES + ["Mutations", "Abilities"]
COL_NAME = 0
COL_GEN  = 1
COL_ROOM = 2
COL_STAT = 3
STAT_COLS = list(range(4, 11))   # STR … LCK  (indices 4–10)
COL_MUTS = 11
COL_ABIL = 12

# Fixed pixel widths for narrow columns
_W_STATUS = 62
_W_STAT   = 34
_W_GEN    = 28


class CatTableModel(QAbstractTableModel):
    def __init__(self):
        super().__init__()
        self._cats: list[Cat] = []

    def load(self, cats: list[Cat]):
        self.beginResetModel()
        self._cats = cats
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):    return len(self._cats)
    def columnCount(self, parent=QModelIndex()): return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return COLUMNS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        cat = self._cats[index.row()]
        col = index.column()

        if role == Qt.DisplayRole:
            if col == COL_NAME: return cat.name
            if col == COL_GEN:  return cat.gender_display
            if col == COL_ROOM: return cat.room_display
            if col == COL_STAT: return STATUS_ABBREV.get(cat.status, cat.status)
            if col in STAT_COLS:
                return str(cat.base_stats[STAT_NAMES[col - 4]])
            if col == COL_MUTS:
                return ", ".join(cat.mutations)
            if col == COL_ABIL:
                return ", ".join(cat.abilities)

        elif role == Qt.UserRole:
            if col in STAT_COLS:
                return cat.base_stats[STAT_NAMES[col - 4]]
            return self.data(index, Qt.DisplayRole)

        elif role == Qt.BackgroundRole:
            if col in STAT_COLS:
                val = cat.base_stats[STAT_NAMES[col - 4]]
                return QBrush(STAT_COLORS.get(val, QColor(100, 100, 115)))
            if col == COL_STAT:
                return QBrush(STATUS_COLOR.get(cat.status, QColor(80, 80, 90)))

        elif role == Qt.ForegroundRole:
            if col in STAT_COLS or col == COL_STAT:
                return QBrush(QColor(255, 255, 255))

        elif role == Qt.ToolTipRole:
            if col in STAT_COLS:
                n = STAT_NAMES[col - 4]
                b = cat.base_stats[n]
                t = cat.total_stats[n]
                extra = f"  (+{t - b})" if t != b else ""
                return f"{n}  base: {b}{extra}  |  total: {t}"
            if col == COL_ROOM:
                return cat.room
            if col == COL_MUTS and cat.mutations:
                return "\n".join(cat.mutations)
            if col == COL_ABIL and cat.abilities:
                return "\n".join(cat.abilities)

        elif role == Qt.TextAlignmentRole:
            if col in STAT_COLS or col in (COL_GEN, COL_STAT):
                return Qt.AlignCenter

        return None

    def cat_at(self, row: int) -> Optional[Cat]:
        return self._cats[row] if 0 <= row < len(self._cats) else None


class RoomFilterModel(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self._room = None
        self.setSortRole(Qt.UserRole)

    def set_room(self, key):
        self._room = key
        self.invalidate()   # invalidateFilter() is deprecated in Qt6

    def filterAcceptsRow(self, source_row, source_parent):
        cat = self.sourceModel().cat_at(source_row)
        if cat is None:
            return False
        if self._room is None:
            return cat.status != "Gone"
        if self._room == "__gone__":
            return cat.status == "Gone"
        if self._room == "__adventure__":
            return cat.status == "Adventure"
        return cat.room == self._room


# ── Detail / breeding panel widgets ──────────────────────────────────────────

_CHIP_STYLE = ("QLabel { background:#252545; color:#ccc; border-radius:6px;"
               " padding:2px 7px; font-size:11px; }")
_SEC_STYLE  = "color:#555; font-size:10px; font-weight:bold; letter-spacing:1px;"
_NAME_STYLE = "color:#eee; font-size:13px; font-weight:bold;"
_META_STYLE = "color:#777; font-size:11px;"
_WARN_STYLE = "color:#e07050; font-size:11px; font-weight:bold;"
_SAFE_STYLE = "color:#50c080; font-size:11px;"
_ANCS_STYLE = "color:#aaa; font-size:11px;"
_PANEL_BG   = "background:#0a0a18; border-top:1px solid #1e1e38;"


def _chip(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(_CHIP_STYLE)
    return lbl

def _sec(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(_SEC_STYLE)
    return lbl

def _vsep() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.VLine)
    f.setStyleSheet("color:#1e1e38;")
    return f


class ChipRow(QWidget):
    def __init__(self, items: list[str]):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(5)
        for item in items:
            row.addWidget(_chip(item))
        row.addStretch()


class CatDetailPanel(QWidget):
    """
    Bottom panel driven by table selection.
    1 cat  → abilities / mutations / ancestry
    2 cats → breeding comparison with lineage safety check
    """

    def __init__(self):
        super().__init__()
        self.setStyleSheet(_PANEL_BG)
        self.setFixedHeight(0)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 10, 14, 10)
        outer.setSpacing(0)
        self._content = QWidget()
        outer.addWidget(self._content)

    def show_cats(self, cats: list[Cat]):
        old = self._content
        self._content = QWidget()
        self.layout().replaceWidget(old, self._content)
        old.deleteLater()

        if not cats:
            self.setFixedHeight(0)
            return

        min_h = 160 if len(cats) == 1 else 220
        self.setMinimumHeight(min_h)
        self.setMaximumHeight(16777215)   # remove the fixed-height lock

        if len(cats) == 1:
            self._build_single(cats[0])
        else:
            self._build_pair(cats[0], cats[1])

    # ── Single cat ─────────────────────────────────────────────────────────

    def _build_single(self, cat: Cat):
        root = QHBoxLayout(self._content)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(20)

        # Identity
        id_col = QVBoxLayout()
        id_col.setSpacing(3)
        name_row = QHBoxLayout()
        nl = QLabel(cat.name); nl.setStyleSheet(_NAME_STYLE)
        gl = QLabel(cat.gender_display)
        gl.setStyleSheet("color:#7ac; font-size:12px; font-weight:bold;")
        name_row.addWidget(nl); name_row.addWidget(gl); name_row.addStretch()
        id_col.addLayout(name_row)
        id_col.addWidget(QLabel(cat.room_display or "—", styleSheet=_META_STYLE))

        # Stat bonuses (only if total differs from base)
        diffs = [(n, cat.base_stats[n], cat.total_stats[n])
                 for n in STAT_NAMES if cat.total_stats[n] != cat.base_stats[n]]
        if diffs:
            id_col.addSpacing(4)
            dl = QLabel("  ".join(f"{n} {b}→{t}" for n, b, t in diffs))
            dl.setStyleSheet("color:#5a9; font-size:11px;")
            id_col.addWidget(dl)

        id_col.addStretch()
        root.addLayout(id_col)

        # Abilities
        if cat.abilities:
            root.addWidget(_vsep())
            ab = QVBoxLayout(); ab.setSpacing(4)
            ab.addWidget(_sec("ABILITIES"))
            ab.addWidget(ChipRow(cat.abilities))
            ab.addStretch()
            root.addLayout(ab)

        # Mutations
        if cat.mutations:
            root.addWidget(_vsep())
            mu = QVBoxLayout(); mu.setSpacing(4)
            mu.addWidget(_sec("MUTATIONS"))
            mu.addWidget(ChipRow(cat.mutations))
            mu.addStretch()
            root.addLayout(mu)

        # Equipment
        if cat.equipment:
            root.addWidget(_vsep())
            eq = QVBoxLayout(); eq.setSpacing(4)
            eq.addWidget(_sec("EQUIPMENT"))
            eq.addWidget(ChipRow(cat.equipment))
            eq.addStretch()
            root.addLayout(eq)

        # Ancestry
        parents = get_parents(cat)
        gparents = get_grandparents(cat)
        if parents:
            root.addWidget(_vsep())
            anc = QVBoxLayout(); anc.setSpacing(4)
            anc.addWidget(_sec("LINEAGE"))

            p_names = " × ".join(
                f"{p.name} ({p.gender_display})" for p in parents)
            pl = QLabel(p_names); pl.setStyleSheet(_ANCS_STYLE)
            anc.addWidget(pl)

            if gparents:
                gp_names = "  ·  ".join(gp.short_name for gp in gparents)
                gl2 = QLabel(gp_names)
                gl2.setStyleSheet("color:#555; font-size:10px;")
                anc.addWidget(gl2)

            anc.addStretch()
            root.addLayout(anc)

        root.addStretch()

    # ── Breeding pair ──────────────────────────────────────────────────────

    def _build_pair(self, a: Cat, b: Cat):
        from PySide6.QtWidgets import QGridLayout, QSizePolicy
        ok, reason = can_breed(a, b)

        root = QVBoxLayout(self._content)
        root.setContentsMargins(0, 4, 0, 0)
        root.setSpacing(10)

        # ── Header: parent names + room ────────────────────────────────────
        hdr = QHBoxLayout()
        hdr.setSpacing(6)

        for cat in (a, b):
            nl = QLabel(cat.name)
            nl.setStyleSheet(_NAME_STYLE)
            nl.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
            hdr.addWidget(nl)
            gl = QLabel(cat.gender_display)
            gl.setStyleSheet("color:#7ac; font-size:12px; font-weight:bold;")
            hdr.addWidget(gl)
            rl = QLabel(f"  {cat.room_display}" if cat.room_display else "")
            rl.setStyleSheet(_META_STYLE)
            hdr.addWidget(rl)
            if cat is not b:
                x = QLabel("×")
                x.setStyleSheet("color:#444; font-size:14px; padding:0 10px;")
                hdr.addWidget(x)

        hdr.addStretch()
        if not ok:
            hdr.addWidget(QLabel(f"⚠  {reason}", styleSheet=_WARN_STYLE))

        root.addLayout(hdr)

        if not ok:
            root.addStretch()
            return

        # ── Stats grid + abilities ─────────────────────────────────────────
        mid = QHBoxLayout()
        mid.setSpacing(20)

        # Grid rows: Cat A, Cat B, then Offspring last
        grid_rows = [
            (a, True),    # (cat, is_cat)
            (b, True),
            (None, False),  # offspring range
        ]

        grid_w = QWidget()
        grid   = QGridLayout(grid_w)
        grid.setHorizontalSpacing(5)
        grid.setVerticalSpacing(5)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setColumnMinimumWidth(0, 110)   # ensure label column has room for full names

        # Stat column headers
        for j, stat in enumerate(STAT_NAMES):
            h = QLabel(stat)
            h.setStyleSheet("color:#555; font-size:9px; font-weight:bold;")
            h.setAlignment(Qt.AlignCenter)
            grid.addWidget(h, 0, j + 1)

        for i, (cat, is_cat) in enumerate(grid_rows):
            row_num = i + 1

            # Label cell: name + gender chip for cat rows, plain text for offspring
            lbl_w  = QWidget()
            lbl_hb = QHBoxLayout(lbl_w)
            lbl_hb.setContentsMargins(0, 0, 6, 0)
            lbl_hb.setSpacing(5)

            if is_cat:
                name_lbl = QLabel(cat.name)
                name_lbl.setStyleSheet("color:#ddd; font-size:11px; font-weight:bold;")
                gen_lbl  = QLabel(cat.gender_display)
                gen_lbl.setFixedWidth(20)
                gen_lbl.setAlignment(Qt.AlignCenter)
                gen_lbl.setStyleSheet(
                    "color:#fff; background:#253555; border-radius:4px;"
                    " font-size:10px; font-weight:bold;")
                lbl_hb.addWidget(name_lbl)
                lbl_hb.addWidget(gen_lbl)
            else:
                off_lbl = QLabel("Offspring")
                off_lbl.setStyleSheet("color:#555; font-size:10px; font-style:italic;")
                lbl_hb.addWidget(off_lbl)

            lbl_hb.addStretch()
            grid.addWidget(lbl_w, row_num, 0)

            # Stat cells
            for j, stat in enumerate(STAT_NAMES):
                if is_cat:
                    val  = cat.base_stats[stat]
                    c    = STAT_COLORS.get(val, QColor(100, 100, 115))
                    cell = QLabel(str(val))
                    cell.setAlignment(Qt.AlignCenter)
                    cell.setStyleSheet(
                        f"background:rgb({c.red()},{c.green()},{c.blue()});"
                        f"color:#fff; font-size:11px; font-weight:bold;"
                        f"border-radius:2px; padding:2px 6px;")
                else:
                    va, vb = a.base_stats[stat], b.base_stats[stat]
                    lo, hi = min(va, vb), max(va, vb)
                    c      = STAT_COLORS.get(hi, QColor(100, 100, 115))
                    text   = f"{lo}–{hi}" if lo != hi else str(lo)
                    cell   = QLabel(text)
                    cell.setAlignment(Qt.AlignCenter)
                    cell.setStyleSheet(
                        f"color:rgb({c.red()},{c.green()},{c.blue()});"
                        f"font-size:11px; font-weight:bold;")
                grid.addWidget(cell, row_num, j + 1)

        mid.addWidget(grid_w)
        mid.addWidget(_vsep())

        # Abilities column
        ab_col = QVBoxLayout()
        ab_col.setSpacing(6)
        ab_col.addWidget(_sec("ABILITIES"))
        for cat in (a, b):
            if cat.abilities:
                row = QHBoxLayout()
                row.setSpacing(5)
                row.addWidget(QLabel(f"{cat.name}:", styleSheet="color:#555; font-size:10px;"))
                for ab in cat.abilities:
                    row.addWidget(_chip(ab))
                row.addStretch()
                ab_col.addLayout(row)
        ab_col.addStretch()
        mid.addLayout(ab_col)

        root.addLayout(mid)

        # ── Possible mutations + lineage ───────────────────────────────────
        bot = QHBoxLayout()
        bot.setSpacing(20)

        if a.mutations or b.mutations:
            mc = QVBoxLayout()
            mc.setSpacing(4)
            mc.addWidget(_sec("MUTATIONS"))
            for cat in (a, b):
                if cat.mutations:
                    mrow = QHBoxLayout()
                    mrow.setSpacing(5)
                    mrow.addWidget(QLabel(f"{cat.name}:", styleSheet="color:#555; font-size:10px;"))
                    for mut in cat.mutations:
                        mrow.addWidget(_chip(mut))
                    mrow.addStretch()
                    mc.addLayout(mrow)
            mc.addStretch()
            bot.addLayout(mc)
            bot.addWidget(_vsep())

        lc = QVBoxLayout()
        lc.setSpacing(3)
        lc.addWidget(_sec("LINEAGE"))
        common    = find_common_ancestors(a, b)
        is_direct = (a in get_parents(b) or b in get_parents(a))

        if is_direct:
            lc.addWidget(QLabel("⚠  Direct parent/offspring", styleSheet=_WARN_STYLE))
        elif common:
            lc.addWidget(QLabel(
                f"⚠  {len(common)} shared ancestor{'s' if len(common) > 1 else ''}: "
                + "  ·  ".join(c.short_name for c in common[:6]),
                styleSheet=_WARN_STYLE))
        elif get_parents(a) or get_parents(b):
            lc.addWidget(QLabel("✓  No shared ancestors", styleSheet=_SAFE_STYLE))
        else:
            lc.addWidget(QLabel("—  Lineage unknown", styleSheet=_META_STYLE))

        lc.addStretch()
        bot.addLayout(lc)
        bot.addStretch()

        root.addLayout(bot)


# ── Sidebar helpers ───────────────────────────────────────────────────────────

_SIDEBAR_BTN = """
QPushButton {
    color:#ccc; background:transparent; border:none;
    text-align:left; padding:6px 10px; border-radius:4px; font-size:12px;
}
QPushButton:hover   { background:#252545; }
QPushButton:checked { background:#353568; color:#fff; font-weight:bold; }
"""

def _sidebar_btn(label: str) -> QPushButton:
    btn = QPushButton(label)
    btn.setCheckable(True)
    btn.setStyleSheet(_SIDEBAR_BTN)
    return btn


# ── Main window ───────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Mewgenics Breeding Manager")
        self.resize(1440, 900)

        self._current_save = None
        self._cats: list[Cat] = []
        self._room_btns: dict = {}
        self._active_btn = None

        self._build_ui()
        self._build_menu()

        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._on_file_changed)

        saves = find_save_files()
        if saves:
            self.load_save(saves[0])

    # ── Menu ──────────────────────────────────────────────────────────────

    def _build_menu(self):
        fm = self.menuBar().addMenu("File")

        oa = QAction("Open Save File…", self)
        oa.setShortcut("Ctrl+O")
        oa.triggered.connect(self._open_file)
        fm.addAction(oa)

        ra = QAction("Reload", self)
        ra.setShortcut("F5")
        ra.triggered.connect(self._reload)
        fm.addAction(ra)

        fm.addSeparator()
        for path in find_save_files():
            a = QAction(os.path.basename(path), self)
            a.triggered.connect(lambda _, p=path: self.load_save(p))
            fm.addAction(a)

    # ── Layout ────────────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        rl = QHBoxLayout(central)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)

        hs = QSplitter(Qt.Horizontal)
        rl.addWidget(hs)
        hs.addWidget(self._build_sidebar())
        hs.addWidget(self._build_content())
        hs.setStretchFactor(0, 0)
        hs.setStretchFactor(1, 1)
        hs.setSizes([190, 1250])

    # ── Sidebar ────────────────────────────────────────────────────────────

    def _build_sidebar(self) -> QWidget:
        w  = QWidget()
        w.setFixedWidth(190)
        w.setStyleSheet("background:#14142a;")
        vb = QVBoxLayout(w)
        vb.setContentsMargins(8, 14, 8, 12)
        vb.setSpacing(2)

        def sl(text):
            l = QLabel(text)
            l.setStyleSheet("color:#444; font-size:10px; font-weight:bold;"
                            " letter-spacing:1px; padding:8px 4px 4px 4px;")
            return l

        vb.addWidget(sl("VIEW"))
        self._btn_all = _sidebar_btn("All Cats")
        self._btn_all.setChecked(True)
        self._active_btn = self._btn_all
        self._btn_all.clicked.connect(lambda: self._filter(None, self._btn_all))
        vb.addWidget(self._btn_all)
        self._room_btns[None] = self._btn_all

        vb.addWidget(_hsep())
        vb.addWidget(sl("ROOMS"))
        self._rooms_vb = QVBoxLayout(); self._rooms_vb.setSpacing(2)
        vb.addLayout(self._rooms_vb)
        vb.addWidget(_hsep())

        vb.addWidget(sl("OTHER"))
        self._btn_adventure = _sidebar_btn("On Adventure")
        self._btn_gone      = _sidebar_btn("Gone")
        self._btn_adventure.clicked.connect(
            lambda: self._filter("__adventure__", self._btn_adventure))
        self._btn_gone.clicked.connect(
            lambda: self._filter("__gone__", self._btn_gone))
        vb.addWidget(self._btn_adventure)
        vb.addWidget(self._btn_gone)
        self._room_btns["__adventure__"] = self._btn_adventure
        self._room_btns["__gone__"]      = self._btn_gone

        vb.addStretch()

        self._save_lbl = QLabel("No save loaded")
        self._save_lbl.setStyleSheet("color:#444; font-size:10px;")
        self._save_lbl.setWordWrap(True)
        vb.addWidget(self._save_lbl)

        rb = QPushButton("⟳  Reload  (F5)")
        rb.setStyleSheet("QPushButton { color:#888; background:#1a1a32;"
                         " border:1px solid #2a2a4a; padding:7px;"
                         " border-radius:4px; font-size:11px; }"
                         "QPushButton:hover { background:#222244; }")
        rb.clicked.connect(self._reload)
        vb.addWidget(rb)
        return w

    def _rebuild_room_buttons(self, cats: list[Cat]):
        while self._rooms_vb.count():
            item = self._rooms_vb.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        rooms = sorted({c.room for c in cats if c.status == "In House" and c.room})
        for room in rooms:
            btn = _sidebar_btn(ROOM_DISPLAY.get(room, room))
            btn.clicked.connect(lambda _, r=room, b=btn: self._filter(r, b))
            self._rooms_vb.addWidget(btn)
            self._room_btns[room] = btn

    # ── Content ────────────────────────────────────────────────────────────

    def _build_content(self) -> QWidget:
        w  = QWidget()
        vb = QVBoxLayout(w)
        vb.setContentsMargins(0, 0, 0, 0)
        vb.setSpacing(0)

        # Header
        hdr = QWidget()
        hdr.setStyleSheet("background:#16213e; border-bottom:1px solid #1e1e38;")
        hdr.setFixedHeight(46)
        hb = QHBoxLayout(hdr); hb.setContentsMargins(14, 0, 14, 0)
        self._header_lbl = QLabel("All Cats")
        self._header_lbl.setStyleSheet("color:#eee; font-size:15px; font-weight:bold;")
        self._count_lbl = QLabel("")
        self._count_lbl.setStyleSheet("color:#555; font-size:12px; padding-left:8px;")
        self._summary_lbl = QLabel("")
        self._summary_lbl.setStyleSheet("color:#4a7a9a; font-size:11px;")
        hb.addWidget(self._header_lbl)
        hb.addWidget(self._count_lbl)
        hb.addStretch()
        hb.addWidget(self._summary_lbl)
        vb.addWidget(hdr)

        # Vertical splitter: table on top, detail panel on bottom (user-resizable)
        vs = QSplitter(Qt.Vertical)
        vs.setHandleWidth(4)
        vs.setStyleSheet("QSplitter::handle:vertical { background:#1e1e38; }")
        self._detail_splitter = vs
        vb.addWidget(vs)

        # Table
        self._source_model = CatTableModel()
        self._proxy_model  = RoomFilterModel()
        self._proxy_model.setSourceModel(self._source_model)
        self._proxy_model.modelReset.connect(self._update_count)
        self._proxy_model.rowsInserted.connect(self._update_count)
        self._proxy_model.rowsRemoved.connect(self._update_count)

        self._table = QTableView()
        self._table.setModel(self._proxy_model)
        self._table.setSortingEnabled(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setShowGrid(False)
        self._table.setWordWrap(False)

        hh = self._table.horizontalHeader()
        # Default: resize to contents
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        # Mutations: interactive (user-resizable), reasonable default width
        # Abilities: stretch to fill remaining space
        hh.setSectionResizeMode(COL_MUTS, QHeaderView.Interactive)
        self._table.setColumnWidth(COL_MUTS, 155)
        hh.setSectionResizeMode(COL_ABIL, QHeaderView.Stretch)
        # Narrow fixed columns
        for col, width in [(COL_GEN, _W_GEN), (COL_STAT, _W_STATUS)] + \
                          [(c, _W_STAT) for c in STAT_COLS]:
            hh.setSectionResizeMode(col, QHeaderView.Fixed)
            self._table.setColumnWidth(col, width)

        self._table.setStyleSheet("""
            QTableView {
                background:#0d0d1c; alternate-background-color:#131326;
                color:#ddd; border:none; font-size:12px;
                selection-background-color:#1e3060;
            }
            QTableView::item { padding:3px 4px; }
            QTableView::item:selected { color:#fff; }
            QHeaderView::section {
                background:#16213e; color:#888; padding:5px 4px;
                border:none; border-bottom:1px solid #1e1e38;
                border-right:1px solid #16213e;
                font-size:11px; font-weight:bold;
            }
            QScrollBar:vertical { background:#0d0d1c; width:10px; }
            QScrollBar::handle:vertical {
                background:#252545; border-radius:5px; min-height:20px;
            }
        """)

        self._table.selectionModel().selectionChanged.connect(self._on_selection)
        vs.addWidget(self._table)

        # Detail panel
        self._detail = CatDetailPanel()
        vs.addWidget(self._detail)
        vs.setStretchFactor(0, 1)
        vs.setStretchFactor(1, 0)

        return w

    # ── Selection → detail ────────────────────────────────────────────────

    def _on_selection(self):
        rows = list({
            self._proxy_model.mapToSource(idx).row()
            for idx in self._table.selectionModel().selectedRows()
        })
        cats = [c for r in rows[:2] if (c := self._source_model.cat_at(r)) is not None]
        was_collapsed = self._detail.maximumHeight() == 0
        self._detail.show_cats(cats)
        if cats and was_collapsed:
            total   = self._detail_splitter.height()
            panel_h = 200 if len(cats) == 1 else 300
            self._detail_splitter.setSizes([max(10, total - panel_h), panel_h])

    # ── Filtering ──────────────────────────────────────────────────────────

    def _filter(self, room_key, btn: QPushButton):
        if self._active_btn and self._active_btn is not btn:
            self._active_btn.setChecked(False)
        btn.setChecked(True)
        self._active_btn = btn
        self._proxy_model.set_room(room_key)
        self._update_header(room_key)
        self._update_count()
        self._detail.show_cats([])

    def _update_header(self, room_key):
        if room_key is None:
            self._header_lbl.setText("All Cats")
        elif room_key == "__gone__":
            self._header_lbl.setText("Gone")
        elif room_key == "__adventure__":
            self._header_lbl.setText("On Adventure")
        else:
            self._header_lbl.setText(ROOM_DISPLAY.get(room_key, room_key))

    def _update_count(self):
        visible = self._proxy_model.rowCount()
        total   = self._source_model.rowCount()
        self._count_lbl.setText(f"  {visible} / {total} cats")

        placed = sum(1 for c in self._cats if c.status == "In House")
        adv    = sum(1 for c in self._cats if c.status == "Adventure")
        gone   = sum(1 for c in self._cats if c.status == "Gone")
        self._summary_lbl.setText(
            f"House: {placed}  |  Away: {adv}  |  Gone: {gone}")

    # ── Loading ────────────────────────────────────────────────────────────

    def load_save(self, path: str):
        self._current_save = path
        if self._watcher.files():
            self._watcher.removePaths(self._watcher.files())
        self._watcher.addPath(path)

        try:
            cats, errors = parse_save(path)
            self._cats = cats
            self._source_model.load(cats)
            self._rebuild_room_buttons(cats)
            self._filter(None, self._btn_all)

            name = os.path.basename(path)
            self._save_lbl.setText(name)
            self.setWindowTitle(f"Mewgenics Breeding Manager — {name}")

            msg = f"Loaded {len(cats)} cats from {name}"
            if errors:
                msg += f"  ({len(errors)} parse errors)"
            self.statusBar().showMessage(msg)
        except Exception as e:
            self.statusBar().showMessage(f"Error loading save: {e}")

    def _open_file(self):
        saves   = find_save_files()
        start   = os.path.dirname(saves[0]) if saves else os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Mewgenics Save File", start,
            "Save Files (*.sav);;All Files (*)")
        if path:
            self.load_save(path)

    def _reload(self):
        if self._current_save:
            self.load_save(self._current_save)

    def _on_file_changed(self, path: str):
        if path == self._current_save:
            self._reload()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _hsep() -> QFrame:
    f = QFrame(); f.setFrameShape(QFrame.HLine)
    f.setStyleSheet("color:#1e1e38; margin:6px 0;")
    return f


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    pal = QPalette()
    pal.setColor(QPalette.Window,          QColor(13,  13,  28))
    pal.setColor(QPalette.WindowText,      QColor(220, 220, 230))
    pal.setColor(QPalette.Base,            QColor(18,  18,  36))
    pal.setColor(QPalette.AlternateBase,   QColor(20,  20,  40))
    pal.setColor(QPalette.Text,            QColor(220, 220, 230))
    pal.setColor(QPalette.Button,          QColor(22,  22,  46))
    pal.setColor(QPalette.ButtonText,      QColor(200, 200, 210))
    pal.setColor(QPalette.Highlight,       QColor(30,  48, 100))
    pal.setColor(QPalette.HighlightedText, QColor(255, 255, 255))
    pal.setColor(QPalette.ToolTipBase,     QColor(20,  20,  40))
    pal.setColor(QPalette.ToolTipText,     QColor(220, 220, 230))
    app.setPalette(pal)

    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
