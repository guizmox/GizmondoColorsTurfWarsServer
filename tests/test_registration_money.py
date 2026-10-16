import concurrent.futures
import contextlib
import io
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from colors_server.codec import encode_request
from colors_server.http import BoundedHTTPServer, ENDPOINT
from colors_server.service import Service, Rules, GameError

class RegistrationMoneyTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.path=Path(self.folder.name)/'world.db'
        self.service=Service(self.path)
    def request(self, command, body, service=None):
        return (service or self.service).handle(command,encode_request(body.encode()))
    def register(self,money=525,terminal='New',service=None):
        return self.request('setuserinfo',f'<user><terminalhardwareid>{terminal}</terminalhardwareid><colorsname>Player {terminal}</colorsname><money>{money}</money></user>',service)
    def balance(self,terminal='New',service=None):
        return int(self.request('getuserinfo',f'<getuserinfo><terminalhardwareid>{terminal}</terminalhardwareid></getuserinfo>',service).findtext('money'))
    def test_525_registration_returns_75000_in_same_response_and_persists(self):
        self.request('getgamevars','<getgamevars><terminalhardwareid>New</terminalhardwareid></getgamevars>')
        with self.assertRaises(GameError) as error:
            self.balance()
        self.assertEqual(error.exception.code,'NoSuchUserException')
        self.assertEqual(self.register().findtext('user/money'),'75000')
        self.assertEqual(self.balance(),75000)
        self.assertEqual(self.balance(service=Service(self.path)),75000)
    def test_registration_floor_is_configurable_and_never_reduces_higher_funds(self):
        for floor in [0,10000,75000,123456]:
            service=Service(Path(self.folder.name)/f'world-{floor}.db',Rules(starting_money=floor))
            for money in [0,525,75000,90000,200000]:
                terminal=f'P{money}'
                self.assertEqual(int(self.register(money,terminal,service).findtext('user/money')),max(floor,money))
    def test_disabled_client_money_sync_still_gets_registration_amount(self):
        service=Service(Path(self.folder.name)/'no-sync.db',Rules(starting_money=75000,sync_single_player_money=False))
        self.assertEqual(self.register(525,service=service).findtext('user/money'),'75000')
        self.assertEqual(self.register(90000,service=service).findtext('user/money'),'75000')
    def test_existing_account_is_not_refilled_after_spending_or_rule_change(self):
        self.register()
        with contextlib.closing(self.service.connection()) as db:
            db.execute('BEGIN IMMEDIATE')
            self.service.pay(db,self.service.user(db,'New'),30000)
            db.commit()
        self.assertEqual(self.balance(),45000)
        service=Service(self.path,Rules(starting_money=150000))
        self.assertEqual(self.balance(service=service),45000)
        self.assertEqual(self.register(45000,service=service).findtext('user/money'),'45000')
        self.assertEqual(self.register(46000,service=service).findtext('user/money'),'46000')
    def test_existing_525_profile_is_not_mistaken_for_new_registration(self):
        self.register(525,service=Service(self.path,Rules(starting_money=0)))
        self.assertEqual(self.balance(),525)
        self.assertEqual(self.register(525).findtext('user/money'),'525')
    def test_raw_existing_account_updates_remain_absolute_not_guessed_solo_deltas(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.register().findtext('user/money'),range(2)))
        # First response is authoritative; a second literal 525 packet remains
        # an ordinary existing-account update. No guessed solo-delta semantics.
        self.assertEqual(sorted(map(int,results)),[525,75000])
    def test_actual_encrypted_http_registration_returns_authoritative_balance(self):
        server=BoundedHTTPServer(('127.0.0.1',0),self.service,None)
        worker=threading.Thread(target=server.serve_forever);worker.start()
        try:
            plain=b'<user><terminalhardwareid>HTTP525</terminalhardwareid><colorsname>HTTP player</colorsname><money>525</money></user>'
            body=urllib.parse.urlencode({'command':'setuserinfo','data':encode_request(plain)}).encode()
            request=urllib.request.Request(f'http://127.0.0.1:{server.server_port}'+ENDPOINT,body,{'Content-Type':'application/x-www-form-urlencoded'})
            with urllib.request.urlopen(request,timeout=5) as response:
                self.assertEqual(response.status,200);result=ET.fromstring(response.read())
            self.assertEqual(result.findtext('user/money'),'75000')
            self.assertEqual(self.balance('HTTP525'),75000)
        finally:
            server.shutdown();worker.join();server.server_close()
