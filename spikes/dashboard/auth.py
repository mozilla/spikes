# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Sign-in with a Mozilla Google account.

Reading the dashboard needs no account.  Changing it does: a route wrapped
in :func:`login_required` only runs for a signed-in user whose verified
Google address is in one of ``login_domains`` (``config/dashboard.json``,
``mozilla.com`` by default).  The flow is OpenID Connect against Google
(Authlib does the state, nonce and ID-token checks); the session is the
signed Flask cookie, so nothing is stored server-side and it survives a
dyno restart as long as ``SECRET_KEY`` is set.

Environment: ``GOOGLE_CLIENT_ID`` and ``GOOGLE_CLIENT_SECRET`` (an "OAuth
client ID" of type *Web application* in the Google Cloud console, with
``https://<host>/dashboard/login/callback`` as authorized redirect URI)
turn the feature on; ``SECRET_KEY`` signs the cookie.  Without the client
credentials the sign-in routes answer 503 and the page hides the button.

Routes: ``GET /dashboard/login?next=``, ``GET /dashboard/login/callback``,
``POST /dashboard/logout``, ``GET /dashboard/api/me``.
"""

import datetime
import functools
import os
import secrets
from urllib.parse import urlsplit

from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.flask_client import OAuth
from flask import (Blueprint, current_app, jsonify, redirect, render_template,
                   request, session, url_for)

from spikes.logger import logger
from . import config


blueprint = Blueprint('auth', __name__, template_folder='templates')

GOOGLE_METADATA = 'https://accounts.google.com/.well-known/openid-configuration'
SESSION_KEY = 'user'
NEXT_KEY = 'login_next'
SESSION_DAYS = 7
SAFE_METHODS = ('GET', 'HEAD', 'OPTIONS')

oauth = OAuth()


def init_app(app):
    """Session cookie settings and the Google client; call once at startup."""
    secret = os.getenv('SECRET_KEY')
    app.config.update(
        GOOGLE_CLIENT_ID=os.getenv('GOOGLE_CLIENT_ID'),
        GOOGLE_CLIENT_SECRET=os.getenv('GOOGLE_CLIENT_SECRET'),
        # random when unset: sessions then end with the process, which is
        # fine for a local run but not for production
        SECRET_KEY=secret or secrets.token_hex(32),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        # Heroku terminates TLS at its router (DYNO is set there); locally
        # the cookie has to work over plain http
        SESSION_COOKIE_SECURE='DYNO' in os.environ,
        PERMANENT_SESSION_LIFETIME=datetime.timedelta(days=SESSION_DAYS),
    )
    if app.config['GOOGLE_CLIENT_ID'] and not secret:
        logger.warning('Dashboard: GOOGLE_CLIENT_ID is set but SECRET_KEY is '
                       'not: sessions will not survive a restart')
    oauth.init_app(app)
    # client id/secret are read from app.config (GOOGLE_CLIENT_ID, ...)
    oauth.register('google', server_metadata_url=GOOGLE_METADATA,
                   client_kwargs={'scope': 'openid email profile'})
    app.register_blueprint(blueprint)


# --------------------------------------------------------------------------
# Who is allowed
# --------------------------------------------------------------------------

def domains():
    """Lower-case e-mail domains whose Google accounts may sign in."""
    return [d.lower() for d in config.get('login_domains', ['mozilla.com'])]


def enabled():
    return bool(current_app.config.get('GOOGLE_CLIENT_ID') and
                current_app.config.get('GOOGLE_CLIENT_SECRET'))


def allowed_email(email):
    if not email or '@' not in email:
        return False
    return email.rsplit('@', 1)[1].lower() in domains()


def user_of(claims):
    """The session user for Google's ID-token claims, or None when the
    account is not allowed.

    The address has to be verified by Google and in an allowed domain.
    ``hd`` (the Google Workspace domain) is only present for Workspace
    accounts; when it is, it has to match too, so a consumer account that
    merely lists a Mozilla address is refused.  The ``hd`` *parameter* sent
    with the authorization request is a hint to the account chooser, never
    a check.
    """
    email = (claims.get('email') or '').strip().lower()
    if not claims.get('email_verified') or not allowed_email(email):
        return None
    hd = claims.get('hd')
    if hd and hd.lower() not in domains():
        return None
    return {'email': email, 'name': claims.get('name') or email,
            'picture': claims.get('picture')}


def current_user():
    """The signed-in user, or None.  The domain is re-checked on every
    request so a change of ``login_domains`` cuts existing sessions."""
    user = session.get(SESSION_KEY)
    if not isinstance(user, dict) or not allowed_email(user.get('email')):
        return None
    return user


def same_origin():
    """False when the browser says the request comes from another site.

    The session cookie is ``SameSite=Lax`` so a cross-site POST does not
    carry it; this is the second lock, for browsers that send ``Origin``
    or ``Sec-Fetch-Site``.
    """
    origin = request.headers.get('Origin')
    if origin and origin.rstrip('/') != request.host_url.rstrip('/'):
        return False
    site = request.headers.get('Sec-Fetch-Site')
    return site in (None, 'same-origin', 'none')


def login_required(view):
    """Only run *view* for a signed-in Mozilla user; JSON errors otherwise
    (401 signed out, 403 cross-site), for the page's ``fetch`` calls."""
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if current_user() is None:
            return jsonify({'error': 'sign-in required',
                            'login': url_for('auth.login')}), 401
        if request.method not in SAFE_METHODS and not same_origin():
            return jsonify({'error': 'cross-site request refused'}), 403
        return view(*args, **kwargs)
    return wrapper


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

def safe_next(url):
    """*url* when it is a path on this site (``/dashboard.html#...``), else
    the dashboard: the redirect after sign-in must not leave the site."""
    default = url_for('dashboard.html')
    if not url:
        return default
    parts = urlsplit(url)
    if parts.scheme or parts.netloc or not parts.path.startswith('/') or \
            parts.path.startswith('//') or '\\' in url:
        return default
    return url


def problem(status, title, detail):
    return render_template('login_error.html', title=title, detail=detail,
                           domains=domains(),
                           dashboard_url=url_for('dashboard.html'),
                           login_url=url_for('auth.login')), status


@blueprint.route('/dashboard/login')
def login():
    if not enabled():
        return problem(503, 'Sign-in is not configured',
                       'GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are not set '
                       'on this server.')
    if current_app.config['SESSION_COOKIE_SECURE'] and not request.is_secure:
        # the cookie holding the OAuth state is Secure: start over https
        return redirect(request.url.replace('http://', 'https://', 1))
    session[NEXT_KEY] = safe_next(request.args.get('next'))
    allowed = domains()
    # hd pre-selects the Workspace account in Google's chooser ('*' = any
    # Workspace account); the real check is user_of()
    hint = allowed[0] if len(allowed) == 1 else '*'
    redirect_uri = url_for('auth.callback', _external=True)
    return oauth.google.authorize_redirect(redirect_uri, hd=hint)


@blueprint.route('/dashboard/login/callback')
def callback():
    if not enabled():
        return problem(503, 'Sign-in is not configured', '')
    try:
        token = oauth.google.authorize_access_token()
    except OAuthError as ex:
        logger.info('Dashboard: sign-in failed: %s', ex)
        if ex.error == 'mismatching_state':
            # the state cookie is gone: the sign-in took too long, the
            # browser blocks cookies, or the callback was replayed
            detail = 'The sign-in took too long or the browser did not ' \
                     'keep its cookie. Please try again.'
        else:
            detail = ex.description or ex.error or \
                'Google refused the request. Please try again.'
        return problem(400, 'Sign-in failed', detail)
    claims = token.get('userinfo') or {}
    if not claims:
        claims = oauth.google.userinfo(token=token)
    user = user_of(claims)
    next_url = session.pop(NEXT_KEY, None)
    if user is None:
        session.pop(SESSION_KEY, None)
        logger.info('Dashboard: sign-in refused for %s',
                    claims.get('email') or '<no email>')
        return problem(403, 'This account cannot sign in',
                       'Only Google accounts of the domains below can change '
                       'the dashboard. Sign out of Google or pick another '
                       'account and try again.')
    session[SESSION_KEY] = user
    session.permanent = True
    logger.info('Dashboard: %s signed in', user['email'])
    return redirect(safe_next(next_url))


@blueprint.route('/dashboard/logout', methods=['POST'])
def logout():
    session.pop(SESSION_KEY, None)
    return redirect(safe_next(request.form.get('next')))


@blueprint.route('/dashboard/api/me')
def me():
    """Who the browser is signed in as (the page shows it in the header)."""
    response = jsonify({'enabled': enabled(), 'user': current_user(),
                        'domains': domains()})
    response.headers['Cache-Control'] = 'no-store'
    return response
