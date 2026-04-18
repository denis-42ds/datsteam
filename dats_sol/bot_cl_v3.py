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
    logging.error("Token not found! Make sure AUTH_TOKEN is in your .env file")
    exit(1)

# ═══════════════════════════════════════════════════════════════════════
#  CONSTANTS
# ═══════════════════════════════════════════════════════════════════════

DEFAULT_AR  = 2
DEFAULT_SR  = 3
DEFAULT_CS  = 5
DEFAULT_MHP = 50   # build completes when construction HP reaches 50

BEAVER_MULTIPLIER = 20   # [PATCH] was 10x, now 20x

# ЦУ survival thresholds
#   TS=5 → 20 turns to complete a fresh cell.
#   Default build takes 50/5 = 10 turns. With repair_power upgrades it's fewer.
#   We need to START the escape build early enough that it finishes before 100%.
CU_BUILD_NEIGHBOR_PCT = 35   # start building adjacent escape route at this %
CU_RELOCATE_PCT       = 75   # relocateMain as soon as adjacent plantation exists
CU_PANIC_HP           = 15   # also relocate if HP drops this low

# Repair thresholds
REPAIR_HP_THRESHOLD = 35
URGENT_HP_THRESHOLD = 15

# Upgrade order
UPGRADE_PRIORITY = [
    "settlement_limit",
    "repair_power",
    "decay_mitigation",
    "beaver_damage_mitigation",
    "max_hp",
    "earthquake_mitigation",
    "signal_range",
    "vision_range",
]

# ═══════════════════════════════════════════════════════════════════════
#  API
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
#  GEOMETRY
# ═══════════════════════════════════════════════════════════════════════

def chebyshev(a, b) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def is_enhanced(x, y) -> bool:
    return x % 7 == 0 and y % 7 == 0


def cell_value(x, y) -> int:
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
    q = deque([main_pos])
    while q:
        pos = q.popleft()
        for nb in neighbors4(*pos):
            if nb in plantation_positions and nb not in visited:
                visited.add(nb)
                q.append(nb)
    return visited


def is_articulation_point(connected: set, node: tuple) -> bool:
    remaining = connected - {node}
    if not remaining:
        return False
    start = next(iter(remaining))
    visited = {start}
    q = deque([start])
    while q:
        pos = q.popleft()
        for nb in neighbors4(*pos):
            if nb in remaining and nb not in visited:
                visited.add(nb)
                q.append(nb)
    return len(visited) < len(remaining)

# ═══════════════════════════════════════════════════════════════════════
#  UPGRADES
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
# ═══════════════════════════════════════════════════════════════════════

class CommandBuilder:
    def __init__(self, connected: set, ar: int, sr: int):
        self.connected = connected
        self.ar = ar
        self.sr = sr
        self.assigned: set = set()
        self.relay_usage: dict = {}
        self.commands: list = []

    def _find_output(self, author: tuple, target: tuple):
        if chebyshev(author, target) <= self.ar:
            return author
        best, best_eff = None, -1
        for relay in self.connected:
            if chebyshev(author, relay) > self.sr:
                continue
            if chebyshev(relay, target) > self.ar:
                continue
            eff = max(0, 5 - self.relay_usage.get(relay, 0))
            if eff > best_eff:
                best_eff, best = eff, relay
        return best

    def try_assign(self, author: tuple, target: tuple, label: str = "") -> bool:
        if author in self.assigned:
            return False
        output = self._find_output(author, target)
        if output is None:
            return False
        if max(0, 5 - self.relay_usage.get(output, 0)) <= 0:
            return False
        self.commands.append({"path": [list(author), list(output), list(target)]})
        self.relay_usage[output] = self.relay_usage.get(output, 0) + 1
        self.assigned.add(author)
        logging.debug(f"    [{label}] {author}→{output}→{target}")
        return True

# ═══════════════════════════════════════════════════════════════════════
#  BUILD-TARGET HELPERS
#
#  Core insight: constructions already started by the server keep their
#  progress between turns. We MUST continue building at the same cell
#  until it completes, not switch to a new target every turn.
# ═══════════════════════════════════════════════════════════════════════

def constructions_near(pos: tuple, construction_map: dict, radius: int) -> list:
    """Return in-progress constructions within Chebyshev radius, sorted by progress desc."""
    result = []
    for cpos, prog in construction_map.items():
        if chebyshev(pos, cpos) <= radius:
            result.append((cpos, prog))
    result.sort(key=lambda x: -x[1])
    return result


def best_free_cell_near(
    pos: tuple,
    radius: int,
    occupied: set,
    mountains: set,
    map_w: int,
    map_h: int,
    beaver_positions: list,
    connected: set,
) -> tuple | None:
    """
    Pick the single best free cell to start a NEW construction near pos.
    Deterministic: sorted by (cell_value DESC, coord tuple ASC).
    The coord tiebreak guarantees the same cell is chosen every turn
    as long as the game state doesn't change.
    """
    candidates = []
    for dx in range(-radius, radius + 1):
        for dy in range(-radius, radius + 1):
            nx, ny = pos[0] + dx, pos[1] + dy
            nb = (nx, ny)
            if nb in occupied or nb in mountains:
                continue
            if not (0 <= nx < map_w and 0 <= ny < map_h):
                continue
            # Score: cell base value + beaver proximity bonus
            score = cell_value(nx, ny)
            for bpos in beaver_positions:
                dist = chebyshev(nb, bpos)
                if dist <= DEFAULT_AR + 2:
                    score += cell_value(*bpos) * BEAVER_MULTIPLIER // max(1, dist)
            # Prefer cells already adjacent to a connected plantation
            if any(chebyshev(nb, c) == 1 for c in connected):
                score += 150
            candidates.append((score, nb))

    if not candidates:
        return None
    # Sort by score DESC, then by coordinate for strict determinism
    candidates.sort(key=lambda x: (-x[0], x[1]))
    return candidates[0][1]


def score_build_target(x, y, connected, beaver_positions) -> float:
    base = cell_value(x, y)
    enhanced_nearby = sum(
        200 for ex in range(-3, 4) for ey in range(-3, 4)
        if is_enhanced(x + ex, y + ey)
    )
    beaver_bonus = 0
    for bpos in beaver_positions:
        dist = chebyshev((x, y), bpos)
        if dist <= DEFAULT_AR + 2:
            beaver_bonus = max(
                beaver_bonus,
                cell_value(*bpos) * BEAVER_MULTIPLIER // max(1, dist),
            )
    adjacency_bonus = 150 if any(chebyshev((x, y), c) == 1 for c in connected) else 0
    return base + enhanced_nearby + beaver_bonus + adjacency_bonus


def get_frontier(connected, occupied, mountains, ar, map_w, map_h, beaver_positions):
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
                    candidates[pos] = score_build_target(nx, ny, connected, beaver_positions)
    return sorted(candidates.items(), key=lambda kv: (-kv[1], kv[0]))

# ═══════════════════════════════════════════════════════════════════════
#  MAIN LOOP
# ═══════════════════════════════════════════════════════════════════════

def main_loop():
    last_turn  = -1
    sr_current = DEFAULT_SR
    cs_current = DEFAULT_CS

    logging.info("DatsSol bot v3 started.")

    while True:
        try:
            state = get_arena()
            turn         = state.get("turnNo", 0)
            next_turn_in = state.get("nextTurnIn", 1.0)

            if turn == last_turn:
                time.sleep(0.1)
                continue
            last_turn = turn

            # ── Parse ─────────────────────────────────────────────────────
            plantations   = state.get("plantations", [])
            enemies       = state.get("enemy", [])
            mountains     = set(tuple(m) for m in state.get("mountains", []))
            constructions = state.get("construction", [])
            beavers       = state.get("beavers", [])
            cells         = {tuple(c["position"]): c for c in state.get("cells", [])}
            upgrades      = state.get("plantationUpgrades", {})
            ar            = state.get("actionRange", DEFAULT_AR)
            map_w, map_h  = state.get("size", [400, 400])

            # construction_map: position → progress (for "commit to existing builds")
            construction_map = {tuple(c["position"]): c.get("progress", 0) for c in constructions}

            tiers_map  = {t["name"]: t for t in upgrades.get("tiers", [])}
            sr_current = DEFAULT_SR + tiers_map.get("signal_range", {}).get("current", 0)
            cs_current = DEFAULT_CS + tiers_map.get("repair_power", {}).get("current", 0)
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

            p_map         = {tuple(p["position"]): p for p in plantations}
            c_pos_set     = set(construction_map.keys())
            enemy_pos_set = {tuple(e["position"]) for e in enemies}
            # occupied = our plantations + our constructions + enemy plantations
            occupied = set(p_map.keys()) | c_pos_set | enemy_pos_set

            connected        = bfs_connected(set(p_map.keys()), main_pos)
            iso_count        = len(plantations) - len(connected)
            beaver_positions = [tuple(b["position"]) for b in beavers]

            logging.info(
                f"══ T{turn:4d} | {len(plantations)} plants "
                f"({len(connected)} conn, {iso_count} iso) "
                f"| constr={len(constructions)} beavers={len(beavers)} "
                f"| next {next_turn_in:.2f}s ══"
            )

            # ── ЦУ cell progress ──────────────────────────────────────────
            cu_cell_prog   = cells.get(main_pos, {}).get("terraformationProgress", 0)
            turns_until_cu = max(0, (100 - cu_cell_prog) // 5)

            if cu_cell_prog >= CU_BUILD_NEIGHBOR_PCT:
                logging.warning(
                    f"  ⚠ ЦУ cell {cu_cell_prog:.0f}% "
                    f"(~{turns_until_cu} turns, build needs {build_turns_needed})"
                )

            upgrade_to_buy = choose_upgrade(upgrades)

            cb = CommandBuilder(connected, ar=ar, sr=sr_current)

            connected_plants = [p for p in plantations if tuple(p["position"]) in connected]
            by_hp_asc  = sorted(connected_plants, key=lambda p: p["hp"])
            by_hp_desc = sorted(connected_plants, key=lambda p: -p["hp"])

            relocate_cmd = None

            # ══════════════════════════════════════════════════════════════
            # PRIORITY 0 — ЦУ SURVIVAL
            #
            # THE KEY FIX:
            #   Instead of picking a fresh adj cell each turn (which caused target
            #   switching and zero accumulated progress), we now:
            #   1. Check the server's `construction` list for any build already
            #      IN PROGRESS adjacent to ЦУ → continue building there.
            #   2. Only start a new escape build if no in-progress adj construction.
            #   3. Always pick the same deterministic free cell (sorted by score,
            #      then coord) so we don't flip targets between turns.
            # ══════════════════════════════════════════════════════════════
            cu_must_relocate = (
                cu_cell_prog >= CU_RELOCATE_PCT or main_p["hp"] <= CU_PANIC_HP
            )

            if cu_must_relocate:
                adj_connected = [
                    nb for nb in neighbors4(*main_pos)
                    if nb in connected and nb != main_pos
                ]
                if adj_connected:
                    best_nb = max(adj_connected, key=lambda nb: p_map[nb]["hp"])
                    relocate_cmd = [list(main_pos), list(best_nb)]
                    logging.warning(
                        f"  🚨 RELOCATE ЦУ → {best_nb} "
                        f"(cell={cu_cell_prog:.0f}%, hp={main_p['hp']})"
                    )
                else:
                    # No adjacent plantation yet — still try to build
                    logging.warning(
                        f"  🚨 PANIC: ЦУ at {cu_cell_prog:.0f}%, "
                        f"no adjacent plantation yet!"
                    )
                    # Fall through to escape-build logic below

            if cu_cell_prog >= CU_BUILD_NEIGHBOR_PCT:
                # Step 1: Is there already a construction in progress adjacent to ЦУ?
                adj_in_progress = constructions_near(main_pos, construction_map, radius=1)

                if adj_in_progress:
                    # COMMIT to the most-advanced in-progress adjacent construction
                    escape_target, prog = adj_in_progress[0]
                    for builder in by_hp_desc:
                        bpos = tuple(builder["position"])
                        if bpos not in connected:
                            continue
                        if cb.try_assign(bpos, escape_target, f"escape-continue({prog}hp)"):
                            logging.warning(
                                f"  🔑 Continue escape build → {escape_target} "
                                f"({prog}hp, ЦУ={cu_cell_prog:.0f}%)"
                            )
                            break
                else:
                    # Step 2: No adj construction — start one. Pick DETERMINISTICALLY.
                    # Exclude constructions further away that we might already be building.
                    adj_target = best_free_cell_near(
                        main_pos, radius=1,
                        occupied=occupied, mountains=mountains,
                        map_w=map_w, map_h=map_h,
                        beaver_positions=beaver_positions,
                        connected=connected,
                    )
                    if adj_target:
                        for builder in by_hp_desc:
                            bpos = tuple(builder["position"])
                            if bpos not in connected:
                                continue
                            if cb.try_assign(bpos, adj_target, f"escape-start"):
                                logging.warning(
                                    f"  🔑 Start escape build → {adj_target} "
                                    f"(ЦУ={cu_cell_prog:.0f}%)"
                                )
                                break
                    else:
                        logging.warning(
                            f"  🚨 No free cell adjacent to ЦУ for escape build!"
                        )

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
            # PRIORITY 2 — Repair bridges (articulation points)
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
            # PRIORITY 3 — Repair any other low-HP plantation
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
            # PRIORITY 4 — Continue in-progress constructions
            #   Any existing construction that we've already started needs
            #   to keep receiving build commands every turn until it completes.
            #   Prioritize the most advanced (closest to 50 HP) first.
            # ══════════════════════════════════════════════════════════════
            ongoing = sorted(construction_map.items(), key=lambda x: -x[1])
            for cpos, prog in ongoing:
                # Find a builder that can reach this construction
                builders_sorted = sorted(
                    connected_plants,
                    key=lambda p: chebyshev(tuple(p["position"]), cpos),
                )
                for builder in builders_sorted:
                    bpos = tuple(builder["position"])
                    if cb.try_assign(bpos, cpos, f"continue-build({prog}hp)"):
                        break

            # ══════════════════════════════════════════════════════════════
            # PRIORITY 5 — Preemptive rebuild of nearly-complete own cells
            #   (only fires once build_turns_needed ≥ turns_left for non-ЦУ cells)
            # ══════════════════════════════════════════════════════════════
            for ppos, cell_data in cells.items():
                if ppos not in connected or ppos == main_pos:
                    continue
                prog       = cell_data.get("terraformationProgress", 0)
                turns_left = max(0, (100 - prog) // 5)
                if turns_left > build_turns_needed + 2:
                    continue

                # Look for in-progress construction adjacent to this expiring cell first
                adj_progress = constructions_near(ppos, construction_map, radius=1)
                if adj_progress:
                    target_nb = adj_progress[0][0]
                else:
                    target_nb = best_free_cell_near(
                        ppos, radius=1,
                        occupied=occupied, mountains=mountains,
                        map_w=map_w, map_h=map_h,
                        beaver_positions=beaver_positions,
                        connected=connected,
                    )

                if target_nb:
                    builders_sorted = sorted(
                        connected_plants,
                        key=lambda p: chebyshev(tuple(p["position"]), target_nb),
                    )
                    for builder in builders_sorted:
                        bpos = tuple(builder["position"])
                        if cb.try_assign(bpos, target_nb, f"preempt({prog:.0f}%)"):
                            logging.info(
                                f"  ⟳ Preemptive build {ppos}→{target_nb} "
                                f"({prog:.0f}%, ~{turns_left} turns)"
                            )
                            break

            # ══════════════════════════════════════════════════════════════
            # PRIORITY 6 — [PATCH] Attack beaver lairs (20x value!)
            # ══════════════════════════════════════════════════════════════
            for beaver in sorted(beavers, key=lambda b: b.get("hp", 100)):
                bvpos = tuple(beaver["position"])
                bv_pts = cell_value(*bvpos) * BEAVER_MULTIPLIER
                for attacker in by_hp_desc:
                    apos = tuple(attacker["position"])
                    if apos not in connected:
                        continue
                    if cb.try_assign(apos, bvpos, f"beaver(hp={beaver.get('hp')})"):
                        logging.info(f"  🦫 Beaver {bvpos} hp={beaver.get('hp')} val={bv_pts}")
                        break

            # ══════════════════════════════════════════════════════════════
            # PRIORITY 7 — Expand toward best frontier cells
            # ══════════════════════════════════════════════════════════════
            frontier = get_frontier(
                connected, occupied, mountains, ar, map_w, map_h, beaver_positions
            )
            for target_pos, score in frontier:
                builders_sorted = sorted(
                    connected_plants,
                    key=lambda p: chebyshev(tuple(p["position"]), target_pos),
                )
                for builder in builders_sorted:
                    bpos = tuple(builder["position"])
                    if cb.try_assign(bpos, target_pos, f"expand({score:.0f})"):
                        break

            # ══════════════════════════════════════════════════════════════
            # PRIORITY 8 — Opportunistic sabotage of critically-low HP enemies
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
                logging.warning("  Nothing to send this turn.")
                time.sleep(max(0.05, next_turn_in - 0.2))
                continue

            result = send_command(
                command=cmds if cmds else None,
                upgrade=upgrade_to_buy,
                relocate=relocate_cmd,
            )

            if result:
                errors = [e for e in result.get("errors", []) if "empty command" not in e]
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
