import sys
sys.path.append('beanstalk')
sys.path.append('tests')

import os

from twisted.internet import protocol, reactor
from twisted.internet.task import Clock
from twisted.trial import unittest

from twisted_client import Beanstalk, BeanstalkClientFactory, BeanstalkClient
from spawner import spawner
from config import get_config


def _setUp(self):
    config = get_config("ServerConn", "../tests/tests.cfg")
    self.host = config.BEANSTALKD_HOST
    self.port = int(config.BEANSTALKD_PORT)
    self.path = os.path.join(config.BPATH, config.BEANSTALKD)
    spawner.spawn(host=self.host, port=self.port, path=self.path)


class BeanstalkTestCase(unittest.TestCase):
    def setUp(self):
        _setUp(self)

    def tearDown(self):
        spawner.terminate_all()

    def test_simplest(self):
        def check(proto):
            self.failUnless(proto)
            return proto.put("tube", 1).addCallback(lambda res: self.failUnlessEqual('ok', res['state']))

        return protocol.ClientCreator(reactor, Beanstalk).connectTCP(self.host, self.port).addCallback(check)


class BeanstalkClientFactoryTestCase(unittest.TestCase):
    def setUp(self):
        _setUp(self)

    def tearDown(self):
        spawner.terminate_all()

    def test_assign_protocol(self):
        f = BeanstalkClientFactory()
        p = f.buildProtocol("abc")
        self.failUnlessEqual(f, p.factory)


class BeanstalkClientTestCase(unittest.TestCase):
    def setUp(self):
        _setUp(self)
        self.client = BeanstalkClient()
        self.client.noisy = True

    def tearDown(self):
        spawner.terminate_all()
        if self.client.protocol:
            self.client.protocol.factory.stopTrying()

    def test_simplest(self):
        def check(proto):
            self.failUnless(proto)
            self.failUnlessEqual(self.client.protocol, proto)
            return proto.put("tube", 1).addCallback(lambda res: self.failUnlessEqual('ok', res['state']))

        return self.client.connectTCP(self.host, self.port).addCallback(check)

    def test_retry_connect(self):
        def check(proto):
            self.failUnless(proto)
            self.failUnlessEqual(self.client.protocol, proto)
            return proto.put("tube", 1).addCallback(lambda res: self.failUnlessEqual('ok', res['state']))

        spawner.terminate_all()
        reactor.callLater(1, _setUp, self)
        return self.client.connectTCP(self.host, self.port).addCallback(check)

    def test_reconnect(self):
        self.checked = 0

        def check(proto):
            self.checked += 1
            self.failUnless(proto)
            self.failUnlessEqual(self.client.protocol, proto)
            return proto.put("tube", 1).addCallback(lambda res: self.failUnlessEqual('ok', res['state']))

        return self.client.connectTCP(self.host, self.port).addCallback(check) \
                   .addCallback(lambda _: spawner.terminate_all()).addCallback(lambda _: reactor.callLater(1, _setUp, self)) \
                   .addCallback(lambda _: self.client.deferred).addCallback(check) \
                   .addCallback(lambda _: self.failUnlessEqual(2, self.checked))
