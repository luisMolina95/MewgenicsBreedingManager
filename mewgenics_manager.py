#!/usr/bin/env python3
"""
Mewgenics Breeding Manager
External viewer for cat stats, room locations, and breeding pairs.
Parsing logic based on pzx521521/mewgenics-save-editor.

Requirements: pip install PySide6 lz4
"""

import sys
import re
import struct
import sqlite3
import lz4.block
import os
from pathlib import Path
from typing import Optional

_IDENT_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTableView, QPushButton, QLabel, QFileDialog, QHeaderView,
    QAbstractItemView, QSplitter, QFrame, QDialog, QGridLayout, QSizePolicy,
    QLineEdit,
)
from PySide6.QtCore import (
    Qt, QAbstractTableModel, QModelIndex, QSortFilterProxyModel,
    QFileSystemWatcher, QItemSelectionModel,
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
    "Floor1_Large":   "Ground Floor Left",
    "Floor1_Small":   "Ground Floor Right",
    "Floor2_Large":   "Second Floor",
    "Floor2_Small":   "Second Floor Right",
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


# ── Ability / mutation tooltip lookup ────────────────────────────────────────
# Keys: display name lowercased with all non-alphanumeric chars removed.
# Sources: mewgenics.wiki.gg/wiki/Mutations and /wiki/Abilities

_ABILITY_LOOKUP: dict[str, str] = {
    # Birth defects
    "twoedarm":           "-2 Strength",
    "twotoedarm":         "-2 Strength",
    "bentarm":            "-2 Speed",
    "conjoinedbody":      "+2 Constitution, -3 Speed",
    "lumpybody":          "Start each battle with 1 Bruise",
    "malnourishedbody":   "-1 Constitution",
    "turnersyndrome":     "-2 Intelligence",
    "birdbeakears":       "Start each battle with Confusion 2",
    "floppyears":         "Start each battle with Immobile 1",
    "inwardeyes":         "Start each battle with Confusion 2",
    "redeyes":            "Gain 5% miss chance every turn",
    "blind":              "Start every battle with Blind 1",
    "bushyeyebrow":       "-1 Luck",
    "noeyebrows":         "-2 Charisma",
    "sloth":              "-3 Charisma, Brace 1",
    "conjoinedtwin":      "+2 Intelligence, -3 Charisma",
    "bentleg":            "Trample — units moved through take damage",
    "duckleg":            "-2 Speed, water does not slow movement",
    "twoedleg":           "-2 Strength",
    "twotoedleg":         "-2 Strength",
    "nomouth":            "Can't use consumables, eat, or musical abilities",
    "cleftlip":           "-2 Charisma",
    "lumpytail":          "+1 Constitution, start with 1 Immobile",
    "notail":             "-1 Dexterity",
    "tailsack":           "-1 Speed, -1 Constitution",
    # Collarless passives
    "180":                "When you use your basic attack, turn around and use it again.",
    "amped":              "Gain +1 Speed at the end of your turn.",
    "amplify":            "+1 Magic Damage.",
    "animalhandler":      "Start each battle with a random vermin familiar.",
    "bareminimum":        "Your stats can't go below 5.",
    "charming":           "25% chance to inflict Charm on units that damage you.",
    "daunt":              "Small enemies won't attack you.",
    "dealer":             "You can use consumables on other units.",
    "deathboon":          "When downed, all allies gain All Stats Up.",
    "deathsdoor":         "While at 1 HP, spells cost 1 mana but can only be cast once per turn.",
    "deathproof":         "While downed, 25% chance to revive with 1 HP at end of each round.",
    "dirtyclaws":         "Attacks on Poisoned/Bleeding enemies inflict +1 Poison/Bleed.",
    "etank":              "Start each battle with +20 unfilled max health.",
    "fastfootsies":       "Immune to negative tile effects.",
    "firstimpression":    "Start each battle with +1 Bonus Attack.",
    "furious":            "Gain +1 Damage per critical hit. +5% critical hit chance.",
    "gassy":              "When you take damage, knock back all adjacent units.",
    "hotblooded":         "Burn you inflict is increased by 1.",
    "infested":           "50% chance to spawn a flea familiar when you end your turn.",
    "latebloomer":        "On your 5th turn, gain All Stats Up 3.",
    "leader":             "Adjacent allies have +1 Damage and +1 Range.",
    "longshot":           "+1 Range.",
    "luckdrain":          "Steal luck from enemies you damage.",
    "lucky":              "+4 Luck.",
    "mange":              "Inflict Poison 1 on units that contact you.",
    "mania":              "10% chance to restore all mana at the start of your turn.",
    "metaldetector":      "5% chance to spawn a coin when you move over a tile.",
    "mightofthemeek":     "Damage of 2 or less is always critical.",
    "minime":             "Start each battle with a tiny duplicate cat at half your stats.",
    "naturalhealing":     "+1 Health Regeneration.",
    "overconfident":      "While at full HP, spells cost 2 less but you take double damage.",
    "patience":           "If you end your turn without actions, gain an extra turn at end of round.",
    "protection":         "Gain +1 Holy Shield.",
    "pulp":               "When you kill a unit, it becomes meat.",
    "rockin":             "Spawn 4 small rocks at the start of each battle.",
    "santasangre":        "When downed, allies heal 12 HP. Excess healing becomes Shield.",
    "scavenger":          "If trinket slot is empty, equip a small food item at battle start.",
    "selfassured":        "Gain a random stat up whenever you down a unit.",
    "serialkiller":       "After 3 kills, gain +6 Speed and backstabs have 100% crit.",
    "skillshare":         "Your other passive is shared with all party cats at battle start.",
    "slugger":            "+1 Damage.",
    "study":              "Gain +1 Intelligence whenever you hit a new unit type.",
    "unrestricted":       "Once-per-battle abilities can be cast once per turn instead.",
    "unscarred":          "While at full HP, 100% critical hit chance.",
    "wiggly":             "+25% Dodge Chance.",
    "worms":              "50% chance to spawn a maggot familiar when you end your turn.",
    "zenkaiboost":        "End battle at 1 HP → +1 random stat permanently, next battle starts with All Stats Up 3.",
    # Fighter passives
    "avenger":            "When an allied cat is downed, gain All Stats Up 2 and heal 8.",
    "boned":              "When you kill a unit without a weapon, gain a Bone Club.",
    "dualwield":          "When you use your weapon, automatically use it again for free.",
    "fervor":             "When you down a unit, heal 5 HP.",
    "frenzy":             "When you down a unit, gain +2 Strength.",
    "hamsterstyle":       "+1 INT, -1 STR, +1 CON, +1 Health Regen, start with 2 Bonus Moves.",
    "hulkup":             "When you take damage, gain +2 Speed.",
    "math":               "Spells cost 3 mana but can only be cast once per turn.",
    "merciless":          "10+ damage in a single hit: +2 Shield and refresh movement action.",
    "overpowered":        "Excess damage causes enemies to explode, dealing overflow to nearby units.",
    "patellarreflex":     "When damaged, counter-attack for 1 damage + Bruise.",
    "punchface":          "Basic attacks hitting the front of a unit are always critical.",
    "ratstyle":           "+2 Speed, +10% Dodge Chance.",
    "scars":              "Start with +1 Brace.",
    "skullcrack":         "Your basic attack inflicts Bruise.",
    "smash":              "Weapons deal triple damage but always break when used.",
    "thickskull":         "All injuries are Concussions. +3 Shield per concussion (max 30).",
    "turtlestyle":        "+4 Armor, +2 Vitality, -1 Speed.",
    "underdog":           "+2 STR and +1 Brace for each adjacent enemy.",
    "vengeful":           "Basic attack is always critical against enemies that have damaged you.",
    "weaponmaster":       "Weapon/item abilities deal +2 Damage and +25% critical chance.",
    # Tank passives
    "bouncer":            "When an ally takes damage, move toward the source and attack if possible.",
    "chainknockback":     "Basic attack gains +1 Knockback; knocked-back units knock back others.",
    "hardhead":           "You block attacks from the front.",
    "hardy":              "Heal to full HP at the start of each battle.",
    "heavyhanded":        "+2 Knockback Damage.",
    "homerun":            "Increases all Knockback by 10.",
    "mountainform":       "Knockback immunity. Tiles walked over become dirt and may spawn rocks.",
    "petrocks":           "Each rock you spawn becomes a Pet Rock. One Pet Rock spawns per combat.",
    "plow":               "When you knock back a unit, leave a rock where it was.",
    "prioritytarget":     "Enemies attack you instead of allies if they can.",
    "protective":         "Your allies have Brace 1.",
    "scabs":              "Gain +2 Shield when you take damage from an ability.",
    "slackoff":           "If you end your turn with unused movement, gain 8 HP.",
    "slowandsteady":      "At speed 0 or below, attack an extra time per turn. -2 SPD, +1 Range/turn.",
    "stoic":              "If you end your turn with unused movement, gain +2 Bonus Moves.",
    "thorns":             "Start with Thorns 2. Gain +1 Thorns when you take damage.",
    "thunderthighs":      "Trample. Contact effects from abilities/items apply when trampling.",
    "toadstyle":          "Movement action is a jump; landing on a unit deals damage and displaces it.",
    "wrestlemaniac":      "Basic attack becomes Suplex when adjacent to enemies. Gain Toss ability.",
    # Psychic passives
    "antigravity":        "Flying Movement. +1 SPD when using Gravity ability. Gravity costs -1 mana.",
    "beckon":             "Your basic attack has +4 Knockback.",
    "blink":              "33% chance to teleport to a random tile when targeted.",
    "eldritchvisage":     "Start of your turn: inflict Magic Weakness 1 on all enemies in line of sight.",
    "enlightened":        "While at full mana, the first spell you cast each turn is free.",
    "fullpower":          "While at full mana, basic attack deals triple damage and has +3 Knockback.",
    "glow":               "Your basic attack inflicts Blind.",
    "omniscience":        "All line-of-sight restrictions ignored. Hidden enemies are always highlighted.",
    "overflow":           "While at full mana, gain +2 Brace and Flying Movement. Mana is uncapped.",
    "psionicrepel":       "Units that attack or contact you get knocked back 10 tiles.",
    "psysmack":           "Knockback damage you and allies deal is doubled.",
    "soulshatter":        "When you kill a unit, deal 1 damage to all enemies.",
    "truesight":          "You and your allies can't miss enemies within your line of sight.",
    "wither":             "Gravity abilities inflict a random negative status on enemies.",
    # Necromancer passives
    "bedbugs":            "Start battles with 2 beefy leech familiars.",
    "cambionconception":  "When downed, spawn a demon kitten familiar.",
    "eternalhealth":      "Suffer only Jinxed when downed; heal to full when your party wins.",
    "infected":           "When you down a unit, reanimate it with 50% HP.",
    "lastgrasp":          "When downed, each enemy takes 6 damage and each ally heals 6 HP.",
    "leechmother":        "Your basic attack spawns a leech familiar.",
    "onewithnothing":     "If you end your turn with 0 mana, Mana Regeneration is doubled.",
    "parasitic":          "When you gain health, spawn a leech familiar.",
    "relentlessdead":     "At end of each round, spawn a Zombie kitten familiar onto a random tile.",
    "sacrificiallamb":    "When downed, allies gain All Stats Up and take an extra turn.",
    "soulbond":           "Your basic attack inflicts Soul Link.",
    "spreadsorrow":       "When you inflict a debuff, also inflict it on another random enemy.",
    "superstition":       "Basic attack inflicts -1 Luck. Units that damage you also lose 1 Luck.",
    "torpor":             "While downed, basic attack is Haunt. Your body gains +6 corpse HP.",
    "undeath":            "When downed, reanimate each ally to 33% HP. (Once per battle.)",
    "vampirism":          "Your basic attack has Lifesteal.",
    # Thief passives
    "afterimage":         "When you move, spawn a shadow that mimics your basic action.",
    "agile":              "+2 Movement Range. Move a 2nd time if not using full range.",
    "backstabber":        "Your backstabs are always critical.",
    "bountyhunter":       "During your turn, one random enemy has a Bounty.",
    "burgle":             "Your basic attack gains you 1 coin when it deals damage.",
    "cripple":            "Your critical hits inflict Immobilize and Weakness 2.",
    "critical":           "Critical hits deal +100% more damage. Gain +1 Luck per critical hit.",
    "doublethrow":        "Your basic attack hits twice for half damage.",
    "firststrike":        "Gain an extra turn at the start of battle.",
    "goldenclaws":        "+1 Damage for each coin you collect.",
    "more":               "When you kill a unit, refresh your movement action.",
    "penetrate":          "Basic attack passes through units and ignores shield. +1 Range.",
    "pinpoint":           "Your critical hits inflict Marked.",
    "poisontips":         "Your basic attack inflicts Poison 1.",
    "razorclaws":         "Your basic attack inflicts Bleed 1.",
    "shank":              "When behind an enemy, basic attack hits 2 times using Strength.",
    "shiv":               "Basic attack: +2 damage, +25% crit, inflicts Bleed 1 in melee range.",
    "stealthed":          "Start each battle with Stealth.",
    "sweetspot":          "+1 Range. Basic attack deals more damage the farther away you are.",
    "weakspot":           "Basic attack ignores shield and inflicts Weakness 1.",
    # Hunter passives
    "animalcontrol":      "Your basic attack causes units to immediately attack an enemy in range.",
    "broodmother":        "Familiars and Charmed units gain +2 Damage and +5 HP.",
    "bullseye":           "Your ranged attacks never miss. +25% critical hit chance.",
    "fleabag":            "Spawn Flea familiars equal to kills this battle when your turn ends.",
    "gravityfalls":       "+1 damage per tile beyond range 3.",
    "hazardous":          "Tile damage and effects are doubled.",
    "huntersboon":        "When you kill an enemy, gain 5 mana.",
    "luckswing":          "+50% critical hit chance but +25% miss chance.",
    "rubberarrows":       "Your projectiles bounce to another enemy within 3 tiles.",
    "sniper":             "Critical hits deal +100% damage and have 25% chance to inflict Stun.",
    "splitshot":          "Basic attack shoots multiple projectiles in a 5-tile cross (half damage each).",
    "survivalist":        "4 healing consumables and a water bottle added. +2 food stored after each battle.",
    "taintedmother":      "Familiars and Charmed units gain +4 Speed and inflict Poison and Bleed.",
    "vampirism":          "Your basic attack has Lifesteal.",
    # Cleric passives
    "angelic":            "When you heal an ally, they also gain mana.",
    "blessed":            "Gain +1 to 2 random stats at the start of each turn.",
    "devoted":            "Healing you provide is doubled.",
    "holyaura":           "Allies adjacent to you gain +1 Brace.",
    "inspiration":        "When you heal an ally, they gain +1 Damage.",
    "martyrdom":          "When you take damage, all allies heal 1 HP.",
    "pacifist":           "Your basic attack heals instead of dealing damage.",
    "radiant":            "Your healing abilities also deal damage to nearby enemies.",
    "sanctuary":          "Allies in your line of sight are immune to debuffs.",
    "smite":              "Holy damage you deal is doubled.",
    # Mage passives
    "arcanemastery":      "Your spells cost 1 less mana.",
    "blastzone":          "Your AOE spells affect a larger area.",
    "crystalclear":       "While at full mana, your spells deal +2 damage.",
    "focused":            "+2 Intelligence. Your spells deal +1 damage.",
    "magicshield":        "Gain +1 Shield when you cast a spell.",
    "manaburn":           "Your spells inflict Mana Drain.",
    "overload":           "When you run out of mana, deal damage equal to mana spent to all nearby enemies.",
    "sorcerersoul":       "Access to Sorcerer class abilities when leveling up.",
    "spellweaver":        "Casting the same spell twice in a row doubles its damage.",
    "unstable":           "Your spells have 20% chance to be empowered for double damage.",
    # Monk passives
    "acrobatics":         "+2 Movement Range. You can move through enemies.",
    "concentration":      "If you don't move during your turn, your next attack is always critical.",
    "counterattack":      "When damaged in melee, automatically counter-attack.",
    "discipline":         "+2 to all stats at the start of each battle.",
    "flowstate":          "After using an ability, gain +1 Speed for the rest of your turn.",
    "harmonize":          "Your abilities heal allies they pass through.",
    "innerpeace":         "+1 Health Regeneration and +1 Mana Regeneration per turn.",
    "ironbody":           "+4 Constitution. You are immune to Stun and Immobilize.",
    "reflexes":           "+10% Dodge Chance. Dodging an attack gives you +1 Speed.",
    "zenmaster":          "While at full HP, all your abilities cost 0 mana.",
    # Druid passives
    "barkaspect":         "Gain +1 Brace when you take damage.",
    "earthbound":         "Immunity to knockback. Gain +2 Constitution.",
    "floral":             "Spawn flowers that heal adjacent allies each turn.",
    "growth":             "Gain +1 to a random stat at the end of each battle.",
    "naturecall":         "Spawn a random nature familiar at the start of each battle.",
    "photosynthesis":     "Regenerate 1 HP and 1 mana each turn when standing on grass/dirt.",
    "pollinate":          "Your familiars spread healing pollen to adjacent allies.",
    "primalrage":         "When you take damage, gain +1 Strength and +1 Speed (stacks).",
    "regrowth":           "When downed, revive with 25% HP once per battle.",
    "thornedbody":        "Units that attack you in melee take 2 damage.",
    # Jester passives
    "allofthem":          "Gain a copy of the last ability used by any unit this battle.",
    "alsorandom":         "At the start of your turn, gain a random status effect.",
    "chaosmagic":         "Your abilities have random additional effects.",
    "clumsy":             "50% chance to hit adjacent allies when attacking.",
    "copycat":            "Your basic attack copies the last ability used by an ally.",
    "gambler":            "At battle start, randomly gain or lose 1-3 of each stat.",
    "jackofalltrades":    "Gain one random ability from each class at the start of each battle.",
    "jinx":               "Units adjacent to you have -2 Luck.",
    "pandemonium":        "At the start of each round, swap positions with a random unit.",
    "pratfall":           "When you miss, all allies gain +1 Damage for the next attack.",
    # Soul passives
    "butcherssoul":       "Access to Butcher class abilities when leveling up.",
    "clericsoul":         "Access to Cleric class abilities when leveling up.",
    "druidsoul":          "Access to Druid class abilities when leveling up.",
    "fighterssoul":       "Access to Fighter class abilities when leveling up.",
    "hunterssoul":        "Access to Hunter class abilities when leveling up.",
    "jesterssoul":        "Access to Jester class abilities when leveling up.",
    "magessoul":          "Access to Mage class abilities when leveling up.",
    "monkssoul":          "Access to Monk class abilities when leveling up.",
    "necromancerssoul":   "Access to Necromancer class abilities when leveling up.",
    "psychicssoul":       "Access to Psychic class abilities when leveling up.",
    "tankssoul":          "Access to Tank class abilities when leveling up.",
    "thiefsoul":          "Access to Thief class abilities when leveling up.",
    "tinkerersoul":       "Access to Tinkerer class abilities when leveling up.",
    "voidsoul":           "Only upgraded Collarless abilities offered on level up. Collarless spells cost 1 less mana.",
}


def _ability_tip(name: str) -> str:
    """Return a tooltip description for an ability/mutation name, or '' if unknown."""
    key = re.sub(r'[^a-z0-9]', '', name.lower())
    return _ABILITY_LOOKUP.get(key, "")


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


# ── Parent UID scanner ────────────────────────────────────────────────────────

def _scan_blob_for_parent_uids(raw: bytes, uid_set: frozenset, self_uid: int) -> tuple[int, int]:
    """
    Scan the decompressed blob byte-by-byte looking for two consecutive u64
    values (4-byte aligned) that are in uid_set and are not self_uid.
    Parent UIDs appear early in the blob so we only scan the first 1 KB.
    Returns (parent_a_uid, parent_b_uid), each 0 if not found.
    """
    if not uid_set:
        return 0, 0
    limit = min(1024, len(raw) - 16)
    i = 12  # skip breed_id(4) + own uid(8)
    while i <= limit - 16:
        lo1, hi1 = struct.unpack_from('<II', raw, i)
        v1 = lo1 + hi1 * 4_294_967_296
        if v1 in uid_set and v1 != self_uid:
            lo2, hi2 = struct.unpack_from('<II', raw, i + 8)
            v2 = lo2 + hi2 * 4_294_967_296
            if v2 in uid_set and v2 != self_uid:
                return v1, v2          # both parents found
            if v2 == 0:
                return v1, 0           # one parent (other unknown)
        i += 4  # u64-aligned steps
    return 0, 0


# ── Visual mutation scanner ───────────────────────────────────────────────────

# 14 body-part slots (0-indexed); slot_id ≥ 300 in the blob = active mutation
VISUAL_MUT_NAMES = [
    "Body", "Head", "Tail", "Eye", "Ear",
    "Leg", "Paw", "Belly", "Back", "Fur",
    "Wing", "Horn", "Fang", "Mark",
]

def _find_mutation_table(raw: bytes) -> int:
    """
    Locate the 296-byte visual-mutation table by scanning for its header:
      entry-0: f32 scale [0.05, 20.0], u32 coat_id [1, 20000], u32 small ≤ 500,
               u32 sentinel (0 or ≤ 5000)
      entries 1-14: 20 bytes each, second u32 (coat_id or 0) validated for ≥10 slots.
    Returns base offset, or -1 if not found.
    """
    size = 16 + 14 * 20   # 296 bytes
    limit = len(raw) - size
    for base in range(limit):
        scale = struct.unpack_from('<f', raw, base)[0]
        if not (0.05 <= scale <= 20.0):
            continue
        coat = struct.unpack_from('<I', raw, base + 4)[0]
        if coat == 0 or coat > 20_000:
            continue
        t1 = struct.unpack_from('<I', raw, base + 8)[0]
        if t1 > 500:
            continue
        t2 = struct.unpack_from('<I', raw, base + 12)[0]
        if t2 != 0xFFFF_FFFF and t2 > 5_000:
            continue
        matches = sum(
            1 for i in range(14)
            if struct.unpack_from('<I', raw, base + 16 + i * 20 + 4)[0] in (coat, 0)
        )
        if matches >= 10:
            return base
    return -1


def _read_visual_mutations(raw: bytes) -> list:
    """Return list of active visual-mutation names (e.g. 'Eye Mutation')."""
    base = _find_mutation_table(raw)
    if base == -1:
        return []
    result = []
    for i in range(14):
        slot_id = struct.unpack_from('<I', raw, base + 16 + i * 20)[0]
        if slot_id >= 300:
            name = VISUAL_MUT_NAMES[i] if i < len(VISUAL_MUT_NAMES) else f"Mutation{i+1}"
            result.append(f"{name} Mutation")
    return result


# ── Cat ───────────────────────────────────────────────────────────────────────

class Cat:
    # parent_a / parent_b are resolved after the full save is loaded
    parent_a: Optional['Cat'] = None
    parent_b: Optional['Cat'] = None
    generation: int = 0   # generation depth: 0=stray, 1=child of strays, etc.

    def __init__(self, blob: bytes, cat_key: int, house_info: dict, adventure_keys: set):
        uncomp_size = struct.unpack('<I', blob[:4])[0]
        raw = lz4.block.decompress(blob[4:], uncompressed_size=uncomp_size)
        r   = BinaryReader(raw)
        self._raw = raw   # kept for parent-UID blob scan in parse_save

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
        self._uid_int = r.u64()            # cat's own unique id (seed)
        self.unique_id = hex(self._uid_int)
        self.name = r.utf16str()
        _name_end = r.pos   # used below for reliable sex u16 read at _name_end+8

        r.str()  # unknown string between name and parent refs

        # Possible parent UIDs — fixed-position attempt.
        # parse_save will run a blob scan as a fallback if these don't resolve.
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

        # Personality stats (aggression, libido, inbredness).
        # Exact offsets not yet documented; filled in by parse_save if found.
        self.aggression  = None   # None = unknown
        self.libido      = None
        self.inbredness  = None

        # Relationship scaffolds — resolved by parse_save after all cats loaded.
        self._lover_uids: list[int] = []
        self._hater_uids: list[int] = []
        self.lovers:   list['Cat'] = []
        self.haters:   list['Cat'] = []
        self.children: list['Cat'] = []   # direct offspring; assigned by parse_save

        # ── Ability run — anchored on "DefaultMove" ─────────────────────────
        # The ability block is a u64-length-prefixed ASCII identifier run.
        # Structure (from open-source editor research):
        #   items[0]  = "DefaultMove"  (active slot 1 default)
        #   items[1-5] = active abilities 2-6
        #   items[6-9] = padding / unknown slots
        #   items[10]  = Passive1 mutation  (e.g. "Sturdy", "Longshot")
        #   After run:  u32 tier, then 3 × [u64 id][u32 tier] tail entries
        #               = Passive2, Disorder1, Disorder2
        curr = r.pos
        run_start = -1
        for i in range(curr, min(curr + 600, len(raw) - 19)):
            lo = struct.unpack_from('<I', raw, i)[0]
            hi = struct.unpack_from('<I', raw, i + 4)[0]
            if hi != 0 or not (1 <= lo <= 96):
                continue
            try:
                cand = raw[i + 8: i + 8 + lo].decode('ascii')
                if cand == 'DefaultMove':
                    run_start = i
                    break
            except Exception:
                continue

        if run_start != -1:
            r.seek(run_start)
            # Read the full run until a non-identifier is encountered
            run_items: list[str] = []
            for _ in range(32):
                saved = r.pos
                item = r.str()
                if item is None or not _IDENT_RE.match(item):
                    r.seek(saved)
                    break
                run_items.append(item)

            # Active abilities: items[1-5] (skip DefaultMove at [0])
            self.abilities = [x for x in run_items[1:6] if _valid_str(x)]

            # Passive mutations: item[10] then 3 tail tier-entries
            passives: list[str] = []
            if len(run_items) > 10 and _valid_str(run_items[10]):
                passives.append(run_items[10])

            try:
                r.u32()   # passive1 tier — discard
            except Exception:
                pass

            for _ in range(3):   # Passive2, Disorder1, Disorder2
                saved = r.pos
                item = r.str()
                if item is None or not _IDENT_RE.match(item) or not _valid_str(item):
                    r.seek(saved)
                    break
                passives.append(item)
                try:
                    r.u32()   # tier — discard
                except Exception:
                    pass

            self.mutations = passives
            self.equipment = []   # equipment parsing requires separate byte-marker logic

        else:
            # Fallback: old heuristic scan for any uppercase-starting ASCII string
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

        # Visual mutations from the 296-byte fixed mutation table (prepend so they
        # show first; passive trait strings from the ability run follow)
        vis = _read_visual_mutations(raw)
        if vis:
            self.mutations = vis + self.mutations

        # For cats whose deep-blob gender string resolved cleanly to "male"/"female",
        # trust that result — it is authoritative and overriding it has been shown
        # to mis-gender some cats (e.g. Midna).  Only attempt the sex_u16 fallback
        # at _name_end+8 when the deep-blob read returned something ambiguous (e.g.
        # ditto cats, where the parser position drifts and returns the wrong string).
        if self.gender not in ("male", "female"):
            try:
                sex_u16 = struct.unpack_from('<H', raw, _name_end + 8)[0]
                self.gender = {1: "male", 2: "female"}.get(sex_u16, "female")
            except Exception:
                pass

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
        return "F"  # unknown/ditto defaults to F

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
    if ga == "M" and gb == "F":
        return True, ""
    if ga == "F" and gb == "M":
        return True, ""
    # Same sex
    label = "female" if ga == "F" else "male"
    return False, f"Both cats are {label} — cannot produce offspring"


# ── Compatibility check ───────────────────────────────────────────────────────

def _compatibility(focus: 'Cat', other: 'Cat') -> str:
    """
    Returns one of: 'self' | 'incompatible' | 'risky' | 'ok'
    Used to dim rows in the table when a single cat is selected.
    """
    if focus is other:
        return 'self'
    ok, _ = can_breed(focus, other)
    if not ok:
        return 'incompatible'
    # Hate relationship
    if other in getattr(focus, 'haters', []) or focus in getattr(other, 'haters', []):
        return 'incompatible'
    # Direct parent/offspring
    if focus in get_parents(other) or other in get_parents(focus):
        return 'incompatible'
    # Shared ancestors → inbreeding risk
    if find_common_ancestors(focus, other):
        return 'risky'
    return 'ok'


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


def _parse_pedigree(conn) -> dict:
    """
    Parse the pedigree blob from the files table.
    Each 32-byte entry: u64 cat_key, u64 parent_a_key, u64 parent_b_key, u64 extra.
    0xFFFFFFFFFFFFFFFF means null/unknown for parent fields.

    Returns ped_map: db_key -> (parent_a_db_key | None, parent_b_db_key | None).

    NOTE: children are NOT derived from this map because the pedigree blob
    appears to store more than just direct parent-child pairs (possibly full
    lineage chains), which causes circular references when used for children.
    Children are instead computed bottom-up from resolved parent fields.
    """
    try:
        row = conn.execute("SELECT data FROM files WHERE key='pedigree'").fetchone()
        if not row:
            return {}
        data = row[0]
    except Exception:
        return {}

    NULL = 0xFFFF_FFFF_FFFF_FFFF
    MAX_KEY = 1_000_000   # anything larger is a legacy UID or garbage
    ped_map: dict = {}

    # Entries start at offset 8 (after a single u64 header), stride 32
    for pos in range(8, len(data) - 31, 32):
        cat_k, pa_k, pb_k, _ = struct.unpack_from('<QQQQ', data, pos)
        if cat_k == 0 or cat_k == NULL or cat_k > MAX_KEY:
            continue
        pa = int(pa_k) if pa_k != NULL and 0 < pa_k <= MAX_KEY else None
        pb = int(pb_k) if pb_k != NULL and 0 < pb_k <= MAX_KEY else None
        # First occurrence of each cat_k is the parent record; later occurrences
        # are other relationship types (breeding history, etc.) — skip them.
        if int(cat_k) not in ped_map:
            ped_map[int(cat_k)] = (pa, pb)

    return ped_map


def parse_save(path: str) -> tuple[list, list]:
    conn  = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    house = _get_house_info(conn)
    adv   = _get_adventure_keys(conn)
    rows  = conn.execute("SELECT key, data FROM cats").fetchall()
    ped_map = _parse_pedigree(conn)
    conn.close()

    cats, errors = [], []
    for key, blob in rows:
        try:
            cats.append(Cat(blob, key, house, adv))
        except Exception as e:
            errors.append((key, str(e)))

    key_to_cat: dict = {c.db_key: c for c in cats}

    for cat in cats:
        # Pedigree db_key lookup — only assigns a parent if that cat is still
        # present in the save.  If the real parents are gone (dead/sold), we
        # leave parent_a/parent_b as None rather than falling back to an
        # unreliable blob-UID scan that picks up wrong living cats.
        pa: Optional[Cat] = None
        pb: Optional[Cat] = None
        if cat.db_key in ped_map:
            pa_k, pb_k = ped_map[cat.db_key]
            pa = key_to_cat.get(pa_k)
            pb = key_to_cat.get(pb_k)
            # Sanity: a cat cannot be its own parent
            if pa is cat: pa = None
            if pb is cat: pb = None
        cat.parent_a = pa
        cat.parent_b = pb

    # Build children bottom-up from the now-resolved parent fields.
    # This avoids the circular-reference problem in the pedigree blob.
    for cat in cats:
        cat.children = []
    for cat in cats:
        for parent in (cat.parent_a, cat.parent_b):
            if parent is not None and cat not in parent.children:
                parent.children.append(cat)

    # Compute generation depth (0=stray, N=max parent gen + 1).
    # Uses memoisation; safe because circular parent refs are already blocked.
    _gen_cache: dict = {}
    def _get_gen(c: Cat) -> int:
        cid = id(c)
        if cid in _gen_cache:
            return _gen_cache[cid]
        if c.parent_a is None and c.parent_b is None:
            _gen_cache[cid] = 0
            return 0
        pa_g = _get_gen(c.parent_a) if c.parent_a else -1
        pb_g = _get_gen(c.parent_b) if c.parent_b else -1
        g = max(pa_g, pb_g) + 1
        _gen_cache[cid] = g
        return g
    for cat in cats:
        cat.generation = _get_gen(cat)

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

COLUMNS   = ["Name", "♀/♂", "Room", "Status"] + STAT_NAMES + ["Sum", "Abilities", "Mutations", "Gen", "Source", "Inbr"]
COL_NAME  = 0
COL_GEN   = 1
COL_ROOM  = 2
COL_STAT  = 3
STAT_COLS = list(range(4, 11))   # STR … LCK  (indices 4–10)
COL_SUM   = 11
COL_ABIL  = 12
COL_MUTS  = 13
COL_AGE   = 14   # generation depth
COL_SRC   = 15
COL_INB   = 16

# Fixed pixel widths for narrow columns
_W_STATUS = 62
_W_STAT   = 34
_W_GEN    = 28


class CatTableModel(QAbstractTableModel):
    def __init__(self):
        super().__init__()
        self._cats: list[Cat] = []
        self._focus_cat: Optional[Cat] = None
        self._show_lineage: bool = False

    def set_show_lineage(self, show: bool):
        self._show_lineage = show
        if self._cats:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self._cats) - 1, len(COLUMNS) - 1),
                [Qt.BackgroundRole, Qt.ForegroundRole],
            )

    def load(self, cats: list[Cat]):
        self.beginResetModel()
        self._cats = cats
        self.endResetModel()

    def set_focus_cat(self, cat: Optional[Cat]):
        self._focus_cat = cat
        if self._cats:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self._cats) - 1, len(COLUMNS) - 1),
                [Qt.BackgroundRole, Qt.ForegroundRole],
            )

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
            if col == COL_SUM:
                return str(sum(cat.base_stats.values()))
            if col == COL_MUTS:
                return ", ".join(cat.mutations)
            if col == COL_ABIL:
                return ", ".join(cat.abilities)
            if col == COL_AGE:
                return str(cat.generation)
            if col == COL_SRC:
                pa, pb = cat.parent_a, cat.parent_b
                if pa is None and pb is None:
                    return "Stray"
                def _pname(p):
                    return p.name if p.status != "Gone" else f"{p.name} (gone)"
                return " × ".join(_pname(p) for p in (pa, pb) if p is not None)
            if col == COL_INB:
                if cat.parent_a is None or cat.parent_b is None:
                    return "—"
                score = len(find_common_ancestors(cat.parent_a, cat.parent_b))
                return str(score) if score else "—"

        elif role == Qt.UserRole:
            if col in STAT_COLS:
                return cat.base_stats[STAT_NAMES[col - 4]]
            if col == COL_SUM:
                return sum(cat.base_stats.values())
            if col == COL_AGE:
                return cat.generation
            return self.data(index, Qt.DisplayRole)

        elif role == Qt.BackgroundRole:
            compat = (
                _compatibility(self._focus_cat, cat)
                if self._focus_cat is not None and cat is not self._focus_cat
                else None
            )
            # Suppress risky highlight when lineage features are off
            if compat == 'risky' and not self._show_lineage:
                compat = 'ok'
            if col in STAT_COLS:
                base_c = STAT_COLORS.get(cat.base_stats[STAT_NAMES[col - 4]], QColor(100, 100, 115))
                if compat == 'incompatible':
                    return QBrush(QColor(base_c.red() // 4, base_c.green() // 4, base_c.blue() // 4))
                if compat == 'risky':
                    return QBrush(QColor(base_c.red() // 2, base_c.green() // 2, base_c.blue() // 2))
                return QBrush(base_c)
            if col == COL_STAT:
                sc = STATUS_COLOR.get(cat.status, QColor(80, 80, 90))
                if compat == 'incompatible':
                    return QBrush(QColor(sc.red() // 4, sc.green() // 4, sc.blue() // 4))
                if compat == 'risky':
                    return QBrush(QColor(sc.red() // 2, sc.green() // 2, sc.blue() // 2))
                return QBrush(sc)
            if col == COL_INB:
                if cat.parent_a is not None and cat.parent_b is not None:
                    score = len(find_common_ancestors(cat.parent_a, cat.parent_b))
                    if score >= 3:
                        return QBrush(QColor(80, 20, 20))
                    if score >= 1:
                        return QBrush(QColor(70, 55, 10))
            if compat == 'incompatible':
                return QBrush(QColor(18, 12, 14))
            if compat == 'risky':
                return QBrush(QColor(22, 18, 10))

        elif role == Qt.ForegroundRole:
            compat = (
                _compatibility(self._focus_cat, cat)
                if self._focus_cat is not None and cat is not self._focus_cat
                else None
            )
            # Suppress risky highlight when lineage features are off
            if compat == 'risky' and not self._show_lineage:
                compat = 'ok'
            if compat == 'incompatible':
                return QBrush(QColor(65, 55, 60))
            if compat == 'risky':
                return QBrush(QColor(130, 110, 60))
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
            if col in STAT_COLS or col in (COL_GEN, COL_STAT, COL_SUM, COL_AGE):
                return Qt.AlignCenter

        return None

    def cat_at(self, row: int) -> Optional[Cat]:
        return self._cats[row] if 0 <= row < len(self._cats) else None


class RoomFilterModel(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self._room = None
        self._name_filter = ""
        self.setSortRole(Qt.UserRole)

    def set_room(self, key):
        self._room = key
        self.invalidate()

    def set_name_filter(self, text: str):
        self._name_filter = text.strip().lower()
        self.invalidate()

    def filterAcceptsRow(self, source_row, source_parent):
        cat = self.sourceModel().cat_at(source_row)
        if cat is None:
            return False
        if self._name_filter and self._name_filter not in cat.name.lower():
            return False
        if self._room == "__all__":
            return True
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


def _chip(text: str, tooltip: str = "") -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(_CHIP_STYLE)
    if tooltip:
        lbl.setToolTip(tooltip)
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
    def __init__(self, items: list[str], tooltip_fn=None):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(5)
        for item in items:
            tip = tooltip_fn(item) if tooltip_fn else ""
            row.addWidget(_chip(item, tip))
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
        self._show_lineage: bool = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 10, 14, 10)
        outer.setSpacing(0)
        self._content = QWidget()
        outer.addWidget(self._content)

    def set_show_lineage(self, show: bool):
        self._show_lineage = show

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

        # Stats: show all 7; highlight any that are modified
        has_mods = any(cat.total_stats[n] != cat.base_stats[n] for n in STAT_NAMES)
        if has_mods:
            id_col.addSpacing(4)
            stat_row = QHBoxLayout(); stat_row.setSpacing(6)
            for n in STAT_NAMES:
                base = cat.base_stats[n]
                total = cat.total_stats[n]
                if total != base:
                    delta = total - base
                    sign  = "+" if delta > 0 else ""
                    text  = f"{n} {base}{sign}{delta}"
                    col_s = "#5a9" if delta > 0 else "#c55"
                else:
                    text  = f"{n} {base}"
                    col_s = "#555"
                lbl = QLabel(text)
                lbl.setStyleSheet(f"color:{col_s}; font-size:10px;")
                stat_row.addWidget(lbl)
            stat_row.addStretch()
            id_col.addLayout(stat_row)

        def _navigate(target: Cat):
            mw = self.window()
            # Use "All Cats" view so gone/adventure cats are always reachable
            mw._filter("__all__", mw._btn_everyone)
            for row in range(mw._source_model.rowCount()):
                if mw._source_model.cat_at(row) is target:
                    proxy_idx = mw._proxy_model.mapFromSource(
                        mw._source_model.index(row, 0))
                    if proxy_idx.isValid():
                        mw._table.selectionModel().setCurrentIndex(
                            proxy_idx,
                            QItemSelectionModel.SelectionFlag.ClearAndSelect |
                            QItemSelectionModel.SelectionFlag.Rows)
                        mw._table.scrollTo(proxy_idx)
                    break

        if self._show_lineage:
            tree_btn = QPushButton("Family Tree…")
            tree_btn.setStyleSheet(
                "QPushButton { color:#5a8aaa; background:transparent; border:1px solid #252545;"
                " padding:3px 8px; border-radius:4px; font-size:10px; }"
                "QPushButton:hover { background:#131328; }")
            tree_btn.clicked.connect(lambda: LineageDialog(cat, self, navigate_fn=_navigate).exec())
            id_col.addWidget(tree_btn)
        id_col.addStretch()
        root.addLayout(id_col)

        # Abilities
        if cat.abilities:
            root.addWidget(_vsep())
            ab = QVBoxLayout(); ab.setSpacing(4)
            ab.addWidget(_sec("ABILITIES"))
            ab.addWidget(ChipRow(cat.abilities, tooltip_fn=_ability_tip))
            ab.addStretch()
            root.addLayout(ab)

        # Mutations
        if cat.mutations:
            root.addWidget(_vsep())
            mu = QVBoxLayout(); mu.setSpacing(4)
            mu.addWidget(_sec("MUTATIONS"))
            mu.addWidget(ChipRow(cat.mutations, tooltip_fn=_ability_tip))
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

        # Lovers & haters
        if cat.lovers or cat.haters:
            root.addWidget(_vsep())
            rel = QVBoxLayout(); rel.setSpacing(4)
            if cat.lovers:
                rel.addWidget(_sec("LOVERS"))
                rel.addWidget(ChipRow([c.name for c in cat.lovers]))
            if cat.haters:
                rel.addWidget(_sec("HATERS"))
                hl = ChipRow([c.name for c in cat.haters])
                for i in range(hl.layout().count() - 1):  # tint hater chips red
                    w = hl.layout().itemAt(i).widget()
                    if w:
                        w.setStyleSheet(w.styleSheet().replace("background:#252545", "background:#452020"))
                rel.addWidget(hl)
            rel.addStretch()
            root.addLayout(rel)

        root.addStretch()

    # ── Breeding pair ──────────────────────────────────────────────────────

    def _build_pair(self, a: Cat, b: Cat):
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
        sum_col = len(STAT_NAMES) + 1
        sh = QLabel("Sum")
        sh.setStyleSheet("color:#455; font-size:9px; font-weight:bold;")
        sh.setAlignment(Qt.AlignCenter)
        grid.addWidget(sh, 0, sum_col)

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

            # Sum cell
            if is_cat:
                sv = sum(cat.base_stats.values())
                sc = QLabel(str(sv))
                sc.setStyleSheet("color:#aaa; font-size:11px; font-weight:bold;")
            else:
                lo_s = sum(min(a.base_stats[st], b.base_stats[st]) for st in STAT_NAMES)
                hi_s = sum(max(a.base_stats[st], b.base_stats[st]) for st in STAT_NAMES)
                sc = QLabel(f"{lo_s}–{hi_s}" if lo_s != hi_s else str(lo_s))
                sc.setStyleSheet("color:#777; font-size:11px; font-weight:bold;")
            sc.setAlignment(Qt.AlignCenter)
            grid.addWidget(sc, row_num, sum_col)

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
                    row.addWidget(_chip(ab, _ability_tip(ab)))
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
                        mrow.addWidget(_chip(mut, _ability_tip(mut)))
                    mrow.addStretch()
                    mc.addLayout(mrow)
            mc.addStretch()
            bot.addLayout(mc)
            bot.addWidget(_vsep())

        if self._show_lineage:
            lc = QVBoxLayout()
            lc.setSpacing(3)
            lc.addWidget(_sec("LINEAGE"))
            common    = find_common_ancestors(a, b)
            is_direct = (a in get_parents(b) or b in get_parents(a))
            is_haters = (b in getattr(a, 'haters', []) or a in getattr(b, 'haters', []))

            if is_haters:
                lc.addWidget(QLabel("⚠  These cats hate each other", styleSheet=_WARN_STYLE))
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


# ── Lineage tree dialog ───────────────────────────────────────────────────────

class LineageDialog(QDialog):
    """
    Family tree dialog — generations from oldest (top) to newest (bottom).
    Layout:  Grandparents → Parents → Self → Children → Grandchildren
    """

    def __init__(self, cat: 'Cat', parent=None, navigate_fn=None):
        super().__init__(parent)
        self.setWindowTitle(f"Family Tree — {cat.name}")
        self.setMinimumSize(700, 400)
        self.setStyleSheet(
            "QDialog { background:#0a0a18; }"
            "QScrollArea { border:none; background:#0a0a18; }"
            "QPushButton { background:#1e1e38; color:#ccc; border:1px solid #2a2a4a;"
            " padding:5px 14px; border-radius:4px; font-size:11px; }"
            "QPushButton:hover { background:#252555; }"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 16, 20, 14)
        outer.setSpacing(12)

        # ── Reusable box builder ─────────────────────────────────────────
        def cat_box(cat_obj, highlight=False, dim=False):
            if cat_obj is None:
                btn = QPushButton("Unknown")
                btn.setEnabled(False)
                btn.setStyleSheet(
                    "QPushButton { color:#252535; font-size:10px; padding:6px 10px;"
                    " background:#0d0d1c; border:1px solid #141424; border-radius:5px; }")
            else:
                line2 = cat_obj.gender_display
                if cat_obj.room_display:
                    line2 += f"  {cat_obj.room_display}"
                bg     = "#1a2840" if highlight else ("#0e0e1a" if dim else "#121222")
                border = "#3060a0" if highlight else ("#1a1a28" if dim else "#222238")
                col    = "#ddd"    if not dim    else "#333"
                can_nav = navigate_fn is not None and cat_obj is not cat
                hover  = "#1d3560" if can_nav else bg
                btn = QPushButton(f"{cat_obj.name}\n{line2}")
                btn.setStyleSheet(
                    f"QPushButton {{ color:{col}; font-size:10px; padding:6px 10px;"
                    f" background:{bg}; border:1px solid {border}; border-radius:5px;"
                    f" text-align:center; }}"
                    f"QPushButton:hover {{ background:{hover}; }}")
                if can_nav:
                    btn.setCursor(Qt.CursorShape.PointingHandCursor)
                    btn.clicked.connect(
                        lambda checked=False, c=cat_obj: (self.accept(), navigate_fn(c)))
            btn.setMinimumWidth(100)
            btn.setMaximumWidth(200)
            return btn

        # ── Generation label ─────────────────────────────────────────────
        def gen_row_label(text):
            lbl = QLabel(text)
            lbl.setStyleSheet(
                "color:#333; font-size:9px; font-weight:bold; letter-spacing:1px;"
                " min-width:90px;")
            lbl.setAlignment(Qt.AlignVCenter | Qt.AlignRight)
            return lbl

        def make_gen_row(label_text, cat_list, highlight_all=False, dim_all=False):
            row = QHBoxLayout()
            row.setSpacing(8)
            row.addWidget(gen_row_label(label_text))
            for c in cat_list:
                row.addWidget(cat_box(c, highlight=highlight_all,
                                      dim=(dim_all and c is not None)))
            row.addStretch()
            outer.addLayout(row)

        # ── Build generations ────────────────────────────────────────────
        pa, pb = cat.parent_a, cat.parent_b
        gp_a1 = pa.parent_a if pa else None
        gp_a2 = pa.parent_b if pa else None
        gp_b1 = pb.parent_a if pb else None
        gp_b2 = pb.parent_b if pb else None

        grandparents = [gp_a1, gp_a2, gp_b1, gp_b2]
        parents      = [pa, pb]

        children = list(cat.children)
        grandchildren: list = []
        for child in children:
            grandchildren.extend(child.children)

        make_gen_row("GRANDPARENTS", grandparents)
        make_gen_row("PARENTS",      parents)
        make_gen_row("",             [cat], highlight_all=True)
        if children:
            make_gen_row("CHILDREN", children[:8])
            if len(children) > 8:
                outer.addWidget(
                    QLabel(f"  … and {len(children)-8} more children",
                           styleSheet="color:#444; font-size:10px; padding-left:100px;"))
        if grandchildren:
            unique_gc = list({id(g): g for g in grandchildren}.values())
            make_gen_row("GRANDCHILDREN", unique_gc[:8])
            if len(unique_gc) > 8:
                outer.addWidget(
                    QLabel(f"  … and {len(unique_gc)-8} more grandchildren",
                           styleSheet="color:#444; font-size:10px; padding-left:100px;"))

        outer.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        outer.addWidget(close_btn, alignment=Qt.AlignRight)


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
        self._show_lineage: bool = False

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

        sm = self.menuBar().addMenu("Settings")
        self._lineage_action = QAction("Show Family Tree && Inbreeding", self)
        self._lineage_action.setCheckable(True)
        self._lineage_action.setChecked(False)
        self._lineage_action.triggered.connect(self._toggle_lineage)
        sm.addAction(self._lineage_action)

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
        self._btn_everyone = _sidebar_btn("All Cats")
        self._btn_everyone.clicked.connect(
            lambda: self._filter("__all__", self._btn_everyone))
        vb.addWidget(self._btn_everyone)
        self._room_btns["__all__"] = self._btn_everyone

        self._btn_all = _sidebar_btn("Alive")
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
        _ROOM_ORDER = {
            "Attic": 0, "Attic_Large": 0,
            "Floor2_Large": 1, "Floor2_Small": 2,
            "Floor1_Large": 3, "Floor1_Small": 4,
            "Basement": 5, "Basement_Large": 5,
        }
        rooms = sorted(
            {c.room for c in cats if c.status == "In House" and c.room},
            key=lambda r: _ROOM_ORDER.get(r, 99),
        )
        for room in rooms:
            count = sum(1 for c in cats if c.room == room)
            display = ROOM_DISPLAY.get(room, room)
            btn = _sidebar_btn(f"{display}  ({count})")
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
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search…")
        self._search.setClearButtonEnabled(True)
        self._search.setFixedWidth(180)
        self._search.setStyleSheet(
            "QLineEdit { background:#0d0d1c; color:#ccc; border:1px solid #2a2a4a;"
            " border-radius:4px; padding:3px 8px; font-size:12px; }"
            "QLineEdit:focus { border-color:#3a3a7a; }")
        hb.addWidget(self._header_lbl)
        hb.addWidget(self._count_lbl)
        hb.addStretch()
        hb.addWidget(self._search)
        hb.addSpacing(12)
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
        hh.setStretchLastSection(False)  # we control stretch manually

        # Name: interactive so the user can resize it; not Stretch so it
        # doesn't eat the blank space that should sit at the right edge.
        hh.setSectionResizeMode(COL_NAME, QHeaderView.Interactive)
        self._table.setColumnWidth(COL_NAME, 130)

        # Room: size to content so it adapts to room name length
        hh.setSectionResizeMode(COL_ROOM, QHeaderView.ResizeToContents)

        # Narrow fixed columns (gender, status, stats, sum)
        for col, width in [(COL_GEN, _W_GEN), (COL_STAT, _W_STATUS),
                           (COL_SUM, 38)] + [(c, _W_STAT) for c in STAT_COLS]:
            hh.setSectionResizeMode(col, QHeaderView.Fixed)
            self._table.setColumnWidth(col, width)

        # Abilities: interactive — user drags to taste
        hh.setSectionResizeMode(COL_ABIL, QHeaderView.Interactive)
        self._table.setColumnWidth(COL_ABIL, 180)

        # Mutations: interactive
        hh.setSectionResizeMode(COL_MUTS, QHeaderView.Interactive)
        self._table.setColumnWidth(COL_MUTS, 155)

        # Generation depth: fixed narrow, hidden by default (behind lineage toggle)
        hh.setSectionResizeMode(COL_AGE, QHeaderView.Fixed)
        self._table.setColumnWidth(COL_AGE, 34)
        self._table.setColumnHidden(COL_AGE, True)

        # Source: Stretch — absorbs blank space, hidden by default (behind lineage toggle)
        hh.setSectionResizeMode(COL_SRC, QHeaderView.Stretch)
        self._table.setColumnHidden(COL_SRC, True)

        # Inbreeding score: fixed narrow, hidden by default
        hh.setSectionResizeMode(COL_INB, QHeaderView.Fixed)
        self._table.setColumnWidth(COL_INB, 38)
        self._table.setColumnHidden(COL_INB, True)

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
        self._search.textChanged.connect(self._proxy_model.set_name_filter)
        self._search.textChanged.connect(self._update_count)
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

        # Highlight compatibility: dim incompatible cats when 1 is selected
        focus = cats[0] if len(cats) == 1 else None
        self._source_model.set_focus_cat(focus)

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
        self._source_model.set_focus_cat(None)

    def _update_header(self, room_key):
        if room_key == "__all__":
            self._header_lbl.setText("All Cats")
        elif room_key is None:
            self._header_lbl.setText("Alive")
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
            # Update fixed sidebar button counts
            total = len(cats)
            alive = sum(1 for c in cats if c.status != "Gone")
            adv   = sum(1 for c in cats if c.status == "Adventure")
            gone  = sum(1 for c in cats if c.status == "Gone")
            self._btn_everyone.setText(f"All Cats  ({total})")
            self._btn_all.setText(f"Alive  ({alive})")
            self._btn_adventure.setText(f"On Adventure  ({adv})")
            self._btn_gone.setText(f"Gone  ({gone})")
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

    def _toggle_lineage(self, checked: bool):
        self._show_lineage = checked
        for col in (COL_AGE, COL_SRC, COL_INB):
            self._table.setColumnHidden(col, not checked)
        self._source_model.set_show_lineage(checked)
        self._detail.set_show_lineage(checked)
        self._on_selection()   # refresh detail panel with updated flag

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
