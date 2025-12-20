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

# Глобальные переменные
decision_cycle = 0
last_booster_check = 0
BOOSTER_CHECK_INTERVAL = 50
target_history = {}

# ==================== ИСПРАВЛЕННЫЕ БУСТЕРЫ ====================

def get_available_boosters():
    """Получает список доступных бустеров."""
    try:
        response = requests.get(f"{BASE_URL}/api/booster", headers=HEADERS, timeout=3)
        if response.status_code == 200:
            return response.json()
        return None
    except Exception:
        return None

def purchase_booster(booster_id):
    """Покупает бустер по ID."""
    try:
        payload = {"booster": booster_id}
        response = requests.post(f"{BASE_URL}/api/booster", headers=HEADERS, json=payload, timeout=3)
        if response.status_code == 200:
            logging.info(f"✅ Куплен бустер ID {booster_id}")
            return True
        return False
    except Exception:
        return False

def smart_booster_logic(state):
    """Умная логика покупки бустеров."""
    global last_booster_check, decision_cycle
    
    if decision_cycle - last_booster_check < BOOSTER_CHECK_INTERVAL:
        return
    
    last_booster_check = decision_cycle
    boosters_data = get_available_boosters()
    
    if not boosters_data:
        return
    
    available_points = boosters_data.get('state', {}).get('points', 0)
    if available_points <= 0:
        return
    
    # Получаем текущие статы
    current_state = boosters_data.get('state', {})
    
    # Стратегия: сначала то, что помогает зарабатывать очки
    if available_points >= 1:
        # 1. Больше бомб (чтобы можно было больше разрушать)
        if current_state.get('bombs', 1) < 3:
            if purchase_booster(1):
                return
        
        # 2. Радиус взрыва (эффективнее разрушение)
        if current_state.get('bomb_range', 1) < 3:
            if purchase_booster(2):
                return
        
        # 3. Скорость (быстрее находить цели)
        if current_state.get('speed', 2) < 4:
            if purchase_booster(3):
                return
        
        # 4. Обзор (видеть больше блоков)
        if current_state.get('view', 5) < 8:
            if purchase_booster(4):
                return

# ==================== ОСНОВНЫЕ ФУНКЦИИ ====================

def get_arena():
    """Получить текущее состояние игры."""
    try:
        response = requests.get(f"{BASE_URL}/api/arena", headers=HEADERS, timeout=3)
        if response.status_code == 200:
            return response.json()
        return None
    except Exception:
        return None

def send_move(commands):
    """Отправить команды движения."""
    try:
        response = requests.post(f"{BASE_URL}/api/move", headers=HEADERS, json=commands, timeout=3)
        if response.status_code != 200:
            logging.error(f"❌ Ошибка отправки: {response.status_code}")
        return response
    except Exception:
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

# ==================== КРИТИЧЕСКОЕ ИСПРАВЛЕНИЕ: ПОИСК БЛОКОВ ====================

def find_all_obstacles(state, bomber_x, bomber_y, max_distance=10):
    """Находит ВСЕ разрушаемые блоки в радиусе."""
    obstacles = state.get('arena', {}).get('obstacles', [])
    
    if not obstacles:
        logging.debug("Нет видимых блоков на карте")
        return []
    
    # Фильтруем по расстоянию и добавляем информацию
    nearby_obstacles = []
    
    for obs in obstacles:
        obs_x, obs_y = obs
        distance = abs(obs_x - bomber_x) + abs(obs_y - bomber_y)
        
        if distance <= max_distance:
            # Проверяем, можем ли мы разрушить этот блок с нашей позиции
            can_destroy = (bomber_x == obs_x or bomber_y == obs_y)
            
            nearby_obstacles.append({
                'pos': (obs_x, obs_y),
                'distance': distance,
                'can_destroy': can_destroy,
                'score': (100 if can_destroy else 50) - distance * 3
            })
    
    # Сортируем по score (чем выше, тем лучше)
    nearby_obstacles.sort(key=lambda x: x['score'], reverse=True)
    
    if nearby_obstacles:
        logging.debug(f"Найдено {len(nearby_obstacles)} блоков, ближайший: {nearby_obstacles[0]['pos']}")
    
    return nearby_obstacles

def find_best_target(state, bomber_id, x, y):
    """Находит лучшую цель для атаки."""
    global target_history
    
    # 1. Ищем блоки (ВЫСШИЙ ПРИОРИТЕТ - они дают очки!)
    obstacles_info = find_all_obstacles(state, x, y, max_distance=8)
    
    if obstacles_info:
        # Фильтруем по истории атак
        fresh_obstacles = []
        current_time = time.time()
        
        for obs in obstacles_info:
            obs_pos = obs['pos']
            recently_targeted = False
            
            # Проверяем историю для этого юнита
            if bomber_id in target_history:
                for hist_pos, hist_time in target_history[bomber_id]:
                    if hist_pos == obs_pos and current_time - hist_time < 20:
                        recently_targeted = True
                        break
            
            if not recently_targeted:
                fresh_obstacles.append(obs)
        
        # Берем лучший свежий блок
        if fresh_obstacles:
            best_obstacle = fresh_obstacles[0]
            
            # Обновляем историю
            if bomber_id not in target_history:
                target_history[bomber_id] = []
            
            target_history[bomber_id].append((best_obstacle['pos'], current_time))
            
            # Ограничиваем размер истории
            if len(target_history[bomber_id]) > 5:
                target_history[bomber_id] = target_history[bomber_id][-5:]
            
            logging.info(f"{bomber_id[:8]}: 🎯 Блок {best_obstacle['pos']} (расстояние {best_obstacle['distance']})")
            return ('obstacle', best_obstacle['pos'], best_obstacle['distance'], best_obstacle['can_destroy'])
    
    # 2. Ищем врагов (низкий приоритет, пока нет блоков)
    enemies = state.get('enemies', [])
    
    if enemies:
        # Берем ближайшего врага
        nearest_enemy = None
        min_distance = float('inf')
        
        for enemy in enemies[:5]:  # Только первые 5 врагов
            enemy_x, enemy_y = enemy['pos']
            distance = abs(enemy_x - x) + abs(enemy_y - y)
            
            if distance < min_distance and distance <= 6:
                min_distance = distance
                nearest_enemy = (enemy_x, enemy_y)
        
        if nearest_enemy:
            logging.info(f"{bomber_id[:8]}: 👤 Враг {nearest_enemy} (расстояние {min_distance})")
            return ('enemy', nearest_enemy, min_distance, False)
    
    return None

# ==================== ПРОСТАЯ И ЭФФЕКТИВНАЯ ЛОГИКА ====================

def smart_attack_logic(state, bomber):
    """Основная логика атаки."""
    global decision_cycle
    bomber_id = bomber['id']
    x, y = bomber['pos']
    
    # Периодически проверяем бустеры
    decision_cycle += 1
    if decision_cycle % BOOSTER_CHECK_INTERVAL == 0:
        smart_booster_logic(state)
    
    # Проверяем возможность действий
    if not bomber.get('can_move', False):
        return None
    
    bombs_available = bomber.get('bombs_available', 1)
    if bombs_available < 1:
        return exploration_logic(state, bomber, aggressive=True)
    
    # Ищем лучшую цель
    target_info = find_best_target(state, bomber_id, x, y)
    
    if not target_info:
        # Нет целей - исследуем
        return exploration_logic(state, bomber, aggressive=True)
    
    target_type, (target_x, target_y), distance, can_destroy = target_info
    
    # ЛОГИКА АТАКИ БЛОКА (ВЫСШИЙ ПРИОРИТЕТ)
    if target_type == 'obstacle':
        # Если уже можем разрушить блок
        if can_destroy:
            # Находим безопасное направление для отхода
            retreat = find_safe_retreat(state, x, y, target_x, target_y)
            
            if retreat:
                retreat_x, retreat_y = retreat
                logging.info(f"{bomber_id[:8]}: 💣 Атакую блок {target_info[1]}")
                return {
                    "id": bomber_id,
                    "path": [[retreat_x, retreat_y]],
                    "bombs": [[x, y]]
                }
            else:
                logging.warning(f"{bomber_id[:8]}: Не могу безопасно атаковать блок")
        
        # Если не на линии с блоком, встаем на линию
        # Пробуем пойти по горизонтали или вертикали к блоку
        if x != target_x and is_position_safe(state, target_x, y):
            return {
                "id": bomber_id,
                "path": [[target_x, y]],
                "bombs": []
            }
        elif y != target_y and is_position_safe(state, x, target_y):
            return {
                "id": bomber_id,
                "path": [[x, target_y]],
                "bombs": []
            }
    
    # ЛОГИКА АТАКИ ВРАГА (низкий приоритет)
    elif target_type == 'enemy' and distance <= 4:
        # Только если враг близко и на одной линии
        if (x == target_x or y == target_y):
            retreat = find_safe_retreat(state, x, y, target_x, target_y)
            if retreat:
                retreat_x, retreat_y = retreat
                logging.info(f"{bomber_id[:8]}: ⚔️ Атакую врага {target_info[1]}")
                return {
                    "id": bomber_id,
                    "path": [[retreat_x, retreat_y]],
                    "bombs": [[x, y]]
                }
    
    # Если не можем атаковать - исследуем
    return exploration_logic(state, bomber, aggressive=True)

def find_safe_retreat(state, x, y, danger_x, danger_y):
    """Находит безопасное направление для отхода."""
    directions = [(1, 0), (-1, 0), (0, 1), (0, -1)]
    retreat_options = []
    
    for dx, dy in directions:
        retreat_x, retreat_y = x + dx, y + dy
        
        if is_position_safe(state, retreat_x, retreat_y, ignore_bombs=True):
            # Оцениваем направление (чем дальше от опасности, тем лучше)
            distance_from_danger = abs(retreat_x - danger_x) + abs(retreat_y - danger_y)
            distance_from_current = abs(retreat_x - x) + abs(retreat_y - y)
            
            score = distance_from_danger * 3 + distance_from_current
            retreat_options.append((score, (retreat_x, retreat_y)))
    
    if retreat_options:
        retreat_options.sort(reverse=True)
        return retreat_options[0][1]
    
    return None

def exploration_logic(state, bomber, aggressive=False):
    """Логика исследования карты."""
    bomber_id = bomber['id']
    x, y = bomber['pos']
    
    if aggressive:
        # Агрессивное исследование - ищем дальние цели
        directions = [
            (x + 2, y), (x - 2, y), (x, y + 2), (x, y - 2),
            (x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)
        ]
    else:
        directions = [(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)]
    
    # Пробуем все направления
    for target_x, target_y in directions:
        if is_position_safe(state, target_x, target_y):
            return {
                "id": bomber_id,
                "path": [[target_x, target_y]],
                "bombs": []
            }
    
    # Если некуда идти
    return None

# ==================== ДИАГНОСТИЧЕСКАЯ ФУНКЦИЯ ====================

def debug_game_state(state):
    """Выводит отладочную информацию о состоянии игры."""
    if not state:
        return
    
    logging.info("=== ДИАГНОСТИКА ===")
    logging.info(f"Раунд: {state.get('round')}")
    logging.info(f"Очки: {state.get('raw_score')}")
    
    arena = state.get('arena', {})
    obstacles = arena.get('obstacles', [])
    enemies = state.get('enemies', [])
    bombers = state.get('bombers', [])
    
    logging.info(f"Блоков в зоне видимости: {len(obstacles)}")
    logging.info(f"Врагов в зоне видимости: {len(enemies)}")
    
    if obstacles:
        # Показываем позиции первых 3 блоков
        for i, (ox, oy) in enumerate(obstacles[:3]):
            logging.info(f"  Блок {i+1}: ({ox}, {oy})")
    
    # Информация о юнитах
    alive_bombers = [b for b in bombers if b.get('alive')]
    logging.info(f"Живых юнитов: {len(alive_bombers)}/{len(bombers)}")
    
    for i, bomber in enumerate(alive_bombers[:3]):
        pos = bomber.get('pos', [0, 0])
        bombs = bomber.get('bombs_available', 0)
        logging.info(f"  Юнит {i+1}: позиция {pos}, бомб: {bombs}")
    
    logging.info("=================")

# ==================== ГЛАВНЫЙ ЦИКЛ ====================

def main_loop():
    """Основной цикл бота."""
    global decision_cycle
    
    logging.info("🚀 Запуск бота с приоритетом на блоки (без самоподрывов)")
    logging.info(f"Сервер: {BASE_URL}")
    
    request_count = 0
    last_reset = time.time()
    last_debug_time = time.time()
    
    stats = {
        'total_cycles': 0,
        'bombs_placed': 0,
        'blocks_destroyed': 0,
        'last_score': 0,
        'score_history': []
    }
    
    while True:
        # Соблюдение лимита запросов
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
            time.sleep(0.3)
            continue
        
        request_count += 1
        stats['total_cycles'] += 1
        
        # Периодическая диагностика
        if current_time - last_debug_time > 30:  # Каждые 30 секунд
            debug_game_state(state)
            last_debug_time = current_time
        
        # Анализируем очки
        current_score = state.get('raw_score', 0)
        score_diff = current_score - stats['last_score']
        stats['last_score'] = current_score
        
        if score_diff > 0:
            stats['blocks_destroyed'] += score_diff
            logging.info(f"🎉 Заработано очков: +{score_diff} (всего: {current_score})")
        
        # Сохраняем историю очков
        stats['score_history'].append(current_score)
        if len(stats['score_history']) > 10:
            stats['score_history'] = stats['score_history'][-10:]
        
        # Формируем команды
        commands = {"bombers": []}
        bombers = state.get('bombers', [])
        alive_bombers = [b for b in bombers if b.get('alive') and b.get('can_move')]
        
        # Обрабатываем каждого юнита
        for i, bomber in enumerate(alive_bombers):
            # ВСЕГДА используем умную логику атаки
            cmd = smart_attack_logic(state, bomber)
            
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
            enemies = len(state.get('enemies', []))
            bombs = len(state.get('arena', {}).get('bombs', []))
            alive_count = len(alive_bombers)
            
            # Эффективность
            efficiency = 0
            if stats['bombs_placed'] > 0:
                efficiency = stats['blocks_destroyed'] / stats['bombs_placed']
            
            logging.info(f"=== СТАТИСТИКА ===")
            logging.info(f"Циклов: {stats['total_cycles']}, Живых: {alive_count}/6")
            logging.info(f"Очки: {current_score}, Уничтожено блоков: {stats['blocks_destroyed']}")
            logging.info(f"Бомб поставлено: {stats['bombs_placed']}, Эффективность: {efficiency:.2f}")
            logging.info(f"Видимых блоков: {obstacles}, Видимых врагов: {enemies}")
            logging.info(f"Активных бомб: {bombs}")
        
        time.sleep(0.3)

# ==================== ТОЧКА ВХОДА ====================

if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        logging.info("Бот остановлен пользователем")
    except Exception as e:
        logging.error(f"Критическая ошибка: {e}", exc_info=True)
