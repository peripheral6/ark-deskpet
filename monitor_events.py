"""Read-only incremental event monitoring; disk I/O runs outside Qt's UI thread."""
import datetime
import hashlib
import json
import os
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from usage_metrics import UsageLedger

def timestamp(value):
    if isinstance(value, (int, float)):
        return value / 1000 if value > 100_000_000_000 else value
    if isinstance(value, str):
        try:
            return datetime.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        except ValueError:
            pass
    return None

def clean(text):
    return ' '.join(text.split())[:160]


def file_fingerprint(path, offset):
    """Detect replaced or rewritten logs without rereading their full contents."""
    with path.open('rb') as f:
        head = f.read(min(4096, offset))
        f.seek(max(0, offset - 4096))
        tail = f.read(min(4096, offset))
    return hashlib.sha256(head + tail).hexdigest()

class LogMonitor:
    def __init__(self, root=None, thread_id=None):
        self.root = Path(root or Path(os.environ.get('CODEX_HOME', str(Path.home()/'.codex')))/'sessions')
        self.thread_id = thread_id
        self.path = None
        self.next_scan = 0
        self.reset()

    def reset(self):
        self.usage = UsageLedger()
        self.session_created = None
        self.session_id = None
        self.state = dict(active=False, phase='unknown', task=None, model=None,
                          progress=None, tokens=None, started_at=None, finished_at=None,
                          turn_id=None, event_key=None)
        self.offset = 0
        self.pending = b''
        self.tail_key = None
        self.tail_transition = None

    def cache_record(self):
        if self.path is None:
            return None
        # The last incomplete JSONL line has not been consumed. Replay it on resume.
        checkpoint = self.offset - len(self.pending)
        state_keys = ('active', 'phase', 'model', 'tokens', 'started_at',
                      'finished_at', 'turn_id', 'event_key')
        return dict(offset=checkpoint, fingerprint=file_fingerprint(self.path, checkpoint),
                    session_created=self.session_created, session_id=self.session_id,
                    state={key: self.state.get(key) for key in state_keys},
                    usage=self.usage.cache_record())

    def restore_cache(self, record):
        if self.path is None or not isinstance(record, dict):
            return False
        checkpoint = record.get('offset')
        if not isinstance(checkpoint, int) or checkpoint < 0 or checkpoint > self.path.stat().st_size:
            return False
        if record.get('fingerprint') != file_fingerprint(self.path, checkpoint):
            return False
        state = record.get('state')
        if not isinstance(state, dict) or not self.usage.restore_cache(record.get('usage')):
            self.reset()
            return False
        self.offset = checkpoint
        self.pending = b''
        self.session_created = record.get('session_created')
        self.session_id = record.get('session_id')
        self.state.update({key: state.get(key) for key in
                           ('active', 'phase', 'model', 'tokens', 'started_at',
                            'finished_at', 'turn_id', 'event_key')})
        return True

    def read_tail_transition(self, size):
        """Find the latest task event without replaying a large session first."""
        with self.path.open('rb') as f:
            for window in (256*1024, 1024*1024, 4*1024*1024):
                start = max(0, size - window)
                f.seek(start)
                lines = f.read(size - start).split(b'\n')
                if start:
                    lines = lines[1:]  # The first line may start in the middle of JSON.
                for line in reversed(lines):
                    if not any(tag in line for tag in (b'"task_started"', b'"task_complete"',
                                                        b'"turn_aborted"', b'"task_failed"')):
                        continue
                    try:
                        obj = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if not isinstance(obj, dict):
                        continue
                    payload = obj.get('payload') or {}
                    if obj.get('type') != 'event_msg' or not isinstance(payload, dict):
                        continue
                    event = payload.get('type')
                    if event not in ('task_started', 'task_complete', 'turn_aborted', 'task_failed'):
                        continue
                    stamp = timestamp(obj.get('timestamp'))
                    phase = {'task_started': 'running', 'task_complete': 'completed',
                             'turn_aborted': 'interrupted', 'task_failed': 'failed'}[event]
                    return dict(active=event == 'task_started', phase=phase,
                                turn_id=payload.get('turn_id'),
                                started_at=(timestamp(payload.get('started_at')) or stamp)
                                if event == 'task_started' else None,
                                finished_at=(timestamp(payload.get('completed_at')) or stamp)
                                if event != 'task_started' else None,
                                event_key=(payload.get('turn_id'), event, stamp))
                if start == 0:
                    break
        return None

    def consume(self, obj):
        p = obj.get('payload') or {}
        if not isinstance(p, dict):
            return
        kind, event = obj.get('type'), p.get('type')
        stamp = timestamp(obj.get('timestamp'))
        if kind == 'session_meta':
            self.session_created = timestamp(p.get('timestamp')) or stamp
            self.session_id = p.get('id')
        if kind == 'event_msg':
            if event == 'task_started':
                if p.get('turn_id') != self.state.get('turn_id') or not self.state.get('active'):
                    self.usage.start_turn()
                self.state.update(active=True, phase='running', turn_id=p.get('turn_id'),
                                  started_at=timestamp(p.get('started_at')) or stamp,
                                  progress=None, task=None,
                                  event_key=(p.get('turn_id'), event, stamp))
            elif event in ('task_complete', 'turn_aborted', 'task_failed'):
                if p.get('turn_id') and self.state['turn_id'] and p['turn_id'] != self.state['turn_id']:
                    return
                phase = {'task_complete':'completed', 'turn_aborted':'interrupted', 'task_failed':'failed'}[event]
                self.state.update(active=False, phase=phase,
                                  finished_at=timestamp(p.get('completed_at')) or stamp,
                                  event_key=(p.get('turn_id'), event, stamp))
                if isinstance(p.get('last_agent_message'), str):
                    self.state['progress'] = clean(p['last_agent_message'])
            elif event == 'user_message' and isinstance(p.get('message'), str):
                self.set_task(p['message'])
            elif event == 'agent_message' and p.get('phase') == 'commentary':
                self.state['progress'] = clean(p.get('message', ''))
            elif event == 'token_count':
                if self.session_created is not None and stamp is not None and stamp < self.session_created:
                    # Forks may copy older usage records. Establish the counter
                    # baseline, but do not count the parent's history again.
                    from usage_metrics import counts
                    self.usage.previous = counts((p.get('info') or {}).get('total_token_usage'))
                else:
                    self.usage.consume(p.get('info'), self.state.get('model'), stamp)
                self.state['tokens'] = ((p.get('info') or {}).get('total_token_usage') or {}).get('total_tokens')
            elif event == 'thread_settings_applied':
                self.state['model'] = (p.get('thread_settings') or {}).get('model', self.state['model'])
        if kind == 'turn_context' and p.get('model'):
            self.state['model'] = p['model']
        if kind == 'response_item' and event == 'message':
            content = p.get('content', [])
            text = content if isinstance(content,str) else ''.join(x.get('text','') for x in content if isinstance(x,dict) and isinstance(x.get('text'),str))
            if p.get('role') == 'user':
                self.set_task(text)
            elif p.get('role') == 'assistant' and p.get('phase') == 'commentary':
                self.state['progress'] = clean(text)

    def set_task(self, text):
        if text and not any(marker in text for marker in ('<environment_context>', '<permissions instructions>', '<turn_aborted>')):
            self.state['task'] = clean(text.split('My request for Codex:')[-1])

    def poll(self, max_read=4*1024*1024):
        try:
            if self.path is None and time.monotonic() >= self.next_scan:
                self.next_scan = time.monotonic() + 3
                files = [p for p in self.root.rglob('rollout-*.jsonl')
                         if not self.thread_id or p.name.endswith(self.thread_id + '.jsonl')]
                if files:
                    self.path = max(files, key=lambda p:p.stat().st_mtime)
            if self.path is not None:
                stat = self.path.stat()
                size = stat.st_size
                if size < self.offset:
                    self.reset()
                if size > self.offset:
                    with self.path.open('rb') as f:
                        f.seek(self.offset)
                        data = f.read(max_read)
                        self.offset = f.tell()
                    lines = (self.pending + data).split(b'\n')
                    self.pending = lines.pop()
                    for line in lines:
                        try:
                            obj = json.loads(line)
                        except (ValueError, UnicodeError):
                            continue
                        if isinstance(obj, dict):
                            self.consume(obj)
                if self.offset < size:
                    tail_key = (size, stat.st_mtime_ns)
                    if tail_key != self.tail_key:
                        self.tail_transition = self.read_tail_transition(size)
                        self.tail_key = tail_key
                self.state['error'] = None
        except OSError as exc:
            self.path = None
            self.reset()
            self.state['error'] = str(exc)
        result = dict(self.state)
        result['usage'] = self.usage.snapshot()
        result['task_id'] = self.session_id or self.thread_id or (self.path.stem[-36:] if self.path else '')
        result['loading'] = bool(self.path and self.offset < size)
        if result['loading']:
            if self.tail_transition is not None:
                result.update(self.tail_transition)
            else:
                result.update(active=False, phase='unknown', event_key=None)
        start, end = result['started_at'], result['finished_at']
        result['elapsed'] = max(0,int(time.time()-start)) if result['active'] and start else None
        result['last_finished'] = datetime.datetime.fromtimestamp(end).strftime('%H:%M:%S') if end else None
        return result

class GlobalMonitor:
    """Aggregate independent incremental readers; never finish while any is active."""
    def __init__(self, root=None, cache_path=None):
        self.thread_id = None
        self.root = Path(root or Path(os.environ.get('CODEX_HOME', str(Path.home()/'.codex')))/'sessions')
        self.readers = {}
        self.reader_states = {}
        self.paths = []
        self.next_scan = 0
        self.poll_cursor = 0
        cache_home = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'ArkCodexDeskpet'
        self.cache_path = Path(cache_path) if cache_path is not None else cache_home / 'usage_cache.json'
        self.cache_dirty = False
        self.next_cache_save = 0.0
        self.cached_readers = self.load_cache()

    def load_cache(self):
        try:
            record = json.loads(self.cache_path.read_text(encoding='utf-8'))
            if (isinstance(record, dict) and record.get('version') == 1
                    and record.get('root') == str(self.root)
                    and isinstance(record.get('readers'), dict)):
                return record.get('readers', {})
        except (OSError, ValueError, AttributeError):
            pass
        return {}

    def save_cache(self):
        if not self.cache_dirty or time.monotonic() < self.next_cache_save:
            return
        self.next_cache_save = time.monotonic() + 10.0
        try:
            records = {str(path): reader.cache_record() for path, reader in self.readers.items()}
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.cache_path.with_suffix('.tmp')
            temporary.write_text(json.dumps(dict(version=1, root=str(self.root), readers=records),
                                            ensure_ascii=False), encoding='utf-8')
            os.replace(temporary, self.cache_path)
            self.cache_dirty = False
        except OSError:
            # Cache failure must not affect live status monitoring.
            pass

    def poll(self):
        try:
            if time.monotonic() >= self.next_scan:
                paths = set(self.root.rglob('rollout-*.jsonl'))
                # Codex may create or switch to a new rollout file at any time.
                # Refresh discovery quickly so an event in a new file is not missed.
                self.next_scan = time.monotonic() + 2.0
                for path in paths:
                    if path not in self.readers:
                        reader = LogMonitor(self.root)
                        reader.path = path
                        try:
                            reader.restore_cache(self.cached_readers.get(str(path)))
                        except OSError:
                            reader.reset()
                        self.readers[path] = reader
                        self.reader_states[path] = dict(reader.state,
                                                         loading=path.stat().st_size > reader.offset,
                                                         usage=reader.usage.snapshot(),
                                                         task_id=reader.session_id or path.stem[-36:])
                        self.cache_dirty = True
                for path in set(self.readers) - paths:
                    del self.readers[path]
                    self.reader_states.pop(path, None)
                    self.cache_dirty = True
                self.paths = sorted(self.readers, key=lambda p: p.stat().st_mtime, reverse=True)
                self.poll_cursor = 0
            paths = self.paths
            states = []
            loading = False
            # Read only a couple of logs per timer tick so large historical
            # directories cannot monopolize Python's GIL and freeze the UI.
            if paths:
                batch = [paths[0]]
                batch.extend(path for path in paths[1:]
                             if self.reader_states[path].get('active'))
                if len(paths) > 1:
                    rotating_count = len(paths) - 1
                    rotating = paths[1 + self.poll_cursor % rotating_count]
                    if rotating not in batch:
                        batch.append(rotating)
                    self.poll_cursor = (self.poll_cursor + 1) % rotating_count
            else:
                batch = []
            for path in batch:
                reader = self.readers[path]
                # Keep each reader attached to exactly one task file.
                reader.path = path
                read_limit = 1024*1024 if path == paths[0] else 64*1024
                old_offset = reader.offset
                state = reader.poll(max_read=read_limit)
                if reader.offset != old_offset:
                    self.cache_dirty = True
                self.reader_states[path] = state
            self.save_cache()
            for path in paths:
                state = self.reader_states[path]
                loading |= bool(state.get('loading') or state.get('error'))
                states.append((str(path), state))
            active = [(path, s) for path, s in states if s.get('active')]
            if active:
                result = dict(max(active, key=lambda item: item[1].get('started_at') or 0)[1])
                result.update(active_count=len(active), event_key=('global-running', tuple(sorted(
                    (path, str(s.get('event_key'))) for path, s in active))), loading=loading)
                result['tasks'] = [s for _, s in states]
                return result
            terminal = [(path, s) for path, s in states if s.get('finished_at') is not None]
            if terminal:
                path, state = max(terminal, key=lambda item: item[1]['finished_at'])
                result = dict(state)
                result.update(active_count=0, event_key=('global-terminal', path, state.get('event_key')),
                              loading=loading)
                result['tasks'] = [s for _, s in states]
                return result
            if loading:
                return dict(active=False, phase='unknown', event_key=None, loading=True,
                            tasks=[s for _, s in states])
            return dict(active=False, phase='unknown', event_key=None, active_count=0,
                        tasks=[s for _, s in states])
        except OSError as exc:
            return dict(active=False, phase='unknown', event_key=None, error=str(exc))


_monitor = None
_future = None
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='pet-log-reader')
_latest = dict(active=False, phase='unknown')

def get_codex_status(thread_id=None):
    global _monitor, _future, _latest
    if _monitor is not None and _monitor.thread_id != thread_id:
        if _future is not None and not _future.done():
            return dict(active=False, phase='unknown')
        _future = None
        _monitor = None
        _latest = dict(active=False, phase='unknown')
    if _monitor is None:
        _monitor = GlobalMonitor() if thread_id is None else LogMonitor(thread_id=thread_id)
    if _future is not None and _future.done():
        try:
            _latest = _future.result()
        except Exception as exc:
            _latest = dict(active=False, phase='unknown', error=str(exc))
        _future = None
    if _future is None:
        _future = _executor.submit(_monitor.poll)
    return dict(_latest)
