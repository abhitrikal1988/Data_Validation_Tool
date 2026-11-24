import logging
import cx_Oracle

# Custom Lib Import
from .Read_config import config_read
cr= config_read()

logger = logging.getLogger(__name__)

updRec: str = 'U'
insRec: str = 'I'

class OracleConnection():
    """
        Class for Oracle Database Operations
    """

    def connect_oracle(self, Environment):
        """ Establish a connection to the Oracle database """
        try:
            oracle_user_connection=cr.oracle_db_connection(Environment)
            connection = cx_Oracle.connect(oracle_user_connection)
            return connection
        except cx_Oracle.DatabaseError as e:
            logger.error(f"Error connecting to Oracle DB: {e}")
            raise

    def __init__(self, Environment):
        self.DbConnection = self.connect_oracle(Environment)
        # self.cursor = self.DBConnection.cursor()

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
                logger.error(f"Error {error.message} at row offset {error.offset}: ")
                logger.error(f"{str(DataList[error.offset])}")
        except Exception as exp:
            logger.exception(f"exception : {exp}")
            logger.error(f'DataList : {DataList}')
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
            result = cursor.execute(selectsql).fetchone()
            return result[0] if result else None
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
            logger.error(f"Error {error.message} at row offset {error.offset}: ")
            logger.error(f"{str(self.DataList[error.offset])} \n")

