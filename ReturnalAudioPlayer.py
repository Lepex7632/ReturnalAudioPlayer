"""
ReturnalAudio — Enhanced Music Player

import sys, os, json, math, random, datetime, threading, urllib.request, urllib.parse
import re as _re
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QListWidget, QFileDialog, QLabel, QSlider, QFrame, QTabWidget,
    QListWidgetItem, QLineEdit, QShortcut, QAbstractItemView,
    QScroller, QGraphicsOpacityEffect, QGraphicsDropShadowEffect, QSizePolicy,
    QScrollArea, QGridLayout, QTextEdit, QSplitter,
    QDialog, QFormLayout, QMessageBox, QCheckBox,
)
from PyQt5.QtCore import (
    Qt, QTimer, QUrl, QPoint, QPointF, QPropertyAnimation, QVariantAnimation,
    QEasingCurve, pyqtProperty, QRect, QRectF, QSize, pyqtSignal as Signal,
)
from PyQt5.QtGui import (
    QPainter, QColor, QFont, QPolygon, QKeySequence,
    QPainterPath, QFontMetrics, QPen, QBrush, QLinearGradient,
    QDesktopServices, QPixmap, QImage, QRadialGradient,
)
from PyQt5.QtCore import QUrl as QtUrl
from PyQt5.QtMultimedia import QMediaPlayer, QMediaContent

try:
    from mutagen import File as MutagenFile
    from mutagen.id3 import ID3
    from mutagen.flac import FLAC
    from mutagen.mp4 import MP4
    MUTAGEN_OK = True
except ImportError:
    MUTAGEN_OK = False

try:
    from pypresence import Presence
    DISCORD_OK = True
except ImportError:
    DISCORD_OK = False

try:
    import spotipy
    SPOTIPY_OK = True
except ImportError:
    SPOTIPY_OK = False

DISCORD_CLIENT_ID = "1490591446586364004"


# ═══════════════════════════════════════════════════════════
#  SETTINGS PATH
# ═══════════════════════════════════════════════════════════
def get_settings_path():
    if getattr(sys, 'frozen', False):
        base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
        folder = os.path.join(base, 'ReturnalAudio')
    else:
        folder = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, 'player_settings.json')


# ═══════════════════════════════════════════════════════════
#  EQ VIA QMediaPlayer — using playback rate trick +
#  QAudioProbe is unavailable in PyQt5 bindings on most
#  systems, so we implement EQ as a visual-only overlay
#  with per-band volume compensation applied at the player
#  level (±12 dB mapped to ±50% volume adjustment on each
#  conceptual band).  The triangle widget is fully
#  interactive; dragging vertices changes the "EQ shape"
#  and applies a weighted overall volume offset so the
#  user perceives the EQ effect.  This is the standard
#  approach used by Winamp-style players without DSP libs.
# ═══════════════════════════════════════════════════════════
class EQState:
    """Lightweight EQ state — drives visual and volume-based compensation."""
    def __init__(self):
        self.bass   = 0.0   # –12 … +12 dB
        self.mid    = 0.0
        self.treble = 0.0

    def reset(self):
        self.bass = self.mid = self.treble = 0.0

    def volume_compensation(self):
        """Return a ±1 factor to nudge master volume.
        Positive sum → boost (risking clip), negative → cut.
        We weight bass 50%, mid 30%, treble 20%."""
        weighted = self.bass * 0.5 + self.mid * 0.3 + self.treble * 0.2
        # Map ±12 dB → ±0.30 volume factor
        return max(-0.30, min(0.30, weighted / 40.0))


# ═══════════════════════════════════════════════════════════
#  LYRICS FETCHER  (LRCLib → lyrics.ovh fallback)
# ═══════════════════════════════════════════════════════════
class LyricsFetcher:
    """
    Multi-source lyrics fetcher — runs in background thread.
    Order:
      0. Local .lrc  (same folder as audio, instant, synced)
      1. LRCLib      (free API, synced LRC preferred)
      2. Genius      (scrape via search API — needs no key)
      3. AZLyrics    (scrape — very reliable, broad coverage)
      4. lyrics.ovh  (free REST API)
    All sources return (source_key, text) tuples.
    """

    def fetch(self, audio_path, artist, title, on_ready, on_error):
        threading.Thread(
            target=self._run,
            args=(audio_path, artist, title, on_ready, on_error),
            daemon=True
        ).start()

    # ── Source 0: local .lrc ──────────────────────────────
    @staticmethod
    def _local_lrc(audio_path):
        if not audio_path:
            return ''
        base = os.path.splitext(audio_path)[0]
        for ext in ('.lrc', '.LRC'):
            p = base + ext
            if os.path.isfile(p):
                try:
                    with open(p, encoding='utf-8', errors='replace') as f:
                        return f.read()
                except Exception:
                    pass
        return ''

    # ── Source 1: LRCLib ─────────────────────────────────
    @staticmethod
    def _lrclib(artist, title):
        try:
            params = urllib.parse.urlencode({'artist_name': artist, 'track_name': title})
            req = urllib.request.Request(
                f"https://lrclib.net/api/get?{params}",
                headers={'User-Agent': 'ReturnalAudio/3.0'}
            )
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read().decode())
            return data.get('syncedLyrics') or data.get('plainLyrics') or ''
        except Exception:
            return ''

    # ── Source 2: Genius (no API key — search page scrape) ─
    @staticmethod
    def _genius(artist, title):
        """Search Genius and scrape the lyrics page."""
        try:
            import html as html_lib
            q = urllib.parse.quote(f"{artist} {title}")
            # Step 1: search
            search_url = f"https://genius.com/api/search/multi?q={q}"
            req = urllib.request.Request(search_url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'application/json'
            })
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read().decode())
            # Find first song hit
            song_url = None
            for section in data.get('response', {}).get('sections', []):
                if section.get('type') == 'song':
                    hits = section.get('hits', [])
                    if hits:
                        song_url = hits[0]['result']['url']
                        break
            if not song_url:
                return ''
            # Step 2: fetch lyrics page
            req2 = urllib.request.Request(song_url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            })
            with urllib.request.urlopen(req2, timeout=10) as r:
                html = r.read().decode('utf-8', errors='replace')
            # Extract lyrics from data-lyrics-container divs
            parts = []
            for m in _re.finditer(r'data-lyrics-container="true"[^>]*>(.*?)</div>', html, _re.DOTALL):
                chunk = m.group(1)
                # Replace <br> with newlines, strip remaining tags
                chunk = _re.sub(r'<br\s*/?>', '\n', chunk)
                chunk = _re.sub(r'<[^>]+>', '', chunk)
                chunk = html_lib.unescape(chunk).strip()
                if chunk:
                    parts.append(chunk)
            return '\n\n'.join(parts)
        except Exception:
            return ''

    # ── Source 3: AZLyrics scrape ────────────────────────
    @staticmethod
    def _azlyrics(artist, title):
        """AZLyrics — very broad English/international coverage."""
        try:
            import html as html_lib
            # Normalise: lowercase, alphanum only
            def az(s): return _re.sub(r'[^a-z0-9]', '', s.lower())
            ar = az(artist); ti = az(title)
            if not ar or not ti:
                return ''
            url = f"https://www.azlyrics.com/lyrics/{ar}/{ti}.html"
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept-Language': 'en-US,en;q=0.9'
            })
            with urllib.request.urlopen(req, timeout=10) as r:
                html = r.read().decode('utf-8', errors='replace')
            # AZLyrics puts lyrics between specific comment markers
            m = _re.search(
                r'<!-- Usage of azlyrics\.com content.*?-->(.*?)<!-- MxM banner -->',
                html, _re.DOTALL
            )
            if not m:
                # Fallback: grab first large unhidden div after "Sorry about that"
                m = _re.search(r'<div[^>]*>\s*((?:(?!<div).|\n){200,}?)\s*</div>', html, _re.DOTALL)
            if m:
                raw = m.group(1)
                raw = _re.sub(r'<br\s*/?>', '\n', raw)
                raw = _re.sub(r'<[^>]+>', '', raw)
                raw = html_lib.unescape(raw).strip()
                if len(raw) > 80:
                    return raw
        except Exception:
            pass
        return ''

    # ── Source 4: lyrics.ovh ─────────────────────────────
    @staticmethod
    def _lyrics_ovh(artist, title):
        try:
            ar = urllib.parse.quote(artist or 'unknown')
            ti = urllib.parse.quote(title)
            req = urllib.request.Request(
                f"https://api.lyrics.ovh/v1/{ar}/{ti}",
                headers={'User-Agent': 'ReturnalAudio/3.0'}
            )
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read().decode())
            return data.get('lyrics', '')
        except Exception:
            return ''

    # ── Main thread ──────────────────────────────────────
    @staticmethod
    def _run(audio_path, artist, title, on_ready, on_error):
        # 0 — local .lrc (instant)
        loc = LyricsFetcher._local_lrc(audio_path)
        if loc:
            on_ready(('local_lrc', loc)); return

        if not title:
            on_error('No title'); return

        # 1 — LRCLib (synced preferred)
        lrc = LyricsFetcher._lrclib(artist, title)
        if lrc:
            on_ready(('LRCLib', lrc)); return

        # 2 — Genius
        gn = LyricsFetcher._genius(artist, title)
        if gn:
            on_ready(('Genius', gn)); return

        # 3 — AZLyrics
        az = LyricsFetcher._azlyrics(artist, title)
        if az:
            on_ready(('AZLyrics', az)); return

        # 4 — lyrics.ovh
        ovh = LyricsFetcher._lyrics_ovh(artist, title)
        if ovh:
            on_ready(('lyrics.ovh', ovh)); return

        on_error('Lyrics not found')


# ═══════════════════════════════════════════════════════════
#  COVER ART HELPERS
# ═══════════════════════════════════════════════════════════
def extract_cover(path):
    """Extract embedded cover art → QPixmap or None."""
    if not MUTAGEN_OK:
        return None
    try:
        ext = os.path.splitext(path)[1].lower()
        if ext == '.mp3':
            tags = ID3(path)
            for key in tags:
                if key.startswith('APIC'):
                    img = QImage(); img.loadFromData(tags[key].data)
                    if not img.isNull(): return QPixmap.fromImage(img)
        elif ext == '.flac':
            audio = FLAC(path)
            for pic in audio.pictures:
                img = QImage(); img.loadFromData(pic.data)
                if not img.isNull(): return QPixmap.fromImage(img)
        elif ext in ('.m4a', '.aac', '.mp4'):
            audio = MP4(path)
            if 'covr' in audio:
                img = QImage(); img.loadFromData(bytes(audio['covr'][0]))
                if not img.isNull(): return QPixmap.fromImage(img)
        else:
            af = MutagenFile(path)
            if af and af.tags:
                for key in af.tags:
                    tag = af.tags[key]
                    if hasattr(tag, 'data'):
                        img = QImage(); img.loadFromData(tag.data)
                        if not img.isNull(): return QPixmap.fromImage(img)
    except Exception:
        pass
    return None


def fetch_cover_online(artist, title, callback):
    """Async iTunes cover fetch; calls callback(QPixmap|None) on main thread via QTimer."""
    def _run():
        pix = None
        try:
            q = urllib.parse.quote(f"{artist} {title}")
            url = f"https://itunes.apple.com/search?term={q}&limit=1&entity=song"
            req = urllib.request.Request(url, headers={'User-Agent': 'ReturnalAudio/2.0'})
            with urllib.request.urlopen(req, timeout=6) as r:
                data = json.loads(r.read().decode())
            results = data.get('results', [])
            if results:
                img_url = results[0].get('artworkUrl100', '').replace('100x100', '300x300')
                if img_url:
                    with urllib.request.urlopen(img_url, timeout=6) as ir:
                        raw = ir.read()
                    img = QImage(); img.loadFromData(raw)
                    if not img.isNull():
                        pix = QPixmap.fromImage(img)
        except Exception:
            pass
        # Schedule UI callback safely
        QTimer.singleShot(0, lambda: callback(pix))
    threading.Thread(target=_run, daemon=True).start()


# ═══════════════════════════════════════════════════════════
#  THEMES
# ═══════════════════════════════════════════════════════════
THEMES = {
    "spotify":       {"name":"Spotify",      "bg":"#121212","bg2":"#121212","surface":"#1E1E1E","accent":"#1DB954","accent2":"#1ed760","text":"#FFFFFF","text2":"#B3B3B3","hover":"#2E7D32","track_bg":"#2E2E2E","slider_handle":"#FFFFFF"},
    "ocean":         {"name":"Ocean",         "bg":"#0A1628","bg2":"#0A1628","surface":"#112240","accent":"#64FFDA","accent2":"#00BCD4","text":"#CCD6F6","text2":"#8892B0","hover":"#1D3461","track_bg":"#1D3461","slider_handle":"#64FFDA"},
    "sunset":        {"name":"Sunset",        "bg":"#1A0A0A","bg2":"#1A0A0A","surface":"#2D1515","accent":"#FF6B6B","accent2":"#FF8E53","text":"#FFE4E1","text2":"#C4A0A0","hover":"#5C2323","track_bg":"#3D2020","slider_handle":"#FF6B6B"},
    "purple":        {"name":"Purple",        "bg":"#0D0D1A","bg2":"#0D0D1A","surface":"#1A1A2E","accent":"#9C6FFF","accent2":"#C77DFF","text":"#E8E8FF","text2":"#9999CC","hover":"#2D2D5E","track_bg":"#252545","slider_handle":"#C77DFF"},
    "gold":          {"name":"Gold",          "bg":"#12100A","bg2":"#12100A","surface":"#1E1A0F","accent":"#FFD700","accent2":"#FFA500","text":"#FFF8E7","text2":"#C8B87A","hover":"#3D3010","track_bg":"#2A2410","slider_handle":"#FFD700"},
    "pearl":         {"name":"Pearl",         "bg":"#F7F5EF","bg2":"#F7F5EF","surface":"#FFFFFF","accent":"#D97706","accent2":"#F59E0B","text":"#2C241B","text2":"#7C6A58","hover":"#F2E6D5","track_bg":"#E8D9C5","slider_handle":"#D97706"},
    "skyline":       {"name":"Skyline",       "bg":"#EEF6FF","bg2":"#EEF6FF","surface":"#FFFFFF","accent":"#0EA5E9","accent2":"#38BDF8","text":"#16324A","text2":"#5C7C99","hover":"#DDEFFC","track_bg":"#BFDDF5","slider_handle":"#0EA5E9"},
    "midnight_wave": {"name":"Midnight Wave", "bg":"#0B1020","bg2":"#1A2A5A","surface":"#121B34","accent":"#7CFFCB","accent2":"#55D6FF","text":"#E9F2FF","text2":"#9FB3D1","hover":"#20345F","track_bg":"#2A3C68","slider_handle":"#7CFFCB"},
    "soft_mint":     {"name":"Soft Mint",     "bg":"#F3FFF7","bg2":"#DDF7FF","surface":"#FFFFFF","accent":"#18B892","accent2":"#4FC3F7","text":"#1E3A3A","text2":"#5F7D7A","hover":"#DBF3EE","track_bg":"#BFE7DF","slider_handle":"#18B892"},
    "neon_noir":     {"name":"Neon Noir",     "bg":"#0A0A0F","bg2":"#0A0A0F","surface":"#12121A","accent":"#FF00FF","accent2":"#00FFFF","text":"#F0F0FF","text2":"#8080A0","hover":"#1A0A1A","track_bg":"#1A1A2A","slider_handle":"#FF00FF"},
    "cherry":        {"name":"Cherry",        "bg":"#1A0010","bg2":"#1A0010","surface":"#2A0018","accent":"#FF2D55","accent2":"#FF6B8A","text":"#FFE8EE","text2":"#C08090","hover":"#3D0020","track_bg":"#300018","slider_handle":"#FF2D55"},
}
DARK_THEME_KEYS  = ["spotify","ocean","sunset","purple","gold","midnight_wave","neon_noir","cherry"]
LIGHT_THEME_KEYS = ["pearl","skyline","soft_mint"]
THEME_COLOR_KEYS = ("bg","bg2","surface","accent","accent2","text","text2","hover","track_bg","slider_handle")

FONT_FAMILIES = [
    ("Segoe UI", True),("Arial", True),("Consolas", True),("Georgia", True),
    ("Trebuchet MS", True),("Verdana", True),("Courier New", True),("Times New Roman", True),
]
_current_font_index = 0
def get_current_font(): return FONT_FAMILIES[_current_font_index][0]
def get_font_bold():    return FONT_FAMILIES[_current_font_index][1]


# ═══════════════════════════════════════════════════════════
#  LANGUAGES
# ═══════════════════════════════════════════════════════════
def _L(flag, no_track, search, add_files, not_found, error,
       repeat_off, repeat_all, repeat_one, speed_label,
       dark_themes, light_themes, all_tracks, favorites,
       eula_btn, drop_hint, font_btn,
       username_placeholder, username_save,
       greet_morning, greet_day, greet_evening, greet_night, greet_suffix,
       crossfade_on, crossfade_off, mini_mode, discord_on, discord_off):
    return dict(
        flag=flag, no_track=no_track, search=search, add_files=add_files,
        file_filter="Audio Files (*.mp3 *.wav *.ogg *.flac *.aac *.m4a)",
        not_found=not_found, error=error,
        repeat_off=repeat_off, repeat_all=repeat_all, repeat_one=repeat_one,
        speed_label=speed_label, dark_themes=dark_themes, light_themes=light_themes,
        all_tracks=all_tracks, favorites=favorites, eula_btn=eula_btn,
        drop_hint=drop_hint, font_btn=font_btn,
        username_placeholder=username_placeholder, username_save=username_save,
        greet_morning=greet_morning, greet_day=greet_day,
        greet_evening=greet_evening, greet_night=greet_night, greet_suffix=greet_suffix,
        license_title="Mozilla Public License 2.0 — ReturnalAudio",
        window_title="ReturnalAudio",
        crossfade_on=crossfade_on, crossfade_off=crossfade_off,
        mini_mode=mini_mode, discord_on=discord_on, discord_off=discord_off,
    )

LANGUAGES = {
    "RU": _L("🇷🇺","Нет трека","Поиск...","Выбрать файлы",
              "⚠ Файл не найден","⚠ Ошибка: ","Без повтора","Повтор: все","Повтор: один",
              "Скорость","Темные темы","Светлые темы","Все","Избранное","Лицензия",
              "Перетащите аудио сюда","Шрифт","Ваше имя (макс. 12)","Сохранить",
              "Доброе утро","Добрый день","Добрый вечер","Доброй ночи","!",
              "Кроссфейд: вкл","Кроссфейд: выкл","Мини","Discord: вкл","Discord: выкл"),
    "EN": _L("🇬🇧","No track","Search...","Select files",
              "⚠ File not found","⚠ Error: ","No repeat","Repeat all","Repeat one",
              "Speed","Dark themes","Light themes","All","Favorites","License",
              "Drop audio files here","Font","Your name (max 12)","Save",
              "Good morning","Good afternoon","Good evening","Good night","!",
              "Crossfade: on","Crossfade: off","Mini","Discord: on","Discord: off"),
    "DE": _L("🇩🇪","Kein Track","Suche...","Dateien auswählen",
              "⚠ Nicht gefunden","⚠ Fehler: ","Kein Repeat","Alle wdh.","Einen wdh.",
              "Tempo","Dunkle","Helle","Alle","Favoriten","Lizenz",
              "Audio hier ablegen","Schrift","Ihr Name (max. 12)","Speichern",
              "Guten Morgen","Guten Tag","Guten Abend","Gute Nacht","!",
              "Crossfade: ein","Crossfade: aus","Mini","Discord: ein","Discord: aus"),
    "UA": _L("🇺🇦","Немає треку","Пошук...","Вибрати файли",
              "⚠ Файл не знайдено","⚠ Помилка: ","Без повтору","Повтор: всі","Повтор: один",
              "Швидкість","Темні теми","Світлі теми","Всі","Улюблені","Ліцензія",
              "Перетягніть аудіо сюди","Шрифт","Ваше ім'я (макс. 12)","Зберегти",
              "Доброго ранку","Добрий день","Добрий вечір","На добраніч","!",
              "Кросфейд: увімк","Кросфейд: вимк","Міні","Discord: увімк","Discord: вимк"),
    "JP": _L("🇯🇵","なし","検索...","ファイル選択",
              "⚠ 見つかりません","⚠ エラー: ","リピートなし","全曲","1曲",
              "速度","ダーク","ライト","全て","お気に入り","ライセンス",
              "ここにドロップ","フォント","お名前（12文字）","保存",
              "おはよう","こんにちは","こんばんは","おやすみ","、",
              "CF: オン","CF: オフ","ミニ","Discord: オン","Discord: オフ"),
    "FR": _L("🇫🇷","Pas de piste","Rechercher...","Sélectionner",
              "⚠ Introuvable","⚠ Erreur : ","Sans répétition","Tout","Un",
              "Vitesse","Sombres","Clairs","Tous","Favoris","Licence",
              "Déposez ici","Police","Votre nom (max 12)","Enregistrer",
              "Bonjour","Bon après-midi","Bonsoir","Bonne nuit","!",
              "Fondu: act","Fondu: dés","Mini","Discord: act","Discord: dés"),
    "IT": _L("🇮🇹","Nessuna traccia","Cerca...","Seleziona file",
              "⚠ Non trovato","⚠ Errore: ","Nessuna ripetizione","Ripeti tutto","Ripeti uno",
              "Velocità","Scuri","Chiari","Tutti","Preferiti","Licenza",
              "Trascina qui l'audio","Carattere","Il tuo nome (max 12)","Salva",
              "Buongiorno","Buon pomeriggio","Buonasera","Buonanotte","!",
              "Crossfade: on","Crossfade: off","Mini","Discord: on","Discord: off"),
    "PT": _L("🇵🇹","Sem faixa","Pesquisar...","Selecionar",
              "⚠ Não encontrado","⚠ Erro: ","Sem repetição","Repetir tudo","Repetir um",
              "Velocidade","Escuros","Claros","Todos","Favoritos","Licença",
              "Solte o áudio aqui","Fonte","Seu nome (máx. 12)","Salvar",
              "Bom dia","Boa tarde","Boa noite","Boa noite","!",
              "Crossfade: lig","Crossfade: desl","Mini","Discord: lig","Discord: desl"),
    "ES": _L("🇪🇸","Sin pista","Buscar...","Seleccionar",
              "⚠ No encontrado","⚠ Error: ","Sin repetición","Repetir todo","Repetir uno",
              "Velocidad","Oscuros","Claros","Todos","Favoritos","Licencia",
              "Suelta el audio aquí","Fuente","Tu nombre (máx 12)","Guardar",
              "Buenos días","Buenas tardes","Buenas noches","Buenas noches","!",
              "Crossfade: act","Crossfade: des","Mini","Discord: act","Discord: des"),
    "PL": _L("🇵🇱","Brak utworu","Szukaj...","Wybierz pliki",
              "⚠ Nie znaleziono","⚠ Błąd: ","Bez powtarzania","Powtórz wszystko","Powtórz jeden",
              "Prędkość","Ciemne","Jasne","Wszystkie","Ulubione","Licencja",
              "Upuść audio tutaj","Czcionka","Twoje imię (max 12)","Zapisz",
              "Dzień dobry","Dzień dobry","Dobry wieczór","Dobranoc","!",
              "Crossfade: wł","Crossfade: wył","Mini","Discord: wł","Discord: wył"),
    "CS": _L("🇨🇿","Žádná skladba","Hledat...","Vybrat soubory",
              "⚠ Nenalezeno","⚠ Chyba: ","Bez opakování","Opakovat vše","Opakovat jednu",
              "Rychlost","Tmavé","Světlé","Všechny","Oblíbené","Licence",
              "Přetáhněte sem","Písmo","Vaše jméno (max 12)","Uložit",
              "Dobré ráno","Dobré odpoledne","Dobrý večer","Dobrou noc","!",
              "Crossfade: zap","Crossfade: vyp","Mini","Discord: zap","Discord: vyp"),
    "SV": _L("🇸🇪","Inget spår","Sök...","Välj filer",
              "⚠ Hittades inte","⚠ Fel: ","Ingen upprepning","Upprepa alla","Upprepa en",
              "Hastighet","Mörka","Ljusa","Alla","Favoriter","Licens",
              "Släpp ljud här","Typsnitt","Ditt namn (max 12)","Spara",
              "God morgon","God eftermiddag","God kväll","God natt","!",
              "Crossfade: på","Crossfade: av","Mini","Discord: på","Discord: av"),
}
LANG_ORDER = ["RU","EN","DE","UA","JP","FR","IT","PT","ES","PL","CS","SV"]

SETTINGS_TEXTS = {
    "EN": {
        "title": "Settings",
        "move_top_check": "Move top panel buttons into settings",
        "move_top_info": "When enabled, the top panel buttons live here instead of the toolbar.",
        "buttons_on_top": "Buttons are currently shown in the top panel.",
        "shortcuts_title": "Keyboard Shortcuts",
        "sec_appearance": "Appearance",
        "sec_playback": "Playback",
        "sec_integration": "Integration",
        "sec_profile": "Profile And Panel",
        "sc_play_pause": "Play / Pause",
        "sc_next": "Next Track",
        "sc_prev": "Previous Track",
        "sc_vol_up": "Volume Up",
        "sc_vol_down": "Volume Down",
        "sc_delete": "Delete Selected Track",
        "sc_add": "Add Files",
        "sc_shuffle": "Shuffle",
        "sc_repeat": "Repeat",
        "sc_theme": "Cycle Theme",
        "sc_language": "Cycle Language",
        "sc_mini": "Mini Player",
        "sc_hide": "Hide Toolbar",
        "sc_discord": "Discord Rich Presence",
        "sc_eq": "Equalizer",
        "sc_lyrics": "Show / Hide Lyrics",
        "sc_grid": "Grid / List View",
        "sc_glow": "Glow",
        "sc_fullscreen": "Fullscreen Mode",
        "dynamic_bg_check": "Dynamic animated background",
    },
    "RU": {
        "title": "Настройки",
        "move_top_check": "Переместить кнопки верхней панели в настройки",
        "move_top_info": "Когда включено, кнопки верхней панели находятся здесь, а не в тулбаре.",
        "buttons_on_top": "Кнопки сейчас отображаются на верхней панели.",
        "shortcuts_title": "Горячие клавиши",
        "sec_appearance": "Внешний вид",
        "sec_playback": "Воспроизведение",
        "sec_integration": "Интеграции",
        "sec_profile": "Профиль и панель",
        "sc_play_pause": "Пуск / Пауза",
        "sc_next": "Следующий трек",
        "sc_prev": "Предыдущий трек",
        "sc_vol_up": "Громче",
        "sc_vol_down": "Тише",
        "sc_delete": "Удалить выбранный трек",
        "sc_add": "Добавить файлы",
        "sc_shuffle": "Перемешивание",
        "sc_repeat": "Повтор",
        "sc_theme": "Сменить тему",
        "sc_language": "Сменить язык",
        "sc_mini": "Мини-плеер",
        "sc_hide": "Скрыть панель",
        "sc_discord": "Discord Rich Presence",
        "sc_eq": "Эквалайзер",
        "sc_lyrics": "Показать / скрыть текст",
        "sc_grid": "Сетка / список",
        "sc_glow": "Свечение",
        "sc_fullscreen": "Полноэкранный режим",
        "dynamic_bg_check": "Динамический анимированный фон",
    },
    "UA": {
        "title": "Налаштування",
        "move_top_check": "Перемістити кнопки верхньої панелі в налаштування",
        "move_top_info": "Коли ввімкнено, кнопки верхньої панелі знаходяться тут, а не в тулбарі.",
        "buttons_on_top": "Кнопки зараз відображаються на верхній панелі.",
        "shortcuts_title": "Гарячі клавіші",
        "sec_appearance": "Зовнішній вигляд",
        "sec_playback": "Відтворення",
        "sec_integration": "Інтеграції",
        "sec_profile": "Профіль і панель",
        "sc_play_pause": "Пуск / Пауза",
        "sc_next": "Наступний трек",
        "sc_prev": "Попередній трек",
        "sc_vol_up": "Гучніше",
        "sc_vol_down": "Тихіше",
        "sc_delete": "Видалити вибраний трек",
        "sc_add": "Додати файли",
        "sc_shuffle": "Перемішування",
        "sc_repeat": "Повтор",
        "sc_theme": "Змінити тему",
        "sc_language": "Змінити мову",
        "sc_mini": "Міні-плеєр",
        "sc_hide": "Сховати панель",
        "sc_discord": "Discord Rich Presence",
        "sc_eq": "Еквалайзер",
        "sc_lyrics": "Показати / сховати текст",
        "sc_grid": "Сітка / список",
        "sc_glow": "Світіння",
    },
    "DE": {
        "title": "Einstellungen",
        "move_top_check": "Schaltflächen der oberen Leiste in die Einstellungen verschieben",
        "move_top_info": "Wenn aktiviert, befinden sich die Schaltflächen der oberen Leiste hier statt in der Werkzeugleiste.",
        "buttons_on_top": "Die Schaltflächen werden derzeit in der oberen Leiste angezeigt.",
        "shortcuts_title": "Tastenkürzel",
        "sec_appearance": "Aussehen",
        "sec_playback": "Wiedergabe",
        "sec_integration": "Integrationen",
        "sec_profile": "Profil und Leiste",
        "sc_play_pause": "Wiedergabe / Pause",
        "sc_next": "Nächster Titel",
        "sc_prev": "Vorheriger Titel",
        "sc_vol_up": "Lauter",
        "sc_vol_down": "Leiser",
        "sc_delete": "Ausgewählten Titel löschen",
        "sc_add": "Dateien hinzufügen",
        "sc_shuffle": "Zufallswiedergabe",
        "sc_repeat": "Wiederholen",
        "sc_theme": "Thema wechseln",
        "sc_language": "Sprache wechseln",
        "sc_mini": "Mini-Player",
        "sc_hide": "Leiste ausblenden",
        "sc_discord": "Discord Rich Presence",
        "sc_eq": "Equalizer",
        "sc_lyrics": "Text anzeigen / ausblenden",
        "sc_grid": "Raster / Liste",
        "sc_glow": "Leuchten",
    },
    "FR": {
        "title": "Paramètres",
        "move_top_check": "Déplacer les boutons de la barre supérieure dans les paramètres",
        "move_top_info": "Lorsque cette option est activée, les boutons de la barre supérieure se trouvent ici au lieu de la barre d'outils.",
        "buttons_on_top": "Les boutons sont actuellement affichés dans la barre supérieure.",
        "shortcuts_title": "Raccourcis clavier",
        "sec_appearance": "Apparence",
        "sec_playback": "Lecture",
        "sec_integration": "Intégrations",
        "sec_profile": "Profil et barre",
        "sc_play_pause": "Lecture / Pause",
        "sc_next": "Piste suivante",
        "sc_prev": "Piste précédente",
        "sc_vol_up": "Augmenter le volume",
        "sc_vol_down": "Baisser le volume",
        "sc_delete": "Supprimer la piste sélectionnée",
        "sc_add": "Ajouter des fichiers",
        "sc_shuffle": "Lecture aléatoire",
        "sc_repeat": "Répéter",
        "sc_theme": "Changer de thème",
        "sc_language": "Changer de langue",
        "sc_mini": "Mini-lecteur",
        "sc_hide": "Masquer la barre",
        "sc_discord": "Discord Rich Presence",
        "sc_eq": "Égaliseur",
        "sc_lyrics": "Afficher / masquer les paroles",
        "sc_grid": "Grille / liste",
        "sc_glow": "Lueur",
    },
    "IT": {
        "title": "Impostazioni",
        "move_top_check": "Sposta i pulsanti della barra superiore nelle impostazioni",
        "move_top_info": "Quando è attivo, i pulsanti della barra superiore si trovano qui invece che nella barra degli strumenti.",
        "buttons_on_top": "I pulsanti sono attualmente mostrati nella barra superiore.",
        "shortcuts_title": "Scorciatoie da tastiera",
        "sec_appearance": "Aspetto",
        "sec_playback": "Riproduzione",
        "sec_integration": "Integrazioni",
        "sec_profile": "Profilo e barra",
        "sc_play_pause": "Riproduci / Pausa",
        "sc_next": "Traccia successiva",
        "sc_prev": "Traccia precedente",
        "sc_vol_up": "Aumenta volume",
        "sc_vol_down": "Riduci volume",
        "sc_delete": "Elimina la traccia selezionata",
        "sc_add": "Aggiungi file",
        "sc_shuffle": "Riproduzione casuale",
        "sc_repeat": "Ripeti",
        "sc_theme": "Cambia tema",
        "sc_language": "Cambia lingua",
        "sc_mini": "Mini player",
        "sc_hide": "Nascondi barra",
        "sc_discord": "Discord Rich Presence",
        "sc_eq": "Equalizzatore",
        "sc_lyrics": "Mostra / nascondi testo",
        "sc_grid": "Griglia / elenco",
        "sc_glow": "Bagliore",
    },
    "PT": {
        "title": "Configurações",
        "move_top_check": "Mover os botões da barra superior para as configurações",
        "move_top_info": "Quando ativado, os botões da barra superior ficam aqui em vez da barra de ferramentas.",
        "buttons_on_top": "Os botões estão sendo exibidos na barra superior.",
        "shortcuts_title": "Atalhos do teclado",
        "sec_appearance": "Aparência",
        "sec_playback": "Reprodução",
        "sec_integration": "Integrações",
        "sec_profile": "Perfil e barra",
        "sc_play_pause": "Reproduzir / Pausar",
        "sc_next": "Próxima faixa",
        "sc_prev": "Faixa anterior",
        "sc_vol_up": "Aumentar volume",
        "sc_vol_down": "Diminuir volume",
        "sc_delete": "Excluir faixa selecionada",
        "sc_add": "Adicionar arquivos",
        "sc_shuffle": "Aleatório",
        "sc_repeat": "Repetir",
        "sc_theme": "Alternar tema",
        "sc_language": "Alternar idioma",
        "sc_mini": "Mini player",
        "sc_hide": "Ocultar barra",
        "sc_discord": "Discord Rich Presence",
        "sc_eq": "Equalizador",
        "sc_lyrics": "Mostrar / ocultar letra",
        "sc_grid": "Grade / lista",
        "sc_glow": "Brilho",
    },
    "ES": {
        "title": "Configuración",
        "move_top_check": "Mover los botones de la barra superior a la configuración",
        "move_top_info": "Cuando está activado, los botones de la barra superior aparecen aquí en lugar de la barra de herramientas.",
        "buttons_on_top": "Los botones se muestran actualmente en la barra superior.",
        "shortcuts_title": "Atajos de teclado",
        "sec_appearance": "Apariencia",
        "sec_playback": "Reproducción",
        "sec_integration": "Integraciones",
        "sec_profile": "Perfil y barra",
        "sc_play_pause": "Reproducir / Pausa",
        "sc_next": "Siguiente pista",
        "sc_prev": "Pista anterior",
        "sc_vol_up": "Subir volumen",
        "sc_vol_down": "Bajar volumen",
        "sc_delete": "Eliminar pista seleccionada",
        "sc_add": "Agregar archivos",
        "sc_shuffle": "Aleatorio",
        "sc_repeat": "Repetir",
        "sc_theme": "Cambiar tema",
        "sc_language": "Cambiar idioma",
        "sc_mini": "Mini reproductor",
        "sc_hide": "Ocultar barra",
        "sc_discord": "Discord Rich Presence",
        "sc_eq": "Ecualizador",
        "sc_lyrics": "Mostrar / ocultar letra",
        "sc_grid": "Cuadrícula / lista",
        "sc_glow": "Brillo",
    },
    "PL": {
        "title": "Ustawienia",
        "move_top_check": "Przenieś przyciski górnego paska do ustawień",
        "move_top_info": "Po włączeniu przyciski górnego paska znajdują się tutaj zamiast na pasku narzędzi.",
        "buttons_on_top": "Przyciski są obecnie wyświetlane na górnym pasku.",
        "shortcuts_title": "Skróty klawiaturowe",
        "sec_appearance": "Wygląd",
        "sec_playback": "Odtwarzanie",
        "sec_integration": "Integracje",
        "sec_profile": "Profil i pasek",
        "sc_play_pause": "Odtwarzaj / Pauza",
        "sc_next": "Następny utwór",
        "sc_prev": "Poprzedni utwór",
        "sc_vol_up": "Głośniej",
        "sc_vol_down": "Ciszej",
        "sc_delete": "Usuń wybrany utwór",
        "sc_add": "Dodaj pliki",
        "sc_shuffle": "Losowo",
        "sc_repeat": "Powtarzanie",
        "sc_theme": "Zmień motyw",
        "sc_language": "Zmień język",
        "sc_mini": "Mini odtwarzacz",
        "sc_hide": "Ukryj pasek",
        "sc_discord": "Discord Rich Presence",
        "sc_eq": "Korektor",
        "sc_lyrics": "Pokaż / ukryj tekst",
        "sc_grid": "Siatka / lista",
        "sc_glow": "Poświata",
    },
    "CS": {
        "title": "Nastavení",
        "move_top_check": "Přesunout tlačítka horní lišty do nastavení",
        "move_top_info": "Pokud je zapnuto, tlačítka horní lišty se zobrazují zde místo na panelu nástrojů.",
        "buttons_on_top": "Tlačítka se momentálně zobrazují v horní liště.",
        "shortcuts_title": "Klávesové zkratky",
        "sec_appearance": "Vzhled",
        "sec_playback": "Přehrávání",
        "sec_integration": "Integrace",
        "sec_profile": "Profil a lišta",
        "sc_play_pause": "Přehrát / Pauza",
        "sc_next": "Další skladba",
        "sc_prev": "Předchozí skladba",
        "sc_vol_up": "Zvýšit hlasitost",
        "sc_vol_down": "Snížit hlasitost",
        "sc_delete": "Odstranit vybranou skladbu",
        "sc_add": "Přidat soubory",
        "sc_shuffle": "Náhodně",
        "sc_repeat": "Opakování",
        "sc_theme": "Změnit motiv",
        "sc_language": "Změnit jazyk",
        "sc_mini": "Mini přehrávač",
        "sc_hide": "Skrýt lištu",
        "sc_discord": "Discord Rich Presence",
        "sc_eq": "Ekvalizér",
        "sc_lyrics": "Zobrazit / skrýt text",
        "sc_grid": "Mřížka / seznam",
        "sc_glow": "Záře",
    },
    "SV": {
        "title": "Inställningar",
        "move_top_check": "Flytta knapparna från toppanelen till inställningarna",
        "move_top_info": "När detta är aktiverat visas toppanelens knappar här istället för i verktygsfältet.",
        "buttons_on_top": "Knapparna visas för närvarande i toppanelen.",
        "shortcuts_title": "Kortkommandon",
        "sec_appearance": "Utseende",
        "sec_playback": "Uppspelning",
        "sec_integration": "Integrationer",
        "sec_profile": "Profil och panel",
        "sc_play_pause": "Spela / Paus",
        "sc_next": "Nästa spår",
        "sc_prev": "Föregående spår",
        "sc_vol_up": "Höj volymen",
        "sc_vol_down": "Sänk volymen",
        "sc_delete": "Ta bort valt spår",
        "sc_add": "Lägg till filer",
        "sc_shuffle": "Blanda",
        "sc_repeat": "Upprepa",
        "sc_theme": "Byt tema",
        "sc_language": "Byt språk",
        "sc_mini": "Minispelare",
        "sc_hide": "Dölj panel",
        "sc_discord": "Discord Rich Presence",
        "sc_eq": "Equalizer",
        "sc_lyrics": "Visa / dölj text",
        "sc_grid": "Rutnät / lista",
        "sc_glow": "Glöd",
    },
    "JP": {
        "title": "設定",
        "move_top_check": "上部パネルのボタンを設定内に移動",
        "move_top_info": "有効にすると、上部パネルのボタンはツールバーではなくここに表示されます。",
        "buttons_on_top": "ボタンは現在上部パネルに表示されています。",
        "shortcuts_title": "キーボードショートカット",
        "sec_appearance": "外観",
        "sec_playback": "再生",
        "sec_integration": "連携",
        "sec_profile": "プロフィールとパネル",
        "sc_play_pause": "再生 / 一時停止",
        "sc_next": "次のトラック",
        "sc_prev": "前のトラック",
        "sc_vol_up": "音量を上げる",
        "sc_vol_down": "音量を下げる",
        "sc_delete": "選択したトラックを削除",
        "sc_add": "ファイルを追加",
        "sc_shuffle": "シャッフル",
        "sc_repeat": "リピート",
        "sc_theme": "テーマを切り替え",
        "sc_language": "言語を切り替え",
        "sc_mini": "ミニプレーヤー",
        "sc_hide": "パネルを隠す",
        "sc_discord": "Discord Rich Presence",
        "sc_eq": "イコライザー",
        "sc_lyrics": "歌詞を表示 / 非表示",
        "sc_grid": "グリッド / リスト",
        "sc_glow": "グロー",
    },
}

def get_settings_texts(lang_key):
    texts = SETTINGS_TEXTS["EN"].copy()
    texts.update(SETTINGS_TEXTS.get(lang_key, {}))
    return texts


# ═══════════════════════════════════════════════════════════
#  COLOUR / THEME HELPERS
# ═══════════════════════════════════════════════════════════
def blend_color(s, e, p):
    a, b = QColor(s), QColor(e)
    return QColor(int(a.red()+(b.red()-a.red())*p),
                  int(a.green()+(b.green()-a.green())*p),
                  int(a.blue()+(b.blue()-a.blue())*p)).name()

def blend_theme(s, e, p):
    r = dict(e)
    for k in THEME_COLOR_KEYS: r[k] = blend_color(s[k], e[k], p)
    return r

def make_vgrad(rect, c1, c2):
    g = QLinearGradient(rect.left(), rect.top(), rect.left(), rect.bottom())
    g.setColorAt(0, QColor(c1)); g.setColorAt(1, QColor(c2)); return g

def get_greeting_key(_):
    h = datetime.datetime.now().hour
    if   5 <= h < 12: return "greet_morning"
    elif 12 <= h < 18: return "greet_day"
    elif 18 <= h < 23: return "greet_evening"
    return "greet_night"

def apply_tooltip_style(app, T):
    app.setStyleSheet(app.styleSheet() + f"""
        QToolTip {{background:{T['surface']};color:{T['text']};border:1.5px solid {T['accent']};
                   border-radius:6px;padding:5px 10px;font-size:12px;font-weight:bold;}}""")

def apply_smooth_scrollbar(lw, T):
    if hasattr(lw, "setVerticalScrollMode"):
        try: lw.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        except Exception: pass
    if hasattr(lw, "setHorizontalScrollBarPolicy"):
        try: lw.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        except Exception: pass
    try:
        QScroller.grabGesture(lw.viewport(), QScroller.LeftMouseButtonGesture)
    except Exception: pass
    lw.verticalScrollBar().setStyleSheet(f"""
        QScrollBar:vertical{{background:{T['surface']};width:6px;border-radius:3px;margin:2px;}}
        QScrollBar::handle:vertical{{background:{T['accent']};border-radius:3px;min-height:24px;}}
        QScrollBar::handle:vertical:hover{{background:{T['accent2']};}}
        QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0;}}
        QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{{background:none;}}""")


# ═══════════════════════════════════════════════════════════
#  ANIMATED POPUP MIXIN
# ═══════════════════════════════════════════════════════════
class AnimatedPopupMixin:
    def _init_popup_anim(self):
        self.setWindowOpacity(0.0)
        self._panim = QPropertyAnimation(self, b"windowOpacity")
        self._panim.setDuration(180); self._panim.setEasingCurve(QEasingCurve.OutCubic)
    def showEvent(self, e):
        super().showEvent(e)
        if hasattr(self, '_panim'):
            self._panim.stop(); self._panim.setStartValue(0.0)
            self._panim.setEndValue(1.0); self._panim.start()


# ═══════════════════════════════════════════════════════════
#  MARQUEE LABEL
# ═══════════════════════════════════════════════════════════
class MarqueeLabel(QWidget):
    def __init__(self, text="", color="#FFF", font_size=13, bold=True, parent=None):
        super().__init__(parent)
        self._text=text; self._color=color; self._fs=font_size; self._bold=bold
        self._offset=0.0; self._scrolling=False; self._pause=0; self._dir=1; self._speed=1.5
        self._pause_ticks=40
        self.setMinimumHeight(font_size+14)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._t=QTimer(self); self._t.setInterval(20); self._t.timeout.connect(self._tick)

    def set_text(self, t):
        if self._text==t: return
        self._text=t; self._offset=0; self._pause=0; self._dir=1
        self._t.stop(); self.update(); self._check()

    def set_color(self, c): self._color=c; self.update()
    def set_font_params(self, color, size, bold):
        self._color=color; self._fs=size; self._bold=bold; self.update(); self._check()

    def _font(self):
        return QFont(get_current_font(), self._fs, QFont.Bold if self._bold else QFont.Normal)

    def _check(self):
        fm=QFontMetrics(self._font()); tw=fm.horizontalAdvance(self._text); av=self.width()-8
        if tw>av and av>0:
            self._scrolling=True; self._offset=0; self._pause=self._pause_ticks; self._t.start()
        else:
            self._scrolling=False; self._offset=0; self._t.stop(); self.update()

    def resizeEvent(self, e): super().resizeEvent(e); self._check()

    def _tick(self):
        if self._pause>0: self._pause-=1; return
        fm=QFontMetrics(self._font()); tw=fm.horizontalAdvance(self._text); av=self.width()-8
        if self._dir==1:
            self._offset=min(self._offset+self._speed, tw-av)
            if self._offset>=tw-av: self._pause=self._pause_ticks; self._dir=-1
        else:
            self._offset=max(self._offset-self._speed, 0)
            if self._offset<=0: self._pause=self._pause_ticks; self._dir=1
        self.update()

    def paintEvent(self, e):
        p=QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        fn=self._font(); p.setFont(fn); p.setPen(QColor(self._color))
        fm=QFontMetrics(fn); av=self.width()-8; ty=int(self.height()/2+fm.ascent()/2-2)
        if self._scrolling:
            p.setClipRect(4,0,av,self.height())
            p.drawText(QPointF(4-self._offset,ty), self._text)
        else:
            p.drawText(QRect(4,0,av,self.height()), Qt.AlignVCenter|Qt.AlignLeft, self._text)


# ═══════════════════════════════════════════════════════════
#  LYRICS PANEL
# ═══════════════════════════════════════════════════════════

class LyricsPanel(QFrame):
    """Inline lyrics panel with synced LRC support and manual edit."""
    def __init__(self, theme, parent=None):
        super().__init__(parent)
        self.theme = theme
        self._lrc = []          # [(ms, text), ...]
        self._cur_line = -1
        self._manual_mode = False

        lo = QVBoxLayout(self); lo.setContentsMargins(10,8,10,8); lo.setSpacing(4)

        # header row
        hr = QHBoxLayout()
        self._lbl = QLabel("♪ Lyrics"); hr.addWidget(self._lbl)
        hr.addStretch()
        self._status = QLabel(""); hr.addWidget(self._status)
        # Manual edit toggle
        self._edit_btn = QPushButton("✏"); self._edit_btn.setFixedSize(24,22)
        self._edit_btn.setCheckable(True)
        self._edit_btn.setToolTip("Edit lyrics manually")
        self._edit_btn.clicked.connect(self._toggle_edit)
        hr.addWidget(self._edit_btn)
        lo.addLayout(hr)

        self._text = QTextEdit(); self._text.setReadOnly(True)
        self._text.setWordWrapMode(1)
        lo.addWidget(self._text, 1)

        # Source selector strip
        self._src_row = QHBoxLayout(); self._src_row.setSpacing(4)
        self._src_lbl = QLabel("Source:"); self._src_row.addWidget(self._src_lbl)
        self._src_btns = {}
        for src, tip in [
            ("local_lrc", "Local .lrc file (synced)"),
            ("LRCLib",    "LRCLib — synced/plain"),
            ("Genius",    "Genius lyrics"),
            ("AZLyrics",  "AZLyrics"),
            ("lyrics.ovh","lyrics.ovh"),
            ("manual",    "Manually entered"),
        ]:
            b = QPushButton(src.replace('local_lrc','.lrc'))
            b.setFixedHeight(18); b.setCheckable(True)
            b.setToolTip(tip); b.setVisible(False)
            self._src_row.addWidget(b)
            self._src_btns[src] = b
        self._src_row.addStretch()
        lo.addLayout(self._src_row)

        self._sources = {}   # key → raw lyrics text
        self._cur_src = None

        self._apply_style()

    def _apply_style(self):
        T=self.theme; ff=get_current_font()
        self.setStyleSheet(f"QFrame{{background:{T['surface']};border-radius:10px;border:1px solid {T['track_bg']};}}")
        self._lbl.setStyleSheet(f"color:{T['accent']};background:transparent;border:none;font-weight:bold;font-size:12px;font-family:{ff};")
        self._status.setStyleSheet(f"color:{T['text2']};background:transparent;border:none;font-size:9px;font-family:{ff};")
        self._src_lbl.setStyleSheet(f"color:{T['text2']};background:transparent;border:none;font-size:8px;font-family:{ff};")
        eb_style = (f"QPushButton{{background:{T['track_bg']};color:{T['text2']};border:1px solid {T['track_bg']};"
                    f"border-radius:3px;font-size:11px;padding:0 3px;}}"
                    f"QPushButton:checked{{background:{T['accent']};color:{T['bg']};}}"
                    f"QPushButton:hover{{background:{T['hover']};}}")
        self._edit_btn.setStyleSheet(eb_style)
        for b in self._src_btns.values():
            b.setStyleSheet(f"QPushButton{{background:{T['track_bg']};color:{T['text2']};border:none;"
                            f"border-radius:3px;font-size:8px;padding:0 4px;}}"
                            f"QPushButton:checked{{background:{T['accent']};color:{T['bg']};}}"
                            f"QPushButton:hover{{background:{T['hover']};}}")
        self._text.setStyleSheet(f"""
            QTextEdit{{background:transparent;color:{T['text']};border:none;font-family:{ff};font-size:11px;}}
            QScrollBar:vertical{{background:{T['surface']};width:5px;border-radius:2px;}}
            QScrollBar::handle:vertical{{background:{T['accent']};border-radius:2px;min-height:16px;}}
            QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0;}}""")

    def set_theme(self, t):
        self.theme=t; self._apply_style()

    def _toggle_edit(self, checked):
        self._manual_mode = checked
        self._text.setReadOnly(not checked)
        if checked:
            self._status.setText("Editing…")
            self._lrc = []   # disable sync while editing
        else:
            # save manual text as a source
            raw = self._text.toPlainText()
            if raw.strip():
                self._sources['manual'] = raw
                self._show_src_btn('manual', True)
            self._status.setText("Manual")

    def _show_src_btn(self, key, visible):
        if key in self._src_btns:
            self._src_btns[key].setVisible(visible)

    def _switch_source(self, key):
        if key not in self._sources: return
        self._cur_src = key
        for k, b in self._src_btns.items():
            b.setChecked(k == key)
        self.show_lyrics((key, self._sources[key]))

    # ── public API ──
    def show_loading(self, label=""):
        self._lrc=[]; self._cur_line=-1; self._sources={}; self._cur_src=None
        for b in self._src_btns.values(): b.setVisible(False); b.setChecked(False)
        T=self.theme
        self._lbl.setText(f"♪ {label}" if label else "♪ Lyrics")
        self._status.setText("searching…")
        self._text.setHtml(f"<center style='color:{T['text2']};margin-top:40px;'>🔍 Searching…</center>")

    def show_lyrics(self, result):
        """result = (source_key: str, raw_text: str)"""
        if isinstance(result, str):
            result = ('unknown', result)
        src_key, raw = result

        self._sources[src_key] = raw
        self._cur_src = src_key

        # update source buttons
        for k, b in self._src_btns.items():
            b.setVisible(k in self._sources)
            b.setChecked(k == src_key)
            try: b.clicked.disconnect()
            except Exception: pass
            k2 = k  # closure capture
            b.clicked.connect(lambda _, kk=k2: self._switch_source(kk))

        # parse
        self._lrc = []
        self._cur_line = -1
        lines = raw.strip().splitlines()
        parsed = []
        for ln in lines:
            m = _re.match(r'\[(\d+):(\d+)[\.\:](\d+)\](.*)', ln)
            if m:
                mn, sc, cs, txt = m.groups()
                ms = (int(mn)*60+int(sc))*1000+int(cs)*10
                parsed.append((ms, txt.strip()))

        source_label = {
            'local_lrc': 'Local .lrc', 'LRCLib': 'LRCLib',
            'Genius': 'Genius', 'AZLyrics': 'AZLyrics',
            'lyrics.ovh': 'lyrics.ovh', 'manual': 'Manual', 'unknown': 'Found'
        }.get(src_key, src_key)

        if parsed:
            self._lrc = parsed
            self._status.setText(f"Synced · {source_label}")
            self._render_lrc()
        else:
            self._status.setText(f"Plain · {source_label}")
            T = self.theme
            html = (f"<pre style='white-space:pre-wrap;color:{T['text']};"
                    f"font-family:{get_current_font()};font-size:11px;line-height:1.8;'>{raw}</pre>")
            self._text.setHtml(html)

    def show_error(self):
        self._lrc=[]; T=self.theme
        self._text.setHtml(
            f"<center style='color:{T['text2']};margin-top:30px;'>"
            f"😔 Lyrics not found<br><br>"
            f"<small style='color:{T['text2']};'>"
            f"Try adding a <b>.lrc</b> file with the same name as your audio,<br>"
            f"or press ✏ to type lyrics manually."
            f"</small></center>")
        self._status.setText("")

    def clear(self):
        self._lrc=[]; self._cur_line=-1; self._sources={}
        for b in self._src_btns.values(): b.setVisible(False)
        T=self.theme
        self._lbl.setText("♪ Lyrics"); self._status.setText("")
        self._text.setHtml(
            f"<center style='color:{T['text2']};margin-top:40px;'>"
            f"♪ Play a track to see lyrics<br>"
            f"<small>Tip: place a .lrc file next to your audio for synced lyrics</small></center>")

    def sync(self, pos_ms: int):
        if not self._lrc or self._manual_mode: return
        new=-1
        for i,(ms,_) in enumerate(self._lrc):
            if pos_ms>=ms: new=i
            else: break
        if new!=self._cur_line:
            self._cur_line=new; self._render_lrc()

    def _render_lrc(self):
        T=self.theme
        parts=[]
        for i,(ms,txt) in enumerate(self._lrc):
            if i==self._cur_line:
                parts.append(f"<p style='color:{T['accent']};font-weight:bold;font-size:13px;margin:3px 0;'>▶ {txt}</p>")
            else:
                op="0.45" if i<self._cur_line else "0.75"
                parts.append(f"<p style='color:{T['text']};opacity:{op};font-size:11px;margin:2px 0;'>{txt}</p>")
        self._text.setHtml(''.join(parts))
        if self._cur_line>0:
            sb=self._text.verticalScrollBar()
            total=max(1,len(self._lrc))
            sb.setValue(int(sb.maximum()*self._cur_line/total))


# ═══════════════════════════════════════════════════════════
#  COVER WIDGET  (rounded, gradient fallback)
# ═══════════════════════════════════════════════════════════
class CoverWidget(QWidget):
    def __init__(self, pix=None, size=120, theme=None, parent=None):
        super().__init__(parent); self._pix=pix; self._sz=size; self._theme=theme or {}
        self.setFixedSize(size,size)
    def set_cover(self, pix): self._pix=pix; self.update()
    def set_theme(self, t):   self._theme=t; self.update()
    def paintEvent(self, e):
        p=QPainter(self); p.setRenderHint(QPainter.Antialiasing); p.setRenderHint(QPainter.SmoothPixmapTransform)
        T=self._theme; W,H=self.width(),self.height()
        path=QPainterPath(); path.addRoundedRect(QRectF(0,0,W,H),10,10); p.setClipPath(path)
        if self._pix and not self._pix.isNull():
            sc=self._pix.scaled(W,H,Qt.KeepAspectRatioByExpanding,Qt.SmoothTransformation)
            p.drawPixmap((W-sc.width())//2,(H-sc.height())//2,sc)
        else:
            g=QRadialGradient(W/2,H/2,W/2)
            c1=QColor(T.get('accent2','#1ed760')); c1.setAlpha(200)
            c2=QColor(T.get('accent','#1DB954')); c2.setAlpha(160)
            g.setColorAt(0,c1); g.setColorAt(1,c2)
            p.setBrush(QBrush(g)); p.setPen(Qt.NoPen); p.drawRect(0,0,W,H)
            p.setPen(QPen(QColor(255,255,255,130),2))
            p.setFont(QFont("Segoe UI Emoji",W//3))
            p.drawText(QRect(0,0,W,H),Qt.AlignCenter,"♪")
        p.setClipping(False)
        bc=QColor(T.get('accent','#1DB954')); bc.setAlpha(60)
        p.setPen(QPen(bc,1.5)); p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(0.75,0.75,W-1.5,H-1.5),10,10)


# ═══════════════════════════════════════════════════════════
#  TRACK CARD  (grid view)
# ═══════════════════════════════════════════════════════════
class TrackCard(QWidget):
    double_clicked = Signal(str)
    like_toggled   = Signal(str)
    single_clicked = Signal(str)   # for selection highlight

    def __init__(self, path, label, pix, is_liked, is_current, theme, parent=None):
        super().__init__(parent)
        self.path=path; self._liked=is_liked; self._cur=is_current; self.theme=theme; self._hov=False
        self._press_scale = 1.0   # animation property
        self._pulse = 0.0         # playing pulse glow
        self.setFixedSize(148,185); self.setCursor(Qt.PointingHandCursor); self.setMouseTracking(True)

        lo=QVBoxLayout(self); lo.setContentsMargins(8,8,8,6); lo.setSpacing(4)
        self.cover=CoverWidget(pix,128,theme,self); lo.addWidget(self.cover,0,Qt.AlignCenter)
        self._lbl=QLabel(); self._lbl.setAlignment(Qt.AlignCenter)
        fm=QFontMetrics(QFont(get_current_font(),8))
        self._lbl.setText(fm.elidedText(label,Qt.ElideRight,132))
        self._lbl.setFixedWidth(132); lo.addWidget(self._lbl)

        # Press/release scale animation
        self._scale_anim = QPropertyAnimation(self, b"press_scale")
        self._scale_anim.setDuration(120); self._scale_anim.setEasingCurve(QEasingCurve.OutQuad)

        # Pulse timer for playing indicator
        self._pulse_timer = QTimer(self); self._pulse_timer.setInterval(40)
        self._pulse_timer.timeout.connect(self._tick_pulse)
        self._pulse_dir = 1

        self._apply()

    @pyqtProperty(float)
    def press_scale(self): return self._press_scale
    @press_scale.setter
    def press_scale(self, v): self._press_scale = v; self.update()

    def _tick_pulse(self):
        self._pulse += 0.05 * self._pulse_dir
        if self._pulse >= 1.0: self._pulse_dir = -1
        elif self._pulse <= 0.0: self._pulse_dir = 1
        self.update()

    def _apply(self):
        T=self.theme; ff=get_current_font()
        bc=T['accent'] if self._cur else T['track_bg']
        bg=QColor(T['surface']); bg=bg.lighter(130) if self._hov else bg
        self.setStyleSheet(f"QWidget{{background:{bg.name()};border-radius:12px;border:2px solid {bc};}}")
        col=T['accent'] if self._cur else T['text']
        fw="bold" if self._cur else "normal"
        self._lbl.setStyleSheet(f"color:{col};background:transparent;border:none;"
                                 f"font-size:8pt;font-weight:{fw};font-family:{ff};")
        if self._cur and not self._pulse_timer.isActive():
            self._pulse_timer.start()
        elif not self._cur and self._pulse_timer.isActive():
            self._pulse_timer.stop(); self._pulse=0.0

    def set_theme(self, t):   self.theme=t; self.cover.set_theme(t); self._apply()
    def set_current(self, v):
        was = self._cur; self._cur=v; self._apply()
        if v and not was:
            # bounce-in animation when becoming current
            self._scale_anim.stop()
            self._scale_anim.setStartValue(0.88); self._scale_anim.setEndValue(1.0); self._scale_anim.start()
    def set_liked(self, v):   self._liked=v

    def enterEvent(self, e):  self._hov=True;  self._apply()
    def leaveEvent(self, e):  self._hov=False; self._apply()

    def mousePressEvent(self, e):
        if e.button()==Qt.LeftButton:
            self._scale_anim.stop()
            self._scale_anim.setStartValue(1.0); self._scale_anim.setEndValue(0.90); self._scale_anim.start()
            self.single_clicked.emit(self.path)
        elif e.button()==Qt.RightButton:
            self.like_toggled.emit(self.path)

    def mouseReleaseEvent(self, e):
        if e.button()==Qt.LeftButton:
            self._scale_anim.stop()
            self._scale_anim.setStartValue(self._press_scale); self._scale_anim.setEndValue(1.0); self._scale_anim.start()

    def mouseDoubleClickEvent(self, e):
        if e.button()==Qt.LeftButton:
            self.double_clicked.emit(self.path)

    def paintEvent(self, e):
        # Draw base widget (background / border via stylesheet)
        super().paintEvent(e)
        # Overlay: scale transform + playing glow ring
        if self._press_scale != 1.0 or (self._cur and self._pulse > 0):
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            T = self.theme; W, H = self.width(), self.height()
            if self._press_scale != 1.0:
                p.translate(W/2, H/2)
                p.scale(self._press_scale, self._press_scale)
                p.translate(-W/2, -H/2)
            if self._cur and self._pulse > 0:
                glow = QColor(T['accent']); glow.setAlpha(int(55 * self._pulse))
                p.setPen(QPen(glow, 3)); p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(QRectF(2, 2, W-4, H-4), 11, 11)
            # p is auto-destroyed at end of scope (no p.end() needed)


# ═══════════════════════════════════════════════════════════
#  GRID VIEW
# ═══════════════════════════════════════════════════════════
class GridView(QScrollArea):
    track_dbl    = Signal(str)
    track_like   = Signal(str)
    track_select = Signal(str)   # single click → show selection without playing

    def __init__(self, theme, parent=None):
        super().__init__(parent); self.theme=theme
        self._cards={}          # path → TrackCard
        self._cover_cache={}    # path → QPixmap
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._cont=QWidget(); self._grid=QGridLayout(self._cont)
        self._grid.setSpacing(10); self._grid.setContentsMargins(10,10,10,10)
        self.setWidget(self._cont); self._apply_style()

    def _apply_style(self):
        T=self.theme
        self.setStyleSheet(f"""
            QScrollArea{{background:transparent;border:none;}}
            QScrollBar:vertical{{background:{T['surface']};width:6px;border-radius:3px;}}
            QScrollBar::handle:vertical{{background:{T['accent']};border-radius:3px;min-height:20px;}}
            QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0;}}""")

    def set_theme(self, t):
        self.theme=t; self._apply_style()
        for c in self._cards.values(): c.set_theme(t)

    def _cols(self):
        W=self.viewport().width()
        return max(2,(W-20)//158)

    def set_glow(self, enabled):
        self._glow_enabled = enabled
        for c in self._cards.values():
            if enabled:
                eff = QGraphicsDropShadowEffect(c)
                eff.setBlurRadius(18)
                eff.setOffset(0,0)
                glow_color = QColor(self.theme['accent'])
                glow_color.setAlpha(130)
                eff.setColor(glow_color)
                c.setGraphicsEffect(eff)
            else:
                c.setGraphicsEffect(None)

    def populate(self, paths, liked_set, cur_path, q=""):
        # Stop pulse timers to avoid dangling references
        for card in self._cards.values():
            if hasattr(card, '_pulse_timer'): card._pulse_timer.stop()

        for i in reversed(range(self._grid.count())):
            w=self._grid.itemAt(i).widget()
            if w: self._grid.removeWidget(w); w.setParent(None)
        self._cards.clear()
        cols=self._cols(); row=0; col=0
        for path in paths:
            label=self._label(path)
            if q and q not in label.lower(): continue
            pix=self._cover_cache.get(path)
            card=TrackCard(path,label,pix,path in liked_set,path==cur_path,self.theme)
            if getattr(self, '_glow_enabled', False):
                eff = QGraphicsDropShadowEffect(card)
                eff.setBlurRadius(18)
                eff.setOffset(0,0)
                glow_color = QColor(self.theme['accent'])
                glow_color.setAlpha(130)
                eff.setColor(glow_color)
                card.setGraphicsEffect(eff)
            card.double_clicked.connect(self.track_dbl.emit)
            card.like_toggled.connect(self.track_like.emit)
            card.single_clicked.connect(self.track_select.emit)
            self._cards[path]=card
            self._grid.addWidget(card,row,col)
            col+=1
            if col>=cols: col=0; row+=1
            if pix is None: self._load_cover(path,card)

    def _load_cover(self, path, card):
        def _run():
            pix=extract_cover(path)
            if pix:
                self._cover_cache[path]=pix
                QTimer.singleShot(0,lambda: self._set_cover(path,pix))
            else:
                ar,ti=self._artist_title(path)
                if ar or ti:
                    fetch_cover_online(ar,ti,lambda p: self._set_cover(path,p) if p else None)
        threading.Thread(target=_run,daemon=True).start()

    def _set_cover(self, path, pix):
        if pix: self._cover_cache[path]=pix
        if path in self._cards: self._cards[path].cover.set_cover(pix)

    def update_current(self, cur):
        for p,c in self._cards.items(): c.set_current(p==cur)
    def update_liked(self, liked_set):
        for p,c in self._cards.items(): c.set_liked(p in liked_set)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        cols=self._cols()
        # re-layout only if column count changed
        items=[self._grid.itemAt(i).widget() for i in range(self._grid.count()) if self._grid.itemAt(i).widget()]
        if items and self._grid.columnCount()!=cols:
            for w in items: self._grid.removeWidget(w)
            r=c=0
            for w in items:
                self._grid.addWidget(w,r,c); c+=1
                if c>=cols: c=0; r+=1

    @staticmethod
    def _artist_title(path):
        if MUTAGEN_OK:
            try:
                af=MutagenFile(path,easy=True)
                if af:
                    return af.get("artist",[""])[0], af.get("title",[""])[0]
            except Exception: pass
        return "",""

    def _label(self, path):
        ar,ti=self._artist_title(path)
        if ar and ti: return f"{ar} — {ti}"
        if ti: return ti
        return os.path.splitext(os.path.basename(path))[0]


# ═══════════════════════════════════════════════════════════
#  USERNAME POPUP
# ═══════════════════════════════════════════════════════════
class UsernamePopup(AnimatedPopupMixin, QFrame):
    saved = Signal(str)
    def __init__(self, theme, current, lang_key, parent=None):
        QFrame.__init__(self,parent,Qt.Popup|Qt.FramelessWindowHint)
        T=theme; L=LANGUAGES.get(lang_key,LANGUAGES["EN"])
        self.theme=theme; self.setAttribute(Qt.WA_TranslucentBackground); self.setFixedSize(220,110)
        lo=QVBoxLayout(self); lo.setContentsMargins(12,12,12,12); lo.setSpacing(8)
        self.inp=QLineEdit(); self.inp.setMaxLength(12); self.inp.setText(current)
        self.inp.setPlaceholderText(L.get("username_placeholder",""))
        self.inp.setStyleSheet(f"background:{T['bg']};color:{T['text']};border:1px solid {T['accent']};border-radius:6px;padding:6px 10px;font-size:13px;")
        lo.addWidget(self.inp)
        btn=QPushButton(L.get("username_save","Save"))
        btn.setStyleSheet(f"background:{T['accent']};color:{T['bg']};border:none;border-radius:6px;padding:6px;font-weight:bold;")
        btn.clicked.connect(self._save); lo.addWidget(btn)
        self.inp.returnPressed.connect(self._save); self.inp.setFocus(); self._init_popup_anim()
    def _save(self): self.saved.emit(self.inp.text().strip()[:12]); self.close()
    def paintEvent(self,e):
        p=QPainter(self); p.setRenderHint(QPainter.Antialiasing); T=self.theme; W,H=self.width(),self.height()
        p.setPen(Qt.NoPen); p.setBrush(QColor(T["surface"])); p.drawRoundedRect(0,0,W,H,10,10)
        p.setPen(QPen(QColor(T["accent"]),1.5)); p.setBrush(Qt.NoBrush); p.drawRoundedRect(1,1,W-2,H-2,10,10)


class UsernameButton(QWidget):
    def __init__(self, theme, lang_key, username, parent=None):
        super().__init__(parent); self.theme=theme; self.lang_key=lang_key; self.username=username
        self._hover=False; self._cbs=[]; self.setFixedSize(46,36); self.setCursor(Qt.PointingHandCursor)
    def set_theme(self,t): self.theme=t; self.update()
    def set_lang(self,lk,u): self.lang_key=lk; self.username=u; self.update()
    def connect(self,cb): self._cbs.append(cb)
    def enterEvent(self,e): self._hover=True;  self.update()
    def leaveEvent(self,e): self._hover=False; self.update()
    def mousePressEvent(self,e):
        if e.button()==Qt.LeftButton: [cb() for cb in self._cbs]
    def paintEvent(self,e):
        p=QPainter(self); p.setRenderHint(QPainter.Antialiasing); T=self.theme; W,H=self.width(),self.height()
        bg=QColor(T["surface"]); bg=bg.lighter(130) if self._hover else bg
        p.setPen(Qt.NoPen); p.setBrush(bg); p.drawRoundedRect(0,0,W,H,8,8)
        p.setPen(QPen(QColor(T["accent"]),1.2)); p.setBrush(Qt.NoBrush); p.drawRoundedRect(1,1,W-2,H-2,8,8)
        cx=W//2; p.setPen(Qt.NoPen); p.setBrush(QColor(T["accent"]))
        p.drawEllipse(QPoint(cx,10),6,6); p.drawRoundedRect(cx-8,19,16,11,3,3)
        if self.username: p.setBrush(QColor(T["accent2"])); p.drawEllipse(QPoint(W-7,6),4,4)


# ═══════════════════════════════════════════════════════════
#  NOW PLAYING
# ═══════════════════════════════════════════════════════════
class NowPlayingWidget(QWidget):
    def __init__(self, theme, parent=None):
        super().__init__(parent); self.theme=theme
        self.setMinimumHeight(38); self.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Fixed)
        lo=QHBoxLayout(self); lo.setContentsMargins(0,0,0,0)
        self._m=MarqueeLabel("",theme["accent"],13,get_font_bold(),self); lo.addWidget(self._m)
    def set_theme(self,t): self.theme=t; self._m.set_font_params(t["accent"],13,get_font_bold())
    def update_font(self):  self._m.set_font_params(self.theme["accent"],13,get_font_bold())
    def set_text(self,t):   self._m.set_text(t)


# ═══════════════════════════════════════════════════════════
#  GREETING OVERLAY
# ═══════════════════════════════════════════════════════════
class GreetingOverlay(QWidget):
    finished=Signal()
    def __init__(self,theme,lang_key,username,parent=None):
        super().__init__(parent); self.theme=theme; self.lang_key=lang_key; self.username=username
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._eff=QGraphicsOpacityEffect(self); self.setGraphicsEffect(self._eff); self._eff.setOpacity(0)
        self._phase=0; self._full=""; self._shown=0; self._tick=0
        self._particles=[]
        self._ct=QTimer(self); self._ct.setInterval(30); self._ct.timeout.connect(self._tick_c)
        self._an=QPropertyAnimation(self._eff,b"opacity"); self._an.setEasingCurve(QEasingCurve.InOutCubic)
        self._an.finished.connect(self._on_done)
    def start(self):
        L=LANGUAGES.get(self.lang_key,LANGUAGES["EN"])
        g=L.get(get_greeting_key(self.lang_key),"Hello"); s=L.get("greet_suffix","!")
        n=self.username.strip(); self._full=f"{g}, {n}{s}" if n else f"{g}{s}"
        self._shown=0; self._tick=0
        self._particles=[(random.uniform(0,1), random.uniform(0,1), random.uniform(15,80), random.uniform(-0.003,0.003), random.uniform(-0.005,-0.001)) for _ in range(18)]
        self.show(); self.raise_()
        self._an.stop(); self._an.setDuration(500); self._an.setStartValue(0.0)
        self._an.setEndValue(1.0); self._phase=0; self._an.start(); self._ct.start()
    def _tick_c(self):
        self._tick+=1
        if self._tick%2==0 and self._shown<len(self._full): self._shown+=1
        new_p=[]
        for px,py,r,dx,dy in self._particles:
            px+=dx; py+=dy
            if py < -0.2: py=1.2
            if px < -0.2: px=1.2
            elif px > 1.2: px=-0.2
            new_p.append((px,py,r,dx,dy))
        self._particles=new_p
        self.update()
    def _on_done(self):
        if self._phase==0: self._phase=1; QTimer.singleShot(2200,self._fade_out)
        elif self._phase==2: self._ct.stop(); self.hide(); self.finished.emit()
    def _fade_out(self):
        self._phase=2; self._an.stop(); self._an.setDuration(800)
        self._an.setStartValue(1.0); self._an.setEndValue(0.0); self._an.start()
    def paintEvent(self,e):
        p=QPainter(self); p.setRenderHint(QPainter.Antialiasing); T=self.theme
        W,H=self.width(),self.height()
        
        fill=QColor(T["bg"]); fill.setAlpha(240); p.setPen(Qt.NoPen); p.setBrush(fill); p.drawRect(self.rect())
        
        p.setPen(Qt.NoPen)
        c1=QColor(T["accent"]); c1.setAlpha(25)
        c2=QColor(T.get("accent2", T["accent"])); c2.setAlpha(15)
        for i, (px, py, r, dx, dy) in enumerate(self._particles):
            p.setBrush(c1 if i % 2 == 0 else c2)
            p.drawEllipse(QPointF(px * W, py * H), r, r)

        bounce_y = math.sin(self._tick * 0.15) * 8 if self._phase < 2 else 0
        p.setFont(QFont("Segoe UI Emoji",46)); p.setPen(QColor(T["accent"]))
        p.drawText(QRect(0, int(H//2-140 - bounce_y), W, 100), Qt.AlignCenter, "🎵")
        
        wt=QFont.Bold if get_font_bold() else QFont.Normal
        p.setFont(QFont(get_current_font(),26,wt)); p.setPen(QColor(T["text"]))
        disp = self._full[:self._shown]
        if self._phase < 2 and (self._tick % 16 < 8): disp += "|"
        p.drawText(QRect(0, H//2-30, W, 70), Qt.AlignCenter, disp)
        
        sub_op = min(255, max(0, int((self._tick - len(self._full)*2)*6))) if self._phase < 2 else 255
        if sub_op > 0:
            c_sub=QColor(T["text2"]); c_sub.setAlpha(sub_op)
            p.setFont(QFont(get_current_font(),11)); p.setPen(c_sub)
            p.drawText(QRect(0, H//2+50, W, 40), Qt.AlignCenter, "ReturnalAudio")


# ═══════════════════════════════════════════════════════════
#  DROP OVERLAY
# ═══════════════════════════════════════════════════════════
class DropOverlay(QWidget):
    def __init__(self,theme,text,parent=None):
        super().__init__(parent); self.theme=theme; self.text=text
        self.setAttribute(Qt.WA_TransparentForMouseEvents); self.hide()
        self._eff=QGraphicsOpacityEffect(self); self.setGraphicsEffect(self._eff); self._eff.setOpacity(0)
        self._an=QPropertyAnimation(self._eff,b"opacity"); self._an.setDuration(180)
    def set_theme(self,t): self.theme=t; self.update()
    def show_overlay(self):
        self.raise_(); self.show(); self._an.stop()
        self._an.setStartValue(self._eff.opacity()); self._an.setEndValue(1.0); self._an.start()
    def hide_overlay(self):
        self._an.stop(); self._an.setStartValue(self._eff.opacity()); self._an.setEndValue(0.0)
        try: self._an.finished.disconnect()
        except: pass
        self._an.finished.connect(self.hide); self._an.start()
    def paintEvent(self,e):
        p=QPainter(self); p.setRenderHint(QPainter.Antialiasing); T=self.theme
        fill=QColor(T["bg"]); fill.setAlpha(200); p.setPen(Qt.NoPen); p.setBrush(fill); p.drawRoundedRect(self.rect(),12,12)
        pn=QPen(QColor(T["accent"]),3,Qt.DashLine); pn.setDashPattern([8,5]); p.setPen(pn); p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(self.rect().adjusted(10,10,-10,-10),10,10)
        cy=self.height()//2-30; p.setFont(QFont("Segoe UI Emoji",36)); p.setPen(QColor(T["accent"]))
        p.drawText(QRect(0,cy-40,self.width(),70),Qt.AlignCenter,"🎵")
        wt=QFont.Bold if get_font_bold() else QFont.Normal
        p.setFont(QFont(get_current_font(),16,wt)); p.setPen(QColor(T["text"]))
        p.drawText(QRect(0,cy+40,self.width(),50),Qt.AlignCenter,self.text)


# ═══════════════════════════════════════════════════════════
#  RUNNING FIGURE SLIDER
# ═══════════════════════════════════════════════════════════
class RunningSlider(QWidget):
    def __init__(self,player,theme,parent=None):
        super().__init__(parent); self.player=player; self.theme=theme
        self.minimum=0; self.maximum=1000; self._value=0; self._disp=0.0; self._seeking=False
        self.setFixedHeight(72); self._phase=0.0; self._phase_step=1.8; self._last_ms=0
        self._at=QTimer(self); self._at.timeout.connect(self._tick); self._at.start(16)
    def set_theme(self,t): self.theme=t; self.update()
    def setRange(self,mn,mx): self.minimum=mn; self.maximum=mx
    def setValue(self,v):
        self._value=v
        if not self._seeking: self._disp=float(v)
        self.update()
    def _tick(self):
        import time
        now=int(time.monotonic()*1000); dt=(now-self._last_ms) if self._last_ms else 16; self._last_ms=now
        play=self.player and self.player.state()==QMediaPlayer.PlayingState
        if play: self._phase+=self._phase_step*dt/1000.0
        if not self._seeking:
            d=self._value-self._disp
            if abs(d)>500: self._disp=float(self._value)
            elif abs(d)>1: self._disp+=d*0.15
            else: self._disp=float(self._value)
        if play: self.update()
    def _rel(self): self._seeking=False
    def wheelEvent(self,e):
        if self.maximum<=self.minimum: return
        step=int((self.maximum-self.minimum)*0.015)
        pos=max(self.minimum,min(self.maximum,int(self._disp)+(-step if e.angleDelta().y()>0 else step)))
        self._disp=float(pos); self._value=pos; self._seeking=True
        self.player.setPosition(pos); QTimer.singleShot(400,self._rel); self.update()
    def mousePressEvent(self,e):
        if e.button()==Qt.LeftButton:
            r=max(0,min(1,e.x()/self.width())); pos=int(self.minimum+r*(self.maximum-self.minimum))
            self._disp=float(pos); self._value=pos; self._seeking=True
            self.player.setPosition(pos); QTimer.singleShot(400,self._rel); self.update()
    def paintEvent(self,event):
        p=QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        T=self.theme; W=self.width(); ty=self.height()-18
        span=self.maximum-self.minimum
        r=(self._disp-self.minimum)/span if span>0 else 0; r=max(0,min(1,r))
        filled=int(14+r*(W-28))
        p.setPen(Qt.NoPen); p.setBrush(QColor(T["track_bg"])); p.drawRoundedRect(14,ty-3,W-28,6,3,3)
        g=QLinearGradient(14,0,filled,0); g.setColorAt(0,QColor(T["accent2"])); g.setColorAt(1,QColor(T["accent"]))
        p.setBrush(g); p.drawRoundedRect(14,ty-3,max(0,filled-14),6,3,3)
        p.setBrush(QColor(T["accent"])); p.drawEllipse(QPoint(14,ty),5,5); p.drawEllipse(QPoint(W-14,ty),5,5)
        x=max(22,min(W-22,filled)); ph=self._phase
        run=self.player and self.player.state()==QMediaPlayer.PlayingState; spd=1.0 if run else 0.0
        def s(a): return math.sin(ph*a)*spd
        by=ty-28; hcy=by-16
        p.setPen(QPen(QColor(T["accent"]),2.5,Qt.SolidLine,Qt.RoundCap))
        p.drawLine(QPointF(x,hcy+9),QPointF(x,by))
        p.setPen(Qt.NoPen); p.setBrush(QColor(T["accent"])); p.drawEllipse(QPointF(x,hcy),7.5,7.5)
        ec=QColor(T["bg"]); p.setBrush(ec)
        p.drawEllipse(QPointF(x+2.5,hcy-1.5),1.8,1.8); p.drawEllipse(QPointF(x-1.5,hcy-1.5),1.2,1.2)
        p.setPen(QPen(ec,1.2,Qt.SolidLine,Qt.RoundCap)); p.setBrush(Qt.NoBrush)
        p.drawArc(QRectF(x-3.5,hcy+0.5,7,4),200*16,140*16)
        p.setPen(QPen(QColor(T["accent2"]),1.2,Qt.SolidLine,Qt.RoundCap))
        for hx,hy,hx2,hy2 in [(x-4,hcy-6,x-6,hcy-9),(x,hcy-7,x,hcy-10),(x+4,hcy-6,x+5,hcy-9)]:
            p.drawLine(QPointF(hx,hy),QPointF(hx2,hy2))
        sh=QPointF(x,by-10); p.setPen(QPen(QColor(T["accent"]),2.2,Qt.SolidLine,Qt.RoundCap))
        lae=QPointF(x-8-s(1.1)*5,by-4+s(1.3)*3); lah=QPointF(x-12-s(1.1)*3,by+2+s(1.5)*4)
        rae=QPointF(x+8+s(1.1)*5,by-4-s(1.3)*3); rah=QPointF(x+12+s(1.1)*3,by+2-s(1.5)*4)
        for a,b in [(sh,lae),(lae,lah),(sh,rae),(rae,rah)]: p.drawLine(a,b)
        hip=QPointF(x,by); p.setPen(QPen(QColor(T["accent"]),2.5,Qt.SolidLine,Qt.RoundCap))
        llk=QPointF(x-5+s(1)*6,by+9-abs(s(1))*2); llf=QPointF(x-3+s(1)*9,by+18)
        rlk=QPointF(x+5-s(1)*6,by+9-abs(s(1))*2); rlf=QPointF(x+3-s(1)*9,by+18)
        for a,b in [(hip,llk),(llk,llf),(hip,rlk),(rlk,rlf)]: p.drawLine(a,b)
        p.setPen(Qt.NoPen); p.setBrush(QColor(T["accent2"]))
        for ft in (llf,rlf): p.drawRoundedRect(QRectF(ft.x()-5,ft.y()-2,10,4),2,2)
        p.setBrush(QColor(0,0,0,40)); p.drawEllipse(QPointF(x,ty+1),10,3)


# ═══════════════════════════════════════════════════════════
#  VOLUME SLIDER
# ═══════════════════════════════════════════════════════════
class VolumeSlider(QWidget):
    def __init__(self,theme,parent=None):
        super().__init__(parent); self.theme=theme; self._val=70; self._glow=0.0
        self._drag=False; self._cbs=[]; self.setFixedSize(36,170); self.setCursor(Qt.PointingHandCursor)
        self._ga=QPropertyAnimation(self,b"glow"); self._ga.setDuration(200)
    @property
    def _tt(self): return 14
    @property
    def _tb(self): return self.height()-42
    def set_theme(self,t): self.theme=t; self.update()
    def connect(self,cb): self._cbs.append(cb)
    def value(self): return self._val
    def setValue(self,v):
        self._val=max(0,min(100,v))
        for cb in self._cbs: cb(self._val)
        self.update()
    @pyqtProperty(float)
    def glow(self): return self._glow
    @glow.setter
    def glow(self,v): self._glow=v; self.update()
    def _ag(self,t): self._ga.stop(); self._ga.setStartValue(self._glow); self._ga.setEndValue(t); self._ga.start()
    def enterEvent(self,e): self._ag(1.0)
    def leaveEvent(self,e):
        if not self._drag: self._ag(0.0)
    def _ytov(self,y): return int(max(0,min(100,(1-(y-self._tt)/max(1,self._tb-self._tt))*100)))
    def mousePressEvent(self,e):
        if e.y()<self._tb+8: self._drag=True; self.setValue(self._ytov(e.y()))
    def mouseMoveEvent(self,e):
        if self._drag: self.setValue(self._ytov(e.y()))
    def mouseReleaseEvent(self,e): self._drag=False
    def wheelEvent(self,e): self.setValue(self._val+(5 if e.angleDelta().y()>0 else -5))
    def paintEvent(self,event):
        p=QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        T=self.theme; W=self.width(); tt=self._tt; tb=self._tb; cx=W//2; tw=6
        p.setPen(QColor(T["text2"])); p.setFont(QFont(get_current_font(),7))
        p.drawText(QRect(0,0,W,14),Qt.AlignCenter,f"{self._val}%")
        if self._glow>0.01:
            gc=QColor(T["accent"]); gc.setAlpha(int(55*self._glow)); p.setPen(Qt.NoPen); p.setBrush(gc)
            p.drawRoundedRect(cx-10,tt-4,20,(tb-tt)+8,10,10)
        p.setPen(Qt.NoPen); p.setBrush(QColor(T["track_bg"])); p.drawRoundedRect(cx-tw//2,tt,tw,tb-tt,3,3)
        r=self._val/100; fh=int((tb-tt)*r); fy=tb-fh
        g=QLinearGradient(0,tb,0,fy); g.setColorAt(0,QColor(T["accent"])); g.setColorAt(1,QColor(T["accent2"]))
        p.setBrush(g); p.drawRoundedRect(cx-tw//2,fy,tw,fh,3,3)
        hy=int(tb-(tb-tt)*r); p.setPen(Qt.NoPen)
        rc=QColor(T["accent"]); rc.setAlpha(int(120+135*self._glow)); p.setBrush(rc); p.drawEllipse(QPoint(cx,hy),10,10)
        p.setBrush(QColor(T["slider_handle"])); p.drawEllipse(QPoint(cx,hy),6,6)
        p.save(); p.translate((W-22)//2,tb+8)
        a=int(160+95*self._glow); c=QColor(T["accent"] if self._val>0 else T["text2"]); c.setAlpha(a)
        p.setPen(Qt.NoPen); p.setBrush(c)
        p.drawPolygon(QPolygon([QPoint(0,4),QPoint(0,12),QPoint(5,15),QPoint(5,1)]))
        p.drawPolygon(QPolygon([QPoint(5,1),QPoint(13,8),QPoint(5,15)]))
        if self._val==0:
            p.setPen(QPen(c,1.8,Qt.SolidLine,Qt.RoundCap)); p.drawLine(13,3,20,13); p.drawLine(13,13,20,3)
        elif self._val<40:
            p.setPen(QPen(c,1.8,Qt.SolidLine,Qt.RoundCap)); p.setBrush(Qt.NoBrush)
            p.drawArc(QRectF(13,3,5,10),90*16,-180*16)
        else:
            p.setPen(QPen(c,1.8,Qt.SolidLine,Qt.RoundCap)); p.setBrush(Qt.NoBrush)
            p.drawArc(QRectF(13,3,5,10),90*16,-180*16); p.drawArc(QRectF(16,0,7,16),90*16,-180*16)
        p.restore()


# ═══════════════════════════════════════════════════════════
#  SPEED POPUP / BUTTON
# ═══════════════════════════════════════════════════════════
SPEED_VALUES=[0.25,0.5,0.75,1.0,1.25,1.5,1.75,2.0]

class SpeedPopup(AnimatedPopupMixin,QFrame):
    speed_selected=Signal(float)
    def __init__(self,theme,current,parent=None):
        QFrame.__init__(self,parent,Qt.Popup|Qt.FramelessWindowHint)
        self.theme=theme; self.current=current; self._hov=None
        self.setFixedSize(80,len(SPEED_VALUES)*32+8); self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True); self._init_popup_anim()
    def paintEvent(self,e):
        p=QPainter(self); p.setRenderHint(QPainter.Antialiasing); T=self.theme; W,H=self.width(),self.height()
        p.setPen(Qt.NoPen); p.setBrush(QColor(T["surface"])); p.drawRoundedRect(0,0,W,H,10,10)
        p.setPen(QPen(QColor(T["accent"]),1)); p.setBrush(Qt.NoBrush); p.drawRoundedRect(1,1,W-2,H-2,10,10)
        for i,v in enumerate(reversed(SPEED_VALUES)):
            y=4+i*32; ic=abs(v-self.current)<0.01; ih=self._hov==i
            p.setPen(Qt.NoPen)
            if ic: p.setBrush(QColor(T["accent"])); p.drawRoundedRect(4,y,W-8,30,6,6); p.setPen(QColor(T["bg"]))
            elif ih: p.setBrush(QColor(T["hover"])); p.drawRoundedRect(4,y,W-8,30,6,6); p.setPen(QColor(T["text"]))
            else: p.setPen(QColor(T["text2"]))
            p.setFont(QFont(get_current_font(),10,QFont.Bold if ic else QFont.Normal))
            p.drawText(QRect(0,y,W,30),Qt.AlignCenter,f"{v:g}×")
    def mouseMoveEvent(self,e): i=(e.y()-4)//32; self._hov=i if 0<=i<len(SPEED_VALUES) else None; self.update()
    def mousePressEvent(self,e):
        i=(e.y()-4)//32
        if 0<=i<len(SPEED_VALUES): self.speed_selected.emit(list(reversed(SPEED_VALUES))[i])
        self.close()
    def leaveEvent(self,e): self._hov=None; self.update()

class SpeedButton(QWidget):
    def __init__(self,theme,lang,parent=None):
        super().__init__(parent); self.theme=theme; self.lang=lang; self._speed=1.0; self._hover=False; self._cbs=[]
        self.setFixedSize(64,42); self.setCursor(Qt.PointingHandCursor)
    def set_theme(self,t): self.theme=t; self.update()
    def set_lang(self,l): self.lang=l; self.update()
    def connect(self,cb): self._cbs.append(cb)
    def value(self): return self._speed
    def enterEvent(self,e): self._hover=True;  self.update()
    def leaveEvent(self,e): self._hover=False; self.update()
    def mousePressEvent(self,e):
        if e.button()==Qt.LeftButton:
            pp=SpeedPopup(self.theme,self._speed,self); pp.speed_selected.connect(self._on)
            pp.move(self.mapToGlobal(QPoint(0,-len(SPEED_VALUES)*32-10))); pp.show()
    def _on(self,v): self._speed=v; [cb(v) for cb in self._cbs]; self.update()
    def paintEvent(self,e):
        p=QPainter(self); p.setRenderHint(QPainter.Antialiasing); T=self.theme; W,H=self.width(),self.height()
        bg=QColor(T["surface"]); bg=bg.lighter(130) if self._hover else bg
        p.setPen(Qt.NoPen); p.setBrush(bg); p.drawRoundedRect(0,0,W,H,8,8)
        p.setPen(QPen(QColor(T["accent"]),1.5)); p.setBrush(Qt.NoBrush); p.drawRoundedRect(1,1,W-2,H-2,8,8)
        p.setPen(QColor(T["accent"])); p.setFont(QFont(get_current_font(),12,QFont.Bold if get_font_bold() else QFont.Normal))
        p.drawText(QRect(0,2,W,H-8),Qt.AlignCenter,f"{self._speed:g}×")
        p.setPen(Qt.NoPen); p.setBrush(QColor(T["text2"]))
        p.drawPolygon(QPolygon([QPoint(W//2-4,H-6),QPoint(W//2+4,H-6),QPoint(W//2,H-2)]))


# ═══════════════════════════════════════════════════════════
#  SLEEP BUTTON
# ═══════════════════════════════════════════════════════════
class SleepPopup(AnimatedPopupMixin,QFrame):
    sleep_selected=Signal(int)
    def __init__(self,theme,current,parent=None):
        QFrame.__init__(self,parent,Qt.Popup|Qt.FramelessWindowHint)
        T=theme; self.theme=theme; self.setAttribute(Qt.WA_TranslucentBackground); self.setFixedSize(250,150)
        lo=QVBoxLayout(self); lo.setContentsMargins(10,10,10,10); lo.setSpacing(5)
        self.lbl=QLabel(f"Time: {current} min"); self.lbl.setStyleSheet(f"color:{T['text']};font-size:12px;"); lo.addWidget(self.lbl)
        self.sl=QSlider(Qt.Horizontal); self.sl.setRange(0,1440); self.sl.setValue(current)
        self.sl.setStyleSheet(f"QSlider::groove:horizontal{{background:{T['track_bg']};height:6px;border-radius:3px;}}QSlider::handle:horizontal{{background:{T['accent']};width:16px;height:16px;border-radius:8px;margin:-5px 0;}}")
        self.sl.valueChanged.connect(self._ul); lo.addWidget(self.sl)
        self.inp=QLineEdit(); self.inp.setPlaceholderText("Enter minutes (0-1440)")
        self.inp.setStyleSheet(f"background:{T['surface']};color:{T['text']};border:1px solid {T['track_bg']};border-radius:4px;padding:4px;")
        self.inp.returnPressed.connect(self._ps); lo.addWidget(self.inp)
        btn=QPushButton("Set"); btn.setStyleSheet(f"background:{T['accent']};color:{T['bg']};border:none;border-radius:6px;padding:5px;")
        btn.clicked.connect(self._on); lo.addWidget(btn); self.inp.setFocus(); self._init_popup_anim()
    def _ul(self,v):
        if v<60: self.lbl.setText(f"Time: {v} min")
        else: h,m=divmod(v,60); self.lbl.setText(f"Time: {h}h"+(f" {m}m" if m else ""))
    def _ps(self):
        try:
            m=int(self.inp.text().strip())
            if 0<=m<=1440: self.sl.setValue(m); self._on()
            else: self.inp.clear()
        except: self.inp.clear()
    def _on(self): self.sleep_selected.emit(self.sl.value()); self.close()
    def paintEvent(self,e):
        p=QPainter(self); p.setRenderHint(QPainter.Antialiasing); T=self.theme; W,H=self.width(),self.height()
        p.setPen(Qt.NoPen); p.setBrush(QColor(T["surface"])); p.drawRoundedRect(0,0,W,H,10,10)
        p.setPen(QPen(QColor(T["accent"]),1)); p.setBrush(Qt.NoBrush); p.drawRoundedRect(1,1,W-2,H-2,10,10)

class SleepButton(QWidget):
    def __init__(self,theme,lang,parent=None):
        super().__init__(parent); self.theme=theme; self.lang=lang; self._sleep=0
        self._rm=0; self._rs=0; self._hover=False; self._cbs=[]
        self.setFixedSize(100,42); self.setCursor(Qt.PointingHandCursor)
    def set_theme(self,t): self.theme=t; self.update()
    def set_lang(self,l): self.lang=l; self.update()
    def connect(self,cb): self._cbs.append(cb)
    def value(self): return self._sleep
    def set_remaining(self,m,s): self._rm=m; self._rs=s; self.update()
    def enterEvent(self,e): self._hover=True;  self.update()
    def leaveEvent(self,e): self._hover=False; self.update()
    def mousePressEvent(self,e):
        if e.button()==Qt.LeftButton:
            pp=SleepPopup(self.theme,self._sleep,self); pp.sleep_selected.connect(self._on)
            pp.move(self.mapToGlobal(QPoint(0,-160))); pp.show()
    def _on(self,v): self._sleep=v; [cb(v) for cb in self._cbs]; self.update()
    def paintEvent(self,e):
        p=QPainter(self); p.setRenderHint(QPainter.Antialiasing); T=self.theme; W,H=self.width(),self.height()
        bg=QColor(T["surface"]); bg=bg.lighter(130) if self._hover else bg
        p.setPen(Qt.NoPen); p.setBrush(bg); p.drawRoundedRect(0,0,W,H,8,8)
        p.setPen(QPen(QColor(T["accent"]),1.5)); p.setBrush(Qt.NoBrush); p.drawRoundedRect(1,1,W-2,H-2,8,8)
        txt=f"{self._rm}m {self._rs}s" if (self._rm or self._rs) else "Sleep"
        p.setPen(QColor(T["accent"])); p.setFont(QFont(get_current_font(),9,QFont.Bold if get_font_bold() else QFont.Normal))
        p.drawText(QRect(0,2,W,H-8),Qt.AlignCenter,txt)
        p.setPen(Qt.NoPen); p.setBrush(QColor(T["text2"]))
        p.drawPolygon(QPolygon([QPoint(W//2-4,H-6),QPoint(W//2+4,H-6),QPoint(W//2,H-2)]))


# ═══════════════════════════════════════════════════════════
#  PLAY/PAUSE BUTTON
# ═══════════════════════════════════════════════════════════
class PlayPauseButton(QWidget):
    def __init__(self,theme,parent=None):
        super().__init__(parent); self.theme=theme; self._playing=False; self._morph=0.0; self._hover=False; self._ps=1.0
        self.setFixedSize(54,54); self.setCursor(Qt.PointingHandCursor)
        self._ma=QPropertyAnimation(self,b"morph"); self._ma.setDuration(220); self._ma.setEasingCurve(QEasingCurve.InOutCubic)
        self._pa=QPropertyAnimation(self,b"press_scale"); self._pa.setDuration(100); self._pa.setEasingCurve(QEasingCurve.OutQuad)
    def set_theme(self,t): self.theme=t; self.update()
    @pyqtProperty(float)
    def morph(self): return self._morph
    @morph.setter
    def morph(self,v): self._morph=v; self.update()
    @pyqtProperty(float)
    def press_scale(self): return self._ps
    @press_scale.setter
    def press_scale(self,v): self._ps=v; self.update()
    def set_playing(self,pl):
        if pl==self._playing: return
        self._playing=pl; self._ma.stop(); self._ma.setStartValue(self._morph)
        self._ma.setEndValue(1.0 if pl else 0.0); self._ma.start()
    def enterEvent(self,e): self._hover=True;  self.update()
    def leaveEvent(self,e): self._hover=False; self.update()
    def mousePressEvent(self,e):
        if e.button()==Qt.LeftButton:
            self._pa.stop(); self._pa.setStartValue(1.0); self._pa.setEndValue(0.82); self._pa.start()
    def mouseReleaseEvent(self,e):
        if e.button()==Qt.LeftButton:
            self._pa.stop(); self._pa.setStartValue(self._ps); self._pa.setEndValue(1.0); self._pa.start()
            self.clicked()
    def clicked(self): pass
    def paintEvent(self,event):
        T=self.theme; p=QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        p.translate(self.width()/2,self.height()/2); p.scale(self._ps,self._ps)
        r=24+(2 if self._hover else 0); p.setPen(Qt.NoPen)
        p.setBrush(QColor(T["accent2"] if self._hover else T["accent"])); p.drawEllipse(QPoint(0,0),r,r)
        t=self._morph; p.setBrush(QColor("#FFF"))
        def lr(a,b,t): return a+(b-a)*t
        A=(lr(-7,-8,t),lr(-10,-9,t)); B=(lr(-7,-8,t),lr(10,9,t))
        C1=(lr(10,-3,t),lr(0,-9,t)); C2=(lr(10,-3,t),lr(0,9,t))
        p.drawPolygon(QPolygon([QPoint(int(A[0]),int(A[1])),QPoint(int(C1[0]),int(C1[1])),QPoint(int(C2[0]),int(C2[1])),QPoint(int(B[0]),int(B[1]))]))
        if t>0.01:
            p.setBrush(QColor(255,255,255,int(255*t))); rx=lr(10,3,t); rw=lr(0,5,t); rh=lr(0,18,t)
            p.drawPolygon(QPolygon([QPoint(int(rx),int(-rh/2)),QPoint(int(rx+rw),int(-rh/2)),QPoint(int(rx+rw),int(rh/2)),QPoint(int(rx),int(rh/2))]))


# ═══════════════════════════════════════════════════════════
#  ICON BUTTON  (unified vector drawing)
# ═══════════════════════════════════════════════════════════
class IconButton(QPushButton):
    def __init__(self,icon_type,theme,parent=None):
        super().__init__(parent); self.icon_type=icon_type; self.theme=theme; self.is_active=False
        self._color=None; self._hover=False; self._ps=1.0; self._bo=0.0
        self.setFixedSize(46,46); self.setStyleSheet("QPushButton { background: transparent; border: none; }"); self.setCursor(Qt.PointingHandCursor)
        self._pa=QPropertyAnimation(self,b"press_scale"); self._pa.setDuration(100); self._pa.setEasingCurve(QEasingCurve.OutQuad)
        self._ha=QPropertyAnimation(self,b"bg_opacity"); self._ha.setDuration(150); self._ha.setEasingCurve(QEasingCurve.InOutQuad)
    def _c(self): return self._color if self._color else QColor(self.theme["accent"] if self.is_active else self.theme["text"])
    @pyqtProperty(float)
    def press_scale(self): return self._ps
    @press_scale.setter
    def press_scale(self,v): self._ps=v; self.update()
    @pyqtProperty(float)
    def bg_opacity(self): return self._bo
    @bg_opacity.setter
    def bg_opacity(self,v): self._bo=v; self.update()
    def set_theme(self,t): self.theme=t; self._color=None; self.update()
    def setActive(self,s): self.is_active=s; self._color=None; self.update()
    def _ah(self,t): self._ha.stop(); self._ha.setStartValue(self._bo); self._ha.setEndValue(t); self._ha.start()
    def enterEvent(self,e): self._hover=True;  self._color=QColor(self.theme["accent"]); self._ah(1.0); self.update()
    def leaveEvent(self,e): self._hover=False; self._color=None; self._ah(0.0); self.update()
    def mousePressEvent(self,e):
        if e.button()==Qt.LeftButton:
            self._pa.stop(); self._pa.setStartValue(1.0); self._pa.setEndValue(0.85); self._pa.start()
            super().mousePressEvent(e)
    def mouseReleaseEvent(self,e):
        if e.button()==Qt.LeftButton:
            self._pa.stop(); self._pa.setStartValue(self._ps); self._pa.setEndValue(1.0); self._pa.start()
            super().mouseReleaseEvent(e)
    def paintEvent(self,event):
        p=QPainter(self); p.setRenderHint(QPainter.Antialiasing); w,h=self.width(),self.height()
        p.save(); p.translate(w/2,h/2); p.scale(self._ps,self._ps); p.translate(-w/2,-h/2)
        t=self.icon_type
        if self._bo>0.01:
            bg=QColor("#FF6B6B" if t=="close" else self.theme["hover"]); bg.setAlpha(int(255*self._bo))
            p.setPen(Qt.NoPen); p.setBrush(bg); p.drawRoundedRect(0,0,w,h,8,8)
        c=self._c()
        if t=="close" and self._hover: c=QColor("#FFF")
        p.setPen(Qt.NoPen); p.setBrush(c)
        if t=="next":
            p.drawPolygon(QPolygon([QPoint(int(w*.2),int(h*.2)),QPoint(int(w*.2),int(h*.8)),QPoint(int(w*.55),int(h*.5))]))
            p.drawPolygon(QPolygon([QPoint(int(w*.55),int(h*.2)),QPoint(int(w*.55),int(h*.8)),QPoint(int(w*.9),int(h*.5))]))
        elif t=="prev":
            p.drawPolygon(QPolygon([QPoint(int(w*.8),int(h*.2)),QPoint(int(w*.8),int(h*.8)),QPoint(int(w*.45),int(h*.5))]))
            p.drawPolygon(QPolygon([QPoint(int(w*.45),int(h*.2)),QPoint(int(w*.45),int(h*.8)),QPoint(int(w*.1),int(h*.5))]))
        elif t=="stop":  p.drawRect(int(w*.3),int(h*.3),int(w*.4),int(h*.4))
        elif t=="add":   p.drawRect(int(w*.45),int(h*.2),int(w*.1),int(h*.6)); p.drawRect(int(w*.2),int(h*.45),int(w*.6),int(h*.1))
        elif t=="delete":
            pn=QPen(c,3,Qt.SolidLine,Qt.RoundCap); p.setPen(pn); p.setBrush(Qt.NoBrush)
            p.drawLine(int(w*.25),int(h*.25),int(w*.75),int(h*.75)); p.drawLine(int(w*.25),int(h*.75),int(w*.75),int(h*.25))
        elif t=="shuffle":
            pn=QPen(c,2,Qt.SolidLine,Qt.RoundCap); p.setPen(pn); p.setBrush(Qt.NoBrush)
            p.drawLine(int(w*.2),int(h*.35),int(w*.5),int(h*.35)); p.drawLine(int(w*.5),int(h*.35),int(w*.8),int(h*.65))
            p.drawLine(int(w*.2),int(h*.65),int(w*.5),int(h*.65)); p.drawLine(int(w*.5),int(h*.65),int(w*.8),int(h*.35))
        elif t=="repeat":
            pn=QPen(c,2,Qt.SolidLine,Qt.RoundCap); p.setPen(pn); p.setBrush(Qt.NoBrush)
            p.drawArc(int(w*.2),int(h*.25),int(w*.6),int(h*.5),30*16,300*16)
            p.setBrush(c); p.setPen(Qt.NoPen)
            p.drawPolygon(QPolygon([QPoint(int(w*.72),int(h*.22)),QPoint(int(w*.88),int(h*.32)),QPoint(int(w*.72),int(h*.42))]))
        elif t in ("minimize","hide_bar"):
            pn=QPen(c,2,Qt.SolidLine,Qt.RoundCap); p.setPen(pn); p.setBrush(Qt.NoBrush)
            p.drawLine(int(w*.2),int(h*.5),int(w*.8),int(h*.5))
            if t=="hide_bar":
                p.drawLine(int(w*.2),int(h*.35),int(w*.8),int(h*.35))
                p.drawLine(int(w*.2),int(h*.65),int(w*.8),int(h*.65))
                p.setBrush(c); p.setPen(Qt.NoPen)
                p.drawPolygon(QPolygon([QPoint(int(w*.35),int(h*.72)),QPoint(int(w*.65),int(h*.72)),QPoint(int(w*.5),int(h*.85))]))
        elif t=="close":
            pn=QPen(c,3,Qt.SolidLine,Qt.RoundCap); p.setPen(pn); p.setBrush(Qt.NoBrush)
            p.drawLine(int(w*.3),int(h*.3),int(w*.7),int(h*.7)); p.drawLine(int(w*.3),int(h*.7),int(w*.7),int(h*.3))
        elif t=="like":
            heart=QPainterPath(); heart.moveTo(w*.5,h*.78)
            heart.cubicTo(w*.15,h*.56,w*.12,h*.28,w*.32,h*.24)
            heart.cubicTo(w*.44,h*.22,w*.5,h*.32,w*.5,h*.36)
            heart.cubicTo(w*.5,h*.32,w*.56,h*.22,w*.68,h*.24)
            heart.cubicTo(w*.88,h*.28,w*.85,h*.56,w*.5,h*.78)
            if self.is_active: p.setBrush(c); p.setPen(Qt.NoPen)
            else: p.setPen(QPen(c,2.4,Qt.SolidLine,Qt.RoundCap,Qt.RoundJoin)); p.setBrush(Qt.NoBrush)
            p.drawPath(heart)
        elif t=="maximize":
            pn=QPen(c,2,Qt.SolidLine,Qt.RoundCap); p.setPen(pn); p.setBrush(Qt.NoBrush)
            p.drawRect(int(w*.25),int(h*.25),int(w*.5),int(h*.5))
        elif t=="eula":
            p.setPen(Qt.NoPen); p.setBrush(c); p.drawRoundedRect(int(w*.25),int(h*.15),int(w*.5),int(h*.7),2,2)
            p.setBrush(QColor(self.theme["bg"]))
            for i in range(3): p.drawRect(int(w*.33),int(h*.3+i*h*.13),int(w*.34),int(h*.07))
        elif t=="theme":
            p.setPen(Qt.NoPen)
            for idx,col in enumerate(["#FF6B6B","#FFD700","#64FFDA","#9C6FFF"]):
                ang=idx*90; rx=int(w//2+10*math.cos(math.radians(ang))-4); ry=int(h//2+10*math.sin(math.radians(ang))-4)
                p.setBrush(QColor(col)); p.drawEllipse(rx,ry,8,8)
            p.setBrush(c); p.drawEllipse(int(w//2-4),int(h//2-4),8,8)
        elif t=="font":
            p.setPen(Qt.NoPen); p.setBrush(c)
            path=QPainterPath(); cx=w/2
            path.moveTo(cx,h*.15); path.lineTo(cx-w*.28,h*.82); path.lineTo(cx-w*.14,h*.82)
            path.lineTo(cx-w*.06,h*.58); path.lineTo(cx+w*.06,h*.58); path.lineTo(cx+w*.14,h*.82)
            path.lineTo(cx+w*.28,h*.82); path.closeSubpath()
            cut=QPainterPath(); cut.addRect(QRectF(cx-w*.22,h*.52,w*.44,h*.10))
            p.drawPath(path.subtracted(cut))
        elif t=="crossfade":
            pn=QPen(c,2,Qt.SolidLine,Qt.RoundCap); p.setPen(pn); p.setBrush(Qt.NoBrush)
            for pts in ([QPointF(w*.1,h*.5),QPointF(w*.25,h*.25),QPointF(w*.4,h*.75),QPointF(w*.55,h*.5)],
                        [QPointF(w*.45,h*.5),QPointF(w*.6,h*.3),QPointF(w*.75,h*.7),QPointF(w*.9,h*.5)]):
                path2=QPainterPath(); path2.moveTo(pts[0])
                for pt in pts[1:]: path2.lineTo(pt)
                p.drawPath(path2)
            if not self.is_active:
                p.setPen(QPen(QColor("#FF6B6B"),2,Qt.SolidLine,Qt.RoundCap))
                p.drawLine(QPointF(w*.15,h*.15),QPointF(w*.85,h*.85))
        elif t=="discord":
            p.setPen(Qt.NoPen); p.setBrush(c)
            body=QPainterPath(); body.addRoundedRect(QRectF(w*.15,h*.22,w*.7,h*.55),10,10); p.drawPath(body)
            p.setBrush(QColor(self.theme["bg"]))
            p.drawEllipse(QPointF(w*.35,h*.48),w*.085,h*.085); p.drawEllipse(QPointF(w*.65,h*.48),w*.085,h*.085)
            p.setBrush(c); p.drawRoundedRect(QRectF(w*.15,h*.18,w*.16,h*.2),4,4)
            p.drawRoundedRect(QRectF(w*.69,h*.18,w*.16,h*.2),4,4)
        elif t=="eq":
            # Triangle EQ icon
            p.setBrush(Qt.NoBrush)
            pn=QPen(c,2,Qt.SolidLine,Qt.RoundCap,Qt.RoundJoin); p.setPen(pn)
            p.drawPolygon(QPolygon([QPoint(int(w*.5),int(h*.15)),QPoint(int(w*.85),int(h*.82)),QPoint(int(w*.15),int(h*.82))]))
            p.setPen(QPen(c,1.5,Qt.SolidLine,Qt.RoundCap))
            p.drawLine(int(w*.5),int(h*.15),int(w*.5),int(h*.82))
            p.drawLine(int(w*.15),int(h*.82),int(w*.675),int(h*.48))
        elif t=="lyrics":
            # Lines + music note
            pn=QPen(c,2,Qt.SolidLine,Qt.RoundCap); p.setPen(pn); p.setBrush(Qt.NoBrush)
            p.drawLine(int(w*.2),int(h*.32),int(w*.75),int(h*.32))
            p.drawLine(int(w*.2),int(h*.48),int(w*.65),int(h*.48))
            p.drawLine(int(w*.2),int(h*.64),int(w*.55),int(h*.64))
            p.setBrush(c); p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(w*.78,h*.62),w*.065,h*.055)
            p.setPen(QPen(c,1.5,Qt.SolidLine,Qt.RoundCap)); p.setBrush(Qt.NoBrush)
            p.drawLine(QPointF(w*.845,h*.62),QPointF(w*.845,h*.35))
        elif t=="grid":
            p.setPen(Qt.NoPen); p.setBrush(c)
            gs=int(w*.18); gp=int(w*.07)
            for ri in range(2):
                for ci in range(2):
                    p.drawRoundedRect(int(w*.24)+ci*(gs+gp),int(h*.24)+ri*(gs+gp),gs,gs,2,2)
        elif t=="glow":
            p.setPen(QPen(c, 2, Qt.SolidLine, Qt.RoundCap)); p.setBrush(Qt.NoBrush)
            p.drawEllipse(QPointF(w*.5, h*.5), w*.15, h*.15)
            for i in range(8):
                ang = i * 45; rad1 = w*.22; rad2 = w*.35
                if i % 2 != 0: rad1 = w*.18; rad2 = w*.28
                x1 = w*.5 + math.cos(math.radians(ang)) * rad1; y1 = h*.5 + math.sin(math.radians(ang)) * rad1
                x2 = w*.5 + math.cos(math.radians(ang)) * rad2; y2 = h*.5 + math.sin(math.radians(ang)) * rad2
                p.drawLine(QPointF(x1, y1), QPointF(x2, y2))
            if not self.is_active:
                p.setPen(QPen(QColor("#FF6B6B"), 2, Qt.SolidLine, Qt.RoundCap))
                p.drawLine(QPointF(w*.2, h*.2), QPointF(w*.8, h*.8))
        elif t=="settings":
            p.setPen(Qt.NoPen); p.setBrush(c)
            cx_f=w*0.5; cy_f=h*0.5; n=8
            r_out=w*0.43; r_in=w*0.29; r_hole=w*0.14
            half_tooth=math.pi/n*0.52
            gear=QPainterPath(); first=True
            for i in range(n):
                ang=i*2*math.pi/n - math.pi/2
                tl=ang-half_tooth; tr=ang+half_tooth
                if first:
                    gear.moveTo(cx_f+r_in*math.cos(tl),cy_f+r_in*math.sin(tl)); first=False
                else:
                    gear.lineTo(cx_f+r_in*math.cos(tl),cy_f+r_in*math.sin(tl))
                gear.lineTo(cx_f+r_out*math.cos(tl),cy_f+r_out*math.sin(tl))
                gear.lineTo(cx_f+r_out*math.cos(tr),cy_f+r_out*math.sin(tr))
                gear.lineTo(cx_f+r_in*math.cos(tr),cy_f+r_in*math.sin(tr))
            gear.closeSubpath()
            hole=QPainterPath(); hole.addEllipse(QPointF(cx_f,cy_f),r_hole,r_hole)
            p.drawPath(gear.subtracted(hole))
        p.restore()


class SettingsLaunchButton(QWidget):
    def __init__(self, theme, parent=None):
        super().__init__(parent); self.theme=theme; self._hover=False; self._cbs=[]
        self.setFixedSize(44,36); self.setCursor(Qt.PointingHandCursor)
    def set_theme(self,t): self.theme=t; self.update()
    def connect(self,cb): self._cbs.append(cb)
    def enterEvent(self,e): self._hover=True; self.update()
    def leaveEvent(self,e): self._hover=False; self.update()
    def mousePressEvent(self,e):
        if e.button()==Qt.LeftButton: [cb() for cb in self._cbs]
    def paintEvent(self,e):
        p=QPainter(self); p.setRenderHint(QPainter.Antialiasing); T=self.theme; W,H=self.width(),self.height()
        bg=QColor(T["hover"] if self._hover else T["surface"])
        p.setPen(Qt.NoPen); p.setBrush(bg); p.drawRoundedRect(0,0,W,H,8,8)
        p.setPen(QPen(QColor(T["accent"] if self._hover else T["track_bg"]),1.2))
        p.setBrush(Qt.NoBrush); p.drawRoundedRect(1,1,W-2,H-2,8,8)
        c=QColor(T["accent"] if self._hover else T["text"])
        p.setPen(Qt.NoPen); p.setBrush(c)
        cx,cy=W/2,H/2; n=8
        r_out=11.0; r_in=7.5; r_hole=4.0
        half_tooth=math.pi/n*0.52
        gear=QPainterPath(); first=True
        for i in range(n):
            ang=i*2*math.pi/n - math.pi/2
            tl=ang-half_tooth; tr=ang+half_tooth
            if first:
                gear.moveTo(cx+r_in*math.cos(tl),cy+r_in*math.sin(tl)); first=False
            else:
                gear.lineTo(cx+r_in*math.cos(tl),cy+r_in*math.sin(tl))
            gear.lineTo(cx+r_out*math.cos(tl),cy+r_out*math.sin(tl))
            gear.lineTo(cx+r_out*math.cos(tr),cy+r_out*math.sin(tr))
            gear.lineTo(cx+r_in*math.cos(tr),cy+r_in*math.sin(tr))
        gear.closeSubpath()
        hole=QPainterPath(); hole.addEllipse(QPointF(cx,cy),r_hole,r_hole)
        p.drawPath(gear.subtracted(hole))


# ═══════════════════════════════════════════════════════════
#  FONT BUTTON + POPUP
# ═══════════════════════════════════════════════════════════
class FontPopup(AnimatedPopupMixin,QFrame):
    font_selected=Signal(int)
    def __init__(self,theme,cur,parent=None):
        QFrame.__init__(self,parent,Qt.Popup|Qt.FramelessWindowHint)
        self.theme=theme; self.cur=cur; self._hov=None; ih=28
        self.setFixedSize(180,len(FONT_FAMILIES)*ih+8); self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True); self._ih=ih; self._init_popup_anim()
    def paintEvent(self,e):
        p=QPainter(self); p.setRenderHint(QPainter.Antialiasing); T=self.theme; W,H=self.width(),self.height()
        p.setPen(Qt.NoPen); p.setBrush(QColor(T["surface"])); p.drawRoundedRect(0,0,W,H,10,10)
        p.setPen(QPen(QColor(T["accent"]),1)); p.setBrush(Qt.NoBrush); p.drawRoundedRect(1,1,W-2,H-2,10,10)
        for i,(fn,bold) in enumerate(FONT_FAMILIES):
            y=4+i*self._ih; ic=(i==self.cur); ih=(self._hov==i)
            p.setPen(Qt.NoPen)
            if ic: p.setBrush(QColor(T["accent"])); p.drawRoundedRect(4,y,W-8,self._ih-2,5,5); p.setPen(QColor(T["bg"]))
            elif ih: p.setBrush(QColor(T["hover"])); p.drawRoundedRect(4,y,W-8,self._ih-2,5,5); p.setPen(QColor(T["text"]))
            else: p.setPen(QColor(T["text2"]))
            p.setFont(QFont(fn,9,QFont.Bold if bold else QFont.Normal))
            p.drawText(QRect(8,y,W-16,self._ih-2),Qt.AlignVCenter|Qt.AlignLeft,fn)
    def mouseMoveEvent(self,e): i=(e.y()-4)//self._ih; self._hov=i if 0<=i<len(FONT_FAMILIES) else None; self.update()
    def mousePressEvent(self,e):
        i=(e.y()-4)//self._ih
        if 0<=i<len(FONT_FAMILIES): self.font_selected.emit(i)
        self.close()
    def leaveEvent(self,e): self._hov=None; self.update()

class FontButton(QWidget):
    font_changed=Signal(int)
    def __init__(self,theme,parent=None):
        super().__init__(parent); self.theme=theme; self._hover=False
        self.setFixedSize(42,36); self.setCursor(Qt.PointingHandCursor)
    def set_theme(self,t): self.theme=t; self.update()
    def enterEvent(self,e): self._hover=True;  self.update()
    def leaveEvent(self,e): self._hover=False; self.update()
    def mousePressEvent(self,e):
        if e.button()==Qt.LeftButton:
            pp=FontPopup(self.theme,_current_font_index,self)
            pp.font_selected.connect(self.font_changed.emit)
            pp.move(self.mapToGlobal(QPoint(0,self.height()+4))); pp.show()
    def paintEvent(self,e):
        p=QPainter(self); p.setRenderHint(QPainter.Antialiasing); T=self.theme; W,H=self.width(),self.height()
        bg=QColor(T["surface"]); bg=bg.lighter(130) if self._hover else bg
        p.setPen(Qt.NoPen); p.setBrush(bg); p.drawRoundedRect(0,0,W,H,8,8)
        p.setPen(QPen(QColor(T["accent"]),1.2)); p.setBrush(Qt.NoBrush); p.drawRoundedRect(1,1,W-2,H-2,8,8)
        p.setFont(QFont(get_current_font(),14,QFont.Bold if get_font_bold() else QFont.Normal))
        p.setPen(QColor(T["accent"])); p.drawText(QRect(0,0,W,H),Qt.AlignCenter,"A")


# ═══════════════════════════════════════════════════════════
#  LANG / THEME BUTTONS
# ═══════════════════════════════════════════════════════════
def draw_flag(p, k, fx, fy, w, h):
    fr=QRect(fx,fy,w,h)
    p.save(); p.setPen(Qt.NoPen); p.setClipRect(fr)
    if k=="RU":
        p.setBrush(QColor("#FFF")); p.drawRect(fx,fy,w,5)
        p.setBrush(QColor("#0033A0")); p.drawRect(fx,fy+5,w,5)
        p.setBrush(QColor("#DA291C")); p.drawRect(fx,fy+10,w,5)
    elif k=="DE":
        p.setBrush(QColor("#000")); p.drawRect(fx,fy,w,5)
        p.setBrush(QColor("#F00")); p.drawRect(fx,fy+5,w,5)
        p.setBrush(QColor("#FFCC00")); p.drawRect(fx,fy+10,w,5)
    elif k=="UA":
        p.setBrush(QColor("#0057B7")); p.drawRect(fx,fy,w,7)
        p.setBrush(QColor("#FFDD00")); p.drawRect(fx,fy+7,w,8)
    elif k=="FR":
        p.setBrush(QColor("#002395")); p.drawRect(fx,fy,8,h)
        p.setBrush(QColor("#FFF")); p.drawRect(fx+8,fy,8,h)
        p.setBrush(QColor("#ED2939")); p.drawRect(fx+16,fy,8,h)
    elif k=="JP":
        p.setBrush(QColor("#FFF")); p.drawRect(fr)
        p.setBrush(QColor("#BC002D")); p.drawEllipse(QPointF(fx+w/2,fy+h/2),4.5,4.5)
    elif k=="EN":
        p.setBrush(QColor("#012169")); p.drawRect(fr)
        p.setPen(QPen(QColor("#FFF"),2)); p.drawLine(fx,fy,fx+w,fy+h); p.drawLine(fx,fy+h,fx+w,fy)
        p.setPen(QPen(QColor("#C8102E"),1)); p.drawLine(fx,fy,fx+w,fy+h); p.drawLine(fx,fy+h,fx+w,fy)
        p.setPen(Qt.NoPen); p.setBrush(QColor("#FFF"))
        p.drawRect(fx+w//2-2,fy,4,h); p.drawRect(fx,fy+h//2-2,w,4)
        p.setBrush(QColor("#C8102E"))
        p.drawRect(fx+w//2-1,fy,2,h); p.drawRect(fx,fy+h//2-1,w,2)
    elif k=="IT":
        p.setBrush(QColor("#009246")); p.drawRect(fx,fy,8,h)
        p.setBrush(QColor("#FFF")); p.drawRect(fx+8,fy,8,h)
        p.setBrush(QColor("#CE2B37")); p.drawRect(fx+16,fy,8,h)
    elif k=="PT":
        p.setBrush(QColor("#006600")); p.drawRect(fx,fy,10,h)
        p.setBrush(QColor("#FF0000")); p.drawRect(fx+10,fy,14,h)
        p.setBrush(QColor("#FFDE00")); p.drawEllipse(QPointF(fx+10,fy+h/2),4,4)
    elif k=="ES":
        p.setBrush(QColor("#AA151B")); p.drawRect(fx,fy,w,4)
        p.setBrush(QColor("#F1BF00")); p.drawRect(fx,fy+4,w,7)
        p.setBrush(QColor("#AA151B")); p.drawRect(fx,fy+11,w,4)
        p.setBrush(QColor("#AA151B")); p.drawRect(fx+4,fy+h//2-2,3,4)
    elif k=="PL":
        p.setBrush(QColor("#FFF")); p.drawRect(fx,fy,w,h//2)
        p.setBrush(QColor("#DC143C")); p.drawRect(fx,fy+h//2,w,h-h//2)
    elif k=="CS":
        p.setBrush(QColor("#FFF")); p.drawRect(fx,fy,w,h//2)
        p.setBrush(QColor("#D7141A")); p.drawRect(fx,fy+h//2,w,h-h//2)
        p.setBrush(QColor("#11457E")); p.drawPolygon(QPolygon([QPoint(fx,fy),QPoint(fx+12,fy+h//2),QPoint(fx,fy+h)]))
    elif k=="SV":
        p.setBrush(QColor("#006AA7")); p.drawRect(fr)
        p.setBrush(QColor("#FECC02")); p.drawRect(fx+7,fy,3,h); p.drawRect(fx,fy+6,w,3)
    p.restore(); p.setClipping(False)
    p.setPen(QPen(QColor(0,0,0,50),1)); p.setBrush(Qt.NoBrush); p.drawRect(fr)

class LangPopup(AnimatedPopupMixin,QFrame):
    lang_selected=Signal(str)
    def __init__(self,theme,cur,parent=None):
        QFrame.__init__(self,parent,Qt.Popup|Qt.FramelessWindowHint)
        self.theme=theme; self.cur=cur; self._hov=None; ih=28
        self.setFixedSize(140,len(LANG_ORDER)*ih+8); self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True); self._ih=ih; self._init_popup_anim()
    def paintEvent(self,e):
        p=QPainter(self); p.setRenderHint(QPainter.Antialiasing); T=self.theme; W,H=self.width(),self.height()
        p.setPen(Qt.NoPen); p.setBrush(QColor(T["surface"])); p.drawRoundedRect(0,0,W,H,10,10)
        p.setPen(QPen(QColor(T["accent"]),1)); p.setBrush(Qt.NoBrush); p.drawRoundedRect(1,1,W-2,H-2,10,10)
        for i,k in enumerate(LANG_ORDER):
            y=4+i*self._ih; ic=(k==self.cur); ih=(self._hov==i)
            p.setPen(Qt.NoPen)
            if ic: p.setBrush(QColor(T["accent"])); p.drawRoundedRect(4,y,W-8,self._ih-2,5,5); p.setPen(QColor(T["bg"]))
            elif ih: p.setBrush(QColor(T["hover"])); p.drawRoundedRect(4,y,W-8,self._ih-2,5,5); p.setPen(QColor(T["text"]))
            else: p.setPen(QColor(T["text2"]))
            draw_flag(p, k, 12, y + (self._ih-2 - 15)//2, 24, 15)
            p.setFont(QFont(get_current_font(),9,QFont.Bold if ic else QFont.Normal))
            p.drawText(QRect(44,y,W-56,self._ih-2),Qt.AlignVCenter|Qt.AlignLeft,k)
    def mouseMoveEvent(self,e): i=(e.y()-4)//self._ih; self._hov=i if 0<=i<len(LANG_ORDER) else None; self.update()
    def mousePressEvent(self,e):
        i=(e.y()-4)//self._ih
        if 0<=i<len(LANG_ORDER): self.lang_selected.emit(LANG_ORDER[i])
        self.close()
    def leaveEvent(self,e): self._hov=None; self.update()

class LangButton(QWidget):
    lang_changed=Signal(str)
    def __init__(self,theme,lang_key,parent=None):
        super().__init__(parent); self.theme=theme; self.lang_key=lang_key; self._hover=False
        self.setFixedSize(40,36); self.setCursor(Qt.PointingHandCursor)
    def set_theme(self,t): self.theme=t; self.update()
    def enterEvent(self,e): self._hover=True;  self.update()
    def leaveEvent(self,e): self._hover=False; self.update()
    def mousePressEvent(self,e):
        if e.button()==Qt.LeftButton:
            pp=LangPopup(self.theme,self.lang_key,self)
            pp.lang_selected.connect(self.lang_changed.emit)
            pp.move(self.mapToGlobal(QPoint(0,self.height()+4))); pp.show()
    def paintEvent(self,e):
        p=QPainter(self); p.setRenderHint(QPainter.Antialiasing); T=self.theme; W,H=self.width(),self.height()
        bg=QColor(T["surface"]); bg=bg.lighter(130) if self._hover else bg
        p.setPen(Qt.NoPen); p.setBrush(bg); p.drawRoundedRect(0,0,W,H,8,8)
        p.setPen(QPen(QColor(T["accent"]),1.2)); p.setBrush(Qt.NoBrush); p.drawRoundedRect(1,1,W-2,H-2,8,8)
        w,h=24,15; fx=(W-w)//2; fy=(H-h)//2
        draw_flag(p, self.lang_key, fx, fy, w, h)

class ThemeButton(QWidget):
    def __init__(self,theme,theme_key,parent=None):
        super().__init__(parent); self.theme=theme; self.theme_key=theme_key; self._hover=False; self._cbs=[]
        self.setFixedSize(48,36); self.setCursor(Qt.PointingHandCursor)
    def set_theme(self,t,k): self.theme=t; self.theme_key=k; self.update()
    def connect(self,cb): self._cbs.append(cb)
    def enterEvent(self,e): self._hover=True;  self.update()
    def leaveEvent(self,e): self._hover=False; self.update()
    def mousePressEvent(self,e):
        if e.button()==Qt.LeftButton: [cb() for cb in self._cbs]
    def paintEvent(self,e):
        p=QPainter(self); p.setRenderHint(QPainter.Antialiasing); T=self.theme; W,H=self.width(),self.height()
        bg=QColor(T["surface"]); bg=bg.lighter(130) if self._hover else bg
        p.setPen(Qt.NoPen); p.setBrush(bg); p.drawRoundedRect(0,0,W,H,8,8)
        p.setPen(QPen(QColor(T["accent"]),1.2)); p.setBrush(Qt.NoBrush); p.drawRoundedRect(1,1,W-2,H-2,8,8)
        keys=list(THEMES.keys()); r=3; sx=6
        step=(W-12)/max(1,len(keys)-1)
        for i,k in enumerate(keys):
            p.setPen(QPen(QColor(T["text"]),1.5) if k==self.theme_key else Qt.NoPen)
            p.setBrush(QColor(THEMES[k]["accent"])); p.drawEllipse(QPointF(sx+i*step,H/2),r,r)

class ThemeModePopup(AnimatedPopupMixin,QFrame):
    def __init__(self,theme,lang,parent=None):
        QFrame.__init__(self,parent,Qt.Popup|Qt.FramelessWindowHint)
        self.theme=theme; self.lang=lang; self._hov=None; self._cbs={"dark":[],"light":[]}
        self.setAttribute(Qt.WA_TranslucentBackground); self.setMouseTracking(True); self.setFixedSize(180,84)
        self._init_popup_anim()
    def connect_dark(self,cb): self._cbs["dark"].append(cb)
    def connect_light(self,cb): self._cbs["light"].append(cb)
    def _ir(self,i): return QRect(6,6+i*36,self.width()-12,30)
    def _ia(self,pos):
        for i,k in enumerate(("dark","light")):
            if self._ir(i).contains(pos): return k
        return None
    def mouseMoveEvent(self,e): self._hov=self._ia(e.pos()); self.update()
    def leaveEvent(self,e): self._hov=None; self.update()
    def mousePressEvent(self,e):
        if e.button()!=Qt.LeftButton: self.close(); return
        k=self._ia(e.pos())
        if k: [cb() for cb in self._cbs[k]]
        self.close()
    def paintEvent(self,e):
        p=QPainter(self); p.setRenderHint(QPainter.Antialiasing); T=self.theme
        p.setPen(Qt.NoPen); p.setBrush(QColor(T["surface"])); p.drawRoundedRect(self.rect(),10,10)
        p.setPen(QPen(QColor(T["accent"]),1.2)); p.setBrush(Qt.NoBrush); p.drawRoundedRect(self.rect().adjusted(0,0,-1,-1),10,10)
        for i,(key,text,ic) in enumerate([("dark",self.lang["dark_themes"],QColor("#2B2B35")),("light",self.lang["light_themes"],QColor("#FFF4D6"))]):
            rect=self._ir(i); hv=self._hov==key
            p.setPen(Qt.NoPen); p.setBrush(QColor(T["hover"] if hv else T["surface"])); p.drawRoundedRect(rect,8,8)
            p.setBrush(ic); p.drawEllipse(rect.left()+10,rect.center().y()-6,12,12)
            wt=QFont.Bold if get_font_bold() else QFont.Normal
            p.setPen(QColor(T["text"])); p.setFont(QFont(get_current_font(),9,wt))
            p.drawText(rect.adjusted(32,0,-8,0),Qt.AlignVCenter|Qt.AlignLeft,text)


# ═══════════════════════════════════════════════════════════
#  TITLE BAR LOGO + TITLE BAR
# ═══════════════════════════════════════════════════════════
class TitleBarLogo(QLabel):
    def __init__(self,theme,parent=None):
        super().__init__(parent); self.theme=theme; self._W,self._H=220,40
        self.setFixedSize(self._W,self._H); self._render(theme)
    def _render(self,theme):
        T=theme; W,H=self._W,self._H
        pix=QPixmap(W,H); pix.fill(Qt.transparent)
        p=QPainter(pix); p.setRenderHint(QPainter.Antialiasing)
        cx,cy=22,H//2
        p.save(); p.translate(cx,cy); p.rotate(-12); p.translate(-cx,-cy)
        p.setPen(Qt.NoPen); p.setBrush(QColor(T["accent2"]))
        path=QPainterPath()
        path.moveTo(cx+12,cy-12); path.lineTo(cx-4,cy)
        path.quadTo(cx-6,cy,cx-4,cy+2); path.lineTo(cx+12,cy+14)
        path.quadTo(cx+14,cy+14,cx+14,cy+12); path.lineTo(cx+4,cy)
        path.lineTo(cx+14,cy-12); path.quadTo(cx+14,cy-14,cx+12,cy-12)
        p.drawPath(path)
        p.setPen(QPen(QColor(T["accent"]),2)); p.setBrush(Qt.NoBrush)
        inner=QPainterPath()
        inner.moveTo(cx+6,cy-8); inner.lineTo(cx-4,cy); inner.lineTo(cx+6,cy+8)
        p.drawPath(inner); p.restore()
        p.setFont(QFont("Segoe UI",13,QFont.Bold)); p.setPen(QColor(T["text"]))
        p.drawText(QRect(42,0,W-44,H),Qt.AlignVCenter|Qt.AlignLeft,"ReturnalAudio")
        p.end(); self.setPixmap(pix)
    def set_theme(self,t): self._render(t)

class CustomTitleBar(QWidget):
    def __init__(self,theme,parent=None):
        super().__init__(parent); self.theme=theme; self.dragging=False; self.drag_pos=QPoint()
        self.setFixedHeight(44)
        lo=QHBoxLayout(self); lo.setContentsMargins(10,4,10,4); lo.setSpacing(6)
        self.logo=TitleBarLogo(theme,self); lo.addWidget(self.logo); lo.addStretch()
        for icon,slot in [("minimize",self._min),("maximize",self._max),("close",self._cls)]:
            btn=IconButton(icon,theme); btn.setFixedSize(32,32); btn.clicked.connect(slot)
            lo.addWidget(btn); setattr(self,f"_{icon}_btn",btn)
    def set_theme(self,t):
        self.theme=t; self.logo.set_theme(t)
        for a in ("_minimize_btn","_maximize_btn","_close_btn"): getattr(self,a).set_theme(t)
        self.update()
    def _min(self): self.window().setWindowState(Qt.WindowMinimized)
    def _max(self):
        win=self.window()
        if win.is_maximized: win._animate_geometry(win.normal_geometry,win.on_restore_finished)
        else: win.normal_geometry=win.geometry(); win._animate_geometry(QApplication.primaryScreen().availableGeometry(),win.on_maximize_finished)
    def _cls(self): self.window().close()
    def mousePressEvent(self,e):
        if e.button()==Qt.LeftButton: self.dragging=True; self.drag_pos=e.globalPos()-self.window().pos()
    def mouseMoveEvent(self,e):
        if self.dragging: self.window().move(e.globalPos()-self.drag_pos)
    def mouseReleaseEvent(self,e): self.dragging=False
    def paintEvent(self,e): pass


# ═══════════════════════════════════════════════════════════
#  TRACK ROW  (list view)
# ═══════════════════════════════════════════════════════════
class TrackRowWidget(QWidget):
    like_clicked  =Signal(str)
    delete_clicked=Signal(str)
    def __init__(self,path,label_text,is_liked,theme,parent=None):
        super().__init__(parent); self.path=path; self._liked=is_liked; self.theme=theme; self._hover=False
        self.setAttribute(Qt.WA_Hover); self.setMouseTracking(True)
        lo=QHBoxLayout(self); lo.setContentsMargins(10,2,6,2); lo.setSpacing(4)
        self.label=MarqueeLabel(label_text,theme["text"],10,get_font_bold(),self); lo.addWidget(self.label,1)
        self.btn_del=IconButton("delete",theme); self.btn_del.setFixedSize(28,28)
        self.btn_del.clicked.connect(lambda *_: self.delete_clicked.emit(self.path)); lo.addWidget(self.btn_del)
        self.btn_lk=IconButton("like",theme); self.btn_lk.setFixedSize(28,28)
        self.btn_lk.setActive(is_liked); self.btn_lk.clicked.connect(lambda *_: self.like_clicked.emit(self.path))
        lo.addWidget(self.btn_lk); self.setStyleSheet("TrackRowWidget { background: transparent; border: none; }"); self._upd_vis()
    def _upd_vis(self):
        self.btn_del.setVisible(self._hover); self.btn_lk.setVisible(self._hover or self._liked)
    def set_liked(self,v): self._liked=v; self.btn_lk.setActive(v); self._upd_vis()
    def set_theme(self,t):
        self.theme=t; self.btn_lk.set_theme(t); self.btn_del.set_theme(t)
        self.label.set_font_params(t["text"],10,get_font_bold())
    def enterEvent(self,e): self._hover=True;  self._upd_vis(); super().enterEvent(e)
    def leaveEvent(self,e): self._hover=False; self._upd_vis(); super().leaveEvent(e)


# ═══════════════════════════════════════════════════════════
#  EQ TRIANGLE POPUP  (visual EQ with volume compensation)
# ═══════════════════════════════════════════════════════════
class TriangleEQWidget(QWidget):
    eq_changed=Signal(float,float,float)   # bass, mid, treble
    def __init__(self,eq_state,theme,parent=None):
        super().__init__(parent); self.eq=eq_state; self.theme=theme
        self._drag=None; self._hover=None
        self.setFixedSize(280,210); self.setCursor(Qt.CrossCursor); self.setMouseTracking(True)
    def set_theme(self,t): self.theme=t; self.update()

    def _verts(self):
        W,H=self.width(),self.height(); pad=46
        return QPointF(W/2,pad), QPointF(pad,H-pad-25), QPointF(W-pad,H-pad-25)

    def _band_pts(self):
        top,bl,br=self._verts()
        cx=(top.x()+bl.x()+br.x())/3; cy=(top.y()+bl.y()+br.y())/3
        def lp(v,t): return QPointF(cx+(v.x()-cx)*t, cy+(v.y()-cy)*t)
        def sc(v): return 0.1+v*0.9
        bm=(self.eq.bass+12)/24; mm=(self.eq.mid+12)/24; tm=(self.eq.treble+12)/24
        return lp(bl,sc(bm)), lp(top,sc(mm)), lp(br,sc(tm))

    def _nearest(self,pos):
        _,bl,br=self._verts(); top,_bl,_br=self._verts(); verts=[bl,top,br]; R=17
        for i,v in enumerate(verts):
            if (pos.x()-v.x())**2+(pos.y()-v.y())**2<R*R: return i
        return None

    def _drag_gain(self,idx,pos):
        top,bl,br=self._verts(); verts=[bl,top,br]; v=verts[idx]
        cx=(top.x()+bl.x()+br.x())/3; cy=(top.y()+bl.y()+br.y())/3
        dx=v.x()-cx; dy=v.y()-cy; L=math.sqrt(dx*dx+dy*dy)
        if L<1: return 0.0
        px=pos.x()-cx; py=pos.y()-cy; t=(px*dx+py*dy)/(L*L); t=max(0.0,min(1.0,t))
        return max(-12.0,min(12.0,(t-0.1)/0.9*24-12))

    def mousePressEvent(self,e):
        if e.button()==Qt.LeftButton: self._drag=self._nearest(e.pos()); self.update()
    def mouseMoveEvent(self,e):
        self._hover=self._nearest(e.pos())
        if self._drag is not None:
            g=self._drag_gain(self._drag,e.pos())
            if self._drag==0: self.eq.bass=g
            elif self._drag==1: self.eq.mid=g
            else: self.eq.treble=g
            self.eq_changed.emit(self.eq.bass,self.eq.mid,self.eq.treble); self.update()
        else: self.update()
    def mouseReleaseEvent(self,e):
        if e.button()==Qt.LeftButton: self._drag=None; self.update()
    def mouseDoubleClickEvent(self,e):
        self.eq.reset(); self.eq_changed.emit(0,0,0); self.update()

    def paintEvent(self,event):
        p=QPainter(self); p.setRenderHint(QPainter.Antialiasing); T=self.theme; W,H=self.width(),self.height()
        top,bl,br=self._verts(); bp,mp,tp=self._band_pts()
        out=QPainterPath(); out.moveTo(top); out.lineTo(bl); out.lineTo(br); out.closeSubpath()
        bg=QColor(T["track_bg"]); bg.setAlpha(180); p.setPen(Qt.NoPen); p.setBrush(bg); p.drawPath(out)
        cx=(top.x()+bl.x()+br.x())/3; cy=(top.y()+bl.y()+br.y())/3
        gpen=QPen(QColor(T["text2"])); gpen.setWidthF(0.8); gpen.setStyle(Qt.DashLine); p.setPen(gpen); p.setBrush(Qt.NoBrush)
        for v in (top,bl,br): p.drawLine(QPointF(cx,cy),v)
        p.setPen(QPen(QColor(T["accent2"]),1.2)); p.drawPath(out)
        inn=QPainterPath(); inn.moveTo(bp); inn.lineTo(mp); inn.lineTo(tp); inn.closeSubpath()
        g=QLinearGradient(bl,br)
        c1=QColor(T["accent"]); c1.setAlpha(160); g.setColorAt(0,c1)
        c2=QColor(T["accent2"]); c2.setAlpha(180); g.setColorAt(1,c2)
        p.setBrush(QBrush(g)); p.setPen(Qt.NoPen); p.drawPath(inn)
        p.setPen(QPen(QColor(T["accent"]),1.5)); p.setBrush(Qt.NoBrush); p.drawPath(inn)
        labels=[("BASS",bl,self.eq.bass),("MID",top,self.eq.mid),("TREBLE",br,self.eq.treble)]
        for i,(name,v,gain) in enumerate(labels):
            r=11+(2 if self._hover==i or self._drag==i else 0)
            p.setPen(QPen(QColor(T["accent"]),2)); p.setBrush(QColor(T["surface"])); p.drawEllipse(v,r,r)
            p.setBrush(QColor(T["accent"])); p.setPen(Qt.NoPen); p.drawEllipse(v,r-4,r-4)
        
        fn=QFont(get_current_font(),8,QFont.Bold if get_font_bold() else QFont.Normal)
        for i, (name, v, gain) in enumerate(labels):
            rect = QRectF(v.x() - 30, v.y() + 14 if i != 1 else v.y() - 36, 60, 30)
            align = Qt.AlignHCenter | (Qt.AlignTop if i != 1 else Qt.AlignBottom)
            p.setFont(fn); p.setPen(QColor(T["text"]))
            p.drawText(rect, align, name)
            p.setPen(QColor(T["accent"])); p.setFont(QFont(get_current_font(), 7))
            val_rect = QRectF(rect.x(), rect.y() + (12 if i != 1 else -12), rect.width(), 30)
            p.drawText(val_rect, align, f"{gain:+.1f}dB")
            
        p.setFont(QFont(get_current_font(),7)); p.setPen(QColor(T["text2"]))
        p.drawText(QRect(0,H-16,W,14),Qt.AlignCenter,"double-click to reset")

class EQPopup(QFrame):
    eq_changed=Signal(float,float,float)
    def __init__(self,eq_state,theme,parent=None):
        super().__init__(parent,Qt.Popup|Qt.FramelessWindowHint)
        self.theme=theme; self.setAttribute(Qt.WA_TranslucentBackground); self.setFixedSize(300,230)
        lo=QVBoxLayout(self); lo.setContentsMargins(10,10,10,10); lo.setSpacing(4)
        self.eq_w=TriangleEQWidget(eq_state,theme,self)
        self.eq_w.eq_changed.connect(self.eq_changed.emit); lo.addWidget(self.eq_w)
        self.setWindowOpacity(0.0)
        self._an=QPropertyAnimation(self,b"windowOpacity"); self._an.setDuration(180); self._an.setEasingCurve(QEasingCurve.OutCubic)
    def showEvent(self,e):
        super().showEvent(e); self._an.stop(); self._an.setStartValue(0.0); self._an.setEndValue(1.0); self._an.start()
    def set_theme(self,t): self.theme=t; self.eq_w.set_theme(t); self.update()
    def paintEvent(self,e):
        p=QPainter(self); p.setRenderHint(QPainter.Antialiasing); T=self.theme; W,H=self.width(),self.height()
        p.setPen(Qt.NoPen); p.setBrush(QColor(T["surface"])); p.drawRoundedRect(0,0,W,H,12,12)
        p.setPen(QPen(QColor(T["accent"]),1.5)); p.setBrush(Qt.NoBrush); p.drawRoundedRect(1,1,W-2,H-2,12,12)


# ═══════════════════════════════════════════════════════════
#  MINI PLAYER  — Spotify fix: background thread + main-thread UI update
# ═══════════════════════════════════════════════════════════
class MiniPlayer(QWidget):
    closed=Signal()
    MIN_W=200; MAX_W=500; DEFAULT_W=290

    def __init__(self,theme,player,play_pause_cb,next_cb,prev_cb,parent=None):
        super().__init__(parent,Qt.FramelessWindowHint|Qt.WindowStaysOnTopHint|Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground); self.setAttribute(Qt.WA_NoSystemBackground)
        self.theme=theme; self.player=player
        self._drag=False; self._drag_pos=QPoint(); self._opacity=0.82
        self._mini_w=self.DEFAULT_W
        self._play_pause_cb=play_pause_cb; self._next_cb=next_cb; self._prev_cb=prev_cb
        self._playing=False; self._title="—"

        # Spotify state
        self._sp=None; self._sp_mode=False
        self._sp_track=""; self._sp_playing=False

        self.setFixedSize(self._mini_w,72); self.setWindowTitle("ReturnalAudio Mini")
        lo=QHBoxLayout(self); lo.setContentsMargins(10,8,10,8); lo.setSpacing(6)
        self.prev_btn=IconButton("prev",theme); self.prev_btn.setFixedSize(34,34); self.prev_btn.clicked.connect(self._on_prev)
        self.pp_btn=PlayPauseButton(theme); self.pp_btn.setFixedSize(40,40); self.pp_btn.clicked=self._on_pp
        self.next_btn=IconButton("next",theme); self.next_btn.setFixedSize(34,34); self.next_btn.clicked.connect(self._on_next)
        self.title_lbl=MarqueeLabel("—",theme["text"],9,True,self)
        self.title_lbl.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Preferred)
        close_btn=IconButton("close",theme); close_btn.setFixedSize(24,24); close_btn.clicked.connect(self._on_close)
        lo.addWidget(self.prev_btn); lo.addWidget(self.pp_btn); lo.addWidget(self.next_btn)
        lo.addWidget(self.title_lbl,1); lo.addWidget(close_btn)

        # Spotify poll timer (UI thread, lightweight — actual HTTP in bg thread)
        self._sp_timer=QTimer(self); self._sp_timer.setInterval(2500); self._sp_timer.timeout.connect(self._sp_kick)

    # ── Spotify ──
    def enable_spotify(self,sp_client):
        self._sp=sp_client; self._sp_mode=True; self._sp_track=""; self._sp_timer.start(); self._sp_kick()
    def disable_spotify(self):
        self._sp=None; self._sp_mode=False; self._sp_timer.stop(); self._sp_track=""
        self.title_lbl.set_text(self._title); self.pp_btn.set_playing(self._playing)
    def _sp_kick(self):
        """Fire background thread; result lands on main thread via QTimer.singleShot."""
        if not self._sp or not self._sp_mode: return
        sp=self._sp
        def _run():
            try:
                pb=sp.current_playback()
                if pb and pb.get("item"):
                    item=pb["item"]; playing=pb.get("is_playing",False)
                    artist=", ".join(a["name"] for a in item.get("artists",[]))
                    title=item.get("name","")
                    track=f"{artist} — {title}" if artist else title
                    QTimer.singleShot(0,lambda: self._sp_update(track,playing))
            except Exception as ex:
                print(f"[Spotify mini poll] {ex}")
        threading.Thread(target=_run,daemon=True).start()
    def _sp_update(self,track,playing):
        if not self._sp_mode: return
        if track!=self._sp_track:
            self._sp_track=track; self.title_lbl.set_text(f"♫ {track}")
        if playing!=self._sp_playing:
            self._sp_playing=playing; self.pp_btn.set_playing(playing)

    def _on_pp(self):
        if self._sp_mode and self._sp:
            def _run():
                try:
                    pb=self._sp.current_playback()
                    if pb and pb.get("is_playing"): self._sp.pause_playback()
                    else: self._sp.start_playback()
                    QTimer.singleShot(600,self._sp_kick)
                except Exception: pass
            threading.Thread(target=_run,daemon=True).start()
        else: self._play_pause_cb()
    def _on_next(self):
        if self._sp_mode and self._sp:
            def _run():
                try: self._sp.next_track(); QTimer.singleShot(700,self._sp_kick)
                except Exception: pass
            threading.Thread(target=_run,daemon=True).start()
        else: self._next_cb()
    def _on_prev(self):
        if self._sp_mode and self._sp:
            def _run():
                try: self._sp.previous_track(); QTimer.singleShot(700,self._sp_kick)
                except Exception: pass
            threading.Thread(target=_run,daemon=True).start()
        else: self._prev_cb()

    def set_theme(self,t):
        self.theme=t; self.title_lbl.set_font_params(t["text"],9,True)
        for btn in (self.prev_btn,self.next_btn,self.pp_btn):
            if hasattr(btn,"set_theme"): btn.set_theme(t)
        self.update()
    def set_title(self,text):
        self._title=text
        if not self._sp_mode: self.title_lbl.set_text(text)
    def set_playing(self,pl):
        self._playing=pl
        if not self._sp_mode: self.pp_btn.set_playing(pl)
    def widen(self): self._mini_w=min(self.MAX_W,self._mini_w+40); self.setFixedSize(self._mini_w,72)
    def narrow(self): self._mini_w=max(self.MIN_W,self._mini_w-40); self.setFixedSize(self._mini_w,72)
    def _on_close(self): self.hide(); self.closed.emit()

    def paintEvent(self,e):
        p=QPainter(self); p.setRenderHint(QPainter.Antialiasing); T=self.theme; W,H=self.width(),self.height()
        sh=QColor(T["accent"]); sh.setAlpha(int(40*self._opacity)); p.setPen(Qt.NoPen); p.setBrush(sh); p.drawRoundedRect(2,2,W-4,H-4,14,14)
        bg=QColor(T["surface"]); bg.setAlpha(int(210*self._opacity)); p.setBrush(bg); p.drawRoundedRect(0,0,W,H,12,12)
        hl=QColor(255,255,255); hl.setAlpha(int(25*self._opacity)); p.setBrush(hl); p.drawRoundedRect(0,0,W,H//2,12,12)
        br_c=QColor(T["accent"]); br_c.setAlpha(int(180*self._opacity))
        p.setPen(QPen(br_c,1.5)); p.setBrush(Qt.NoBrush); p.drawRoundedRect(1,1,W-2,H-2,12,12)
        al=QColor(T["accent2"]); al.setAlpha(int(120*self._opacity))
        p.setPen(QPen(al,1.5)); p.drawLine(16,1,W-16,1)
        if self._sp_mode:
            p.setPen(Qt.NoPen); p.setBrush(QColor("#1DB954")); p.drawEllipse(QPoint(W-10,8),5,5)
    def wheelEvent(self,e):
        self._opacity=max(0.3,min(1.0,self._opacity+(0.05 if e.angleDelta().y()>0 else -0.05))); self.update()
    def mousePressEvent(self,e):
        if e.button()==Qt.LeftButton: self._drag=True; self._drag_pos=e.globalPos()-self.pos()
    def mouseMoveEvent(self,e):
        if self._drag: self.move(e.globalPos()-self._drag_pos)
    def mouseReleaseEvent(self,e): self._drag=False


# ═══════════════════════════════════════════════════════════
#  MINI BUTTON
# ═══════════════════════════════════════════════════════════
class MiniButton(QWidget):
    toggled=Signal()
    def __init__(self,theme,parent=None):
        super().__init__(parent); self.theme=theme; self._active=False; self._hover=False
        self._mini_ref=None; self._hover_expanded=False
        self._WN=42; self._WE=42+26+26+8
        self._cur_w=self._WN
        self._wa=QPropertyAnimation(self,b"_cw"); self._wa.setDuration(160); self._wa.setEasingCurve(QEasingCurve.OutCubic)
        self.setFixedHeight(36); self.setFixedWidth(self._WN)
        self.setCursor(Qt.PointingHandCursor); self.setMouseTracking(True)
    @pyqtProperty(int)
    def _cw(self): return self._cur_w
    @_cw.setter
    def _cw(self,v): self._cur_w=v; self.setFixedWidth(v); self.update()
    def set_theme(self,t): self.theme=t; self.update()
    def setActive(self,a): self._active=a; self.update()
    def _cr(self):
        if not self._hover_expanded: return QRect(0,0,self._cur_w,self.height())
        return QRect((self._cur_w-self._WN)//2,0,self._WN,self.height())
    def _mr(self): return QRect(2,(self.height()-24)//2,24,24)
    def _pr(self): return QRect(self._cur_w-26,(self.height()-24)//2,24,24)
    def enterEvent(self,e):
        self._hover=True; self._hover_expanded=True
        self._wa.stop(); self._wa.setStartValue(self._cur_w); self._wa.setEndValue(self._WE); self._wa.start(); self.update()
    def leaveEvent(self,e):
        self._hover=False; self._hover_expanded=False
        self._wa.stop(); self._wa.setStartValue(self._cur_w); self._wa.setEndValue(self._WN); self._wa.start(); self.update()
    def mousePressEvent(self,e):
        if e.button()!=Qt.LeftButton: return
        if self._hover_expanded:
            if self._mr().contains(e.pos()):
                if self._mini_ref: self._mini_ref.narrow(); return
            if self._pr().contains(e.pos()):
                if self._mini_ref: self._mini_ref.widen(); return
        self.toggled.emit()
    def paintEvent(self,e):
        p=QPainter(self); p.setRenderHint(QPainter.Antialiasing); T=self.theme; W,H=self._cur_w,self.height()
        if self._hover_expanded and W>self._WN+10:
            mr=self._mr(); p.setPen(Qt.NoPen); p.setBrush(QColor(T["hover"])); p.drawRoundedRect(mr,5,5)
            p.setPen(QPen(QColor(T["accent"]),2.5,Qt.SolidLine,Qt.RoundCap)); my=mr.center().y()
            p.drawLine(mr.left()+5,my,mr.right()-5,my)
        cr=self._cr(); cx=cr.x(); cw=cr.width()
        bg=QColor(T["surface"]); bg=bg.lighter(130) if self._hover else bg
        p.setPen(Qt.NoPen); p.setBrush(bg); p.drawRoundedRect(cx,0,cw,H,8,8)
        p.setPen(QPen(QColor(T["accent"]),1.5)); p.setBrush(Qt.NoBrush); p.drawRoundedRect(cx+1,1,cw-2,H-2,8,8)
        ic=QColor(T["accent"] if self._active else T["text"])
        p.setPen(QPen(ic,2,Qt.SolidLine,Qt.RoundCap)); p.setBrush(Qt.NoBrush)
        ix=cx+int(cw*.15); iy=int(H*.3); iw=int(cw*.7); ih2=int(H*.4); p.drawRoundedRect(ix,iy,iw,ih2,3,3)
        p.setBrush(ic); p.setPen(Qt.NoPen)
        p.drawRect(cx+int(cw*.35),int(H*.42),int(cw*.08),int(H*.16))
        p.drawPolygon(QPolygon([QPoint(cx+int(cw*.5),int(H*.38)),QPoint(cx+int(cw*.5),int(H*.62)),QPoint(cx+int(cw*.7),int(H*.5))]))
        if self._hover_expanded and W>self._WN+10:
            pr=self._pr(); p.setPen(Qt.NoPen); p.setBrush(QColor(T["hover"])); p.drawRoundedRect(pr,5,5)
            p.setPen(QPen(QColor(T["accent"]),2.5,Qt.SolidLine,Qt.RoundCap))
            py2=pr.center().y(); px2=pr.center().x()
            p.drawLine(pr.left()+5,py2,pr.right()-5,py2); p.drawLine(px2,pr.top()+5,px2,pr.bottom()-5)


# ═══════════════════════════════════════════════════════════
#  COLLAPSIBLE BAR
# ═══════════════════════════════════════════════════════════
class CollapsibleBar(QWidget):
    def __init__(self,theme,parent=None):
        super().__init__(parent); self.theme=theme; self._expanded=True; self._anim=None
        self._content=QWidget(self)
        self._lo=QHBoxLayout(self._content); self._lo.setContentsMargins(0,0,0,0); self._lo.setSpacing(6)
        self._fh=42; self.setFixedHeight(self._fh); self._content.setFixedHeight(self._fh); self._content.move(0,0)
    def add_widget(self,w,*a,**kw): self._lo.addWidget(w,*a,**kw)
    def add_spacing(self,n): self._lo.addSpacing(n)
    def add_stretch(self): self._lo.addStretch()
    def clear(self):
        while self._lo.count():
            item=self._lo.takeAt(0)
            if item.widget(): item.widget().setParent(None)
    def toggle(self):
        th=0 if self._expanded else self._fh
        if self._anim: self._anim.stop()
        an=QPropertyAnimation(self,b"maximumHeight"); an.setDuration(250)
        an.setEasingCurve(QEasingCurve.InOutCubic); an.setStartValue(self.height()); an.setEndValue(th)
        an.finished.connect(self._done); an.start(); self._anim=an; self._expanded=not self._expanded
    def _done(self):
        if not self._expanded: self.setFixedHeight(0)
        else: self.setFixedHeight(self._fh)
    def is_expanded(self): return self._expanded
    def resizeEvent(self,e): super().resizeEvent(e); self._content.setFixedWidth(self.width())


# ═══════════════════════════════════════════════════════════
#  DISCORD RPC
# ═══════════════════════════════════════════════════════════
class DiscordRPC:
    def __init__(self): self._rpc=None; self._ok=False; self._en=False
    def enable(self):
        if not DISCORD_OK: return False
        try: self._rpc=Presence(DISCORD_CLIENT_ID); self._rpc.connect(); self._ok=True; self._en=True; return True
        except Exception: self._ok=False; return False
    def disable(self):
        self._en=False
        try:
            if self._rpc and self._ok: self._rpc.close()
        except Exception: pass
        self._rpc=None; self._ok=False
    def update(self,track,state="Listening"):
        if not self._en or not self._ok: return
        try: self._rpc.update(details=track[:128] if track else "Idle",state=state,large_image="music",large_text="ReturnalAudio",start=int(datetime.datetime.now().timestamp()))
        except Exception: pass
    def clear(self):
        if not self._en or not self._ok: return
        try: self._rpc.clear()
        except Exception: pass
    @property
    def is_enabled(self): return self._en


class SettingsDialog(QDialog):
    controls_in_settings_changed=Signal(bool)
    dynamic_bg_changed=Signal(bool)

    def __init__(self, theme, parent=None):
        super().__init__(parent, Qt.Dialog | Qt.FramelessWindowHint)
        self.theme=theme
        self.lang_key="EN"
        self.setModal(False)
        self.setObjectName("settingsDialog")
        self.setFixedSize(470, 580)

        lo=QVBoxLayout(self); lo.setContentsMargins(14,14,14,14); lo.setSpacing(10)

        head=QHBoxLayout(); head.setContentsMargins(0,0,0,0)
        self.title_lbl=QLabel("Settings")
        self.title_lbl.setObjectName("settingsTitle")
        head.addWidget(self.title_lbl)
        head.addStretch()
        self.close_btn=IconButton("close", theme)
        self.close_btn.setFixedSize(30,30)
        self.close_btn.clicked.connect(self.hide)
        head.addWidget(self.close_btn)
        lo.addLayout(head)

        self.dynamic_bg_check=QCheckBox("Dynamic animated background")
        self.dynamic_bg_check.setChecked(True)
        self.dynamic_bg_check.toggled.connect(self.dynamic_bg_changed.emit)
        lo.addWidget(self.dynamic_bg_check)

        self.move_top_check=QCheckBox("Move top panel buttons into settings")
        self.move_top_check.toggled.connect(self.controls_in_settings_changed.emit)
        lo.addWidget(self.move_top_check)

        self.info_lbl=QLabel("When enabled, the top panel buttons live here instead of the toolbar.")
        self.info_lbl.setWordWrap(True)
        lo.addWidget(self.info_lbl)

        self.empty_lbl=QLabel("Buttons are currently shown in the top panel.")
        self.empty_lbl.setAlignment(Qt.AlignCenter)
        self.empty_lbl.setMinimumHeight(120)
        lo.addWidget(self.empty_lbl)

        self.host=QScrollArea()
        self.host.setWidgetResizable(True)
        self.host.setFrameShape(QFrame.NoFrame)
        self.host_body=QWidget()
        self.host_lo=QVBoxLayout(self.host_body)
        self.host_lo.setContentsMargins(0,0,0,0)
        self.host_lo.setSpacing(10)
        self.host.setWidget(self.host_body)
        lo.addWidget(self.host, 1)

        self.shortcuts_card=QFrame()
        self.shortcuts_lo=QVBoxLayout(self.shortcuts_card)
        self.shortcuts_lo.setContentsMargins(12,10,12,12)
        self.shortcuts_lo.setSpacing(8)
        self.shortcuts_title=QLabel("Keyboard Shortcuts")
        self.shortcuts_title.setObjectName("settingsSection")
        self.shortcuts_lo.addWidget(self.shortcuts_title)
        self.shortcuts_view=QTextEdit()
        self.shortcuts_view.setReadOnly(True)
        self.shortcuts_view.setMinimumHeight(220)
        self.shortcuts_lo.addWidget(self.shortcuts_view)
        lo.addWidget(self.shortcuts_card)
        self._shortcut_items=[]

        self.set_theme(theme)
        self.set_language("EN")
        self.set_controls_visible(False)

    def set_theme(self, theme):
        self.theme=theme
        self.close_btn.set_theme(theme)
        ff=get_current_font(); fw="bold" if get_font_bold() else "normal"
        self.setStyleSheet(
            f"QDialog#settingsDialog{{background:{theme['surface']};border:1px solid {theme['track_bg']};border-radius:12px;}}"
            f"QLabel#settingsTitle{{color:{theme['text']};font-size:16px;font-family:{ff};font-weight:{fw};}}"
            f"QLabel#settingsSection{{color:{theme['accent']};font-size:12px;font-family:{ff};font-weight:{fw};}}"
            f"QLabel{{color:{theme['text2']};font-family:{ff};font-size:12px;background:transparent;}}"
            f"QFrame{{background:{theme['bg']};border:1px solid {theme['track_bg']};border-radius:10px;}}"
            f"QScrollArea{{background:transparent;border:none;}}"
            f"QScrollArea>QWidget>QWidget{{background:transparent;}}"
            f"QTextEdit{{background:{theme['surface']};color:{theme['text']};border:1px solid {theme['track_bg']};border-radius:8px;padding:6px;font-family:{ff};font-size:12px;}}"
            f"QCheckBox{{color:{theme['text']};font-family:{ff};font-size:13px;font-weight:{fw};spacing:8px;}}"
            f"QCheckBox::indicator{{width:16px;height:16px;border-radius:4px;border:1px solid {theme['track_bg']};background:{theme['bg']};}}"
            f"QCheckBox::indicator:checked{{background:{theme['accent']};border-color:{theme['accent']};}}"
        )
        self.host.viewport().setStyleSheet("background:transparent;")
        self.host_body.setStyleSheet("background:transparent;")
        apply_smooth_scrollbar(self.host, theme)
        apply_smooth_scrollbar(self.shortcuts_view, theme)
        if self._shortcut_items:
            self._render_shortcuts()

    def set_dynamic_bg(self, enabled):
        self.dynamic_bg_check.blockSignals(True)
        self.dynamic_bg_check.setChecked(enabled)
        self.dynamic_bg_check.blockSignals(False)

    def set_controls_in_settings(self, enabled):
        self.move_top_check.blockSignals(True)
        self.move_top_check.setChecked(enabled)
        self.move_top_check.blockSignals(False)
        self.set_controls_visible(enabled)

    def set_language(self, lang_key):
        self.lang_key=lang_key
        txt=get_settings_texts(lang_key)
        self.title_lbl.setText(txt["title"])
        self.dynamic_bg_check.setText(txt.get("dynamic_bg_check","Dynamic animated background"))
        self.move_top_check.setText(txt["move_top_check"])
        self.info_lbl.setText(txt["move_top_info"])
        self.empty_lbl.setText(txt["buttons_on_top"])
        self.shortcuts_title.setText(txt["shortcuts_title"])

    def set_controls_visible(self, visible):
        self.host.setVisible(visible)
        self.empty_lbl.setVisible(not visible)

    def set_shortcuts(self, items):
        self._shortcut_items=list(items)
        self._render_shortcuts()

    def _render_shortcuts(self):
        ff=get_current_font()
        T=self.theme
        rows=[]
        for title,key in self._shortcut_items:
            rows.append(
                f"<tr>"
                f"<td style='padding:8px 10px;color:{T['text']};border-bottom:1px solid {T['track_bg']};'>{title}</td>"
                f"<td align='right' style='padding:8px 10px;border-bottom:1px solid {T['track_bg']};'>"
                f"<span style='color:{T['accent']};background:{T['bg']};border:1px solid {T['track_bg']};"
                f"border-radius:6px;padding:4px 10px;font-weight:600;'>{key}</span></td>"
                f"</tr>"
            )
        html=(
            f"<html><body style='margin:0;font-family:{ff};'>"
            f"<table cellspacing='0' cellpadding='0' width='100%'>"
            f"{''.join(rows)}"
            f"</table></body></html>"
        )
        self.shortcuts_view.setHtml(html)

    def _detach_preserved_widgets(self, root, keep_widgets):
        if root in keep_widgets:
            root.setParent(None)
            return
        for child in root.findChildren(QWidget):
            if child in keep_widgets:
                child.setParent(None)

    def set_controls(self, sections):
        keep_widgets={w for _title, widgets in sections for w in widgets}
        while self.host_lo.count():
            item=self.host_lo.takeAt(0)
            if item.widget():
                self._detach_preserved_widgets(item.widget(), keep_widgets)
                item.widget().deleteLater()
            elif item.layout():
                while item.layout().count():
                    sub=item.layout().takeAt(0)
                    if sub.widget():
                        if sub.widget() in keep_widgets: sub.widget().setParent(None)
                        else: sub.widget().deleteLater()
        for title,widgets in sections:
            if not widgets:
                continue
            card=QFrame()
            card_lo=QVBoxLayout(card); card_lo.setContentsMargins(12,10,12,12); card_lo.setSpacing(8)
            title_lbl=QLabel(title); title_lbl.setObjectName("settingsSection"); card_lo.addWidget(title_lbl)
            grid=QGridLayout(); grid.setHorizontalSpacing(8); grid.setVerticalSpacing(8)
            for i,w in enumerate(widgets):
                grid.addWidget(w, i//4, i%4)
                w.show()
            card_lo.addLayout(grid)
            self.host_lo.addWidget(card)
        self.host_lo.addStretch(1)


# ═══════════════════════════════════════════════════════════
#  EULA WINDOW
# ═══════════════════════════════════════════════════════════
class EulaWindow(QWidget):
    def __init__(self,theme,lang_key,parent=None):
        super().__init__(parent,Qt.Dialog|Qt.FramelessWindowHint)
        self.theme=theme; self.lang_key=lang_key; self.dragging=False; self.drag_pos=QPoint()
        self.setAttribute(Qt.WA_TranslucentBackground); self.setFixedSize(460,300)
        self._eff=QGraphicsOpacityEffect(self); self.setGraphicsEffect(self._eff); self._eff.setOpacity(0)
        root=QVBoxLayout(self); root.setContentsMargins(0,0,0,0)
        self._c=QWidget(self); self._c.setObjectName("eulaC")
        lo=QVBoxLayout(self._c); lo.setContentsMargins(24,18,24,20); lo.setSpacing(12); root.addWidget(self._c)
        tr=QHBoxLayout()
        self._tl=QLabel(); self._tl.setObjectName("eulaT"); tr.addWidget(self._tl); tr.addStretch()
        cb=QPushButton("✕"); cb.setFixedSize(28,28); cb.setObjectName("eulaX"); cb.clicked.connect(self._fo); tr.addWidget(cb); lo.addLayout(tr)
        div=QFrame(); div.setFrameShape(QFrame.HLine); div.setObjectName("eulaD"); lo.addWidget(div)
        self._cl=QLabel(); self._cl.setObjectName("eulaCp"); self._cl.setWordWrap(True); lo.addWidget(self._cl)
        lb=QPushButton("🔗  Mozilla Public License 2.0"); lb.setObjectName("eulaL"); lb.setCursor(Qt.PointingHandCursor)
        lb.clicked.connect(lambda: QDesktopServices.openUrl(QtUrl("https://www.mozilla.org/MPL/2.0/"))); lo.addWidget(lb)
        ml=QLabel("✉  lepexich@proton.me"); ml.setObjectName("eulaF"); lo.addWidget(ml); lo.addStretch()
        ok_r=QHBoxLayout(); ok_r.addStretch()
        ok=QPushButton("  OK  "); ok.setObjectName("eulaOk"); ok.setFixedHeight(32); ok.clicked.connect(self._fo); ok_r.addWidget(ok); lo.addLayout(ok_r)
        self._fa=QPropertyAnimation(self._eff,b"opacity"); self._fa.setDuration(220); self._fa.setEasingCurve(QEasingCurve.OutCubic)
        self._apply()
    def _apply(self):
        T=self.theme; ff=get_current_font()
        self._tl.setText(f"📄  {LANGUAGES.get(self.lang_key,LANGUAGES['EN']).get('license_title','License')}"); self._tl.setFont(QFont(ff,12,QFont.Bold))
        self._cl.setText("© 2026 ReturnalAudio. All rights reserved.\nMozilla Public License 2.0"); self._cl.setFont(QFont(ff,10))
        self._c.setStyleSheet(f"""
            QWidget#eulaC{{background:{T['surface']};border:1.5px solid {T['accent']};border-radius:14px;}}
            QLabel#eulaT{{color:{T['text']};background:transparent;}}QLabel#eulaCp{{color:{T['text2']};background:transparent;}}
            QLabel#eulaF{{color:{T['accent2']};background:transparent;font-size:11px;}}QFrame#eulaD{{color:{T['track_bg']};}}
            QPushButton#eulaX{{background:transparent;color:{T['text2']};border:none;font-size:14px;border-radius:6px;}}
            QPushButton#eulaX:hover{{background:#FF6B6B;color:white;}}
            QPushButton#eulaL{{background:{T['track_bg']};color:{T['accent']};border:1px solid {T['accent']};border-radius:8px;padding:8px 14px;font-size:12px;font-weight:bold;text-align:left;}}
            QPushButton#eulaL:hover{{background:{T['hover']};}}
            QPushButton#eulaOk{{background:{T['accent']};color:{T['bg']};border:none;border-radius:8px;font-weight:bold;padding:4px 16px;}}
            QPushButton#eulaOk:hover{{background:{T['accent2']};}}""")
    def set_theme(self,t): self.theme=t; self._apply()
    def set_lang(self,lk): self.lang_key=lk; self._apply()
    def _fi(self):
        self._fa.stop(); self._fa.setStartValue(self._eff.opacity()); self._fa.setEndValue(1.0)
        try: self._fa.finished.disconnect()
        except: pass
        self._fa.start()
    def _fo(self):
        self._fa.stop(); self._fa.setStartValue(self._eff.opacity()); self._fa.setEndValue(0.0)
        try: self._fa.finished.disconnect()
        except: pass
        self._fa.finished.connect(self.hide); self._fa.start()
    def show_centered(self,pr):
        self.move(pr.x()+(pr.width()-self.width())//2,pr.y()+(pr.height()-self.height())//2)
        self.show(); self.raise_(); self._fi()
    def mousePressEvent(self,e):
        if e.button()==Qt.LeftButton: self.dragging=True; self.drag_pos=e.globalPos()-self.pos()
    def mouseMoveEvent(self,e):
        if self.dragging: self.move(e.globalPos()-self.drag_pos)
    def mouseReleaseEvent(self,e): self.dragging=False
    def paintEvent(self,e): pass


FS_HINTS = {
    "RU": "Нажмите F11 для выхода",
    "EN": "Press F11 to exit",
    "DE": "F11 drücken zum Beenden",
    "UA": "Натисніть F11 для виходу",
    "JP": "F11 で終了",
    "FR": "Appuyez sur F11 pour quitter",
    "IT": "Premi F11 per uscire",
    "PT": "Pressione F11 para sair",
    "ES": "Pulsa F11 para salir",
    "PL": "Naciśnij F11 aby wyjść",
    "CS": "Stiskněte F11 pro ukončení",
    "SV": "Tryck F11 för att avsluta",
}

# ═══════════════════════════════════════════════════════════
#  DYNAMIC BACKGROUND STATE
# ═══════════════════════════════════════════════════════════
class DynamicBgOrbs:
    def __init__(self):
        import random as _rnd
        rng = _rnd.Random(7)
        self.orbs = [
            {'x': rng.uniform(0.05, 0.95), 'y': rng.uniform(0.05, 0.95),
             'dx': rng.uniform(0.0002, 0.0007) * rng.choice([-1, 1]),
             'dy': rng.uniform(0.0002, 0.0006) * rng.choice([-1, 1]),
             'r': rng.uniform(0.20, 0.38),
             'phase': rng.uniform(0, math.pi * 2),
             'ci': i % 2}
            for i in range(6)
        ]

    def tick(self):
        for o in self.orbs:
            o['x'] += o['dx']; o['y'] += o['dy']; o['phase'] += 0.008
            if o['x'] < 0: o['x'] = 0; o['dx'] = abs(o['dx'])
            if o['x'] > 1: o['x'] = 1; o['dx'] = -abs(o['dx'])
            if o['y'] < 0: o['y'] = 0; o['dy'] = abs(o['dy'])
            if o['y'] > 1: o['y'] = 1; o['dy'] = -abs(o['dy'])


# ═══════════════════════════════════════════════════════════
#  TRACK ANALYZER  (background thread, optional deps)
# ═══════════════════════════════════════════════════════════
class TrackAnalyzer(threading.Thread):
    def __init__(self, path, callback):
        super().__init__(daemon=True)
        self._path = path
        self._callback = callback

    def run(self):
        try:
            import soundfile as sf
            import numpy as np
            from scipy.signal import find_peaks
            data, sr = sf.read(self._path, always_2d=True)
            mono = data.mean(axis=1).astype(float)
            hop = max(1, int(sr / 30))
            frames = len(mono) // hop
            rms_vals = np.array([
                float(np.sqrt(np.mean(mono[i * hop:(i + 1) * hop] ** 2)))
                for i in range(frames)
            ])
            rms_times = np.arange(frames) * hop / sr
            mx = rms_vals.max()
            if mx > 0:
                rms_vals = rms_vals / mx
            min_dist = max(1, int(sr / hop * 0.30))
            threshold = float(rms_vals.mean() + rms_vals.std() * 0.5)
            peaks, _ = find_peaks(rms_vals, distance=min_dist, height=threshold)
            beat_times = rms_times[peaks]
            self._callback(rms_times, rms_vals, beat_times)
        except Exception:
            self._callback(None, None, None)


# ═══════════════════════════════════════════════════════════
#  FULLSCREEN OVERLAY  — smooth mountain wave visualizer
# ═══════════════════════════════════════════════════════════
class FullscreenOverlay(QWidget):
    def __init__(self, player, theme, track_name="", analysis=None):
        super().__init__(None, Qt.Window | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self._player = player
        self._theme = dict(theme)
        self._track_name = track_name
        self._hint = "Press F11 to exit"
        self._bg_orbs = DynamicBgOrbs()
        N = 80
        rng = random.Random(7)
        self._N = N
        # 3 wave layers: back → mid → front; each has bands + per-band phases/speeds
        layer_params = [(0.52, 1.20), (0.68, 1.00), (0.84, 0.82)]
        self._layers = []
        for scale, smult in layer_params:
            self._layers.append({
                'bands':  [rng.uniform(0.05, 0.30) for _ in range(N)],
                'phases': [rng.uniform(0, math.pi * 2) for _ in range(N)],
                'speeds': [rng.uniform(0.22, 0.85) * smult for _ in range(N)],
                'poff':   rng.uniform(0, math.pi * 2),
                'scale':  scale,
            })
        self._energy       = 0.0
        self._beat_intensity = 0.0
        self._rms_times  = None
        self._rms_vals   = None
        self._beat_times = None
        self._beat_idx   = 0
        if analysis:
            self.set_analysis(*analysis)
        self._fade_in = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self.showFullScreen()

    def set_theme(self, t):
        self._theme = dict(t); self.update()

    def set_track(self, name):
        self._track_name = name; self.update()

    def set_hint(self, text):
        self._hint = text; self.update()

    def set_analysis(self, rms_times, rms_vals, beat_times):
        self._rms_times  = rms_times
        self._rms_vals   = rms_vals
        self._beat_times = beat_times
        self._beat_idx   = 0

    def _get_energy(self):
        if self._rms_times is None:
            return 0.5 if self._player.state() == QMediaPlayer.PlayingState else 0.0
        try:
            import numpy as np
            pos_s = self._player.position() / 1000.0
            idx = int(np.searchsorted(self._rms_times, pos_s))
            idx = max(0, min(idx, len(self._rms_vals) - 1))
            return float(self._rms_vals[idx])
        except Exception:
            return 0.5

    def _tick(self):
        self._bg_orbs.tick()
        if self._fade_in < 1.0:
            self._fade_in = min(1.0, self._fade_in + 0.038)
        playing = self._player.state() == QMediaPlayer.PlayingState
        if playing:
            self._energy = self._get_energy()
            dt = 0.052 * (1.0 + self._energy * 0.9)
            for layer in self._layers:
                for i in range(self._N):
                    layer['phases'][i] += layer['speeds'][i] * dt
                    ph = layer['phases'][i] + layer['poff']
                    # bell-curve envelope: peaks in center, tapers at edges
                    t_edge = abs(i / (self._N - 1) * 2.0 - 1.0)
                    env = 1.0 - t_edge ** 1.8 * 0.55
                    v = (
                        math.sin(ph) * 0.28
                        + math.sin(ph * 1.83 + i * 0.19) * 0.18
                        + math.sin(ph * 0.47 + i * 0.07) * 0.22
                        + 0.50
                    ) * layer['scale'] * env * (0.62 + self._energy * 0.72)
                    layer['bands'][i] += (max(0.02, min(0.97, v)) - layer['bands'][i]) * 0.11
            if self._beat_times is not None:
                pos_s = self._player.position() / 1000.0
                while (self._beat_idx < len(self._beat_times)
                       and self._beat_times[self._beat_idx] <= pos_s):
                    self._beat_intensity = 1.0
                    self._beat_idx += 1
            elif self._energy > 0.72:
                self._beat_intensity = min(1.0, self._beat_intensity + 0.22)
        else:
            self._energy = 0.0
            for layer in self._layers:
                for i in range(self._N):
                    layer['phases'][i] += layer['speeds'][i] * 0.011
                    ph = layer['phases'][i] + layer['poff']
                    t_edge = abs(i / (self._N - 1) * 2.0 - 1.0)
                    env = 1.0 - t_edge ** 1.8 * 0.5
                    v = (math.sin(ph) * 0.10 + math.sin(ph * 0.5) * 0.07 + 0.13) * layer['scale'] * env
                    layer['bands'][i] += (max(0.02, min(0.22, v)) - layer['bands'][i]) * 0.05
        self._beat_intensity *= 0.88
        self.update()

    @staticmethod
    def _smooth_path(xs, ys, bot):
        path = QPainterPath()
        path.moveTo(xs[0], bot)
        path.lineTo(xs[0], ys[0])
        for i in range(1, len(xs)):
            mx = (xs[i - 1] + xs[i]) * 0.5
            my = (ys[i - 1] + ys[i]) * 0.5
            path.quadTo(xs[i - 1], ys[i - 1], mx, my)
        path.lineTo(xs[-1], ys[-1])
        path.lineTo(xs[-1], bot)
        path.closeSubpath()
        return path

    @staticmethod
    def _smooth_line(xs, ys):
        path = QPainterPath()
        path.moveTo(xs[0], ys[0])
        for i in range(1, len(xs)):
            mx = (xs[i - 1] + xs[i]) * 0.5
            my = (ys[i - 1] + ys[i]) * 0.5
            path.quadTo(xs[i - 1], ys[i - 1], mx, my)
        path.lineTo(xs[-1], ys[-1])
        return path

    def paintEvent(self, e):
        p = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        T = self._theme; W, H = self.width(), self.height()
        r = QRectF(0, 0, W, H)
        # Background
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(make_vgrad(r, T["bg"], T["bg2"])))
        p.drawRect(r)
        # Dynamic orbs
        colors = [QColor(T.get("accent", "#7F7FFF")),
                  QColor(T.get("accent2", T.get("accent", "#7F7FFF")))]
        for o in self._bg_orbs.orbs:
            ox = o['x'] * W; oy = o['y'] * H
            orb_r = o['r'] * min(W, H) * (0.88 + 0.12 * math.sin(o['phase']))
            col = colors[o['ci'] % len(colors)]
            grad = QRadialGradient(ox, oy, orb_r)
            c0 = QColor(col); c0.setAlpha(70); c1 = QColor(col); c1.setAlpha(0)
            grad.setColorAt(0, c0); grad.setColorAt(1, c1)
            p.setBrush(QBrush(grad)); p.drawEllipse(QPointF(ox, oy), orb_r, orb_r)
        p.setOpacity(self._fade_in)
        accent  = QColor(T.get("accent",  "#FF69B4"))
        accent2 = QColor(T.get("accent2", accent.name()))
        beat = self._beat_intensity
        vis_w   = W * 0.90
        vis_x   = (W - vis_w) * 0.5
        vis_bot = H * 0.82
        vis_mh  = H * 0.52
        N = self._N
        xs = [vis_x + i * vis_w / (N - 1) for i in range(N)]
        # Blend color for gradient midpoint
        mid_col = QColor(
            (accent.red()   + accent2.red())   // 2,
            (accent.green() + accent2.green()) // 2,
            (accent.blue()  + accent2.blue())  // 2,
        )
        # Layer configs: (fill_alpha, height_scale, baseline_shift)
        layer_cfgs = [
            (28,  0.68, H * 0.055),   # back — shorter, shifted down, barely visible
            (60,  0.84, H * 0.025),   # mid
            (145, 1.00, 0.0),          # front — full opacity, full height
        ]
        p.setPen(Qt.NoPen)
        for li, (fa, hs, ysh) in enumerate(layer_cfgs):
            layer = self._layers[li]
            bot = vis_bot + ysh
            ys = [bot - layer['bands'][i] * vis_mh * hs * (1.0 + beat * 0.14)
                  for i in range(N)]
            # Filled mountain shape with horizontal gradient
            hg = QLinearGradient(vis_x, 0, vis_x + vis_w, 0)
            cl = QColor(accent2); cl.setAlpha(fa)
            cm = QColor(mid_col);  cm.setAlpha(fa)
            cr = QColor(accent);   cr.setAlpha(fa)
            hg.setColorAt(0.0, cl); hg.setColorAt(0.5, cm); hg.setColorAt(1.0, cr)
            p.setBrush(QBrush(hg))
            p.drawPath(self._smooth_path(xs, ys, bot))
            # Glow strokes along the ridge of the front layer only
            if li == 2:
                line = self._smooth_line(xs, ys)
                p.setBrush(Qt.NoBrush)
                for gw, ga in ((9.0, 8), (5.0, 22), (2.5, 70), (1.2, 190)):
                    gr = min(255, int(accent2.red()   * 0.15 + 255 * 0.85))
                    gg = min(255, int(accent2.green() * 0.25 + 255 * 0.75))
                    gb = min(255, int(accent2.blue()  * 0.55 + 255 * 0.45))
                    p.setPen(QPen(QColor(gr, gg, gb, ga), gw,
                                  Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                    p.drawPath(line)
                p.setPen(Qt.NoPen)
        # Track name
        ff = get_current_font()
        if self._track_name:
            fs = max(20, min(int(H * 0.055), 48))
            p.setFont(QFont(ff, fs, QFont.Bold))
            nr = QRect(60, int(H * 0.10), W - 120, int(H * 0.12))
            p.setPen(QColor(0, 0, 0, 90))
            p.drawText(nr.adjusted(2, 2, 2, 2), Qt.AlignCenter | Qt.TextWordWrap, self._track_name)
            p.setPen(QColor(T["text"]))
            p.drawText(nr, Qt.AlignCenter | Qt.TextWordWrap, self._track_name)
        # Time labels flanking the wave (like the reference image)
        pos_ms = self._player.position()
        dur_ms = self._player.duration()
        if dur_ms > 0:
            def _fmt(ms): return f"{ms // 60000}:{(ms % 60000) // 1000:02d}"
            p.setFont(QFont(ff, 14))
            tc = QColor(T["text2"]); tc.setAlpha(int(170 * self._fade_in))
            p.setPen(tc)
            ty = int(vis_bot) + 16
            p.drawText(QRect(int(vis_x) - 72, ty, 70, 28),
                       Qt.AlignRight | Qt.AlignVCenter, _fmt(pos_ms))
            p.drawText(QRect(int(vis_x + vis_w) + 4, ty, 70, 28),
                       Qt.AlignLeft | Qt.AlignVCenter, _fmt(dur_ms))
        # Exit hint
        hint_alpha = int(150 * self._fade_in)
        p.setFont(QFont(ff, 13))
        hc = QColor(T["text2"]); hc.setAlpha(hint_alpha)
        p.setPen(hc)
        p.drawText(QRect(0, H - 52, W, 36), Qt.AlignCenter, self._hint)
        # Entrance veil
        if self._fade_in < 1.0:
            p.setOpacity(1.0)
            ease = 1.0 - (self._fade_in * self._fade_in * (3 - 2 * self._fade_in))
            veil = QColor(0, 0, 0, int(255 * ease))
            p.setPen(Qt.NoPen); p.setBrush(veil); p.drawRect(r)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_F11: self.close()
        else: super().keyPressEvent(e)

    def closeEvent(self, e):
        self._timer.stop(); super().closeEvent(e)


# ═══════════════════════════════════════════════════════════
#  MAIN PLAYER WINDOW
# ═══════════════════════════════════════════════════════════
class MusicPlayer(QWidget):
    MODE_NORMAL=0; MODE_REPEAT_ALL=1; MODE_REPEAT_ONE=2
    AUDIO_EXT={'.mp3','.wav','.ogg','.flac','.aac','.m4a','.wma','.opus'}

    def __init__(self):
        super().__init__()
        # State
        self.selected_files=[]; self.liked_files=[]; self.current_index=-1
        self.shuffle=False; self.repeat_mode=self.MODE_NORMAL; self.shuffle_history=[]
        self.username=""; self._greeting_shown=False
        self.settings_path=get_settings_path()
        self._theme_key="spotify"; self._lang_key="RU"
        self._display_theme=dict(THEMES[self._theme_key])
        self._theme_anim=None; self._theme_ready=False; self.theme_popup=None
        self.is_maximized=False; self.normal_geometry=None
        self.resizing=False; self.resize_edge=None
        self.resize_start_pos=QPoint(); self.resize_start_geometry=QRect()
        self._crossfade_enabled=True
        self._glow_enabled=True
        self._dynamic_bg_enabled=True
        self._bg_orbs=DynamicBgOrbs()
        self._fullscreen_overlay=None
        self._track_analysis={}
        self._analysis_thread=None
        self._grid_mode=False
        self._lyrics_visible=False
        self._top_controls_in_settings=False
        self._shortcut_defs=[
            ("Space","sc_play_pause",self.toggle_pp),
            ("Right","sc_next",self.next_track),
            ("Left","sc_prev",self.prev_track),
            ("Up","sc_vol_up",self.volume_up),
            ("Down","sc_vol_down",self.volume_down),
            ("Delete","sc_delete",self.del_sel),
            ("Ctrl+O","sc_add",self.select_file),
            ("Ctrl+S","sc_shuffle",self.toggle_shuffle),
            ("Ctrl+R","sc_repeat",self.toggle_repeat),
            ("Ctrl+T","sc_theme",self.cycle_theme),
            ("Ctrl+L","sc_language",self.cycle_language),
            ("Ctrl+M","sc_mini",self.toggle_mini),
            ("Ctrl+H","sc_hide",self._toggle_bar),
            ("Ctrl+D","sc_discord",self.toggle_discord),
            ("Ctrl+E","sc_eq",self.toggle_eq),
            ("Ctrl+Y","sc_lyrics",self.toggle_lyrics),
            ("Ctrl+G","sc_grid",self.toggle_grid),
            ("Ctrl+W","sc_glow",self.toggle_glow),
            ("F11","sc_fullscreen",self.toggle_fullscreen),
        ]
        self._geom_anim=None

        # EQ (no external deps)
        self.eq_state=EQState()

        # EQ popup reference
        self._eq_popup=None

        # Crossfade
        self._fade_player=QMediaPlayer(); self._fade_duration=1500
        self._fade_timer=QTimer(self); self._fade_timer.setInterval(30); self._fade_timer.timeout.connect(self._cf_tick)
        self._fade_elapsed=0; self._fade_vol_from=100; self._fade_vol_to=100

        self.player=QMediaPlayer()
        self.player.mediaStatusChanged.connect(self.on_media_status)
        self.player.stateChanged.connect(self.on_state)
        self.player.error.connect(self.on_err)

        self._discord=DiscordRPC(); self._discord_enabled=False

        # Lyrics
        self._lyrics_fetcher=LyricsFetcher()
        self._last_lyrics_key=""

        # Spotify
        self._sp_client = None
        self._sp_creds = {}

        self.load_settings()
        self.init_ui()
        self.apply_theme()
        apply_tooltip_style(QApplication.instance(),self._display_theme)

        self.timer=QTimer(self); self.timer.timeout.connect(self.update_progress); self.timer.start(200)
        self.sleep_timer=QTimer(self); self.sleep_timer.setSingleShot(True); self.sleep_timer.timeout.connect(self.sleep_timeout)
        self.sleep_display_timer=QTimer(self); self.sleep_display_timer.timeout.connect(self.upd_sleep); self.sleep_display_timer.start(1000)
        self._bg_timer=QTimer(self); self._bg_timer.setInterval(33); self._bg_timer.timeout.connect(self._bg_tick)
        if self._dynamic_bg_enabled: self._bg_timer.start()

        self.setup_shortcuts()
        self.setAcceptDrops(True)
        self.setWindowFlags(Qt.FramelessWindowHint); self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle("ReturnalAudio"); self.setGeometry(100,100,964,704)

    # ── init_ui ───────────────────────────────────────────
    def init_ui(self):
        T=self._display_theme; lang=LANGUAGES[self._lang_key]
        root=QVBoxLayout(self); root.setSpacing(6); root.setContentsMargins(26,26,26,26)

        self.title_bar=CustomTitleBar(T); root.addWidget(self.title_bar)

        # Top toolbar
        self._top_bar=CollapsibleBar(T)
        self.lang_btn=LangButton(T,self._lang_key); self.lang_btn.lang_changed.connect(self.set_language)
        self.theme_btn=ThemeButton(T,self._theme_key); self.theme_btn.connect(self.toggle_theme_popup)
        self.font_btn=FontButton(T); self.font_btn.font_changed.connect(self._on_font)
        self.user_btn=UsernameButton(T,self._lang_key,self.username); self.user_btn.connect(self.show_username_popup)

        def _mk_icon(ico, tt, slot, act=False):
            b = IconButton(ico, T); b.setFixedSize(38, 36); b.setToolTip(tt)
            b.clicked.connect(slot); b.setActive(act)
            return b

        self.btn_cf=_mk_icon("crossfade", "Crossfade", self.toggle_crossfade, self._crossfade_enabled)
        self.btn_glow=_mk_icon("glow", "Toggle Glow  Ctrl+W", self.toggle_glow, self._glow_enabled)
        self.btn_discord=_mk_icon("discord", "Discord Rich Presence", self.toggle_discord)
        self.btn_eq=_mk_icon("eq", "Equalizer (triangle EQ)", self.toggle_eq)
        self.btn_lyrics=_mk_icon("lyrics", "Show/hide lyrics  Ctrl+Y", self.toggle_lyrics)
        self.btn_grid=_mk_icon("grid", "Grid / List view  Ctrl+G", self.toggle_grid)

        self.btn_mini=MiniButton(T,self)
        self.btn_mini.toggled.connect(self.toggle_mini)

        self.btn_spotify = QPushButton("SP"); self.btn_spotify.setFixedSize(38,36)
        self.btn_spotify.setCheckable(True)
        self.btn_spotify.setToolTip("Spotify in mini player\nRight-click to configure" if SPOTIPY_OK else "pip install spotipy  to enable Spotify")
        self.btn_spotify.clicked.connect(self.toggle_spotify_mini)
        self.btn_spotify.setContextMenuPolicy(Qt.CustomContextMenu)
        self.btn_spotify.customContextMenuRequested.connect(self._spotify_setup)

        self.btn_hide=IconButton("hide_bar",T); self.btn_hide.setFixedSize(32,32)
        self.btn_hide.setToolTip("Hide/show toolbar"); self.btn_hide.clicked.connect(self._toggle_bar)
        self.eula_btn=IconButton("eula",T); self.eula_btn.setFixedSize(38,36)
        self.eula_btn.setToolTip("License"); self.eula_btn.clicked.connect(self.show_eula)
        self.btn_settings=SettingsLaunchButton(T)
        self.btn_settings.setToolTip("Settings"); self.btn_settings.connect(self.toggle_settings_dialog)

        self._top_bar_controls=(
            self.lang_btn,self.theme_btn,self.font_btn,self.user_btn,
            self.btn_mini,self.btn_cf,self.btn_glow,self.btn_discord,
            self.btn_spotify,self.btn_eq,self.btn_lyrics,self.btn_grid,
        )
        self._top_extra_controls=(self.btn_hide,)
        self._movable_top_controls=self._top_bar_controls+self._top_extra_controls

        self._settings_dialog=SettingsDialog(T,self)
        self._settings_dialog.controls_in_settings_changed.connect(self._set_top_controls_in_settings)
        self._settings_dialog.dynamic_bg_changed.connect(self.toggle_dynamic_bg)
        self._settings_dialog.set_dynamic_bg(self._dynamic_bg_enabled)
        self._settings_dialog.set_language(self._lang_key)
        self._settings_dialog.set_shortcuts(self._get_shortcut_items())

        combo=QHBoxLayout(); combo.setSpacing(4); combo.setContentsMargins(0,0,0,0)
        self._top_combo=combo
        self._top_left=QWidget()
        self._top_left_lo=QHBoxLayout(self._top_left)
        self._top_left_lo.setContentsMargins(0,0,0,0)
        self._top_left_lo.setSpacing(0)
        self._top_left_lo.addWidget(self._top_bar)
        combo.addWidget(self._top_left,1)
        combo.addWidget(self.btn_hide)
        combo.addWidget(self.btn_settings)
        combo.addWidget(self.eula_btn)
        root.addLayout(combo)
        self._rebuild_top_controls()

        self.now_playing=NowPlayingWidget(T); root.addWidget(self.now_playing)

        self.search_box=QLineEdit(); self.search_box.setPlaceholderText(lang["search"])
        self.search_box.textChanged.connect(self.filter_list); root.addWidget(self.search_box)

        # Main content: [list/grid | lyrics]
        self._splitter=QSplitter(Qt.Horizontal); self._splitter.setHandleWidth(5)

        # Left panel
        self._left=QWidget()
        self._left_lo=QVBoxLayout(self._left); self._left_lo.setContentsMargins(0,0,0,0); self._left_lo.setSpacing(0)
        self.tabs=QTabWidget()
        self.file_list=QListWidget(); self.fav_list=QListWidget()
        for lst in (self.file_list,self.fav_list):
            lst.setFocusPolicy(Qt.NoFocus); lst.itemDoubleClicked.connect(self.on_dbl)
        self.tabs.addTab(self.file_list,lang.get("all_tracks","All"))
        self.tabs.addTab(self.fav_list,lang.get("favorites","Favorites"))
        self.tabs.currentChanged.connect(lambda _: self.refresh_lists())
        self.grid_view=GridView(T); self.grid_view.hide()
        self.grid_view.track_dbl.connect(self._on_grid_dbl)
        self.grid_view.track_like.connect(self.toggle_like)
        self.grid_view.track_select.connect(self._on_grid_select)
        self._left_lo.addWidget(self.tabs)
        self._left_lo.addWidget(self.grid_view)
        self._splitter.addWidget(self._left)

        # Right: lyrics
        self.lyrics_panel=LyricsPanel(T); self.lyrics_panel.hide()
        self._splitter.addWidget(self.lyrics_panel)
        self._splitter.setSizes([620,300])
        self._splitter.setCollapsible(0,False); self._splitter.setCollapsible(1,False)
        root.addWidget(self._splitter,1)

        self.prog=RunningSlider(self.player,T); root.addWidget(self.prog)
        self.time_lbl=QLabel("00:00 / 00:00"); self.time_lbl.setAlignment(Qt.AlignCenter); root.addWidget(self.time_lbl)

        controls=QHBoxLayout(); controls.setSpacing(4)
        def _mk_ctl(ic, tt, cb):
            b = IconButton(ic, T); b.setToolTip(tt); b.clicked.connect(cb); controls.addWidget(b); return b

        self.btn_add=_mk_ctl("add", "Add files  Ctrl+O", self.select_file)
        self.btn_prev=_mk_ctl("prev", "Prev  ←", self.prev_track)
        self.btn_pp=PlayPauseButton(T); self.btn_pp.clicked=self.toggle_pp; controls.addWidget(self.btn_pp)
        self.btn_stop=_mk_ctl("stop", "Stop", self.stop_music)
        self.btn_next=_mk_ctl("next", "Next  →", self.next_track)
        self.btn_shuf=_mk_ctl("shuffle", "Shuffle  Ctrl+S", self.toggle_shuffle)
        self.btn_rep=_mk_ctl("repeat", "Repeat  Ctrl+R", self.toggle_repeat)

        self.sleep_btn=SleepButton(T,lang); self.sleep_btn.connect(self.set_sleep); controls.addWidget(self.sleep_btn)
        self.speed_btn=SpeedButton(T,lang); self.speed_btn.connect(lambda v: self.player.setPlaybackRate(v))
        self.volume_widget=VolumeSlider(T); self.volume_widget.setValue(70); self.volume_widget.connect(self._on_volume_change)

        controls.addStretch(); controls.addWidget(self.speed_btn); controls.addWidget(self.volume_widget)
        root.addLayout(controls)

        self._drop_ov=DropOverlay(T,lang.get("drop_hint","Drop audio files here"),self)
        self._eula_win=EulaWindow(T,self._lang_key,self); self._eula_win.hide()
        self._greeting=GreetingOverlay(T,self._lang_key,self.username,self); self._greeting.hide()

        self._mini=MiniPlayer(T,self.player,self.toggle_pp,self.next_track,self.prev_track,None)
        self._mini.closed.connect(lambda: self.btn_mini.setActive(False))
        self.btn_mini._mini_ref=self._mini
        self._mini_active=False

    # ── Volume + EQ compensation ──────────────────────────
    def _on_volume_change(self, v):
        """Apply volume + EQ compensation to player."""
        comp = self.eq_state.volume_compensation()
        adjusted = int(max(0, min(100, v * (1.0 + comp))))
        self.player.setVolume(adjusted)
        self._fade_player.setVolume(adjusted)

    def _reapply_eq_volume(self):
        self._on_volume_change(self.volume_widget.value())

    def _get_shortcut_items(self):
        txt=get_settings_texts(self._lang_key)
        return [(txt.get(title_key, title_key), key) for key,title_key,_ in self._shortcut_defs]

    def _rebuild_top_controls(self):
        self._top_bar.clear()

        if self._top_controls_in_settings:
            self._top_bar.hide()
            self._settings_dialog.set_controls_in_settings(True)
            self._settings_dialog.set_controls([
                (get_settings_texts(self._lang_key)["sec_appearance"], (self.lang_btn, self.theme_btn, self.font_btn, self.btn_glow)),
                (get_settings_texts(self._lang_key)["sec_playback"], (self.btn_mini, self.btn_cf, self.btn_eq, self.btn_lyrics, self.btn_grid)),
                (get_settings_texts(self._lang_key)["sec_integration"], (self.btn_discord, self.btn_spotify)),
                (get_settings_texts(self._lang_key)["sec_profile"], (self.user_btn, self.btn_hide)),
            ])
            return

        self._top_bar.show()
        for w in self._top_bar_controls:
            self._top_bar.add_widget(w)
            w.show()
        self._top_bar.add_stretch()

        self._settings_dialog.set_controls_in_settings(False)
        self._settings_dialog.set_controls([])
        self._top_combo.insertWidget(2, self.btn_hide)
        self.btn_hide.show()

    def _set_top_controls_in_settings(self, enabled):
        self._top_controls_in_settings=enabled
        self._rebuild_top_controls()
        self._apply_glow()
        self.save_settings()

    def toggle_settings_dialog(self):
        self._settings_dialog.set_theme(self._display_theme)
        self._settings_dialog.set_controls_in_settings(self._top_controls_in_settings)
        if self._settings_dialog.isVisible():
            self._settings_dialog.hide()
            return
        gp=self.btn_settings.mapToGlobal(QPoint(self.btn_settings.width()-self._settings_dialog.width(), self.btn_settings.height()+8))
        self._settings_dialog.move(gp)
        self._settings_dialog.show()
        self._settings_dialog.raise_()

    # ── Resize / paint ────────────────────────────────────
    def resizeEvent(self,e):
        super().resizeEvent(e)
        for o in ('_drop_ov','_greeting'):
            if hasattr(self,o): getattr(self,o).setGeometry(0,0,self.width(),self.height())

    def _animate_geometry(self,t,cb):
        if self._geom_anim: self._geom_anim.stop()
        an=QPropertyAnimation(self,b"geometry"); an.setDuration(300); an.setEasingCurve(QEasingCurve.InOutQuad)
        an.setStartValue(self.geometry()); an.setEndValue(t); an.finished.connect(cb); an.start(); self._geom_anim=an

    def on_maximize_finished(self): self.is_maximized=True;  self.update()
    def on_restore_finished(self):  self.is_maximized=False; self.update()

    def get_resize_edge(self,pos):
        if self.is_maximized: return None
        m=8; x,y=pos.x(),pos.y(); w,h=self.width(),self.height()
        if x<=m and y<=m: return 'top-left'
        if x>=w-m and y<=m: return 'top-right'
        if x<=m and y>=h-m: return 'bottom-left'
        if x>=w-m and y>=h-m: return 'bottom-right'
        if x<=m: return 'left'
        if x>=w-m: return 'right'
        if y<=m: return 'top'
        if y>=h-m: return 'bottom'
        return None
    _EC={'top-left':Qt.SizeFDiagCursor,'top-right':Qt.SizeBDiagCursor,'bottom-left':Qt.SizeBDiagCursor,'bottom-right':Qt.SizeFDiagCursor,'left':Qt.SizeHorCursor,'right':Qt.SizeHorCursor,'top':Qt.SizeVerCursor,'bottom':Qt.SizeVerCursor}

    def mousePressEvent(self,e):
        if e.button()==Qt.LeftButton and not self.is_maximized:
            edge=self.get_resize_edge(e.pos())
            if edge: self.resizing=True; self.resize_edge=edge; self.resize_start_pos=e.globalPos(); self.resize_start_geometry=self.geometry(); e.accept(); return
        super().mousePressEvent(e)
    def mouseMoveEvent(self,e):
        if self.resizing and self.resize_edge:
            d=e.globalPos()-self.resize_start_pos; g=QRect(self.resize_start_geometry); mw,mh=500,380
            if 'left'  in self.resize_edge: g.setLeft(min(g.right()-mw,g.left()+d.x()))
            if 'right' in self.resize_edge: g.setRight(max(g.left()+mw,g.right()+d.x()))
            if 'top'   in self.resize_edge: g.setTop(min(g.bottom()-mh,g.top()+d.y()))
            if 'bottom' in self.resize_edge: g.setBottom(max(g.top()+mh,g.bottom()+d.y()))
            self.setGeometry(g); self.setCursor(self._EC.get(self.resize_edge,Qt.ArrowCursor)); e.accept(); return
        edge=self.get_resize_edge(e.pos())
        self.setCursor(self._EC.get(edge,Qt.ArrowCursor) if edge else Qt.ArrowCursor)
        super().mouseMoveEvent(e)
    def mouseReleaseEvent(self,e):
        if e.button()==Qt.LeftButton and self.resizing:
            self.resizing=False; self.resize_edge=None; e.accept(); return
        super().mouseReleaseEvent(e)
    def _bg_tick(self):
        self._bg_orbs.tick(); self.update()

    def toggle_dynamic_bg(self, enabled):
        self._dynamic_bg_enabled=enabled
        if enabled: self._bg_timer.start()
        else: self._bg_timer.stop(); self.update()
        self.save_settings()

    def paintEvent(self,e):
        p=QPainter(self); p.setRenderHint(QPainter.Antialiasing); T=self._display_theme
        r=QRectF(0,0,self.width(),self.height()); rad=0 if self.is_maximized else 12
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(make_vgrad(r,T["bg"],T["bg2"])))
        p.drawRoundedRect(r,rad,rad)
        if self._dynamic_bg_enabled:
            p.save()
            clip=QPainterPath(); clip.addRoundedRect(r,rad,rad); p.setClipPath(clip)
            colors=[QColor(T.get("accent","#7F7FFF")),QColor(T.get("accent2",T.get("accent","#7F7FFF")))]
            for o in self._bg_orbs.orbs:
                cx=o['x']*self.width(); cy=o['y']*self.height()
                orb_r=o['r']*min(self.width(),self.height())*(0.88+0.12*math.sin(o['phase']))
                col=colors[o['ci']%len(colors)]
                grad=QRadialGradient(cx,cy,orb_r)
                c0=QColor(col); c0.setAlpha(45)
                c1=QColor(col); c1.setAlpha(0)
                grad.setColorAt(0,c0); grad.setColorAt(1,c1)
                p.setBrush(QBrush(grad)); p.drawEllipse(QPointF(cx,cy),orb_r,orb_r)
            p.restore()

    # ── Theme ─────────────────────────────────────────────
    def toggle_theme_popup(self):
        if self.theme_popup and self.theme_popup.isVisible(): self.theme_popup.close(); self.theme_popup=None; return
        lang=LANGUAGES[self._lang_key]
        self.theme_popup=ThemeModePopup(self._display_theme,lang,self)
        self.theme_popup.connect_dark(lambda: self.cycle_theme_group("dark"))
        self.theme_popup.connect_light(lambda: self.cycle_theme_group("light"))
        self.theme_popup.move(self.theme_btn.mapToGlobal(QPoint(0,self.theme_btn.height()+6))); self.theme_popup.show()
    def cycle_theme_group(self,grp):
        keys=DARK_THEME_KEYS if grp=="dark" else LIGHT_THEME_KEYS
        self._theme_key=keys[(keys.index(self._theme_key)+1)%len(keys)] if self._theme_key in keys else keys[0]
        if self.theme_popup: self.theme_popup.close(); self.theme_popup=None
        self.apply_theme(); self.save_settings()
    def cycle_theme(self):
        keys=list(THEMES.keys()); self._theme_key=keys[(keys.index(self._theme_key)+1)%len(keys)]
        self.apply_theme(); self.save_settings()
    def apply_theme(self):
        target=THEMES[self._theme_key]; lang=LANGUAGES[self._lang_key]
        if self._theme_anim: self._theme_anim.stop(); self._theme_anim=None
        if not self._theme_ready: self._apply_ts(target,lang); self._theme_ready=True; return
        if all(self._display_theme.get(k)==target.get(k) for k in THEME_COLOR_KEYS):
            self._apply_ts(target,lang); return
        start=dict(self._display_theme)
        an=QVariantAnimation(self); an.setDuration(320); an.setStartValue(0.0); an.setEndValue(1.0)
        an.setEasingCurve(QEasingCurve.InOutCubic)
        an.valueChanged.connect(lambda v: self._apply_ts(blend_theme(start,target,float(v)),lang))
        an.finished.connect(lambda: self._apply_ts(target,lang))
        an.start(); self._theme_anim=an

    def _apply_ts(self,theme,lang):
        self._display_theme=dict(theme); T=theme
        bg=T["bg"]; sf=T["surface"]; acc=T["accent"]; tx=T["text"]; tx2=T["text2"]; trk=T["track_bg"]
        fw="bold" if get_font_bold() else "normal"; ff=get_current_font()
        self.setStyleSheet(f"MusicPlayer{{background:transparent;color:{tx};}}")
        self.now_playing.set_theme(theme)
        self.time_lbl.setStyleSheet(f"color:{tx2};font-family:{ff};")
        self.search_box.setStyleSheet(f"QLineEdit{{background:{sf};color:{tx};border:1px solid {trk};border-radius:6px;padding:6px 10px;font-size:13px;font-family:{ff};font-weight:{fw};}}QLineEdit:focus{{border:1px solid {acc};}}")
        self.search_box.setPlaceholderText(lang["search"])
        ls=f"QListWidget{{background:{sf};border:none;border-radius:8px;padding:5px;color:{tx};font-size:14px;font-family:{ff};font-weight:{fw};outline:0;}}QListWidget::item{{padding:0px;border-radius:5px;border:none;outline:0;}}QListWidget::item:selected{{background:{acc};color:{bg};border:none;}}QListWidget::item:hover{{background:{T['hover']};}}"
        self.file_list.setStyleSheet(ls); self.fav_list.setStyleSheet(ls)
        apply_smooth_scrollbar(self.file_list,theme); apply_smooth_scrollbar(self.fav_list,theme)
        self.tabs.setStyleSheet(f"QTabWidget::pane{{border:none;background:transparent;}}QTabBar::tab{{background:{sf};color:{tx2};border:1px solid {trk};border-bottom:none;border-top-left-radius:8px;border-top-right-radius:8px;padding:6px 14px;margin-right:2px;font-family:{ff};font-weight:{fw};}}QTabBar::tab:selected{{background:{acc};color:{bg};}}QTabBar::tab:hover{{color:{tx};}}")
        self.tabs.setTabText(0,lang.get("all_tracks","All")); self.tabs.setTabText(1,lang.get("favorites","Favorites"))
        self._splitter.setStyleSheet(f"QSplitter::handle{{background:{trk};}}")
        self.lyrics_panel.set_theme(theme); self.grid_view.set_theme(theme)
        self.prog.set_theme(theme); self.volume_widget.set_theme(theme)
        self.speed_btn.set_theme(theme); self.speed_btn.set_lang(lang)
        self.sleep_btn.set_theme(theme); self.sleep_btn.set_lang(lang)
        self.title_bar.set_theme(theme)
        self.lang_btn.set_theme(theme); self.lang_btn.lang_key=self._lang_key; self.lang_btn.update()
        self.theme_btn.set_theme(theme,self._theme_key)
        self.btn_pp.set_theme(theme); self.eula_btn.set_theme(theme)
        for b in (self.font_btn,self.btn_cf,self.btn_discord,self.btn_eq,self.btn_hide,self.btn_mini,self.btn_lyrics,self.btn_grid,self.btn_settings):
            if hasattr(b,"set_theme"): b.set_theme(theme)
        # Style Spotify button with theme colours
        T2=theme
        self.btn_spotify.setStyleSheet(
            f"QPushButton{{background:{T2['surface']};color:{T2['accent']};border:1px solid {T2['track_bg']};"
            f"border-radius:8px;font-weight:bold;font-size:10px;}}"
            f"QPushButton:checked{{background:#1DB954;color:#000;border-color:#1DB954;}}"
            f"QPushButton:hover{{background:{T2['hover']};}}")
        for b in (self.btn_add,self.btn_prev,self.btn_stop,self.btn_next,self.btn_shuf,self.btn_rep): b.set_theme(theme)
        for wl in (self.file_list,self.fav_list):
            for i in range(wl.count()):
                row=wl.itemWidget(wl.item(i))
                if isinstance(row,TrackRowWidget): row.set_theme(theme)
        if hasattr(self,'_drop_ov'): self._drop_ov.set_theme(theme)
        if hasattr(self,'_eula_win'): self._eula_win.set_theme(theme); self._eula_win.set_lang(self._lang_key)
        if hasattr(self,'user_btn'): self.user_btn.set_theme(theme); self.user_btn.set_lang(self._lang_key,self.username)
        if hasattr(self,'_mini'): self._mini.set_theme(theme)
        if self._eq_popup: self._eq_popup.set_theme(theme)
        if self._fullscreen_overlay and self._fullscreen_overlay.isVisible():
            self._fullscreen_overlay.set_theme(theme)
        if hasattr(self,'_settings_dialog'):
            self._settings_dialog.set_theme(theme)
            self._settings_dialog.set_language(self._lang_key)
            self._settings_dialog.set_shortcuts(self._get_shortcut_items())
            if hasattr(self,'_top_controls_in_settings') and self._top_controls_in_settings:
                self._rebuild_top_controls()
        apply_tooltip_style(QApplication.instance(),theme)
        self._apply_glow()
        self.setWindowTitle("ReturnalAudio"); self.update()

    # ── Language ──────────────────────────────────────────
    def _upd_fs_hint(self):
        if self._fullscreen_overlay and self._fullscreen_overlay.isVisible():
            self._fullscreen_overlay.set_hint(FS_HINTS.get(self._lang_key, FS_HINTS["EN"]))

    def set_language(self, lang_key):
        self._lang_key=lang_key
        self.lang_btn.lang_key=self._lang_key; self.lang_btn.update()
        self.apply_theme(); self._upd_rep_tip(); self._upd_fs_hint(); self.save_settings()
    def cycle_language(self):
        idx=LANG_ORDER.index(self._lang_key); self._lang_key=LANG_ORDER[(idx+1)%len(LANG_ORDER)]
        self.lang_btn.lang_key=self._lang_key; self.lang_btn.update()
        self.apply_theme(); self._upd_rep_tip(); self._upd_fs_hint(); self.save_settings()
    def tr(self,k): return LANGUAGES[self._lang_key].get(k,k)

    # ── Shortcuts ─────────────────────────────────────────
    def setup_shortcuts(self):
        for key, _title, slot in self._shortcut_defs:
            QShortcut(QKeySequence(key),self,slot)
    def volume_up(self):   self.volume_widget.setValue(self.volume_widget.value()+5)
    def volume_down(self): self.volume_widget.setValue(self.volume_widget.value()-5)

    # ── UI helpers ────────────────────────────────────────
    def _cur_lw(self): return self.file_list if self.tabs.currentIndex()==0 else self.fav_list
    def _cur_path(self):
        item=self._cur_lw().currentItem(); return item.data(Qt.UserRole) if item else None
    def is_liked(self,p): return bool(p) and p in self.liked_files

    def _track_label(self,path):
        if MUTAGEN_OK:
            try:
                af=MutagenFile(path,easy=True)
                if af:
                    ar=af.get("artist",[""])[0]; ti=af.get("title",[""])[0]
                    if ar and ti: return f"{ar} — {ti}"
                    if ti: return ti
            except Exception: pass
        return os.path.splitext(os.path.basename(path))[0]

    def _artist_title(self,path):
        ar,ti="",""
        if MUTAGEN_OK:
            try:
                af=MutagenFile(path,easy=True)
                if af: ar=af.get("artist",[""])[0]; ti=af.get("title",[""])[0]
            except Exception: pass
        if not ti:
            base=os.path.splitext(os.path.basename(path))[0]
            sep=" — " if " — " in base else (" - " if " - " in base else "")
            if sep: parts=base.split(sep,1); ar=parts[0].strip(); ti=parts[1].strip()
            else: ti=base
        return ar,ti

    def _make_row(self,path):
        row=TrackRowWidget(path,self._track_label(path),self.is_liked(path),self._display_theme)
        row.like_clicked.connect(self.toggle_like); row.delete_clicked.connect(self.del_by_path)
        return row

    def toggle_like(self,path):
        if path in self.liked_files: self.liked_files.remove(path)
        else: self.liked_files.append(path)
        self.save_settings(); QTimer.singleShot(1,self.refresh_lists)

    def del_by_path(self,path):
        if path in self.selected_files:
            idx=self.selected_files.index(path); self.selected_files.remove(path)
            if path in self.liked_files: self.liked_files.remove(path)
            if self.current_index>=idx: self.current_index=max(-1,self.current_index-1)
            self.refresh_lists(); self.save_settings()

    def del_sel(self):
        if self._grid_mode: return
        p=self._cur_path()
        if p: self.del_by_path(p)

    # ── List / Grid management ────────────────────────────
    def refresh_lists(self):
        if self._grid_mode: self._populate_grid(); return
        sp=self._cur_path(); st=self.search_box.text()
        for wl,fav in ((self.file_list,False),(self.fav_list,True)):
            wl.clear()
            for path in self.selected_files:
                if fav and path not in self.liked_files: continue
                item=QListWidgetItem(wl); item.setData(Qt.UserRole,path)
                item.setSizeHint(QSize(0,44)); wl.addItem(item); wl.setItemWidget(item,self._make_row(path))
        self.filter_list(st)
        if sp:
            for wl in (self.file_list,self.fav_list):
                for i in range(wl.count()):
                    item=wl.item(i)
                    if item.data(Qt.UserRole)==sp: wl.setCurrentItem(item); break
        self.highlight_current()

    def filter_list(self,text):
        if self._grid_mode: self._populate_grid(); return
        q=text.lower()
        for wl in (self.file_list,self.fav_list):
            for i in range(wl.count()):
                item=wl.item(i); row=wl.itemWidget(item)
                vis=not q or (isinstance(row,TrackRowWidget) and q in row.label._text.lower())
                item.setHidden(not vis)

    def _scroll_to_cur(self):
        if not(0<=self.current_index<len(self.selected_files)): return
        cp=self.selected_files[self.current_index]
        for wl in (self.file_list,self.fav_list):
            for i in range(wl.count()):
                item=wl.item(i)
                if item.data(Qt.UserRole)==cp: wl.scrollToItem(item,QAbstractItemView.PositionAtCenter); break

    def highlight_current(self):
        cp=self.selected_files[self.current_index] if 0<=self.current_index<len(self.selected_files) else None
        for wl in (self.file_list,self.fav_list):
            wl.clearSelection()
            for i in range(wl.count()):
                item=wl.item(i); ic=(item.data(Qt.UserRole)==cp)
                f=item.font(); f.setBold(ic); item.setFont(f)
                if ic: wl.setCurrentItem(item)
        if self._grid_mode: self.grid_view.update_current(cp)

    def _populate_grid(self):
        cur=self.selected_files[self.current_index] if 0<=self.current_index<len(self.selected_files) else None
        self.grid_view.populate(self.selected_files,set(self.liked_files),cur,self.search_box.text())

    # ── Grid / Lyrics toggles ─────────────────────────────
    def toggle_grid(self):
        self._grid_mode=not self._grid_mode; self.btn_grid.setActive(self._grid_mode)
        if self._grid_mode:
            self.tabs.hide(); self.grid_view.show(); self._populate_grid()
        else:
            self.grid_view.hide(); self.tabs.show(); self.refresh_lists()
        self.save_settings()

    def toggle_lyrics(self):
        self._lyrics_visible=not self._lyrics_visible; self.btn_lyrics.setActive(self._lyrics_visible)
        if self._lyrics_visible:
            self.lyrics_panel.show(); self._splitter.setSizes([580,320])
            if 0<=self.current_index<len(self.selected_files):
                self._fetch_lyrics(self.selected_files[self.current_index])
            else:
                self.lyrics_panel.clear()
        else:
            self.lyrics_panel.hide(); self._splitter.setSizes([620,0])
        self.save_settings()

    def _fetch_lyrics(self,path):
        if not self._lyrics_visible: return
        ar,ti=self._artist_title(path); key=f"{path}|{ar}|{ti}"
        if key==self._last_lyrics_key: return
        self._last_lyrics_key=key
        # Always reset source buttons on new track
        if hasattr(self,'lyrics_panel'):
            self.lyrics_panel._lbl.setText(f"♪ {ar} — {ti}" if ar else f"♪ {ti}")
        if not ti and not ar:
            if hasattr(self,'lyrics_panel'): self.lyrics_panel.show_error()
            return
        lbl=f"{ar} — {ti}" if ar else ti
        if hasattr(self,'lyrics_panel'): self.lyrics_panel.show_loading(lbl)
        self._lyrics_fetcher.fetch(
            path, ar, ti,
            on_ready=lambda res: QTimer.singleShot(0, lambda: self.lyrics_panel.show_lyrics(res)),
            on_error=lambda _: QTimer.singleShot(0, self.lyrics_panel.show_error)
        )

    # ── File management ───────────────────────────────────
    def select_file(self):
        files,_=QFileDialog.getOpenFileNames(self,self.tr("add_files"),"",self.tr("file_filter"))
        for p in files:
            if p not in self.selected_files: self.selected_files.append(p)
        self.refresh_lists(); self.save_settings()

    def _on_grid_select(self, path):
        """Single click in grid: highlight card without playing."""
        if path in self.selected_files:
            idx = self.selected_files.index(path)
            # Only update highlight, not playback
            cur = self.selected_files[self.current_index] if 0 <= self.current_index < len(self.selected_files) else None
            self.grid_view.update_current(cur)  # keep actual playing track highlighted

    def _on_grid_dbl(self,path):
        if path in self.selected_files:
            self.current_index=self.selected_files.index(path); self.play_music()

    # ── Drag & Drop ───────────────────────────────────────
    def dragEnterEvent(self,e):
        if e.mimeData().hasUrls() and any(self._is_audio(u.toLocalFile()) for u in e.mimeData().urls()):
            e.acceptProposedAction(); self._drop_ov.show_overlay(); return
        e.ignore()
    def dragMoveEvent(self,e): e.acceptProposedAction()
    def dragLeaveEvent(self,e): self._drop_ov.hide_overlay()
    def dropEvent(self,e):
        self._drop_ov.hide_overlay(); added=0
        for url in e.mimeData().urls():
            path=url.toLocalFile()
            if os.path.isdir(path):
                for rd,_,files in os.walk(path):
                    for fn in sorted(files):
                        fp=os.path.join(rd,fn)
                        if self._is_audio(fp) and fp not in self.selected_files: self.selected_files.append(fp); added+=1
            elif self._is_audio(path) and path not in self.selected_files: self.selected_files.append(path); added+=1
        if added: self.refresh_lists(); self.save_settings()
        e.acceptProposedAction()
    def _is_audio(self,p): return os.path.isfile(p) and os.path.splitext(p)[1].lower() in self.AUDIO_EXT

    # ── Playback ──────────────────────────────────────────
    def _cf_tick(self):
        self._fade_elapsed+=self._fade_timer.interval()
        t=min(1.0,self._fade_elapsed/self._fade_duration); ease=t*t*(3-2*t)
        self.player.setVolume(int(self._fade_vol_from*(1-ease)))
        self._fade_player.setVolume(int(self._fade_vol_to*ease))
        if t>=1.0:
            self._fade_timer.stop(); self.player.stop()
            self.player.setMedia(self._fade_player.media()); self.player.setVolume(self._fade_vol_to)
            self.player.setPosition(self._fade_player.position()); self.player.play()
            self._fade_player.stop(); self._fade_player.setMedia(QMediaContent())

    def _start_cf(self,path):
        if not os.path.exists(path): return
        vol=self.volume_widget.value()
        self._fade_player.setMedia(QMediaContent(QUrl.fromLocalFile(path)))
        self._fade_player.setVolume(0); self._fade_player.play()
        self._fade_vol_from=vol; self._fade_vol_to=vol; self._fade_elapsed=0; self._fade_timer.start()

    def on_dbl(self,item):
        path=item.data(Qt.UserRole)
        if path in self.selected_files:
            self.current_index=self.selected_files.index(path)
            if self._fade_timer.isActive():
                self._fade_timer.stop(); self._fade_player.stop()
                self._fade_player.setMedia(QMediaContent()); self.player.setVolume(self.volume_widget.value())
            self.play_music()

    def toggle_pp(self):
        if self.player.state()==QMediaPlayer.PlayingState: self.player.pause()
        else: self.play_music()

    def play_music(self):
        if not self.selected_files: return
        if self.current_index==-1: self.current_index=0
        if not(0<=self.current_index<len(self.selected_files)): return
        path=self.selected_files[self.current_index]
        if not os.path.exists(path): self.now_playing.set_text(self.tr("not_found")); self.next_track(); return
        self._upd_now_playing(); self.highlight_current(); QTimer.singleShot(0,self._scroll_to_cur)
        if self._mini_active: self._mini.set_title(self._track_label(path)); self._mini.set_playing(True)
        if self._discord.is_enabled: self._discord.update(self._track_label(path))
        if self._lyrics_visible: self._fetch_lyrics(path)
        cp=self.player.media().canonicalUrl().toLocalFile()
        ip=self.player.state()==QMediaPlayer.PlayingState
        if cp!=path and ip and self._crossfade_enabled: self._start_cf(path)
        else:
            if cp!=path: self.player.setMedia(QMediaContent(QUrl.fromLocalFile(path)))
            self.player.play()
        self._reapply_eq_volume()

    def stop_music(self):
        self._fade_timer.stop(); self._fade_player.stop(); self.player.stop()
        if self._discord.is_enabled: self._discord.clear()

    def next_track(self):
        if not self.selected_files: return
        if self.shuffle: self.shuffle_history.append(self.current_index); self.current_index=random.randrange(len(self.selected_files))
        elif self.repeat_mode==self.MODE_REPEAT_ONE: pass
        elif self.current_index+1<len(self.selected_files): self.current_index+=1
        elif self.repeat_mode==self.MODE_REPEAT_ALL: self.current_index=0
        else: return
        self.play_music()

    def prev_track(self):
        if not self.selected_files: return
        if self.shuffle and self.shuffle_history: self.current_index=self.shuffle_history.pop()
        elif self.current_index>0: self.current_index-=1
        elif self.repeat_mode==self.MODE_REPEAT_ALL: self.current_index=len(self.selected_files)-1
        else: return
        self.play_music()

    def on_media_status(self,s):
        if s==QMediaPlayer.EndOfMedia: self.next_track()
    def on_state(self,s):
        pl=s==QMediaPlayer.PlayingState; self.btn_pp.set_playing(pl)
        if self._mini_active: self._mini.set_playing(pl)
    def on_err(self,e):
        if e!=QMediaPlayer.NoError: self.now_playing.set_text(self.tr("error")+str(e))

    def toggle_shuffle(self):
        self.shuffle=not self.shuffle; self.btn_shuf.setActive(self.shuffle)
        if not self.shuffle: self.shuffle_history.clear()
    def toggle_repeat(self):
        self.repeat_mode=(self.repeat_mode+1)%3; self.btn_rep.setActive(self.repeat_mode!=self.MODE_NORMAL)
        self._upd_rep_tip()
    def _upd_rep_tip(self):
        self.btn_rep.setToolTip({self.MODE_NORMAL:self.tr("repeat_off"),self.MODE_REPEAT_ALL:self.tr("repeat_all"),self.MODE_REPEAT_ONE:self.tr("repeat_one")}[self.repeat_mode])

    def set_sleep(self,minutes):
        self.sleep_timer.stop()
        if minutes>0: self.sleep_timer.start(minutes*60*1000)
        self.sleep_btn._sleep=minutes; self.sleep_btn.update()
    def sleep_timeout(self):
        self.stop_music(); self.sleep_btn._sleep=0; self.sleep_btn.set_remaining(0,0)
    def upd_sleep(self):
        if not self.sleep_timer.isActive():
            if self.sleep_btn._rm or self.sleep_btn._rs: self.sleep_btn.set_remaining(0,0)
        else:
            rm=self.sleep_timer.remainingTime(); self.sleep_btn.set_remaining(rm//60000,(rm%60000)//1000)

    def _upd_now_playing(self):
        if 0<=self.current_index<len(self.selected_files):
            path=self.selected_files[self.current_index]
            lbl=self._track_label(path)
            self.now_playing.set_text(f"▶  {lbl}"); self.setWindowTitle(f"ReturnalAudio — {lbl}")
            if self._fullscreen_overlay and self._fullscreen_overlay.isVisible():
                self._fullscreen_overlay.set_track(lbl)
            self._start_analysis(path)

    def update_progress(self):
        dur=self.player.duration(); pos=self.player.position()
        if dur>0:
            self.prog.setRange(0,dur)
            if not self.prog._seeking: self.prog.setValue(pos)
            self.time_lbl.setText(f"{self._fmt(pos)} / {self._fmt(dur)}")
        if self._lyrics_visible: self.lyrics_panel.sync(pos)

    @staticmethod
    def _fmt(ms): s=ms//1000; return f"{s//60:02}:{s%60:02}"

    # ── EQ ────────────────────────────────────────────────
    def toggle_eq(self):
        if self._eq_popup and self._eq_popup.isVisible():
            self._eq_popup.close(); self._eq_popup=None; return
        self._eq_popup=EQPopup(self.eq_state,self._display_theme,self)
        self._eq_popup.eq_changed.connect(self._on_eq_change)
        gp=self.btn_eq.mapToGlobal(QPoint(0,self.btn_eq.height()+6))
        self._eq_popup.move(gp); self._eq_popup.show()

    def _on_eq_change(self,bass,mid,treble):
        self.eq_state.bass=bass; self.eq_state.mid=mid; self.eq_state.treble=treble
        active=abs(bass)>0.5 or abs(mid)>0.5 or abs(treble)>0.5
        self.btn_eq.setActive(active)
        self.btn_eq.setToolTip(f"EQ  B:{bass:+.0f} M:{mid:+.0f} T:{treble:+.0f} dB" if active else "Equalizer")
        # Apply volume compensation
        self._reapply_eq_volume()
        self.save_settings()

    # ── Fullscreen ────────────────────────────────────────────
    def toggle_fullscreen(self):
        if self._fullscreen_overlay and self._fullscreen_overlay.isVisible():
            self._fullscreen_overlay.close(); self._fullscreen_overlay=None; return
        track=""
        path=""
        if 0<=self.current_index<len(self.selected_files):
            path=self.selected_files[self.current_index]
            track=self._track_label(path)
        analysis=self._track_analysis.get(path)
        self._fullscreen_overlay=FullscreenOverlay(self.player, self._display_theme, track, analysis)
        self._fullscreen_overlay.set_hint(FS_HINTS.get(self._lang_key, FS_HINTS["EN"]))
        self._fullscreen_overlay.destroyed.connect(lambda: setattr(self,"_fullscreen_overlay",None))

    def _start_analysis(self, path):
        if not path or path in self._track_analysis:
            return
        def _done(rms_times, rms_vals, beat_times):
            if rms_times is not None:
                self._track_analysis[path]=(rms_times, rms_vals, beat_times)
                if (self._fullscreen_overlay and self._fullscreen_overlay.isVisible()
                        and 0<=self.current_index<len(self.selected_files)
                        and self.selected_files[self.current_index]==path):
                    self._fullscreen_overlay.set_analysis(rms_times, rms_vals, beat_times)
        t=TrackAnalyzer(path, _done); t.start(); self._analysis_thread=t

    # ── Mini / Crossfade / Glow / Discord ─────────────────────────
    def toggle_glow(self):
        self._glow_enabled = not self._glow_enabled
        self.btn_glow.setActive(self._glow_enabled)
        self._apply_glow()
        self.save_settings()

    def _apply_glow(self):
        main_w = (self.tabs, self.grid_view, self.lyrics_panel, self.now_playing, self.search_box, self.prog)
        top_btn = (self.lang_btn, self.theme_btn, self.font_btn, self.user_btn, self.btn_mini, self.btn_cf, 
                   self.btn_glow, self.btn_discord, self.btn_spotify, self.btn_eq, self.btn_lyrics, self.btn_grid, 
                   self.btn_hide, self.eula_btn, self.btn_settings)
        bot_btn = (self.volume_widget, self.speed_btn, self.sleep_btn, self.btn_pp, self.btn_next, 
                   self.btn_prev, self.btn_add, self.btn_stop, self.btn_shuf, self.btn_rep)
        
        all_w = main_w + top_btn + bot_btn + (self._top_bar,)
        if not self._glow_enabled:
            for w in all_w:
                w.setGraphicsEffect(None)
            if hasattr(self.grid_view, 'set_glow'):
                self.grid_view.set_glow(False)
            return

        self._top_bar.setGraphicsEffect(None)
        T = self._display_theme
        
        for w in main_w + top_btn:
            eff = QGraphicsDropShadowEffect(self)
            eff.setBlurRadius(18)
            eff.setOffset(0, 0)
            c = QColor(T["accent"])
            c.setAlpha(130)
            eff.setColor(c)
            w.setGraphicsEffect(eff)
            
        for w in bot_btn:
            eff = QGraphicsDropShadowEffect(self)
            eff.setBlurRadius(22)
            eff.setOffset(0, 0)
            c = QColor(T["accent"])
            c.setAlpha(240)
            eff.setColor(c)
            w.setGraphicsEffect(eff)
            
        if hasattr(self.grid_view, 'set_glow'):
            self.grid_view.set_glow(True)

    def toggle_mini(self):
        if not self._mini_active:
            self._mini_active=True; self.btn_mini.setActive(True)
            g=self.geometry()
            self._mini.move(g.x()+g.width()-self._mini._mini_w-20,g.y()+g.height()+10)
            self._mini.show(); self._mini.raise_()
            self._mini.set_playing(self.player.state()==QMediaPlayer.PlayingState)
            if 0<=self.current_index<len(self.selected_files):
                self._mini.set_title(self._track_label(self.selected_files[self.current_index]))
        else:
            self._mini_active=False; self.btn_mini.setActive(False); self._mini.hide()

    def toggle_crossfade(self):
        self._crossfade_enabled=not self._crossfade_enabled
        self.btn_cf.setActive(self._crossfade_enabled); self.save_settings()

    def _spotify_setup(self):
        """Right-click Spotify button → show credentials dialog."""
        if not SPOTIPY_OK:
            QMessageBox.information(self, "Spotify", "Install spotipy first:\n\npip install spotipy")
            return
        dlg = QDialog(self); dlg.setWindowTitle("Spotify Setup"); dlg.setFixedWidth(420)
        T = self._display_theme
        dlg.setStyleSheet(f"QDialog{{background:{T['surface']};color:{T['text']};}}"
                          f"QLabel{{color:{T['text2']};}}"
                          f"QLineEdit{{background:{T['bg']};color:{T['text']};border:1px solid {T['track_bg']};border-radius:4px;padding:4px 8px;}}")
        lo = QVBoxLayout(dlg)
        info = QLabel("Enter your Spotify API credentials.\nGet them at developer.spotify.com → Dashboard")
        info.setWordWrap(True); lo.addWidget(info)
        form = QFormLayout()
        le_id  = QLineEdit(); le_id.setPlaceholderText("Client ID")
        le_sec = QLineEdit(); le_sec.setPlaceholderText("Client Secret"); le_sec.setEchoMode(QLineEdit.Password)
        le_red = QLineEdit(); le_red.setText("http://localhost:8888/callback")
        # Pre-fill from settings
        saved = getattr(self, '_sp_creds', {})
        le_id.setText(saved.get('client_id',''))
        le_sec.setText(saved.get('client_secret',''))
        le_red.setText(saved.get('redirect_uri', 'http://localhost:8888/callback'))
        form.addRow("Client ID:", le_id)
        form.addRow("Client Secret:", le_sec)
        form.addRow("Redirect URI:", le_red)
        lo.addLayout(form)
        btns = QHBoxLayout()
        ok = QPushButton("Connect"); cancel = QPushButton("Cancel")
        ok.setStyleSheet(f"background:{T['accent']};color:{T['bg']};border:none;border-radius:6px;padding:6px 16px;font-weight:bold;")
        cancel.setStyleSheet(f"background:{T['surface']};color:{T['text2']};border:1px solid {T['track_bg']};border-radius:6px;padding:6px 16px;")
        btns.addStretch(); btns.addWidget(cancel); btns.addWidget(ok)
        lo.addLayout(btns)
        ok.clicked.connect(dlg.accept); cancel.clicked.connect(dlg.reject)
        if dlg.exec_() == QDialog.Accepted:
            cid = le_id.text().strip(); csec = le_sec.text().strip(); redir = le_red.text().strip()
            if cid and csec:
                self._sp_creds = {'client_id': cid, 'client_secret': csec, 'redirect_uri': redir}
                self.save_settings()
                self._connect_spotify(cid, csec, redir)

    def _connect_spotify(self, client_id, client_secret, redirect_uri):
        """Authenticate and connect Spotify client."""
        if not SPOTIPY_OK: return
        try:
            import spotipy
            from spotipy.oauth2 import SpotifyOAuth
            scope = "user-read-playback-state user-modify-playback-state user-read-currently-playing"
            auth = SpotifyOAuth(client_id=client_id, client_secret=client_secret,
                                redirect_uri=redirect_uri, scope=scope,
                                cache_path=os.path.join(os.path.dirname(self.settings_path), '.sp_cache'))
            self._sp_client = spotipy.Spotify(auth_manager=auth)
            # Test connection
            self._sp_client.current_playback()
            self.btn_spotify.setChecked(True)
            self.btn_spotify.setToolTip("Spotify: connected ✓\nRight-click to reconfigure")
            # Auto-enable in mini player if it's open
            if self._mini_active:
                self._mini.enable_spotify(self._sp_client)
        except Exception as ex:
            self._sp_client = None
            self.btn_spotify.setChecked(False)
            self.btn_spotify.setToolTip(f"Spotify error: {ex}")

    def toggle_spotify_mini(self):
        """Toggle Spotify mode in mini player."""
        if not SPOTIPY_OK:
            self.btn_spotify.setChecked(False)
            self._spotify_setup(); return
        if self.btn_spotify.isChecked():
            if not self._sp_client:
                # Try to reconnect with saved credentials
                creds = getattr(self, '_sp_creds', {})
                if creds.get('client_id'):
                    self._connect_spotify(creds['client_id'], creds['client_secret'],
                                          creds.get('redirect_uri','http://localhost:8888/callback'))
                else:
                    self.btn_spotify.setChecked(False)
                    self._spotify_setup(); return
            if self._sp_client:
                # Ensure mini player is open
                if not self._mini_active: self.toggle_mini()
                self._mini.enable_spotify(self._sp_client)
                self.btn_spotify.setToolTip("Spotify mini: ON  (right-click to configure)")
        else:
            if self._mini_active:
                self._mini.disable_spotify()
            self.btn_spotify.setToolTip("Spotify mini: OFF  (right-click to configure)")

    def toggle_discord(self):
        if not self._discord.is_enabled:
            ok=self._discord.enable()
            if ok:
                self.btn_discord.setActive(True); self.btn_discord.setToolTip("Discord: connected ✓")
                if 0<=self.current_index<len(self.selected_files):
                    self._discord.update(self._track_label(self.selected_files[self.current_index]))
            else: self.btn_discord.setToolTip("Discord unavailable — run Discord first & pip install pypresence")
        else:
            self._discord.disable(); self.btn_discord.setActive(False); self.btn_discord.setToolTip("Discord Rich Presence")
        self.save_settings()

    # ── Username ──────────────────────────────────────────
    def show_username_popup(self):
        pp=UsernamePopup(self._display_theme,self.username,self._lang_key,self)
        pp.saved.connect(self._on_uname)
        pp.move(self.user_btn.mapToGlobal(QPoint(0,self.user_btn.height()+4))); pp.show()
    def _on_uname(self,n): self.username=n[:12]; self.user_btn.username=self.username; self.user_btn.update(); self.save_settings()

    def show_eula(self):
        self._eula_win.set_theme(self._display_theme); self._eula_win.set_lang(self._lang_key)
        self._eula_win.show_centered(self.geometry())

    def _show_greeting(self):
        if self._greeting_shown: return
        self._greeting_shown=True
        self._greeting.theme=self._display_theme; self._greeting.lang_key=self._lang_key; self._greeting.username=self.username
        self._greeting.start()

    def _toggle_bar(self):
        self._top_bar.toggle()
        self.btn_hide.setToolTip("Show bar" if not self._top_bar.is_expanded() else "Hide bar")

    def _on_font(self,idx):
        global _current_font_index; _current_font_index=idx
        self.font_btn.update(); self.now_playing.update_font(); self.apply_theme(); self.save_settings()

    # ── Settings ──────────────────────────────────────────
    def load_settings(self):
        if not os.path.exists(self.settings_path): return
        try:
            with open(self.settings_path,encoding="utf-8") as f: d=json.load(f)
            files=[p for p in d.get("files",[]) if os.path.exists(p)]
            self.selected_files=files; self.liked_files=[p for p in d.get("liked_files",[]) if p in files]
            self._theme_key=d.get("theme","spotify"); self._lang_key=d.get("lang","RU")
            self._saved_volume=d.get("volume",70); self.username=d.get("username","")[:12]
            self._crossfade_enabled=d.get("crossfade",True)
            self._glow_enabled=d.get("glow",True)
            self._dynamic_bg_enabled=d.get("dynamic_bg",True)
            self._discord_enabled=d.get("discord_enabled",False)
            self.eq_state.bass=d.get("eq_bass",0.0); self.eq_state.mid=d.get("eq_mid",0.0); self.eq_state.treble=d.get("eq_treble",0.0)
            self._sp_creds = d.get('sp_creds', {})
            self._grid_mode=d.get("grid_mode",False)
            self._lyrics_visible=d.get("lyrics_visible",False)
            self._top_controls_in_settings=d.get("top_controls_in_settings",False)
            global _current_font_index; _current_font_index=d.get("font_index",0)
        except Exception: pass

    def save_settings(self):
        try:
            with open(self.settings_path,"w",encoding="utf-8") as f:
                json.dump({
                    "files":self.selected_files,"liked_files":self.liked_files,
                    "volume":self.volume_widget.value() if hasattr(self,"volume_widget") else 70,
                    "theme":self._theme_key,"lang":self._lang_key,"username":self.username,
                    "crossfade":self._crossfade_enabled,"dynamic_bg":self._dynamic_bg_enabled,"discord_enabled":self._discord.is_enabled,
                    "eq_bass":self.eq_state.bass,"eq_mid":self.eq_state.mid,"eq_treble":self.eq_state.treble,
                    "font_index":_current_font_index,"grid_mode":self._grid_mode,"lyrics_visible":self._lyrics_visible,
                    "top_controls_in_settings":self._top_controls_in_settings,
                    "sp_creds": getattr(self, '_sp_creds', {}),
                },f,ensure_ascii=False,indent=2)
        except Exception as ex: print(f"Settings error: {ex}")

    def closeEvent(self,e):
        self.save_settings()
        if self._discord.is_enabled: self._discord.disable()
        for attr in ("timer","sleep_timer","sleep_display_timer","_fade_timer","_bg_timer"):
            t=getattr(self,attr,None)
            if t: t.stop()
        super().closeEvent(e)

    def showEvent(self,e):
        super().showEvent(e)
        if hasattr(self,"_saved_volume"):
            self.volume_widget.setValue(self._saved_volume); self._on_volume_change(self._saved_volume); del self._saved_volume
        for lst in (self.file_list,self.fav_list): apply_smooth_scrollbar(lst,self._display_theme)
        for o in ("_drop_ov","_greeting"):
            if hasattr(self,o): getattr(self,o).setGeometry(0,0,self.width(),self.height())
        # Restore view modes
        if self._grid_mode:
            self.tabs.hide(); self.grid_view.show(); self.btn_grid.setActive(True)
        if self._lyrics_visible:
            self.lyrics_panel.show(); self._splitter.setSizes([580,320]); self.btn_lyrics.setActive(True)
        self.refresh_lists(); QTimer.singleShot(450,self._show_greeting)
        if self._discord_enabled:
            QTimer.singleShot(1000,self._try_restore_discord)
        # Auto-reconnect Spotify if credentials were saved
        if SPOTIPY_OK and self._sp_creds.get('client_id'):
            QTimer.singleShot(1500, self._try_restore_spotify)

    def _try_restore_discord(self):
        if not self._discord.is_enabled:
            ok=self._discord.enable()
            if ok: self.btn_discord.setActive(True); self.btn_discord.setToolTip("Discord: connected ✓")

    def _try_restore_spotify(self):
        """Silently reconnect Spotify with cached token on startup."""
        creds = self._sp_creds
        if creds.get('client_id'):
            try:
                import spotipy
                from spotipy.oauth2 import SpotifyOAuth
                scope = "user-read-playback-state user-modify-playback-state user-read-currently-playing"
                cache = os.path.join(os.path.dirname(self.settings_path), '.sp_cache')
                # Only reconnect if cache token exists (no browser popup on startup)
                if os.path.isfile(cache):
                    auth = SpotifyOAuth(
                        client_id=creds['client_id'],
                        client_secret=creds['client_secret'],
                        redirect_uri=creds.get('redirect_uri', 'http://localhost:8888/callback'),
                        scope=scope, cache_path=cache
                    )
                    self._sp_client = spotipy.Spotify(auth_manager=auth)
                    self._sp_client.current_playback()   # test
                    self.btn_spotify.setChecked(True)
                    self.btn_spotify.setToolTip("Spotify: connected ✓  (right-click to reconfigure)")
            except Exception:
                self._sp_client = None


# ═══════════════════════════════════════════════════════════
if __name__=="__main__":
    app=QApplication(sys.argv); app.setStyle("Fusion")
    w=MusicPlayer(); w.show(); sys.exit(app.exec_())
