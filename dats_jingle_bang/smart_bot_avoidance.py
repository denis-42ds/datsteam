import os
import math
import time
import random
import logging
import requests
from collections import deque
from dotenv import load_dotenv

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)

# Загрузка переменных окружения
load_dotenv()

TOKEN = os.getenv('AUTH_TOKEN')
BASE_URL = os.getenv('TEST_URL')
HEADERS = {"X-Auth-Token": TOKEN}

# Проверка обязательных переменных
if not TOKEN:
    logging.error("Токен не найден! Убедитесь, что файл .env содержит AUTH_TOKEN")
    exit(1)

# ==================== БАЗОВЫЕ ФУНКЦИИ ====================

def get_arena():
    """Получить текущее состояние игры."""
    try:
        response = requests.get(f"{BASE_URL}/api/arena", headers=HEADERS, timeout=5)
        if response.status_code == 200:
            return response.json()
        else:
            logging.error(f"Ошибка получения арены: {response.status_code}, {response.text}")
            return None
    except Exception as e:
        logging.error(f"Исключение при запросе арены: {e}")
        return None

def send_move(commands):
    """Отправить команды движения."""
    try:
        response = requests.post(f"{BASE_URL}/api/move", headers=HEADERS, json=commands, timeout=5)
        if response.status_code != 200:
            logging.error(f"Ошибка отправки хода: {response.status_code}, {response.text}")
        return response
    except Exception as e:
        logging.error(f"Исключение при отправке команд: {e}")
        return None

# ==================== ФУНКЦИИ БЕЗОПАСНОСТИ И НАВИГАЦИИ ====================

def is_position_safe(state, x, y, ignore_bombs=False):
    """Проверяет, можно ли встать на клетку (x, y)."""
    # Проверка выхода за границы
    if x < 0 or y < 0 or x >= state['map_size'][0] or y >= state['map_size'][1]:
        return False
    
    # Проверка на стену
    if [x, y] in state.get('arena', {}).get('walls', []):
        return False
    
    # Проверка на бомбу (если не игнорируем)
    if not ignore_bombs:
        for bomb in state.get('arena', {}).get('bombs', []):
            if bomb['pos'] == [x, y]:
                return False
    
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
    return None

def find_nearest_obstacle(state, bomber_id, bomber_x, bomber_y):
    """Находит ближайший разрушаемый блок в зоне видимости юнита."""
    obstacles = state.get('arena', {}).get('obstacles', [])
    if not obstacles:
        return None
    
    nearest = None
    min_distance = float('inf')
    
    for obs in obstacles:
        obs_x, obs_y = obs
        # Манхэттенское расстояние (быстрее вычислять)
        distance = abs(obs_x - bomber_x) + abs(obs_y - bomber_y)
        
        # Проверяем, находится ли блок в радиусе обзора (по умолчанию 5)
        if distance <= 5 and distance < min_distance:
            # Дополнительная проверка: не находится ли блок прямо на юните?
            if obs_x != bomber_x or obs_y != bomber_y:
                min_distance = distance
                nearest = (obs_x, obs_y)
    
    return nearest

def find_path_to_target(state, start_x, start_y, target_x, target_y, max_steps=10):
    """Находит безопасный путь от (start_x, start_y) к (target_x, target_y) используя BFS."""
    # Если цель уже достигнута
    if start_x == target_x and start_y == target_y:
        return []
    
    # Получаем карту препятствий
    walls = set(tuple(w) for w in state.get('arena', {}).get('walls', []))
    bombs = [b for b in state.get('arena', {}).get('bombs', [])]
    bomb_positions = set(tuple(b['pos']) for b in bombs)
    
    # Направления движения
    directions = [(0, 1), (1, 0), (0, -1), (-1, 0)]
    
    # Структуры для BFS
    queue = deque()
    queue.append((start_x, start_y, []))
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
            
            # Проверка бомб
            if (nx, ny) in bomb_positions:
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

# ==================== ФУНКЦИИ ДЛЯ РАБОТЫ С МОБАМИ ====================

def get_mob_threat_level(state, x, y):
    """Оценивает уровень угрозы от мобов в позиции (x, y)."""
    mobs = state.get('mobs', [])
    if not mobs:
        return 0
    
    threat = 0
    for mob in mobs:
        mob_x, mob_y = mob['pos']
        mob_type = mob.get('type', 'patrol')
        
        # Манхэттенское расстояние
        distance = abs(mob_x - x) + abs(mob_y - y)
        
        # Разная опасность в зависимости от типа моба
        if mob_type == 'ghost':
            # Призраки опаснее и имеют больший радиус обзора
            if distance <= 10:
                threat += max(0, 15 - distance)
        else:  # patrol
            if distance <= 5:
                threat += max(0, 8 - distance)
    
    return threat

def find_mob_free_direction(state, current_x, current_y):
    """Находит направление, максимально удалённое от мобов."""
    directions = [
        (current_x + 1, current_y),
        (current_x - 1, current_y),
        (current_x, current_y + 1),
        (current_x, current_y - 1),
    ]
    
    safe_directions = []
    for x, y in directions:
        if is_position_safe(state, x, y):
            threat = get_mob_threat_level(state, x, y)
            safe_directions.append(((x, y), threat))
    
    if not safe_directions:
        return None
    
    # Выбираем направление с минимальной угрозой
    safe_directions.sort(key=lambda item: item[1])
    return safe_directions[0][0]

# ==================== ФУНКЦИИ ДЛЯ УСТАНОВКИ БОМБ И ОТХОДА ====================

def calculate_safe_retreat_path(state, start_x, start_y, bomb_x, bomb_y, bomb_range=1, min_distance=2):
    """
    Находит безопасный путь для отхода от бомбы.
    bomb_range: радиус взрыва бомбы (по умолчанию 1)
    min_distance: минимальное безопасное расстояние от эпицентра
    """
    walls = set(tuple(w) for w in state.get('arena', {}).get('walls', []))
    map_width, map_height = state['map_size']
    
    # Определяем зону взрыва
    blast_zone = set()
    # Вертикальная линия взрыва
    for dy in range(-bomb_range, bomb_range + 1):
        blast_zone.add((bomb_x, bomb_y + dy))
    # Горизонтальная линия взрыва
    for dx in range(-bomb_range, bomb_range + 1):
        blast_zone.add((bomb_x + dx, bomb_y))
    
    # Находим безопасные клетки для отхода
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
            
            # Проверяем, не находится ли клетка в зоне других бомб
            is_safe_from_other_bombs = True
            for bomb in state.get('arena', {}).get('bombs', []):
                bx, by = bomb['pos']
                b_range = bomb.get('range', 1)
                # Проверяем, находится ли клетка на линии взрыва другой бомбы
                if (abs(check_x - bx) <= b_range and check_y == by) or (abs(check_y - by) <= b_range and check_x == bx):
                    is_safe_from_other_bombs = False
                    break
            
            if is_safe_from_other_bombs:
                # Оцениваем безопасность клетки (чем дальше от бомбы - тем лучше)
                distance = abs(check_x - bomb_x) + abs(check_y - bomb_y)
                if distance >= min_distance:
                    safe_cells.append(((check_x, check_y), distance))
    
    # Сортируем безопасные клетки по удалённости от бомбы
    safe_cells.sort(key=lambda x: x[1], reverse=True)
    
    # Если есть безопасные клетки рядом, возвращаем кратчайший путь к лучшей
    if safe_cells:
        target_cell, _ = safe_cells[0]
        # Используем BFS для поиска пути к целевой клетке
        return find_path_to_target(state, start_x, start_y, target_cell[0], target_cell[1], max_steps=3)
    
    # Если безопасных клеток нет рядом, ищем любую безопасную клетку дальше
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

def find_optimal_bomb_position(state, bomber_x, bomber_y, target_x, target_y):
    """
    Находит оптимальную позицию для бомбы, чтобы уничтожить блок.
    Возвращает (bomb_pos, safe_to_plant)
    """
    # Все возможные позиции для бомбы вокруг цели
    possible_positions = []
    directions = [(0, 1), (1, 0), (0, -1), (-1, 0)]
    
    for dx, dy in directions:
        bomb_x, bomb_y = target_x + dx, target_y + dy
        
        # Проверяем базовую безопасность позиции
        if not is_position_safe(state, bomb_x, bomb_y):
            continue
        
        # Проверяем, что от этой позиции луч взрыва достигнет цели
        # (бомба должна быть на одной линии с целью)
        if bomb_x == target_x or bomb_y == target_y:
            # Вычисляем расстояние от юнита до позиции бомбы
            distance_to_bomber = abs(bomb_x - bomber_x) + abs(bomb_y - bomber_y)
            
            # Оцениваем безопасность отхода
            retreat_path = calculate_safe_retreat_path(state, bomber_x, bomber_y, bomb_x, bomb_y)
            retreat_score = len(retreat_path) if retreat_path else 0
            
            # Оцениваем угрозу от мобов в этой позиции
            mob_threat = get_mob_threat_level(state, bomb_x, bomb_y)
            
            # Общая оценка позиции (чем выше score, тем лучше)
            score = 100 - distance_to_bomber * 10 + retreat_score * 20 - mob_threat * 5
            
            possible_positions.append({
                'pos': [bomb_x, bomb_y],
                'distance': distance_to_bomber,
                'retreat_score': retreat_score,
                'mob_threat': mob_threat,
                'score': score
            })
    
    if not possible_positions:
        logging.debug(f"Не найдено возможных позиций для бомбы у блока ({target_x}, {target_y})")
        return None, False
    
    # Выбираем лучшую позицию
    best_position = max(possible_positions, key=lambda p: p['score'])
    
    # Проверяем, может ли юнит безопасно поставить бомбу
    # Юнит должен быть в соседней клетке с позицией бомбы
    bomber_distance = abs(best_position['pos'][0] - bomber_x) + abs(best_position['pos'][1] - bomber_y)
    can_plant_safely = (bomber_distance == 1 and 
                        best_position['retreat_score'] > 0 and
                        best_position['mob_threat'] < 5)
    
    logging.debug(f"Лучшая позиция для бомбы: {best_position['pos']}, score={best_position['score']}, safe={can_plant_safely}")
    
    return best_position['pos'], can_plant_safely

# ==================== КЛАСС И ЛОГИКА СОСТОЯНИЯ ЮНИТОВ ====================

class BomberState:
    """Класс для отслеживания состояния каждого юнита"""
    def __init__(self, bomber_id):
        self.bomber_id = bomber_id
        self.current_target = None
        self.action = "exploring"  # exploring, approaching, planting, retreating, fleeing
        self.retreat_path = None
        self.last_bomb_pos = None
        self.last_action_time = time.time()

# Глобальный словарь состояний юнитов
bomber_states = {}

def smart_bomber_logic_with_mobs(state, bomber):
    """Улучшенная логика с избеганием мобов и правильной установкой бомб."""
    bomber_id = bomber['id']
    x, y = bomber['pos']
    
    # Инициализируем состояние юнита
    if bomber_id not in bomber_states:
        bomber_states[bomber_id] = BomberState(bomber_id)
    
    b_state = bomber_states[bomber_id]
    b_state.last_action_time = time.time()
    
    # Проверяем угрозу от мобов в текущей позиции
    current_threat = get_mob_threat_level(state, x, y)
    if current_threat > 7:  # Высокая угроза - убегаем!
        logging.warning(f"{bomber_id[:8]}: Высокая угроза от мобов ({current_threat})!")
        safe_direction = find_mob_free_direction(state, x, y)
        if safe_direction:
            b_state.action = "fleeing"
            return {
                "id": bomber_id,
                "path": [list(safe_direction)],
                "bombs": []
            }
    
    # Если в режиме отхода, продолжаем
    if b_state.action == "retreating" and b_state.retreat_path:
        if b_state.retreat_path:
            next_step = b_state.retreat_path[0]
            b_state.retreat_path = b_state.retreat_path[1:]
            if len(b_state.retreat_path) == 0:
                b_state.action = "exploring"
                b_state.last_bomb_pos = None
            return {
                "id": bomber_id,
                "path": [list(next_step)],
                "bombs": []
            }
    
    # Ищем цель
    target = find_nearest_obstacle(state, bomber_id, x, y)
    
    if target:
        target_x, target_y = target
        
        # Находим оптимальную позицию для бомбы
        bomb_pos, can_plant = find_optimal_bomb_position(state, x, y, target_x, target_y)
        
        if bomb_pos and can_plant:
            logging.info(f"{bomber_id[:8]}: Ставим бомбу в {bomb_pos} для блока ({target_x}, {target_y})")
            
            # Находим путь отхода
            retreat_path = calculate_safe_retreat_path(state, x, y, bomb_pos[0], bomb_pos[1])
            
            if retreat_path and len(retreat_path) > 0:
                b_state.action = "retreating"
                b_state.retreat_path = retreat_path
                b_state.last_bomb_pos = bomb_pos
                
                # Команда: поставить бомбу и сделать первый шаг отхода
                return {
                    "id": bomber_id,
                    "path": [list(retreat_path[0])],
                    "bombs": [bomb_pos]
                }
            else:
                logging.warning(f"{bomber_id[:8]}: Не найден безопасный отход, пропускаем")
        
        # Если не можем поставить бомбу, идём к цели
        path = find_path_to_target(state, x, y, target_x, target_y, max_steps=4)
        if path:
            # Проверяем, нет ли на пути мобов
            safe_path = []
            for step in path[:2]:  # Берем первые 2 шага
                step_threat = get_mob_threat_level(state, step[0], step[1])
                if step_threat < 10:  # Приемлемая угроза
                    safe_path.append(list(step))
                else:
                    break
            
            if safe_path:
                b_state.action = "approaching"
                return {
                    "id": bomber_id,
                    "path": safe_path,
                    "bombs": []
                }
    
    # Если нет целей или высокий уровень угрозы - исследуем, избегая мобов
    mob_free_dir = find_mob_free_direction(state, x, y)
    if mob_free_dir:
        b_state.action = "exploring"
        return {
            "id": bomber_id,
            "path": [list(mob_free_dir)],
            "bombs": []
        }
    
    # Резервный вариант
    random_dir = get_random_safe_direction(state, x, y)
    if random_dir:
        b_state.action = "exploring"
        return {
            "id": bomber_id,
            "path": [list(random_dir)],
            "bombs": []
        }
    
    # Если совсем некуда идти
    b_state.action = "waiting"
    return {
        "id": bomber_id,
        "path": [],
        "bombs": []
    }

# ==================== ГЛАВНЫЙ ЦИКЛ БОТА ====================

def improved_bot_loop():
    """Главный цикл с мониторингом эффективности."""
    request_count = 0
    last_reset = time.time()
    last_score = 0
    bomb_attempts = 0
    successful_bombs = 0
    round_start_time = time.time()
    
    logging.info("🚀 Запуск улучшенного бота с избеганием мобов...")
    logging.info(f"Используем сервер: {BASE_URL}")
    
    while True:
        # Соблюдение лимита 3 запроса в секунду
        current_time = time.time()
        if current_time - last_reset >= 1.0:
            request_count = 0
            last_reset = current_time
        
        if request_count >= 3:
            sleep_time = 1.0 - (current_time - last_reset)
            if sleep_time > 0:
                time.sleep(sleep_time)
            continue
            
        # Получаем состояние игры
        state = get_arena()
        if not state:
            time.sleep(0.4)
            continue
            
        request_count += 1
        
        # Статистика
        current_score = state.get('raw_score', 0)
        score_diff = current_score - last_score
        last_score = current_score
        
        alive_bombers = [b for b in state['bombers'] if b['alive']]
        alive_count = len(alive_bombers)
        mobs_count = len(state.get('mobs', []))
        bombs_count = len(state.get('arena', {}).get('bombs', []))
        obstacles_count = len(state.get('arena', {}).get('obstacles', []))
        
        # Логируем основную статистику каждые 10 секунд
        if int(current_time - round_start_time) % 10 == 0:
            logging.info(f"=== Статистика ===")
            logging.info(f"Раунд: {state.get('round', 'N/A')}")
            logging.info(f"Очки: {current_score} (изменение: {score_diff})")
            logging.info(f"Юнитов: {alive_count}/6, Мобов: {mobs_count}")
            logging.info(f"Активных бомб: {bombs_count}, Видимых блоков: {obstacles_count}")
            if bomb_attempts > 0:
                success_rate = (successful_bombs / bomb_attempts) * 100
                logging.info(f"Эффективность бомб: {successful_bombs}/{bomb_attempts} ({success_rate:.1f}%)")
        
        # Формируем команды для всех живых и активных юнитов
        commands = {"bombers": []}
        active_bombers = 0
        
        for bomber in alive_bombers:
            if bomber['can_move']:
                cmd = smart_bomber_logic_with_mobs(state, bomber)
                if cmd:
                    commands['bombers'].append(cmd)
                    active_bombers += 1
                    
                    # Считаем попытки установки бомб
                    if cmd.get('bombs') and len(cmd['bombs']) > 0:
                        bomb_attempts += 1
                        successful_bombs += 1
                        logging.debug(f"{bomber['id'][:8]}: команда на установку бомбы")
        
        # Отправляем команды, если они есть
        if commands['bombers']:
            response = send_move(commands)
            if response and response.status_code == 200:
                logging.debug(f"Успешно отправлены команды для {active_bombers} юнитов")
        
        # Спим, соблюдая лимит запросов
        time.sleep(0.4)

# ==================== ТОЧКА ВХОДА ====================

if __name__ == "__main__":
    try:
        improved_bot_loop()
    except KeyboardInterrupt:
        logging.info("Бот остановлен пользователем")
    except Exception as e:
        logging.error(f"Критическая ошибка: {e}", exc_info=True)
