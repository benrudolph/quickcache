# -*- coding: utf-8 -*-
from __future__ import absolute_import, unicode_literals
import time

from unittest import TestCase
import datetime

import uuid

from quickcache import get_quickcache
from quickcache.cache_helpers import TieredCache

BUFFER = []


class LocMemCache(object):

    def __init__(self, name, timeout):
        self._cache = {}  # key: (expire-time, value)
        self.name = name
        self.default_timeout = timeout  # allow sub-second timeout

    def get(self, key, default=None):
        try:
            expire_time, value = self._cache[key]
        except KeyError:
            return default

        if datetime.datetime.utcnow() < expire_time:
            return value
        else:
            del self._cache[key]
            return default

    def set(self, key, value, timeout=None):
        timeout_td = datetime.timedelta(seconds=self.default_timeout
                                        if timeout is None else timeout)
        self._cache[key] = (datetime.datetime.utcnow() + timeout_td, value)


class CacheMock(LocMemCache):

    def __init__(self, name, timeout, silent_set=True):
        self.silent_set = silent_set
        super(CacheMock, self).__init__(name, timeout=timeout)

    def get(self, key, default=None):
        result = super(CacheMock, self).get(key, default)
        if result is default:
            BUFFER.append('{} miss'.format(self.name))
        else:
            BUFFER.append('{} hit'.format(self.name))
        return result

    def set(self, key, value, timeout=None):
        super(CacheMock, self).set(key, value, timeout)
        if not self.silent_set:
            BUFFER.append('{} set'.format(self.name))


SHORT_TIME_UNIT = 0.01

_local_cache = CacheMock('local', timeout=SHORT_TIME_UNIT)
_shared_cache = CacheMock('shared', timeout=2 * SHORT_TIME_UNIT)
_cache = TieredCache([_local_cache, _shared_cache])
_cache_with_set = CacheMock('cache', timeout=SHORT_TIME_UNIT, silent_set=False)

quickcache = get_quickcache(cache=TieredCache([
    CacheMock('local', timeout=10),
    CacheMock('shared', timeout=5 * 60)]
))


class QuickcacheTest(TestCase):

    def tearDown(self):
        self.consume_buffer()

    def consume_buffer(self):
        result = list(BUFFER)
        del BUFFER[:]
        return result

    def test_tiered_cache(self):
        @quickcache([], cache=_cache)
        def simple():
            BUFFER.append('called')
            return 'VALUE'

        self.assertEqual(simple(), 'VALUE')
        self.assertEqual(self.consume_buffer(), ['local miss', 'shared miss', 'called'])
        self.assertEqual(simple(), 'VALUE')
        self.assertEqual(self.consume_buffer(), ['local hit'])
        # let the local cache expire
        time.sleep(SHORT_TIME_UNIT)
        self.assertEqual(simple(), 'VALUE')
        self.assertEqual(self.consume_buffer(), ['local miss', 'shared hit'])
        # let the shared cache expire
        time.sleep(SHORT_TIME_UNIT)
        self.assertEqual(simple(), 'VALUE')
        self.assertEqual(self.consume_buffer(), ['local miss', 'shared miss', 'called'])
        # and that this is again cached locally
        self.assertEqual(simple(), 'VALUE')
        self.assertEqual(self.consume_buffer(), ['local hit'])

    def test_vary_on(self):
        @quickcache(['n'], cache=_cache)
        def fib(n):
            BUFFER.append(n)
            if n < 2:
                return 1
            else:
                return fib_r(n - 1) + fib_r(n - 2)

        fib_r = fib

        # [1, 1, 2, 3, 5, 8]
        self.assertEqual(fib(5), 8)
        self.assertEqual(self.consume_buffer(),
                         ['local miss', 'shared miss', 5,
                          'local miss', 'shared miss', 4,
                          'local miss', 'shared miss', 3,
                          'local miss', 'shared miss', 2,
                          'local miss', 'shared miss', 1,
                          'local miss', 'shared miss', 0,
                          # fib(3/4/5) also ask for fib(1/2/3)
                          # so three cache hits
                          'local hit', 'local hit', 'local hit'])

    def test_vary_on_attr(self):
        class Item(object):

            def __init__(self, id, name):
                self.id = id
                self.name = name

            @quickcache(['self.id'], cache=_cache)
            def get_name(self):
                BUFFER.append('called method')
                return self.name

        @quickcache(['item.id'], cache=_cache)
        def get_name(item):
            BUFFER.append('called function')
            return item.name

        james = Item(1, 'james')
        fred = Item(2, 'fred')
        self.assertEqual(get_name(james), 'james')
        self.assertEqual(self.consume_buffer(),
                         ['local miss', 'shared miss', 'called function'])
        self.assertEqual(get_name(fred), 'fred')
        self.assertEqual(self.consume_buffer(),
                         ['local miss', 'shared miss', 'called function'])
        self.assertEqual(get_name(james), 'james')
        self.assertEqual(self.consume_buffer(), ['local hit'])
        self.assertEqual(get_name(fred), 'fred')
        self.assertEqual(self.consume_buffer(), ['local hit'])

        # this also works, and uses different keys
        self.assertEqual(james.get_name(), 'james')
        self.assertEqual(self.consume_buffer(),
                         ['local miss', 'shared miss', 'called method'])
        self.assertEqual(fred.get_name(), 'fred')
        self.assertEqual(self.consume_buffer(),
                         ['local miss', 'shared miss', 'called method'])
        self.assertEqual(james.get_name(), 'james')
        self.assertEqual(self.consume_buffer(), ['local hit'])
        self.assertEqual(fred.get_name(), 'fred')
        self.assertEqual(self.consume_buffer(), ['local hit'])

    def test_bad_vary_on(self):
        with self.assertRaisesRegexp(ValueError, 'cucumber'):
            @quickcache(['cucumber'], cache=_cache)
            def square(number):
                return number * number

    def test_weird_data(self):
        @quickcache(['bytes'])
        def encode(bytes):
            return hash(bytes)

        symbols = '!@#$%^&*():{}"?><.~`'
        bytes = b'\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09'
        self.assertEqual(encode(symbols), hash(symbols))
        self.assertEqual(encode(bytes), hash(bytes))

    def test_lots_of_args(self):
        @quickcache('abcdef')
        def lots_of_args(a, b, c, d, e, f):
            pass

        # doesn't fail
        lots_of_args(u"x", u"x", u"x", u"x", u"x", u"x")
        key = lots_of_args.get_cache_key(u"x", u"x", u"x", u"x", u"x", u"x")
        self.assertLess(len(key), 250)
        # assert it's actually been hashed
        self.assertEqual(
            len(key), len('quickcache.lots_of_args.xxxxxxxx/H') + 32, key)

    def test_really_long_function_name(self):
        @quickcache([])
        def aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa():
            """60 a's in a row"""
            pass

        # doesn't fail
        aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa()
        key = (aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
               .get_cache_key())
        self.assertEqual(
            len(key), len('quickcache.' + 'a' * 40 + '...xxxxxxxx/'), key)

    def test_vary_on_func(self):
        def vary_on(data):
            return [data['name']]

        @quickcache(vary_on=vary_on)
        def cached_fn(data):
            pass

        key = cached_fn.get_cache_key({'name': 'a1'})
        self.assertRegexpMatches(key, 'quickcache.cached_fn.[a-z0-9]{8}/u[a-z0-9]{32}')

    def test_unicode_string(self):
        @quickcache(['name'], cache=_cache)
        def by_name(name):
            BUFFER.append('called')
            return 'VALUE'

        name_unicode = u'namé'
        name_utf8 = name_unicode.encode('utf-8')

        self.assertEqual(by_name(name_unicode), 'VALUE')
        self.assertEqual(self.consume_buffer(), ['local miss', 'shared miss', 'called'])
        self.assertEqual(by_name(name_unicode), 'VALUE')
        self.assertEqual(self.consume_buffer(), ['local hit'])

        self.assertEqual(by_name(name_utf8), 'VALUE')
        self.assertEqual(self.consume_buffer(), ['local hit'])

    def test_skippable(self):
        @quickcache(['name'], cache=_cache_with_set, skip_arg='force')
        def by_name(name, force=False):
            BUFFER.append('called')
            return 'VALUE'

        name = 'name'
        self.assertEqual(by_name(name), 'VALUE')
        self.assertEqual(self.consume_buffer(), ['cache miss', 'called', 'cache set'])
        self.assertEqual(by_name(name), 'VALUE')
        self.assertEqual(self.consume_buffer(), ['cache hit'])

        self.assertEqual(by_name(name, force=True), 'VALUE')
        self.assertEqual(self.consume_buffer(), ['called', 'cache set'])
        self.assertEqual(by_name(name), 'VALUE')
        self.assertEqual(self.consume_buffer(), ['cache hit'])

    def test_skippable_fn(self):
        @quickcache(['name'], cache=_cache_with_set, skip_arg=lambda name: name == 'Ben')
        def by_name(name, force=False):
            BUFFER.append('called')
            return 'VALUE'

        name = 'name'
        self.assertEqual(by_name(name), 'VALUE')
        self.assertEqual(self.consume_buffer(), ['cache miss', 'called', 'cache set'])
        self.assertEqual(by_name(name), 'VALUE')
        self.assertEqual(self.consume_buffer(), ['cache hit'])

        name = 'Ben'
        self.assertEqual(by_name(name), 'VALUE')
        self.assertEqual(self.consume_buffer(), ['called', 'cache set'])
        self.assertEqual(by_name(name), 'VALUE')
        self.assertEqual(self.consume_buffer(), ['called', 'cache set'])

    def test_skippable_non_kwarg(self):
        @quickcache(['name'], cache=_cache_with_set, skip_arg='skip_cache')
        def by_name(name, skip_cache):
            BUFFER.append('called')
            return 'VALUE'

        name = 'name'
        self.assertEqual(by_name(name, False), 'VALUE')
        self.assertEqual(self.consume_buffer(), ['cache miss', 'called', 'cache set'])
        self.assertEqual(by_name(name, False), 'VALUE')
        self.assertEqual(self.consume_buffer(), ['cache hit'])

        self.assertEqual(by_name(name, True), 'VALUE')
        self.assertEqual(self.consume_buffer(), ['called', 'cache set'])
        self.assertEqual(by_name(name, False), 'VALUE')
        self.assertEqual(self.consume_buffer(), ['cache hit'])

    def test_skippable_validation(self):
        # skip_arg not supplied
        @quickcache(['name'])
        def by_name(name, skip_cache=False):
            return 'VALUE'

        # skip_arg also in vary_on
        with self.assertRaises(ValueError):
            @quickcache(['name', 'skip_cache'], skip_arg='skip_cache')
            def by_name(name, skip_cache=False):
                return 'VALUE'

        # skip_arg not in args
        with self.assertRaises(ValueError):
            @quickcache(['name'], skip_arg='missing')
            def by_name(name):
                return 'VALUE'

    def test_dict_arg(self):
        @quickcache(['dct'])
        def return_random(dct):
            return uuid.uuid4().hex
        value_1 = return_random({})
        self.assertEqual(return_random({}), value_1)

        value_2 = return_random({'abc': 123})
        self.assertEqual(return_random({'abc': 123}), value_2)
        self.assertNotEqual(value_2, value_1)

        value_3 = return_random({'abc': 123, 'def': 456})
        self.assertEqual(return_random({'abc': 123, 'def': 456}), value_3)
        self.assertNotEqual(value_3, value_1)
        self.assertNotEqual(value_3, value_2)

    def test_set_cached_value(self):
        @quickcache(['name'], cache=_cache)
        def return_name(name):
            BUFFER.append('called')
            return 'VALUE'

        name = 'name'
        # Test calling the cache as is
        self.assertEqual(return_name(name), 'VALUE')
        self.assertEqual(self.consume_buffer(), ['local miss', 'shared miss', 'called'])
        self.assertEqual(return_name(name), 'VALUE')
        self.assertEqual(self.consume_buffer(), ['local hit'])

        # Test resetting the cached value and calling the cache again
        return_name.set_cached_value(name).to('NEW VALUE')
        self.assertEqual(return_name(name), 'NEW VALUE')
        self.assertEqual(self.consume_buffer(), ['local hit'])
