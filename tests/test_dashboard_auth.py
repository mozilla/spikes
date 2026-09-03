# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Sign-in with a Mozilla Google account (spikes/dashboard/auth.py).

Google is replaced by a fake Authlib client; nothing touches the network.
"""

import unittest
from unittest import mock

from flask import redirect, session

from spikes import app
from spikes.dashboard import auth, config


MOZ = {'email': 'Someone@mozilla.com', 'email_verified': True,
       'hd': 'mozilla.com', 'name': 'Someone', 'picture': 'https://p/x'}


class FakeGoogle:
    """What auth.py uses of Authlib's remote app."""

    def __init__(self, claims=None, error=None):
        self.claims = claims
        self.error = error
        self.redirects = []

    def authorize_redirect(self, redirect_uri, **kwargs):
        self.redirects.append((redirect_uri, kwargs))
        return redirect('https://accounts.google.com/o/oauth2/v2/auth?s=1')

    def authorize_access_token(self):
        if self.error:
            raise auth.OAuthError(error=self.error, description='refused')
        return {'access_token': 't', 'userinfo': dict(self.claims)}


class AllowedTest(unittest.TestCase):

    def test_mozilla_account(self):
        user = auth.user_of(MOZ)
        self.assertEqual(user, {'email': 'someone@mozilla.com',
                                'name': 'Someone', 'picture': 'https://p/x'})

    def test_refused(self):
        cases = {
            'other domain': dict(MOZ, email='x@gmail.com', hd=None),
            'unverified': dict(MOZ, email_verified=False),
            'no email': dict(MOZ, email=None),
            'lookalike': dict(MOZ, email='x@mozilla.com.evil.org'),
            'workspace mismatch': dict(MOZ, hd='evil.org'),
        }
        for name, claims in cases.items():
            with self.subTest(name):
                self.assertIsNone(auth.user_of(claims))

    def test_consumer_account_without_hd(self):
        # hd is absent for non-Workspace accounts: the verified address rules
        self.assertIsNotNone(auth.user_of(dict(MOZ, hd=None)))

    def test_domains_from_config(self):
        previous = config.override(login_domains=['Mozilla.com',
                                                  'mozillafoundation.org'])
        try:
            self.assertEqual(auth.domains(), ['mozilla.com',
                                              'mozillafoundation.org'])
            self.assertTrue(auth.allowed_email('a@mozillafoundation.org'))
            self.assertFalse(auth.allowed_email('a@example.com'))
        finally:
            config.restore(previous)

    def test_login_required(self):
        view = auth.login_required(lambda: ('ok', 200))
        with app.test_request_context('/x', method='POST'):
            self.assertEqual(view()[1], 401)
        with app.test_request_context('/x', method='POST'):
            session[auth.SESSION_KEY] = auth.user_of(MOZ)
            self.assertEqual(view(), ('ok', 200))
        with app.test_request_context('/x', method='GET'):
            session[auth.SESSION_KEY] = auth.user_of(MOZ)
            self.assertEqual(view(), ('ok', 200))
        # a session whose domain is no longer allowed is signed out
        with app.test_request_context('/x', method='POST'):
            session[auth.SESSION_KEY] = {'email': 'x@example.com'}
            self.assertEqual(view()[1], 401)
        # cross-site writes are refused even with a session
        for headers in ({'Origin': 'https://evil.example'},
                        {'Sec-Fetch-Site': 'cross-site'}):
            with self.subTest(headers), app.test_request_context(
                    '/x', method='POST', headers=headers):
                session[auth.SESSION_KEY] = auth.user_of(MOZ)
                self.assertEqual(view()[1], 403)
        with app.test_request_context(
                '/x', method='POST', base_url='https://spikes.test',
                headers={'Origin': 'https://spikes.test',
                         'Sec-Fetch-Site': 'same-origin'}):
            session[auth.SESSION_KEY] = auth.user_of(MOZ)
            self.assertEqual(view(), ('ok', 200))


class RoutesTest(unittest.TestCase):

    def setUp(self):
        self.client = app.test_client()
        self.previous = {k: app.config.get(k)
                         for k in ('GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET')}
        app.config.update(GOOGLE_CLIENT_ID='id', GOOGLE_CLIENT_SECRET='s')
        self.google = FakeGoogle(claims=MOZ)
        self.patch = mock.patch.object(auth.oauth, 'create_client',
                                       return_value=self.google)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        app.config.update(self.previous)

    def me(self):
        r = self.client.get('/dashboard/api/me')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get('Cache-Control'), 'no-store')
        return r.get_json()

    def test_signed_out(self):
        self.assertEqual(self.me(), {'enabled': True, 'user': None,
                                     'domains': ['mozilla.com']})

    def test_login_redirects_to_google(self):
        r = self.client.get('/dashboard/login?next=/dashboard.html%23'
                            'Firefox/release')
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers['Location'].startswith(
            'https://accounts.google.com/'))
        redirect_uri, kwargs = self.google.redirects[0]
        self.assertTrue(redirect_uri.endswith('/dashboard/login/callback'))
        self.assertEqual(kwargs, {'hd': 'mozilla.com'})
        with self.client.session_transaction() as s:
            self.assertEqual(s[auth.NEXT_KEY],
                             '/dashboard.html#Firefox/release')

    def test_next_stays_on_this_site(self):
        for nxt in ('https://evil.example/', '//evil.example/x',
                    'dashboard.html', '/\\evil.example', ''):
            with self.subTest(nxt):
                self.client.get('/dashboard/login', query_string={'next': nxt})
                with self.client.session_transaction() as s:
                    self.assertEqual(s[auth.NEXT_KEY], '/dashboard.html')

    def test_callback_signs_in(self):
        self.client.get('/dashboard/login?next=/dashboard.html%23Fenix/beta')
        r = self.client.get('/dashboard/login/callback?code=c&state=x')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers['Location'], '/dashboard.html#Fenix/beta')
        self.assertEqual(self.me()['user'],
                         {'email': 'someone@mozilla.com', 'name': 'Someone',
                          'picture': 'https://p/x'})
        with self.client.session_transaction() as s:
            self.assertNotIn(auth.NEXT_KEY, s)
            self.assertTrue(s.permanent)

    def test_callback_refuses_other_domain(self):
        self.google.claims = dict(MOZ, email='x@gmail.com', hd=None)
        r = self.client.get('/dashboard/login/callback?code=c&state=x')
        self.assertEqual(r.status_code, 403)
        self.assertIn(b'@mozilla.com', r.data)
        self.assertIsNone(self.me()['user'])

    def test_callback_error_from_google(self):
        self.google.error = 'access_denied'
        r = self.client.get('/dashboard/login/callback?error=access_denied')
        self.assertEqual(r.status_code, 400)
        self.assertIsNone(self.me()['user'])

    def test_logout(self):
        with self.client.session_transaction() as s:
            s[auth.SESSION_KEY] = auth.user_of(MOZ)
        self.assertIsNotNone(self.me()['user'])
        r = self.client.post('/dashboard/logout',
                             data={'next': '/dashboard.html#all'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers['Location'], '/dashboard.html#all')
        self.assertIsNone(self.me()['user'])
        # a GET cannot sign someone out from a link on another site
        self.assertEqual(self.client.get('/dashboard/logout').status_code,
                         405)

    def test_disabled_without_credentials(self):
        app.config.update(GOOGLE_CLIENT_ID=None, GOOGLE_CLIENT_SECRET=None)
        self.assertEqual(self.me()['enabled'], False)
        r = self.client.get('/dashboard/login')
        self.assertEqual(r.status_code, 503)
        self.assertEqual(self.google.redirects, [])
        self.assertEqual(
            self.client.get('/dashboard/login/callback?code=c').status_code,
            503)
