# App Engine Standard Python logging deadlock

Python applications running on App Engine Standard can run into a deadlock between the logging handler lock and the Python module import lock. For some reason, App Engine Standard's protocol buffer library holds the Python module import lock while calculating message sizes, causing a deadlock when one thread tries to flush the logs while another thread is importing modules. We've worked around this by monkey-patching the App Engine logging code to drop log messages when the deadlock might occur. We would love to have Google fix this bug so we don't have to do this. [This has been reported on the public issue tracker](https://issuetracker.google.com/issues/65016348).



The following sequence of events causes the deadlock:

### Thread/Request A:
1. Calls `logging.info` which acquires the `logging.Handler` lock.
2. `logging.info` eventually calls the App Engine log hook: `AppLogsHandler.emit`.
3. `AppLogsHandler.emit` calls `logservice.write_record`, which decides to flush the log.
4. `logservice.py:382` (in `_LogsDequeBuffer._flush`) is: `bytes_left -= 1 + group.lengthString(line.ByteSize())`. The call to `group.lengthString` is a Cython function on App Engine Standard. For some reason, it requires the Python module import lock so it blocks.


### Thread/Request B:
1. Calls `import x`: acquiring the Python import lock and calling the module code.
2. Module x (or something transitively executed by it) calls `logging.info()`.
3. This tries to acquire the lock on the `logging.Handler` installed by App Engine and blocks.


Thread A acquires the log lock, then the import lock. Thread B acquires the import lock, then the log lock. This results in a deadlock if the steps are executed in the following order: A1, A2, B1, A3, A4, B2, B3. This happens to our production service a few times a day. It is worst in "bursty" services fed by a task queue. A burst of tasks causes new instances to be started, which execute multiple requests on different threads very quickly. While those requests are executing, they are performing imports and logging at the same time. After an idle period, they exit, causing the process to repeat, which increases the chances that this happens.


# Reproducing the problem

I've reproduced this bug in this repository by doing the following:

1. A request grabs a lock, then starts a new thread.
2. The thread imports another module. This module tries to grab the lock and blocks. This means that the import lock is held until this original lock is released.
3. The original request calls logservice.flush().

This causes the deadlock every single time on App Engine Standard, but not locally on the development server. To run it:

1. `gcloud app deploy logdeadlock.yaml`
2. Make a request to `/` on this new module.
3. Observe that the logs report a deadlock.
4. Comment out the `logservice.flush()` calls in the code and re-deploy: `gcloud app deploy logdeadlock.yaml`
5. Request the same path and observe that the deadlock no longer occurs.


# Workaround

We have worked around this bug by monkey-patching `app_logging.AppLogsHandler`. To use it, `import log_bug_patch` and call `log_bug_patch.apply()` (see `log_bug_patch.py` in this repository). As an overview, the code in the logging library that hits the deadlock is:

* `logging.info` calls `logging.root.info` (a `RootLogger`, which inherits from `Logger`)
* `RootLogger.info` is actually `Logger.info`, which calls `self._log`
* `Logger._log` calls `self.handle(record)`
* `Logger.handle` calls `self.callHandlers`
* `Logger.callHandlers` calls `Handler.handle` on each `logging.Handler` that is registered on the root handler.
* `Handler.handle` calls `self.acquire()` then `self.emit()`
* `Handler.acquire` calls `self.lock.acquire()` which is a `threading.RLock` involved in the deadlock.

If the current thread holds the import lock, it is not safe for it to wait on this `Handler.lock`, since the thread that holds the lock might decide to flush. To avoid this, we patch `AppLogsHandler.handle` to call `self.lock.acquire(blocking=False)` to see if the lock is available. If we can't get the lock, we use `imp.lock_held()` to test if the import lock is held. If the import lock is held, then it might be held by this thread, so it is not safe to wait. Instead, we drop the log message and return.


# Detailed deadlocked thread stacks

To debug this problem, we started a "watchdog" thread as part of our requests, which created a Task Queue message with all thread stacks when no progress was made for 10 minutes. The thread stacks we obtained are the following:


### Thread A

```
File "/base/data/home/runtimes/python27_experiment/python27_lib/versions/1/google/appengine/runtime/runtime.py", line 152, in HandleRequest
  error)
File "/base/data/home/runtimes/python27_experiment/python27_lib/versions/1/google/appengine/runtime/wsgi.py", line 329, in HandleRequest
  return WsgiRequest(environ, handler_name, url, post_data, error).Handle()
File "/base/data/home/runtimes/python27_experiment/python27_lib/versions/1/google/appengine/runtime/wsgi.py", line 267, in Handle
  result = handler(dict(self._environ), self._StartResponse)
File "/base/data/home/apps/s~project/service:version/service_lib/installed/webapp2.py", line 1529, in __call__
  rv = self.router.dispatch(request, response)
File "/base/data/home/apps/s~project/service:version/service_lib/installed/webapp2.py", line 1278, in default_dispatcher
  return route.handler_adapter(request, response)
File "/base/data/home/apps/s~project/service:version/service_lib/installed/webapp2.py", line 1102, in __call__
  return handler.dispatch()
File "/base/data/home/apps/s~project/service:version/service_lib/installed/webapp2.py", line 570, in dispatch
  return method(*args, **kwargs)

... application code ...

File "/base/data/home/apps/s~project/service:version/googleapiclient/discovery.py", line 852, in method
  logger.info('URL being requested: %s %s' % (httpMethod,url))
File "/base/data/home/runtimes/python27_experiment/python27_dist/lib/python2.7/logging/__init__.py", line 1167, in info
  self._log(INFO, msg, args, **kwargs)
File "/base/data/home/runtimes/python27_experiment/python27_dist/lib/python2.7/logging/__init__.py", line 1286, in _log
  self.handle(record)
File "/base/data/home/runtimes/python27_experiment/python27_dist/lib/python2.7/logging/__init__.py", line 1296, in handle
  self.callHandlers(record)
File "/base/data/home/runtimes/python27_experiment/python27_dist/lib/python2.7/logging/__init__.py", line 1336, in callHandlers
  hdlr.handle(record)
File "/base/data/home/runtimes/python27_experiment/python27_dist/lib/python2.7/logging/__init__.py", line 759, in handle
  self.emit(record)
File "/base/data/home/runtimes/python27_experiment/python27_lib/versions/1/google/appengine/api/app_logging.py", line 78, in emit
  self._AppLogsLocation(record))
File "/base/data/home/runtimes/python27_experiment/python27_lib/versions/1/google/appengine/api/logservice/logservice.py", line 457, in write_record
  logs_buffer().write_record(level, created, message, source_location)
File "/base/data/home/runtimes/python27_experiment/python27_lib/versions/1/google/appengine/api/logservice/logservice.py", line 280, in write_record
  self._autoflush()
File "/base/data/home/runtimes/python27_experiment/python27_lib/versions/1/google/appengine/api/logservice/logservice.py", line 423, in _autoflush
  self._flush()
File "/base/data/home/runtimes/python27_experiment/python27_lib/versions/1/google/appengine/api/logservice/logservice.py", line 382, in _flush
  bytes_left -= 1 + group.lengthString(line.ByteSize())
```


### Thread B

```
File "/base/data/home/runtimes/python27_experiment/python27_lib/versions/1/google/appengine/runtime/runtime.py", line 152, in HandleRequest
  error)
File "/base/data/home/runtimes/python27_experiment/python27_lib/versions/1/google/appengine/runtime/wsgi.py", line 329, in HandleRequest
  return WsgiRequest(environ, handler_name, url, post_data, error).Handle()
File "/base/data/home/runtimes/python27_experiment/python27_lib/versions/1/google/appengine/runtime/wsgi.py", line 267, in Handle
  result = handler(dict(self._environ), self._StartResponse)
File "/base/data/home/apps/s~project/service:version/service_lib/installed/webapp2.py", line 1529, in __call__
  rv = self.router.dispatch(request, response)
File "/base/data/home/apps/s~project/service:version/service_lib/installed/webapp2.py", line 1278, in default_dispatcher
  return route.handler_adapter(request, response)
File "/base/data/home/apps/s~project/service:version/service_lib/installed/webapp2.py", line 1102, in __call__
  return handler.dispatch()
File "/base/data/home/apps/s~project/service:version/service_lib/installed/webapp2.py", line 570, in dispatch
  return method(*args, **kwargs)

... application code ...

File "/base/data/home/apps/s~project/service:version/application/something.py", line 108, in some_function
  from some.module import Something
File "/base/data/home/apps/s~project/service:version/some/module.py", line 28, in <module>
  app = makeApp()
File "/base/data/home/apps/s~project/service:version/some/module.py", line 85, in makeApp
  logging.debug("hooks installed")
File "/base/data/home/runtimes/python27_experiment/python27_dist/lib/python2.7/logging/__init__.py", line 1637, in debug
  root.debug(msg, *args, **kwargs)
File "/base/data/home/runtimes/python27_experiment/python27_dist/lib/python2.7/logging/__init__.py", line 1155, in debug
  self._log(DEBUG, msg, args, **kwargs)
File "/base/data/home/runtimes/python27_experiment/python27_dist/lib/python2.7/logging/__init__.py", line 1286, in _log
  self.handle(record)
File "/base/data/home/runtimes/python27_experiment/python27_dist/lib/python2.7/logging/__init__.py", line 1296, in handle
  self.callHandlers(record)
File "/base/data/home/runtimes/python27_experiment/python27_dist/lib/python2.7/logging/__init__.py", line 1336, in callHandlers
  hdlr.handle(record)
File "/base/data/home/runtimes/python27_experiment/python27_dist/lib/python2.7/logging/__init__.py", line 757, in handle
  self.acquire()
File "/base/data/home/runtimes/python27_experiment/python27_dist/lib/python2.7/logging/__init__.py", line 708, in acquire
  self.lock.acquire()
File "/base/data/home/runtimes/python27_experiment/python27_dist/lib/python2.7/threading.py", line 174, in acquire
  rc = self.__block.acquire(blocking)
```
