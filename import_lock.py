import logdeadlock

# grabs logdeadlock's lock at import time: this thread is also holding the Python module lock
logdeadlock.wait_for_lock()
DONE = True
