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
        config_obj = configparser.ConfigParser()
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
        return self.get_conf_data(environemnt, 'mongo_db_replicaset')

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

    def time_zone(self,Timezone):
        return self.get_conf_data(Timezone, 'TIMEZONE')

    def log_dir(self, dir):
        return self.get_conf_data(dir, 'dir')

    def report_dir(self, dir):
        return self.get_conf_data(dir, 'dir')

    def mismatch_count(self, missmatch):
        return self.get_conf_data(missmatch, 'mismatch')

    def missing_count(self, missing):
        return self.get_conf_data(missing, 'missing')
        # Run data Validation Scripts

Timezone = pytz.timezone(config_read().time_zone('Time_Zone'))