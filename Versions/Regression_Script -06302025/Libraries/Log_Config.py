import inspect
import sys
import logging
import os
from datetime import datetime
from logging import FileHandler
from logging.handlers import SMTPHandler
from colorlog import ColoredFormatter
from pythonjsonlogger import jsonlogger
from .Read_config import config_read, Timezone
from .Folder_creation import DirHandler

cr = config_read()
log_dirc = cr.log_dir("Log_Dir")

# Timestamp
now = datetime.now()
date_time = now.strftime('%Y%m%d_%H%M%S')
date = now.strftime('%Y%m%d')


# Helper to make directories
def make_dir(dir_path):
    DirHandler.mkdir(dir_path)
    return dir_path


# Custom Email Handler
class EmailHandler(SMTPHandler):
    def __init__(self, mailhost, fromaddr, toaddrs, subject, credentials=None, secure=None, timeout=5.0):
        super().__init__(mailhost, fromaddr, toaddrs, subject, credentials, secure, timeout)


# Custom File Handler with timestamped filenames
class TimestampedFileHandler(FileHandler):
    def __init__(self, filename, mode='a', encoding='utf-8', delay=False):
        base, ext = os.path.splitext(filename)
        filename = f"{base}_{date_time}{ext}"
        super().__init__(filename, mode, encoding, delay)

class JSONFileHandler(FileHandler):
    def __init__(self, filename, mode='a', encoding='utf-8', delay=False):
        base, ext = os.path.splitext(filename)
        filename = f"{base}_{date_time}{ext}"
        super().__init__(filename)
        self.setFormatter(jsonlogger.JsonFormatter())

# Formatters
class Formatter():
    def __init__(self):
        self.simple = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
        self.detail = logging.Formatter(
            "%(asctime)s | %(levelname)s | %(message)s"
        )
        self.json = None  # reserved for future use


# Console Handler with color
class ConsoleHandler():
    def __init__(self):
        self.handler = logging.StreamHandler(sys.stdout)
        self.handler.setLevel(logging.DEBUG)
        formatter = ColoredFormatter(
            "%(log_color)s%(asctime)s | %(levelname)s | %(message)s",
            log_colors={
                'DEBUG': 'cyan',
                'INFO': 'green',
                'WARNING': 'yellow',
                'ERROR': 'red',
                'CRITICAL': 'bold_red'
            }
        )
        self.handler.setFormatter(formatter)

#add logs to jsonlog
class JSONLogHandler():
    def __init__(self, json_log_file):
        self.log_dir = make_dir(log_dirc)
        self.log_date_dir = make_dir(os.path.join(log_dirc, date))
        self.handler = JSONFileHandler(filename=os.path.join(self.log_date_dir, json_log_file))
        self.handler.setLevel(logging.INFO)

# Debug log handler (writes all logs)
class DebugHandler():
    def __init__(self, log_file):
        log_dir = make_dir(log_dirc)
        dated_dir = make_dir(os.path.join(log_dir, "Process_Log"))
        self.handler = TimestampedFileHandler(filename=os.path.join(dated_dir, log_file))
        self.handler.setLevel(logging.DEBUG)
        self.handler.setFormatter(Formatter().detail)


# Error log handler (only error and above)
class ErrorHandler():
    def __init__(self, error_file):
        log_dir = make_dir(log_dirc)
        dated_dir = make_dir(os.path.join(log_dir, date))
        self.handler = TimestampedFileHandler(filename=os.path.join(dated_dir, error_file))
        self.handler.setLevel(logging.ERROR)
        self.handler.setFormatter(Formatter().detail)


# Unified Logger class
class Logger_():
    def __init__(self, name, package=None):
        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.DEBUG)

        # Avoid adding handlers more than once
        if self.logger.handlers:  # If handlers already added, skip
            return

        script_path = os.path.basename(sys.path[0])
        if package:
            script_path = package

        self._log_file = f"{script_path}_log.log"
        self._error_file = f"{script_path}_error.log"
        self._json_file = f"{script_path}_json.log"

        self.logger.addHandler(ConsoleHandler().handler)
        self.logger.addHandler(DebugHandler(self._log_file).handler)
        # self.logger.addHandler(ErrorHandler(self._error_file).handler)
        # self.logger.addHandler(JSONLogHandler(self._json_file).handler)

        self.logger.propagate = False  # avoid duplicate logs

    def get_logger(self):
        return self.logger
