"""Повторні спроби для мережевих запитів.

Обрив з'єднання трапляється: за перший вечір роботи один прохід
втратив цілу конфігурацію на десять хвилин через `Connection reset
by peer`. Для пошуку, де хороші варіанти розбирають за години,
десять хвилин сліпоти — відчутна втрата, а сам збій минущий.
"""
import time

# Мережеві збої, які минають самі: обрив, таймаут, тимчасова відмова
TRANSIENT = ("connection reset", "recv failure", "timed out", "timeout",
             "connection aborted", "temporarily unavailable", "502", "503", "504",
             "eof occurred", "connection refused")

DELAYS = (2, 6, 15)


def is_transient(exc):
    text = str(exc).lower()
    return any(marker in text for marker in TRANSIENT)


def with_retry(call, attempts=3, on_retry=None):
    """Викликає call(), повторюючи лише на минущих збоях.

    Постійні помилки (404, зміна розмітки) прокидаються одразу —
    їх повтор не вилікує, а лише сховає проблему.
    """
    last = None
    for attempt in range(attempts):
        try:
            return call()
        except Exception as exc:
            last = exc
            if not is_transient(exc) or attempt == attempts - 1:
                raise
            delay = DELAYS[min(attempt, len(DELAYS) - 1)]
            if on_retry:
                on_retry(attempt + 1, delay, exc)
            time.sleep(delay)
    raise last
