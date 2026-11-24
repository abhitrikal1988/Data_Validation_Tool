import sys
import os
import configparser
import pytz


class config_read():

    def __init__(self):
        if getattr(sys, 'frozen', False):  # If running as .exe
            self.path = os.path.dirname(sys.executable)  # Get the directory of the .exe
        else:  # If running as a script (e.g., in PyCharm)
            self.path = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
            # print(self.path)

    def get_conf_data(self, section_name, variable_name):
        config_obj = configparser.ConfigParser(interpolation=None)
        config_dir = os.path.join(self.path, "Config")
        # fule file path to the '.env' file dir with the 'config' dir
        config_file = os.path.normpath(os.path.join(config_dir, "environment.ini"))
        # confg_file = self.path + "\\Config\\environment.ini"
        config_obj.read(config_file)
        section_variable = config_obj[section_name]
        variable_data = section_variable[variable_name]
        return variable_data

    def test_data_environment(self, regression_data):
        return self.get_conf_data(regression_data, 'test_data_environment')

    def mongo_db_user_name(self, environment):
        return self.get_conf_data(environment, 'mongo_db_username')

    def mongo_db_password(self, environemnt):
        return self.get_conf_data(environemnt, 'mongo_db_password')

    def mongo_db_ipaddress(self, environemnt):
        return self.get_conf_data(environemnt, 'mongo_db_ipaddress')

    def mongo_db_replicaset(self, environemnt):
        # Optional: an environment without a mongo_db_replicaset line connects
        # without a replica set, the same as an empty value.
        try:
            return self.get_conf_data(environemnt, 'mongo_db_replicaset')
        except KeyError:
            return ""

    def mongo_db_auth(self, environemnt):
        return self.get_conf_data(environemnt, 'mongo_auth')

    def regression_collection_name(self, rg_data):
        return self.get_conf_data(rg_data, 'regression_collection_name')

    def log_collection_name(self, rg_data):
        return self.get_conf_data(rg_data, 'log_collection_name')

    def db_name(self, rg_data):
        return self.get_conf_data(rg_data, 'db_name')

    def oracle_db_connection(self, environemnt):
        return self.get_conf_data(environemnt, 'oracle_connection')

    def time_zone(self, Timezone):
        return self.get_conf_data(Timezone, 'TIMEZONE')

    def time_zone_conversion(self) -> bool:
        """
        [Time_Zone] Conversion = Yes | No (default Yes when the line is absent).
        Yes — MongoDB dates (stored in UTC) are read in TIMEZONE, for Oracle
              tables that hold local time.
        No  — MongoDB dates are read in UTC as stored, for Oracle tables that
              hold UTC.
        """
        try:
            value = self.get_conf_data('Time_Zone', 'Conversion')
        except KeyError:
            return True
        return str(value).strip().lower() not in ("no", "n", "false", "0", "off")

    def log_dir(self, dir):
        return self.get_conf_data(dir, 'dir')

    def report_dir(self, dir):
        return self.get_conf_data(dir, 'dir')

    def mismatch_count(self, missmatch):
        return self.get_conf_data(missmatch, 'mismatch')

    def missing_count(self, missing):
        return self.get_conf_data(missing, 'missing')

    def edi835_api_url(self, environment: str) -> str:
        return self.get_conf_data("EDI835_API", environment)

    def max_threads(self, max_threads):
        return self.get_conf_data(max_threads, 'max_threads')

    def visible_row_count(self, section):
        return self.get_conf_data(section, 'count')

    def view_row_count(self):
        """
        Rows to display for the DataView validation type.
        Precedence: [DataView] rows  ->  [Visible_Row_count] count  ->  10.
        Falls back rather than raising when a section is absent — this is
        called for every run, not just DataView test cases, so a missing
        [DataView] section must not abort the whole validation.
        """
        try:
            return int(self.get_conf_data("DataView", "rows"))
        except Exception:
            pass
        try:
            return int(self.get_conf_data("Visible_Row_count", "count"))
        except Exception:
            return 10

    # ── AI Agent (Test_Case_Generator) ────────────────────────────────────
    def ollama_api(self) -> str:
        return self.get_conf_data("AI_AGENT", "OLLAMA_API")

    def ollama_model(self) -> str:
        return self.get_conf_data("AI_AGENT", "OLLAMA_MODEL")

    def testcase_dir(self) -> str:
        return self.get_conf_data("AI_AGENT", "TestCase_Dir")

    # ── Aspose.PDF licence ────────────────────────────────────────────────
    def aspose_license_path(self) -> str:
        return self.get_conf_data("ASPOSE", "license_path")


Timezone = pytz.timezone(config_read().time_zone('Time_Zone'))

# Timezone MongoDB dates are read in (see time_zone_conversion): TIMEZONE when
# Conversion = Yes (the default), UTC when Conversion = No.
TimezoneConversion = config_read().time_zone_conversion()
MongoTimezone = Timezone if TimezoneConversion else pytz.utc