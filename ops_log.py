"""Bounded, privacy-preserving operational events; no raw logs or user data."""
import queue
import threading
import time

EVENTS = {
    'started': '🟢 Сервер запущен',
    'search': '🔎 Поиск завершён',
    'search_error': '⚠️ Ошибка поиска',
    'api_error': '⚠️ Ошибка рыночного API',
    'partner': '🤝 Получена заявка партнёра',
    'delivery_error': '⚠️ Ошибка доставки',
    'test': '✅ Проверка служебных логов',
}

class Events:
    def __init__(self):
        self.queue = queue.Queue(maxsize=100)
        self.lock = threading.Lock()
        self.last = {}

    def publish(self, event, *, requested=None, found=None, status=None):
        if event not in EVENTS:
            return False
        text = EVENTS[event]
        if event == 'search':
            if type(requested) is not int or type(found) is not int or not 0 <= found <= requested <= 100:
                return False
            text += f'\nЗапрошено: {requested} · Найдено: {found}'
        if event == 'api_error' and type(status) is int and 400 <= status <= 599:
            text += f'\nHTTP {status}'
        # Avoid flooding the monitoring group; no user identifiers are accepted.
        with self.lock:
            now = time.monotonic()
            key = (event, status if event == 'api_error' and type(status) is int else None)
            if event in ('api_error', 'search_error', 'delivery_error') and now - self.last.get(key, -1e9) < 60:
                return False
            try:
                self.queue.put_nowait('✦ GIFT RADAR | SYSTEM\n' + text)
            except queue.Full:
                return False
            self.last[key] = now
        return True

    def run(self, destination, allowed, send):
        while True:
            text = self.queue.get()
            try:
                chat_id = destination()
                if chat_id and allowed(chat_id) and destination() == chat_id:
                    send(chat_id, text)
            except Exception:
                # Never forward raw exceptions or retry a failed log delivery indefinitely.
                pass
            finally:
                self.queue.task_done()
            time.sleep(2)
