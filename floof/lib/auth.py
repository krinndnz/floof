from pyramid.interfaces import IAuthenticationPolicy
from pyramid.security import ACLAllowed, ACLDenied
from pyramid.security import Authenticated, Everyone
from pyramid.security import effective_principals, has_permission
from pyramid.security import has_permission
from pyramid.security import principals_allowed_by_permission
from sqlalchemy.orm import joinedload, joinedload_all
from sqlalchemy.orm.exc import NoResultFound
from zope.interface import implements

from floof import model

import calendar
import OpenSSL.crypto as ssl
import os.path

from datetime import datetime, timedelta

DEFAULT_CONFIDENCE_EXPIRY = 60 * 10  # seconds

# TRUST_MAP: Rules for converting 'pseudo-principals' and other pre-requisites
# into principals.  Pseudo-principals include:
#   auth:*   , referring to the relative strength of the user's auth method
#   trusted:*, referring to the currently valid authentication mechanisms
#
# (derived_principal, [prerequisite_principals, ...])
TRUST_MAP = dict([
    ('trusted_for:auth', [
        ('role:user', 'auth:insecure', 'trusted:openid_recent'),
        ('role:user', 'auth:insecure', 'trusted:cert'),
        ('role:user', 'auth:secure', 'trusted:cert'),
    ]),
    ('trusted_for:admin', [
        ('role:admin', 'auth:secure', 'trusted:cert'),
    ]),
])

class FloofAuthnPolicy(object):
    """Authentication policy bolted atop a beaker session.

    Most of the actual work here is done by the Authenticizer class below.
    The Pyramid auth interface is extremely clunky, and this class only exists
    so standard Pyramid authorization stuff still works.
    """
    implements(IAuthenticationPolicy)

    def authenticated_userid(self, request):
        raise NotImplementedError()

    def unauthenticated_userid(self, request):
        raise NotImplementedError()

    def effective_principals(self, request):
        user = request.auth.user
        principals = set([Everyone])

        if not user:
            return principals

        principals.add(Authenticated)
        principals.update(['user:' + str(user.id)])
        principals.update('role:' + role.name for role in user.roles)
        principals.update('trusted:' + flag for flag in request.auth.trust)

        if user.cert_auth in ('required', 'sensitive_required'):
            principals.add('auth:secure')
        else:
            principals.add('auth:insecure')

        # Add derived principals
        for derivative, prereqs_list in TRUST_MAP.iteritems():
            for prereqs in prereqs_list:
                f = lambda x: x in principals
                if all(map(f, prereqs)):
                    principals.add(derivative)
                    break

        return principals

    def remember(self, request, principal, **kw):
        # XXX: Fix one thing, embugger another, ugh
        if 'openid_url' in kw:
            request.auth.login_openid(principal, kw['openid_url'])
        request.session.save()
        return []

    def forget(self, request):
        request.auth.clear()
        request.session.save()
        return []


class CertNotFoundError(Exception): pass
class CertAuthDisabledError(Exception): pass
class CertExpiredError(Exception): pass
class CertRevokedError(Exception): pass
class OpenIDAuthDisabledError(Exception): pass
class OpenIDNotFoundError(Exception): pass
class AuthConflictError(Exception): pass

class Authenticizer(object):
    """Manages the authentication and authorization state of the current user.

    This class is intended to be instantiated from a Request object.  The authn
    and authz policy classes above expect to find an instance of this class on
    the request, and delegate Pyramid security functionality here.

    State is contained within a dictionary, passed to the constructor.
    """
    def __repr__(self):
        openid_age = None
        if 'openid_timestamp' in self.state:
            openid_age = datetime.now() - datetime.fromtimestamp(
                    self.state['openid_timestamp'])

        return ("<Authenticizer ( User: {0}, Cert: {1}, OpenID URL: {2}, "
                "OpenID Age: {3}, Trust Flags: {4} )>".format(
                    self.user.name if self.user else None,
                    self.state.get('cert_serial', None),
                    self.state.get('openid_url', None),
                    openid_age,
                    repr(self.trust),
                    ))

    def __init__(self, request):
        # This constructor determines the user ID and authentication status.
        # Note: everything within the state is meant to be consistent at all
        # times; i.e., there should never be a cert serial, openid, or user id
        # that don't all match.

        config = request.registry.settings

        self.state = request.session.setdefault('auth', {})

        # Test amenity
        if 'tests.user_id' in request.environ:
            # Override user id manually
            self.clear()
            self.user = model.session.query(model.User) \
                    .get(request.environ['tests.user_id'])
            self.trust = request.environ.get(
                'tests.auth_trust',
                ['cert', 'openid', 'openid_recent'])  # maximum!
            request.session.changed()
            return

        # Check for client certificate serial; ATM, the cert serial is passed by
        # the frontend server in an HTTP header.
        cert_serial = None
        if config.get('client_cert_auth', '').lower() == 'true':
            cert_serial = request.headers.get(
                    'X-Floof-SSL-Client-Serial', None) or cert_serial

        # Authentication and identity resolution.
        # The login_* functions below have the following obligations:
        #
        #   1. If they cannot resolve a valid authentication, they must clear
        #      any related authentication information from self.state
        #
        #   2. If they resolve a valid authentication, and this differs from
        #      the prevailing self.user, they again must clear any related
        #      authentication information from self.state
        #
        #   3. If they resolve a valid authentication, and this agrees with
        #      the prevailing self.user (or self.user is None), they must add
        #      any related authentication information to self.state and append
        #      appropriate flags to self.trust.
        #      If self.user is None, they must set it to the resolved user

        self.user = model.AnonymousUser()  # Make no assumptions
        self.trust = []  # XXX Work out a better name

        try:
            self.check_certificate(cert_serial)
        except CertNotFoundError:
            # This should NEVER happen in production (certs should last
            # forever)
            request.session.flash("I don't recognize your client certificate.",
                level='error', icon='key--exclamation')
        except CertAuthDisabledError:
            request.session.flash("Client certificates are disabled for your account.",
                level='error', icon='key--exclamation')
        except CertExpiredError:
            request.session.flash("That client certificate has expired.",
                level='error', icon='key--exclamation')
        except CertRevokedError:
            request.session.flash("That client certificate has been revoked.",
                level='error', icon='key--exclamation')

        try:
            self.check_openid(config)
        except OpenIDNotFoundError:
            request.session.flash("I don't recognize your OpenID identity.",
                level='error', icon='key--exclamation')
        except OpenIDAuthDisabledError:
            request.session.flash("Your OpenID is no longer accepted as your account has disabled OpenID authentication.",
                level='error', icon='key--exclamation')
        except AuthConflictError:
            request.session.flash("Your OpenID conflicted with your certificate and has been cleared.",
                level='error', icon='key--exclamation')

        if len(self.trust) == 0:
            # Either there's no user, or none of their current auths are valid.
            # Wipe the slate clean
            self.clear()

        print self
        request.session.changed()

    def check_certificate(self, serial):
        """Check a client certificate serial and add authentication if valid."""

        self.state.pop('cert_serial', None)

        if not serial:
            # No cert. Our obligation to wipe cert state is fulfilled above.
            return

        # Figure out what certificate and user this serial belongs to
        # TODO: Optimize eagerloading
        serial = serial.lower()
        q = model.session.query(model.Certificate) \
            .options(joinedload_all('user.roles')) \
            .filter_by(serial=serial)

        try:
            cert = q.one()
        except NoResultFound:
            raise CertNotFoundError

        if cert.user.cert_auth == u'disabled':
            raise CertAuthDisabledError
        if cert.expired:
            raise CertExpiredError
        if cert.revoked:
            raise CertRevokedError
        if self.user and self.user.id != cert.user_id:
            raise AuthConflictError

        # At this point, we're confident that the supplied cert is valid

        self.state['cert_serial'] = serial
        self.trust.append('cert')

        if not self.user:
            self.user = cert.user

    def check_openid(self, config):
        """Check OpenID state and add authentication if valid, else clear."""

        url = self.state.pop('openid_url', None)
        timestamp = self.state.pop('openid_timestamp', None)

        if not url or not timestamp:
            # No (or corrupted) OpenID login. By popping, our obligation to
            # clear relevent state is already fulfilled, so just return
            return

        # TODO: Optimize eagerloading
        q = model.session.query(model.IdentityURL) \
            .options(joinedload_all('user.roles')) \
            .filter_by(url=url)

        try:
            openid = q.one()
        except NoResultFound:
            raise OpenIDNotFoundError

        if openid.user.cert_auth == 'required':
            raise OpenIDAuthDisabledError
        if self.user and self.user.id != openid.user_id:
            raise AuthConflictError

        # XXX Check timestamp sanity?
        # At this point, we're confident that the stored OpenID login is valid

        self.state['openid_url'] = url
        self.state['openid_timestamp'] = timestamp
        self.trust.append('openid')

        # Evaluate OpenID freshness
        confidence_expiry_secs = int(config.get(
            'auth_confidence_expiry_seconds',
            DEFAULT_CONFIDENCE_EXPIRY))

        age = datetime.now() - datetime.fromtimestamp(timestamp)
        if age <= timedelta(seconds=confidence_expiry_secs):
            self.trust.append('openid_recent')

        if not self.user:
            self.user = openid.user

    def login_openid(self, user, url):
        """Log in via OpenID, adding appropriate authentication state.

        Remember that any authentication change will only take effect on the
        next request.  The typical scenario is that the user is redirected at
        the end of a request that calls this method.

        Also remember to save the session after this!
        """
        # XXX Temporarily drop auth level if user's certs have all expired

        if user.cert_auth == 'required':
            raise OpenIDAuthDisabledError
        if not url in (u.url for u in user.identity_urls):
            raise OpenIDNotFoundError

        self.state['openid_url'] = url
        self.state['openid_timestamp'] = calendar.timegm(datetime.now().timetuple())

    def clear(self):
        """Log the user out completely."""
        # TODO what shall this do with certificates
        self.state.clear()
        self.user = model.AnonymousUser()

    @property
    def can_purge(self):
        return 'openid' in self.trust

    @property
    def pending_user(self):
        return False
        # XXX this is mainly used in login.mako, for when the user has logged
        # in with one mechanism but only the other one is allowed

    @property
    def certificate_serial(self):
        """Returns the serial for the active client certificate, or None if
        there isn't one.
        """
        return self.state.get('cert_serial', None)


def get_ca(settings):
    """Fetches the Certifiacte Authority certificate and key.

    Returns a (ca_cert, ca_key) tuple, where ca_cert is a pyOpenSSL
    X509 object and ca_key is a pyOpenSSL PKey object.

    """
    cert_dir = settings['client_cert_dir']
    ca_cert_file = os.path.join(cert_dir, 'ca.pem')
    ca_key_file = os.path.join(cert_dir, 'ca.key')

    with open(ca_cert_file, 'rU') as f:
        ca_cert = ssl.load_certificate(ssl.FILETYPE_PEM, f.read())

    with open(ca_key_file, 'rU') as f:
        ca_key = ssl.load_privatekey(ssl.FILETYPE_PEM, f.read())

    return ca_cert, ca_key

# The following functions mostly assist with Authentication Upgrade.
# Authentication Upgrade refers to increasing the expected strength of a
# users's authentication to the system, generally with the goal of gaining
# additional principals and thus additional authorization.
# It may occur through adding an authentication token (providiing a cert or
# logging in with OpenID) by renewing an expireable token (e.g. renewing an
# OpenID login) or by changing to an authentication method setting that is
# considered more secure.
# The key point is that the user may perform the upgrade autonomously -- no new
# permissions need to be administratively granted.

def outstanding_principals(permission, context, request):
    """Returns a list of principal sets, where the addition of any of the sets
    would be sufficient to grant the current user the permission."""

    # TODO be able to determine a context based on a route name

    if has_permission(permission, context, request):
        return []

    principals = principals_allowed_by_permission(context, permission)
    effective = set(effective_principals(request))
    outstanding = []

    for p in principals:
        if p in TRUST_MAP:
            for alternative_principals in TRUST_MAP[p]:
                diff = set(alternative_principals) - effective
                if len(diff) > 0 and 'auth:insecure' not in diff:
                    outstanding.append(diff)
        else:
            outstanding.append(set([p]))

    return outstanding

def could_have_permission(permission, context, request):
    """Returns True if the current user either holds the permission or could
    hold it after authentication (i.e. pseudo-principal) upgrade."""

    outstanding = outstanding_principals(permission, context, request)

    if not outstanding:
        return True

    for altset in outstanding:
        f = lambda x: not x.startswith('role:')
        if all(map(f, altset)):
            return True

    return False

def attempt_privilege_escalation(permission, context, request):
    """Try to automatically guide the user through elevating their privileges.

    If it is possible to automatically guide the user to gain the privileges
    needed to gain the given permission in the given context, do so.  This may
    entail setting a return key then redirecting.

    """
    if not could_have_permission(permission, context, request):
        return

    for option in outstanding_principals(permission, context, request):
        if len(option) != 1:
            continue

        principal = option.pop()

        if 'trusted:openid' in effective_principals(request):
            msg = "You need to re-authenticate with your OpenID identity " \
                  "to complete this action"
        else:
            msg = "You need to authenticate with an OpenID identity to " \
                  "complete this action"

        if principal in ('trusted:openid', 'trusted:openid_recent'):
            # Can elevate by performing an OpenID authentication; so set a
            # return_key and redirect to the login screen
            from floof.lib.stash import stash_request
            key = stash_request(request)
            location = request.route_url('account.login',
                                         _query=[('return_key', key)])
            request.session.flash(msg, level='notice')
            from pyramid.httpexceptions import HTTPSeeOther
            raise HTTPSeeOther(location=location)

def current_view_permission(request):
    """Returns the permission on the current (non-error) view or None.

    Only works with URL Dispatch at present.

    """
    # HACK: uses non-API classes
    # And lo, epii reached forth into the bowels of Pyramid to retrieve that
    # permission attached to the view reached by the current request, and there
    # was much wailing and gnashing of teeth.
    # XXX may not yet work with pages that replace context with a ORM object

    from pyramid.config.views import MultiView
    from pyramid.interfaces import IMultiView
    from pyramid.interfaces import ISecuredView
    from pyramid.interfaces import IView
    from pyramid.interfaces import IViewClassifier
    from zope.interface import providedBy

    request_iface = request.request_iface
    r_context = providedBy(request.context)

    for view_type in (IView, ISecuredView, IMultiView):
        view = request.registry.adapters.lookup(
            (IViewClassifier, request_iface, r_context),
            view_type)
        if view is not None:
            break

    if isinstance(view, MultiView):
        view = view.match(request.context, request)

    if view is None or not hasattr(view, '__permission__'):
        return None

    return view.__permission__


# The following are help messages for user-upgradable privileges
# XXX this is ugly, ugh

def help_auth_secure(request):
    msg = "onfigure your certificate authentication option to either " \
          "'Require using client certificates for login' or " \
          "'Allow using client certificates for login; Required for Sensitive Operations'"
    if len(request.user.certificates) > 0:
        msg = "C" + msg
    else:
        msg = "First, generate and configure a client certificate, then c" + msg
    return msg

def help_trusted_cert(request):
    if len(request.user.certificates) > 1:
        msg = "Present one of your client certificates for authentication"
    elif len(request.user.certificates) == 1:
        msg = "Present your client certificate for authentication"
    else:
        msg = "Generate and configure a client certificate, then present it for authentication"

#    if request.user.cert_auth == 'disabled':
#        return "Configure your certificate authentication option to " \
#               "something other than 'Disallow using client certificates " \
#               "for login', then " + msg
    return msg

def help_trusted_openid(request):
    if len(request.user.identity_urls) > 1:
        return "Authenticate with one of your OpenIDs"
    return "Authenticate with your OpenID"

def help_trusted_openid_recent(request):
    if 'trust:openid' in effective_principals(request):
        return "Re-authenticate with your OpenID"
    return trusted_openid(request)

auth_actions = dict([
    ('auth:secure', help_auth_secure),
    ('trusted:cert', help_trusted_cert),
    ('trusted:openid', help_trusted_openid),
    ('trusted:openid_recent', help_trusted_openid_recent),
])
