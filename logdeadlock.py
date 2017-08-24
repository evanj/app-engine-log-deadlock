from google.appengine.api.logservice import logservice
import cStringIO
import dis
import google.appengine.api.logservice.log_service_pb
import imp
import logging
import sys
import threading
import time
import traceback
import webapp2


# used to have a thread holding the Python import lock in a controlled way
_LOCK = threading.Lock()
_WAITING_FOR_LOCK = threading.Event()

def wait_for_lock():
    # Signals that we are attempting to hold the lock, then grabs the lock
    _WAITING_FOR_LOCK.set()
    _LOCK.acquire()
    _LOCK.release()


def hold_import_lock():
    logging.info('hold_import_lock(): will hold the Python import lock until _LOCK is released ...')
    # Import caching means this code is only executed ONCE at instance startup
    # the second time it is not called and the request will hang
    import import_lock
    assert import_lock.DONE
    logging.info('hold_import_lock(): exiting')


class Handler(webapp2.RequestHandler):
    def get(self):
        logging.info('started; imp.lock_held:%s', imp.lock_held())
        # flush to prove the code started running
        logservice.flush()

        with _LOCK:
            # run a thread which will hold the import lock until we release _LOCK
            thread = threading.Thread(target=hold_import_lock)
            thread.start()

            # wait for the thread to be blocked
            logging.info('waiting for import lock to be held by thread ...')
            is_set = _WAITING_FOR_LOCK.wait(30)
            if not is_set:
                raise Exception('_WAITING_FOR_LOCK not set: the module was already imported; this only works on the first call')
            logging.info('done; imp.lock_held:%s', imp.lock_held())
            # flush logs: will hang forever due to the call to group.lengthString(...)
            logservice.flush()

        logging.info('lock released')
        thread.join()
        logging.info('all done')

        self.response.write('OK!')
        _WAITING_FOR_LOCK.clear()


app = webapp2.WSGIApplication([
    ('/', Handler),
], debug=True)
