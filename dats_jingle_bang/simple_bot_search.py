import os
import math
import time
import random
import requests
from collections import deque
from dotenv import load_dotenv
load_dotenv()

TOKEN = os.getenv('AUTH_TOKEN')
BASE_URL = os.getenv('TEST_URL')
HEADERS = {"X-Auth-Token": TOKEN}

def get_arena():
    """Получить текущее состояние игры."""
    response = requests.get(f"{BASE_URL}/api/arena", headers=HEADERS)
    if response.status_code == 200:
        return response.json()
    else:
        print(f"Ошибка получения арены: {response.status_code}, {response.text}")
        return None

def send_move(commands):
    """Отправить команды движения.
    commands: dict в формате {"bombers": [{"id": "id1", "path": [[x1,y1],...], "bombs": [[xb1, yb1],...]}, ...]}
    """
    response = requests.post(f"{BASE_URL}/api/move", headers=HEADERS, json=commands)
    if response.status_code != 200:
        print(f"Ошибка отправки хода: {response.status_code}, {response.text}")

def is_position_safe(state, x, y):
    """Проверяет, можно ли встать на клетку (x, y)."""
    # Проверка выхода за границы
    if x < 0 or y < 0 or x >= state['map_size'][0] or y >= state['map_size'][1]:
        return False
    
    # Проверка на стену
    if [x, y] in state.get('arena', {}).get('walls', []):
        return False
    
    # Проверка на бомбу (позже добавим)
    # if [x, y] in state.get('arena', {}).get('bombs', []):
    #     return False
    
    return True

def get_random_safe_direction(state, current_x, current_y):
    """Возвращает безопасную случайную соседнюю клетку."""
    directions = [
        (current_x + 1, current_y),   # вправо
        (current_x - 1, current_y),   # влево
        (current_x, current_y + 1),   # вверх
        (current_x, current_y - 1),   # вниз
    ]
    
    # Фильтруем только безопасные направления
    safe_directions = [(x, y) for x, y in directions if is_position_safe(state, x, y)]
    
    if safe_directions:
        return random.choice(safe_directions)
    return None  # Некуда идти

def find_nearest_obstacle(state, bomber_id, bomber_x, bomber_y):
    """Находит ближайший разрушаемый блок в зоне видимости юнита."""
    obstacles = state.get('arena', {}).get('obstacles', [])
    if not obstacles:
        return None
    
    nearest = None
    min_distance = float('inf')
    
    for obs in obstacles:
        obs_x, obs_y = obs
        # Расстояние по прямой (формула Пифагора)
        distance = math.sqrt((obs_x - bomber_x) ** 2 + (obs_y - bomber_y) ** 2)
        
        # Проверяем, находится ли блок в радиусе обзора (по умолчанию 5)
        if distance <= 5 and distance < min_distance:
            # Дополнительная проверка: не находится ли блок прямо на юните?
            if obs_x != bomber_x or obs_y != bomber_y:
                min_distance = distance
                nearest = obs
    
    return nearest

def find_path_to_target(state, start_x, start_y, target_x, target_y, max_steps=10):
    """Находит безопасный путь от (start_x, start_y) к (target_x, target_y) используя BFS."""
    
    # Если цель уже достигнута
    if start_x == target_x and start_y == target_y:
        return []
    
    # Получаем карту препятствий
    walls = set(tuple(w) for w in state.get('arena', {}).get('walls', []))
    bombs = set(tuple(b['pos']) for b in state.get('arena', {}).get('bombs', []))
    
    # Направления движения: вверх, вправо, вниз, влево
    directions = [(0, 1), (1, 0), (0, -1), (-1, 0)]
    
    # Структуры для BFS
    queue = deque()
    queue.append((start_x, start_y, []))  # (x, y, путь)
    visited = set()
    visited.add((start_x, start_y))
    
    while queue:
        x, y, path = queue.popleft()
        
        # Ограничиваем длину пути
        if len(path) >= max_steps:
            continue
        
        for dx, dy in directions:
            nx, ny = x + dx, y + dy
            
            # Проверка границ карты
            if nx < 0 or ny < 0 or nx >= state['map_size'][0] or ny >= state['map_size'][1]:
                continue
            
            # Проверка препятствий
            if (nx, ny) in walls:
                continue
            
            # Проверка бомб (пока избегаем их)
            if (nx, ny) in bombs:
                continue
            
            # Если достигли цели
            if nx == target_x and ny == target_y:
                return path + [(nx, ny)]
            
            # Если клетка свободна и не посещалась
            if (nx, ny) not in visited:
                visited.add((nx, ny))
                queue.append((nx, ny, path + [(nx, ny)]))
    
    # Путь не найден
    return None

def find_bomb_position(state, bomber_x, bomber_y, target_x, target_y):
    """Определяет, где поставить бомбу, чтобы уничтожить блок в (target_x, target_y)."""
    
    # Проверяем все 4 направления от цели
    directions = [(0, 1), (1, 0), (0, -1), (-1, 0)]
    
    for dx, dy in directions:
        bomb_x, bomb_y = target_x + dx, target_y + dy
        
        # Бомба должна быть на безопасной клетке (не стена, не за границей)
        if is_position_safe(state, bomb_x, bomb_y):
            # И должна быть смежной с текущей позицией юнита
            if abs(bomb_x - bomber_x) + abs(bomb_y - bomber_y) == 1:
                return [bomb_x, bomb_y]
    
    return None

def calculate_safe_retreat_path(state, start_x, start_y, bomb_x, bomb_y, bomb_range=1, min_distance=2):
    """
    Находит безопасный путь для отхода от бомбы.
    bomb_range: радиус взрыва бомбы (по умолчанию 1)
    min_distance: минимальное безопасное расстояние от эпицентра
    """
    walls = set(tuple(w) for w in state.get('arena', {}).get('walls', []))
    obstacles = set(tuple(o) for o in state.get('arena', {}).get('obstacles', []))
    map_width, map_height = state['map_size']
    
    # 1. Определяем зону взрыва
    blast_zone = set()
    # Вертикальная линия взрыва
    for dy in range(-bomb_range, bomb_range + 1):
        blast_zone.add((bomb_x, bomb_y + dy))
    # Горизонтальная линия взрыва
    for dx in range(-bomb_range, bomb_range + 1):
        blast_zone.add((bomb_x + dx, bomb_y))
    
    # 2. Находим безопасные клетки для отхода
    safe_cells = []
    directions = [(0, 1), (1, 0), (0, -1), (-1, 0)]
    
    # Проверяем соседние клетки от стартовой позиции
    for dx, dy in directions:
        check_x, check_y = start_x + dx, start_y + dy
        
        # Базовая проверка безопасности
        if (0 <= check_x < map_width and 
            0 <= check_y < map_height and
            (check_x, check_y) not in walls and
            (check_x, check_y) not in blast_zone):
            
            # Дополнительно: проверяем, не находится ли клетка в зоне другой бомбы
            is_safe_from_other_bombs = True
            for bomb in state.get('arena', {}).get('bombs', []):
                bx, by = bomb['pos']
                b_range = bomb.get('range', 1)
                if (abs(check_x - bx) <= b_range and check_y == by) or (abs(check_y - by) <= b_range and check_x == bx):
                    is_safe_from_other_bombs = False
                    break
            
            if is_safe_from_other_bombs:
                # Оцениваем безопасность клетки (чем дальше от бомбы - тем лучше)
                distance = abs(check_x - bomb_x) + abs(check_y - bomb_y)
                if distance >= min_distance:
                    safe_cells.append(((check_x, check_y), distance))
    
    # 3. Сортируем безопасные клетки по удалённости от бомбы
    safe_cells.sort(key=lambda x: x[1], reverse=True)
    
    # 4. Если есть безопасные клетки рядом, возвращаем кратчайший путь к лучшей
    if safe_cells:
        target_cell, _ = safe_cells[0]
        
        # Используем BFS для поиска пути к целевой клетке
        return find_path_to_target(state, start_x, start_y, target_cell[0], target_cell[1], max_steps=3)
    
    # 5. Если безопасных клеток нет рядом, ищем любую безопасную клетку дальше
    return find_any_safe_cell(state, start_x, start_y, blast_zone, walls)

def find_any_safe_cell(state, start_x, start_y, blast_zone, walls, search_radius=5):
    """Ищет любую безопасную клетку в пределах search_radius."""
    directions = [(0, 1), (1, 0), (0, -1), (-1, 0)]
    
    queue = deque()
    queue.append((start_x, start_y, []))
    visited = set()
    visited.add((start_x, start_y))
    
    while queue:
        x, y, path = queue.popleft()
        
        for dx, dy in directions:
            nx, ny = x + dx, y + dy
            
            if (nx, ny) in visited:
                continue
            
            # Проверяем границы
            if (0 <= nx < state['map_size'][0] and 
                0 <= ny < state['map_size'][1] and
                (nx, ny) not in walls and
                (nx, ny) not in blast_zone):
                
                new_path = path + [(nx, ny)]
                
                # Проверяем, что эта клетка безопасна от других бомб
                safe_from_other_bombs = True
                for bomb in state.get('arena', {}).get('bombs', []):
                    bx, by = bomb['pos']
                    b_range = bomb.get('range', 1)
                    if (abs(nx - bx) <= b_range and ny == by) or (abs(ny - by) <= b_range and nx == bx):
                        safe_from_other_bombs = False
                        break
                
                if safe_from_other_bombs and len(new_path) <= 3:
                    return new_path
                
                if len(new_path) < search_radius:
                    visited.add((nx, ny))
                    queue.append((nx, ny, new_path))
    
    return None

class BomberState:
    """Класс для отслеживания состояния каждого юнита"""
    def __init__(self, bomber_id):
        self.bomber_id = bomber_id
        self.current_target = None
        self.action = "exploring"  # exploring, approaching, planting, retreating
        self.retreat_path = None
        self.last_bomb_pos = None
        
bomber_states = {}

def smart_bomber_logic(state, bomber):
    """Основная логика для одного юнита с учётом отхода"""
    bomber_id = bomber['id']
    x, y = bomber['pos']
    
    # Инициализируем состояние юнита, если его нет
    if bomber_id not in bomber_states:
        bomber_states[bomber_id] = BomberState(bomber_id)
    
    b_state = bomber_states[bomber_id]
    
    # Если мы в режиме отхода, продолжаем отходить
    if b_state.action == "retreating" and b_state.retreat_path:
        if len(b_state.retreat_path) > 0:
            next_step = b_state.retreat_path[0]
            b_state.retreat_path = b_state.retreat_path[1:]
            
            # Если это последний шаг, переключаемся на исследование
            if len(b_state.retreat_path) == 0:
                b_state.action = "exploring"
                b_state.last_bomb_pos = None
            
            return {
                "id": bomber_id,
                "path": [list(next_step)],
                "bombs": []
            }
    
    # Ищем ближайший блок для уничтожения
    target = find_nearest_obstacle(state, bomber_id, x, y)
    
    if target:
        target_x, target_y = target
        print(f"  {bomber_id[:8]}: Цель - блок ({target_x}, {target_y})")
        
        # Определяем позицию для бомбы
        bomb_pos = find_bomb_position(state, x, y, target_x, target_y)
        
        # Если мы уже рядом и можем поставить бомбу
        if bomb_pos and abs(bomb_pos[0] - x) + abs(bomb_pos[1] - y) == 1:
            print(f"    Ставим бомбу в {bomb_pos} и отходим!")
            
            # Находим путь для отхода
            retreat_path = calculate_safe_retreat_path(state, x, y, bomb_pos[0], bomb_pos[1])
            
            if retreat_path:
                # Устанавливаем состояние отхода
                b_state.action = "retreating"
                b_state.retreat_path = retreat_path
                b_state.last_bomb_pos = bomb_pos
                
                # Отдаём команду: поставить бомбу и начать отход
                return {
                    "id": bomber_id,
                    "path": [list(retreat_path[0])] if retreat_path else [],
                    "bombs": [bomb_pos]
                }
            else:
                # Не нашли безопасного отхода - не ставим бомбу
                print(f"    ⚠️ Небезопасно! Не ставим бомбу")
                # Ищем новое направление
                new_target = get_random_safe_direction(state, x, y)
                if new_target:
                    return {
                        "id": bomber_id,
                        "path": [list(new_target)],
                        "bombs": []
                    }
        
        # Если не рядом с целью, идём к ней
        path = find_path_to_target(state, x, y, target_x, target_y, max_steps=6)
        if path:
            # Берем только первые 2 шага
            short_path = path[:2]
            return {
                "id": bomber_id,
                "path": short_path,
                "bombs": []
            }
    
    # Если нет целей или не можем к ним подойти - исследуем
    new_target = get_random_safe_direction(state, x, y)
    if new_target:
        return {
            "id": bomber_id,
            "path": [list(new_target)],
            "bombs": []
        }
    
    # Если совсем некуда идти - стоим на месте
    return {
        "id": bomber_id,
        "path": [],
        "bombs": []
    }

def safe_bot_loop():
    """Основной цикл бота с безопасным отходом"""
    request_count = 0
    last_reset = time.time()
    
    while True:
        # Соблюдение лимита 3 запроса в секунду
        current_time = time.time()
        if current_time - last_reset >= 1.0:
            request_count = 0
            last_reset = current_time
        
        if request_count >= 3:
            time.sleep(1.0 - (current_time - last_reset))
            continue
            
        state = get_arena()
        if not state:
            time.sleep(0.4)
            continue
            
        request_count += 1
        
        # Логирование
        alive_bombers = [b for b in state['bombers'] if b['alive'] and b['can_move']]
        active_bombs = len(state.get('arena', {}).get('bombs', []))
        
        print(f"[{state.get('round', '?')}] Очки: {state.get('raw_score', 0)}, "
              f"Юнитов: {len(alive_bombers)}, Активных бомб: {active_bombs}")
        
        commands = {"bombers": []}
        
        for bomber in alive_bombers:
            bomber_command = smart_bomber_logic(state, bomber)
            if bomber_command:
                commands['bombers'].append(bomber_command)
        
        # Отправляем команды, если они есть
        if commands['bombers']:
            send_move(commands)
            # Логируем действия каждого юнита
            for cmd in commands['bombers']:
                action = "движется" if cmd['path'] else "стоит"
                bombs = f", ставит бомбу" if cmd['bombs'] else ""
                print(f"  {cmd['id'][:8]}: {action}{bombs}")
        
        time.sleep(0.4)

if __name__ == "__main__":
    safe_bot_loop()
