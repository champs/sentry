"""
sentry.buffer.redis
~~~~~~~~~~~~~~~~~~~

:copyright: (c) 2010-2012 by the Sentry Team, see AUTHORS for more details.
:license: BSD, see LICENSE for more details.
"""

from django.db import models
from hashlib import md5
from nydus.db import create_cluster
from sentry.buffer import Buffer
from sentry.utils.compat import pickle


class RedisBuffer(Buffer):
    def __init__(self, hosts=None, router='nydus.db.routers.keyvalue.PartitionRouter', **options):
        super(RedisBuffer, self).__init__(**options)
        if hosts is None:
            hosts = {
                0: {}  # localhost / default
            }
        self.conn = create_cluster({
            'engine': 'nydus.db.backends.redis.Redis',
            'router': router,
            'hosts': hosts,
        })

    def _map_column(self, model, column, value):
        if isinstance(value, models.Model):
            value = value.pk
        else:
            value = unicode(value)
        return value

    def _make_key(self, model, filters, column):
        """
        Returns a Redis-compatible key for the model given filters.
        """
        return '%s:%s:%s' % (model._meta,
            md5('&'.join('%s=%s' % (k, self._map_column(model, k, v)) for k, v in sorted(filters.iteritems()))).hexdigest(),
            column)

    def _make_extra_key(self, model, filters):
        return '%s:extra:%s' % (model._meta,
            md5('&'.join('%s=%s' % (k, self._map_column(model, k, v)) for k, v in sorted(filters.iteritems()))).hexdigest())

    def incr(self, model, columns, filters, extra=None):
        with self.conn.map() as conn:
            for column, amount in columns.iteritems():
                conn.incr(self._make_key(model, filters, column), amount)

            # Store extra in a hashmap so it can easily be removed
            if extra:
                key = self._make_extra_key(model, filters)
                for column, value in extra.iteritems():
                    conn.hset(key, column, pickle.dumps(value))
        super(RedisBuffer, self).incr(model, columns, filters, extra)

    def process(self, model, columns, filters, extra=None):
        results = {}
        with self.conn.map() as conn:
            for column, amount in columns.iteritems():
                results[column] = conn.getset(self._make_key(model, filters, column), 0)

            hash_key = self._make_extra_key(model, filters)
            extra_results = conn.hgetall(hash_key)
            conn.delete(hash_key)

        # We combine the stored extra values with whatever was passed.
        # This ensures that static values get updated to their latest value,
        # and dynamic values (usually query expressions) are still dynamic.
        if extra_results:
            if not extra:
                extra = {}
            for key, value in extra_results.iteritems():
                if not value:
                    continue
                extra[key] = pickle.loads(str(value))

        # Filter out empty or zero'd results to avoid a potentially unnescesary update
        results = dict((k, int(v)) for k, v in results.iteritems() if int(v or 0) > 0)
        if not results:
            return
        super(RedisBuffer, self).process(model, results, filters, extra)