#!/usr/bin/env python3
"""
trace.py — механическая часть проверок dev-cycle.

Читает модель (обычно docs/model/) и ничего в ней не меняет, кроме
сводной таблицы INDEX.md по команде `index --write`.

Команды:
  check                 трассировка и покрытие — на каждом останове
  next-id KIND [-n N]   следующий свободный номер: USRSTR, NFR, RISK, Q,
                        USRSTR-3/ACPT, AGGR-2/TR ...
  impact ID [ID ...]    что держится на этих номерах (change, answer,
                        правка на верхнем шаге)
  index [--write]       сводная таблица номеров для INDEX.md
  tests [--slice SLUG]  покрытие критериев и НФТ тестами и ручной приёмкой
  status                статусы шагов, вопросы, предположения, риски

Какая модель: --model, затем файл .dev-cycle в корне рабочего дерева,
затем единственная docs/model в дереве.

Коды выхода: 0 — чисто, 1 — есть ошибки (или непокрытые критерии
для tests), 2 — модель не определена или неверный вызов.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

# ------------------------------------------------------------------ номера

KIND_ORDER = ['USRSTR', 'ACPT', 'NFR', 'ROLE', 'AGGR', 'REL', 'PROC',
              'ST', 'ATTR', 'TR', 'MTR', 'DES', 'TASK', 'RISK', 'Q']
STEP = {'USRSTR': 1, 'ACPT': 2, 'NFR': 2, 'ROLE': 3, 'AGGR': 3, 'REL': 4,
        'PROC': 5, 'ST': 6, 'ATTR': 6, 'TR': 7, 'MTR': 8, 'DES': 9,
        'TASK': 10, 'RISK': None, 'Q': None}
NEEDS_REFS = {'NFR', 'ROLE', 'AGGR', 'REL', 'PROC', 'ATTR', 'TR', 'MTR',
              'DES', 'TASK'}
NESTED = {'ACPT': 'USRSTR', 'ST': 'AGGR', 'ATTR': 'AGGR', 'TR': 'AGGR'}

ID_CORE = (r'(?:USRSTR-\d+(?:/ACPT-\d+)?'
           r'|AGGR-\d+(?:/(?:ST|ATTR|TR)-\d+)?'
           r'|(?:ROLE|REL|PROC|DES|MTR|NFR|TASK|RISK|Q)-\d+)')
REF_RE = re.compile(r'(?<![\w/-])(?:(?P<model>[A-Za-z0-9][\w.-]*):)?'
                    r'(?P<id>' + ID_CORE + r')(?!\w)')
LEAD_ID_RE = re.compile(r'^[\s>*_`\[]*(' + ID_CORE + r')(?![\w/])')

HEAD_RE = re.compile(r'^(#{1,6})\s+(.*?)\s*#*\s*$')
ROW_RE = re.compile(r'^\s*\|(.*)\|\s*$')
SEP_RE = re.compile(r'^\s*\|?(\s*:?-{3,}:?\s*\|)+\s*(:?-{3,}:?\s*)?\|?\s*$')
ITEM_RE = re.compile(r'^(\s*)[-*+]\s+(?:\[([ xX])\]\s+)?(.*)$')
STATE_RE = re.compile(r'(?i)(удал[её]н\w*|отмен[её]н\w*|снят\w*|отклон[её]н\w*)\s+(\d{4}-\d{2}-\d{2})')
STATE_WORD_RE = re.compile(r'(?i)^(удал|отмен|снят|отклон)')
SEC_STATE_RE = re.compile(r'(?i)(удал[её]нн|отмен[её]нн|сняты|отклон[её]нн)')
DATE_RE = re.compile(r'\d{4}-\d{2}-\d{2}')
CLOSER_RE = re.compile(r'(?i)чем\s+закрыт\W{0,3}[:：]\W{0,3}(.*\S)')
STATUS_RE = re.compile(r'(?i)^>?\s*статус\s*:\s*(.+?)\s*$')
TREAT_LINE_RE = re.compile(r'(?i)обработка\W{0,3}[:：]\W{0,3}(.*\S)')
TREAT_KW = ('критерием', 'решением', 'вне модели', 'принят')
Q_CLOSED_RE = re.compile(r'(?i)закрыт\w*\s+\d{4}-\d{2}-\d{2}')
STRIP_RE = re.compile(r'^[\s>*_`+\-–]+')
CODE_SPAN_RE = re.compile(r'`[^`]*`')
VERIFIES_RE = re.compile(r'(?i)verifies\s*:\s*(.*)')

CODE_EXT = {'.go', '.py', '.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs',
            '.java', '.kt', '.kts', '.cs', '.rb', '.php', '.rs', '.swift',
            '.scala', '.sql', '.sh', '.feature', '.ex', '.exs', '.clj',
            '.dart', '.c', '.cc', '.cpp', '.h', '.hpp', '.m', '.vue',
            '.svelte', '.lua', '.pl', '.r'}
SKIP_DIRS = {'.git', 'node_modules', 'vendor', 'dist', 'build', 'target',
             '.venv', 'venv', '__pycache__', '.idea', '.vscode', '.claude',
             '.gradle', 'obj', '.next', '.terraform'}


def kind(i: str) -> str:
    return i.split('/', 1)[1].split('-')[0] if '/' in i else i.split('-')[0]


def parent(i: str):
    return i.split('/', 1)[0] if '/' in i else None


def sort_key(i: str):
    k = kind(i)
    return (KIND_ORDER.index(k) if k in KIND_ORDER else 99,
            [int(n) for n in re.findall(r'\d+', i)])


def refs_in(text: str):
    return [(m.group('model'), m.group('id')) for m in REF_RE.finditer(text)]


def up_refs(text: str):
    """Ссылки вверх: номер вопроса рядом со стрелкой — пометка ‼, а не ссылка."""
    return [(m, i) for m, i in refs_in(text) if kind(i) != 'Q']


def ids_in(text: str):
    return [m.group('id') for m in REF_RE.finditer(text) if not m.group('model')]


def clean(s: str) -> str:
    return s.strip().strip('*_` ').strip()


def lead_id(s: str):
    m = LEAD_ID_RE.match(s)
    return m.group(1) if m else None


def title_after(text: str, id_: str) -> str:
    t = text.split(id_, 1)[1] if id_ in text else text
    t = re.sub(r'^[\s*_`.:\-—–)]+', '', t).strip().strip('*_`').strip()
    return t


def split_cells(inner: str):
    return [c.strip() for c in re.split(r'(?<!\\)\|', inner)]


def mentions(text: str, id_: str) -> bool:
    return re.search(r'(?<![\w/-])' + re.escape(id_) + r'(?![\w/])', text) is not None


def norm_state(word: str) -> str:
    w = word.lower().strip('*_` ')
    for k, v in (('удал', 'удалено'), ('отмен', 'отменено'),
                 ('снят', 'снято'), ('отклон', 'отклонено')):
        if w.startswith(k):
            return v
    return 'удалено'


STATE_SHORT = {'удалено': 'удалён', 'отменено': 'отменён', 'снято': 'снят', 'отклонено': 'отклонён'}


def state_text(st) -> str:
    return f'{STATE_SHORT.get(st[0], st[0])} {st[1]}'.strip()


def short(s: str, n: int = 70) -> str:
    s = ' '.join(s.split())
    return s if len(s) <= n else s[:n - 1] + '…'


# ------------------------------------------------------------------ модель

class El:
    def __init__(self, id_, rel, line, title='', section=0):
        self.id = id_
        self.kind = kind(id_)
        self.file = rel
        self.line = line
        self.title = title
        self.section = (rel, section)  # в каком разделе файла определён
        self.refs = []          # [(model|None, id)]
        self.affected = []      # ⚠ — для рисков
        self.treatment = None   # «Обработка:» риска
        self.closer = None      # «Чем закрыт:» риска
        self.assume = []        # [(Q|None, line)]
        self.state = None       # ('удалено'|'отменено'|'снято'|'отклонено', дата или '')
        self.text = []
        self.fields = {}        # заголовок столбца -> ячейка (для строк таблиц)
        self.done = None        # для задач: отмечена ли [x]

    @property
    def active(self):
        return self.state is None

    def internal_refs(self):
        return [r for m, r in self.refs if not m]


def home_kinds(rel: str):
    parts = rel.split('/')
    if parts[0] == 'specs':
        return None
    if parts[0] == 'user-stories':
        return {'USRSTR', 'ACPT'}
    if parts[0] == 'processes':
        return {'PROC'}
    if parts[0] == 'lifecycles':
        return {'TR'}
    if parts[0] == 'slices' and len(parts) == 3:
        return {'design.md': {'DES'}, 'tasks.md': {'TASK'}}.get(parts[2], set())
    return {
        'roles-and-aggregates.md': {'ROLE', 'AGGR'},
        'relations.md': {'REL'},
        'states-and-attributes.md': {'ST', 'ATTR'},
        'metrics.md': {'MTR'},
        'nfr.md': {'NFR'},
        'risks.md': {'RISK'},
    }.get(rel, set())


TITLE_KEYS = ('назван', 'имя', 'роль', 'агрегат', 'атрибут', 'требован', 'состояни',
              'событ', 'риск', 'метрик', 'вопрос')


def pick_title(cells, header):
    for key in TITLE_KEYS:
        hit = next((i for i, h in enumerate(header) if key in h and 0 < i < len(cells)), None)
        if hit:
            return clean(cells[hit])
    if len(header) > 2 and len(cells) > 2 and header[1] in ('от', 'из') and header[2] in ('к', 'в'):
        return f'{clean(cells[1])} → {clean(cells[2])}'
    hit = next((i for i, h in enumerate(header) if 'описан' in h and 0 < i < len(cells)), None)
    if hit:
        return clean(cells[hit])
    return clean(cells[1]) if len(cells) > 1 else ''


class Model:
    def __init__(self, root: Path):
        self.root = root
        self.els = {}
        self.dups = []
        self.qs = {}                      # Q -> {'line', 'closed'}
        self.rindex = defaultdict(set)    # Q -> ids
        self.index_rows = {}              # id -> строка сводной таблицы
        self.index_sections = defaultdict(list)
        self.has_index = False
        self.has_block = False
        self.has_steps_block = False
        self.stray = []                   # ‼ вне элемента
        self.mention_list = []            # (model, id, rel, line)
        self.status = {}                  # rel -> строка статуса | None
        self.file_text = {}
        self.all_ids = set()
        self._parse()

    def define(self, id_, rel, line, title='', section=0):
        el = El(id_, rel, line, title, section)
        if id_ in self.els:
            self.dups.append((id_, self.els[id_], el))
        else:
            self.els[id_] = el
        return el

    # -------------------------------------------------------- разбор файлов
    def _parse(self):
        files = sorted(p for p in self.root.rglob('*.md') if p.is_file())
        for p in files:
            rel = p.relative_to(self.root).as_posix()
            text = p.read_text(encoding='utf-8', errors='replace')
            for i in ids_in(text) + ids_in(p.name):
                self.all_ids.add(i)
                if parent(i):
                    self.all_ids.add(parent(i))
            if rel == 'INDEX.md':
                self.has_index = True
                self._parse_index(text)
                continue
            kinds = home_kinds(rel)
            if kinds is None:       # specs/ — представления, не источник
                continue
            self.file_text[rel] = text
            first = next((ln for ln in text.splitlines() if ln.strip()), '')
            m = STATUS_RE.match(first.strip())
            self.status[rel] = m.group(1) if m else None
            self._parse_file(rel, p.name, text, kinds)

    def _parse_file(self, rel, name, text, kinds):
        stack = []            # [уровень, El]
        file_el = None
        if rel.startswith('user-stories/'):
            m = re.match(r'(USRSTR-\d+)(?!\d)', name)
            if m:
                file_el = self.define(m.group(1), rel, 1)
                stack.append([0, file_el])
        pending = file_el     # элемент, чья первая строка может быть пометкой удаления
        header = None
        tbl = 'none'
        in_code = False
        sec = 0               # строка последнего заголовка: раздел файла
        sec_state, sec_level = None, 0   # раздел «Удалённые», «Снятые»…

        for n, raw in enumerate(text.splitlines(), 1):
            line = raw.rstrip()
            s = line.strip()
            if s.startswith('```') or s.startswith('~~~'):
                in_code = not in_code
                continue
            if in_code:
                continue
            if not s:
                tbl, header = 'none', None
                continue

            for mm, i in refs_in(line):
                self.mention_list.append((mm, i, rel, n))

            # ---- заголовок
            h = HEAD_RE.match(line)
            if h:
                tbl, header = 'none', None
                level, htext = len(h.group(1)), h.group(2)
                sec = n
                if sec_state and level <= sec_level:
                    sec_state = None
                ss = SEC_STATE_RE.search(htext)
                if ss:
                    sec_state, sec_level = norm_state(ss.group(1)), level
                while stack and stack[-1][0] >= level:
                    stack.pop()
                lid = lead_id(htext)
                if file_el is not None and lid == file_el.id:
                    file_el.title = file_el.title or title_after(htext, lid)
                    pending = file_el
                    continue
                if lid and kind(lid) in kinds:
                    el = self.define(lid, rel, n, title_after(htext, lid), sec)
                    if sec_state:
                        el.state = (sec_state, '')
                    stack.append([level, el])
                    pending = el
                else:
                    if file_el is not None and not file_el.title and not lid:
                        file_el.title = clean(htext)
                    pending = None
                continue

            cur = stack[-1][1] if stack else None

            # ---- таблица
            r = ROW_RE.match(line)
            if r:
                if SEP_RE.match(line):
                    tbl = 'body'
                    continue
                cells = split_cells(r.group(1))
                if tbl == 'none':
                    header = [clean(c).lower() for c in cells]
                    tbl = 'header'
                    continue
                if tbl == 'body':
                    lid = lead_id(clean(cells[0]))
                    if lid and kind(lid) in kinds:
                        known = self.els.get(lid)
                        # номер, уже определённый в другом разделе, — упоминание
                        # (сводки «Необработанные», «Чего не хватает» и т.п.)
                        if known is None or known.section == (rel, sec):
                            self._table_el(lid, rel, n, line, cells, header, sec, sec_state)
                            continue
                # строка чужой таблицы — обычный текст текущего элемента
            else:
                tbl, header = 'none', None

            # ---- пункт списка, открывающий элемент
            it = ITEM_RE.match(line)
            if it and not r:
                body = it.group(3)
                lid = lead_id(body)
                if lid and kind(lid) in kinds and self._is_list_def(body, lid) \
                        and lid not in self.els:
                    lvl = 7 + len(it.group(1))
                    while stack and stack[-1][0] >= lvl:
                        stack.pop()
                    el = self.define(lid, rel, n, title_after(body, lid), sec)
                    if it.group(2) is not None:
                        el.done = it.group(2).lower() == 'x'
                    sm = STATE_RE.search(body)
                    if sm:
                        el.state = (norm_state(sm.group(1)), sm.group(2))
                    elif sec_state:
                        el.state = (sec_state, '')
                    el.text.append(line)
                    stack.append([lvl, el])
                    pending = el
                    continue

            # ---- обычная строка
            if pending is not None:
                sm = STATE_RE.match(STRIP_RE.sub('', s))
                if sm:
                    pending.state = (norm_state(sm.group(1)), sm.group(2))
                pending = None
            if cur is not None:
                cur.text.append(line)
                st = STRIP_RE.sub('', s)
                if '←' in s:
                    cur.refs += up_refs(s.split('←', 1)[1])
                elif st.startswith('→'):
                    cur.refs += up_refs(st[1:])
                if '⚠' in s:
                    cur.affected += ids_in(s.split('⚠', 1)[1])
                tm = TREAT_LINE_RE.search(s)
                if tm:
                    cur.treatment = clean(tm.group(1))
                cm = CLOSER_RE.search(s)
                if cm:
                    cur.closer = clean(cm.group(1))
            bare = CODE_SPAN_RE.sub('', s)    # ‼ в `коде` — подсказка шаблона, не пометка
            if '‼' in bare:
                qs = [i for i in ids_in(bare) if kind(i) == 'Q']
                if cur is not None:
                    cur.assume += [(q, n) for q in (qs or [None])]
                else:
                    self.stray.append((rel, n))

    @staticmethod
    def _is_list_def(body, lid):
        rest = body.split(lid, 1)[1] if lid in body else ''
        rest = rest.lstrip('*_`')
        return bool(re.match(r'^\s*[.:)]|^\s+[—–-]\s', rest))

    def _table_el(self, lid, rel, n, line, cells, header, sec, sec_state):
        header = header or []
        el = self.define(lid, rel, n, pick_title(cells, header), sec)
        el.text.append(line)
        el.fields = {header[i]: cells[i] for i in range(min(len(header), len(cells)))}
        for i, c in enumerate(cells[1:], 1):
            hname = header[i] if i < len(header) else ''
            cc = clean(c)
            low = cc.lower()
            if cc.startswith('←') or re.search(r'←|трассир|ссылк|основан', hname):
                el.refs += up_refs(cc.split('←', 1)[1] if '←' in cc else cc)
            elif '←' in cc:
                el.refs += up_refs(cc.split('←', 1)[1])
            if cc.startswith('⚠') or 'затраг' in hname:
                el.affected += ids_in(cc)
            elif '⚠' in cc:
                el.affected += ids_in(cc.split('⚠', 1)[1])
            if 'чем закрыт' in hname:
                if cc:
                    el.closer = cc
            elif 'обработ' in hname or low.startswith(TREAT_KW):
                if cc:
                    el.treatment = cc
            bare = CODE_SPAN_RE.sub('', cc)
            if '‼' in bare:
                qs = [q for q in ids_in(bare) if kind(q) == 'Q']
                el.assume += [(q, n) for q in (qs or [None])]
            d = DATE_RE.search(cc)
            if 'статус' in hname and STATE_WORD_RE.match(low):
                el.state = (norm_state(low), d.group(0) if d else '')
            elif re.search(r'удал|снят|отмен', hname) and d:
                el.state = (norm_state(hname), d.group(0))
            else:
                sm = STATE_RE.search(cc)
                if sm and STATE_WORD_RE.match(low):
                    el.state = (norm_state(sm.group(1)), sm.group(2))
        if sec_state and el.state is None:
            d = DATE_RE.search(line)
            el.state = (sec_state, d.group(0) if d else '')

    def _parse_index(self, text):
        section = ''
        blk = None
        cur_q = None
        tbl, header = 'none', None
        for n, raw in enumerate(text.splitlines(), 1):
            if 'trace:steps:begin' in raw:
                blk = 'steps'
                self.has_steps_block = True
                continue
            if 'trace:begin' in raw:
                blk = 'ids'
                self.has_block = True
                continue
            if 'trace:end' in raw or 'trace:steps:end' in raw:
                blk = None
                continue
            if blk == 'ids':
                r = ROW_RE.match(raw)
                if r and not SEP_RE.match(raw):
                    lid = lead_id(clean(split_cells(r.group(1))[0]))
                    if lid:
                        self.index_rows[lid] = raw.strip()
                continue
            if blk == 'steps':
                continue
            for mm, i in refs_in(raw):
                self.mention_list.append((mm, i, 'INDEX.md', n))
            h = HEAD_RE.match(raw)
            if h and len(h.group(1)) <= 2:
                section = h.group(2).lower()
                cur_q = None
                tbl, header = 'none', None
                continue
            self.index_sections[section].append(raw)
            r = ROW_RE.match(raw)
            cells = None
            if r:
                if SEP_RE.match(raw):
                    tbl = 'body'
                    continue
                cells = split_cells(r.group(1))
                if tbl == 'none':
                    header = [clean(c).lower() for c in cells]
                    tbl = 'header'
                    continue
            else:
                tbl, header = 'none', None
            if 'обратный индекс' in section:
                if cells:
                    lid = lead_id(clean(cells[0]))
                    if lid and kind(lid) == 'Q':
                        self.rindex[lid].update(ids_in(' '.join(cells[1:])))
            elif 'ответ' in section:            # «Полученные ответы»
                if cells:
                    lid = lead_id(clean(cells[0]))
                    if lid and kind(lid) == 'Q':
                        self.qs.setdefault(lid, {'line': n, 'closed': True})['closed'] = True
            elif 'вопрос' in section:
                it = ITEM_RE.match(raw)
                src = h.group(2) if h else (clean(cells[0]) if cells else (it.group(3) if it else None))
                lid = lead_id(src) if src else None
                if lid and kind(lid) == 'Q':
                    closed = 'закрыт' in section or bool(Q_CLOSED_RE.search(raw))
                    if cells and header:
                        for i, hh in enumerate(header):
                            if 'статус' in hh and i < len(cells) and \
                                    clean(cells[i]).lower().startswith(('закрыт', 'отвечен', 'снят')):
                                closed = True
                    q = self.qs.setdefault(lid, {'line': n, 'closed': closed})
                    q['closed'] = q['closed'] or closed
                    cur_q = lid
                elif cur_q and Q_CLOSED_RE.search(raw):
                    self.qs[cur_q]['closed'] = True


    # -------------------------------------------------------- производное
    def name(self):
        r = self.root.resolve()
        if r.name == 'model' and r.parent.name == 'docs':
            return r.parent.parent.name
        return r.name

    def section_text(self, needle):
        return '\n'.join(l for k, v in self.index_sections.items()
                         if needle in k for l in v)

    def active(self, k=None):
        return [e for e in self.els.values() if e.active and (k is None or e.kind == k)]


# ------------------------------------------------------------------ где модель

def worktree_root(start: Path) -> Path:
    p = start.resolve()
    for d in [p] + list(p.parents):
        if (d / '.dev-cycle').is_file() or (d / '.git').exists():
            return d
    return p


def find_models(root: Path, max_depth=5):
    found = []
    root = root.resolve()
    for dirpath, dirnames, _ in os.walk(root):
        depth = len(Path(dirpath).relative_to(root).parts)
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and depth < max_depth]
        p = Path(dirpath)
        if p.name == 'model' and p.parent.name == 'docs':
            found.append(p)
            dirnames[:] = []
    return sorted(found)


def resolve_model(arg):
    if arg:
        p = Path(arg)
        if (p / 'docs' / 'model').is_dir():
            p = p / 'docs' / 'model'
        if not p.is_dir():
            die(f'модель не найдена: {arg}')
        return p.resolve()
    wt = worktree_root(Path.cwd())
    f = wt / '.dev-cycle'
    if f.is_file():
        lines = [l.strip() for l in f.read_text(encoding='utf-8').splitlines() if l.strip()]
        if lines:
            p = wt / lines[0]
            if p.is_dir():
                return p.resolve()
            die(f'путь из .dev-cycle не существует: {lines[0]}')
    models = find_models(wt)
    if len(models) == 1:
        return models[0]
    if not models:
        die('модель не найдена: нет ни .dev-cycle, ни docs/model в дереве')
    die('моделей несколько, укажи --model или запиши .dev-cycle:\n  '
        + '\n  '.join(m.relative_to(wt).as_posix() for m in models))


def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(2)


def rel_to_cwd(p: Path) -> str:
    try:
        return p.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return p.as_posix()


# ------------------------------------------------------------------ отчёт

class Report:
    def __init__(self):
        self.err = {}
        self.warn = {}
        self.info = []

    def e(self, msg, item):
        self.err.setdefault(msg, [])
        if item not in self.err[msg]:
            self.err[msg].append(item)

    def w(self, msg, item):
        self.warn.setdefault(msg, [])
        if item not in self.warn[msg]:
            self.warn[msg].append(item)

    @staticmethod
    def _items(items, limit=20):
        head = ', '.join(items[:limit])
        return head + (f' и ещё {len(items) - limit}' if len(items) > limit else '')

    def render(self):
        out = []
        if self.err:
            out.append(f'ОШИБКИ ({sum(len(v) for v in self.err.values())}) — исправить или показать как находку')
            out += [f'  {m}: {self._items(v)}' for m, v in self.err.items()]
        if self.warn:
            out.append(f'ПРОВЕРИТЬ ({sum(len(v) for v in self.warn.values())}) — показать человеку')
            out += [f'  {m}: {self._items(v)}' for m, v in self.warn.items()]
        if not self.err and not self.warn:
            out.append('Трассировка и покрытие: расхождений нет.')
        out += self.info
        return '\n'.join(out)


# ------------------------------------------------------------------ срезы

def slice_scope(model: Model, slug: str):
    d = model.root / 'slices' / slug / 'design.md'
    if not d.is_file():
        return None, False
    lines = d.read_text(encoding='utf-8', errors='replace').splitlines()
    ids, found = [], False
    for i, line in enumerate(lines):
        if re.search(r'(?i)состав среза', line):
            found = True
            ids += ids_in(line)
            if not ids:
                for nxt in lines[i + 1:]:
                    if not nxt.strip() or HEAD_RE.match(nxt):
                        break
                    ids += ids_in(nxt)
            break
    if not found:
        ids = [i for i in ids_in('\n'.join(lines)) if kind(i) in ('USRSTR', 'ACPT', 'NFR')]
    scope = set()
    for i in ids:
        k = kind(i)
        if k == 'USRSTR':
            scope |= {a.id for a in model.active('ACPT') if parent(a.id) == i}
        elif k in ('ACPT', 'NFR') and i in model.els and model.els[i].active:
            scope.add(i)
    return scope, not found


def slices(model: Model):
    d = model.root / 'slices'
    return sorted(p.name for p in d.iterdir() if p.is_dir()) if d.is_dir() else []


# ------------------------------------------------------------------ check

def check(model: Model, rep: Report):
    els = model.els

    for id_, a, b in model.dups:
        rep.e('номер определён дважды', f'{id_} ({a.file}:{a.line} и {b.file}:{b.line})')

    ext = defaultdict(list)
    refd_by = defaultdict(set)
    for el in els.values():
        p = parent(el.id)
        if p and p not in els:
            rep.e('нет родителя для вложенного номера', el.id)
        for m, r in el.refs:
            if m:
                ext[m].append((r, el.id))
                continue
            if r == el.id:
                continue
            if el.active:
                refd_by[r].add(el.id)
                if parent(r):
                    refd_by[parent(r)].add(el.id)
            if r not in els:
                if el.active:
                    rep.e('ссылка на несуществующий номер', f'{el.id} ← {r}')
                continue
            if not el.active:
                continue
            if not els[r].active:
                rep.w('ссылка на удалённый, отменённый или отклонённый', f'{el.id} ← {r}')
            s_src, s_dst = STEP.get(el.kind), STEP.get(kind(r))
            if el.kind != 'RISK' and s_src is not None and s_dst is not None and s_dst >= s_src:
                rep.e('ссылка не вверх (шаги перепутаны?)', f'{el.id} ← {r}')
        if el.active and el.kind in NEEDS_REFS and not [x for x in el.refs if x[1] != el.id]:
            rep.e('нет ссылки ← — элемент взялся ниоткуда', el.id)

    stories = [e.id for e in model.active('USRSTR')]
    if model.active('ACPT'):
        with_acpt = {parent(a.id) for a in model.active('ACPT')}
        for s in stories:
            if s not in with_acpt:
                rep.w('история без критериев', s)
    if model.active('ROLE'):
        for s in stories:
            if not any(kind(x) == 'ROLE' for x in refd_by[s]):
                rep.w('история без роли', s)
    if model.active('AGGR') or model.active('PROC'):
        for s in stories:
            if not any(kind(x) in ('AGGR', 'PROC') for x in refd_by[s]):
                rep.w('история никуда не пришла: ни агрегата, ни процесса', s)
    if model.active('PROC'):
        for s in stories:
            if not any(kind(x) == 'PROC' for x in refd_by[s]):
                rep.w('история без процесса', s)
    if model.active('ATTR'):
        with_attr = {parent(a.id) for a in model.active('ATTR')}
        for a in model.active('AGGR'):
            if a.id not in with_attr:
                rep.w('кандидат в агрегаты без атрибутов (не атрибут ли он?)', a.id)
    for r in model.active('REL'):
        if not any(kind(x) in ('USRSTR', 'ACPT') for x in r.internal_refs()):
            rep.w('связь без обоснования историей', r.id)
    tr_text = defaultdict(str)
    for t in model.active('TR'):
        tr_text[parent(t.id)] += '\n'.join(t.text) + '\n'
        if not any(kind(x) in ('PROC', 'ACPT') for x in t.internal_refs()):
            rep.w('переход не обоснован процессом или критерием', t.id)
    for st in model.active('ST'):
        agg = parent(st.id)
        if tr_text[agg] and not mentions(tr_text[agg], st.id):
            rep.w('состояние не упомянуто ни в одном переходе', st.id)
    for m in model.active('MTR'):
        body = '\n'.join(m.text).lower()
        missing = [f for f in ('формула', 'владелец', 'частота')
                   if f not in body and not any(f in k and clean(v) for k, v in m.fields.items())]
        if missing:
            rep.w('метрике не хватает полей', f'{m.id}: {", ".join(missing)}')
        if not any(kind(x) in ('TR', 'ATTR') for x in m.internal_refs()):
            rep.w('метрика не выводится из переходов и атрибутов', m.id)

    by_file = defaultdict(list)
    for t in model.active('TASK'):
        by_file[t.file].append(t)
        if t.internal_refs() and not any(kind(x) in ('ACPT', 'NFR') for x in t.internal_refs()):
            rep.e('задача не ссылается ни на критерий, ни на НФТ', t.id)
    for f, tasks in by_file.items():
        pos = {t.id: t.line for t in tasks}
        for t in tasks:
            for i in ids_in('\n'.join(t.text)):
                if kind(i) == 'TASK' and i != t.id and pos.get(i, 0) > t.line:
                    rep.w('задача ссылается вперёд', f'{t.id} → {i}')
    for slug in slices(model):
        tf = f'slices/{slug}/tasks.md'
        if tf not in model.status:
            continue
        scope, fallback = slice_scope(model, slug)
        if scope is None:
            continue
        covered = {x for t in by_file.get(tf, []) for x in t.internal_refs()}
        for c in sorted(scope, key=sort_key):
            if c not in covered:
                rep.w(f'срез {slug}: критерий или НФТ без задачи', c)
        if fallback:
            rep.info.append(f'срез {slug}: в design.md нет строки «Состав среза:», состав взят по упоминаниям')

    for rk in model.active('RISK'):
        obr, closer = rk.treatment or '', rk.closer or ''
        if not obr and not closer:
            rep.e('риск без обработки', rk.id)
        else:
            low = (obr or closer).lower()
            kws = [k for k in TREAT_KW if k in low]
            if len(kws) > 1 and '/' in low:
                rep.w('обработка не выбрана — остался перечень из шаблона', rk.id)
            elif not kws:
                rep.w('обработка не из четырёх способов', rk.id)
            else:
                kw = kws[0]
                targets = ids_in(obr + ' ' + closer)
                if kw in ('критерием', 'решением'):
                    if not targets:
                        rep.w('обработка без номера критерия или решения', rk.id)
                    for t in targets:
                        if t not in els:
                            rep.e('обработка ссылается на несуществующий номер', f'{rk.id} → {t}')
                            continue
                        tel = els[t]
                        # решение может разбирать риск в разделе «Риски» своего проекта
                        where = model.file_text.get(tel.file, '') if tel.kind == 'DES' else '\n'.join(tel.text)
                        if not mentions(where, rk.id):
                            rep.w('риск закрыт в одну сторону: элемент не упоминает риск', f'{t} ↛ {rk.id}')
                else:
                    reason = (low.split(kw, 1)[1] + ' ' + closer).strip(' :—–-.,*«»')
                    if len(reason) < 3:
                        rep.w(f'обработка «{kw}» без причины или документа', rk.id)
        if not rk.affected:
            rep.w('риск ничего не затрагивает', rk.id)
        for a in rk.affected:
            if a not in els:
                rep.e('риск затрагивает несуществующий номер', f'{rk.id} ⚠ {a}')

    for el in els.values():
        if not el.active:
            continue
        for q, n in el.assume:
            if q is None:
                rep.e('‼ без номера вопроса', f'{el.id} ({el.file}:{n})')
            elif q not in model.qs:
                rep.e('‼ ссылается на незаведённый вопрос', f'{el.id} → {q}')
            elif model.qs[q]['closed']:
                rep.e('‼ ссылается на закрытый вопрос', f'{el.id} → {q}')
            elif el.id not in model.rindex.get(q, set()):
                rep.e('‼ нет в обратном индексе', f'{q} → {el.id}')
    for rel, n in model.stray:
        rep.w('пометка ‼ вне элемента', f'{rel}:{n}')
    for q, ids in model.rindex.items():
        if q not in model.qs:
            rep.w('вопрос из обратного индекса не заведён', q)
        for i in ids:
            if i not in els:
                rep.e('обратный индекс ссылается на несуществующий номер', f'{q} → {i}')

    seen = set()
    for m, i, rel, n in model.mention_list:
        if m or i in seen:
            continue
        exists = i in model.qs if kind(i) == 'Q' else (i in els or i in model.index_rows)
        if not exists:
            seen.add(i)
            rep.w('упоминание несуществующего номера', f'{i} ({rel}:{n})')

    if not model.has_index:
        rep.w('нет файла', 'INDEX.md')
    elif not model.has_block:
        rep.info.append('в INDEX.md нет маркеров <!-- trace:begin --> / <!-- trace:end --> — '
                        'index --write не сможет обновить сводную таблицу')
    for i in model.index_rows:
        if i not in els:
            rep.e('номер пропал из файлов без пометки об удалении', i)

    for rel, st in sorted(model.status.items()):
        if st is None:
            rep.w('нет строки «> Статус:» в первой строке', rel)

    check_external(model, rep, ext)
    check_specs(model, rep)


def sibling_model(model: Model, name: str):
    wt = worktree_root(model.root)
    for base in (wt, wt.parent):
        for cand in (base / name / 'docs' / 'model', base / name):
            if cand.is_dir() and cand.resolve() != model.root.resolve() \
                    and (cand.name == 'model' or (cand / 'INDEX.md').is_file()):
                return cand
    return None


def check_external(model: Model, rep: Report, ext):
    if not ext:
        return
    ours = model.name()
    neighbours = model.section_text('соседние')
    unchecked = []
    for m, pairs in sorted(ext.items()):
        if not re.search(re.escape(m), neighbours):
            rep.w('соседняя модель не записана в «Соседние модели»', m)
        path = sibling_model(model, m)
        if path is None:
            unchecked.append(f'{m}: ' + ', '.join(sorted({r for r, _ in pairs}, key=sort_key)))
            continue
        sub = Model(path)
        back = sub.section_text('на нас ссыла')
        for r, src in pairs:
            if r not in sub.els:
                rep.e('внешняя ссылка на несуществующий номер', f'{m}:{r} (из {src})')
            elif ours not in back or not mentions(back, r):
                rep.w('соседняя модель не знает о ссылке (раздел «На нас ссылаются»)', f'{m}:{r}')
    if unchecked:
        rep.info.append('Внешние ссылки, не проверены (модели нет рядом): ' + '; '.join(unchecked))


def check_specs(model: Model, rep: Report):
    sd = model.root / 'specs'
    if not sd.is_dir():
        return
    newest = 0.0
    for p in model.root.rglob('*.md'):
        rel = p.relative_to(model.root).as_posix()
        if rel.startswith('specs/') or rel == 'INDEX.md':
            continue
        newest = max(newest, p.stat().st_mtime)
    for p in sorted(sd.glob('*.md')):
        if p.stat().st_mtime < newest:
            rep.w('спецификация старше модели — пересобрать', p.name)


def summary(model: Model):
    counts = defaultdict(int)
    for e in model.active():
        counts[e.kind] += 1
    parts = [f'{k} {counts[k]}' for k in KIND_ORDER if counts[k]]
    gone = sum(1 for e in model.els.values() if not e.active)
    open_q = sum(1 for q in model.qs.values() if not q['closed'])
    live = sum(1 for e in model.active() for q, _ in e.assume
               if q in model.qs and not model.qs[q]['closed'])
    s = f'Модель: {rel_to_cwd(model.root)} ({model.name()})\n'
    s += f'Элементов: {sum(counts.values())}' + (f' ({", ".join(parts)})' if parts else '')
    if gone:
        s += f'; удалено, отменено, снято или отклонено: {gone}'
    s += f'. Открытых вопросов: {open_q}, живых ‼: {live}.'
    return s


# ------------------------------------------------------------------ next-id

def next_id(model: Model, spec: str, count: int):
    spec = spec.strip().upper()
    if '/' in spec:
        par, sub = spec.split('/', 1)
        if kind(par) != NESTED.get(sub) or not re.fullmatch(r'[A-Z]+-\d+', par):
            die(f'неверный вид номера: {spec} (пример: USRSTR-3/ACPT, AGGR-2/TR)')
        if par not in model.els:
            print(f'внимание: {par} не определён в модели', file=sys.stderr)
        pref = f'{par}/{sub}-'
        nums = [int(i[len(pref):]) for i in model.all_ids if i.startswith(pref)]
        base = max(nums, default=0)
        return [f'{pref}{base + k}' for k in range(1, count + 1)]
    if spec in NESTED:
        die(f'{spec} — вложенный номер, укажи родителя: {NESTED[spec]}-N/{spec}')
    if spec not in KIND_ORDER:
        die(f'неизвестный вид номера: {spec}. Возможные: ' + ', '.join(
            k for k in KIND_ORDER if k not in NESTED))
    nums = [int(i.split('-')[1]) for i in model.all_ids if '/' not in i and kind(i) == spec]
    base = max(nums, default=0)
    return [f'{spec}-{base + k}' for k in range(1, count + 1)]


# ------------------------------------------------------------------ impact

def dependents(model: Model):
    dep = defaultdict(set)
    for el in model.els.values():
        for r in el.internal_refs():
            dep[r].add(el.id)
        if parent(el.id):
            dep[parent(el.id)].add(el.id)
        for a in el.affected:
            dep[a].add(el.id)
        for q, _ in el.assume:
            if q:
                dep[q].add(el.id)
        if el.kind == 'RISK':
            for t in ids_in((el.treatment or '') + ' ' + (el.closer or '')):
                dep[t].add(el.id)
    for q, ids in model.rindex.items():
        dep[q] |= ids
    return dep


def project_root(model: Model):
    r = model.root.resolve()
    if r.name == 'model' and r.parent.name == 'docs':
        return r.parent.parent
    return worktree_root(r)


def scan_tags(src: Path, model_root: Path):
    tags = defaultdict(list)
    mr = model_root.resolve()
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS
                       and Path(dirpath, d).resolve() != mr]
        for f in filenames:
            p = Path(dirpath, f)
            if p.suffix.lower() not in CODE_EXT:
                continue
            try:
                if p.stat().st_size > 2_000_000:
                    continue
                text = p.read_text(encoding='utf-8', errors='ignore')
            except OSError:
                continue
            rel = p.relative_to(src).as_posix()
            for n, line in enumerate(text.splitlines(), 1):
                m = VERIFIES_RE.search(line)
                if m:
                    for i in ids_in(m.group(1)):
                        tags[i].append(f'{rel}:{n}')
    return tags


def cmd_impact(model: Model, ids, src):
    ids = [i.strip().upper() for i in ids]
    for i in ids:
        if i not in model.els and i not in model.qs:
            print(f'внимание: {i} нет в модели', file=sys.stderr)
    dep = dependents(model)
    seen, queue = set(ids), list(ids)
    while queue:
        cur = queue.pop(0)
        for d in dep.get(cur, ()):
            if d not in seen:
                seen.add(d)
                queue.append(d)
    hit = sorted((i for i in seen if i not in ids and i in model.els), key=sort_key)
    print(f'Затронуто изменением {", ".join(ids)}: {len(hit)}')
    for i in hit:
        e = model.els[i]
        step = STEP.get(e.kind)
        mark = []
        qs = sorted({q for q, _ in e.assume if q})
        if qs:
            mark.append('‼ ' + ', '.join(qs))
        if not e.active:
            mark.append(state_text(e.state))
        where = f'шаг {step}' if step else 'риск'
        print(f'  {where:<7} {i:<18} {short(e.title, 50):<50} {"  ".join(mark)}')
    sl = defaultdict(list)
    for i in hit:
        f = model.els[i].file
        if f.startswith('slices/'):
            sl[f.split('/')[1]].append(i)
    if sl:
        print('Срезы: ' + '; '.join(f'{s} ({", ".join(v)})' for s, v in sl.items()))
    tagged = [i for i in hit + ids if kind(i) in ('ACPT', 'NFR')]
    if tagged:
        tags = scan_tags(src or project_root(model), model.root)
        lines = [f'{i} → {", ".join(tags[i])}' for i in tagged if tags.get(i)]
        if lines:
            print('Тесты с метками verifies:')
            for l in lines:
                print('  ' + l)
    back = model.section_text('на нас ссыла')
    touched = [l.strip() for l in back.splitlines()
               if any(mentions(l, i) for i in set(hit) | set(ids))]
    if touched:
        print('Соседние модели, которые ссылаются на затронутое (предупредить, не править):')
        for l in touched:
            print('  ' + l)
    sd = model.root / 'specs'
    if sd.is_dir() and any(sd.glob('*.md')):
        print('Собранные спецификации устареют: ' + ', '.join(p.name for p in sorted(sd.glob('*.md'))))


# ------------------------------------------------------------------ index

INDEX_HEAD = ['| № | Название | Шаг | ← ссылки вверх | Статус |', '|---|---|---|---|---|']


def index_rows(model: Model):
    rows = []
    for i in sorted(model.els, key=sort_key):
        e = model.els[i]
        step = STEP.get(e.kind)
        if e.kind == 'RISK':
            refs = ('⚠ ' + ', '.join(dict.fromkeys(e.affected))) if e.affected else ''
        else:
            refs = ', '.join(dict.fromkeys(
                (f'{m}:{r}' if m else r) for m, r in e.refs if r != e.id))
        st = [state_text(e.state) if e.state else 'активен']
        qs = sorted({q for q, _ in e.assume if q})
        if qs:
            st.append('‼ ' + ', '.join(qs))
        if e.kind == 'TASK' and e.done:
            st.append('[x]')
        title = short(e.title, 60).replace('|', '\\|')
        rows.append(f'| {i} | {title} | {step if step else "любой"} | {refs or "—"} | {"; ".join(st)} |')
    for i, raw in model.index_rows.items():
        if i not in model.els:
            cells = split_cells(ROW_RE.match(raw).group(1)) if ROW_RE.match(raw) else [i]
            cells += [''] * (5 - len(cells))
            cells[4] = ('⚠ нет в файлах — пометить удалённым? ' + cells[4]).strip()
            rows.append('| ' + ' | '.join(cells[:5]) + ' |')
    return INDEX_HEAD + rows


def step_groups(model: Model):
    st = model.status
    pick = lambda f: [v for k, v in st.items() if f(k)]
    g = [
        ('1–2', 'Истории и критерии', 'user-stories/', pick(lambda k: k.startswith('user-stories/'))),
        ('2', 'НФТ', 'nfr.md', pick(lambda k: k == 'nfr.md')),
        ('3', 'Роли и агрегаты', 'roles-and-aggregates.md', pick(lambda k: k == 'roles-and-aggregates.md')),
        ('4', 'Связи', 'relations.md', pick(lambda k: k == 'relations.md')),
        ('5', 'Процессы', 'processes/', pick(lambda k: k.startswith('processes/'))),
        ('6', 'Состояния и атрибуты', 'states-and-attributes.md', pick(lambda k: k == 'states-and-attributes.md')),
        ('7', 'Жизненные циклы', 'lifecycles/', pick(lambda k: k.startswith('lifecycles/'))),
        ('8', 'Метрики', 'metrics.md', pick(lambda k: k == 'metrics.md')),
    ]
    for sl in slices(model):
        for step, fname, label in (('9', 'design.md', 'Проектирование'), ('10', 'tasks.md', 'Задачи'),
                                   ('12', 'acceptance.md', 'Приёмка')):
            k = f'slices/{sl}/{fname}'
            g.append((step, f'{label} · {sl}', k, [st[k]] if k in st else []))
    g.append(('—', 'Риски (сквозной)', 'risks.md', pick(lambda k: k == 'risks.md')))
    return g


def steps_table(model: Model):
    rows = ['| Шаг | Документ | Статус |', '|---|---|---|']
    for step, label, doc, vals in step_groups(model):
        name = label if step == '—' else f'{step}. {label}'
        rows.append(f'| {name} | {doc} | {step_status(vals)} |')
    return rows


INDEX_SKELETON = """> Статус: черновик

# Указатель модели {name}

## Статус шагов

<!-- trace:steps:begin -->
<!-- trace:steps:end -->

## Реестр номеров

<!-- trace:begin -->
<!-- trace:end -->

## Открытые вопросы

| № | Вопрос | Откуда | Кому адресован | Статус |
|---|---|---|---|---|

## Обратный индекс предположений

| Q | Что пересмотреть при ответе |
|---|---|

## Полученные ответы

| Q | Ответ | Дата | Что пересмотрено |
|---|---|---|---|

## Материал, не ставший историями

## Соседние модели

## На нас ссылаются
"""


def replace_block(text, begin, end, rows):
    a, rest = text.split(begin, 1)
    _, b = rest.split(end, 1)
    return a + begin + '\n' + '\n'.join(rows) + '\n' + end + b


def cmd_index(model: Model, write: bool):
    rows = index_rows(model)
    if not write:
        print('\n'.join(steps_table(model)))
        print()
        print('\n'.join(rows))
        return 0
    path = model.root / 'INDEX.md'
    if not path.exists():
        tpl = Path(__file__).resolve().parent.parent / 'templates' / 'index.md'
        body = tpl.read_text(encoding='utf-8') if tpl.is_file() else INDEX_SKELETON.format(name=model.name())
        path.write_text(body, encoding='utf-8')
    text = path.read_text(encoding='utf-8')
    if '<!-- trace:begin -->' not in text or '<!-- trace:end -->' not in text:
        die('в INDEX.md нет маркеров <!-- trace:begin --> и <!-- trace:end -->: '
            'поставь их на место реестра номеров и повтори')
    text = replace_block(text, '<!-- trace:begin -->', '<!-- trace:end -->', rows)
    done = f'реестр номеров — строк {len(rows) - 2}'
    if '<!-- trace:steps:begin -->' in text and '<!-- trace:steps:end -->' in text:
        text = replace_block(text, '<!-- trace:steps:begin -->', '<!-- trace:steps:end -->', steps_table(model))
        done += ', статус шагов'
    path.write_text(text, encoding='utf-8')
    print(f'INDEX.md обновлён: {done}')
    return 0


# ------------------------------------------------------------------ tests

def acceptance_rows(model: Model, slug: str):
    """Строки таблиц с колонкой «Метод»: номер -> (метод, где, результат)."""
    p = model.root / 'slices' / slug / 'acceptance.md'
    rows = {}
    if not p.is_file():
        return rows
    tbl, header = 'none', None
    for raw in p.read_text(encoding='utf-8', errors='replace').splitlines():
        r = ROW_RE.match(raw)
        if not r:
            tbl, header = 'none', None
            continue
        if SEP_RE.match(raw):
            tbl = 'body'
            continue
        cells = split_cells(r.group(1))
        if tbl == 'none':
            header = [clean(c).lower() for c in cells]
            tbl = 'header'
            continue
        if not header or not any('метод' in h for h in header):
            continue
        lid = lead_id(clean(cells[0]))
        if lid and kind(lid) in ('ACPT', 'NFR'):
            d = {header[i]: clean(cells[i]) for i in range(min(len(header), len(cells)))}
            get = lambda key: next((v for k, v in d.items() if key in k), '')
            if get('метод'):
                rows[lid] = (get('метод'), get('где'), get('результат'))
    return rows


def cmd_tests(model: Model, slug, src):
    src = Path(src).resolve() if src else project_root(model)
    tags = scan_tags(src, model.root)
    if slug:
        scope, fallback = slice_scope(model, slug)
        if scope is None:
            die(f'нет среза: slices/{slug}/design.md')
        manual = acceptance_rows(model, slug)
        label = f'Срез {slug}'
        if fallback:
            print('внимание: в design.md нет строки «Состав среза:», состав взят по упоминаниям')
    else:
        scope = {e.id for e in model.active() if e.kind in ('ACPT', 'NFR')}
        manual = {}
        for sl in slices(model):
            manual.update(acceptance_rows(model, sl))
        label = 'Вся модель'
    by_test, by_hand, missing, no_result = [], [], [], 0
    for c in sorted(scope, key=sort_key):
        if tags.get(c):
            by_test.append(f'{c} → {", ".join(tags[c][:3])}' + (' …' if len(tags[c]) > 3 else ''))
        elif c in manual and not manual[c][0].lower().startswith('тест'):
            method, where, result = manual[c]
            if not result:
                no_result += 1
            by_hand.append(f'{c} → {method}' + (f', {short(where, 40)}' if where else '')
                           + f' — {short(result, 40) if result else "результат не поставлен"}')
        else:
            missing.append(c)
    print(f'{label}: критериев и НФТ {len(scope)}, тестами {len(by_test)}, '
          f'другим методом {len(by_hand)}' + (f' (без результата {no_result})' if no_result else '')
          + f', без проверки {len(missing)}')
    if by_test:
        print('Тестами (метка verifies:):')
        for l in by_test:
            print('  ' + l)
    if by_hand:
        print('Другим методом (acceptance.md):')
        for l in by_hand:
            print('  ' + l)
    if missing:
        print('Без проверки: ' + ', '.join(missing))
    bad = []
    for i, locs in sorted(tags.items(), key=lambda x: sort_key(x[0])):
        if i not in model.els:
            bad.append(f'{i} ({locs[0]}) — такого номера нет')
        elif not model.els[i].active:
            bad.append(f'{i} ({locs[0]}) — номер {state_text(model.els[i].state)}')
        elif kind(i) not in ('ACPT', 'NFR'):
            bad.append(f'{i} ({locs[0]}) — метка должна вести на критерий или НФТ')
    if bad:
        print('Метки verifies:, которые надо поправить:')
        for l in bad:
            print('  ' + l)
    return 1 if (missing or bad) else 0


# ------------------------------------------------------------------ status

def step_status(values):
    if not values:
        return 'не начат'
    if all(v and v.lower().startswith('утвержд') for v in values):
        dates = sorted(re.findall(r'\d{4}-\d{2}-\d{2}', ' '.join(values)))
        return 'утверждено' + (f' {dates[-1]}' if dates else '')
    drafts = sum(1 for v in values if not (v and v.lower().startswith('утвержд')))
    return f'черновик ({drafts} из {len(values)})' if len(values) > 1 else 'черновик'


def cmd_status(model: Model):
    print(f'Корень модели: {model.root.resolve()}')
    first = None
    for step, label, doc, vals in step_groups(model):
        s = step_status(vals)
        print(f'  шаг {step:<4} {label:<32} {s}')
        optional = label in ('НФТ', 'Риски (сквозной)') and s == 'не начат'
        if first is None and step != '—' and not s.startswith('утвержд') and not optional:
            first = f'шаг {step} — {label}'
    print(f'Первый неутверждённый: {first or "нет, все шаги утверждены"}')
    open_q = sorted((q for q, v in model.qs.items() if not v['closed']), key=sort_key)
    closed = sum(1 for v in model.qs.values() if v['closed'])
    live = sum(1 for e in model.active() for q, _ in e.assume
               if q in model.qs and not model.qs[q]['closed'])
    print(f'Вопросы: открыто {len(open_q)}' + (f' ({", ".join(open_q)})' if open_q else '')
          + f', закрыто {closed}; живых предположений ‼: {live}')
    risks = model.active('RISK')
    no_t = [r.id for r in risks if not (r.treatment or r.closer)]
    print(f'Риски: {len(risks)}, без обработки: {len(no_t)}' + (f' ({", ".join(no_t)})' if no_t else ''))
    wt = worktree_root(model.root)
    others = [m for m in find_models(wt) if m.resolve() != model.root.resolve()]
    if others:
        print('Другие модели в дереве: ' + ', '.join(m.relative_to(wt).as_posix() for m in others))
    return 0


# ------------------------------------------------------------------ main

def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(prog='trace.py', description='Проверки модели dev-cycle')
    ap.add_argument('--model', help='корень модели или папка проекта с docs/model')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('check', help='трассировка и покрытие')
    p = sub.add_parser('next-id', help='следующий свободный номер')
    p.add_argument('kind', help='USRSTR, NFR, RISK, Q, USRSTR-3/ACPT, AGGR-2/TR ...')
    p.add_argument('-n', '--count', type=int, default=1)
    p = sub.add_parser('impact', help='что держится на номерах')
    p.add_argument('ids', nargs='+')
    p.add_argument('--src', help='где искать тесты (по умолчанию — папка проекта)')
    p = sub.add_parser('index', help='сводная таблица для INDEX.md')
    p.add_argument('--write', action='store_true', help='записать между маркерами в INDEX.md')
    p = sub.add_parser('tests', help='покрытие критериев и НФТ проверками')
    p.add_argument('--slice', dest='slug')
    p.add_argument('--src', help='где искать тесты (по умолчанию — папка проекта)')
    sub.add_parser('status', help='состояние модели')
    a = ap.parse_args(argv)

    model = Model(resolve_model(a.model))
    if a.cmd == 'check':
        rep = Report()
        check(model, rep)
        print(summary(model))
        print(rep.render())
        return 1 if rep.err else 0
    if a.cmd == 'next-id':
        print('\n'.join(next_id(model, a.kind, max(1, a.count))))
        return 0
    if a.cmd == 'impact':
        cmd_impact(model, a.ids, a.src)
        return 0
    if a.cmd == 'index':
        return cmd_index(model, a.write)
    if a.cmd == 'tests':
        return cmd_tests(model, a.slug, a.src)
    if a.cmd == 'status':
        return cmd_status(model)
    return 2


if __name__ == '__main__':
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
