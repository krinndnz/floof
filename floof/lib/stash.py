"""
Provides functions for manipulating the session stash.

The stash is a way of storing POST data between requests and retrieveing that
data automatically when certain conditions are met.  Retrieval involves the
automatic attachment of the specific stash to the request (as
``request.stash``) when:

 1. The current path equals a stash's set path; and
 2. Either:

    a. The stash's key equals the return_key parameter in the request; or
    b. The stash's value for ``immediate`` evaluates True.

If stash is attached to the request, then it is automatically dropped from the
session; that is, stashes may only be automatically activated once.

Generally, a stash with a True ``immediate`` flag would be used to save POST
data for a form prior to a redirect, say after a validation error, where the
page redirected to would immediately use the stashed data.  However, exercise
caution using this option since it is subject to race conditions.

A stash with a False ``immediate`` flag would be used to save POST data
before redirecting to a re-authentication attempt.

A stash, when returned, is a dict-like object with at least the keys ``key``,
``url``, ``path``, ``immediate`` and ``post``, although ``post`` may map to an
empty dict-like object and ``key`` may be None.

Forms that subclass :class:`floof.forms.FloofForm` will automatically set their
default values to any held in the ``post`` key of the active stash (i.e.
attached to ``request.stash``)

"""
import logging
import random

log = logging.getLogger(__name__)

SESSION_KEY = 'post_stashes'


def manage_stashes(event):
    """Pyramid listener that attaches the current stash, if any, to
    ``request.stash``.  Also changes the request method to POST if a return key
    was used and there is any stashed POST data."""

    request = event.request
    request.stash = None

    key = key_from_request(request)
    stash = fetch_stash(request, request.path, key)

    if stash:
        request.stash = stash
        log.debug("Consuming stash '{0}: {1}'", request.path, stash)

        if key and stash['post']:
            # Turn this request into a POST.  This side-steps the anti-CSRF
            # protection measures in floof.app, so check the CSRF token here
            token = stash['post'].get('csrf_token')
            real_token = request.session.get_csrf_token()

            if token != real_token and 'paste.testing' not in request.environ:
                from pyramid.exceptions import Forbidden
                raise Forbidden('Possible cross-site request forgery.')

            request.method = 'POST'

        drop_stash(request, request.path, key=stash['key'])


def stash_post(request, route_name=None, immediate=False, post=None):
    """Stash the request's path and any POST data in request's session.
    Returns a key that may be used to retrieve the stash later.

    The request's path must be unique among all stashes within a single
    session.  A new stash request with a conflicting path will silently
    clobber the existing stash with that path.

    """
    stashes = request.session.setdefault(SESSION_KEY, dict())

    key = None if immediate else str(random.getrandbits(80))
    path = request.route_path(route_name) if route_name else request.path
    url = request.route_url(route_name) if route_name else request.url
    post = post or request.POST

    stashes[path] = dict(
        immediate=immediate,
        key=key,
        url=url,
        path=path,
        post=post,
    )

    request.session.changed()
    log.debug("Added stash '{0}: {1}'".format(path, stashes[path]))

    return key


def get_stash_keys(request):
    """Returns a list of all stash keys in the current session."""
    stashes = request.session.setdefault(SESSION_KEY, dict())
    return [s['key'] for s in stashes.values() if s['key']]


def key_from_request(request):
    """Returns request.params['return_key'] if it exists and is a valid key,
    otherwise None.

    If return_key exists but is invalid, a warning is logged.

    If return_key has multiple values, a warning is logged and the first valid
    key (if any) is returned.

    """
    keylist = request.params.getall('return_key')
    if not keylist:
        return None

    if len(keylist) > 1:
        # We should never have multiple return_key params in one URL
        log.warning("More than one return_key was found in a URL; this "
                    "shouldn't happen.  URL was: '{0}'".format(request.url))

    valid_keys = get_stash_keys(request)
    checked_keylist = [k for k in keylist if k in valid_keys]
    key = checked_keylist[0] if checked_keylist else None

    if key:
        return key
    else:
        log.warning("Unknown return_key value: {0!r}".format(keylist[0]))


def fetch_stash(request, path=None, key=None):
    """Retrieve stash from current session.  If the stash is not `immediate`,
    it will only be retrieved if its `key` is provided."""

    stashes = request.session.get(SESSION_KEY, dict())

    if path and path in stashes and (
            (stashes[path]['immediate'] and not key) or
            (key and stashes[path]['key'] == key)):
        return stashes[path]

    # Allow retrieval by key alone
    if key and not path:
        for stash in stashes.values():
            if stash['key'] == key:
                return stash


def drop_stash(request, path=None, key=None):
    """Drops the stash with the given path.  If the stash is not `immediate`,
    it will only be dropped if its `key` is provided."""

    stash = fetch_stash(request, path, key)
    if stash:
        stashes = request.session.get(SESSION_KEY, dict())
        del stashes[stash['path']]
