RU-РУ

Техническая архитектура ReturnalAudio — это хорошо продуманный, модульный и анимационно-ориентированный PyQt5-проект. Вот подробный обзор:
1. Общая структура приложения

Главный класс: MusicPlayer(QWidget) — наследует от QWidget и содержит всю логику плеера.
Приложение полностью однооконное и безрамочное (Qt.FramelessWindowHint + WA_TranslucentBackground).
Используется кастомная отрисовка многих элементов вместо стандартных виджетов Qt.

2. Архитектура по слоям
UI-слой (Кастомные виджеты)
Большая часть интерфейса построена на полностью кастомных классах:

NowPlayingWidget — анимированная смена текста (слайд + fade с помощью QParallelAnimationGroup)
RunningSlider — прогресс-бар с анимированной бегущей фигуркой (рисуется вручную в paintEvent, использует QTimer для 60 fps)
VolumeSlider — вертикальный слайдер с градиентом и иконкой динамика
PlayPauseButton — кнопка с морфингом иконки (play ↔ pause) через свойство morph
IconButton — универсальная кнопка с разными иконками, hover- и press-анимациями
SpeedButton + SpeedPopup, SleepButton + SleepPopup
TrackRowWidget — строка трека с кнопками like/delete (появляются по hover)
DropOverlay, EulaWindow, CustomTitleBar, LogoWidget

Все виджеты активно используют:

paintEvent + QPainter (Antialiasing)
QPropertyAnimation, QParallelAnimationGroup, QSequentialAnimationGroup
QGraphicsOpacityEffect
Собственные свойства (@pyqtProperty)

Тема-система

Словарь THEMES с 9 пресетами (тёмные + светлые)
Функции blend_color() и blend_theme() для плавной анимации смены темы
apply_theme() запускает QVariantAnimation, которая интерполирует цвета между старой и новой темой

Языковая система

Словарь LANGUAGES + LANG_ORDER
Метод tr() и apply_theme() обновляют все тексты при смене языка

3. Аудио-часть

Основной плеер: QMediaPlayer
Второй плеер для кроссфейда (_fade_player)
Реализация кроссфейда через таймер (_crossfade_tick) с ease-функцией
Поддержка метаданных через mutagen (artist — title)
QTimer (200 мс) для обновления прогресса

4. Управление состоянием

selected_files — основной плейлист
liked_files — избранное
current_index — текущий трек
shuffle, repeat_mode, shuffle_history
Автосохранение в player_settings.json (JSON)

5. Взаимодействие и события

Drag & Drop реализован вручную (dragEnterEvent, dropEvent) с рекурсивным обходом папок
Горячие клавиши через QShortcut
Двойной клик по треку → воспроизведение
Hover-логика в списках через TrackRowWidget
Sleep timer на базе QTimer

6. Технические решения и стиль кода
Плюсы архитектуры:

Очень высокая кастомизация UI (почти всё рисуется вручную)
Хорошая анимационная система
Чёткое разделение на мелкие виджеты
Плавные переходы между состояниями
Минимальное использование стандартных виджетов Qt (кроме списков и табов)

Особенности / нюансы:

Большой объём кода в одном файле (~1740 строк) — монолитный подход
Много paintEvent → высокая нагрузка на отрисовку, но выглядит очень красиво
Активное использование таймеров (QTimer) для анимаций вместо чистого declarative подхода
Нет разделения на Model / View / Controller в классическом смысле (всё в MusicPlayer)

МОЯ СИСТЕМА

ReturnalAudio разработан и протестирован на следующей системе:

Оборудование
CPU: AMD Ryzen 5 5600G
GPU: AMD Radeon RX 7700 XT
ОЗУ: 32 ГБ DDR4
Накопитель: NVMe SSD
Программная среда
Операционная система: Windows 11 (основная платформа разработки и тестирования)
Python: 3.13.12
Основной фреймворк: PyQt5
Дополнительные библиотеки: mutagen (для чтения метаданных аудио)
Инструменты разработки: Visual Studio Code + расширение Python 

Вся информация актуальна для последней версии Returnal Audio, то есть для беты

ENG-АНГЛ

Technical Architecture of ReturnalAudio
ReturnalAudio features a well-thought-out, modular, and animation-focused PyQt5 architecture. Here is a detailed overview:
1. Overall Application Structure

Main class: MusicPlayer(QWidget) — inherits from QWidget and contains all the player logic.
The application is a single-window, frameless app (Qt.FramelessWindowHint + WA_TranslucentBackground).
Many interface elements use custom painting instead of standard Qt widgets.

2. Layered Architecture
UI Layer (Custom Widgets)
Most of the interface is built using fully custom widget classes:

NowPlayingWidget — animated text switching (slide + fade using QParallelAnimationGroup)
RunningSlider — progress bar with an animated running figure (drawn manually in paintEvent, uses QTimer for ~60 fps)
VolumeSlider — vertical volume slider with gradient and speaker icon
PlayPauseButton — button with icon morphing (play ↔ pause) via the morph property
IconButton — universal button with different icons, hover and press animations
SpeedButton + SpeedPopup, SleepButton + SleepPopup
TrackRowWidget — track row with like/delete buttons (appear on hover)
DropOverlay, EulaWindow, CustomTitleBar, LogoWidget

All widgets heavily utilize:

paintEvent + QPainter (with Antialiasing)
QPropertyAnimation, QParallelAnimationGroup, QSequentialAnimationGroup
QGraphicsOpacityEffect
Custom properties (@pyqtProperty)

Theme System

THEMES dictionary with 9 presets (dark + light)
blend_color() and blend_theme() functions for smooth theme transition animation
apply_theme() launches QVariantAnimation, which interpolates colors between the old and new theme

Language System

LANGUAGES dictionary + LANG_ORDER
tr() method and apply_theme() update all texts when the language changes

3. Audio Engine

Main player: QMediaPlayer
Secondary player for crossfade (_fade_player)
Crossfade implemented via a timer (_crossfade_tick) with an easing function
Metadata support via mutagen (artist — title)
QTimer (200 ms) for progress updates

4. State Management

selected_files — main playlist
liked_files — favorites
current_index — currently playing track
shuffle, repeat_mode, shuffle_history
Automatic saving to player_settings.json (JSON format)

5. Interaction and Events

Drag & Drop implemented manually (dragEnterEvent, dropEvent) with recursive folder scanning
Keyboard shortcuts via QShortcut
Double-click on a track starts playback
Hover logic in lists handled by TrackRowWidget
Sleep timer based on QTimer

6. Technical Decisions and Code Style
Strengths of the architecture:

Extremely high UI customization (almost everything is hand-drawn)
Excellent animation system
Clear breakdown into small, focused widgets
Smooth state transitions
Minimal use of standard Qt widgets (except for lists and tabs)

Notable aspects:

Large amount of code in a single file (~1740 lines) — monolithic approach
Heavy use of paintEvent — high rendering load, but results in a very beautiful look
Active use of QTimer for animations instead of a purely declarative approach
No classic Model/View/Controller separation (everything lives inside MusicPlayer)

MY SYSTEM

**ReturnalAudio** is developed and tested on the following system:

### Hardware
- **CPU**: AMD Ryzen 5 5600G 
- **GPU**: AMD Radeon RX 7700 XT 
- **RAM**: 32 GB DDR4
- **Storage**: NVMe SSD 


### Software Environment
- **Operating System**: Windows 11 (primary development and testing platform)
- **Python**: 3.13.12
- **Main framework**: PyQt5
- **Additional libraries**: mutagen (for reading audio metadata)
- **Development tools**: Visual Studio Code + Python extension

All information is relevant for the latest version of Returnal Audio, i.e., for the beta

<img width="2559" height="1358" alt="зображення" src="https://github.com/user-attachments/assets/9fcf4381-8401-4efd-9ea5-599a257dd507" />

