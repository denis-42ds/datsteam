import os
import time
import logging
import requests
from collections import deque
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)

load_dotenv()

TOKEN = os.getenv('AUTH_TOKEN')
BASE_URL = os.getenv('PROD_URL')
HEADERS = {"X-Auth-Token": TOKEN}

if not TOKEN:
    logging.error("Token not found! Make sure your .env file contains AUTH_TOKEN")
    exit(1)

# ═══════════════════════════════════════════════════════════════════════
#  CONSTANTS & TUNING
# ═══════════════════════════════════════════════════════════════════════

DEFAULT_AR  = 2
DEFAULT_SR  = 3
DEFAULT_CS  = 5    # construction speed HP/turn
DEFAULT_MHP = 50   # build completes at 50 HP → 50/CS = 10 turns

# [PATCH v2] Cell degradation: 80 → 40 → 20 turns.
#   Faster cell recycling = more free spawn spots, smaller map pressure.
CELL_DECAY_TURNS = 20

# [PATCH v2] Beaver kill value: 10x → 20x cell value.
#   Regular lair = 20 * 1000 = 20 000 pts (vs ~50 pts/turn/cell terraforming).
#   Killing one lair ≈ 400 turns of terraforming on a single cell.
BEAVER_MULTIPLIER = 20

# ── ЦУ survival thresholds ──────────────────────────────────────────────
# ЦУ cell completes at 100% (TS=5 → 20 turns from 0%).
# Build needs DEFAULT_MHP/DEFAULT_CS = 10 turns.
# START building neighbor when ~12 turns remain → 40% cell progress.
# RELOCATE when ≤5 turns remain → 75% cell progress.
CU_BUILD_NEIGHBOR_PCT = 40   # build escape neighbor at this ЦУ cell progress %
CU_RELOCATE_PCT       = 75   # relocate ЦУ when cell reaches this %
CU_PANIC_HP           = 15   # also relocate if ЦУ HP falls this low

# General repair thresholds
REPAIR_HP_THRESHOLD = 35
URGENT_HP_THRESHOLD = 15

# [PATCH v2] Upgrade order revised:
# - Victory criterion 2 is now "fewer ЦУ lost" → repair_power helps (faster builds =
#   faster escape routes, fewer ЦУ deaths).
# - Beaver 20x → beaver_damage_mitigation worth more.
UPGRADE_PRIORITY = [
    "settlement_limit",          # +1 plantation slot (max +10)
    "repair_power",              # faster build/repair → fewer ЦУ deaths
    "decay_mitigation",          # slow isolated plantation degradation
    "beaver_damage_mitigation",  # [PATCH] we'll be near beavers a lot now
    "max_hp",                    # bigger HP buffer
    "earthquake_mitigation",     # reduce earthquake damage
    "signal_range",              # longer relay chains
    "vision_range",              # spot more beavers
]

# ═══════════════════════════════════════════════════════════════════════
#  API HELPERS
# ═══════════════════════════════════════════════════════════════════════

def get_arena() -> dict:
    r = requests.get(f"{BASE_URL}/api/arena", headers=HEADERS, timeout=5)
    r.raise_for_status()
    return r.json()


def send_command(command: list = None, upgrade: str = None, relocate: list = None):
    body = {}
    if command:
        body["command"] = command
    if upgrade:
        body["plantationUpgrade"] = upgrade
    if relocate:
        body["relocateMain"] = relocate
    if not body:
        return None
    r = requests.post(f"{BASE_URL}/api/command", headers=HEADERS, json=body, timeout=5)
    r.raise_for_status()
    return r.json()

# ═══════════════════════════════════════════════════════════════════════
#  GEOMETRY HELPERS
# ═══════════════════════════════════════════════════════════════════════

def chebyshev(a: tuple, b: tuple) -> int:
    """Square-zone Chebyshev metric used for AR / SR / VR."""
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def is_enhanced(x: int, y: int) -> bool:
    return x % 7 == 0 and y % 7 == 0


def cell_value(x: int, y: int) -> int:
    return 1500 if is_enhanced(x, y) else 1000


def neighbors4(x, y):
    return [(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)]

# ═══════════════════════════════════════════════════════════════════════
#  CONNECTIVITY
# ═══════════════════════════════════════════════════════════════════════

def bfs_connected(plantation_positions: set, main_pos: tuple) -> set:
    if main_pos not in plantation_positions:
        return set()
    visited = {main_pos}
    queue = deque([main_pos])
    while queue:
        pos = queue.popleft()
        for nb in neighbors4(*pos):
            if nb in plantation_positions and nb not in visited:
                visited.add(nb)
                queue.append(nb)
    return visited


def is_articulation_point(connected: set, node: tuple) -> bool:
    """Would removing this node disconnect any other node from ЦУ?"""
    remaining = connected - {node}
    if not remaining:
        return False
    start = next(iter(remaining))
    visited = {start}
    queue = deque([start])
    while queue:
        pos = queue.popleft()
        for nb in neighbors4(*pos):
            if nb in remaining and nb not in visited:
                visited.add(nb)
                queue.append(nb)
    return len(visited) < len(remaining)

# ═══════════════════════════════════════════════════════════════════════
#  UPGRADE SELECTOR
# ═══════════════════════════════════════════════════════════════════════

def choose_upgrade(upgrades: dict) -> str | None:
    if not upgrades or upgrades.get("points", 0) == 0:
        return None
    tiers = {t["name"]: t for t in upgrades.get("tiers", [])}
    for name in UPGRADE_PRIORITY:
        t = tiers.get(name)
        if t and t["current"] < t["max"]:
            logging.info(f"  ↑ Upgrade: {name} ({t['current']}/{t['max']})")
            return name
    return None

# ═══════════════════════════════════════════════════════════════════════
#  COMMAND BUILDER
#  - One command per author plantation per turn.
#  - Tracks output-point relay usage (each extra command through same relay -1 eff).
# ═══════════════════════════════════════════════════════════════════════

class CommandBuilder:
    def __init__(self, connected: set, ar: int = DEFAULT_AR, sr: int = DEFAULT_SR):
        self.connected = connected
        self.ar = ar
        self.sr = sr
        self.assigned_authors: set = set()
        self.output_usage: dict = {}
        self.commands: list = []

    def _find_output(self, author: tuple, target: tuple) -> tuple | None:
        # Direct (author IS output point)
        if chebyshev(author, target) <= self.ar:
            return author
        # Relay through connected plantation within SR
        best, best_eff = None, -1
        for relay in self.connected:
            if chebyshev(author, relay) > self.sr:
                continue
            if chebyshev(relay, target) > self.ar:
                continue
            eff = max(0, 5 - self.output_usage.get(relay, 0))
            if eff > best_eff:
                best_eff, best = eff, relay
        return best

    def try_assign(self, author: tuple, target: tuple, label: str = "") -> bool:
        if author in self.assigned_authors:
            return False
        output = self._find_output(author, target)
        if output is None:
            return False
        eff = max(0, 5 - self.output_usage.get(output, 0))
        if eff <= 0:
            return False
        self.commands.append({"path": [list(author), list(output), list(target)]})
        self.output_usage[output] = self.output_usage.get(output, 0) + 1
        self.assigned_authors.add(author)
        logging.debug(f"    [{label}] {author}→{output}→{target}")
        return True

# ═══════════════════════════════════════════════════════════════════════
#  FRONTIER SCORING
#  [PATCH] Beavers are 20x now → heavy bonus for cells near lairs.
# ═══════════════════════════════════════════════════════════════════════

def score_build_target(
    x: int, y: int, connected: set, beaver_positions: list
) -> float:
    base = cell_value(x, y)

    # Bonus for each enhanced cell within 3 Chebyshev radius
    enhanced_nearby = sum(
        200
        for ex in range(-3, 4)
        for ey in range(-3, 4)
        if is_enhanced(x + ex, y + ey)
    )

    # [PATCH] Proximity bonus toward beaver lairs (now 20x per lair)
    beaver_bonus = 0
    for bpos in beaver_positions:
        dist = chebyshev((x, y), bpos)
        if dist <= DEFAULT_AR + 2:  # within striking range after one more build
            bv = cell_value(*bpos) * BEAVER_MULTIPLIER
            beaver_bonus = max(beaver_bonus, bv // max(1, dist))

    # Prefer cells adjacent to current chain (no relay required)
    adjacency_bonus = 150 if any(chebyshev((x, y), c) == 1 for c in connected) else 0

    return base + enhanced_nearby + beaver_bonus + adjacency_bonus


def get_frontier(
    connected: set,
    occupied: set,
    mountains: set,
    ar: int,
    map_w: int,
    map_h: int,
    beaver_positions: list,
) -> list:
    candidates = {}
    for cpos in connected:
        cx, cy = cpos
        for dx in range(-ar, ar + 1):
            for dy in range(-ar, ar + 1):
                pos = (cx + dx, cy + dy)
                if pos in occupied or pos in mountains:
                    continue
                nx, ny = pos
                if not (0 <= nx < map_w and 0 <= ny < map_h):
                    continue
                if pos not in candidates:
                    candidates[pos] = score_build_target(
                        nx, ny, connected, beaver_positions
                    )
    return sorted(candidates.items(), key=lambda kv: -kv[1])

# ═══════════════════════════════════════════════════════════════════════
#  MAIN LOOP
# ═══════════════════════════════════════════════════════════════════════

def main_loop():
    last_turn  = -1
    sr_current = DEFAULT_SR
    cs_current = DEFAULT_CS

    logging.info("DatsSol bot v2 started.")

    while True:
        try:
            state = get_arena()
            turn        = state.get("turnNo", 0)
            next_turn_in = state.get("nextTurnIn", 1.0)

            if turn == last_turn:
                time.sleep(0.1)
                continue
            last_turn = turn

            # ── Parse state ───────────────────────────────────────────────
            plantations   = state.get("plantations", [])
            enemies       = state.get("enemy", [])
            mountains     = set(tuple(m) for m in state.get("mountains", []))
            constructions = state.get("construction", [])
            beavers       = state.get("beavers", [])
            cells         = {tuple(c["position"]): c for c in state.get("cells", [])}
            upgrades      = state.get("plantationUpgrades", {})
            ar            = state.get("actionRange", DEFAULT_AR)
            map_w, map_h  = state.get("size", [400, 400])

            # Update SR and CS from current upgrade tiers
            tiers_map  = {t["name"]: t for t in upgrades.get("tiers", [])}
            sr_current = DEFAULT_SR + tiers_map.get("signal_range", {}).get("current", 0)
            cs_bonus   = tiers_map.get("repair_power", {}).get("current", 0)
            cs_current = DEFAULT_CS + cs_bonus
            # Turns needed to finish a new plantation from scratch at current CS
            build_turns_needed = max(1, DEFAULT_MHP // cs_current)

            if not plantations:
                logging.warning("No plantations — waiting for respawn…")
                time.sleep(0.4)
                continue

            main_p = next((p for p in plantations if p.get("isMain")), None)
            if not main_p:
                logging.warning("ЦУ not found!")
                time.sleep(0.4)
                continue

            main_pos = tuple(main_p["position"])

            # Position sets
            p_map     = {tuple(p["position"]): p for p in plantations}
            c_pos_set = {tuple(c["position"]) for c in constructions}
            # [BUG FIX] Enemy positions MUST be in occupied to avoid wasted build commands
            enemy_pos_set = {tuple(e["position"]) for e in enemies}
            occupied  = set(p_map.keys()) | c_pos_set | enemy_pos_set

            # Connectivity
            connected    = bfs_connected(set(p_map.keys()), main_pos)
            iso_count    = len(plantations) - len(connected)
            beaver_positions = [tuple(b["position"]) for b in beavers]

            logging.info(
                f"══ T{turn:4d} | {len(plantations)} plants "
                f"({len(connected)} conn, {iso_count} iso) | "
                f"beavers={len(beavers)} | next {next_turn_in:.2f}s ══"
            )

            # ── ЦУ cell terraformation progress ───────────────────────────
            cu_cell_prog   = cells.get(main_pos, {}).get("terraformationProgress", 0)
            turns_until_cu = max(0, (100 - cu_cell_prog) // max(1, 5))

            if cu_cell_prog >= CU_BUILD_NEIGHBOR_PCT:
                logging.warning(
                    f"  ⚠ ЦУ cell {cu_cell_prog:.0f}% "
                    f"(~{turns_until_cu} turns, build needs {build_turns_needed})"
                )

            # ── Upgrade ───────────────────────────────────────────────────
            upgrade_to_buy = choose_upgrade(upgrades)

            # ── Command builder ───────────────────────────────────────────
            cb = CommandBuilder(connected, ar=ar, sr=sr_current)

            connected_plants = [p for p in plantations if tuple(p["position"]) in connected]
            by_hp_asc  = sorted(connected_plants, key=lambda p: p["hp"])
            by_hp_desc = sorted(connected_plants, key=lambda p: -p["hp"])

            relocate_cmd = None

            # ══════════════════════════════════════════════════════════════
            # PRIORITY 0 — ЦУ SURVIVAL
            #
            # [BUG FIX] The main bug in v1: ЦУ kept spawning on high-progress
            # cells (e.g. 85%) from other players, then dying in 3 turns because
            # the 85% threshold left no time to finish a 10-turn build.
            #
            # Fix: start building escape neighbor at 40%, relocate at 75%.
            # ══════════════════════════════════════════════════════════════
            cu_must_escape = (
                cu_cell_prog >= CU_RELOCATE_PCT
                or main_p["hp"] <= CU_PANIC_HP
            )

            if cu_must_escape:
                # Option A: relocate ЦУ to adjacent connected plantation (instant)
                adj_connected = [
                    nb for nb in neighbors4(*main_pos)
                    if nb in connected and nb != main_pos
                ]
                if adj_connected:
                    best_nb = max(adj_connected, key=lambda nb: p_map[nb]["hp"])
                    relocate_cmd = [list(main_pos), list(best_nb)]
                    logging.warning(f"  🚨 RELOCATE ЦУ → {best_nb} (cell={cu_cell_prog:.0f}%)")

                else:
                    # Option B: no neighbor yet — emergency build all-in
                    logging.warning("  🚨 PANIC: no adjacent plantation for ЦУ relocation!")
                    for nb in neighbors4(*main_pos):
                        if nb in occupied or nb in mountains:
                            continue
                        nx, ny = nb
                        if not (0 <= nx < map_w and 0 <= ny < map_h):
                            continue
                        for builder in by_hp_desc:
                            bpos = tuple(builder["position"])
                            if cb.try_assign(bpos, nb, "panic-build"):
                                break
                        break

            elif cu_cell_prog >= CU_BUILD_NEIGHBOR_PCT:
                # Build escape neighbor before things get critical
                adj_free = [
                    nb for nb in neighbors4(*main_pos)
                    if nb not in occupied and nb not in mountains
                    and 0 <= nb[0] < map_w and 0 <= nb[1] < map_h
                ]
                if adj_free:
                    adj_free.sort(key=lambda pos: -cell_value(*pos))
                    for target in adj_free:
                        for builder in by_hp_desc:
                            bpos = tuple(builder["position"])
                            if bpos not in connected:
                                continue
                            if cb.try_assign(bpos, target, "escape-build"):
                                logging.warning(f"  🔑 Escape build → {target}")
                                break
                        break

            # ══════════════════════════════════════════════════════════════
            # PRIORITY 1 — Emergency repair of ЦУ
            # ══════════════════════════════════════════════════════════════
            if main_p["hp"] <= URGENT_HP_THRESHOLD:
                for healer in by_hp_desc:
                    hpos = tuple(healer["position"])
                    if hpos == main_pos:
                        continue
                    if cb.try_assign(hpos, main_pos, "repair-ЦУ"):
                        break

            # ══════════════════════════════════════════════════════════════
            # PRIORITY 2 — Repair bridge (articulation-point) plantations
            # ══════════════════════════════════════════════════════════════
            for target_p in by_hp_asc:
                tpos = tuple(target_p["position"])
                if target_p["hp"] >= REPAIR_HP_THRESHOLD:
                    break
                if tpos == main_pos:
                    continue
                if not is_articulation_point(connected, tpos):
                    continue
                for healer in by_hp_desc:
                    hpos = tuple(healer["position"])
                    if hpos == tpos:
                        continue
                    if cb.try_assign(hpos, tpos, "repair-bridge"):
                        break

            # ══════════════════════════════════════════════════════════════
            # PRIORITY 3 — Repair any other low-HP connected plantation
            # ══════════════════════════════════════════════════════════════
            for target_p in by_hp_asc:
                tpos = tuple(target_p["position"])
                if target_p["hp"] >= REPAIR_HP_THRESHOLD:
                    break
                if tpos == main_pos:
                    continue
                for healer in by_hp_desc:
                    hpos = tuple(healer["position"])
                    if hpos == tpos:
                        continue
                    if cb.try_assign(hpos, tpos, "repair"):
                        break

            # ══════════════════════════════════════════════════════════════
            # PRIORITY 4 — [PATCH] Attack beaver lairs (now 20x points)
            #   Killing one lair > 400 turns of single-cell terraforming.
            #   Focus multiple plantations on the lowest-HP lair to finish fast.
            # ══════════════════════════════════════════════════════════════
            for beaver in sorted(beavers, key=lambda b: b.get("hp", 100)):
                bvpos = tuple(beaver["position"])
                bv_pts = cell_value(*bvpos) * BEAVER_MULTIPLIER
                # Try to assign multiple attackers (diminishing returns via relay)
                assigned_any = False
                for attacker in by_hp_desc:
                    apos = tuple(attacker["position"])
                    if apos not in connected:
                        continue
                    if cb.try_assign(apos, bvpos, f"beaver(hp={beaver.get('hp')})"):
                        if not assigned_any:
                            logging.info(f"  🦫 Attack beaver {bvpos} hp={beaver.get('hp')} ({bv_pts}pts)")
                        assigned_any = True

            # ══════════════════════════════════════════════════════════════
            # PRIORITY 5 — Preemptive rebuild of nearly-complete own cells
            #   [BUG FIX] Only fires for OWN plantation cells (ppos in connected)
            #   and NOT for ЦУ cell (handled separately in P0).
            #   Threshold: turns_left ≤ build_turns_needed + 2.
            # ══════════════════════════════════════════════════════════════
            for ppos, cell_data in cells.items():
                if ppos not in connected or ppos == main_pos:
                    continue
                prog       = cell_data.get("terraformationProgress", 0)
                turns_left = max(0, (100 - prog) // 5)
                if turns_left > build_turns_needed + 2:
                    continue  # still plenty of time

                for nb in neighbors4(*ppos):
                    if nb in occupied or nb in mountains:
                        continue
                    nx, ny = nb
                    if not (0 <= nx < map_w and 0 <= ny < map_h):
                        continue
                    builders_sorted = sorted(
                        connected_plants,
                        key=lambda p: chebyshev(tuple(p["position"]), nb),
                    )
                    for builder in builders_sorted:
                        bpos = tuple(builder["position"])
                        if cb.try_assign(bpos, nb, f"preempt({prog:.0f}%)"):
                            logging.info(
                                f"  ⟳ Preemptive build {ppos}→{nb} "
                                f"({prog:.0f}%, ~{turns_left} turns)"
                            )
                            break
                    break  # one replacement per expiring cell per turn

            # ══════════════════════════════════════════════════════════════
            # PRIORITY 6 — Expand: build toward enhanced cells & beavers
            # ══════════════════════════════════════════════════════════════
            frontier = get_frontier(
                connected, occupied, mountains, ar, map_w, map_h, beaver_positions
            )
            for target_pos, score in frontier:
                builders_sorted = sorted(
                    [p for p in plantations if tuple(p["position"]) in connected],
                    key=lambda p: chebyshev(tuple(p["position"]), target_pos),
                )
                for builder in builders_sorted:
                    bpos = tuple(builder["position"])
                    if cb.try_assign(bpos, target_pos, f"expand({score:.0f})"):
                        break

            # ══════════════════════════════════════════════════════════════
            # PRIORITY 7 — Opportunistic sabotage of low-HP enemies
            # ══════════════════════════════════════════════════════════════
            for enemy_p in sorted(enemies, key=lambda e: e.get("hp", 999)):
                ehp  = enemy_p.get("hp", 999)
                epos = tuple(enemy_p["position"])
                if ehp > 25:
                    continue
                for attacker in by_hp_desc:
                    apos = tuple(attacker["position"])
                    if apos not in connected:
                        continue
                    if cb.try_assign(apos, epos, f"sabotage(hp={ehp})"):
                        logging.info(f"  ⚔ Sabotage {epos} hp={ehp}")
                        break

            # ══════════════════════════════════════════════════════════════
            # SEND
            # ══════════════════════════════════════════════════════════════
            cmds = cb.commands
            if not cmds and not upgrade_to_buy and not relocate_cmd:
                logging.warning("  No actions this turn — nothing to send.")
                time.sleep(max(0.05, next_turn_in - 0.2))
                continue

            result = send_command(
                command=cmds if cmds else None,
                upgrade=upgrade_to_buy,
                relocate=relocate_cmd,
            )

            if result:
                errors = [
                    e for e in result.get("errors", [])
                    if "empty command" not in e
                ]
                if errors:
                    logging.warning(f"  Server errors: {errors}")
                else:
                    rel_str = (
                        "→".join(str(tuple(x)) for x in relocate_cmd)
                        if relocate_cmd else "—"
                    )
                    logging.info(
                        f"  ✔ {len(cmds)} cmds | up={upgrade_to_buy or '—'} | rel={rel_str}"
                    )

            time.sleep(max(0.05, next_turn_in - 0.25))

        except requests.exceptions.HTTPError as e:
            logging.error(f"HTTP {e.response.status_code}: {e.response.text[:200]}")
            time.sleep(1)
        except requests.exceptions.RequestException as e:
            logging.error(f"Network error: {e}")
            time.sleep(1)
        except Exception as e:
            logging.error(f"Unexpected error: {e}", exc_info=True)
            time.sleep(0.5)


if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        logging.info("Bot stopped by user.")
    except Exception as e:
        logging.error(f"Critical error: {e}", exc_info=True)
