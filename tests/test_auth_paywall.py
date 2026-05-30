"""
Durcissement Tekkai :
  - routes leads (PII) désormais protégées par @login_required ;
  - paywall (email + cap IP/jour) ; rate-limit login (brute-force).
"""
import unittest
from unittest.mock import patch

import app as tk_app

app = tk_app.app


def _login(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 'admin'


class TestLeadsAuth(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        with tk_app._RL_LOCK:
            tk_app._RL_BUCKETS.clear()

    def test_leads_routes_require_auth(self):
        # Le fix : ces routes PII renvoyaient les leads sans authentification.
        self.assertEqual(self.client.get('/api/leads').status_code, 401)
        self.assertEqual(self.client.get('/api/leads/export.csv').status_code, 401)
        self.assertEqual(self.client.delete('/api/leads/sometoken').status_code, 401)

    def test_leads_list_ok_when_logged_in(self):
        _login(self.client)
        self.assertEqual(self.client.get('/api/leads').status_code, 200)

    def test_stats_requires_auth(self):
        self.assertEqual(self.client.get('/api/stats').status_code, 401)


class TestPaywall(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self._cap = tk_app._LEAD_REGISTER_IP_DAILY
        with tk_app._RL_LOCK:
            tk_app._RL_BUCKETS.clear()

    def tearDown(self):
        tk_app._LEAD_REGISTER_IP_DAILY = self._cap
        with tk_app._RL_LOCK:
            tk_app._RL_BUCKETS.clear()

    def test_invalid_email_400(self):
        r = self.client.post('/api/leads/register',
                             json={'email': 'pasunemail', 'first_name': 'A',
                                   'last_name': 'B', 'company': 'C'})
        self.assertEqual(r.status_code, 400)

    @patch('app._db.lead_register', return_value={'token': 't', 'uses': 0})
    def test_ip_daily_cap_429(self, _m):
        tk_app._LEAD_REGISTER_IP_DAILY = 2
        with tk_app._RL_LOCK:
            tk_app._RL_BUCKETS.clear()
        body = lambda e: {'email': e, 'first_name': 'A', 'last_name': 'B', 'company': 'C'}
        self.assertEqual(self.client.post('/api/leads/register', json=body('a@b.co')).status_code, 200)
        self.assertEqual(self.client.post('/api/leads/register', json=body('c@d.co')).status_code, 200)
        self.assertEqual(self.client.post('/api/leads/register', json=body('e@f.co')).status_code, 429)


class TestLoginRateLimit(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        with tk_app._RL_LOCK:
            tk_app._RL_BUCKETS.clear()

    @patch('app.time.sleep', lambda *a, **k: None)  # évite les pauses anti-bruteforce
    def test_login_brute_force_429(self):
        with tk_app._RL_LOCK:
            tk_app._RL_BUCKETS.clear()
        for _ in range(10):
            self.client.post('/api/login', json={'username': 'x', 'password': 'bad'})
        r = self.client.post('/api/login', json={'username': 'x', 'password': 'bad'})
        self.assertEqual(r.status_code, 429)


if __name__ == '__main__':
    unittest.main()
