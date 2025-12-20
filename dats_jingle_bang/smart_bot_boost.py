import os
import math
import time
import random
import logging
import requests
from dotenv import load_dotenv

# Настройка логирования
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
    logging.error("Токен не найден! Убедитесь, что файл .env содержит AUTH_TOKEN")
    exit(1)

# Глобальные переменные для управления состоянием
decision_cycle = 0
last_booster_check = 0
BOOSTER_CHECK_INTERVAL = 30  # Проверять бустеры каждые 30 циклов

# ==================== ФУНКЦИИ ДЛЯ РАБОТЫ С БУСТЕРАМИ ====================

def get_available_boosters():
    """Получает список доступных бустеров и текущие очки."""
    try:
        response = requests.get(f"{BASE_URL}/api/booster", headers=HEADERS, timeout=5)
        if response.status_code == 200:
            return response.json()
        else:
            logging.error(f"Ошибка получения бустеров: {response.status_code}")
            return None
    except Exception as e:
        logging.error(f"Исключение при запросе бустеров: {e}")
        return None

def purchase_booster(booster_type):
    """Покупает указанный бустер (booster_type - числовой идентификатор)."""
    try:
        payload = {"booster": booster_type}
        response = requests.post(f"{BASE_URL}/api/booster", headers=HEADERS, json=payload, timeout=5)
        if response.status_code == 200:
            logging.info(f"✅ Куплен бустер типа {booster_type}")
            return True
        else:
            logging.error(f"Ошибка покупки бустера {booster_type}: {response.status_code}")
            return False
    except Exception as e:
        logging.error(f"Исключение при покупке бустера: {e}")
        return False

def booster_management_logic(state):
    """Логика управления бустерами. Вызывается периодически."""
    global last_booster_check
    
    # Проверяем, нужно ли проверять бустеры сейчас
    current_cycle = decision_cycle
    if current_cycle - last_booster_check < BOOSTER_CHECK_INTERVAL:
        return
    
    last_booster_check = current_cycle
    boosters_data = get_available_boosters()
    
    if not boosters_data:
        return
    
    available_points = boosters_data.get('state', {}).get('points', 0)
    
    if available_points <= 0:
        return
    
    # Анализируем текущее состояние для принятия решения
    bombers = state.get('bombers', [])
    alive_bombers = [b for b in bombers if b.get('alive')]
    total_deaths = len([b for b in bombers if not b.get('alive')])
    
    # Приоритет 1: Броня (если есть частые смерти)
    if available_points >= 1 and total_deaths > 3:
        # Попробуем купить броню (предполагаемый ID = 6)
        if purchase_booster(6):
            return
    
    # Приоритет 2: Больше бомб (если у всех юнитов мало бомб)
    low_bomb_count = sum(1 for b in alive_bombers if b.get('bombs_available', 0) < 2)
    if available_points >= 1 and low_bomb_count >= 3:
        if purchase_booster(1):  # ID для "апгрейд карманов"
            return
    
    # Приоритет 3: Скорость (если юниты медленные)
    if available_points >= 1:
        if purchase_booster(3):  # ID для "апгрейд тела" (скорость)
            return
    
    # Приоритет 4: Радиус взрыва
    if available_points >= 1:
        if purchase_booster(2):  # ID для "апгрейд бомбы" (радиус)
            return

# ==================== ОСНОВНЫЕ ФУНКЦИИ ====================

def get_arena():
    """Получить текущее состояние игры."""
    try:
        response = requests.get(f"{BASE_URL}/api/arena", headers=HEADERS, timeout=5)
        if response.status_code == 200:
            return response.json()
        else:
            logging.error(f"Ошибка получения арены: {response.status_code}")
            return None
    except Exception as e:
        logging.error(f"Исключение при запросе арены: {e}")
        return None

def send_move(commands):
    """Отправить команды движения."""
    try:
        response = requests.post(f"{BASE_URL}/api/move", headers=HEADERS, json=commands, timeout=5)
        if response.status_code != 200:
            logging.error(f"Ошибка отправки хода: {response.status_code}")
        return response
    except Exception as e:
        logging.error(f"Исключение при отправке команд: {e}")
        return None

def is_position_safe(state, x, y, ignore_bombs=False):
    """Проверяет, можно ли встать на клетку (x, y)."""
    if x < 0 or y < 0 or x >= state['map_size'][0] or y >= state['map_size'][1]:
        return False
    
    if [x, y] in state.get('arena', {}).get('walls', []):
        return False
    
    if not ignore_bombs:
        for bomb in state.get('arena', {}).get('bombs', []):
            if bomb['pos'] == [x, y]:
                return False
    
    return True

def find_nearest_obstacle(state, bomber_x, bomber_y, max_distance=8):
    """Находит ближайший разрушаемый блок."""
    obstacles = state.get('arena', {}).get('obstacles', [])
    if not obstacles:
        return None
    
    nearest = None
    min_distance = float('inf')
    
    for obs in obstacles:
        obs_x, obs_y = obs
        distance = abs(obs_x - bomber_x) + abs(obs_y - bomber_y)
        
        if distance < min_distance and distance <= max_distance:
            min_distance = distance
            nearest = (obs_x, obs_y)
    
    return nearest

# ==================== БЕЗОПАСНОЕ ПЛАНИРОВАНИЕ МАРШРУТА ====================

def plan_safe_bomb_route(state, bomber, target_block):
    """
    Планирует безопасный маршрут: подойти, поставить бомбу, отойти.
    Возвращает команду для юнита или None, если безопасный путь не найден.
    """
    bomber_id = bomber['id']
    start_x, start_y = bomber['pos']
    block_x, block_y = target_block
    
    # Получаем текущий радиус взрыва бомб
    bomb_range = 1  # базовое значение
    
    # 1. Находим все безопасные клетки для установки бомбы вокруг блока
    bomb_positions = []
    directions = [(0, 1), (1, 0), (0, -1), (-1, 0)]
    
    for dx, dy in directions:
        bomb_x, bomb_y = block_x + dx, block_y + dy
        
        # Проверяем, можно ли стоять на этой клетке
        if is_position_safe(state, bomb_x, bomb_y):
            # Проверяем, что бомба будет на одной линии с блоком
            if bomb_x == block_x or bomb_y == block_y:
                bomb_positions.append((bomb_x, bomb_y))
    
    # 2. Для каждой возможной позиции бомбы ищем безопасный путь отхода
    for bomb_x, bomb_y in bomb_positions:
        # Безопасная дистанция = радиус бомбы + 1
        safe_distance = bomb_range + 1
        safe_retreat_cells = []
        
        # Проверяем клетки на безопасном расстоянии
        for check_distance in range(1, 4):  # Проверяем до 3 шагов
            for dx, dy in directions:
                retreat_x, retreat_y = bomb_x + dx * check_distance, bomb_y + dy * check_distance
                
                if is_position_safe(state, retreat_x, retreat_y):
                    # Проверяем, что клетка вне радиуса взрыва
                    manhattan_distance = abs(retreat_x - bomb_x) + abs(retreat_y - bomb_y)
                    if manhattan_distance >= safe_distance:
                        # Также проверяем, что на пути к этой клетке нет препятствий
                        path_clear = True
                        for step in range(1, check_distance):
                            step_x = bomb_x + dx * step
                            step_y = bomb_y + dy * step
                            if not is_position_safe(state, step_x, step_y):
                                path_clear = False
                                break
                        
                        if path_clear:
                            safe_retreat_cells.append((retreat_x, retreat_y, manhattan_distance))
        
        # 3. Если нашли безопасные клетки для отхода
        if safe_retreat_cells:
            # Выбираем самую дальнюю клетку для отхода
            safe_retreat_cells.sort(key=lambda pos: pos[2], reverse=True)
            retreat_x, retreat_y, _ = safe_retreat_cells[0]
            
            # Строим путь
            path = []
            
            # Если мы еще не стоим на клетке бомбы
            if (start_x, start_y) != (bomb_x, bomb_y):
                # Проверяем, можем ли мы дойти до клетки бомбы
                if is_position_safe(state, bomb_x, bomb_y):
                    path.append([bomb_x, bomb_y])
                else:
                    continue  # Не можем дойти до этой позиции бомбы
            
            # Добавляем отход
            path.append([retreat_x, retreat_y])
            
            logging.info(f"{bomber_id[:8]}: 🛡️ Безопасный маршрут к блоку ({block_x}, {block_y})")
            return {
                "id": bomber_id,
                "path": path,
                "bombs": [[bomb_x, bomb_y]] if (start_x, start_y) != (bomb_x, bomb_y) else []
            }
    
    return None  # Безопасный путь не найден

# ==================== ЛОГИКА ПОВЕДЕНИЯ ЮНИТОВ ====================

def simple_exploration_logic(state, bomber):
    """Упрощённая логика для исследования."""
    bomber_id = bomber['id']
    x, y = bomber['pos']
    
    directions = [
        (x + 1, y), (x - 1, y),
        (x, y + 1), (x, y - 1)
    ]
    
    safe_dirs = [(nx, ny) for nx, ny in directions if is_position_safe(state, nx, ny)]
    
    if safe_dirs:
        target = random.choice(safe_dirs)
        return {
            "id": bomber_id,
            "path": [list(target)],
            "bombs": []
        }
    
    return None

def find_safe_retreat(state, x, y, block_x, block_y):
    """Находит безопасное направление для отхода от блока."""
    directions = [(1, 0), (-1, 0), (0, 1), (0, -1)]
    safe_directions = []
    
    for dx, dy in directions:
        nx, ny = x + dx, y + dy
        if is_position_safe(state, nx, ny):
            # Предпочитаем отход в сторону от блока
            distance_after = abs(nx - block_x) + abs(ny - block_y)
            distance_before = abs(x - block_x) + abs(y - block_y)
            
            if distance_after > distance_before:
                safe_directions.append(((nx, ny), 2))  # Высший приоритет
            else:
                safe_directions.append(((nx, ny), 1))  # Низший приоритет
    
    if safe_directions:
        # Сортируем по приоритету
        safe_directions.sort(key=lambda x: x[1], reverse=True)
        return safe_directions[0][0]
    
    return None

def can_bomb_reach_block(bomber_x, bomber_y, block_x, block_y):
    """Проверяет, может ли бомба на позиции (bomber_x, bomber_y) уничтожить блок."""
    return bomber_x == block_x or bomber_y == block_y

def safe_bomber_logic(state, bomber):
    """
    Основная логика юнита с приоритетом на безопасность.
    """
    global decision_cycle
    bomber_id = bomber['id']
    x, y = bomber['pos']
    
    # 1. Проверяем бустеры (делаем это периодически для всех юнитов)
    decision_cycle += 1
    if decision_cycle % BOOSTER_CHECK_INTERVAL == 0:
        booster_management_logic(state)
    
    # 2. Если у юнита есть броня > 0, он может рисковать больше
    armor = bomber.get('armor', 0)
    safe_mode = armor == 0  # Режим максимальной осторожности без брони
    
    # 3. Проверяем, есть ли у юнита бомбы
    bombs_available = bomber.get('bombs_available', 1)
    if bombs_available < 1:
        return simple_exploration_logic(state, bomber)
    
    # 4. Ищем ближайший разрушаемый блок
    target_block = find_nearest_obstacle(state, x, y, max_distance=6)
    
    if target_block:
        block_x, block_y = target_block
        
        # 5. Пытаемся спланировать безопасный маршрут для атаки
        bomb_plan = plan_safe_bomb_route(state, bomber, target_block)
        
        if bomb_plan:
            return bomb_plan
        elif not safe_mode and armor > 0:
            # 6. Если безопасный путь не найден, но есть броня - можем рискнуть
            logging.warning(f"{bomber_id[:8]}: ⚠️ Рискую атаковать (броня={armor})")
            
            if can_bomb_reach_block(x, y, block_x, block_y):
                retreat = find_safe_retreat(state, x, y, block_x, block_y)
                if retreat:
                    retreat_x, retreat_y = retreat
                    return {
                        "id": bomber_id,
                        "path": [[retreat_x, retreat_y]],
                        "bombs": [[x, y]]
                    }
        
        # 7. Если не можем атаковать, пытаемся встать на линию с блоком
        possible_positions = []
        
        # Пробуем встать на ту же X или Y координату
        if is_position_safe(state, block_x, y):
            distance = abs(block_x - x)
            possible_positions.append(((block_x, y), distance, "same_x"))
        
        if is_position_safe(state, x, block_y):
            distance = abs(block_y - y)
            possible_positions.append(((x, block_y), distance, "same_y"))
        
        if possible_positions:
            possible_positions.sort(key=lambda p: p[1])
            target_pos, distance, reason = possible_positions[0]
            target_x, target_y = target_pos
            
            logging.info(f"{bomber_id[:8]}: Иду к ({target_x}, {target_y}) чтобы встать на линию с блоком")
            
            return {
                "id": bomber_id,
                "path": [[target_x, target_y]],
                "bombs": []
            }
    
    # 8. Если атака невозможна - исследуем карту
    return simple_exploration_logic(state, bomber)

# ==================== ГЛАВНЫЙ ЦИКЛ ====================

def main_loop():
    """Основной цикл бота."""
    global decision_cycle
    
    logging.info("🚀 Запуск бота с безопасной логикой и управлением бустерами")
    logging.info(f"Используем сервер: {BASE_URL}")
    
    request_count = 0
    last_reset = time.time()
    stats = {
        'total_cycles': 0,
        'bombs_placed': 0,
        'blocks_destroyed': 0,
        'last_score': 0,
        'total_deaths': 0
    }
    
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
        
        # Получаем состояние
        state = get_arena()
        if not state:
            time.sleep(0.4)
            continue
        
        request_count += 1
        stats['total_cycles'] += 1
        
        # Анализируем состояние
        current_score = state.get('raw_score', 0)
        score_diff = current_score - stats['last_score']
        stats['last_score'] = current_score
        
        if score_diff > 0:
            stats['blocks_destroyed'] += score_diff
            logging.info(f"🎉 Уничтожено блоков! +{score_diff} очков")
        
        # Формируем команды
        commands = {"bombers": []}
        bombers = state.get('bombers', [])
        
        # Подсчитываем смертность
        current_deaths = len([b for b in bombers if not b.get('alive')])
        if current_deaths > stats['total_deaths']:
            new_deaths = current_deaths - stats['total_deaths']
            stats['total_deaths'] = current_deaths
            logging.warning(f"💀 Погибло {new_deaths} юнитов. Всего смертей: {current_deaths}")
        
        # Разные юниты получают разные задачи
        alive_bombers = [b for b in bombers if b.get('alive') and b.get('can_move')]
        
        for i, bomber in enumerate(alive_bombers):
            if i < 4:  # Первые 4 живых юнита атакуют
                cmd = safe_bomber_logic(state, bomber)
            else:  # Остальные исследуют
                cmd = simple_exploration_logic(state, bomber)
            
            if cmd:
                commands['bombers'].append(cmd)
                
                # Считаем бомбы
                if cmd.get('bombs'):
                    stats['bombs_placed'] += len(cmd['bombs'])
        
        # Отправляем команды
        if commands['bombers']:
            send_move(commands)
        
        # Периодическая статистика
        if stats['total_cycles'] % 25 == 0:
            obstacles = len(state.get('arena', {}).get('obstacles', []))
            active_bombs = len(state.get('arena', {}).get('bombs', []))
            alive_count = len(alive_bombers)
            
            # Получаем информацию о бустерах
            boosters_data = get_available_boosters()
            skill_points = boosters_data.get('state', {}).get('points', 0) if boosters_data else 0
            
            logging.info(f"=== СТАТИСТИКА ===")
            logging.info(f"Циклов: {stats['total_cycles']}, Живых: {alive_count}/6")
            logging.info(f"Очки: {current_score}, Уничтожено блоков: {stats['blocks_destroyed']}")
            logging.info(f"Бомб поставлено: {stats['bombs_placed']}, Активных бомб: {active_bombs}")
            logging.info(f"Смертей: {stats['total_deaths']}, Очки навыков: {skill_points}")
            logging.info(f"Блоков в зоне видимости: {obstacles}")
        
        time.sleep(0.4)

# ==================== ТОЧКА ВХОДА ====================

if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        logging.info("Бот остановлен пользователем")
    except Exception as e:
        logging.error(f"Критическая ошибка: {e}", exc_info=True)
