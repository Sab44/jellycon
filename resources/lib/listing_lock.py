from __future__ import (
    division, absolute_import, print_function, unicode_literals
)

import os
import random
import time
from contextlib import contextmanager

import xbmc
import xbmcaddon
import xbmcvfs

try:
    import fcntl
except ImportError:
    # Not available on Windows, listings simply run unserialized there
    fcntl = None

# Plugin modes that only build a directory listing and never wait on the user
LISTING_MODES = ('GET_CONTENT', 'WIDGET_CONTENT', 'WIDGET_CONTENT_CAST')
LOCK_DIR = 'special://temp/jellycon_listing'
MAX_WAIT = 10
POLL_INTERVAL = 0.02


def is_listing_call(paramstring):
    return any('mode={}&'.format(mode) in paramstring + '&'
               for mode in LISTING_MODES)


def _is_first_in_queue(queue_dir, ticket):
    '''
    True if no live ticket older than ours is waiting.  Tickets older than
    MAX_WAIT belong to calls that gave up waiting or died, so they are ignored.
    '''
    stale_before = '{:020d}'.format(int((time.time() - MAX_WAIT) * 1e6))
    for name in sorted(os.listdir(queue_dir)):
        if name == ticket:
            return True
        if name < stale_before:
            try:
                os.remove(os.path.join(queue_dir, name))
            except OSError:
                pass
            continue
        return False
    return True


@contextmanager
def listing_lock(paramstring):
    '''
    Run directory listings one at a time, in the order Kodi requested them.

    Kodi loads up to three widgets at once, each in its own Python
    interpreter.  The interpreters share one GIL, so on low power devices
    three concurrent listings finish later than three sequential ones.
    Holding this lock from before the heavy imports until the listing is
    done lets each widget finish in turn instead of all finishing late.

    Each caller drops a timestamped ticket into a queue directory and only
    takes the lock once its ticket is the oldest, so widgets are served in
    the order the skin requested them.  The lock is advisory: after
    MAX_WAIT seconds a call proceeds regardless.
    '''
    if fcntl is None or not is_listing_call(paramstring):
        yield
        return

    if xbmcaddon.Addon().getSetting('serialize_listings') != 'true':
        yield
        return

    # utils.translate_path is not used, importing utils is what we defer
    translate = getattr(xbmcvfs, 'translatePath', None) or xbmc.translatePath
    lock_dir = translate(LOCK_DIR)
    queue_dir = os.path.join(lock_dir, 'queue')
    try:
        os.makedirs(queue_dir)
    except OSError:
        pass

    ticket = '{:020d}-{:08x}'.format(int(time.time() * 1e6),
                                     random.getrandbits(32))
    ticket_path = os.path.join(queue_dir, ticket)
    open(ticket_path, 'w').close()

    handle = open(os.path.join(lock_dir, 'lock'), 'a')
    try:
        deadline = time.time() + MAX_WAIT
        while time.time() < deadline:
            if _is_first_in_queue(queue_dir, ticket):
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except (IOError, OSError):
                    pass
            time.sleep(POLL_INTERVAL)
        try:
            os.remove(ticket_path)
        except OSError:
            pass
        yield
    finally:
        # Closing the file releases the lock
        handle.close()
