from twisted.protocols import basic
from twisted.internet import reactor, defer, protocol
from twisted.python import log
import protohandler

# Stolen from memcached protocol
try:
    from collections import deque
except ImportError:
    class deque(list):
        def popleft(self):
            return self.pop(0)
        def appendleft(self, item):
            self.insert(0, item)

class Command(object):
    """
    Wrap a client action into an object, that holds the values used in the
    protocol.

    @ivar _deferred: the L{Deferred} object that will be fired when the result
        arrives.
    @type _deferred: L{Deferred}

    @ivar command: name of the command sent to the server.
    @type command: C{str}
    """

    def __init__(self, command, handler, **kwargs):
        """
        Create a command.

        @param command: the name of the command.
        @type command: C{str}

        @param kwargs: this values will be stored as attributes of the object
            for future use
        """
        self.command = command
        self.handler = handler
        self._deferred = defer.Deferred()
        for k, v in kwargs.items():
            setattr(self, k, v)

    def __repr__(self):
        return "<Command: %s>" % self.command

    def success(self, value):
        """
        Shortcut method to fire the underlying deferred.
        """
        self._deferred.callback(value)


    def fail(self, error):
        """
        Make the underlying deferred fails.
        """
        self._deferred.errback(error)


protocol_commands = [name.partition('_')[2] for name in dir(protohandler) if name.startswith('process_')]


class Beanstalk(basic.LineReceiver):
    def __init__(self):
        self._current = deque()

    def connectionMade(self):
        self.setLineMode()

    def connectionLost(self, reason):
        for pending in self._current:
            pending.fail(reason)

    def _cmd(self, command, full_command, handler):
        # Note here: the protohandler already inserts the \r\n, so
        # it would be an error to do self.sendline()
        self.transport.write(full_command)
        cmdObj = Command(command, handler)
        self._current.append(cmdObj)
        return cmdObj._deferred

    def lineReceived(self, line):
        """
        Receive line commands from the server.
        """
        pending = self._current.popleft()
        try:
            # this is a bit silly as twisted is so nice as to remove the
            # eol from the line, but protohandler.Handler needs it...
            # the reason its needed is that protohandler needs to work
            # in situations without twisted where things aren't so nice
            res = pending.handler(line + "\r\n")
        except Exception, e:
            pending.fail(e)
        else:
            if res is not None: # we have a result!
                pending.success(res)
            else: # there is more data, its a job or something...
                # push the pending command back on the stack
                self._current.appendleft(pending)
                self.setRawMode()

    def rawDataReceived(self, data):
        pending = self._current.popleft()
        if len(data) >= pending.handler.remaining:
            rem = data[pending.handler.remaining:]
            data = data[:pending.handler.remaining]
        else:
            rem = None

        try:
            res = pending.handler(data)
        except Exception, e:
            pending.fail(e)
            self.setLineMode(rem)
        if res:
            pending.success(res)
            self.setLineMode(rem)
        else:
            self._current.appendleft(pending)

for command in protocol_commands:
    def wrapper(cmd):
        def caller(self, *args, **kw):
            return self._cmd(cmd, *getattr(protohandler, 'process_%s' % cmd)(*args, **kw))
        return caller

    setattr(Beanstalk, command, wrapper(command))


class BeanstalkClientFactory(protocol.ReconnectingClientFactory):
    """
    Retries on disconnect.
    Intended to be used with L{BeanstalkClient}, but also may be used as normal Twisted L{ReconnectingClientFactory}.
    """

    noisy = False
    protocol = Beanstalk

    def __init__(self, _client=None):
        self._client = _client

    def buildProtocol(self, addr):
        if self.noisy:
            log.msg("BeanstalkClientFactory - buildProtocol %r" % addr)
        self.resetDelay()
        protocol_instance = protocol.ReconnectingClientFactory.buildProtocol(self, addr)
        if self._client:
            # using delayed call to get attached transport
            reactor.callLater(0, self._client._fire, protocol_instance)
        return protocol_instance

    def clientConnectionFailed(self, connector, reason):
        if self.noisy:
            log.msg("BeanstalkClientFactory - clientConnectionFailed: %s" % reason)
        if self._client:
            self._client._fire(reason)
        return protocol.ReconnectingClientFactory.clientConnectionFailed(self, connector, reason)

    def clientConnectionLost(self, connector, reason):
        if self.noisy:
            log.msg("BeanstalkClientFactory - clientConnectionLost: %s" % reason)
        if self._client:
            self._client._fire(reason)
        return protocol.ReconnectingClientFactory.clientConnectionLost(self, connector, reason)


class BeanstalkClient(object):
    """
    @ivar deferred: on connect callbacks C{self},
                    on disconnect errbacks (C{self}, reason) if L{consumeDisconnects} is C{False}
    @type deferred: L{Deferred}
    @ivar protocol: beanstalkd protocol instance if connected, C{None} otherwise
    @type protocol: L{Beanstalk} or C{None}
    @ivar factory: factory, may be used to set L{protocol.ReconnectingClientFactory} parameters: maxDelay, factor, etc.
    @type factory: L{BeanstalkClientFactory}
    @ivar host: beanstalkd host
    @ivar port: beanstalkd port
    @ivar used_tube: used beanstalkd tube
    @type used_tube: C{str}
    @ivar watched_tubes: watched beanstalkd tubes
    @type watched_tubes: C{set} of C{str}
    """

    def __init__(self, consumeDisconnects=True, restoreState=True, noisy=False):
        self.factory = BeanstalkClientFactory(self)
        self.factory.noisy = noisy
        self.noisy = noisy
        self.protocol = None
        self.consumeDisconnects = consumeDisconnects
        self.restoreState = restoreState
        self.host = None
        self.port = None
        self.deferred = defer.Deferred()
        self._reset_state()

    def _swap_deferred(self):
        d = self.deferred
        self.deferred = defer.Deferred()
        if self.restoreState:
            self.deferred.addCallback(lambda _: self._restore_state())
        return d

    def _reset_state(self):
        self.used_tube = "default"
        self.watched_tubes = set(["default"])

    def _restore_state(self):
        def use():
            if self.used_tube == "default":
                return defer.succeed(None)
            else:
                return self.protocol.use(self.used_tube)

        def watch(_):
            d = defer.succeed(None)
            for tube in self.watched_tubes:
                d.addCallback(lambda _: self.protocol.watch(tube))
            if "default" not in self.watched_tubes:
                d.addCallback(lambda _: self.protocol.ignore("default"))
            return d

        assert self.protocol
        return use().addCallback(watch).addCallback(lambda _: self)

    def _fire(self, arg):
        if self.noisy:
            log.msg("BeanstalkClient - _fire %r" % (arg))

        if isinstance(arg, Beanstalk):
            self.protocol = arg
            return self._swap_deferred().callback(self)
        else:
            self.protocol = None
            if not self.consumeDisconnects:
                return self._swap_deferred().errback((self, arg.value))

    def connectTCP(self, host, port):
        """
        Connects to given L{host} and L{port}. Disconnects before it if already connected.

        @return: C{self.deferred}
        """

        assert not self.protocol, "BeanstalkClient.connectTCP(%r, %d) called while already connected to %r:%r" % (host, port, self.host, self.port)
        self.host = host
        self.port = port
        reactor.connectTCP(host, port, self.factory)
        return self.deferred

    def retry(self):
        """
        Resets reconnect delay.

        @return: C{None}
        """

        self.factory.resetDelay()
        if self.factory.connector and not self.protocol:
            self.factory.stopTrying()
            self.factory.retry()

    def disconnect(self):
        """
        Disconnects from server and stops attempts to reconnect.

        @return: C{self.deferred}
        """

        self.factory.resetDelay()
        self.factory.stopTrying()
        if self.protocol:
            self.protocol.transport.loseConnection()
        self._reset_state()
        return self.deferred

for command in protocol_commands:
    def wrapper(cmd):
        def caller(self, *args, **kw):
            if cmd == "use":
                self.used_tube = args[0]
            elif cmd == "watch":
                self.watched_tubes |= frozenset([args[0]])
            elif cmd == "ignore":
                self.watched_tubes -= frozenset([args[0]])

            return getattr(self.protocol, cmd)(*args, **kw)
        return caller

    setattr(BeanstalkClient, command, wrapper(command))
