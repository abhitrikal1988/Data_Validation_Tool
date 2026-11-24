import logging
import pdb
import threading

from bson.codec_options import CodecOptions
from pymongo.read_preferences import Primary
from pymongo.cursor import Cursor
from pymongo.errors import BulkWriteError
from pymongo.mongo_client import MongoClient
from pymongo.operations import UpdateMany, UpdateOne
from pymongo.errors import ConnectionFailure, OperationFailure, ConfigurationError

# Custom Lib Import
from .Read_config import config_read, Timezone, MongoTimezone

cr = config_read()

logger = logging.getLogger(__name__)

ASCENDING = 1
"""Ascending sort order."""
DESCENDING = -1
"""Descending sort order."""

# ─────────────────────────────────────────────────────────────────
# MongoClient cache — one client per environment, shared across
# all Collection() instances and all threads. PyMongo's MongoClient
# is thread-safe and pools connections internally; reusing it
# avoids the TCP handshake + auth + ping cost on every Collection().
# ─────────────────────────────────────────────────────────────────

_clients = {}
_clients_lock = threading.Lock()


def _get_client(Environment: str) -> MongoClient:
    # Fast path — already cached
    if Environment in _clients:
        return _clients[Environment]

    # Slow path — create under lock with double-check
    with _clients_lock:
        if Environment in _clients:
            return _clients[Environment]

    try:
        username = cr.mongo_db_user_name(Environment)
        password = cr.mongo_db_password(Environment)
        ip_address = cr.mongo_db_ipaddress(Environment)
        replica_set = cr.mongo_db_replicaset(Environment)  # Add this method in Read_config if not present
        uri = f"mongodb://{username}:{password}@{ip_address}/?authSource=admin"
        if replica_set:
            uri += f"&replicaSet={replica_set}"

        client = MongoClient(uri, read_preference=Primary(),
                             maxPoolSize=50,  # max connections in pool
                             minPoolSize=5,  # pre-warm connections
                             maxConnecting=10,  # max simultaneous handshakes
                             # socketTimeoutMS      = 300_000,
                             connectTimeoutMS=100_000,
                             serverSelectionTimeoutMS=100_000
                             )

        # Force immediate connection to catch issues early
        client.admin.command("ping")
        _clients[Environment] = client
        # logger.info(f"MongoDB client cached for environment '{Environment}'.")
        return client
    except (ConnectionFailure, OperationFailure, ConfigurationError) as e:
        message = "Database Connection is not established"
        print(message)
        logger.issue(f"{message}\nReason: {e}")
        raise ConnectionError(message) from e


class Collection():  # collection
    """
        Class for Mongo DB Collection
    """
    # Dates are read in [Time_Zone] TIMEZONE when Conversion = Yes (default),
    # or in UTC as stored when Conversion = No.
    codec_option = CodecOptions(
        tz_aware=True,
        tzinfo=MongoTimezone
    )

    def connect_mongo(self, Environment):
        """  Returns the cached, shared MongoClient for this environment.
        :return:
        """
        # logger.info(MongoConfig.__dict__)
        # if MongoConfig.ReplicaSet is None:

        return _get_client(Environment)
        # return MongoClient(uri, read_preference=Primary())

    def __init__(self, db: str, collection: str, Environment):
        self.g_next_ids = None
        self.BlankDict: dict = {}
        self.Client = self.connect_mongo(Environment)
        if self.Client is None:
            raise ConnectionError(f"connect_mongo returned none for environment' {Environment}'. ")
        self.DB = self.Client[db]
        # print(self.DB)
        # logger.info(f'{self.DB}')
        self.DbCollection = self.DB[collection].with_options(codec_options=self.codec_option)

    def get_next_id(self, sequence_num_type: str):
        """      get_next_id   """
        next_id = getattr(self.g_next_ids, sequence_num_type) + 1
        setattr(self.g_next_ids, sequence_num_type, next_id)
        return next_id

    def truncateCollection(self):
        return self.DbCollection.delete_many(filter={})

    def DeleteMany(self, filter):
        return self.DbCollection.delete_many(filter=filter)

    def DeleteOne(self, filter):
        return self.DbCollection.delete_one(filter=filter)

    def getDataCount(self, filter: dict):  # type: ignore
        """  getDataCount   """
        return self.DbCollection.count_documents(
            filter=filter
        )

    def getData(self, filter: dict, batch_size: int = 2000):
        return self.DbCollection.with_options(
            codec_options=self.codec_option
        ).find(filter=filter).batch_size(batch_size)

    def getDataView(self, filter: dict, view: dict, batch_size: int = 2000):
        return self.DbCollection.with_options(
            codec_options=self.codec_option
        ).find(filter=filter, projection=view).batch_size(batch_size)

    def getDataSort(self, filter: dict, datasort):  # type: ignore
        """  getData   """
        return self.DbCollection.with_options(
            codec_options=self.codec_option
        ).find(
            filter=filter
        ).sort(
            datasort
        )

    def getDataViewSort(self, filter: dict, view: dict, datasort, datalimit: int):  # type: ignore
        """  getDataViewSort   """
        return self.DbCollection.with_options(
            codec_options=self.codec_option
        ).find(
            filter=filter,
            projection=view
        ).sort(
            datasort
        ).limit(
            limit=datalimit
        )

    def getDataLimit(self, criteria: dict, limit: int):  # type: ignore
        """ getDataLimit """
        return self.DbCollection.with_options(
            codec_options=self.codec_option
        ).find(
            filter=criteria
        ).limit(limit)

    def getFirstRec(self, criteria: dict) -> dict():  # type: ignore #
        """  getFirstRec   """
        for data in self.getData(filter=criteria):  # self.DbCollection.find(filter=criteria):
            return data

    def getFirstRecView(self, criteria: dict, view: dict()) -> dict():  # type: ignore #
        """  getFirstRecView   """
        for data in self.getDataView(filter=criteria,
                                     view=view):  # self.DbCollection.find(filter=criteria, projection=view):
            return data

    def getPipeDatawithBatch(self, pipe: list, Batch: int, AllowDiskUse: bool):  # type: ignore
        """  getPipeDatawithBatch   """
        return self.DbCollection.with_options(
            codec_options=self.codec_option
        ).aggregate(
            pipeline=pipe,
            allowDiskUse=AllowDiskUse,
            batchSize=Batch
        )

    def getPipeData(self, pipe: list):  # type: ignore
        """  getPipeData   """
        return self.DbCollection.with_options(
            codec_options=self.codec_option
        ).aggregate(
            pipeline=pipe,
            allowDiskUse=True
        )

    def find_one(self, query):
        return self.DbCollection.find_one(query)

    def UpdateData(self, criterria, update_query):
        """Update data with Mongo-style update operators like $set, $inc, etc."""
        if update_query:
            try:
                with self.Client.start_session() as session:
                    with session.start_transaction():
                        update_result = self.DbCollection.update_one(
                            filter=criterria,
                            update=update_query  # << Do NOT wrap in "$set"
                        )
                        # logger.info('Result.UpdateData : {0}'.format(update_result.raw_result))
            except Exception as exp:
                logger.issue("Error.UpdateData : {0}".format(exp))
                logger.issue(criterria)
                logger.issue(update_query)
                raise
        else:
            logger.warning('Warn.UpdateData : No data to Update')

    # def UpdateData(self, criterria, setdata):
    #     """  UpdateData   """
    #     if len(setdata) != 0:
    #         try:

    #             with self.Client.start_session() as session:
    #                 with session.start_transaction():
    #                     update_result = self.DbCollection.update_one(
    #                         filter=criterria,
    #                         update={"$set": setdata}
    #                     )
    #                     logger.info('Result.UpdateData : {0}'.format(update_result.raw_result))
    #         except Exception as exp:
    #             logger.error("Error.UpdateData : {0}".format(exp))
    #             logger.error(criterria)
    #             logger.error(setdata)
    #             raise
    #     else:
    #         logger.warning('Warn.UpdateData : No data to Update')

    def UpdateMany(self, criterria, setdata):
        """  UpdateData   """
        if len(setdata) != 0:
            try:
                with self.Client.start_session() as session:
                    with session.start_transaction():
                        update_result = self.DbCollection.update_many(
                            filter=criterria,
                            update={"$set": setdata}
                        )
                        logger.info('Result.UpdateData : {0}'.format(update_result.raw_result))
            except Exception as exp:
                logger.issue("Error.UpdateMany : {0}".format(exp))
                logger.issue(criterria)
                logger.issue(setdata)
                raise
        else:
            logger.warning('Warn.UpdateMany : No data to Update')

    def BulkUpdate(self, bulk_requests):
        """  BulkUpdate   """
        if len(bulk_requests) != 0:
            try:
                # logger.info(bulk_requests)
                with self.Client.start_session() as session:
                    with session.start_transaction():
                        batch_result = self.DbCollection.bulk_write(bulk_requests)
                        logger.info('Result.BulkUpdate : {0}'.format(batch_result.bulk_api_result))
            except BulkWriteError as bwe:
                logger.issue("Error.BulkUpdate : {0}".format(bwe.details))
                logger.issue(bulk_requests)
                raise
        else:
            logger.warning('Warn.BulkUpdate : No data to update')

    def InsertData(self, InsData):
        """  InsertData   """
        if len(InsData) != 0:
            try:
                # logger.info(InsData)
                with self.Client.start_session() as session:
                    with session.start_transaction():
                        insert_result = self.DbCollection.insert_one(InsData)
                        logger.info('Result.InsertData : {0}'.format(insert_result.inserted_id))
            except Exception as exp:
                logger.issue(f"Error.InsertData : {InsData}  \n {exp}")
                raise
        else:
            logger.warning('Warn.InsertData : No data to Insert')

    def BulkInsert(self, InsData):
        """  BulkInsert   """
        if len(InsData) != 0:
            try:

                with self.Client.start_session() as session:
                    with session.start_transaction():
                        insert_result = self.DbCollection.insert_many(InsData)
                        logger.info('Result.BulkInsert  : {0}'.format(
                            len(insert_result.inserted_ids)))
            except Exception as exp:
                logger.issue("Error.InsertData : {0}".format(exp))
                logger.info(InsData)
                raise
        else:
            logger.warning('Warn.BulkInsert : No data to Insert')


# Operations used in Bulk updates
class Operation:

    def __init__(self, criterria, setdata, upsert: bool = False) -> None:
        self.filter = criterria
        self.update = {"$set": setdata}
        self.upsert = upsert

    def UpdateMany(self):
        return UpdateMany(
            filter=self.filter,
            update=self.update,
            upsert=self.upsert
        )

    def UpdateOne(self):
        return UpdateOne(
            filter=self.filter,
            update=self.update,
            upsert=self.upsert
        )