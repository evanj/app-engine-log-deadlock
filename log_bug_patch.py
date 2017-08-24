from google.appengine.api import app_logging
import imp

def applog_handle_patched(self, record):
    '''
    Monkey-patch to avoid deadlock in google.appengine.api.app_logging.AppLogsHandler.

    A rare deadlock is possible because flushing the logs holds this handler's lock, as well as
    the _LogsDequeBuffer's lock. It then blocks on the import lock as part of the native
    protocol buffer library for some mysterious reason. Meanwhile, if another thread executes an
    import that then logs something, it holds the import lock and blocks on the log lock.

    To avoid it: we try to acquire the log lock. If it fails, we test if import lock is held.
    If it is, we might deadlock: If this thread holds the import lock, it is not safe
    to wait for the log lock. Instead, we drop the log message. I did add a task queue
    message in a previous version, so I know that this does get hit occasionally.

    For details, see: https://github.com/evanj/app-engine-log-deadlock
    '''
    # Modified from the version in logging.Handler.handle
    rv = self.filter(record)
    if rv:
        # attempt to acquire the lock in a non-blocking fashion to detect deadlocks
        acquired = self.lock.acquire(False)
        if not acquired:
            if imp.lock_held():
                # this thread MIGHT hold the import lock (some other thread might also)
                # it is not safe to block: if we hold the import lock, the thread holding the
                # log lock will deadlock if it tries to flush
                return rv
            # safe to block: this thread does not hold the import lock
            self.lock.acquire()
        try:
            self.emit(record)
        finally:
            self.lock.release()
    return rv

_ORIGINAL_APPLOG_HANDLE = app_logging.AppLogsHandler.handle

def apply():
    app_logging.AppLogsHandler.handle = applog_handle_patched

def undo():
    app_logging.AppLogsHandler.handle = _ORIGINAL_APPLOG_HANDLE
