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

# ==================== УЛУЧШЕННАЯ ЛОГИКА ====================

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

def can_bomb_reach_block(bomber_x, bomber_y, block_x, block_y):
    """Проверяет, может ли бомба на позиции (bomber_x, bomber_y) уничтожить блок."""
    # Бомба уничтожает блок, если они на одной линии (одинаковая X или Y координата)
    # И расстояние по прямой (только по одной оси) не важно - взрыв идёт по всей линии
    return bomber_x == block_x or bomber_y == block_y

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

def smart_destruction_logic(state, bomber):
    """Умная логика разрушения блоков."""
    bomber_id = bomber['id']
    x, y = bomber['pos']
    bombs_available = bomber.get('bombs_available', 1)
    
    # Если нет бомб, просто исследуем
    if bombs_available < 1:
        return simple_exploration_logic(state, bomber)
    
    # Ищем ближайший разрушаемый блок
    obstacles = state.get('arena', {}).get('obstacles', [])
    
    if not obstacles:
        return simple_exploration_logic(state, bomber)
    
    # Находим самый близкий блок
    nearest_block = None
    min_distance = float('inf')
    
    for block_x, block_y in obstacles:
        distance = abs(block_x - x) + abs(block_y - y)
        if distance < min_distance and distance <= 5:
            min_distance = distance
            nearest_block = (block_x, block_y)
    
    if not nearest_block:
        return simple_exploration_logic(state, bomber)
    
    block_x, block_y = nearest_block
    
    # Проверяем, можем ли мы уже уничтожить этот блок
    if can_bomb_reach_block(x, y, block_x, block_y):
        # Мы на линии с блоком! Можем поставить бомбу
        logging.info(f"{bomber_id[:8]}: 💣 Могу уничтожить блок ({block_x}, {block_y}) с позиции ({x}, {y})")
        
        # Находим безопасный отход
        retreat = find_safe_retreat(state, x, y, block_x, block_y)
        
        if retreat:
            retreat_x, retreat_y = retreat
            return {
                "id": bomber_id,
                "path": [[retreat_x, retreat_y]],
                "bombs": [[x, y]]
            }
        else:
            logging.warning(f"{bomber_id[:8]}: Не могу безопасно отойти, не ставлю бомбу")
            return simple_exploration_logic(state, bomber)
    else:
        # Нужно встать на линию с блоком
        # Пробуем встать на ту же X или Y координату
        possible_positions = []
        
        # Вариант 1: встать на ту же X координату
        if is_position_safe(state, block_x, y):
            distance = abs(block_x - x)
            possible_positions.append(((block_x, y), distance, "same_x"))
        
        # Вариант 2: встать на ту же Y координату
        if is_position_safe(state, x, block_y):
            distance = abs(block_y - y)
            possible_positions.append(((x, block_y), distance, "same_y"))
        
        if possible_positions:
            # Выбираем ближайшую позицию
            possible_positions.sort(key=lambda p: p[1])
            target_pos, distance, reason = possible_positions[0]
            target_x, target_y = target_pos
            
            logging.info(f"{bomber_id[:8]}: Иду к ({target_x}, {target_y}) чтобы встать на линию с блоком")
            
            return {
                "id": bomber_id,
                "path": [[target_x, target_y]],
                "bombs": []
            }
    
    # Если ничего не подошло - исследуем
    return simple_exploration_logic(state, bomber)

# ==================== ГЛАВНЫЙ ЦИКЛ ====================

def main_loop():
    """Основной цикл бота."""
    logging.info("🚀 Запуск улучшенного бота с умной логикой разрушения")
    
    request_count = 0
    last_reset = time.time()
    stats = {
        'total_cycles': 0,
        'bombs_placed': 0,
        'blocks_destroyed': 0,
        'last_score': 0
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
            logging.info(f"🎉 Уничтожено блоков! +{score_diff} очков, всего: {stats['blocks_destroyed']}")
        
        # Формируем команды
        commands = {"bombers": []}
        bombers = state.get('bombers', [])
        
        # Разные юниты получают разные задачи
        for i, bomber in enumerate(bombers):
            if bomber.get('alive') and bomber.get('can_move'):
                if i < 3:  # Первые 3 юнита разрушают блоки
                    cmd = smart_destruction_logic(state, bomber)
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
        if stats['total_cycles'] % 20 == 0:
            obstacles = len(state.get('arena', {}).get('obstacles', []))
            bombs = len(state.get('arena', {}).get('bombs', []))
            alive = sum(1 for b in bombers if b.get('alive'))
            
            logging.info(f"=== СТАТИСТИКА ===")
            logging.info(f"Циклов: {stats['total_cycles']}, Живых: {alive}/6")
            logging.info(f"Очки: {current_score}, Блоков видно: {obstacles}")
            logging.info(f"Бомб поставлено: {stats['bombs_placed']}")
            logging.info(f"Блоков уничтожено: {stats['blocks_destroyed']}")
            logging.info(f"Активных бомб: {bombs}")
        
        time.sleep(0.4)

# ==================== ТОЧКА ВХОДА ====================

if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        logging.info("Бот остановлен пользователем")
    except Exception as e:
        logging.error(f"Критическая ошибка: {e}", exc_info=True)
