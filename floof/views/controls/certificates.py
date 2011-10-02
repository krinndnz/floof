# encoding: utf8
import logging

from pyramid.httpexceptions import HTTPNotFound, HTTPSeeOther
from pyramid.response import Response
from pyramid.view import view_config
from sqlalchemy.orm.exc import NoResultFound
import wtforms

from floof import model
from floof.forms import KeygenField
from floof.lib.auth import get_ca
from floof.lib.helpers import friendly_serial

log = logging.getLogger(__name__)

def get_cert(serial, user=None, check_validity=False):
    """Helper for fetching certs and running common authorization checks."""
    # XXX return a tuple or check result with isinstance()?
    try:
        cert = model.session.query(model.Certificate) \
                .filter_by(serial=serial) \
                .one()
    except NoResultFound:
        raise HTTPNotFound(detail="Certificate not found.")

    if cert is None:
        raise HTTPNotFound(detail="Certificate not found.")
    if user and cert not in user.certificates:
        raise HTTPForbidden(detail="That does not appear to be your certificate.")
    if check_validity and not cert.valid:
        raise HTTPNotFound(detail="That certificate has already expired or "
                "been revoked.")

    return cert

# XXX also include CryptoAPI method?
class CertificateForm(wtforms.form.Form):
    days = wtforms.fields.SelectField(u'New Certificate Validity Period',
            coerce=int,
            choices=[(31, '31 days'), (366, '1 year'), (1096, '3 years')]
            )

class BrowserCertificateForm(CertificateForm):
    pubkey = KeygenField(u'Public Key')
    generate_browser = wtforms.fields.SubmitField(u'Generate In Browser')

    def validate_pubkey(form, field):
        if not field.data and form.generate_browser.data:
            raise wtforms.ValidationError('It looks like your browser '
                    'doesn\'t support this method.  Try &quot;Generate '
                    'Certificate on Server&quot;.')

class ServerCertificateForm(CertificateForm):
    name = wtforms.fields.TextField(u'PKCS12 Friendly Name', [
            wtforms.validators.Length(max=64),
            ])
    passphrase = wtforms.fields.PasswordField(u'PKCS12 Passphrase', [
            wtforms.validators.Length(max=64),
            ])
    generate_server = wtforms.fields.SubmitField(u'Generate On Server')

class RevokeCertificateForm(wtforms.form.Form):
    ok = wtforms.fields.SubmitField(u'Revoke Certificate')
    cancel = wtforms.fields.SubmitField(u'Cancel')


@view_config(
    route_name='controls.certs',
    permission='auth.certificates',
    request_method='GET',
    renderer='account/controls/certificates.mako')
def certificates(context, request, err=None):
    return dict(
        browser_form=BrowserCertificateForm(),
        server_form=ServerCertificateForm(),
    )

@view_config(
    route_name='controls.certs',
    permission='auth.certificates',
    request_method='POST',
    renderer='account/controls/certificates.mako')
def certificates_generate_client(context, request):
    form = BrowserCertificateForm(request.POST) # XXX fetch_post(session, request))

    ret = dict(
        browser_form=form,
        server_form=ServerCertificateForm(),
    )

    if not form.validate():
        return ret

    # Generate a new certificate from UA-supplied key.
    spkac = form.pubkey.data
    try:
        cert = model.Certificate(
            request.user,
            *get_ca(request.registry.settings),
            spkac=spkac,
            days=form.days.data
        )
    except model.Certificate.InvalidSPKACError:
        form.pubkey.errors.append("Invalid SPKAC; "
                "try using a server-generated certificate")
        return ret

    request.user.certificates.append(cert)
    request.session.flash(
        u'New certificate generated.  You may need to restart '
        'your browser to begin authenticating with it.',
        level=u'success')

    return Response(
        body=cert.public_data_der,
        headerlist=[('Content-type', 'application/x-x509-user-cert')],
    )

@view_config(
    route_name='controls.certs.generate_server',
    permission='auth.certificates',
    request_method='POST',
    renderer='account/controls/certificates.mako')
def certificates_generate_server(context, request):
    form = ServerCertificateForm(request.POST)
    if not form.validate():
        return dict(form=form)

    # Generate a new certificate.
    ca = get_ca(request.registry.settings)
    cert = model.Certificate(
        request.user,
        *ca,
        days=form.days.data
    )
    request.user.certificates.append(cert)
    request.session.flash(
        u'New certificate generated.',
        level=u'success')

    return Response(
        body=cert.pkcs12(form.passphrase.data, form.name.data, *ca),
        headerlist=[('Content-type', 'application/x-pkcs12')],
    )

@view_config(
    route_name='controls.certs.details',
    permission='auth.certificates',
    request_method='GET',
    renderer='account/controls/certificates_details.mako')
def certificates_details(context, request):
    cert = get_cert(request.matchdict['serial'], request.user)
    return dict(cert=cert)

@view_config(
    route_name='controls.certs.download',
    permission='auth.certificates',
    request_method='GET')
def certificates_download(context, request):
    cert = get_cert(request.matchdict['serial'], request.user)

    # TODO: Redirect to the cert overview page.  Somehow.
    return Response(
        body=cert.public_data,
        headerlist=[('Content-type', 'application/x-pem-file')],
    )

@view_config(
    route_name='controls.certs.revoke',
    permission='auth.certificates',
    request_method='GET',
    renderer='account/controls/certificates_revoke.mako')
def certificates_revoke(context, request, id=None):
    form = RevokeCertificateForm()
    cert = get_cert(request.matchdict['serial'], request.user)

    will_override_auth = len(request.user.valid_certificates) == 1 and \
            user.cert_auth in ['required', 'sensitive_required']

    return dict(
        form=form,
        cert=cert,
        will_override_auth=will_override_auth,
    )

@view_config(
    route_name='controls.certs.revoke',
    permission='auth.certificates',
    request_method='POST')
def certificates_revoke_commit(context, request):
    cert = get_cert(request.matchdict['serial'], request.user)

    cert.revoke()
    request.session.flash(
        u"Certificate {0} revoked successfully.".format(friendly_serial(cert.serial)),
        level=u'success')

    return HTTPSeeOther(location=request.route_url('controls.certs'))