import glob
import os
import re
import subprocess
import uuid

from ccmlib import common
from dtest import Tester, debug
from tools import since, require
import time

KEYSPACE = 'ks'


class TestHelper(Tester):

    def get_table_path(self, table):
        """
        Return the path where the table sstables are located
        """
        node1 = self.cluster.nodelist()[0]
        path = ""
        basepath = os.path.join(node1.get_path(), 'data', KEYSPACE)
        for x in os.listdir(basepath):
            if x.startswith(table):
                path = os.path.join(basepath, x)
                break
        return path

    def get_index_path(self, table, index):
        """
        Return the path where the index sstables are located
        """
        path = self.get_table_path(table)
        return os.path.join(path, '.' + index)

    def get_sstable_files(self, path):
        """
        Return the sstable files at a specific location
        """
        ret = []
        debug('Checking sstables in %s' % (path))

        for ext in ('*.db', '*.txt', '*.adler32', '*.sha1'):
            for fname in glob.glob(os.path.join(path, ext)):
                bname = os.path.basename(fname)
                debug('Found sstable file %s' % (bname))
                ret.append(bname)
        return ret

    def delete_non_essential_sstable_files(self, table):
        """
        Delete all sstable files except for the -Data.db file and the
        -Statistics.db file (only available in >= 3.0)
        """
        for fname in self.get_sstable_files(self.get_table_path(table)):
            if not fname.endswith("-Data.db") and not fname.endswith("-Statistics.db"):
                fullname = os.path.join(self.get_table_path(table), fname)
                debug('Deleting {}'.format(fullname))
                os.remove(fullname)

    def get_sstables(self, table, indexes):
        """
        Return the sstables for a table and the specified indexes of this table
        """
        sstables = {}
        table_sstables = self.get_sstable_files(self.get_table_path(table))
        assert len(table_sstables) > 0
        sstables[table] = sorted(table_sstables)

        for index in indexes:
            index_sstables = self.get_sstable_files(self.get_index_path(table, index))
            assert len(index_sstables) > 0
            sstables[index] = sorted('%s/%s' % (index, sstable) for sstable in index_sstables)

        return sstables

    def launch_nodetool_cmd(self, cmd):
        """
        Launch a nodetool command and check the result is empty (no error)
        """
        node1 = self.cluster.nodelist()[0]
        response = node1.nodetool(cmd, capture_output=True)[0]
        assert len(response) == 0  # nodetool does not print anything unless there is an error

    def launch_standalone_scrub(self, ks, cf):
        """
        Launch the standalone scrub
        """
        node1 = self.cluster.nodelist()[0]
        env = common.make_cassandra_env(node1.get_install_cassandra_root(), node1.get_node_cassandra_root())
        scrub_bin = node1.get_tool('sstablescrub')
        debug(scrub_bin)

        args = [scrub_bin, ks, cf]
        p = subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = p.communicate()
        debug(out)
        if err:
            debug(err)
            assert False, 'sstablescrub failed'

    def perform_node_tool_cmd(self, cmd, table, indexes):
        """
        Perform a nodetool command on a table and the indexes specified
        """
        self.launch_nodetool_cmd('%s %s %s' % (cmd, KEYSPACE, table))
        for index in indexes:
            self.launch_nodetool_cmd('%s %s %s.%s' % (cmd, KEYSPACE, table, index))

    def flush(self, table, *indexes):
        """
        Flush table and indexes via nodetool, and then return all sstables
        in a dict keyed by the table or index name.
        """
        self.perform_node_tool_cmd('flush', table, indexes)
        return self.get_sstables(table, indexes)

    def scrub(self, table, *indexes):
        """
        Scrub table and indexes via nodetool, and then return all sstables
        in a dict keyed by the table or index name.
        """
        self.perform_node_tool_cmd('scrub', table, indexes)
        return self.get_sstables(table, indexes)

    def standalonescrub(self, table, *indexes):
        """
        Launch standalone scrub on table and indexes, and then return all sstables
        in a dict keyed by the table or index name.
        """
        self.launch_standalone_scrub(KEYSPACE, table)
        for index in indexes:
            self.launch_standalone_scrub(KEYSPACE, '%s.%s' % (table, index))
        return self.get_sstables(table, indexes)

    def increment_generation_by(self, sstable, generation_increment):
        """
        Set the generation number for an sstable file name
        """
        return re.sub('(\d(?!\d))\-', lambda x: str(int(x.group(1)) + generation_increment) + '-', sstable)

    def increase_sstable_generations(self, sstables):
        """
        After finding the number of existing sstables, increase all of the
        generations by that amount.
        """
        for table_or_index, table_sstables in sstables.items():
            increment_by = len(set(re.match('.*(\d)[^0-9].*', s).group(1) for s in table_sstables))
            sstables[table_or_index] = [self.increment_generation_by(s, increment_by) for s in table_sstables]

        debug('sstables after increment %s' % (str(sstables)))

@since('2.2')
class TestScrubIndexes(TestHelper):
    """
    Test that we scrub indexes as well as their parent tables
    """

    def create_users(self, cursor):
        columns = {"password": "varchar", "gender": "varchar", "session_token": "varchar", "state": "varchar", "birth_year": "bigint"}
        self.create_cf(cursor, 'users', columns=columns)

        cursor.execute("CREATE INDEX gender_idx ON users (gender)")
        cursor.execute("CREATE INDEX state_idx ON users (state)")
        cursor.execute("CREATE INDEX birth_year_idx ON users (birth_year)")

    def update_users(self, cursor):
        cursor.execute("INSERT INTO users (KEY, password, gender, state, birth_year) VALUES ('user1', 'ch@ngem3a', 'f', 'TX', 1978)")
        cursor.execute("INSERT INTO users (KEY, password, gender, state, birth_year) VALUES ('user2', 'ch@ngem3b', 'm', 'CA', 1982)")
        cursor.execute("INSERT INTO users (KEY, password, gender, state, birth_year) VALUES ('user3', 'ch@ngem3c', 'f', 'TX', 1978)")
        cursor.execute("INSERT INTO users (KEY, password, gender, state, birth_year) VALUES ('user4', 'ch@ngem3d', 'm', 'CA', 1982)")
        cursor.execute("INSERT INTO users (KEY, password, gender, state, birth_year) VALUES ('user5', 'ch@ngem3e', 'f', 'TX', 1978)")
        cursor.execute("INSERT INTO users (KEY, password, gender, state, birth_year) VALUES ('user6', 'ch@ngem3f', 'm', 'CA', 1982)")
        cursor.execute("INSERT INTO users (KEY, password, gender, state, birth_year) VALUES ('user7', 'ch@ngem3g', 'f', 'TX', 1978)")
        cursor.execute("INSERT INTO users (KEY, password, gender, state, birth_year) VALUES ('user8', 'ch@ngem3h', 'm', 'CA', 1982)")

        cursor.execute("DELETE FROM users where KEY = 'user1'")
        cursor.execute("DELETE FROM users where KEY = 'user5'")
        cursor.execute("DELETE FROM users where KEY = 'user7'")

    def query_users(self, cursor):
        ret = cursor.execute("SELECT * FROM users")
        ret.extend(cursor.execute("SELECT * FROM users WHERE state='TX'"))
        ret.extend(cursor.execute("SELECT * FROM users WHERE gender='f'"))
        ret.extend(cursor.execute("SELECT * FROM users WHERE birth_year=1978"))
        assert len(ret) == 8
        return ret

    def test_scrub_static_table(self):
        cluster = self.cluster
        cluster.populate(1).start()
        node1 = cluster.nodelist()[0]

        cursor = self.patient_cql_connection(node1)
        self.create_ks(cursor, KEYSPACE, 1)

        self.create_users(cursor)
        self.update_users(cursor)

        initial_users = self.query_users(cursor)
        initial_sstables = self.flush('users', 'gender_idx', 'state_idx', 'birth_year_idx')
        scrubbed_sstables = self.scrub('users', 'gender_idx', 'state_idx', 'birth_year_idx')

        self.increase_sstable_generations(initial_sstables)
        self.assertEqual(initial_sstables, scrubbed_sstables)

        users = self.query_users(cursor)
        self.assertEqual(initial_users, users)

        # Scrub and check sstables and data again
        scrubbed_sstables = self.scrub('users', 'gender_idx', 'state_idx', 'birth_year_idx')
        self.increase_sstable_generations(initial_sstables)
        self.assertEqual(initial_sstables, scrubbed_sstables)

        users = self.query_users(cursor)
        self.assertEqual(initial_users, users)

        # Restart and check data again
        cluster.stop()
        cluster.start()

        cursor = self.patient_cql_connection(node1)
        cursor.execute('USE %s' % (KEYSPACE))

        users = self.query_users(cursor)
        self.assertEqual(initial_users, users)

    def test_standalone_scrub(self):
        cluster = self.cluster
        cluster.populate(1).start()
        node1 = cluster.nodelist()[0]

        cursor = self.patient_cql_connection(node1)
        self.create_ks(cursor, KEYSPACE, 1)

        self.create_users(cursor)
        self.update_users(cursor)

        initial_users = self.query_users(cursor)
        initial_sstables = self.flush('users', 'gender_idx', 'state_idx', 'birth_year_idx')

        cluster.stop()

        scrubbed_sstables = self.standalonescrub('users', 'gender_idx', 'state_idx', 'birth_year_idx')
        self.increase_sstable_generations(initial_sstables)
        self.assertEqual(initial_sstables, scrubbed_sstables)

        cluster.start()
        cursor = self.patient_cql_connection(node1)
        cursor.execute('USE %s' % (KEYSPACE))

        users = self.query_users(cursor)
        self.assertEqual(initial_users, users)

    def test_scrub_collections_table(self):
        cluster = self.cluster
        cluster.populate(1).start()
        node1 = cluster.nodelist()[0]

        cursor = self.patient_cql_connection(node1)
        self.create_ks(cursor, KEYSPACE, 1)

        cursor.execute("CREATE TABLE users (user_id uuid PRIMARY KEY, email text, uuids list<uuid>)")
        cursor.execute("CREATE INDEX user_uuids_idx on users (uuids)")

        _id = uuid.uuid4()
        num_users = 100
        for i in range(0, num_users):
            user_uuid = uuid.uuid4()
            cursor.execute(("INSERT INTO users (user_id, email) values ({user_id}, 'test@example.com')").format(user_id=user_uuid))
            cursor.execute(("UPDATE users set uuids = [{id}] where user_id = {user_id}").format(id=_id, user_id=user_uuid))

        initial_users = cursor.execute(("SELECT * from users where uuids contains {some_uuid}").format(some_uuid=_id))
        self.assertEqual(num_users, len(initial_users))

        initial_sstables = self.flush('users', 'user_uuids_idx')
        scrubbed_sstables = self.scrub('users', 'user_uuids_idx')

        self.increase_sstable_generations(initial_sstables)
        self.assertEqual(initial_sstables, scrubbed_sstables)

        users = cursor.execute(("SELECT * from users where uuids contains {some_uuid}").format(some_uuid=_id))
        self.assertEqual(initial_users, users)

        scrubbed_sstables = self.scrub('users', 'user_uuids_idx')

        self.increase_sstable_generations(initial_sstables)
        self.assertEqual(initial_sstables, scrubbed_sstables)

        users = cursor.execute(("SELECT * from users where uuids contains {some_uuid}").format(some_uuid=_id))

        self.assertListEqual(initial_users, users)

class TestScrub(TestHelper):
    """
    Generic tests for scrubbing
    """
    def create_users(self, cursor):
        columns = {"password": "varchar", "gender": "varchar", "session_token": "varchar", "state": "varchar", "birth_year": "bigint"}
        self.create_cf(cursor, 'users', columns=columns)

    def update_users(self, cursor):
        cursor.execute("INSERT INTO users (KEY, password, gender, state, birth_year) VALUES ('user1', 'ch@ngem3a', 'f', 'TX', 1978)")
        cursor.execute("INSERT INTO users (KEY, password, gender, state, birth_year) VALUES ('user2', 'ch@ngem3b', 'm', 'CA', 1982)")
        cursor.execute("INSERT INTO users (KEY, password, gender, state, birth_year) VALUES ('user3', 'ch@ngem3c', 'f', 'TX', 1978)")
        cursor.execute("INSERT INTO users (KEY, password, gender, state, birth_year) VALUES ('user4', 'ch@ngem3d', 'm', 'CA', 1982)")
        cursor.execute("INSERT INTO users (KEY, password, gender, state, birth_year) VALUES ('user5', 'ch@ngem3e', 'f', 'TX', 1978)")
        cursor.execute("INSERT INTO users (KEY, password, gender, state, birth_year) VALUES ('user6', 'ch@ngem3f', 'm', 'CA', 1982)")
        cursor.execute("INSERT INTO users (KEY, password, gender, state, birth_year) VALUES ('user7', 'ch@ngem3g', 'f', 'TX', 1978)")
        cursor.execute("INSERT INTO users (KEY, password, gender, state, birth_year) VALUES ('user8', 'ch@ngem3h', 'm', 'CA', 1982)")

        cursor.execute("DELETE FROM users where KEY = 'user1'")
        cursor.execute("DELETE FROM users where KEY = 'user5'")
        cursor.execute("DELETE FROM users where KEY = 'user7'")

    def query_users(self, cursor):
        ret = cursor.execute("SELECT * FROM users")
        assert len(ret) == 5
        return ret

    def test_nodetool_scrub(self):
        cluster = self.cluster
        cluster.populate(1).start()
        node1 = cluster.nodelist()[0]

        cursor = self.patient_cql_connection(node1)
        self.create_ks(cursor, KEYSPACE, 1)

        self.create_users(cursor)
        self.update_users(cursor)

        initial_users = self.query_users(cursor)
        initial_sstables = self.flush('users')
        scrubbed_sstables = self.scrub('users')

        self.increase_sstable_generations(initial_sstables)
        self.assertEqual(initial_sstables, scrubbed_sstables)

        users = self.query_users(cursor)
        self.assertEqual(initial_users, users)

        # Scrub and check sstables and data again
        scrubbed_sstables = self.scrub('users')
        self.increase_sstable_generations(initial_sstables)
        self.assertEqual(initial_sstables, scrubbed_sstables)

        users = self.query_users(cursor)
        self.assertEqual(initial_users, users)

        # Restart and check data again
        cluster.stop()
        cluster.start()

        cursor = self.patient_cql_connection(node1)
        cursor.execute('USE %s' % (KEYSPACE))

        users = self.query_users(cursor)
        self.assertEqual(initial_users, users)

    def test_standalone_scrub(self):
        cluster = self.cluster
        cluster.populate(1).start()
        node1 = cluster.nodelist()[0]

        cursor = self.patient_cql_connection(node1)
        self.create_ks(cursor, KEYSPACE, 1)

        self.create_users(cursor)
        self.update_users(cursor)

        initial_users = self.query_users(cursor)
        initial_sstables = self.flush('users')

        cluster.stop()

        scrubbed_sstables = self.standalonescrub('users')
        self.increase_sstable_generations(initial_sstables)
        self.assertEqual(initial_sstables, scrubbed_sstables)

        cluster.start()
        cursor = self.patient_cql_connection(node1)
        cursor.execute('USE %s' % (KEYSPACE))

        users = self.query_users(cursor)
        self.assertEqual(initial_users, users)

    @require('9591*')
    def test_standalone_scrub_essential_files_only(self):
        cluster = self.cluster
        cluster.populate(1).start()
        node1 = cluster.nodelist()[0]

        cursor = self.patient_cql_connection(node1)
        self.create_ks(cursor, KEYSPACE, 1)

        self.create_users(cursor)
        self.update_users(cursor)

        initial_users = self.query_users(cursor)
        initial_sstables = self.flush('users')

        cluster.stop()

        self.delete_non_essential_sstable_files('users')

        scrubbed_sstables = self.standalonescrub('users')
        self.increase_sstable_generations(initial_sstables)
        self.assertEqual(initial_sstables, scrubbed_sstables)

        cluster.start()
        cursor = self.patient_cql_connection(node1)
        cursor.execute('USE %s' % (KEYSPACE))

        users = self.query_users(cursor)
        self.assertEqual(initial_users, users)

    @since('2.1')
    def test_scrub_with_UDT(self):
        """
        @jira_ticket CASSANDRA-7665
        """
        cluster = self.cluster
        cluster.populate(1).start()
        node1 = cluster.nodelist()[0]

        cursor = self.patient_cql_connection(node1)
        cursor.execute("CREATE KEYSPACE test WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1 };")
        cursor.execute("use test;")
        cursor.execute("CREATE TYPE point_t (x double, y double);")

        node1.nodetool("scrub")
        time.sleep(2)
        match = node1.grep_log("org.apache.cassandra.serializers.MarshalException: Not enough bytes to read a set")
        self.assertEqual(len(match), 0)
