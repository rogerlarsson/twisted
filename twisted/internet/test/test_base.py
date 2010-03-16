# Copyright (c) 2009 Twisted Matrix Laboratories.
# See LICENSE for details.

"""
Tests for L{twisted.internet.base}.
"""

import socket
from Queue import Queue

from zope.interface import implements
from zope.interface.verify import verifyClass

from twisted.python.threadpool import ThreadPool
from twisted.python.util import setIDFunction
from twisted.internet.interfaces import (
    IReactorTime, IReactorThreads, INameResolver)
from twisted.internet.error import DNSLookupError
from twisted.internet.base import (
    ThreadedResolver, AddressInformation, _ResolverComplexifier,
    DelayedCall)
from twisted.internet.task import Clock
from twisted.trial.unittest import TestCase

from twisted.internet.test.test_tcp import FakeResolver


class FakeReactor(object):
    """
    A fake reactor implementation which just supports enough reactor APIs for
    L{ThreadedResolver}.
    """
    implements(IReactorTime, IReactorThreads)

    def __init__(self):
        self._clock = Clock()
        self.callLater = self._clock.callLater

        self._threadpool = ThreadPool()
        self._threadpool.start()
        self.getThreadPool = lambda: self._threadpool

        self._threadCalls = Queue()


    def callFromThread(self, f, *args, **kwargs):
        self._threadCalls.put((f, args, kwargs))


    def _runThreadCalls(self):
        f, args, kwargs = self._threadCalls.get()
        f(*args, **kwargs)


    def _stop(self):
        self._threadpool.stop()



class NameResolverAdapterTests(TestCase):
    """
    L{_ResolverComplexifier} adapters an L{IResolverSimple} provider
    to L{INameResolver}.
    """
    def test_interface(self):
        """
        L{_ResolverComplexifier} implements L{INameResolver}.
        """
        self.assertTrue(verifyClass(INameResolver, _ResolverComplexifier))


    def _successTest(self, address, family):
        simple = FakeResolver({'example.com': address})
        resolver = _ResolverComplexifier(simple)
        d = resolver.getAddressInformation('example.com', 1234)
        d.addCallback(
            self.assertEquals, [
                AddressInformation(
                    family,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    (address, 1234))])
        return d


    def test_ipv4Success(self):
        """
        L{_ResolverComplexifier} calls the wrapped object's
        C{getHostByName} method and returns a L{Deferred} which fires
        with a list of one element containing an AF_INET element with
        the IPv4 address which C{getHostByName}'s L{Deferred} fired
        with.
        """
        return self._successTest('192.168.1.12', socket.AF_INET)


    def test_ipv6Success(self):
        """
        L{_ResolverComplexifier} calls the wrapped object's
        C{getHostByName} method and returns a L{Deferred} which fires
        with a list of one element containing an AF_INET6 element with
        the IPv6 address which C{getHostByName}'s L{Deferred} fired
        with.
        """
        return self._successTest('::1', socket.AF_INET6)


    def test_failure(self):
        """
        The L{Deferred} L{_ResolverComplexifier.getAddressInformation}
        returns fails if the wrapped resolver's C{getHostByName}
        L{Deferred} fails.
        """
        error = DNSLookupError("Problems abound")
        simple = FakeResolver({'example.com': error})
        resolver = _ResolverComplexifier(simple)
        d = resolver.getAddressInformation('example.com', 1234)
        return self.assertFailure(d, DNSLookupError)



class ThreadedResolverTests(TestCase):
    """
    Tests for L{ThreadedResolver}.
    """
    def test_success(self):
        """
        L{ThreadedResolver.getHostByName} returns a L{Deferred} which fires
        with the value returned by the call to L{socket.gethostbyname} in the
        threadpool of the reactor passed to L{ThreadedResolver.__init__}.
        """
        ip = "10.0.0.17"
        name = "foo.bar.example.com"
        timeout = 30

        reactor = FakeReactor()
        self.addCleanup(reactor._stop)

        lookedUp = []
        resolvedTo = []
        def fakeGetHostByName(name):
            lookedUp.append(name)
            return ip

        self.patch(socket, 'gethostbyname', fakeGetHostByName)

        resolver = ThreadedResolver(reactor)
        d = resolver.getHostByName(name, (timeout,))
        d.addCallback(resolvedTo.append)

        reactor._runThreadCalls()

        self.assertEqual(lookedUp, [name])
        self.assertEqual(resolvedTo, [ip])

        # Make sure that any timeout-related stuff gets cleaned up.
        reactor._clock.advance(timeout + 1)
        self.assertEqual(reactor._clock.calls, [])


    def test_failure(self):
        """
        L{ThreadedResolver.getHostByName} returns a L{Deferred} which fires a
        L{Failure} if the call to L{socket.gethostbyname} raises an exception.
        """
        timeout = 30

        reactor = FakeReactor()
        self.addCleanup(reactor._stop)

        def fakeGetHostByName(name):
            raise IOError("ENOBUFS (this is a funny joke)")

        self.patch(socket, 'gethostbyname', fakeGetHostByName)

        failedWith = []
        resolver = ThreadedResolver(reactor)
        d = resolver.getHostByName("some.name", (timeout,))
        self.assertFailure(d, DNSLookupError)
        d.addCallback(failedWith.append)

        reactor._runThreadCalls()

        self.assertEqual(len(failedWith), 1)

        # Make sure that any timeout-related stuff gets cleaned up.
        reactor._clock.advance(timeout + 1)
        self.assertEqual(reactor._clock.calls, [])


    def test_timeout(self):
        """
        If L{socket.gethostbyname} does not complete before the specified
        timeout elapsed, the L{Deferred} returned by
        L{ThreadedResolver.getHostByBame} fails with L{DNSLookupError}.
        """
        timeout = 10

        reactor = FakeReactor()
        self.addCleanup(reactor._stop)

        result = Queue()
        def fakeGetHostByName(name):
            raise result.get()

        self.patch(socket, 'gethostbyname', fakeGetHostByName)

        failedWith = []
        resolver = ThreadedResolver(reactor)
        d = resolver.getHostByName("some.name", (timeout,))
        self.assertFailure(d, DNSLookupError)
        d.addCallback(failedWith.append)

        reactor._clock.advance(timeout - 1)
        self.assertEqual(failedWith, [])
        reactor._clock.advance(1)
        self.assertEqual(len(failedWith), 1)

        # Eventually the socket.gethostbyname does finish - in this case, with
        # an exception.  Nobody cares, though.
        result.put(IOError("The I/O was errorful"))



class DelayedCallTests(TestCase):
    """
    Tests for L{DelayedCall}.
    """
    def test_str(self):
        """
        The string representation of a L{DelayedCall} instance, as returned by
        C{str}, includes the unsigned id of the instance, as well as its state,
        the function to be called, and the function arguments.
        """
        def nothing():
            pass
        dc = DelayedCall(12, nothing, (3, ), {"A": 5}, None, None, lambda: 1.5)
        ids = {dc: 200}
        def fakeID(obj):
            try:
                return ids[obj]
            except (TypeError, KeyError):
                return id(obj)
        self.addCleanup(setIDFunction, setIDFunction(fakeID))
        self.assertEquals(
            str(dc),
            "<DelayedCall 0xc8 [10.5s] called=0 cancelled=0 nothing(3, A=5)>")
