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
BASE_URL = os.getenv('TEST_URL')
HEADERS = {"X-Auth-Token": TOKEN}

if not TOKEN:
    logging.error("Token not found! Make sure your .env file contains AUTH_TOKEN")
    exit(1)

# ═══════════════════════════════════════════════════════
#  CONSTANTS & TUNING
# ═══════════════════════════════════════════════════════

DEFAULT_AR = 2          # action range (Chebyshev)
DEFAULT_SR = 3          # signal range (Chebyshev)
DEFAULT_TS = 5          # terraforming speed %/turn  → 100/5 = 20 turns to complete
DEFAULT_DS = 10         # degradation speed HP/turn

REPAIR_HP_THRESHOLD   = 30   # repair if HP ≤ this
URGENT_HP_THRESHOLD   = 15   # top-priority repair if HP ≤ this
PREEMPTIVE_TF_PERCENT = 85   # start building replacement when cell reaches this %

# Upgrade buy order (most impactful first)
UPGRADE_PRIORITY = [
    "settlement_limit",        # more plantations → more points (max +10 slots)
    "repair_power",            # faster repair / build
    "decay_mitigation",        # slower degradation of isolated plantations
    "max_hp",                  # bigger HP buffer
    "earthquake_mitigation",   # reduce earthquake damage
    "beaver_damage_mitigation",# reduce beaver damage
    "signal_range",            # reach further relays
    "vision_range",            # see more of the map
]

# ═══════════════════════════════════════════════════════
#  API HELPERS
# ═══════════════════════════════════════════════════════

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

# ═══════════════════════════════════════════════════════
#  GEOMETRY HELPERS
# ═══════════════════════════════════════════════════════

def chebyshev(a: tuple, b: tuple) -> int:
    """Square-zone radius metric used by AR / SR / VR."""
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def is_enhanced(x: int, y: int) -> bool:
    """усиленная клетка → 1500 max points instead of 1000."""
    return x % 7 == 0 and y % 7 == 0


def cell_value(x: int, y: int) -> int:
    return 1500 if is_enhanced(x, y) else 1000


def neighbors4(x, y):
    return [(x+1, y), (x-1, y), (x, y+1), (x, y-1)]

# ═══════════════════════════════════════════════════════
#  CONNECTIVITY
# ═══════════════════════════════════════════════════════

def bfs_connected(plantation_positions: set, main_pos: tuple) -> set:
    """Return all plantation positions reachable from ЦУ via 4-dir adjacency."""
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
    """
    Quick check: would removing 'node' disconnect any other node from ЦУ?
    Used to protect critical bridge plantations.
    """
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

# ═══════════════════════════════════════════════════════
#  UPGRADE SELECTOR
# ═══════════════════════════════════════════════════════

def choose_upgrade(upgrades: dict) -> str | None:
    if not upgrades or upgrades.get("points", 0) == 0:
        return None
    tiers = {t["name"]: t for t in upgrades.get("tiers", [])}
    for name in UPGRADE_PRIORITY:
        t = tiers.get(name)
        if t and t["current"] < t["max"]:
            logging.info(f"  → Upgrade selected: {name} (currently {t['current']}/{t['max']})")
            return name
    return None

# ═══════════════════════════════════════════════════════
#  COMMAND BUILDER
# ═══════════════════════════════════════════════════════

class CommandBuilder:
    """
    Manages command assignments for one turn.
    Enforces:
      - one command per author plantation
      - efficiency loss tracking per output point
    """

    def __init__(self, connected: set, ar: int = DEFAULT_AR, sr: int = DEFAULT_SR):
        self.connected = connected
        self.ar = ar
        self.sr = sr
        self.assigned_authors: set = set()
        self.output_usage: dict = {}   # pos → count of commands routed through it
        self.commands: list = []

    def _find_output(self, author: tuple, target: tuple) -> tuple | None:
        """Find best relay/output point. Returns None if unreachable."""
        # Option A: direct (author == output)
        if chebyshev(author, target) <= self.ar:
            return author
        # Option B: relay through a connected plantation
        best, best_eff = None, -1
        for relay in self.connected:
            if chebyshev(author, relay) > self.sr:
                continue
            if chebyshev(relay, target) > self.ar:
                continue
            eff = max(0, 5 - self.output_usage.get(relay, 0))
            if eff > best_eff:
                best_eff, best = eff, relay
        return best  # None if not found

    def try_assign(self, author: tuple, target: tuple, label: str = "") -> bool:
        """
        Try to create a command: author → output → target.
        Returns True if successful.
        """
        if author in self.assigned_authors:
            return False
        output = self._find_output(author, target)
        if output is None:
            return False
        # Check remaining efficiency at this output
        eff = max(0, 5 - self.output_usage.get(output, 0))
        if eff <= 0:
            return False
        self.commands.append({"path": [list(author), list(output), list(target)]})
        self.output_usage[output] = self.output_usage.get(output, 0) + 1
        self.assigned_authors.add(author)
        if label:
            logging.debug(f"    [{label}] {author} → {output} → {target}")
        return True

# ═══════════════════════════════════════════════════════
#  FRONTIER SCORING
# ═══════════════════════════════════════════════════════

def score_build_target(x: int, y: int, connected: set) -> float:
    """
    Score a cell as a build target.
    Higher = better.
    """
    base = cell_value(x, y)

    # Bonus if adjacent enhanced cells will become reachable soon
    enhanced_proximity = sum(
        300 for ex in range(-3, 4) for ey in range(-3, 4)
        if is_enhanced(x + ex, y + ey)
    )

    # Bonus if this cell is in a direction toward dense enhanced clusters
    # Simple heuristic: penalise distance from nearest enhanced cell
    nearest_enhanced_dist = min(
        (abs(x - ex * 7) + abs(y - ey * 7))
        for ex in range(max(0, x // 7 - 1), x // 7 + 2)
        for ey in range(max(0, y // 7 - 1), y // 7 + 2)
    )
    proximity_bonus = max(0, 200 - nearest_enhanced_dist * 15)

    # Prefer cells closer to existing connected plantations (cheaper to reach)
    reach_bonus = 100 if any(chebyshev((x, y), c) == 1 for c in connected) else 0

    return base + enhanced_proximity + proximity_bonus + reach_bonus


def get_frontier(
    connected: set,
    occupied: set,
    mountains: set,
    ar: int,
    map_w: int,
    map_h: int,
) -> list:
    """Return sorted list of (score, target_pos) candidates for building."""
    candidates = {}
    for cpos in connected:
        cx, cy = cpos
        for dx in range(-ar, ar + 1):
            for dy in range(-ar, ar + 1):
                nx, ny = cx + dx, cy + dy
                pos = (nx, ny)
                if pos in occupied or pos in mountains:
                    continue
                if not (0 <= nx < map_w and 0 <= ny < map_h):
                    continue
                if pos not in candidates:
                    candidates[pos] = score_build_target(nx, ny, connected)
    return sorted(candidates.items(), key=lambda kv: -kv[1])

# ═══════════════════════════════════════════════════════
#  MAIN LOOP
# ═══════════════════════════════════════════════════════

def main_loop():
    last_turn = -1
    sr_override = DEFAULT_SR  # updated from upgrade data when possible

    logging.info("DatsSol bot started. Waiting for first turn...")

    while True:
        try:
            state = get_arena()
            turn = state.get("turnNo", 0)
            next_turn_in = state.get("nextTurnIn", 1.0)

            if turn == last_turn:
                time.sleep(0.15)
                continue
            last_turn = turn

            logging.info(f"══ Turn {turn:4d}  (next in {next_turn_in:.2f}s) ══")

            # ── Parse state ──────────────────────────────────────────────
            plantations  = state.get("plantations", [])
            enemies      = state.get("enemy", [])
            mountains    = set(tuple(m) for m in state.get("mountains", []))
            constructions= state.get("construction", [])
            beavers      = state.get("beavers", [])
            cells        = {tuple(c["position"]): c for c in state.get("cells", [])}
            upgrades     = state.get("plantationUpgrades", {})
            ar           = state.get("actionRange", DEFAULT_AR)
            map_w, map_h = state.get("size", [500, 500])

            # Update SR from upgrade tiers
            sr_tier = next(
                (t for t in upgrades.get("tiers", []) if t["name"] == "signal_range"),
                None,
            )
            if sr_tier:
                sr_override = DEFAULT_SR + sr_tier.get("current", 0)

            if not plantations:
                logging.warning("No plantations — waiting for respawn…")
                time.sleep(0.5)
                continue

            # Find ЦУ
            main_p = next((p for p in plantations if p.get("isMain")), None)
            if not main_p:
                logging.warning("ЦУ not found in plantation list!")
                time.sleep(0.5)
                continue

            main_pos = tuple(main_p["position"])

            # Build position maps
            p_map      = {tuple(p["position"]): p for p in plantations}
            c_pos_set  = {tuple(c["position"]) for c in constructions}
            occupied   = set(p_map.keys()) | c_pos_set

            # Connectivity analysis
            connected = bfs_connected(set(p_map.keys()), main_pos)
            logging.info(
                f"  Plantations: {len(plantations)} total | "
                f"{len(connected)} connected | "
                f"{len(plantations)-len(connected)} isolated"
            )

            # Upgrade
            upgrade_to_buy = choose_upgrade(upgrades)

            # Command builder
            cb = CommandBuilder(connected, ar=ar, sr=sr_override)

            # Sorted plantation lists for assignment loops
            connected_plants = [p for p in plantations if tuple(p["position"]) in connected]
            by_hp_asc  = sorted(connected_plants, key=lambda p: p["hp"])
            by_hp_desc = sorted(connected_plants, key=lambda p: -p["hp"])

            # ──────────────────────────────────────────────────────────────
            # PRIORITY 0: Protect ЦУ — move it if HP is critically low
            # ──────────────────────────────────────────────────────────────
            relocate_cmd = None
            if main_p["hp"] <= URGENT_HP_THRESHOLD:
                # Find a safe adjacent plantation to become the new ЦУ
                for nb in neighbors4(*main_pos):
                    if nb in connected and nb != main_pos:
                        nb_p = p_map[nb]
                        if nb_p["hp"] > main_p["hp"]:
                            relocate_cmd = [list(main_pos), list(nb)]
                            logging.warning(f"  ⚠ Moving ЦУ from {main_pos} to {nb} (ЦУ HP={main_p['hp']})")
                            break

            # ──────────────────────────────────────────────────────────────
            # PRIORITY 1: Emergency repair of ЦУ itself
            # ──────────────────────────────────────────────────────────────
            if main_p["hp"] < REPAIR_HP_THRESHOLD:
                for healer in by_hp_desc:
                    hpos = tuple(healer["position"])
                    if hpos == main_pos:
                        continue
                    if cb.try_assign(hpos, main_pos, "repair-ЦУ"):
                        break

            # ──────────────────────────────────────────────────────────────
            # PRIORITY 2: Repair articulation points (bridges) first
            # ──────────────────────────────────────────────────────────────
            for target_p in by_hp_asc:
                tpos = tuple(target_p["position"])
                if target_p["hp"] >= REPAIR_HP_THRESHOLD:
                    break  # sorted asc, everything else is fine
                if tpos == main_pos:
                    continue  # handled above
                if not is_articulation_point(connected, tpos):
                    continue
                for healer in by_hp_desc:
                    hpos = tuple(healer["position"])
                    if hpos == tpos:
                        continue
                    if cb.try_assign(hpos, tpos, "repair-bridge"):
                        break

            # ──────────────────────────────────────────────────────────────
            # PRIORITY 3: Repair any other low-HP connected plantation
            # ──────────────────────────────────────────────────────────────
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
                    if cb.try_assign(hpos, tpos, "repair-low"):
                        break

            # ──────────────────────────────────────────────────────────────
            # PRIORITY 4: Preemptive rebuild of nearly-complete plantations
            #   A plantation at 85%+ will disappear in ≤3 turns.
            #   Build a neighbour now to avoid chain breaks.
            # ──────────────────────────────────────────────────────────────
            for ppos, cell in cells.items():
                prog = cell.get("terraformationProgress", 0)
                if prog < PREEMPTIVE_TF_PERCENT:
                    continue
                if ppos not in connected:
                    continue
                # Find an adjacent free cell to place a bridge
                for nb in neighbors4(*ppos):
                    if nb in occupied or nb in mountains:
                        continue
                    nx, ny = nb
                    if not (0 <= nx < map_w and 0 <= ny < map_h):
                        continue
                    # Assign a builder
                    for builder in by_hp_desc:
                        bpos = tuple(builder["position"])
                        if bpos not in connected:
                            continue
                        if cb.try_assign(bpos, nb, "preemptive-build"):
                            logging.info(f"  ⟳ Preemptive build at {nb} (parent {ppos} is {prog:.0f}%)")
                            break
                    break  # one replacement per expiring cell per turn

            # ──────────────────────────────────────────────────────────────
            # PRIORITY 5: Expand toward enhanced cells
            # ──────────────────────────────────────────────────────────────
            frontier = get_frontier(connected, occupied, mountains, ar, map_w, map_h)

            for target_pos, score in frontier:
                # Assign a free connected plantation as builder
                # Prefer the one closest to the target (minimises relay hops)
                builders = sorted(
                    [p for p in plantations if tuple(p["position"]) in connected],
                    key=lambda p: chebyshev(tuple(p["position"]), target_pos),
                )
                for builder in builders:
                    bpos = tuple(builder["position"])
                    if cb.try_assign(bpos, target_pos, "expand"):
                        break

            # ──────────────────────────────────────────────────────────────
            # PRIORITY 6: Attack beaver lairs in vision range
            # ──────────────────────────────────────────────────────────────
            # Killing a lair gives 10× cell points = huge bonus
            for beaver in sorted(beavers, key=lambda b: b.get("hp", 100)):
                bvpos = tuple(beaver["position"])
                for attacker in by_hp_desc:
                    apos = tuple(attacker["position"])
                    if apos not in connected:
                        continue
                    if cb.try_assign(apos, bvpos, "attack-beaver"):
                        logging.info(f"  🦫 Attacking beaver at {bvpos} (HP={beaver.get('hp')})")
                        break

            # ──────────────────────────────────────────────────────────────
            # PRIORITY 7: Sabotage enemy plantations (opportunistic)
            # ──────────────────────────────────────────────────────────────
            # Only attack enemies that are not ЦУ (destroying non-ЦУ is lower risk
            # than provoking retaliation on our ЦУ chain)
            for enemy_p in sorted(enemies, key=lambda e: e.get("hp", 999)):
                epos = tuple(enemy_p["position"])
                for attacker in by_hp_desc:
                    apos = tuple(attacker["position"])
                    if apos not in connected:
                        continue
                    if cb.try_assign(apos, epos, "sabotage"):
                        logging.info(f"  ⚔ Sabotage enemy at {epos} (HP={enemy_p.get('hp')})")
                        break

            # ──────────────────────────────────────────────────────────────
            # Send turn
            # ──────────────────────────────────────────────────────────────
            cmds = cb.commands
            if not cmds and not upgrade_to_buy and not relocate_cmd:
                logging.warning("  No commands generated this turn — skipping send.")
                sleep_time = max(0.05, next_turn_in - 0.2)
                time.sleep(sleep_time)
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
                    logging.info(
                        f"  ✔ {len(cmds)} commands sent | "
                        f"upgrade={upgrade_to_buy or '—'} | "
                        f"relocate={'yes' if relocate_cmd else 'no'}"
                    )

            sleep_time = max(0.05, next_turn_in - 0.25)
            time.sleep(sleep_time)

        except requests.exceptions.HTTPError as e:
            logging.error(f"HTTP error: {e.response.status_code} — {e.response.text[:200]}")
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
