import os
import math
import time
import random
import logging
import requests
from collections import deque
from dotenv import load_dotenv

# Настройка логирования с большей детализацией
logging.basicConfig(
    level=logging.DEBUG,  # Изменено на DEBUG для детальной диагностики
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)

# Загрузка переменных окружения
load_dotenv()

TOKEN = os.getenv('AUTH_TOKEN')
BASE_URL = os.getenv('TEST_URL')
HEADERS = {"X-Auth-Token": TOKEN}

if not TOKEN:
    logging.error("Токен не найден! Убедитесь, что файл .env содержит AUTH_TOKEN")
    exit(1)

# ==================== ДИАГНОСТИЧЕСКИЕ ФУНКЦИИ ====================

def analyze_game_state(state):
    """Анализирует состояние игры для диагностики."""
    if not state:
        return
    
    arena = state.get('arena', {})
    logging.info("=" * 50)
    logging.info("ДИАГНОСТИКА СОСТОЯНИЯ ИГРЫ:")
    logging.info(f"Размер карты: {state.get('map_size')}")
    logging.info(f"Раунд: {state.get('round')}")
    logging.info(f"Очки: {state.get('raw_score')}")
    
    # Объекты в зоне видимости
    walls = arena.get('walls', [])
    obstacles = arena.get('obstacles', [])
    bombs = arena.get('bombs', [])
    enemies = state.get('enemies', [])
    mobs = state.get('mobs', [])
    
    logging.info(f"Стены в зоне видимости: {len(walls)}")
    logging.info(f"Разрушаемые блоки в зоне видимости: {len(obstacles)}")
    logging.info(f"Бомбы в зоне видимости: {len(bombs)}")
    logging.info(f"Враги в зоне видимости: {len(enemies)}")
    logging.info(f"Мобы в зоне видимости: {len(mobs)}")
    
    # Состояние юнитов
    bombers = state.get('bombers', [])
    alive_count = sum(1 for b in bombers if b.get('alive'))
    can_move_count = sum(1 for b in bombers if b.get('can_move'))
    logging.info(f"Юнитов всего: {len(bombers)}, живых: {alive_count}, могут двигаться: {can_move_count}")
    
    # Позиции первых двух юнитов
    for i, bomber in enumerate(bombers[:2]):
        if bomber.get('alive'):
            pos = bomber.get('pos', [0, 0])
            logging.info(f"Юнит {i+1}: позиция {pos}, может двигаться: {bomber.get('can_move')}")
    
    # Если есть блоки, покажем ближайшие
    if obstacles:
        if bombers:
            first_bomber_pos = bombers[0].get('pos', [0, 0])
            bx, by = first_bomber_pos
            distances = []
            for obs in obstacles[:5]:  # Первые 5 блоков
                ox, oy = obs
                dist = abs(ox - bx) + abs(oy - by)
                distances.append((dist, obs))
            distances.sort()
            logging.info(f"Ближайшие блоки от первого юнита:")
            for dist, (ox, oy) in distances[:3]:
                logging.info(f"  Блок ({ox}, {oy}) - расстояние {dist}")
    
    logging.info("=" * 50)

# ==================== ОСНОВНЫЕ ФУНКЦИИ ====================

def get_arena():
    """Получить текущее состояние игры."""
    try:
        response = requests.get(f"{BASE_URL}/api/arena", headers=HEADERS, timeout=5)
        if response.status_code == 200:
            data = response.json()
            # Периодически анализируем состояние для отладки
            if random.random() < 0.1:  # 10% chance
                analyze_game_state(data)
            return data
        else:
            logging.error(f"Ошибка получения арены: {response.status_code}")
            return None
    except Exception as e:
        logging.error(f"Исключение при запросе арены: {e}")
        return None

def send_move(commands):
    """Отправить команды движения с логированием."""
    try:
        # Логируем, что отправляем
        if commands.get('bombers'):
            for cmd in commands['bombers']:
                if cmd.get('bombs'):
                    logging.info(f"ОТПРАВКА: {cmd['id'][:8]} ставит бомбу в {cmd['bombs']}")
        
        response = requests.post(f"{BASE_URL}/api/move", headers=HEADERS, json=commands, timeout=5)
        
        if response.status_code != 200:
            logging.error(f"Ошибка отправки хода: {response.status_code} - {response.text[:100]}")
        else:
            logging.debug(f"Команды успешно отправлены")
            
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

# ==================== УПРОЩЁННАЯ ЛОГИКА ДЛЯ ТЕСТИРОВАНИЯ ====================

def simple_exploration_logic(state, bomber):
    """Упрощённая логика для тестирования - только исследование."""
    bomber_id = bomber['id']
    x, y = bomber['pos']
    
    # Просто двигаемся в случайном безопасном направлении
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

def find_and_destroy_logic(state, bomber):
    """Логика поиска и уничтожения блоков (упрощённая)."""
    bomber_id = bomber['id']
    x, y = bomber['pos']
    
    # Ищем ближайший блок
    obstacles = state.get('arena', {}).get('obstacles', [])
    
    if obstacles:
        # Находим самый близкий блок
        nearest = None
        min_dist = float('inf')
        
        for ox, oy in obstacles:
            dist = abs(ox - x) + abs(oy - y)
            if dist < min_dist and dist <= 10:  # Увеличенный радиус
                min_dist = dist
                nearest = (ox, oy)
        
        if nearest:
            ox, oy = nearest
            logging.info(f"{bomber_id[:8]}: Нашёл блок в ({ox}, {oy}), расстояние {min_dist}")
            
            # Если стоим рядом с блоком (в соседней клетке)
            if abs(ox - x) + abs(oy - y) == 1:
                # Определяем, с какой стороны от блока мы стоим
                # И ставим бомбу на своей текущей позиции
                logging.info(f"{bomber_id[:8]}: 💣 Стою рядом с блоком, ставлю бомбу!")
                
                # Находим безопасное направление для отхода
                safe_directions = []
                directions = [(1, 0), (-1, 0), (0, 1), (0, -1)]
                
                for dx, dy in directions:
                    nx, ny = x + dx, y + dy
                    if is_position_safe(state, nx, ny):
                        safe_directions.append((nx, ny))
                
                if safe_directions:
                    retreat = random.choice(safe_directions)
                    return {
                        "id": bomber_id,
                        "path": [list(retreat)],
                        "bombs": [[x, y]]  # Бомба на текущей позиции
                    }
    
    # Если нет блоков или не можем их атаковать - исследуем
    return simple_exploration_logic(state, bomber)

# ==================== ТЕСТОВЫЙ РЕЖИМ ====================

def test_mode_bot_loop():
    """Тестовый режим для диагностики проблемы."""
    logging.info("🚀 ЗАПУСК ТЕСТОВОГО РЕЖИМА")
    logging.info("Цель: понять, почему бот не видит блоки")
    
    request_count = 0
    last_reset = time.time()
    
    # Счётчики для статистики
    cycles_with_obstacles = 0
    total_cycles = 0
    
    while True:
        # Лимит запросов
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
            time.sleep(0.5)
            continue
        
        request_count += 1
        total_cycles += 1
        
        # Анализируем состояние
        arena = state.get('arena', {})
        obstacles = arena.get('obstacles', [])
        
        if obstacles:
            cycles_with_obstacles += 1
            obstacle_percentage = (cycles_with_obstacles / total_cycles) * 100
            
            logging.info(f"Цикл {total_cycles}: ВИЖУ {len(obstacles)} БЛОКОВ!")
            logging.info(f"Процент циклов с блоками: {obstacle_percentage:.1f}%")
            
            # Покажем информацию о первом блоке
            if obstacles:
                ox, oy = obstacles[0]
                
                # Найдём ближайшего юнита к этому блоку
                bombers = state.get('bombers', [])
                if bombers:
                    closest_bomber = None
                    min_dist = float('inf')
                    
                    for bomber in bombers:
                        if bomber.get('alive'):
                            bx, by = bomber.get('pos', [0, 0])
                            dist = abs(bx - ox) + abs(by - oy)
                            if dist < min_dist:
                                min_dist = dist
                                closest_bomber = bomber
                    
                    if closest_bomber:
                        bid = closest_bomber['id'][:8]
                        bx, by = closest_bomber['pos']
                        logging.info(f"Ближайший юнит {bid} в ({bx}, {by}), расстояние до блока: {min_dist}")
        
        # Формируем команды
        commands = {"bombers": []}
        bombers = state.get('bombers', [])
        
        # Первые 2 юнита используют логику уничтожения, остальные - исследование
        for i, bomber in enumerate(bombers):
            if bomber.get('alive') and bomber.get('can_move'):
                if i < 2 and obstacles:  # Первые два атакуют, если есть цели
                    cmd = find_and_destroy_logic(state, bomber)
                else:
                    cmd = simple_exploration_logic(state, bomber)
                
                if cmd:
                    commands['bombers'].append(cmd)
        
        # Отправляем команды
        if commands['bombers']:
            send_move(commands)
        
        # Статистика каждые 30 секунд
        if total_cycles % 20 == 0:
            logging.info(f"=== ТЕКУЩАЯ СТАТИСТИКА ===")
            logging.info(f"Всего циклов: {total_cycles}")
            logging.info(f"Циклов с блоками: {cycles_with_obstacles}")
            logging.info(f"Процент: {obstacle_percentage if total_cycles > 0 else 0:.1f}%")
            
            if obstacles:
                logging.info(f"Сейчас видно блоков: {len(obstacles)}")
                # Покажем разнообразие позиций блоков
                unique_x = len(set(ox for ox, oy in obstacles[:10]))
                unique_y = len(set(oy for ox, oy in obstacles[:10]))
                logging.info(f"Уникальные X: {unique_x}, уникальные Y: {unique_y}")
        
        time.sleep(0.4)

# ==================== ОСНОВНОЙ РЕЖИМ ====================

def main_bot_loop():
    """Основной режим работы бота."""
    logging.info("🚀 ЗАПУСК ОСНОВНОГО РЕЖИМА")
    
    # Сначала запустим тестовый режим на 2 минуты
    test_end_time = time.time() + 120  # 2 минуты
    
    while time.time() < test_end_time:
        test_mode_bot_loop()
        time.sleep(0.1)
    
    logging.info("Тестовый режим завершён. Анализируем результаты...")
    
    # Здесь можно добавить логику для перехода к основному режиму
    # после диагностики проблемы

# ==================== ТОЧКА ВХОДА ====================

if __name__ == "__main__":
    try:
        # Сначала запускаем тестовый режим для диагностики
        main_bot_loop()
    except KeyboardInterrupt:
        logging.info("Бот остановлен пользователем")
    except Exception as e:
        logging.error(f"Критическая ошибка: {e}", exc_info=True)
