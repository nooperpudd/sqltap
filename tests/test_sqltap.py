from __future__ import print_function

from sqlalchemy import *  # noqa
from sqlalchemy.orm import *  # noqa
from sqlalchemy.ext.declarative import declarative_base
import sqlalchemy.event
import sqlparse
import nose.tools
from werkzeug.test import Client
from werkzeug.wrappers import BaseResponse

import sqltap
import sqltap.wsgi


REPORT_TITLE = "SQLTap Profiling Report"


def _startswith(qs, text):
    return list(filter(lambda q: str(q.text).strip().startswith(text), qs))


class MockResults(object):
    def __init__(self, rowcount):
        self.rowcount = rowcount


class TestSQLTap(object):

    def setUp(self):
        self.engine = create_engine('sqlite:///:memory:', echo=True)

        Base = declarative_base(bind=self.engine)

        class A(Base):
            __tablename__ = "a"
            id = Column("id", Integer, primary_key=True)
        self.A = A

        Base.metadata.create_all(self.engine)

        self.Session = sessionmaker(bind=self.engine)

    def assertEqual(self, expected, actual, message=None):
        message = message or "{0!r} == {1!r}".format(expected, actual)
        assert expected == actual, message

    def test_insert(self):
        """ Simple test that sqltap collects an insert query. """
        profiler = sqltap.start(self.engine)

        sess = self.Session()
        sess.add(self.A())
        sess.flush()

        stats = profiler.collect()
        assert len(_startswith(stats, 'INSERT')) == 1
        profiler.stop()

    def test_select(self):
        """ Simple test that sqltap collects a select query. """
        profiler = sqltap.start(self.engine)

        sess = self.Session()
        sess.query(self.A).all()

        stats = profiler.collect()
        assert len(_startswith(stats, 'SELECT')) == 1
        profiler.stop()

    def test_engine_scoped(self):
        """
        Test that calling sqltap.start with a particular engine instance
        properly captures queries only to that engine.
        """
        engine2 = create_engine('sqlite:///:memory:', echo=True)

        Base = declarative_base(bind=engine2)

        class B(Base):
            __tablename__ = "b"
            id = Column("id", Integer, primary_key=True)

        Base.metadata.create_all(engine2)
        Session = sessionmaker(bind=engine2)
        profiler = sqltap.start(engine2)

        sess = self.Session()
        sess.query(self.A).all()

        sess2 = Session()
        sess2.query(B).all()

        stats = _startswith(profiler.collect(), 'SELECT')
        assert len(stats) == 1
        profiler.stop()

    def test_engine_global(self):
        """
        Test that registering globally for all queries correctly pulls queries
        from multiple engines.
        """
        engine2 = create_engine('sqlite:///:memory:', echo=True)

        Base = declarative_base(bind=engine2)

        class B(Base):
            __tablename__ = "b"
            id = Column("id", Integer, primary_key=True)

        Base.metadata.create_all(engine2)
        Session = sessionmaker(bind=engine2)
        profiler = sqltap.start()

        sess = self.Session()
        sess.query(self.A).all()

        sess2 = Session()
        sess2.query(B).all()

        stats = _startswith(profiler.collect(), 'SELECT')
        assert len(stats) == 2
        profiler.stop()

    def test_start_twice(self):
        """
        Ensure that multiple calls to ProfilingSession.start() raises assertion
        error.
        """
        profiler = sqltap.ProfilingSession(self.engine)
        profiler.start()
        try:
            profiler.start()
            raise ValueError("Second start should have asserted")
        except AssertionError:
            pass
        except:
            assert False, "Got some non-assertion exception"
        profiler.stop()

    def test_stop(self):
        """
        Ensure queries after you call ProfilingSession.stop() are not recorded.
        """
        profiler = sqltap.start(self.engine)
        sess = self.Session()
        sess.query(self.A).all()
        profiler.stop()
        sess.query(self.A).all()

        assert len(profiler.collect()) == 1

    def test_stop_global(self):
        """
        Ensure queries after you call ProfilingSession.stop() are not recorded
        when passing in the 'global' Engine object to record queries across all
        engines.
        """
        profiler = sqltap.start()
        sess = self.Session()
        sess.query(self.A).all()
        profiler.stop()
        sess.query(self.A).all()

        assert len(profiler.collect()) == 1

    def test_querygroup_add_params_no_dup(self):
        """Ensure that two identical parameter sets, belonging to different queries,
        are treated as separate."""
        python_query = 'SELECT * FROM pythons WHERE name=:name'
        directors_query = 'SELECT * FROM movies WHERE director=:name'
        jones = {'name': 'Terry Jones'}
        gilliam = {'name': 'Terry Gilliam'}
        group = sqltap.QueryGroup()
        group.add(sqltap.QueryStats(
            python_query, 'stack1', 1, 2, None, jones, MockResults(1)))
        group.add(sqltap.QueryStats(
            directors_query, 'stack2', 3, 4, None, jones, MockResults(4)))
        group.add(sqltap.QueryStats(
            python_query, 'stack1', 1, 2, None, gilliam, MockResults(1)))
        group.add(sqltap.QueryStats(
            directors_query, 'stack2', 3, 4, None, gilliam, MockResults(12)))
        group.add(sqltap.QueryStats(
            python_query, 'stack1', 1, 2, None, gilliam, MockResults(1)))
        group.add(sqltap.QueryStats(
            directors_query, 'stack9', 3, 4, None, gilliam, MockResults(12)))

        self.assertEqual(3, len(group.stacks))
        self.assertEqual(set([1, 2, 3]), set(group.stacks.values()))

        self.assertEqual(4, len(group.params_hashes))
        gilliam_movie_queries = group.params_hashes[
            (hash(directors_query),
             sqltap.QueryStats.calculate_params_hash(gilliam))]
        jones_movie_queries = group.params_hashes[
            (hash(directors_query),
             sqltap.QueryStats.calculate_params_hash(jones))]
        self.assertEqual(1, jones_movie_queries[0])
        self.assertEqual(jones, jones_movie_queries[2])
        self.assertEqual(2, gilliam_movie_queries[0])
        self.assertEqual(gilliam, gilliam_movie_queries[2])

    def test_report(self):
        profiler = sqltap.start(self.engine)

        sess = self.Session()
        q = sess.query(self.A)
        qtext = sqltap.format_sql(str(q))
        q.all()

        stats = profiler.collect()
        report = sqltap.report(stats, report_format="html")
        assert REPORT_TITLE in report
        assert qtext in report
        report = sqltap.report(stats, report_format="text")
        assert REPORT_TITLE in report
        assert sqlparse.format(qtext, reindent=True) in report
        profiler.stop()

    def test_report_raw_sql(self):
        """ Ensure that reporting works when raw SQL queries were emitted. """
        profiler = sqltap.start(self.engine)

        sess = self.Session()
        sql = 'SELECT * FROM %s' % self.A.__tablename__
        sess.connection().execute(sql)

        stats = profiler.collect()
        report = sqltap.report(stats, report_format="html")
        assert REPORT_TITLE in report
        assert sqltap.format_sql(sql) in report
        report = sqltap.report(stats, report_format="text")
        assert REPORT_TITLE in report
        assert sqlparse.format(sql, reindent=True) in report
        profiler.stop()

    def test_report_ddl(self):
        """ Ensure that reporting works when DDL were emitted """
        engine2 = create_engine('sqlite:///:memory:', echo=True)
        Base2 = declarative_base(bind=engine2)

        class B(Base2):
            __tablename__ = "b"
            id = Column("id", Integer, primary_key=True)

        profiler = sqltap.start(engine2)
        Base2.metadata.create_all(engine2)

        stats = profiler.collect()
        report = sqltap.report(stats, report_format="html")
        assert REPORT_TITLE in report
        report = sqltap.report(stats, report_format="text")
        assert REPORT_TITLE in report
        profiler.stop()

    def test_no_before_exec(self):
        """
        If SQLTap is started dynamically on one thread,
        any SQLAlchemy sessions running on other threads start being profiled.
        Their connections did not receive the before_execute event,
        so when they receive the after_execute event, extra care must be taken.
        """
        profiler = sqltap.ProfilingSession(self.engine)
        sqlalchemy.event.listen(self.engine, "after_execute",
                                profiler._after_exec)
        sess = self.Session()
        q = sess.query(self.A)
        q.all()
        stats = profiler.collect()
        assert len(stats) == 1
        assert stats[0].duration == 0.0, str(stats[0].duration)
        sqlalchemy.event.remove(self.engine, "after_execute",
                                profiler._after_exec)

    def test_report_aggregation(self):
        """
        Test that we aggregate stats for the same query called from
        different locations as well as aggregating queries called
        from the same stack trace.
        """
        profiler = sqltap.start(self.engine)

        sess = self.Session()
        q = sess.query(self.A)

        q.all()
        q.all()

        q2 = sess.query(self.A).filter(self.A.id == 10)
        for i in range(10):
            q2.all()

        report = sqltap.report(profiler.collect())
        print(report)
        assert '2 unique' in report
        assert '<dd>10</dd>' in report
        profiler.stop()

    def test_start_stop(self):
        sess = self.Session()
        q = sess.query(self.A)
        profiled = sqltap.ProfilingSession(self.engine)

        q.all()

        stats = profiled.collect()
        assert len(_startswith(stats, 'SELECT')) == 0

        profiled.start()
        q.all()
        q.all()
        profiled.stop()
        q.all()

        stats2 = profiled.collect()
        assert len(_startswith(stats2, 'SELECT')) == 2

    def test_decorator(self):
        """ Test that queries issued in a decorated function are profiled """
        sess = self.Session()
        q = sess.query(self.A)
        profiled = sqltap.ProfilingSession(self.engine)

        @profiled
        def test_function():
            self.Session().query(self.A).all()

        q.all()

        stats = profiled.collect()
        assert len(_startswith(stats, 'SELECT')) == 0

        test_function()
        test_function()
        q.all()

        stats = profiled.collect()
        assert len(_startswith(stats, 'SELECT')) == 2

    def test_context_manager(self):
        sess = self.Session()
        q = sess.query(self.A)
        profiled = sqltap.ProfilingSession(self.engine)

        with profiled:
            q.all()

        q.all()

        stats = profiled.collect()
        assert len(_startswith(stats, 'SELECT')) == 1

    def test_context_fn(self):
        profiler = sqltap.start(self.engine, lambda *args: 1)

        sess = self.Session()
        q = sess.query(self.A)
        q.all()
        stats = profiler.collect()

        ctxs = [qstats.user_context for qstats in _startswith(stats, 'SELECT')]
        assert ctxs[0] == 1
        profiler.stop()

    def test_context_fn_isolation(self):
        x = {"i": 0}

        def context_fn(*args):
            x['i'] += 1
            return x['i']

        profiler = sqltap.start(self.engine, context_fn)

        sess = self.Session()
        sess.query(self.A).all()

        sess2 = Session()
        sess2.query(self.A).all()

        stats = profiler.collect()

        ctxs = [qstats.user_context for qstats in _startswith(stats, 'SELECT')]
        assert ctxs.count(1) == 1
        assert ctxs.count(2) == 1
        profiler.stop()

    def test_collect_empty(self):
        profiler = sqltap.start(self.engine)
        assert len(profiler.collect()) == 0
        profiler.stop()

    def test_collect_fn(self):
        collection = []

        def my_collector(q):
            collection.append(q)

        sess = self.Session()
        profiler = sqltap.start(self.engine, collect_fn=my_collector)
        sess.query(self.A).all()

        assert len(collection) == 1

        sess.query(self.A).all()

        assert len(collection) == 2
        profiler.stop()

    @nose.tools.raises(AssertionError)
    def test_collect_fn_execption_on_collect(self):
        def noop():
            pass
        profiler = sqltap.start(self.engine, collect_fn=noop)
        profiler.collect()
        profiler.stop()

    def test_report_escaped(self):
        """ Test that string escaped correctly. """
        engine2 = create_engine('sqlite:///:memory:', echo=True)

        Base = declarative_base(bind=engine2)

        class B(Base):
            __tablename__ = "b"
            id = Column("id", Unicode, primary_key=True)

        Base.metadata.create_all(engine2)
        Session = sessionmaker(bind=engine2)
        profiler = sqltap.start(engine2)

        sess = Session()
        sess.query(B).filter(B.id == u"<blockquote class='test'>").all()

        report = sqltap.report(profiler.collect())
        assert "<blockquote class='test'>" not in report
        assert "&#34;&lt;blockquote class=&#39;test&#39;&gt;&#34;" in report
        profiler.stop()

    def test_context_return_self(self):
        with sqltap.ProfilingSession() as profiler:
            assert type(profiler) is sqltap.ProfilingSession


class TestSQLTapMiddleware(TestSQLTap):

    def setUp(self):
        super(TestSQLTapMiddleware, self).setUp()
        from werkzeug.testapp import test_app
        self.app = sqltap.wsgi.SQLTapMiddleware(app=test_app)
        self.client = Client(self.app, BaseResponse)

    def test_can_construct_wsgi_wrapper(self):
        """
        Only verifies that the imports and __init__ work, not a real Test.
        """
        sqltap.wsgi.SQLTapMiddleware(self.app)

    def test_wsgi_get_request(self):
        """Verify we can get the middleware path"""
        response = self.client.get(self.app.path)
        assert response.status_code == 200
        assert 'text/html' in response.headers['content-type']

    def test_wsgi_post_turn_on(self):
        """Verify we can POST turn=on to middleware"""
        response = self.client.post(self.app.path, data='turn=on')
        assert response.status_code == 200
        assert 'text/html' in response.headers['content-type']

    def test_wsgi_post_turn_off(self):
        """Verify we can POST turn=off to middleware"""
        response = self.client.post(self.app.path, data='turn=off')
        assert response.status_code == 200
        assert 'text/html' in response.headers['content-type']

    def test_wsgi_post_turn_400(self):
        """Verify we POSTing and invalid turn value returns a 400"""
        response = self.client.post(self.app.path, data='turn=invalid_string')
        assert response.status_code == 400
        assert 'text/plain' in response.headers['content-type']

    def test_wsgi_post_clear(self):
        """Verify we can POST clean=1 works"""
        response = self.client.post(self.app.path, data='clear=1')
        assert response.status_code == 200
        assert 'text/html' in response.headers['content-type']
