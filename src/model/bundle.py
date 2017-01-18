import os
import hashlib

from tornado.gen import coroutine, Return
from common.model import Model
from common.database import DatabaseError, format_conditions_json
from common.options import options

import ujson
from common import random_string


class BundleError(Exception):
    def __init__(self, message):
        self.message = message

    def __str__(self):
        return self.message


class BundleQueryError(Exception):
    def __init__(self, message):
        self.message = message

    def __str__(self):
        return self.message


class BundleAdapter(object):
    def __init__(self, data):
        self.bundle_id = data["bundle_id"]
        self.version = data["version_id"]
        self.name = data["bundle_name"]
        self.hash = data["bundle_hash"]
        self.url = data["bundle_url"]
        self.status = data["bundle_status"]
        self.size = data["bundle_size"]
        self.filters = data.get("bundle_filters", {})
        self.payload = data.get("bundle_payload", {})
        self.key = data.get("bundle_key", "")

    def get_key(self):
        return str(self.bundle_id) + "_" + self.key


class NoSuchBundleError(Exception):
    pass


class BundleQuery(object):
    def __init__(self, gamespace_id, data_id, db):
        self.gamespace_id = gamespace_id
        self.data_id = data_id
        self.db = db

        self.status = None
        self.filters = None

        self.offset = 0
        self.limit = 0

    def __values__(self):
        conditions = [
            "`bundles`.`gamespace_id`=%s",
            "`bundles`.`version_id`=%s"
        ]

        data = [
            str(self.gamespace_id),
            str(self.data_id)
        ]

        if self.status:
            conditions.append("`bundles`.`bundle_status`=%s")
            data.append(str(self.status))

        if self.filters:
            for condition, values in format_conditions_json('bundle_filters', self.filters):
                conditions.append(condition)
                data.extend(values)

        return conditions, data

    @coroutine
    def query(self, one=False, count=False):
        conditions, data = self.__values__()

        query = """
            SELECT {0} * FROM `bundles`
            WHERE {1}
        """.format(
            "SQL_CALC_FOUND_ROWS" if count else "",
            " AND ".join(conditions))

        query += """
            ORDER BY `bundle_id` DESC
        """

        if self.limit:
            query += """
                LIMIT %s,%s
            """
            data.append(int(self.offset))
            data.append(int(self.limit))

        query += ";"

        if one:
            try:
                result = yield self.db.get(query, *data)
            except DatabaseError as e:
                raise BundleQueryError("Failed to get message: " + e.args[1])

            if not result:
                raise Return(None)

            raise Return(BundleAdapter(result))
        else:
            try:
                result = yield self.db.query(query, *data)
            except DatabaseError as e:
                raise BundleQueryError("Failed to query messages: " + e.args[1])

            count_result = 0

            if count:
                count_result = yield self.db.get(
                    """
                        SELECT FOUND_ROWS() AS count;
                    """)
                count_result = count_result["count"]

            items = map(BundleAdapter, result)

            if count:
                raise Return((items, count_result))

            raise Return(items)


class BundlesModel(Model):

    STATUS_CREATED = "CREATED"
    STATUS_UPLOADED = "UPLOADED"
    STATUS_DELIVERING = "DELIVERING"
    STATUS_DELIVERED = "DELIVERED"
    STATUS_ERROR = "ERROR"

    HASH_METHOD = hashlib.sha256

    def __init__(self, db):
        self.db = db
        self.data_location = options.data_location

    def get_setup_db(self):
        return self.db

    def get_setup_tables(self):
        return ["bundles"]

    @coroutine
    def delete_bundle(self, gamespace_id, app_id, bundle_id):

        bundle = yield self.get_bundle(gamespace_id, bundle_id)
        data_id = bundle.version

        bundle_file = os.path.join(self.data_location, str(app_id), str(data_id), str(bundle_id))

        try:
            os.remove(bundle_file)
        except OSError:
            pass

        try:
            yield self.db.execute(
                """
                DELETE FROM `bundles`
                WHERE `bundle_id`=%s AND `gamespace_id`=%s;
                """, bundle_id, gamespace_id)
        except DatabaseError as e:
            raise BundleError("Failed to delete bundle: " + e.args[1])

    @coroutine
    def find_bundle(self, gamespace_id, data_id, bundle_name):
        try:
            bundle = yield self.db.get(
                """
                SELECT *
                FROM `bundles`
                WHERE `version_id`=%s AND `bundle_name`=%s AND `gamespace_id`=%s;
                """, data_id, bundle_name, gamespace_id)
        except DatabaseError as e:
            raise BundleError("Failed to find bundle: " + e.args[1])

        if not bundle:
            raise NoSuchBundleError()

        raise Return(BundleAdapter(bundle))

    @coroutine
    def get_bundle(self, gamespace_id, bundle_id):
        try:
            bundle = yield self.db.get(
                """
                SELECT *
                FROM `bundles`
                WHERE `bundle_id`=%s AND `gamespace_id`=%s;
                """, bundle_id, gamespace_id)
        except DatabaseError as e:
            raise BundleError("Failed to get bundle: " + e.args[1])

        if not bundle:
            raise NoSuchBundleError()

        raise Return(BundleAdapter(bundle))

    def bundles_query(self, gamespace_id, data_id):
        return BundleQuery(gamespace_id, data_id, self.db)

    @coroutine
    def list_bundles(self, gamespace_id, data_id):
        try:
            bundles = yield self.db.query(
                """
                SELECT *
                FROM `bundles`
                WHERE `version_id`=%s AND `gamespace_id`=%s
                ORDER BY `bundle_id` DESC;
                """, data_id, gamespace_id)
        except DatabaseError as e:
            raise BundleError("Failed to list bundles: " + e.args[1])

        raise Return(map(BundleAdapter, bundles))

    @coroutine
    def create_bundle(self, gamespace_id, data_id, bundle_name, bundle_filters, bundle_payload, bundle_key):

        if not isinstance(bundle_filters, dict):
            raise BundleError("bundle_filters should be a dict")

        if not isinstance(bundle_payload, dict):
            raise BundleError("bundle_payload should be a dict")

        try:
            yield self.find_bundle(gamespace_id, data_id, bundle_name)
        except NoSuchBundleError:
            pass
        else:
            raise BundleError("Bundle with such name already exists")

        try:
            bundle_id = yield self.db.insert(
                """
                INSERT INTO `bundles`
                (`version_id`, `gamespace_id`, `bundle_name`, `bundle_status`,
                    `bundle_filters`, `bundle_payload`, `bundle_key`)
                VALUES (%s, %s, %s, %s, %s, %s, %s);
                """, data_id, gamespace_id, bundle_name, BundlesModel.STATUS_CREATED,
                ujson.dumps(bundle_filters), ujson.dumps(bundle_payload), bundle_key)
        except DatabaseError as e:
            raise BundleError("Failed to create bundle: " + e.args[1])

        raise Return(bundle_id)

    @coroutine
    def update_bundle_properties(self, gamespace_id, bundle_id, bundle_filters, bundle_payload):

        if not isinstance(bundle_filters, dict):
            raise BundleError("bundle_filters should be a dict")

        try:
            yield self.db.execute(
                """
                UPDATE `bundles`
                SET `bundle_filters`=%s, `bundle_payload`=%s
                WHERE `bundle_id`=%s AND `gamespace_id`=%s;
                """, ujson.dumps(bundle_filters), ujson.dumps(bundle_payload), bundle_id, gamespace_id)
        except DatabaseError as e:
            raise BundleError("Failed to update bundle: " + e.args[1])

    @coroutine
    def update_bundle(self, gamespace_id, bundle_id, bundle_hash, bundle_status, bundle_size):

        try:
            yield self.db.execute(
                """
                UPDATE `bundles`
                SET `bundle_hash`=%s, `bundle_status`=%s, `bundle_size`=%s
                WHERE `bundle_id`=%s AND `gamespace_id`=%s;
                """, bundle_hash, bundle_status, bundle_size, bundle_id, gamespace_id)
        except DatabaseError as e:
            raise BundleError("Failed to update bundle: " + e.args[1])

    @coroutine
    def update_bundle_status(self, gamespace_id, bundle_id, bundle_status):

        try:
            yield self.db.execute(
                """
                UPDATE `bundles`
                SET `bundle_status`=%s
                WHERE `bundle_id`=%s AND `gamespace_id`=%s;
                """, bundle_status, bundle_id, gamespace_id)
        except DatabaseError as e:
            raise BundleError("Failed to update bundle status: " + e.args[1])

    @coroutine
    def update_bundle_url(self, gamespace_id, bundle_id, bundle_status, bundle_url):

        try:
            yield self.db.execute(
                """
                UPDATE `bundles`
                SET `bundle_status`=%s, `bundle_url`=%s
                WHERE `bundle_id`=%s AND `gamespace_id`=%s;
                """, bundle_status, bundle_url, bundle_id, gamespace_id)
        except DatabaseError as e:
            raise BundleError("Failed to update bundle status: " + e.args[1])

    def bundle_path(self, app_id, data_id, bundle):
        return os.path.join(self.data_location, str(app_id), str(data_id), bundle.get_key())

    def bundle_directory(self, app_id, data_id):
        return os.path.join(self.data_location, str(app_id), str(data_id))

    @coroutine
    def upload_bundle(self, gamespace_id, app_id, bundle, producer):

        bundle_id = bundle.bundle_id
        data_id = bundle.version

        if not os.path.exists(self.bundle_directory(app_id, data_id)):
            os.makedirs(self.bundle_directory(app_id, data_id))

        bundle_file = self.bundle_path(app_id, data_id, bundle)

        _h = BundlesModel.HASH_METHOD()
        output_file = open(bundle_file, 'wb')

        class Size:
            bundle_size = 0

        @coroutine
        def write(data):
            output_file.write(data)
            _h.update(data)
            Size.bundle_size += len(data)

        yield producer(write)

        output_file.close()

        bundle_hash = _h.hexdigest()

        yield self.update_bundle(
            gamespace_id, bundle_id, bundle_hash, BundlesModel.STATUS_UPLOADED, Size.bundle_size)