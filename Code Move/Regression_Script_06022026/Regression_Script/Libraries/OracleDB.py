import logging
import re
import threading
import cx_Oracle

# Custom Lib Import
from .Read_config import config_read
cr= config_read()

logger = logging.getLogger(__name__)

updRec: str = 'U'
insRec: str = 'I'

# ─────────────────────────────────────────────────────────────────
# Oracle SessionPool cache — one pool per environment, shared
# across all OracleConnection() instances and all threads.
# Each OracleConnection.acquire()s a connection from the pool;
# .close() returns it to the pool (does NOT physically close it).
# Pool config: min=2, max=10, increment=1, threaded=True.
# Tune `max` to match your DB's allowed concurrent sessions.
# ─────────────────────────────────────────────────────────────────
_pools      = {}
_pools_lock = threading.Lock()
 
POOL_MIN       = 2
POOL_MAX       = 10
POOL_INCREMENT = 1
 
 
def _parse_conn_str(conn_str: str):
    """
    Splits an Oracle DSN string of the form 'user/password@dsn' into
    (user, password, dsn) parts. Required for SessionPool which takes
    them as separate arguments.
    """
    m = re.match(r'^([^/]+)/([^@]+)@(.+)$', conn_str.strip())
    if not m:
        raise ValueError(
            f"Oracle connection string must be in the format "
            f"'user/password@dsn', got: {conn_str!r}"
        )
    return m.group(1), m.group(2), m.group(3)
 
 
def _get_pool(environment: str) -> cx_Oracle.SessionPool:
    # Fast path
    if environment in _pools:
        return _pools[environment]
 
    # Slow path — create under lock with double-check
    with _pools_lock:
        if environment in _pools:
            return _pools[environment]
 
        conn_str = cr.oracle_db_connection(environment)
        user, password, dsn = _parse_conn_str(conn_str)
 
        try:
            pool = cx_Oracle.SessionPool(
                user      = user,
                password  = password,
                dsn       = dsn,
                min       = POOL_MIN,
                max       = POOL_MAX,
                increment = POOL_INCREMENT,
                threaded  = True,
                getmode   = cx_Oracle.SPOOL_ATTRVAL_WAIT
            )
            _pools[environment] = pool
            # logger.info(
            #     f"Oracle SessionPool created for environment '{environment}' "
            #     f"(min={POOL_MIN}, max={POOL_MAX})."
            # )
            return pool
        except cx_Oracle.DatabaseError as e:
            logger.issue(f"Failed to create Oracle SessionPool for '{environment}': {e}")
            raise
class OracleConnection():
    """
        Class for Oracle Database Operations
    """

    def connect_oracle(self, Environment):
        """ Acquire a connection from the per-environment SessionPool. """
        try:
            pool = _get_pool(Environment)
            return pool.acquire()
        except cx_Oracle.DatabaseError as e:
            logger.issue(f"Error acquiring Oracle connection: {e}")
            raise
 
    def __init__(self, Environment):
        self._environment = Environment
        self.DbConnection = self.connect_oracle(Environment)

    # ── Pool lifecycle ────────────────────────────────────────────
    def close(self):
        """
        Returns the connection to the SessionPool. With pooled connections
        calling .close() does NOT physically disconnect — it just releases
        the session back to the pool for reuse.
        """
        if getattr(self, "DbConnection", None) is not None:
            try:
                self.DbConnection.close()
            except Exception as e:
                logger.warning(f"Error releasing Oracle connection to pool: {e}")
            self.DbConnection = None
 
    def __del__(self):
        # Safety net — if caller forgets to close(), GC will release
        # the connection back to the pool when the object is collected.
        try:
            self.close()
        except Exception:
            pass

    def __del__(self):
        # Safety net — if caller forgets to close(), GC will release
        # the connection back to the pool when the object is collected.
        try:
            self.close()
        except Exception:
            pass
 
    # ── Context manager so users can: with OracleConnection(env) as c: ──
    def __enter__(self):
        return self
 
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
    def execute(self, Sql):
        # logger.info(f'sql : {Sql}')
        cursor = self.DbConnection.cursor()
        cursor.execute(
            statement=Sql
        )

    def fetchAllDict(self, Sql):
        # logger.info(f'sql : {Sql}')
        cursor = self.DbConnection.cursor()
        cursor.execute(Sql)

        columns = [col[0] for col in cursor.description]
        cursor.rowfactory = lambda *args: dict(zip(columns, args))
        data = cursor.fetchall()
        return data

    def fetchAllFilterDict(self, Sql, where):
        # logger.info(f'sql : {Sql}')
        cursor = self.DbConnection.cursor()
        cursor.execute(
            statement=Sql,
            owner=where
        )

        columns = [col[0] for col in cursor.description]
        cursor.rowfactory = lambda *args: dict(zip(columns, args))
        data = cursor.fetchall()
        return data

    def executeMany(self, Sql, DataList):
        try:
            # logger.info(f'sql : {Sql}')

            # self.DbConnection = conn
            cursor = self.DbConnection.cursor()
            logger.info(f'DB connection : {cursor}')
            cursor.executemany(
                statement=Sql,
                parameters=DataList,
                batcherrors=True,
                arraydmlrowcounts=True
            )
            # logger.info(f'DML Count : {cursor.getarraydmlrowcounts()}')
            count = 0
            for i in cursor.getarraydmlrowcounts():
                count += i
            logger.info(f'Count : {count} ')
            self.DbConnection.commit()

            for error in cursor.getbatcherrors():
                logger.issue(f"Error {error.message} at row offset {error.offset}: ")
                logger.issue(f"{str(DataList[error.offset])}")
        except Exception as exp:
            logger.exception(f"exception : {exp}")
            logger.issue(f'DataList : {DataList}')
            raise

    def fetchData(self, selectsql, whereparameter):
        cursor = self.DbConnection.cursor()
        return cursor.execute(
            statement=selectsql,
            parameters=whereparameter
        ).fetchall()

    def fetchAll(self, selectsql):
        try:
            cursor = self.DbConnection.cursor()
            return cursor.execute(selectsql).fetchall()
        except Exception as e:
            print(e)
    def fetchOne(self, selectsql):
        try:
            cursor = self.DbConnection.cursor()
            if "count(" in selectsql.lower():
                row = cursor.execute(selectsql).fetchone()
                return int(row[0]) if row and row[0] is not None else 0
            count_sql = f"SELECT COUNT(*) FROM ({selectsql}) q"
            row = cursor.execute(count_sql).fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        except Exception as e:
            print(e)
    def rec_type(self, selectsql, whereparameter):
        count = self.fetchData(selectsql, whereparameter).__len__()
        if count == 0:
            return insRec
        elif count >= 1:
            return updRec
        else:
            return None

class FetchData(OracleConnection):

    def __init__(self, selectsql, whereparameter):
        self.SelectSql = selectsql
        self.WhereParameter = whereparameter

    def get_table_data(self):
        return OracleConnection().cursor().execute(
            statement=self.SelectSql,
            parameters=self.WhereParameter
        ).fetchall()

    def rec_type(self):
        count = self.get_table_data().__len__()
        if count == 0:
            return insRec
        elif count == 1:
            return updRec
        else:
            return None

class DmlOperations(OracleConnection):

    def __init__(self, sql, data_list):
        self.Sql = sql
        self.DataList = data_list

    def execute(self):
        cursor = OracleConnection.cursor()
        cursor.executemany(
            statement=self.Sql,
            parameters=self.DataList,
            batcherrors=True,
            arraydmlrowcounts=True
        )
        logger.info(f'DML Count : {cursor.arraydmlrowcounts()}')
        self.DbConnection.commit()

        for error in cursor.getbatcherrors():
            logger.issue(f"Error {error.message} at row offset {error.offset}: ")
            logger.issue(f"{str(self.DataList[error.offset])} \n")

