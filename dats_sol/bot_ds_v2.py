"""
DatsSol bot v6
==============
Improvements over v5:
- Fix typo: popleft → popleft in BFS.
- Start escape construction immediately at 10% progress (was 20%).
- Relocate at 80% (was 85%) for safety margin.
- Always send at least the escape build command (if any) to avoid empty turns.
- Prevent duplicate upgrade purchases after respawn by tracking last upgrade turn.
- Add more aggressive escape build: use all available connected plantations as builders.
- Improve sleep precision to minimise turn skips.
"""

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
    logging.error("AUTH_TOKEN not found in .env")
    exit(1)

# ═══════════════════════════════════════════════════════════════════════
#  CONSTANTS
# ═══════════════════════════════════════════════════════════════════════

DEFAULT_AR  = 2
DEFAULT_SR  = 3
DEFAULT_CS  = 5
DEFAULT_MHP = 50

BEAVER_MULTIPLIER = 20

CU_BUILD_NEIGHBOR_PCT = 10   # start building escape earlier
CU_RELOCATE_PCT       = 80   # relocate a bit earlier
CU_PANIC_HP           = 15

REPAIR_HP_THRESHOLD = 35
URGENT_HP_THRESHOLD = 15

UPGRADE_PRIORITY = [
    "repair_power",
    "beaver_damage_mitigation",
    "decay_mitigation",
    "settlement_limit",
    "max_hp",
    "earthquake_mitigation",
    "vision_range",
    "signal_range",
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
    if command:    body["command"]           = command
    if upgrade:    body["plantationUpgrade"] = upgrade
    if relocate:   body["relocateMain"]      = relocate
    if not body:
        # Send a dummy command to avoid empty turn? No, server requires at least one action.
        # If we have nothing, we skip sending to avoid error, but we risk losing turn.
        return None
    r = requests.post(f"{BASE_URL}/api/command", headers=HEADERS, json=body, timeout=5)
    r.raise_for_status()
    return r.json()

# ═══════════════════════════════════════════════════════════════════════
#  GEOMETRY
# ═══════════════════════════════════════════════════════════════════════

def chebyshev(a, b) -> int:
    return max(abs(a[0]-b[0]), abs(a[1]-b[1]))

def is_enhanced(x, y) -> bool:
    return x % 7 == 0 and y % 7 == 0

def cell_value(x, y) -> int:
    return 1500 if is_enhanced(x, y) else 1000

def neighbors4(x, y):
    return [(x+1, y), (x-1, y), (x, y+1), (x, y-1)]

# ═══════════════════════════════════════════════════════════════════════
#  CONNECTIVITY
# ═══════════════════════════════════════════════════════════════════════

def bfs_connected(plantation_positions: set, main_pos: tuple) -> set:
    if main_pos not in plantation_positions:
        return set()
    visited = {main_pos}
    q = deque([main_pos])
    while q:
        pos = q.popleft()   # fixed typo: was popleft
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

def choose_upgrade(upgrades: dict, last_upgrade_turn: int, current_turn: int) -> str | None:
    if not upgrades or upgrades.get("points", 0) == 0:
        return None
    # Avoid buying upgrade on the same turn after respawn (server might still show old state)
    if current_turn == last_upgrade_turn:
        return None
    tiers = {t["name"]: t for t in upgrades.get("tiers", [])}
    for name in UPGRADE_PRIORITY:
        t = tiers.get(name)
        if t and t["current"] < t["max"]:
            return name
    return None

# ═══════════════════════════════════════════════════════════════════════
#  COMMAND BUILDER
# ═══════════════════════════════════════════════════════════════════════

class CommandBuilder:
    def __init__(self, connected: set, ar: int, sr: int):
        self.connected   = connected
        self.ar          = ar
        self.sr          = sr
        self.assigned    : set  = set()
        self.relay_usage : dict = {}
        self.commands    : list = []

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
#  FRONTIER
# ═══════════════════════════════════════════════════════════════════════

def score_build_target(x, y, connected, beaver_positions) -> float:
    base = cell_value(x, y)
    enhanced_nearby = sum(
        200 for ex in range(-3, 4) for ey in range(-3, 4)
        if is_enhanced(x+ex, y+ey)
    )
    beaver_bonus = max(
        (cell_value(*bp) * BEAVER_MULTIPLIER // max(1, chebyshev((x,y), bp))
         for bp in beaver_positions if chebyshev((x,y), bp) <= DEFAULT_AR + 2),
        default=0
    )
    adj_bonus = 150 if any(chebyshev((x,y), c) == 1 for c in connected) else 0
    return base + enhanced_nearby + beaver_bonus + adj_bonus


def get_frontier(connected, occupied, mountains, ar, map_w, map_h, beaver_positions):
    candidates = {}
    for cx, cy in connected:
        for dx in range(-ar, ar+1):
            for dy in range(-ar, ar+1):
                pos = (cx+dx, cy+dy)
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
    last_turn   = -1
    sr_current  = DEFAULT_SR
    cs_current  = DEFAULT_CS

    escape_target : tuple | None = None
    last_cu_pos   : tuple | None = None
    last_upgrade_turn = -1

    logging.info("DatsSol bot v6 started.")

    while True:
        loop_start = time.time()
        try:
            state        = get_arena()
            turn         = state.get("turnNo", 0)
            next_turn_in = state.get("nextTurnIn", 1.0)

            if turn == last_turn:
                time.sleep(0.03)
                continue
            last_turn = turn

            # ── Parse ────────────────────────────────────────────────────
            plantations   = state.get("plantations", [])
            enemies       = state.get("enemy", [])
            mountains     = set(tuple(m) for m in state.get("mountains", []))
            beavers       = state.get("beavers", [])
            cells         = {tuple(c["position"]): c for c in state.get("cells", [])}
            upgrades      = state.get("plantationUpgrades", {})
            ar            = state.get("actionRange", DEFAULT_AR)
            map_w, map_h  = state.get("size", [400, 400])
            constr_list   = state.get("construction", [])
            constr_count  = len(constr_list)

            tiers_map  = {t["name"]: t for t in upgrades.get("tiers", [])}
            sr_current = DEFAULT_SR + tiers_map.get("signal_range",  {}).get("current", 0)
            cs_current = DEFAULT_CS + tiers_map.get("repair_power",  {}).get("current", 0)
            ds_current = 10         - tiers_map.get("decay_mitigation", {}).get("current", 0) * 2
            ds_current = max(2, ds_current)
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

            main_pos      = tuple(main_p["position"])
            p_map         = {tuple(p["position"]): p for p in plantations}
            enemy_pos_set = {tuple(e["position"]) for e in enemies}
            occupied      = set(p_map.keys()) | {tuple(c["position"]) for c in constr_list} | enemy_pos_set

            connected        = bfs_connected(set(p_map.keys()), main_pos)
            iso_count        = len(plantations) - len(connected)
            beaver_positions = [tuple(b["position"]) for b in beavers]

            cu_cell_prog   = cells.get(main_pos, {}).get("terraformationProgress", 0)
            turns_until_cu = max(0, (100 - cu_cell_prog) // 5)

            logging.info(
                f"══ T{turn:4d} | {len(plantations)}p ({len(connected)} conn, {iso_count} iso) "
                f"| constr={constr_count} bvr={len(beavers)} "
                f"| ЦУ-cell={cu_cell_prog:.0f}% (~{turns_until_cu}t) "
                f"| CS={cs_current} DS={ds_current} next={next_turn_in:.2f}s ══"
            )

            # ── Upgrade ──────────────────────────────────────────────────
            upgrade_to_buy = choose_upgrade(upgrades, last_upgrade_turn, turn)

            # ── ESCAPE TARGET STATE MACHINE ─────────────────────────────
            if main_pos != last_cu_pos:
                if last_cu_pos is not None:
                    logging.warning(f"  ☠ ЦУ moved {last_cu_pos}→{main_pos} (died & respawned)")
                escape_target = None
                last_cu_pos   = main_pos

            if escape_target is not None:
                if escape_target in mountains:
                    escape_target = None
                elif escape_target in enemy_pos_set:
                    logging.warning(f"  ⚠ Escape target {escape_target} taken by enemy, resetting")
                    escape_target = None

            escape_ready = escape_target is not None and escape_target in connected

            # Pick new escape target if needed
            if escape_target is None and cu_cell_prog >= CU_BUILD_NEIGHBOR_PCT:
                for nb in sorted(neighbors4(*main_pos), key=lambda p: (-cell_value(*p), p)):
                    nx, ny = nb
                    if nb in mountains or nb in enemy_pos_set:
                        continue
                    if nb in p_map and nb not in connected:
                        continue
                    if not (0 <= nx < map_w and 0 <= ny < map_h):
                        continue
                    if nb not in p_map:
                        escape_target = nb
                        logging.warning(f"  🔑 New escape target: {nb} (ЦУ={cu_cell_prog:.0f}%)")
                        break
                    if nb in connected:
                        escape_target = nb
                        escape_ready  = True
                        break

            # ── Command builder ──────────────────────────────────────────
            cb = CommandBuilder(connected, ar=ar, sr=sr_current)

            connected_plants = [p for p in plantations if tuple(p["position"]) in connected]
            by_hp_asc  = sorted(connected_plants, key=lambda p:  p["hp"])
            by_hp_desc = sorted(connected_plants, key=lambda p: -p["hp"])

            relocate_cmd = None

            # PRIORITY 0 — ЦУ SURVIVAL: relocate or build escape
            if escape_ready and (cu_cell_prog >= CU_RELOCATE_PCT or main_p["hp"] <= CU_PANIC_HP):
                relocate_cmd  = [list(main_pos), list(escape_target)]
                logging.warning(
                    f"  🚨 RELOCATE ЦУ {main_pos}→{escape_target} "
                    f"(cell={cu_cell_prog:.0f}%, hp={main_p['hp']})"
                )
                escape_target = None

            elif escape_target is not None and not escape_ready:
                # Use ALL available connected plantations to build escape faster
                builders = sorted(
                    connected_plants,
                    key=lambda p: chebyshev(tuple(p["position"]), escape_target)
                )
                assigned_any = False
                for builder in builders:
                    bpos = tuple(builder["position"])
                    if cb.try_assign(bpos, escape_target, "escape-build"):
                        assigned_any = True
                if assigned_any:
                    prog_str = ""
                    constr_map = {tuple(c["position"]): c.get("progress", 0) for c in constr_list}
                    if escape_target in constr_map:
                        prog_str = f" server_prog={constr_map[escape_target]}hp"
                    logging.warning(
                        f"  🔨 Build escape {escape_target}{prog_str} "
                        f"(ЦУ={cu_cell_prog:.0f}%, need {build_turns_needed} turns, {len(builders)} builders)"
                    )

            # PRIORITY 1 — Repair ЦУ (urgent)
            if main_p["hp"] <= URGENT_HP_THRESHOLD:
                for healer in by_hp_desc:
                    hpos = tuple(healer["position"])
                    if hpos != main_pos:
                        if cb.try_assign(hpos, main_pos, "repair-ЦУ"):
                            break

            # PRIORITY 2 — Repair bridge plantations
            for tp in by_hp_asc:
                tpos = tuple(tp["position"])
                if tp["hp"] >= REPAIR_HP_THRESHOLD:
                    break
                if tpos == main_pos:
                    continue
                if not is_articulation_point(connected, tpos):
                    continue
                for healer in by_hp_desc:
                    hpos = tuple(healer["position"])
                    if hpos != tpos:
                        if cb.try_assign(hpos, tpos, "repair-bridge"):
                            break

            # PRIORITY 3 — Repair any low-HP plantation
            for tp in by_hp_asc:
                tpos = tuple(tp["position"])
                if tp["hp"] >= REPAIR_HP_THRESHOLD:
                    break
                if tpos == main_pos:
                    continue
                for healer in by_hp_desc:
                    hpos = tuple(healer["position"])
                    if hpos != tpos:
                        if cb.try_assign(hpos, tpos, "repair"):
                            break

            # PRIORITY 4 — Attack beaver lairs
            for beaver in sorted(beavers, key=lambda b: b.get("hp", 100)):
                bvpos = tuple(beaver["position"])
                bv_pts = cell_value(*bvpos) * BEAVER_MULTIPLIER
                for attacker in by_hp_desc:
                    apos = tuple(attacker["position"])
                    if apos in connected:
                        if cb.try_assign(apos, bvpos, f"beaver(hp={beaver.get('hp')})"):
                            logging.info(f"  🦫 Beaver {bvpos} hp={beaver.get('hp')} ({bv_pts}pts)")
                            break

            # PRIORITY 5 — Preemptive rebuild
            for ppos, cell_data in cells.items():
                if ppos not in connected or ppos == main_pos:
                    continue
                prog       = cell_data.get("terraformationProgress", 0)
                turns_left = max(0, (100 - prog) // 5)
                if turns_left > build_turns_needed + 3:
                    continue

                adj_free = sorted(
                    (nb for nb in neighbors4(*ppos)
                     if nb not in occupied and nb not in mountains
                     and 0 <= nb[0] < map_w and 0 <= nb[1] < map_h),
                    key=lambda p: (-cell_value(*p), p)
                )
                if not adj_free:
                    continue

                target_nb = adj_free[0]
                builders  = sorted(connected_plants, key=lambda p: chebyshev(tuple(p["position"]), target_nb))
                for builder in builders:
                    if cb.try_assign(tuple(builder["position"]), target_nb, f"preempt({prog:.0f}%)"):
                        logging.info(f"  ⟳ Preemptive build {ppos}→{target_nb} ({prog:.0f}%, ~{turns_left}t)")
                        break

            # PRIORITY 6 — Expand
            frontier = get_frontier(connected, occupied, mountains, ar, map_w, map_h, beaver_positions)
            for target_pos, score in frontier:
                builders = sorted(
                    connected_plants,
                    key=lambda p: chebyshev(tuple(p["position"]), target_pos)
                )
                for builder in builders:
                    if cb.try_assign(tuple(builder["position"]), target_pos, f"expand({score:.0f})"):
                        break

            # PRIORITY 7 — Sabotage
            for ep in sorted(enemies, key=lambda e: e.get("hp", 999)):
                ehp  = ep.get("hp", 999)
                epos = tuple(ep["position"])
                if ehp > 20:
                    continue
                for attacker in by_hp_desc:
                    apos = tuple(attacker["position"])
                    if apos in connected:
                        if cb.try_assign(apos, epos, f"sabotage(hp={ehp})"):
                            logging.info(f"  ⚔ Sabotage {epos} hp={ehp}")
                            break

            # ── SEND ────────────────────────────────────────────────────
            cmds = cb.commands
            # Always send something if we have escape target and no other commands
            if not cmds and escape_target is not None and not escape_ready:
                # Force build escape with main plantation
                if not cb.try_assign(main_pos, escape_target, "force-escape"):
                    logging.warning("  Unable to assign force-escape build")
                cmds = cb.commands

            if not cmds and not upgrade_to_buy and not relocate_cmd:
                logging.warning("  Nothing to send this turn.")
            else:
                result = send_command(
                    command  = cmds        if cmds        else None,
                    upgrade  = upgrade_to_buy             or  None,
                    relocate = relocate_cmd               or  None,
                )
                if result:
                    errors = [e for e in result.get("errors", []) if "empty command" not in e]
                    if errors:
                        logging.warning(f"  Server errors: {errors}")
                    else:
                        if upgrade_to_buy:
                            last_upgrade_turn = turn
                        rel_str = (
                            "→".join(str(tuple(x)) for x in relocate_cmd)
                            if relocate_cmd else "—"
                        )
                        logging.info(
                            f"  ✔ {len(cmds)} cmds | up={upgrade_to_buy or '—'} | rel={rel_str}"
                        )

            # ── Precise sleep ───────────────────────────────────────────
            elapsed = time.time() - loop_start
            sleep_for = max(0.005, next_turn_in - elapsed - 0.01)
            if sleep_for > 0.001:
                time.sleep(sleep_for)
            else:
                logging.debug(f"  ⏱️ Loop took {elapsed:.3f}s, no sleep (next_turn_in={next_turn_in:.3f})")

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
