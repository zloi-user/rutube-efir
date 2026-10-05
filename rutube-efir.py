import sys
import re
import time
import traceback
import json
import locale
import math
import os
import hashlib
import warnings
from datetime import datetime, date, timedelta, timezone
from urllib.parse import urljoin

import requests
from PyQt6.QtCore import (
    Qt, QThread, QSettings, QTimer, QEvent, QSize, QStandardPaths, pyqtSignal,
    QPointF, QRectF,
)
from PyQt6.QtGui import (
    QBrush, QColor, QCursor, QKeySequence, QShortcut, QIcon, QPixmap,
    QPainter, QPainterPath, QPen, QPolygonF, QPalette,
)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QCheckBox, QComboBox, QLineEdit, QPushButton, QTreeWidget, QTreeWidgetItem,
    QSplitter, QDialog, QDialogButtonBox, QLabel, QToolTip, QAbstractItemView,
    QMenu,
)

import mpv  # pip install python-mpv  (+ libmpv / mpv-2.dll в системе)

warnings.filterwarnings("ignore")
requests.packages.urllib3.disable_warnings()

HEADERS = {
    "Accept-Encoding": "gzip, deflate",
    "Accept": "*/*",
    "Connection": "keep-alive",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64)",
}

ID_ROLE = Qt.ItemDataRole.UserRole
CAT_ROLE = Qt.ItemDataRole.UserRole + 1   # узел-категория, а не канал
PSTART_ROLE = Qt.ItemDataRole.UserRole + 2   # начало передачи (datetime)
PEND_ROLE = Qt.ItemDataRole.UserRole + 3     # конец передачи (datetime|None)

AUTOWIDGET_URL = (
    "http://rutube.ru/api/feeds/autowidget/2?client=wdp"
    "&show_hidden_videos=True&show_user_hidden_videos=True"
    "&origin__type=rst,rspa"
)


# ==========================
# ПРОКСИ
# ==========================
# Открытый список HTTPS-прокси hideip.me: строка «ip:port:Страна»,
# российские адреса оканчиваются на «:Russia».
RU_PROXY_LIST_URL = (
    "https://raw.githubusercontent.com/zloi-user/hideip.me/HEAD/https.txt"
)
RU_PROXY_SCHEME = "https"


def parse_ru_proxies(text: str):
    """Список hideip.me -> ['https://ip:port', ...] (только строки ':Russia')."""
    urls = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.endswith(":Russia"):
            continue
        host_port = line[: -len(":Russia")].strip()
        if not host_port:
            continue
        url = host_port if "://" in host_port else f"{RU_PROXY_SCHEME}://{host_port}"
        if url not in urls:
            urls.append(url)
    return urls


def fetch_ru_proxy_list(proxies=None):
    """(текст_списка, ошибка). Пробуем текущим прокси, затем напрямую."""
    err = "неизвестная ошибка"
    for attempt in (proxies, None):
        try:
            r = requests.get(RU_PROXY_LIST_URL, headers=HEADERS,
                             proxies=attempt, timeout=20, verify=False)
            if r.status_code == 200:
                return r.text, ""
            err = f"HTTP {r.status_code}"
        except Exception as e:
            err = f"{type(e).__name__}: {str(e)[:100]}"
    return "", err


def normalize_proxy(text: str, default_scheme: str = "http"):
    """Пустая строка -> None (прокси не используется)."""
    text = (text or "").strip()
    if not text:
        return None
    if "://" not in text:
        text = f"{default_scheme}://{text}"
    return text


def build_requests_proxies(enabled: bool, text: str):
    if not enabled:
        return None
    url = normalize_proxy(text)
    if not url:
        return None
    return {"http": url, "https": url}


# ==========================
# ЗАПРОСЫ К RUTUBE API (только requests + API proxy)
# ==========================
def api_get_ex(url, proxies=None, ref=None, json_mode=False):
    """Возвращает (результат, текст_ошибки). При успехе ошибка = None.

    На не-200 в json_mode тело всё равно разбирается и возвращается вместе
    с ошибкой: blocking_rule приходит с HTTP 404, и без его JSON канал
    остался бы в списке (ошибка при этом остаётся — «HTTP 404»).
    """
    session = None
    try:
        session = requests.Session()
        session.verify = False
        headers = HEADERS.copy()

        if ref:
            headers["Referer"] = ref
            try:  # «прогрев» сессии; его сбой не должен ломать основной запрос
                session.get(ref, headers=headers, proxies=proxies, timeout=10)
            except Exception:
                pass

        r = session.get(url, headers=headers, proxies=proxies, timeout=15)

        if r.status_code != 200:
            body = None
            if json_mode:
                try:
                    body = r.json()
                except ValueError:
                    body = None
            return body, f"HTTP {r.status_code}"

        if json_mode:
            try:
                return r.json(), None
            except ValueError:
                snippet = r.text[:80].replace("\n", " ")
                return None, f"ответ не JSON: {snippet!r}"
        return r.text, None

    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:120]}"
    finally:
        if session is not None:
            session.close()


def api_get(url, proxies=None, ref=None, json_mode=False):
    """Только результат: при не-200 JSON-тело возвращается (см. api_get_ex)."""
    return api_get_ex(url, proxies, ref, json_mode)[0]


def parse_variant_streams(m3u8_text, base_url):
    """Master-плейлист -> [{pixels, bandwidth, url}] (url приводится к абсолютному)."""
    streams = []
    lines = m3u8_text.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue
        url = ""
        j = i + 1
        while j < len(lines) and not lines[j].strip():   # пропуск пустых строк
            j += 1
        if j < len(lines):
            url = lines[j].strip()
        if not url or url.startswith("#"):
            continue
        if not url.startswith("http"):                   # относительный путь
            url = urljoin(base_url, url)
        m = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
        b = re.search(r"BANDWIDTH=(\d+)", line)
        streams.append({
            "pixels": int(m.group(1)) * int(m.group(2)) if m else 0,
            "bandwidth": int(b.group(1)) if b else 0,
            "url": url,
        })
    return streams


def _head_ok(url, proxies, timeout=5):
    """Жив ли вариант потока (HEAD -> 200)."""
    try:
        r = requests.head(url, allow_redirects=True, timeout=timeout,
                          proxies=proxies, headers=HEADERS, verify=False)
        return r.status_code == 200
    except Exception:
        return False


def pick_best_stream(m3u8_text, base_url, proxies, timeout=5):
    """URL потока с максимальным разрешением среди живых (HTTP 200).

    Предпочтение:
        1. rtbcdn.ru (если HTTP 200)
        2. любой другой живой URL с максимальным разрешением
    Вариантов нет, но это медиа-плейлист (#EXTINF) — проигрывать надо сам
    запрошенный URL (мы его только что читали, значит, он жив).
    """
    streams = parse_variant_streams(m3u8_text, base_url)
    if not streams:
        return base_url if "#EXTINF" in m3u8_text else None
    # максимум: сначала по разрешению, а если его нет — по BANDWIDTH
    top = max(s["pixels"] for s in streams)
    if top:
        candidates = [s["url"] for s in streams if s["pixels"] == top]
    else:
        top_bw = max(s["bandwidth"] for s in streams)
        candidates = [s["url"] for s in streams if s["bandwidth"] == top_bw]
    candidates = list(dict.fromkeys(candidates))     # без дублей
    alive = [u for u in candidates if _head_ok(u, proxies, timeout)]
    if not alive:
        return None
    for url in alive:                                # CDN rutube предпочтительнее
        if "rtbcdn.ru" in url:
            return url
    return alive[0]


def get_stream_url(m3u_page_url, proxies, timeout=5):
    """Лучший URL потока из m3u8: максимальное разрешение среди живых
    (HEAD -> 200), предпочтение rtbcdn.ru; медиа-плейлист -> сам URL."""
    text = api_get(m3u_page_url, proxies)
    if not text:
        return None
    return pick_best_stream(text, m3u_page_url, proxies, timeout)


def pick_field(data, key):
    """play/options держит метаданные и в корне ответа, и вложением в "video"."""
    if isinstance(data, dict):
        val = data.get(key)
        if val:
            return val
        video = data.get("video")
        if isinstance(video, dict):
            return video.get(key)
    return None


def norm_text(val):
    """description -> строка, category -> его name (dict -> name)."""
    if isinstance(val, dict):
        val = val.get("name")
    return str(val).strip() if val else ""


# прибавляется к названию живых трансляций и только мешает
TITLE_PREFIX = "Прямой эфир"

# переименования групп плейлиста (одинаковые группы складываются в одну)
CATEGORY_RENAMES = {
    "Все прямые эфиры": "Прямые эфиры",
    "Новости и СМИ": "Новости",
}


def clean_title(title):
    """'Прямой эфир Первый канал' -> 'Первый канал'."""
    title = str(title or "").strip()
    m = re.match(rf"^{re.escape(TITLE_PREFIX)}[\s:;,\-–—]*", title)
    if m:
        rest = title[m.end():].strip()
        if rest:
            return rest   # совсем пустое название оставляем как было
    return title


def clean_category(name):
    """'Все прямые эфиры' -> 'Прямые эфиры' (см. CATEGORY_RENAMES)."""
    name = str(name or "").strip()
    return CATEGORY_RENAMES.get(name, name)


def fetch_play_options(video_id, proxies):
    return api_get(f"http://rutube.ru/api/play/options/{video_id}",
                   proxies, json_mode=True)


# ответы play/options, после которых смотреть нечего — канал прячем
HIDE_DETAIL_TYPES = ("blocking_rule", "player_stub")


def is_blocked(data):
    """play/options без потока — канал не сможет смотреть, прячем его:
    - {"type": "blocking_rule"} — решение правообладателя или включённый VPN;
    - {"detail": {"type": "player_stub", "name": "login_required"}} — видео
      скрыто автором, видно только авторизованным.
    Оба приходят с HTTP 404; JSON-тело на не-200 разбирает api_get_ex.
    """
    if not isinstance(data, dict):
        return False
    if data.get("type") in HIDE_DETAIL_TYPES:
        return True
    detail = data.get("detail")
    if not isinstance(detail, dict):
        return False
    if detail.get("type") in HIDE_DETAIL_TYPES:
        return True
    # запасной признак: имя «blocking_rule_9645266» или «login_required»
    name = str(detail.get("name") or "")
    return name.startswith("blocking_rule") or name == "login_required"


def blocked_reason(data):
    """Короткая причина недоступности — для строк статуса."""
    if not isinstance(data, dict):
        return "видео недоступно"
    detail = data.get("detail")
    detail = detail if isinstance(detail, dict) else {}
    name = str(detail.get("name") or "")
    typ = data.get("type") or detail.get("type")
    if typ == "blocking_rule" or name.startswith("blocking_rule"):
        return "правообладатель или VPN"
    if typ == "player_stub" or name == "login_required":
        return "скрыто автором (нужен вход)"
    return "видео недоступно"


def parse_play_options(data, proxies=None, with_stream=False):
    """play/options -> {ok, blocked, reason, url, description, category,
    author, avatar}.

    ok = False — запрос не удался (не JSON/сеть), такие метаданные можно
    повторить; ok = True, но description пуст — канала без описания.
    blocked = True — ответ без потока (blocking_rule, скрыто автором),
    такой канал прячем из списка; reason — причина для статус-строки.
    """
    blocked = is_blocked(data)
    out = {"ok": isinstance(data, dict), "blocked": blocked,
           "reason": blocked_reason(data) if blocked else "",
           "url": None, "description": "",
           "category": "", "author": "", "avatar": ""}
    if not isinstance(data, dict):
        return out
    out["description"] = norm_text(pick_field(data, "description"))
    out["category"] = norm_text(pick_field(data, "category"))
    author = pick_field(data, "author")
    if isinstance(author, dict):
        out["author"] = norm_text(author.get("name"))
        out["avatar"] = str(author.get("avatar_url") or "").strip()
    if with_stream:
        try:
            live = data.get("live_streams")
            hls = live.get("hls") if isinstance(live, dict) else None
            if hls and hls[0].get("url"):
                out["url"] = get_stream_url(hls[0]["url"], proxies)
        except Exception:
            out["url"] = None
    return out


def get_channel_info(video_id, proxies):
    """Метаданные (описание, подкатегория, автор, логотип) без резолва потока."""
    return parse_play_options(fetch_play_options(video_id, proxies))


def get_stream_by_id(video_id, proxies):
    """Метаданные + HLS-поток: url = None, если живого потока нет."""
    return parse_play_options(fetch_play_options(video_id, proxies),
                              proxies, with_stream=True)


def empty_info():
    """«Ничего неизвестно» (ok=False — запрос не удался, можно повторить)."""
    return {"ok": False, "blocked": False, "reason": "", "url": None,
            "description": "", "category": "", "author": "", "avatar": ""}


def download_image(url, proxies):
    """Скачивание бинарного ресурса (аватар) -> bytes | None."""
    try:
        r = requests.get(url, headers=HEADERS, proxies=proxies,
                         timeout=10, verify=False)
        if r.status_code == 200 and r.content:
            return r.content
    except Exception:
        pass
    return None


def avatar_cache_path(url):
    """Путь к дисковому кешу аватара (чтобы не качать его каждый запуск)."""
    try:
        base = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.CacheLocation)
    except Exception:
        base = ""
    if not base:
        base = os.path.expanduser("~/.cache/rutube-efir")
    return os.path.join(base, "avatars",
                        hashlib.sha1(url.encode("utf-8")).hexdigest() + ".img")


# ==========================
# ПРОГРАММА ПЕРЕДАЧ (pangolin tvprogram)
# ==========================
PROGRAM_URL = ("http://rutube.ru/pangolin/api/web/tvprogram/"
               "{video_id}/?programDate={date}&client=wdp")

WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")

# ключи ответа: структура pangolin не документирована, ищем по ходовым именам;
# у tvprogram это start/stop (UTC) и title, см. parse_program_time
PROGRAM_LIST_KEYS = ("results", "items", "programs", "program", "schedule",
                     "tv_program", "broadcasts", "events", "list", "data",
                     "result")
PROGRAM_TITLE_KEYS = ("title", "name", "program_title", "event_name",
                      "show_title", "caption", "topic")
PROGRAM_START_KEYS = ("start", "start_time", "begin_time", "begin",
                      "start_date", "begin_date", "air_time", "time", "date")
PROGRAM_END_KEYS = ("stop", "end_time", "end", "finish_time", "finish",
                    "stop_time", "end_date", "finish_date")
PROGRAM_DESC_KEYS = ("description", "desc", "details", "annotation",
                     "about", "text")

# дата «только времени» (18:00), когда её нет в самом значении
_NO_DATE = date(1900, 1, 1)


def today_iso():
    return datetime.now().strftime("%Y-%m-%d")


def _epoch_to_local(ts):
    """epoch UTC (секунды или миллисекунды) -> локальный datetime."""
    try:
        if ts > 1e12:                       # миллисекунды
            ts /= 1000.0
        return datetime.fromtimestamp(ts)
    except Exception:
        return None


def parse_program_time(val, base_day=None):
    """1790820000 | 18:00 | 2026-10-01T15:00:00Z -> локальный datetime.

    tvprogram отдаёт start/stop как epoch UTC (строкой «1790820000» или
    числом); даты-строки тоже считаются UTC — явным («Z», «+00:00»)
    или наивным («2026-10-01T15:00:00»).
    base_day — дата, к которой привязать значение «только времени».
    """
    if val is None or isinstance(val, bool) or val == "":
        return None
    if isinstance(val, (int, float)):       # epoch числом
        return _epoch_to_local(float(val))
    s = str(val).strip()
    if re.fullmatch(r"\d{9,14}", s):        # epoch строкой: «1790820000»
        dt = _epoch_to_local(float(s))
        if dt is not None:
            return dt
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", s)
    if m:                                        # «18:00» / «18:00:30»
        t = datetime.strptime(s, "%H:%M:%S" if m.group(3) else "%H:%M").time()
        return datetime.combine(base_day or _NO_DATE, t)
    dt, has_time = None, False
    try:                                         # ISO, в т.ч. «Z» и «+00:00»
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        has_time = ":" in s
    except ValueError:
        pass
    if dt is None:
        for fmt, ht in (("%d.%m.%Y %H:%M:%S", True), ("%d.%m.%Y %H:%M", True),
                        ("%d.%m.%Y", False)):
            try:
                dt = datetime.strptime(s, fmt)
                has_time = ht
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is not None:                    # явный пояс -> локальное
        return dt.astimezone().replace(tzinfo=None)
    if has_time:                                 # наивное = UTC -> локальное
        try:
            return dt.replace(tzinfo=timezone.utc) \
                     .astimezone().replace(tzinfo=None)
        except Exception:
            return dt
    return dt                                    # просто дата — часы не трогаем


def _first_str(entry, keys):
    """Первое непустое строковое значение по списку ключей (dict -> его значение)."""
    if not isinstance(entry, dict):
        return ""
    for k in keys:
        v = entry.get(k)
        if isinstance(v, dict):                  # {"rus": "..."}
            v = v.get("rus") or v.get("ru") or v.get("name") \
                or next(iter(v.values()), None)
        if v:
            s = str(v).strip()
            if s:
                return s
    return ""


def _pick_time(entry, sub, keys, base_day):
    for src in (entry, sub):
        if not isinstance(src, dict):
            continue
        for k in keys:
            t = parse_program_time(src.get(k), base_day)
            if t is not None:
                return t
    return None


def _normalize_program(entry, base_day=None):
    """Словарь ответа -> {title, start, end, desc} | None (не передача)."""
    if not isinstance(entry, dict):
        return None
    sub = None                                  # вложенная «программа»
    for k in ("program", "event", "show"):
        v = entry.get(k)
        if isinstance(v, dict):
            sub = v
            break
    title = _first_str(entry, PROGRAM_TITLE_KEYS) \
        or _first_str(sub, PROGRAM_TITLE_KEYS)
    if not title:
        return None
    start = _pick_time(entry, sub, PROGRAM_START_KEYS, base_day)
    end = _pick_time(entry, sub, PROGRAM_END_KEYS,
                     start.date() if start else base_day)
    desc = _first_str(entry, PROGRAM_DESC_KEYS) \
        or _first_str(sub, PROGRAM_DESC_KEYS)
    subtitle = _first_str(entry, ("subtitle",)) \
        or _first_str(sub, ("subtitle",))
    if subtitle:                 # подзаголовок сверху: «Молодой муж против…»
        desc = f"{subtitle}\n\n{desc}" if desc else subtitle
    return {"title": title, "start": start, "end": end, "desc": desc}


def _looks_like_program(x):
    if not isinstance(x, dict):
        return False
    if not any(x.get(k) for k in PROGRAM_TITLE_KEYS):
        return False
    return (any(k in x for k in PROGRAM_START_KEYS + PROGRAM_END_KEYS)
            or isinstance(x.get("program") or x.get("event"), dict))


def _find_program_list(data):
    """Первый в ответе список, похожий на программу передач."""
    queue = [data]
    guard = 0
    while queue and guard < 4000:
        node = queue.pop(0)
        guard += 1
        if isinstance(node, list):
            if node:
                good = sum(1 for x in node if _looks_like_program(x))
                if good and good * 2 >= len(node):
                    return node
                queue.extend(x for x in node if isinstance(x, (dict, list)))
        elif isinstance(node, dict):
            pref, rest = [], []
            for k, v in node.items():
                if not isinstance(v, (dict, list)):
                    continue
                # значения под известными ключами смотрим в первую очередь
                (pref if k in PROGRAM_LIST_KEYS else rest).append(v)
            queue[0:0] = pref
            queue.extend(rest)
    return None


def parse_program(data, date_iso=None):
    """Ответ tvprogram -> [{title, start, end, desc}] (по возрастанию времени).

    start/stop из ответа — UTC (см. parse_program_time), на экран попадает
    локальное время. Передачи без времени идут после датированных,
    сохраняя порядок ответа.
    """
    raw = _find_program_list(data)
    if not raw:
        return []
    base_day = None
    if date_iso:
        try:
            base_day = datetime.strptime(date_iso, "%Y-%m-%d").date()
        except ValueError:
            base_day = None
    items = []
    for entry in raw:
        p = _normalize_program(entry, base_day)
        if p is not None:
            items.append(p)
    items.sort(key=lambda p: (p["start"] is None,
                              p["start"] or datetime.max))
    return items


def program_time_label(p):
    """«18:00–19:00» / «18:00» / «—» (времени нет)."""
    s, e = p.get("start"), p.get("end")
    if not isinstance(s, datetime):
        s = None
    if not isinstance(e, datetime):
        e = None
    if s and e:
        return f"{s:%H:%M}–{e:%H:%M}"
    if s:
        return f"{s:%H:%M}"
    return "—"


def fetch_program(video_id, date_iso, proxies):
    """Программа передач канала на дату -> {"ok", "items"} (см. parse_program)."""
    url = PROGRAM_URL.format(video_id=video_id, date=date_iso)
    data, err = api_get_ex(url, proxies, json_mode=True)
    if "HTTP 404" in (err or ""):
        return {"ok": True, "items": []}        # у канала нет программы
    if err or data is None:
        return {"ok": False, "items": []}
    try:
        return {"ok": True, "items": parse_program(data, date_iso)}
    except Exception:
        traceback.print_exc()
        return {"ok": False, "items": []}


def parse_items(data):
    """Возвращает (кол-во каналов, [элементы]).

    autowidget/2 отдаёт либо категории: results[] -> {name, childs[]},
    либо плоский список (feed.resources[1].items). У самого канала может
    быть свой "category": {"id": ..., "name": "Телепередачи"} и
    "description" — они уходят в элемент дальше, чтобы из них построить
    дерево плейлиста и подсказку. Каналы повторяются между категориями,
    поэтому дедупликация по id (первое вхождение побеждает).
    """
    items = []
    seen = set()

    def add(raw, group=None):
        if not isinstance(raw, dict):
            return
        vid, title = raw.get("id"), raw.get("title")
        if not vid or not title:
            return
        if raw.get("duration") not in (None, 0):
            return
        if raw.get("origin_type") == "ifrm":
            return
        if vid in seen:
            return
        seen.add(vid)
        items.append({
            "id": vid,
            "title": str(title).strip(),
            "description": norm_text(raw.get("description")),
            # своя категория канала важнее имени группы-обёртки
            "category": (norm_text(raw.get("category"))
                         or str(group or "").strip() or None),
        })

    if not isinstance(data, dict):
        return 0, items

    if data.get("results"):
        for entry in data["results"]:
            if not isinstance(entry, dict):
                continue
            if entry.get("childs"):        # категория -> её каналы
                for child in entry["childs"]:
                    add(child, entry.get("name"))
            else:                          # обычный плоский элемент
                add(entry)
    elif data.get("feed") and data["feed"].get("resources"):
        resources = data["feed"]["resources"]
        if len(resources) > 1 and resources[1].get("items"):
            for raw in resources[1]["items"]:
                add(raw)

    return len(items), items


# ==========================
# ПОТОКИ
# ==========================
class ChannelsWorker(QThread):
    batch = pyqtSignal(list)       # [ {id, title, description, category} ]
    status = pyqtSignal(str)

    def __init__(self, proxies, parent=None):
        super().__init__(parent)
        self.proxies = proxies
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        self.status.emit("Загрузка списка каналов...")
        try:
            data, err = api_get_ex(AUTOWIDGET_URL, self.proxies,
                                   ref="http://rutube.ru/", json_mode=True)
            if self._stop:
                return
            if err:
                self.status.emit(f"Ошибка запроса: {err}")
                return

            count, items = parse_items(data)
        except Exception:
            # исключение в потоке PyQt -> аварийный останов приложения
            traceback.print_exc()
            self.status.emit("Ошибка при разборе списка каналов")
            return
        if items:
            self.batch.emit(items)
        else:
            keys = list(data.keys())[:6] if isinstance(data, dict) else type(data).__name__
            self.status.emit(
                f"Ответ получен, но каналов нет (элементов: {count}, ключи: {keys})")


class StreamWorker(QThread):
    resolved = pyqtSignal(str, int, object)  # video_id, token, метаданные+url

    def __init__(self, video_id, token, proxies, parent=None):
        super().__init__(parent)
        self.video_id = video_id
        self.token = token
        self.proxies = proxies

    def run(self):
        # исключение в потоке PyQt приводит к аварийному останову,
        # поэтому любая ошибка — это «поток не найден», а не падение
        try:
            info = get_stream_by_id(self.video_id, self.proxies)
        except Exception:
            traceback.print_exc()
            info = empty_info()
        self.resolved.emit(self.video_id, self.token, info)


class InfoWorker(QThread):
    """Тянет метаданные канала из play/options: описание, категорию, автора."""
    info = pyqtSignal(str, object)   # video_id, dict (ok=False — сбой)

    def __init__(self, video_id, proxies, parent=None):
        super().__init__(parent)
        self.video_id = video_id
        self.proxies = proxies

    def run(self):
        try:
            info = get_channel_info(self.video_id, self.proxies)
        except Exception:
            traceback.print_exc()
            info = empty_info()
        self.info.emit(self.video_id, info)


class AvatarWorker(QThread):
    """Скачивает логотип автора (с дисковым кешем)."""
    avatar = pyqtSignal(str, object)  # avatar_url, bytes | None

    def __init__(self, url, proxies, parent=None):
        super().__init__(parent)
        self.url = url
        self.proxies = proxies

    def run(self):
        data = None
        try:
            path = avatar_cache_path(self.url)
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except OSError:
                pass
            if not data:
                data = download_image(self.url, self.proxies)
                if data:
                    try:
                        os.makedirs(os.path.dirname(path), exist_ok=True)
                        with open(path, "wb") as f:
                            f.write(data)
                    except OSError:
                        pass  # не записалось в кеш — просто не сохраняем
        except Exception:
            # ни падения, ни зависшего потока на выходе быть не должно
            traceback.print_exc()
            data = None
        self.avatar.emit(self.url, data)


class ProgramWorker(QThread):
    """Тянет программу передач канала (pangolin tvprogram) на дату."""
    ready = pyqtSignal(str, str, object)  # video_id, дата, {"ok", "items"}

    def __init__(self, video_id, date_iso, proxies, parent=None):
        super().__init__(parent)
        self.video_id = video_id
        self.date_iso = date_iso
        self.proxies = proxies

    def run(self):
        try:
            result = fetch_program(self.video_id, self.date_iso, self.proxies)
        except Exception:
            # исключение в потоке PyQt -> аварийный останов приложения
            traceback.print_exc()
            result = {"ok": False, "items": []}
        self.ready.emit(self.video_id, self.date_iso, result)


# ==========================
# СПИСОК RU-ПРОКСИ (hideip.me)
# ==========================
# Живые потоки загрузки списка: ссылка вне диалога, чтобы закрытие
# диалога на время загрузки не оборвало работающий поток.
_PROXY_LIST_WORKERS = set()


def _forget_proxy_worker(worker):
    _PROXY_LIST_WORKERS.discard(worker)
    worker.deleteLater()


class ProxyListWorker(QThread):
    """Скачивает список прокси и оставляет строки ':Russia'."""
    done = pyqtSignal(list, str)   # ['https://ip:port', ...], ошибка ("" — ок)

    def __init__(self, proxies=None, parent=None):
        super().__init__(parent)
        self.proxies = proxies

    def run(self):
        urls, err = [], "сбой загрузки списка"
        try:
            text, err = fetch_ru_proxy_list(self.proxies)
            if not err:
                urls = parse_ru_proxies(text)
                if not urls:
                    err = "в списке нет адресов Russia"
                else:
                    err = ""
        except Exception:
            # исключение в потоке PyQt -> аварийный останов приложения
            traceback.print_exc()
            urls, err = [], "сбой загрузки списка"
        self.done.emit(urls, err)


class ProxyPickDialog(QDialog):
    """Выбор прокси из списка hideip.me (строки ':Russia')."""

    def __init__(self, proxies=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("RU прокси — список")
        self.setModal(True)
        self.resize(420, 420)
        self._urls = []
        self._worker = None

        root = QVBoxLayout(self)

        self.status = QLabel("Загрузка списка…")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color:#888;")
        root.addWidget(self.status)

        self.list = QTreeWidget()
        self.list.setColumnCount(1)
        self.list.setHeaderLabels(["Прокси (Россия)"])
        self.list.setRootIsDecorated(False)
        self.list.setUniformRowHeights(True)
        self.list.setAlternatingRowColors(True)
        self.list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list.header().setStretchLastSection(True)
        root.addWidget(self.list, 1)

        self.refresh_btn = QPushButton("Обновить")
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        self.refresh_btn.clicked.connect(self.reload)
        bottom = QHBoxLayout()
        bottom.addWidget(self.refresh_btn)
        bottom.addStretch(1)
        bottom.addWidget(bb)
        root.addLayout(bottom)

        self.list.itemDoubleClicked.connect(lambda *_: self.accept())
        self._proxies = proxies
        self.reload()

    # ---------- загрузка ----------
    def reload(self):
        if self._worker is not None and self._worker.isRunning():
            return
        self.refresh_btn.setEnabled(False)
        self.status.setText("Загрузка списка…")
        w = ProxyListWorker(self._proxies)
        # ссылка на поток живёт отдельно от диалога: закрытый на загрузке
        # диалог не должен оборвать работающий поток
        _PROXY_LIST_WORKERS.add(w)
        w.done.connect(self._loaded)
        w.finished.connect(lambda worker=w: _forget_proxy_worker(worker))
        self._worker = w
        w.start()

    def _loaded(self, urls, err):
        self.refresh_btn.setEnabled(True)
        self._worker = None
        if err and not urls:
            self.status.setText(f"Ошибка: {err}")
            return
        self._urls = urls
        self.list.clear()
        for url in urls:
            self.list.addTopLevelItem(QTreeWidgetItem([url]))
        self.status.setText(f"Адресов: {len(urls)}" + (f" · {err}" if err else ""))
        if urls:
            self.list.setCurrentItem(self.list.topLevelItem(0))

    # ---------- выбор ----------
    def proxy_url(self):
        item = self.list.currentItem()
        return item.text(0) if item is not None else ""


# ==========================
# ГЛАВНОЕ ОКНО
# ==========================
class SettingsDialog(QDialog):
    """Настройки (шестерёнка): RU API-прокси (в т.ч. выбор из списка
    hideip.me) и прокси для потока mpv.

    Поля вводятся здесь, в QSettings попадают только по OK:
    Cancel откатывает несохранённые правки обратно из настроек.
    """

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Настройки")
        self.setModal(True)

        root = QVBoxLayout(self)

        grid = QGridLayout()
        self.api_check = QCheckBox("RU API proxy:")
        self.api_edit = QLineEdit()
        self.api_edit.setPlaceholderText("https://92.242.41.77:443")
        self.api_edit.setToolTip(
            "Прокси для запросов к API Rutube (российские адреса)")
        self.api_list_btn = QPushButton("Из списка…")
        self.api_list_btn.setToolTip(
            "Выбрать прокси из списка hideip.me — только строки ':Russia'")
        self.api_list_btn.clicked.connect(self._pick_api_proxy)
        self.stream_check = QCheckBox("Stream proxy:")
        self.stream_edit = QLineEdit()
        self.stream_edit.setPlaceholderText("http://127.0.0.1:8080")
        self.stream_edit.setToolTip("Прокси только для mpv (воспроизведение)")
        self.prefetch_check = QCheckBox("Предзагрузка логотипов и описаний")
        self.prefetch_check.setToolTip(
            "Фоново запрашивать play/options для каналов списка, "
            "чтобы сразу были подкатегории, подсказки и логотипы")

        grid.addWidget(self.api_check, 0, 0)
        grid.addWidget(self.api_edit, 0, 1)
        grid.addWidget(self.api_list_btn, 0, 2)
        grid.addWidget(self.stream_check, 1, 0)
        grid.addWidget(self.stream_edit, 1, 1)
        grid.addWidget(self.prefetch_check, 2, 0, 1, 3)
        grid.setColumnStretch(1, 1)
        root.addLayout(grid)

        hint = QLabel(
            "Пустое поле — прокси не используется. "
            "Схема (http://) добавляется автоматически. "
            "«Из списка…» подставляет адрес из списка hideip.me "
            "(строки с ':Russia').")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#888;")
        root.addWidget(hint)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

        self._load()

    def _widgets(self):
        return (self.api_check, self.api_edit, self.stream_check,
                self.stream_edit, self.prefetch_check)

    def _pick_api_proxy(self):
        """Подстановка выбранного адреса из списка hideip.me (':Russia')."""
        dlg = ProxyPickDialog(
            build_requests_proxies(self.api_check.isChecked(),
                                   self.api_edit.text()),
            self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.proxy_url():
            self.api_edit.setText(dlg.proxy_url())
            self.api_check.setChecked(True)   # выбранный прокси должен работать

    def _load(self):
        s = self.settings
        # блокируем сигналы: иначе setText/setChecked затрут ещё не
        # загруженные значения полей через обработчики сохранения
        for w in self._widgets():
            w.blockSignals(True)
        try:
            self.api_check.setChecked(s.value("api_proxy_enabled", False, type=bool))
            self.api_edit.setText(s.value("api_proxy", "", type=str))
            self.stream_check.setChecked(
                s.value("stream_proxy_enabled", False, type=bool))
            self.stream_edit.setText(s.value("stream_proxy", "", type=str))
            self.prefetch_check.setChecked(
                s.value("prefetch_icons", True, type=bool))
        finally:
            for w in self._widgets():
                w.blockSignals(False)

    def _save(self):
        s = self.settings
        s.setValue("api_proxy_enabled", self.api_check.isChecked())
        s.setValue("api_proxy", self.api_edit.text())
        s.setValue("stream_proxy_enabled", self.stream_check.isChecked())
        s.setValue("stream_proxy", self.stream_edit.text())
        s.setValue("prefetch_icons", self.prefetch_check.isChecked())
        s.sync()

    def accept(self):
        self._save()
        super().accept()

    def reject(self):
        self._load()  # откат несохранённых правок
        super().reject()


NO_GROUP_LABEL = "(без группы)"


class EditChannelDialog(QDialog):
    """Переименование канала и/или перевод в другую группу (можно новую)."""

    def __init__(self, title, group, groups, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Редактировать канал")
        self.setModal(True)
        grid = QGridLayout(self)
        grid.addWidget(QLabel("Название:"), 0, 0)
        self.title_edit = QLineEdit(title)
        self.title_edit.setClearButtonEnabled(True)
        grid.addWidget(self.title_edit, 0, 1)
        grid.addWidget(QLabel("Группа:"), 1, 0)
        # редактируемый комбобокс: список известных групп + любая новая
        self.group_combo = QComboBox()
        self.group_combo.setEditable(True)
        self.group_combo.addItem(NO_GROUP_LABEL, "")
        for g in groups:
            if g and self.group_combo.findData(g) < 0:
                self.group_combo.addItem(g, g)
        if group:
            if self.group_combo.findData(group) < 0:
                self.group_combo.addItem(group, group)  # своей группы в списке нет
            self.group_combo.setCurrentText(group)
        else:
            self.group_combo.setCurrentIndex(0)         # «(без группы)»
        grid.addWidget(self.group_combo, 1, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                              | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        grid.addWidget(bb, 2, 0, 1, 2)

    def values(self):
        """(название, группа); пустая группа — канал на верхнем уровне."""
        g = self.group_combo.currentText().strip()
        if g == NO_GROUP_LABEL:
            g = ""
        return self.title_edit.text().strip(), g


# ==========================
# ПИКТОГРАММЫ КНОПОК
# ==========================
def pict(name, size=16, color=None):
    """Иконка, нарисованная QPainter: без файлов ресурсов и глифов шрифта
    (глифы «⚙ ◀ ▶» выглядят по-разному в разных шрифтах, у векторной
    пиктограммы — предсказуемый вид в любой теме)."""
    if color is None:
        color = QApplication.palette().color(QPalette.ColorRole.ButtonText)
    pen_c = QColor(color)
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(pen_c)
    pen.setWidthF(max(1.4, size / 11.0))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)

    m = size * 0.14
    x0, y0, x1, y1 = m, m, size - m, size - m
    w, h = x1 - x0, y1 - y0
    cx, cy = size / 2, size / 2
    r = min(w, h) / 2

    def dot(pt, rad=None):
        p.setBrush(QBrush(pen_c))
        p.drawEllipse(pt, rad or size * 0.055, rad or size * 0.055)
        p.setBrush(Qt.BrushStyle.NoBrush)

    def arc(a0, a1, rad=None, steps=30):
        # дуга в экранных координатах (y вниз): угол 90° — верх
        rad = r if rad is None else rad
        path = QPainterPath()
        for i in range(steps + 1):
            t = math.radians(a0 + (a1 - a0) * i / steps)
            pt = QPointF(cx + rad * math.cos(t), cy - rad * math.sin(t))
            if i == 0:
                path.moveTo(pt)
            else:
                path.lineTo(pt)
        p.drawPath(path)

    def arrow(tip, direction, length, spread=math.radians(32)):
        # наконечник в tip, «хвост» смотрит навстречу direction
        base = math.atan2(-direction[1], -direction[0])
        for s in (1, -1):
            a = base + s * spread
            p.drawLine(tip, tip + QPointF(length * math.cos(a),
                                          length * math.sin(a)))

    def calendar(day_dot=False):
        top = y0 + h * 0.16
        p.drawRoundedRect(QRectF(x0, top, w, y1 - top),
                          size * 0.10, size * 0.10)
        head = y0 + h * 0.40
        p.drawLine(QPointF(x0, head), QPointF(x1, head))
        for xf in (0.30, 0.70):                       # «ножки» календаря
            xx = x0 + w * xf
            p.drawLine(QPointF(xx, y0), QPointF(xx, top + h * 0.16))
        if day_dot:
            dot(QPointF(cx, (head + y1) / 2), size * 0.11)

    if name == "refresh":
        arc(70, -250)               # почти полный круг, разрыв сверху
        t = math.radians(70)
        arrow(QPointF(cx + r * math.cos(t), cy - r * math.sin(t)),
              (math.sin(t), math.cos(t)), size * 0.26)   # по часовой
    elif name == "list":
        for i in range(3):          # строки с маркерами — плейлист
            yy = y0 + h * (i + 0.5) / 3
            dot(QPointF(x0 + w * 0.07, yy))
            p.drawLine(QPointF(x0 + w * 0.28, yy), QPointF(x1, yy))
    elif name == "calendar":
        calendar()
    elif name == "today":
        calendar(day_dot=True)
    elif name == "fullscreen":
        k = min(w, h) * 0.34        # уголки «развернуть»
        for sx, sy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
            px, py = (x0 if sx < 0 else x1), (y0 if sy < 0 else y1)
            p.drawLine(QPointF(px, py), QPointF(px - sx * k, py))
            p.drawLine(QPointF(px, py), QPointF(px, py - sy * k))
    elif name == "collapse":
        # короткие стрелки к центру — обратная операция «на весь экран»;
        # важно не сходиться в центре, иначе фигура сливается в кляксу
        half = min(w, h) / 2
        for sx, sy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
            corner = QPointF(x0 if sx < 0 else x1, y0 if sy < 0 else y1)
            tail = QPointF(corner.x() - sx * half * 0.12,
                           corner.y() - sy * half * 0.12)
            tip = QPointF(corner.x() - sx * half * 0.60,
                          corner.y() - sy * half * 0.60)
            p.drawLine(tail, tip)
            arrow(tip, (-sx, -sy), size * 0.17)
    elif name in ("prev", "next"):
        p.setBrush(QBrush(pen_c))
        if name == "prev":
            pts = [QPointF(x1, y0), QPointF(x1, y1),
                   QPointF(x0, (y0 + y1) / 2)]
        else:
            pts = [QPointF(x0, y0), QPointF(x0, y1),
                   QPointF(x1, (y0 + y1) / 2)]
        p.drawPolygon(QPolygonF(pts))
        p.setBrush(Qt.BrushStyle.NoBrush)
    elif name == "gear":
        r_body, r_hole = r * 0.72, r * 0.26
        for k in range(8):          # 8 зубьев
            a = math.radians(k * 45)
            p.drawLine(QPointF(cx + r_body * math.cos(a),
                               cy - r_body * math.sin(a)),
                       QPointF(cx + r * math.cos(a),
                               cy - r * math.sin(a)))
        p.drawEllipse(QPointF(cx, cy), r_body, r_body)
        p.drawEllipse(QPointF(cx, cy), r_hole, r_hole)
    p.end()
    return QIcon(pm)


def set_icon(btn, name, size=16, color=None):
    """Иконка + её размер: иначе стиль может растянуть маленький pixmap."""
    btn.setIcon(pict(name, size, color))
    btn.setIconSize(QSize(size, size))


class FullscreenOverlay(QWidget):
    """Плавающая кнопка выхода из полного экрана.

    Окно mpv забирает мышь и клавиатуру себе, поэтому из него нельзя
    рассчитывать получить Esc/F11/клик. Эта кнопка — отдельное окно
    поверх видео (дочернее к главному, чтобы менеджер окон держал его
    выше полноэкранного родителя), и её клик не зависит от mpv.
    """
    exit_clicked = pyqtSignal()

    def __init__(self, parent):
        super().__init__(
            parent,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        btn = QPushButton("Выйти из полного экрана")
        set_icon(btn, "collapse", 14, "white")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(
            "QPushButton{background:rgba(0,0,0,175);color:white;"
            "border:1px solid rgba(255,255,255,90);border-radius:6px;"
            "padding:8px 14px;font-size:14px;}"
            "QPushButton:hover{background:rgba(190,40,40,230);}"
        )
        btn.clicked.connect(self.exit_clicked)
        lay.addWidget(btn)
        self.adjustSize()

    def place_top_right(self, screen_geo, margin=16):
        self.adjustSize()
        self.move(screen_geo.right() - self.width() - margin,
                  screen_geo.top() + margin)


class MainWindow(QMainWindow):
    # из потока mpv в GUI-поток: "toggle" | "leave"
    fs_request = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Рутюб Эфир")
        self.resize(1200, 700)

        self.settings = QSettings("rutube-efir", "rutube-efir")
        self.player = None
        self.channels_worker = None
        self.stream_workers = set()
        self.info_workers = set()
        self.avatar_workers = set()
        self.program_workers = set()
        self.stream_token = 0
        self.known_ids = set()
        self._cat_nodes = {}        # имя категории -> узел дерева
        self._items_by_id = {}      # id канала -> его элемент
        self._desc_cache = {}       # id -> description (для кеша списка)
        self.synonyms = {}          # список синонимов: id -> {title, group}
        self._load_synonyms()       # правки пользователя поверх сырого списка
        self._info_by_id = {}       # id -> метаданные из play/options
        self._info_done = set()     # id, по которым метаданные уже получены
        self._blocked_ids = set()   # id без потока — прячем из списка
        self._blocked_reasons = {}  # id -> причина (для статус-строк)
        self._info_pending = set()  # id, чей запрос сейчас выполняется
        self._info_queue = []       # очередь запросов (для предзагрузки)
        self._avatar_url_by_id = {}  # id -> avatar_url
        self._icons = {}            # avatar_url -> QIcon
        self._avatar_pending = set()  # avatar_url, скачиваемые сейчас
        self._avatar_failed = set()   # avatar_url, которые скачать не вышло
        self._current_id = None
        self._current_title = ""
        self._last_status = ""
        self._closing = False        # запрет на новые воркеры при выходе
        self._close_pending = None   # потоки, дожидаемыхся после закрытия
        self._was_maximized = False
        self._fs_last_pos = None
        self._fs_last_move = 0.0
        self.playlist_visible = True   # виден ли плейлист (вне полного экрана)
        self.program_visible = True    # видна ли панель программы передач
        self._program_id = None        # канал, чья программа показана
        self._program_date = today_iso()  # показанная дата (ГГГГ-ММ-ДД)
        self._program_cache = {}       # (id, дата) -> {"ok", "items"}

        self._build_ui()
        self._load_cache()

        if self.list.topLevelItemCount() == 0:
            QTimer.singleShot(0, self.refresh_channels)

    # ---------- UI ----------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # панель управления (прячется в полноэкранном режиме)
        self.controls = QWidget()
        controls_lay = QVBoxLayout(self.controls)
        controls_lay.setContentsMargins(0, 0, 0, 0)

        # поиск + обновить + плейлист + настройки
        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск канала...")
        self.search.setClearButtonEnabled(True)
        self.refresh_btn = QPushButton("Обновить")
        set_icon(self.refresh_btn, "refresh")
        self.playlist_btn = QPushButton("Скрыть список")
        set_icon(self.playlist_btn, "list")
        self.playlist_btn.setToolTip("Показать/скрыть плейлист (Ctrl+L)")
        self.program_btn = QPushButton("Скрыть программу")
        set_icon(self.program_btn, "calendar")
        self.program_btn.setToolTip("Показать/скрыть программу передач (Ctrl+P)")
        self.fs_btn = QPushButton("На весь экран")
        set_icon(self.fs_btn, "fullscreen")
        self.settings_btn = QPushButton()           # шестерёнка-пиктограмма
        set_icon(self.settings_btn, "gear")
        self.settings_btn.setToolTip("Настройки: прокси (Ctrl+,)")
        self.settings_btn.setFixedWidth(36)
        row.addWidget(self.search, 1)
        row.addWidget(self.refresh_btn)
        row.addWidget(self.playlist_btn)
        row.addWidget(self.program_btn)
        row.addWidget(self.fs_btn)
        row.addWidget(self.settings_btn)
        controls_lay.addLayout(row)
        root.addWidget(self.controls)

        # список + видео
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.list = QTreeWidget()
        self.list.setHeaderHidden(True)
        # равные высоты строк нельзя держать: под логотип 32 px строка
        # обязана подстраиваться под содержимое (иначе иконка обрежется)
        self.list.setUniformRowHeights(False)
        self.list.setIconSize(QSize(32, 32))   # логотип канала (в 2 раза крупнее)
        self.list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list.viewport().installEventFilter(self)   # подсказки каналов
        self.splitter.addWidget(self.list)

        self.video = QWidget()
        self.video.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.video.setStyleSheet("background: black;")
        self.splitter.addWidget(self.video)

        # правая панель — программа передач выбранного канала
        self.program_panel = QWidget()
        panel_lay = QVBoxLayout(self.program_panel)
        panel_lay.setContentsMargins(0, 0, 0, 0)
        panel_lay.setSpacing(2)
        pnav = QHBoxLayout()
        self.pg_prev = QPushButton()
        self.pg_next = QPushButton()
        set_icon(self.pg_prev, "prev", 14)
        set_icon(self.pg_next, "next", 14)
        self.pg_prev.setFixedWidth(28)
        self.pg_next.setFixedWidth(28)
        self.pg_prev.setToolTip("Предыдущий день")
        self.pg_next.setToolTip("Следующий день")
        self.pg_date = QLabel("")
        self.pg_date.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pg_today = QPushButton("Сегодня")
        set_icon(self.pg_today, "today")
        self.pg_today.setToolTip("Вернуться к сегодняшнему дню")
        pnav.addWidget(self.pg_prev)
        pnav.addWidget(self.pg_date, 1)
        pnav.addWidget(self.pg_next)
        pnav.addWidget(self.pg_today)
        panel_lay.addLayout(pnav)

        self.program = QTreeWidget()
        self.program.setColumnCount(2)
        self.program.setHeaderLabels(["Время", "Передача"])
        self.program.setRootIsDecorated(False)
        self.program.setUniformRowHeights(True)   # иконок нет — строки ровные
        self.program.setAlternatingRowColors(True)
        self.program.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.program.setColumnWidth(0, 66)
        self.program.header().setStretchLastSection(True)
        panel_lay.addWidget(self.program, 1)

        self.splitter.addWidget(self.program_panel)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 0)
        self.splitter.setSizes([300, 620, 280])
        root.addWidget(self.splitter, 1)

        self.statusBar().showMessage("Готово")

        # диалог настроек (прокси); значения грузятся/сохраняются им самим
        self.settings_dlg = SettingsDialog(self.settings, self)

        # сигналы
        self.search.textChanged.connect(self.apply_filter)
        self.refresh_btn.clicked.connect(self.refresh_channels)
        self.playlist_btn.clicked.connect(self.toggle_playlist)
        self.program_btn.clicked.connect(self.toggle_program)
        self.pg_prev.clicked.connect(lambda: self._shift_program_date(-1))
        self.pg_next.clicked.connect(lambda: self._shift_program_date(1))
        self.pg_today.clicked.connect(self._program_today)
        self.fs_btn.clicked.connect(self.toggle_fullscreen)
        self.settings_btn.clicked.connect(self.open_settings)
        self.fs_request.connect(self._on_fs_request)

        # горячие клавиши, когда фокус у Qt-окна
        for key in ("F11", "F"):
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.ShortcutContext.ApplicationShortcut)
            sc.activated.connect(self.toggle_fullscreen)
        esc = QShortcut(QKeySequence("Esc"), self)
        esc.setContext(Qt.ShortcutContext.ApplicationShortcut)
        esc.activated.connect(self.leave_fullscreen)
        pl = QShortcut(QKeySequence("Ctrl+L"), self)
        pl.setContext(Qt.ShortcutContext.ApplicationShortcut)
        pl.activated.connect(self.toggle_playlist)
        pg = QShortcut(QKeySequence("Ctrl+P"), self)
        pg.setContext(Qt.ShortcutContext.ApplicationShortcut)
        pg.activated.connect(self.toggle_program)
        st = QShortcut(QKeySequence("Ctrl+,"), self)
        st.setContext(Qt.ShortcutContext.ApplicationShortcut)
        st.activated.connect(self.open_settings)

        self.list.itemClicked.connect(self.play_item)
        self.list.itemActivated.connect(self.play_item)
        # правый клик по каналу — «Редактировать канал…» (название/группа)
        self.list.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._list_context_menu)

        # кнопка выхода из полного экрана + опрос мыши (показ/скрытие)
        self.fs_overlay = FullscreenOverlay(self)
        self.fs_overlay.exit_clicked.connect(self.leave_fullscreen)
        self.fs_timer = QTimer(self)
        self.fs_timer.setInterval(150)
        self.fs_timer.timeout.connect(self._poll_cursor)

        # подсветка текущей передачи (обновляем раз в минуту)
        self.program_timer = QTimer(self)
        self.program_timer.setInterval(60_000)
        self.program_timer.timeout.connect(
            lambda: self._highlight_current_program(scroll=False))
        self.program_timer.start()
        self._set_program_message(
            "Выберите канал — программа передач появится здесь")
        self._sync_program_dates()   # дата в шапке панели (сегодня)

    # ---------- настройки ----------
    def open_settings(self):
        self.settings_dlg.exec()

    def _load_cache(self):
        """Формат v2: {id: [title, category, description]}; v1: {id: title}."""
        raw = self.settings.value("channels_cache", "", type=str)
        if not raw:
            return
        try:
            data = json.loads(raw)
        except Exception:
            return
        entries = []
        for vid, val in data.items():
            if isinstance(val, str):
                entries.append({"id": vid, "title": val,
                                "category": None, "description": ""})
            elif isinstance(val, (list, tuple)) and val:
                entries.append({
                    "id": vid,
                    "title": val[0],
                    "category": val[1] if len(val) > 1 else None,
                    "description": val[2] if len(val) > 2 else "",
                })
        if entries:
            self.add_channels(entries)
            self._start_prefetch()   # фоном — подкатегории и логотипы

    def _save_cache(self):
        data = {}
        for vid, item in self._items_by_id.items():
            node = item.parent()
            data[vid] = [
                item.text(0),
                node.text(0) if node is not None else "",
                self._desc_cache.get(vid, ""),
            ]
        self.settings.setValue("channels_cache", json.dumps(data, ensure_ascii=False))

    def _load_synonyms(self):
        """Список синонимов — отдельно от кеша списка: {id: [название, группа]}.

        Сырые названия/группы приходят от rutube, а отредактированные
        пользователем значения всегда подгружаются поверх них по id
        (при каждом обновлении списка в том числе).
        """
        raw = self.settings.value("synonyms", "", type=str)
        if not raw:
            return
        try:
            data = json.loads(raw)
        except Exception:
            return
        for vid, val in (data or {}).items():
            if isinstance(val, (list, tuple)) and len(val) >= 2:
                ov = {}
                if val[0]:
                    ov["title"] = str(val[0])
                ov["group"] = str(val[1] or "")
                self.synonyms[vid] = ov
            elif isinstance(val, dict):
                ov = {}
                if val.get("title"):
                    ov["title"] = str(val["title"])
                if "group" in val:
                    ov["group"] = str(val["group"] or "")
                if ov:
                    self.synonyms[vid] = ov

    def _save_synonyms(self):
        # хранится как список «id, название, группа»: {id: [название, группа]}
        data = {vid: [ov.get("title", ""), ov.get("group", "")]
                for vid, ov in self.synonyms.items()}
        self.settings.setValue("synonyms",
                               json.dumps(data, ensure_ascii=False))

    # ---------- прокси ----------
    def api_proxies(self):
        d = self.settings_dlg
        return build_requests_proxies(d.api_check.isChecked(), d.api_edit.text())

    def stream_proxy(self):
        d = self.settings_dlg
        if not d.stream_check.isChecked():
            return None
        return normalize_proxy(d.stream_edit.text())

    # ---------- каналы ----------
    def refresh_channels(self):
        if self.channels_worker and self.channels_worker.isRunning():
            self.channels_worker.stop()
            self.channels_worker.wait(3000)

        self.list.clear()
        self.known_ids.clear()
        self._cat_nodes.clear()
        self._items_by_id.clear()

        self.refresh_btn.setEnabled(False)
        w = ChannelsWorker(self.api_proxies(), self)
        w.batch.connect(self.add_channels)
        w.status.connect(self._on_worker_status)
        w.finished.connect(self._channels_done)
        self.channels_worker = w
        w.start()

    def _on_worker_status(self, text):
        self._last_status = text
        self.statusBar().showMessage(text)

    def _channels_done(self):
        self.refresh_btn.setEnabled(True)
        count = len(self._items_by_id)
        if count:
            self.statusBar().showMessage(f"Каналов: {count}")
            self._save_cache()
            self._start_prefetch()
        else:
            # оставляем в строке состояния причину из воркера (ошибка/пустой ответ)
            self.statusBar().showMessage(
                getattr(self, "_last_status", "") or "Каналы не найдены")

    def add_channels(self, found: list):
        for entry in found:
            vid = entry.get("id")
            ov = self.synonyms.get(vid) or {}
            # правка пользователя (название) сильнее исходного списка
            title = (ov.get("title") or "").strip() \
                or clean_title(entry.get("title"))   # без «Прямой эфир…»
            if (not vid or not title or vid in self.known_ids
                    or vid in self._blocked_ids):
                continue   # заблокированный (blocking_rule) не возвращаем
            self.known_ids.add(vid)

            # группа: правка (в т.ч. «без группы») против исходной
            category = ov["group"] if "group" in ov else entry.get("category")
            node = self._category_node(category)
            item = QTreeWidgetItem([title])
            item.setData(0, ID_ROLE, vid)
            if node is not None:
                node.addChild(item)
            else:
                self.list.addTopLevelItem(item)
            self._items_by_id[vid] = item

            desc = (entry.get("description") or "").strip() \
                or self._desc_cache.get(vid, "")
            if desc:
                self._desc_cache[vid] = desc
                item.setToolTip(0, desc)
            if vid in self._info_by_id:
                # метаданные уже известны (список обновлялся) — применим их
                self._apply_item_info(vid)

        # пересчитываем видимость с учётом поисковой строки
        self.apply_filter(self.search.text())

    def _category_node(self, name):
        """Узел-категория (None -> канал лежит на верхнем уровне)."""
        name = clean_category(name)
        if not name:
            return None
        node = self._cat_nodes.get(name)
        if node is None:
            node = QTreeWidgetItem([name])
            node.setData(0, CAT_ROLE, True)
            # по клику на категорию ничего не воспроизводим
            node.setFlags(node.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self.list.addTopLevelItem(node)
            node.setExpanded(True)
            self._cat_nodes[name] = node
        return node

    def apply_filter(self, text):
        needle = text.strip().lower()
        for i in range(self.list.topLevelItemCount()):
            node = self.list.topLevelItem(i)
            if node.data(0, CAT_ROLE):
                shown = 0
                for j in range(node.childCount()):
                    child = node.child(j)
                    vis = not needle or needle in child.text(0).lower()
                    child.setHidden(not vis)
                    shown += vis
                # категорию прячем, когда внутри не осталось ничего видимого
                node.setHidden(bool(needle) and shown == 0)
            else:
                node.setHidden(bool(needle) and needle not in node.text(0).lower())

    # ---------- список синонимов: правка названия/группы канала ----------
    def _list_context_menu(self, pos):
        item = self.list.itemAt(pos)
        if item is None or not item.data(0, ID_ROLE):
            return   # категории не редактируем — только каналы
        menu = QMenu(self)
        act = menu.addAction("Редактировать канал…")
        if menu.exec(self.list.viewport().mapToGlobal(pos)) is act:
            self._edit_channel_dialog(item)

    def _edit_channel_dialog(self, item):
        vid = item.data(0, ID_ROLE)
        if not vid:
            return
        cur_group = item.parent().text(0) if item.parent() is not None else ""
        dlg = EditChannelDialog(item.text(0), cur_group,
                                sorted(self._cat_nodes), self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.apply_channel_edit(vid, *dlg.values())

    def apply_channel_edit(self, vid, title, group):
        """Записывает правку в отдельный список синонимов
        ({id: [название, группа]}) и применяет её к дереву.

        Группа может быть новой; в синоним пишется полная пара
        «название + группа» — она сильнее всего, что приходит от rutube
        (включая уточнения группы из play/options) и подгружается по id
        при каждом обновлении списка.
        """
        item = self._items_by_id.get(vid)
        if item is None:
            return
        title = (title or "").strip() or item.text(0)
        group = clean_category(group or "")
        cur_group = item.parent().text(0) if item.parent() is not None else ""
        if title == item.text(0) and group == cur_group:
            return                                   # править нечего
        self.synonyms[vid] = {"title": title, "group": group}
        self._save_synonyms()
        item.setText(0, title)
        self._move_item(item, self._category_node(group))
        if vid == self._current_id:
            self._current_title = title
        self.apply_filter(self.search.text())
        self.statusBar().showMessage(
            f"Изменено: «{title}» — группа «{group or 'без группы'}»")

    # ---------- метаданные канала (описание, подкатегория, логотип) ----------
    # play/options отдаёт всё разом, поэтому один запрос закрывает и
    # подсказку, и подкатегорию, и логотип. Пачка запросов крутится в
    # фоне (до 3 параллельно), наведение/воспроизведение — только
    # поднимают нужный канал в начало очереди.
    MAX_INFO_WORKERS = 3

    def eventFilter(self, obj, event):
        if obj is self.list.viewport() and event.type() == QEvent.Type.ToolTip:
            item = self.list.itemAt(event.pos())
            vid = item.data(0, ID_ROLE) if item is not None else None
            if vid and vid not in self._info_done:
                # подсказка из списка уже может быть — тогда просто тянем
                # метаданные (подкатегория/логотип) молча
                if not item.toolTip(0):
                    item.setToolTip(0, "Загрузка описания…")
                self._request_info(vid, urgent=True)
        return super().eventFilter(obj, event)

    def _request_info(self, video_id, urgent=False):
        if (self._closing or not video_id
                or video_id in self._info_done
                or video_id in self._blocked_ids
                or video_id in self._info_pending):
            return
        if video_id in self._info_queue:
            if urgent:   # наведение не должно ждать конца предзагрузки
                self._info_queue.remove(video_id)
                self._info_queue.insert(0, video_id)
            return
        if urgent:
            self._info_queue.insert(0, video_id)
        else:
            self._info_queue.append(video_id)
        self._pump_info()

    def _pump_info(self):
        if self._closing:
            return
        while (len(self.info_workers) < self.MAX_INFO_WORKERS
               and self._info_queue):
            vid = self._info_queue.pop(0)
            if (vid in self._info_done or vid in self._info_pending
                    or vid in self._blocked_ids):
                continue
            self._info_pending.add(vid)
            w = InfoWorker(vid, self.api_proxies(), self)
            w.info.connect(self._on_info)
            w.finished.connect(lambda w=w: self._on_info_finished(w))
            self.info_workers.add(w)
            w.start()

    def _on_info_finished(self, w):
        self.info_workers.discard(w)
        self._pump_info()   # освободившийся слот — следующий из очереди

    def _on_info(self, video_id, info):
        self._info_pending.discard(video_id)
        if info.get("blocked"):
            # ответ без потока (blocking_rule / скрыто автором) — прячем
            self._hide_blocked(video_id, info.get("reason") or "")
            return
        if not info.get("ok"):
            # сбой — не помечаем выполненным, при следующем наведении повторим
            item = self._items_by_id.get(video_id)
            if item is not None and item.toolTip(0) == "Загрузка описания…":
                item.setToolTip(0, "Описание недоступно")
            return
        self._info_done.add(video_id)
        self._remember_info(video_id, info)
        item = self._items_by_id.get(video_id)
        self._refresh_live_tooltip(video_id,
                                   item.toolTip(0) if item is not None else "")

    def _start_prefetch(self):
        """После загрузки списка тихо тянем метаданные всех каналов."""
        if not self.settings_dlg.prefetch_check.isChecked():
            return
        for vid in list(self._items_by_id):
            self._request_info(vid)

    def _remember_info(self, video_id, info):
        if not video_id or not isinstance(info, dict) or not info.get("ok"):
            return
        self._info_by_id[video_id] = info
        if info.get("description"):
            self._desc_cache[video_id] = info["description"]
        self._apply_item_info(video_id)

    def _tooltip_text(self, video_id):
        """Описание из play/options, иначе имя автора, иначе описание из списка."""
        info = self._info_by_id.get(video_id) or {}
        return (info.get("description") or info.get("author")
                or self._desc_cache.get(video_id, ""))

    def _apply_item_info(self, video_id):
        """Навешивает на элемент дерева всё, что знаем о канале."""
        if video_id not in self._info_by_id:
            return
        item = self._items_by_id.get(video_id)
        if item is None:
            return
        info = self._info_by_id[video_id]
        # подсказка: описание, а если его нет — имя автора
        item.setToolTip(0, self._tooltip_text(video_id))
        # подкатегория из play/options (category -> name)
        self._apply_category(video_id, info.get("category"))
        # логотип автора (author -> avatar_url)
        self._request_avatar(video_id, info.get("avatar"))

    def _move_item(self, item, node):
        """Переносит элемент в узел категории (node=None — на верхний уровень)
        и убирает опустевшую старую группу, чтобы не мусорила в дереве."""
        old = item.parent()
        if old is node:
            return
        if old is not None:
            old.removeChild(item)
        else:
            idx = self.list.indexOfTopLevelItem(item)
            if idx >= 0:
                self.list.takeTopLevelItem(idx)
        if node is not None:
            node.addChild(item)
            node.setExpanded(True)
        else:
            self.list.addTopLevelItem(item)
        if old is not None and old.childCount() == 0:
            idx = self.list.indexOfTopLevelItem(old)
            if idx >= 0:
                self.list.takeTopLevelItem(idx)   # без удаления из дерева
            self._cat_nodes.pop(old.text(0), None)

    def _apply_category(self, video_id, category):
        category = str(category or "").strip()
        item = self._items_by_id.get(video_id)
        if not category or item is None:
            return
        if "group" in (self.synonyms.get(video_id) or {}):
            return   # группа задана в синонимах — play/options её не двигает
        self._move_item(item, self._category_node(category))
        self.apply_filter(self.search.text())

    # ---------- недоступные каналы (blocking_rule, скрыто автором) ----------
    def _hide_blocked(self, video_id, reason=""):
        """play/options без потока — убираем канал из списка.

        Такие ответы бывают, когда видео заблокировано по решению
        правообладателя/из-за VPN (blocking_rule) либо скрыто автором
        и доступно только авторизованным (player_stub/login_required).
        id запоминаем на сессию, чтобы список (и очередь метаданных)
        его не вернул; кеш переписываем без него.
        """
        self._blocked_ids.add(video_id)
        if reason:
            self._blocked_reasons[video_id] = reason
        self._info_done.add(video_id)          # повторно не вытягиваем
        self._info_pending.discard(video_id)
        if video_id in self._info_queue:
            self._info_queue.remove(video_id)
        self._info_by_id.pop(video_id, None)
        self._desc_cache.pop(video_id, None)
        self._avatar_url_by_id.pop(video_id, None)
        if video_id == self._current_id:
            self._current_id = None
        if video_id == self._program_id:
            self._program_id = None
            if self.program_visible:
                self._set_program_message("Канал недоступен — программы нет")

        item = self._items_by_id.pop(video_id, None)
        if item is None:
            return                             # уже убран (двойной ответ)
        title = item.text(0)
        old = item.parent()
        if old is not None:
            old.removeChild(item)
        else:
            idx = self.list.indexOfTopLevelItem(item)
            if idx >= 0:
                self.list.takeTopLevelItem(idx)
        # пустую категорию убираем, чтобы не мусорила в дереве
        if old is not None and old.childCount() == 0:
            idx = self.list.indexOfTopLevelItem(old)
            if idx >= 0:
                self.list.takeTopLevelItem(idx)
            self._cat_nodes.pop(old.text(0), None)

        self.apply_filter(self.search.text())
        self._save_cache()                     # не вернётся из кеша
        self.statusBar().showMessage(
            f"Скрыт недоступный канал: {title}")

    # ---------- логотипы ----------
    def _request_avatar(self, video_id, url):
        if self._closing or not video_id or not url:
            return
        self._avatar_url_by_id[video_id] = url
        icon = self._icons.get(url)
        if icon is not None:
            item = self._items_by_id.get(video_id)
            if item is not None and item.icon(0).isNull():
                item.setIcon(0, icon)
            return
        if url in self._avatar_pending or url in self._avatar_failed:
            return
        self._avatar_pending.add(url)
        w = AvatarWorker(url, self.api_proxies(), self)
        w.avatar.connect(self._on_avatar)
        w.finished.connect(lambda w=w: self.avatar_workers.discard(w))
        self.avatar_workers.add(w)
        w.start()

    def _on_avatar(self, url, data):
        self._avatar_pending.discard(url)
        pm = QPixmap()
        if not data or not pm.loadFromData(data):
            self._avatar_failed.add(url)
            return
        icon = QIcon(pm)
        self._icons[url] = icon
        for vid, u in self._avatar_url_by_id.items():
            if u == url:
                item = self._items_by_id.get(vid)
                if item is not None:
                    item.setIcon(0, icon)

    def _refresh_live_tooltip(self, video_id, text):
        """Если подсказка ещё держится над тем же каналом — обновим её."""
        vp = self.list.viewport()
        local = vp.mapFromGlobal(QCursor.pos())
        if not vp.rect().contains(local):
            return
        item = self.list.itemAt(local)
        if item is not None and item.data(0, ID_ROLE) == video_id and text:
            QToolTip.showText(QCursor.pos(), text)

    # ---------- плейлист (показ/скрытие) ----------
    def toggle_playlist(self):
        self.playlist_visible = not self.playlist_visible
        self._apply_playlist(self.playlist_visible)
        self._sync_playlist_btn()

    def _apply_playlist(self, visible):
        # вместе со списком прячем и ручку сплита, иначе у края остаётся
        # полоса, за которую нечего перетаскивать
        self.list.setVisible(visible)
        handle = self.splitter.handle(1)
        if handle is not None:
            handle.setVisible(visible)

    def _sync_playlist_btn(self):
        self.playlist_btn.setText(
            "Показать список" if not self.playlist_visible else "Скрыть список")

    # ---------- программа передач ----------
    def toggle_program(self):
        self.program_visible = not self.program_visible
        self._apply_program(self.program_visible)
        self._sync_program_btn()
        if self.program_visible:
            # показываем то, что уже выбрано (из кеша или новым запросом)
            self._load_program(self._program_id, self._program_date)

    def _apply_program(self, visible):
        # как и со списком — прячем и ручку сплита, иначе у края
        # остаётся полоса, за которую нечего перетаскивать
        self.program_panel.setVisible(visible)
        handle = self.splitter.handle(2)
        if handle is not None:
            handle.setVisible(visible)

    def _sync_program_btn(self):
        self.program_btn.setText(
            "Показать программу" if not self.program_visible
            else "Скрыть программу")

    def _load_program(self, video_id, date_iso):
        """Показывает программу канала на дату (из кеша или новым запросом)."""
        self._program_id = video_id
        self._program_date = date_iso or today_iso()
        self._sync_program_dates()
        if not video_id:
            self._set_program_message(
                "Выберите канал — программа передач появится здесь")
            return
        if not self.program_visible:
            return   # тянем, когда панель покажут (см. toggle_program)
        cached = self._program_cache.get((video_id, self._program_date))
        if cached is not None:
            self._fill_program(cached)
            return
        self._set_program_message("Загрузка программы…")
        if self._closing:
            return
        w = ProgramWorker(video_id, self._program_date,
                          self.api_proxies(), self)
        w.ready.connect(self._on_program)
        w.finished.connect(lambda w=w: self.program_workers.discard(w))
        self.program_workers.add(w)
        w.start()

    def _on_program(self, video_id, date_iso, result):
        if (video_id, date_iso) != (self._program_id, self._program_date):
            return   # пользователь уже переключился на другой канал/дату
        if result.get("ok"):
            # ошибки не кешируем — при следующем показе повторим запрос
            self._program_cache[(video_id, date_iso)] = result
        self._fill_program(result)

    def _fill_program(self, result):
        items = result.get("items") or []
        if not result.get("ok"):
            self._set_program_message("Не удалось загрузить программу")
            return
        if not items:
            self._set_program_message("На эту дату программы нет")
            return
        self.program.clear()
        for p in items:
            # страховка: fetch_program обязан вернуть datetime, но падать
            # в GUI-слоте из-за неразобранной строки не хочется
            start, end = p.get("start"), p.get("end") or p.get("stop")
            if not isinstance(start, datetime):    # строка/epoch/None
                start = parse_program_time(start)
            if not isinstance(end, datetime):
                end = parse_program_time(end, start.date() if start else None)
            it = QTreeWidgetItem([
                program_time_label({"start": start, "end": end}),
                str(p.get("title") or "—")])
            it.setData(0, PSTART_ROLE, start)
            it.setData(0, PEND_ROLE, end)
            tip = p.get("desc") or p.get("title") or ""
            it.setToolTip(0, tip)
            it.setToolTip(1, tip)
            self.program.addTopLevelItem(it)
        self._highlight_current_program(scroll=True)

    def _set_program_message(self, text):
        self.program.clear()
        it = QTreeWidgetItem([text])
        it.setFirstColumnSpanned(True)
        it.setFlags(Qt.ItemFlag.NoItemFlags)   # не выделяется, не кликается
        it.setForeground(0, QBrush(QColor("#777777")))
        self.program.addTopLevelItem(it)

    def _highlight_current_program(self, scroll=True):
        """Выделяет текущую передачу (или ближайшую) и скроллит к ней."""
        if not self.program.isVisible():
            return
        now = datetime.now()
        items = [self.program.topLevelItem(i)
                 for i in range(self.program.topLevelItemCount())]
        cur = None
        for it in items:                       # идёт прямо сейчас
            start, end = it.data(0, PSTART_ROLE), it.data(0, PEND_ROLE)
            if not isinstance(start, datetime):
                continue
            if now >= start and (end is None or not isinstance(end, datetime)
                                 or now < end):
                cur = it
                break
        if cur is None:                        # ещё не начиналась
            for it in items:
                start = it.data(0, PSTART_ROLE)
                if isinstance(start, datetime) and start >= now:
                    cur = it
                    break
        for it in items:
            bold = it is cur
            font = it.font(0)
            font.setBold(bold)
            it.setFont(0, font)
            it.setFont(1, font)
            if bold:
                bg = QBrush(QColor("#2f5fa8"))
                fg = QBrush(QColor(Qt.GlobalColor.white))
            else:
                bg, fg = QBrush(), QBrush()
            it.setBackground(0, bg)
            it.setBackground(1, bg)
            it.setForeground(0, fg)
            it.setForeground(1, fg)
        if cur is not None and scroll:
            self.program.scrollToItem(
                cur, QAbstractItemView.ScrollHint.PositionAtCenter)

    def _shift_program_date(self, days):
        if not self._program_id:
            return
        try:
            base = datetime.strptime(self._program_date, "%Y-%m-%d")
        except ValueError:
            base = datetime.now()
        self._load_program(self._program_id,
                           (base + timedelta(days=days)).strftime("%Y-%m-%d"))

    def _program_today(self):
        if not self._program_id:
            return
        self._load_program(self._program_id, today_iso())

    def _sync_program_dates(self):
        try:
            d = datetime.strptime(self._program_date, "%Y-%m-%d")
        except ValueError:
            d = datetime.now()
        text = f"{d:%d.%m.%Y} ({WEEKDAYS[d.weekday()]})"
        if d.date() == datetime.now().date():
            text = "Сегодня, " + text
        self.pg_date.setText(text)
        self.pg_today.setEnabled(d.date() != datetime.now().date())

    # ---------- воспроизведение ----------
    def play_item(self, item: QTreeWidgetItem, column: int = 0):
        video_id = item.data(0, ID_ROLE)
        if not video_id:
            return  # это категория, а не канал
        if video_id in self._blocked_ids:
            self.statusBar().showMessage(
                "Канал недоступен: "
                + self._blocked_reasons.get(video_id, "видео недоступно"))
            return

        self.stream_token += 1
        token = self.stream_token
        self._current_id = video_id

        self.statusBar().showMessage(f"Получение потока: {item.text(0)}...")
        self._current_title = item.text(0)

        # get_stream_by_id — только для выбранного канала
        w = StreamWorker(video_id, token, self.api_proxies(), self)
        w.resolved.connect(self._stream_resolved)
        w.finished.connect(lambda w=w: self.stream_workers.discard(w))
        self.stream_workers.add(w)
        w.start()

        # программа передач: на новый канал — свежая (сегодняшняя) дата
        self._load_program(video_id, today_iso())

    def _stream_resolved(self, video_id, token, info):
        # ответ без потока — канал прячем и называем причину
        if isinstance(info, dict) and info.get("blocked"):
            reason = info.get("reason") or "видео недоступно"
            self._hide_blocked(video_id, reason)
            if token == self.stream_token:
                self.statusBar().showMessage(f"Канал недоступен: {reason}")
            return
        # тот же play/options дал описание/подкатегорию/аватар — применяем
        # даже если пользователь уже переключился на другой канал
        if isinstance(info, dict):
            self._remember_info(video_id, info)
        if token != self.stream_token:
            return  # пользователь уже выбрал другой канал
        url = info.get("url") if isinstance(info, dict) else None
        if not url:
            self.statusBar().showMessage("Поток не найден")
            return

        player = self.ensure_player()

        # Stream proxy — только для mpv
        proxy = self.stream_proxy()
        try:
            player["http-proxy"] = proxy or ""
        except Exception:
            pass

        player.play(url)
        self.statusBar().showMessage(f"Играет: {self._current_title}")

    # ---------- полноэкранный режим ----------
    # mpv встроен в наш виджет через wid, у него нет собственного окна,
    # поэтому его свойство fullscreen ничего не делает. Разворачиваем
    # окно Qt и прячем всё, кроме видео.
    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.leave_fullscreen()
        else:
            self.enter_fullscreen()

    def enter_fullscreen(self):
        if self.isFullScreen():
            return
        self._was_maximized = self.isMaximized()
        self.controls.hide()
        self._apply_playlist(False)
        self._apply_program(False)
        self.statusBar().hide()
        self.centralWidget().layout().setContentsMargins(0, 0, 0, 0)
        self.showFullScreen()
        self._fs_last_pos = None
        self.fs_timer.start()
        QTimer.singleShot(200, self._show_fs_overlay)
        self._sync_mpv_fullscreen(True)

    def leave_fullscreen(self):
        if not self.isFullScreen():
            return
        self.fs_timer.stop()
        self.fs_overlay.hide()
        self.controls.show()
        self._apply_playlist(self.playlist_visible)  # учесть скрытие вручную
        self._apply_program(self.program_visible)
        if self.program_visible:
            self._highlight_current_program(scroll=False)
        self.statusBar().show()
        self.centralWidget().layout().setContentsMargins(9, 9, 9, 9)
        if self._was_maximized:
            self.showMaximized()
        else:
            self.showNormal()
        self._sync_mpv_fullscreen(False)

    def _show_fs_overlay(self):
        if not self.isFullScreen():
            return
        self.fs_overlay.place_top_right(self.screen().geometry())
        self.fs_overlay.show()
        self.fs_overlay.raise_()
        self._fs_last_move = time.monotonic()

    def _poll_cursor(self):
        # События мыши над mpv до Qt не доходят, поэтому следим за курсором
        # напрямую: двигается — показываем кнопку, 3 с покоя — прячем.
        if not self.isFullScreen():
            self.fs_timer.stop()
            return
        pos = QCursor.pos()
        now = time.monotonic()
        if pos != self._fs_last_pos:
            self._fs_last_pos = pos
            self._fs_last_move = now
            if not self.fs_overlay.isVisible():
                self._show_fs_overlay()
        elif (self.fs_overlay.isVisible()
              and now - self._fs_last_move > 3.0
              and not self.fs_overlay.geometry().contains(pos)):
            self.fs_overlay.hide()

    def _on_fs_request(self, action):
        if action == "toggle":
            self.toggle_fullscreen()
        elif action == "enter":
            self.enter_fullscreen()
        else:
            self.leave_fullscreen()

    def _observe_mpv_fullscreen(self, player):
        """Значок полного экрана в OSC mpv -> полный экран окна Qt.

        OSC переключает свойство fullscreen самого mpv, а разворачиваем
        окно на самом деле мы (см. комментарий над toggle_fullscreen):
        без этой подписки клик по значку выглядит как «не реагирует».
        Колбэк приходит в потоке mpv, поэтому в GUI шлём только сигнал.
        """
        def on_fullscreen(_name, value):
            if value is None:        # свойство ещё не доступно
                return
            self.fs_request.emit("enter" if value else "leave")

        try:
            player.observe_property("fullscreen", on_fullscreen)
        except Exception:
            traceback.print_exc()

    def _sync_mpv_fullscreen(self, value):
        """Держим значок OSC mpv в согласии с состоянием окна Qt."""
        if self.player is None:
            return
        try:
            if bool(self.player.fullscreen) != value:
                self.player.fullscreen = value
        except Exception:
            pass

    def _bind_mpv_keys(self, player):
        # Когда фокус внутри окна mpv, клавиши и мышь получает он, а не Qt.
        # Привязки вызываются в потоке mpv, поэтому только шлём сигнал.
        # python-mpv 1.0.8 вызывает колбэк с 5 аргументами
        # (state, key, char, scale, arg), state вида «p--» (нажатие);
        # принимать надо любое число аргументов, иначе TypeError глотается
        # event loop'ом python-mpv и привязка молча не работает.
        def make(action):
            def handler(state="p", *rest):
                # реагируем на нажатие, а не на отпускание клавиши
                if state and state[0] in ("d", "p"):
                    self.fs_request.emit(action)
            return handler

        for key, action in (
            ("f", "toggle"),
            ("F11", "toggle"),
            ("MBTN_LEFT_DBL", "toggle"),
            ("ESC", "leave"),
        ):
            try:
                player.register_key_binding(key, make(action))
            except Exception:
                traceback.print_exc()

    def ensure_player(self):
        if self.player is None:
            self.player = mpv.MPV(
                wid=str(int(self.video.winId())),
                input_default_bindings=False,
                input_vo_keyboard=True,
                osc=True,
            )
            self._bind_mpv_keys(self.player)
            self._observe_mpv_fullscreen(self.player)
        return self.player

    # ---------- завершение ----------
    def _running_workers(self):
        """Потоки, которые ещё работают прямо сейчас."""
        out = []
        if self.channels_worker is not None and self.channels_worker.isRunning():
            out.append(self.channels_worker)
        out += [w for w in list(self.stream_workers) + list(self.info_workers)
                + list(self.avatar_workers)
                + list(self.program_workers) if w.isRunning()]
        return out

    def _shutdown_player(self):
        if self.player is None: return
        try:
            self.player.terminate()
        except Exception:
            return

    def closeEvent(self, event):
        # Закрытие откладываем, пока живы фоновые потоки: иначе mpv и
        # фоновые запросы оборвутся посреди работы. Повторный вызов при
        # уже идущем выходе просто игнорируется.
        if self._close_pending:
            event.ignore()
            return
        self._closing = True
        self._info_queue.clear()
        self.fs_timer.stop()
        self.fs_overlay.hide()
        if self.channels_worker is not None:
            self.channels_worker.stop()

        workers = self._running_workers()
        deadline = time.monotonic() + 2
        for w in workers:
            left = int((deadline - time.monotonic()) * 1000)
            if left <= 0:
                break
            try:
                w.wait(left)
            except Exception:
                continue
        still = [w for w in workers if w.isRunning()]
        if still:
            event.ignore()
            self._close_pending = still
            self.hide()
            self.statusBar().showMessage(
                "Дожидаемся завершения фоновых запросов…")
            self._close_timer = QTimer(self)
            self._close_timer.setInterval(100)
            self._close_timer.timeout.connect(self._poll_close)
            self._close_timer.start()
            return
        self._shutdown_player()
        super().closeEvent(event)

    def _poll_close(self):
        """Выходим сами, когда дождались висящих в сети потоков."""
        if any(w.isRunning() for w in self._close_pending):
            return
        self._close_timer.stop()
        self._close_pending = None
        self._shutdown_player()
        QApplication.quit()


def main():
    app = QApplication(sys.argv)

    app.setOrganizationName("rutube-efir")
    app.setApplicationName("rutube-efir")

    locale.setlocale(locale.LC_NUMERIC, "C")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
