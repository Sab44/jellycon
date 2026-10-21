from __future__ import (
    division, absolute_import, print_function, unicode_literals
)

import os
import random
import time
from contextlib import contextmanager

import xbmc
import xbmcaddon
import xbmcgui
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
# Kodi starts a widget's interpreter only when one of its three worker
# slots is free, i.e. right after another listing finished.  Once widgets
# with priority=high are in use, a normal ticket on the home screen must be
# this old, and the last listing must have finished this long ago, before it
# may take the lock.  That gives a high priority call started at the same
# moment time to queue up and go first.
NORMAL_PRIORITY_DELAY = 0.3
HOME_WINDOW_ID = 10000


def is_listing_call(paramstring):
    return any('mode={}&'.format(mode) in paramstring + '&'
               for mode in LISTING_MODES)


def is_high_priority(paramstring):
    '''
    Widget paths can add priority=high so they are served before other
    listings, e.g. the widgets of the menu entry shown at startup.
    '''
    return 'priority=high&' in paramstring + '&'


def _seconds_since(path):
    try:
        return time.time() - os.path.getmtime(path)
    except OSError:
        return float('inf')


def _is_first_in_queue(queue_dir, ticket):
    '''
    True if no live ticket ahead of ours is waiting.  Tickets sort by
    priority, then by age.  Tickets older than MAX_WAIT belong to calls that
    gave up waiting or died, so they are ignored.
    '''
    stale_before = '{:020d}'.format(int((time.time() - MAX_WAIT) * 1e6))
    for name in sorted(os.listdir(queue_dir)):
        if name == ticket:
            return True
        if name[1:21] < stale_before:
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
    takes the lock once its ticket is first in line, so widgets are served
    in the order the skin requested them (high priority ones first).  The
    lock is advisory: after MAX_WAIT seconds a call proceeds regardless.
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

    high_priority = is_high_priority(paramstring)
    ticket = '{}{:020d}-{:08x}'.format(
        0 if high_priority else 1,
        int(time.time() * 1e6), random.getrandbits(32))
    ticket_path = os.path.join(queue_dir, ticket)
    open(ticket_path, 'w').close()

    last_done_path = os.path.join(lock_dir, 'last_done')
    high_seen_path = os.path.join(lock_dir, 'high_priority_seen')
    if high_priority:
        open(high_seen_path, 'w').close()
    # Hold back only when a skin uses priorities, and only on the home
    # screen, browsing a library view is never delayed
    yield_to_high = (not high_priority and
                     os.path.exists(high_seen_path) and
                     xbmcgui.getCurrentWindowId() == HOME_WINDOW_ID)
    created = time.time()

    handle = open(os.path.join(lock_dir, 'lock'), 'a')
    locked = False
    try:
        deadline = created + MAX_WAIT
        while time.time() < deadline:
            held_back = yield_to_high and (
                time.time() - created < NORMAL_PRIORITY_DELAY or
                _seconds_since(last_done_path) < NORMAL_PRIORITY_DELAY)
            if not held_back and _is_first_in_queue(queue_dir, ticket):
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
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
        if locked:
            open(last_done_path, 'w').close()
        # Closing the file releases the lock
        handle.close()
