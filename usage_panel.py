"""Hover card and local-only multi-task dashboard for the desktop pet."""
from datetime import date
from time import monotonic
from PySide6.QtCore import Qt, QTimer, QObject
from PySide6.QtGui import QGuiApplication, QFont, QCursor, QColor
from PySide6.QtWidgets import (QFrame, QDialog, QLabel, QPushButton, QVBoxLayout,
    QHBoxLayout, QComboBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QFormLayout, QDoubleSpinBox, QLineEdit, QDialogButtonBox)
from usage_metrics import merge, summarize


def words(owner, zh, en):
    return en if owner.settings.get('subtitle_language') == 'en' else zh


def phase_label(owner, phase):
    zh = dict(running='运行中', completed='已完成', interrupted='已中断', failed='失败', unknown='未知')
    en = dict(running='Running', completed='Completed', interrupted='Interrupted', failed='Failed', unknown='Unknown')
    return (en if owner.settings.get('subtitle_language') == 'en' else zh).get(phase, '—')


def tasks(owner):
    return owner.latest_status.get('tasks', [])


def selected_tasks(owner):
    selection = owner.settings.get('monitor_thread_id')
    return [s for s in tasks(owner) if not selection or s.get('task_id') == selection]


def scope_tasks(owner, scope):
    """Choose task files for each displayed accounting scope."""
    records = tasks(owner) if scope == 'all' else selected_tasks(owner)
    active = [s for s in records if s.get('active')]
    if scope == 'turn':
        records = active or sorted(records, key=lambda s: s.get('finished_at') or 0,
                                   reverse=True)[:1]
    elif scope == 'session':
        candidates = active or records
        records = sorted(candidates, key=lambda s: (s.get('started_at') if active else
                                                     s.get('finished_at')) or
                         s.get('started_at') or 0, reverse=True)[:1]
    return records, active


def money(value):
    if value is None:
        return '—'
    if value >= 1_000_000:
        return f'≈ ${value / 1_000_000:,.2f}M'
    if value >= 1_000:
        return f'≈ ${value / 1_000:,.2f}K'
    return f'≈ ${value:,.4f}'


def theme_from_image(image):
    """Use the dominant saturated colour of the transparent character sprite."""
    fallback = QColor('#182333')
    if image is None or image.isNull():
        return fallback
    bins = [[0.0, 0.0, 0.0, 0.0, 0.0] for _ in range(12)]
    step = max(1, min(image.width(), image.height()) // 65)
    for y in range(0, image.height(), step):
        for x in range(0, image.width(), step):
            color = image.pixelColor(x, y)
            saturation = color.hslSaturationF()
            hue = color.hslHueF()
            if color.alphaF() < 0.5 or saturation < 0.18 or hue < 0:
                continue
            weight = color.alphaF() * saturation * saturation
            bucket = bins[min(11, int(hue * 12))]
            bucket[0] += weight
            bucket[1] += hue * weight
            bucket[2] += saturation * weight
            bucket[3] += color.lightnessF() * weight
            bucket[4] += 1
    winner = max(bins, key=lambda bucket: bucket[0])
    if winner[0] == 0:
        return fallback
    hue = winner[1] / winner[0]
    saturation = min(0.75, max(0.35, winner[2] / winner[0]))
    lightness = min(0.68, max(0.22, winner[3] / winner[0]))
    return QColor.fromHslF(hue, saturation, lightness)


def contrast_text(background):
    def luminance(color):
        channels = [color.redF(), color.greenF(), color.blueF()]
        linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
                  for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
    background_luminance = luminance(background)
    white_contrast = 1.05 / (background_luminance + 0.05)
    dark_contrast = (background_luminance + 0.05) / (luminance(QColor('#101820')) + 0.05)
    return '#ffffff' if white_contrast >= dark_contrast else '#101820'


def duration(task):
    seconds = task.get('elapsed')
    if seconds is None:
        start, end = task.get('started_at'), task.get('finished_at')
        seconds = max(0, int(end - start)) if start and end else None
    return '—' if seconds is None else f'{seconds // 60:02d}:{seconds % 60:02d}'


class RatesDialog(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.setFont(QFont('Microsoft YaHei UI', 10))
        self.owner = owner
        self.setWindowTitle(words(owner, '模型参考价格', 'Model reference prices'))
        layout = QVBoxLayout(self)
        note = QLabel(words(owner,
            '手动填写每百万Token的美元参考价。仅作API等价估算，不是Codex订阅账单。\n'
            '默认按标准短上下文和30分钟缓存写入计价；不含12小时缓存、长上下文、处理档位、地区和工具费用。',
            'Enter USD per million tokens. API-equivalent estimate, not your Codex subscription bill.\n'
            'Defaults use standard short-context and 30m cache-write rates; 12h cache, long context, processing tiers, regions and tool fees are excluded.'))
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.model = QComboBox()
        self.model.setEditable(True)
        models = sorted({s.get('model') for s in tasks(owner) if s.get('model')}
                        | set(owner.settings.get('usage_prices', {})))
        self.model.addItems(models)
        form.addRow('Model', self.model)
        self.rates = {}
        for key, zh, en in [('input', '未缓存输入', 'Uncached input'), ('cached', '缓存输入', 'Cached input'),
                            ('write', '缓存写入', 'Cache write'), ('output', '输出（含推理）', 'Output (incl. reasoning)')]:
            field = QDoubleSpinBox()
            field.setDecimals(6)
            field.setRange(0, 1_000_000)
            self.rates[key] = field
            form.addRow(words(owner, zh, en), field)
        self.as_of = QLineEdit(date.today().isoformat())
        form.addRow(words(owner, '参考日期', 'As of'), self.as_of)
        layout.addLayout(form)
        self.notice = QLabel()
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        self.model.currentTextChanged.connect(self.load_rate)
        self.load_rate(self.model.currentText())
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.save_rate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(530, 330)

    def load_rate(self, model):
        rate = self.owner.settings.get('usage_prices', {}).get(model, {})
        for key, field in self.rates.items():
            field.setValue(rate.get(key, 0))
        self.as_of.setText(rate.get('as_of', date.today().isoformat()))
        self.notice.setText(words(self.owner, '未配置的模型不会自动套用其他模型价格。',
                                  'Unconfigured models never inherit another model’s rates.'))

    def save_rate(self):
        model = self.model.currentText().strip()
        try:
            date.fromisoformat(self.as_of.text())
            if not model:
                raise ValueError()
        except ValueError:
            self.notice.setText(words(self.owner, '请填写模型名及 YYYY-MM-DD 日期。',
                                      'Enter a model name and YYYY-MM-DD date.'))
            return
        self.owner.settings.setdefault('usage_prices', {})[model] = dict(
            **{key: field.value() for key, field in self.rates.items()}, as_of=self.as_of.text())
        self.owner.save_position()
        self.accept()


class HoverCard(QFrame):
    def __init__(self, owner):
        super().__init__(owner, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setFont(QFont('Microsoft YaHei UI', 10))
        self.owner = owner
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setObjectName('usageHoverCard')
        self.theme_pet = object()
        self.apply_theme()
        layout = QVBoxLayout(self)
        self.title = QLabel()
        layout.addWidget(self.title)
        self.scope = QComboBox()
        self.scope.addItems(['', '', '', ''])
        self.scope.currentIndexChanged.connect(self.refresh)
        layout.addWidget(self.scope)
        self.cost = QLabel()
        self.cost.setObjectName('costLabel')
        self.cost.setWordWrap(True)
        layout.addWidget(self.cost)
        self.body = QLabel()
        self.body.setTextFormat(Qt.PlainText)
        self.body.setWordWrap(True)
        layout.addWidget(self.body)
        self.more = QPushButton()
        self.more.clicked.connect(owner.open_usage_dashboard)
        layout.addWidget(self.more)
        self.setFixedWidth(365)

    def apply_theme(self):
        pet_name = getattr(self.owner, 'pet_name', None)
        if pet_name == self.theme_pet:
            return
        self.theme_pet = pet_name
        image = self.owner.current_image() if hasattr(self.owner, 'current_image') else None
        background = theme_from_image(image)
        foreground = contrast_text(background)
        control = background.lighter(125) if foreground == '#ffffff' else background.darker(125)
        control_foreground = contrast_text(control)
        self.setStyleSheet(
            f'QFrame#usageHoverCard {{background:{background.name()}; color:{foreground}; border-radius:12px;}} '
            f'QLabel {{color:{foreground}; padding:3px;}} '
            f'QLabel#costLabel {{font-size:14px; font-weight:600; color:{foreground};}} '
            f'QPushButton,QComboBox {{padding:6px; color:{control_foreground}; background:{control.name()};}} '
            f'QComboBox QAbstractItemView {{color:{control_foreground}; background:{control.name()};}}')

    def refresh(self):
        owner = self.owner
        self.apply_theme()
        labels = [('本次运行 / 最近一次', 'Current / latest run'),
                  ('当前/最近会话累计', 'Current / latest session'),
                  ('今日（本地时区）', 'Today (local time)'), ('全部本地任务', 'All local tasks')]
        for i, pair in enumerate(labels):
            self.scope.setItemText(i, words(owner, *pair))
        scope = ('turn', 'session', 'today', 'all')[max(0, self.scope.currentIndex())]
        records, active = scope_tasks(owner, scope)
        self.title.setText(words(owner, f'运行任务：{len(active)}', f'Running tasks: {len(active)}'))
        bucket = 'session' if scope == 'all' else scope
        value = summarize(merge([s.get('usage', {}).get(bucket, {}) for s in records]), owner.settings.get('usage_prices', {}))
        partial = bool(value['unpriced'])
        loading = scope == 'all' and owner.latest_status.get('loading')
        cost_title = words(owner, '已定价部分估算' if partial else '已读取部分估价' if loading else '预计总花费',
                           'Priced subtotal' if partial else 'Known estimate so far' if loading else 'Estimated total')
        self.cost.setText(cost_title + '  ' + money(value['cost_usd']))
        observed = any(s.get('usage', {}).get('observed') for s in records)
        text = words(owner,
            f'输入（含缓存）  {value["input_tokens"]:,}\n缓存输入  {value["cached_input_tokens"]:,}\n缓存写入  {value["cache_write_input_tokens"]:,}\n输出  {value["output_tokens"]:,}\n',
            f'Input (incl. cache)  {value["input_tokens"]:,}\nCached input  {value["cached_input_tokens"]:,}\nCache writes  {value["cache_write_input_tokens"]:,}\nOutput  {value["output_tokens"]:,}\n') if observed else words(owner, '暂未收到可用用量数据\n', 'No usable usage data yet\n')
        if owner.latest_status.get('loading'):
            text = words(owner, '正在读取历史日志，统计尚不完整\n', 'Loading history; totals are incomplete\n') + text
        if active:
            text += words(owner, '运行时长（最长） ', 'Longest running ') + duration(max(active, key=lambda s: s.get('elapsed') or 0)) + '\n'
        if value['unpriced']:
            text += words(owner, '未配置参考价：', 'Missing rates: ') + ', '.join(value['unpriced']) + '\n'
        if any(not s.get('usage', {}).get('complete', True) for s in records):
            text += words(owner, '部分用量无法归属：仅显示可确认记录\n', 'Partial accounting: attributable records only\n')
        dates = sorted({r.get('as_of', '?') for r in owner.settings.get('usage_prices', {}).values()})
        text += words(owner, '价格日期：', 'Rates as of: ') + (', '.join(dates) if dates else '—') + '\n'
        text += words(owner, '固定参考价 · 非账单 · 不含额外费用', 'Flat reference rates · Not a bill · Extras excluded')
        self.body.setText(text)
        self.more.setText(words(owner, '任务面板 / 配置价格', 'Tasks / Configure prices'))
        self.adjustSize()

    def reveal(self):
        self.refresh()
        screen = QGuiApplication.screenAt(self.owner.geometry().center()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        x = self.owner.x() - self.width() - 8
        if x < area.left():
            x = self.owner.x() + self.owner.width() + 8
        self.move(max(area.left(), min(x, area.right() - self.width() + 1)),
                  max(area.top(), min(self.owner.y(), area.bottom() - self.height() + 1)))
        self.show()


class TaskDashboard(QDialog):
    def __init__(self, owner):
        super().__init__(owner, Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setFont(QFont('Microsoft YaHei UI', 10))
        self.owner = owner
        self.setWindowTitle(words(owner, '桌宠任务面板', 'Deskpet tasks'))
        layout = QVBoxLayout(self)
        self.notice = QLabel()
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels([words(owner, '匿名任务', 'Task'), words(owner, '状态', 'Status'),
            'Model', words(owner, '本次Tokens', 'Run tokens'), words(owner, '估算USD', 'Est. USD'), words(owner, '耗时', 'Duration')])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(True)
        for column, width in enumerate((120, 90, 150, 110, 105)):
            self.table.setColumnWidth(column, width)
        layout.addWidget(self.table)
        row = QHBoxLayout()
        for zh, en, callback in [('跟踪选中任务', 'Follow selected', self.follow),
                                 ('跟踪全部', 'Follow all', lambda: owner.follow_task(None)),
                                 ('参考价格', 'Reference prices', self.prices),
                                 ('找回桌宠', 'Recover pet', owner.recover_pet)]:
            button = QPushButton(words(owner, zh, en))
            button.clicked.connect(callback)
            row.addWidget(button)
        layout.addLayout(row)
        self.timer = QTimer(self)
        self.timer.setInterval(3000)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.resize(780, 460)
        self.refresh()

    def refresh(self):
        if not self.isVisible() and self.table.rowCount():
            return
        selected = self.table.item(self.table.currentRow(), 0)
        selected_id = selected.data(Qt.UserRole) if selected else None
        records = sorted(tasks(self.owner), key=lambda s: (bool(s.get('active')), s.get('started_at') or 0), reverse=True)
        total_records = len(records)
        records = records[:100]
        prices = self.owner.settings.get('usage_prices', {})
        rows = []
        for i, task in enumerate(records):
            identity = task.get('task_id', '')
            value = summarize(task.get('usage', {}).get('turn', {}), prices)
            columns = ['Task ' + identity[-8:], phase_label(self.owner, task.get('phase')), task.get('model') or '—',
                       f'{value["total_tokens"]:,}' if task.get('usage', {}).get('observed') else '—',
                       money(value['cost_usd']), duration(task)]
            if not task.get('usage', {}).get('complete', True):
                columns[3] += ' *'
                columns[4] += ' *'
            elif value['unpriced']:
                columns[4] += ' *'
            rows.append((identity, columns))
        signature = (self.owner.settings.get('monitor_thread_id'),
                     bool(self.owner.latest_status.get('loading')),
                     tuple((identity, tuple(columns)) for identity, columns in rows))
        if signature == getattr(self, '_last_signature', None):
            return
        self._last_signature = signature
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(len(rows))
        for i, (identity, columns) in enumerate(rows):
            for j, content in enumerate(columns):
                item = QTableWidgetItem(content)
                if j == 0:
                    item.setData(Qt.UserRole, identity)
                self.table.setItem(i, j, item)
            if identity == selected_id:
                self.table.selectRow(i)
        self.table.setUpdatesEnabled(True)
        target = self.owner.settings.get('monitor_thread_id')
        scope = 'Task ' + target[-8:] if target else words(self.owner, '全部本地任务', 'All local tasks')
        clipped = words(self.owner, f'（显示最近 {len(records)} / {total_records} 项）',
                        f' (showing latest {len(records)} of {total_records})') if total_records > len(records) else ''
        self.notice.setText(words(self.owner, '跟踪：', 'Following: ') + scope + clipped + '\n' + words(self.owner,
            '仅本机日志，不展示正文。* 表示统计不完整或有模型缺价；无结束事件不能判定完成。价格是固定参考价估算，非账单。',
            'Local logs only; no text. * = partial accounting or missing model rates. Missing end events remain unresolved. Flat-rate estimates, NOT a bill.'))

    def follow(self):
        item = self.table.item(self.table.currentRow(), 0)
        if item:
            self.owner.follow_task(item.data(Qt.UserRole))
            self.refresh()

    def prices(self):
        RatesDialog(self.owner).exec()
        self.refresh()


class UsageController(QObject):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.card = HoverCard(owner)
        self.dashboard = None
        self.hover = QTimer(self)
        self.hover.setSingleShot(True)
        self.hover.setInterval(500)
        self.hover.timeout.connect(self.reveal)
        self.check = QTimer(self)
        self.check.setInterval(250)
        self.check.timeout.connect(self.tick)
        self.check.start()
        self.outside_since = None
        self.last_refresh = 0.0

    def reveal(self):
        if self.owner.isVisible() and self.owner.underMouse() and not self.owner.drag:
            self.card.reveal()
            self.outside_since = None
            self.last_refresh = monotonic()

    def tick(self):
        if not self.owner.isVisible() or self.owner.drag:
            self.card.hide()
            self.outside_since = None
            return
        if self.card.isVisible():
            now = monotonic()
            popup_open = self.card.scope.view().isVisible()
            # Include the gap between the pet and card so the cursor can reach its controls.
            hover_area = self.owner.frameGeometry().united(self.card.frameGeometry())
            if popup_open or hover_area.contains(QCursor.pos()):
                self.outside_since = None
            elif self.outside_since is None:
                self.outside_since = now
            elif now - self.outside_since >= 1.5:
                self.card.hide()
                self.outside_since = None
                return
            if not popup_open and now - self.last_refresh >= 1.0:
                self.card.refresh()
                self.last_refresh = now

    def open_dashboard(self):
        self.card.hide()
        language = self.owner.settings.get('subtitle_language')
        if self.dashboard is not None and getattr(self, 'dashboard_language', None) != language:
            self.dashboard.close()
            self.dashboard.deleteLater()
            self.dashboard = None
        if self.dashboard is None:
            self.dashboard = TaskDashboard(self.owner)
            self.dashboard_language = language
        self.dashboard.show()
        self.dashboard.refresh()
        self.dashboard.raise_()
