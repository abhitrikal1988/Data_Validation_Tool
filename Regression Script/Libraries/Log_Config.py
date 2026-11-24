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

# ---------------------------------------------------
# Custom ISSUE log level
# ---------------------------------------------------
ISSUE_LEVEL_NUM = 35
logging.addLevelName(ISSUE_LEVEL_NUM, "ISSUE")


def issue(self, message, *args, **kws):
    if self.isEnabledFor(ISSUE_LEVEL_NUM):
        self._log(ISSUE_LEVEL_NUM, message, args, **kws)


logging.Logger.issue = issue


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
        # extra={"file_hide": True}: a terminal-only summary line
        self.addFilter(lambda record: not getattr(record, "file_hide", False))


class JSONFileHandler(FileHandler):
    def __init__(self, filename, mode='a', encoding='utf-8', delay=False):
        base, ext = os.path.splitext(filename)
        filename = f"{base}_{date_time}{ext}"
        super().__init__(filename, mode, encoding, delay)
        self.setFormatter(jsonlogger.JsonFormatter())


# Formatters
class Formatter:
    def __init__(self):
        self.simple = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
        self.detail = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
        self.json = None  # reserved for future use


# Console formatter: a log call can pass extra={"console_text": "..."} to show
# a short line in the terminal, or extra={"console_hide": True} to keep the
# line out of the terminal; the log files still get the full message.
class ConsoleFormatter(ColoredFormatter):
    def format(self, record):
        short = getattr(record, "console_text", None)
        if short:
            record = logging.makeLogRecord(record.__dict__)
            record.msg, record.args = short, ()
        return super().format(record)


# Console Handler with color
class ConsoleHandler:
    def __init__(self):
        self.handler = logging.StreamHandler(sys.stdout)
        self.handler.setLevel(logging.DEBUG)
        formatter = ConsoleFormatter(
            "%(log_color)s%(asctime)s | %(levelname)s | %(message)s",
            log_colors={
                'DEBUG': 'cyan',
                'INFO': 'green',
                'WARNING': 'yellow',
                'ISSUE': 'red',
                'ERROR': 'red',
                'CRITICAL': 'bold_red'
            }
        )
        self.handler.setFormatter(formatter)
        self.handler.addFilter(lambda record: not getattr(record, "console_hide", False))


# Add logs to json log
class JSONLogHandler:
    def __init__(self, json_log_file):
        self.log_dir = make_dir(log_dirc)
        self.log_date_dir = make_dir(os.path.join(log_dirc, date))
        self.handler = JSONFileHandler(filename=os.path.join(self.log_date_dir, json_log_file))
        self.handler.setLevel(logging.INFO)

def _module_folder(module: str) -> str | None:
    """
    Returns a safe folder name for a single module.
    Returns None if module is absent or multiple modules are passed
    (comma-separated) — file goes directly in Process_Log / DB_Log.
    """
    if not module:
        return None
    parts = [m.strip() for m in module.split(",") if m.strip()]
    if len(parts) != 1:
        return None                          # multiple modules — no subfolder
    return parts[0].replace(" ", "_")


# Debug log handler (writes all logs)
class DebugHandler:
    def __init__(self, log_file, module = None):
        process_dir = make_dir(os.path.join(log_dirc, "Process_Log"))
        folder      = _module_folder(module)
        if folder:
            target_dir = os.path.join(process_dir, folder)
            os.makedirs(target_dir, exist_ok=True)
        else:
            target_dir = process_dir
        self.handler = TimestampedFileHandler(filename=os.path.join(target_dir, log_file))
        self.handler.setLevel(logging.DEBUG)
        self.handler.setFormatter(Formatter().detail)


# Issue log handler (only ISSUE and above)
class IssueHandler:
    def __init__(self, issue_file):
        log_dir = make_dir(log_dirc)
        dated_dir = make_dir(os.path.join(log_dir, "Process_Log"))
        self.handler = TimestampedFileHandler(filename=os.path.join(dated_dir, issue_file))
        self.handler.setLevel(ISSUE_LEVEL_NUM)
        self.handler.setFormatter(Formatter().detail)


# Error log handler (only error and above)
class ErrorHandler:
    def __init__(self, error_file):
        log_dir = make_dir(log_dirc)
        dated_dir = make_dir(os.path.join(log_dir,  "Process_Log"))
        self.handler = TimestampedFileHandler(filename=os.path.join(dated_dir, error_file))
        self.handler.setLevel(logging.ERROR)
        self.handler.setFormatter(Formatter().detail)


# Unified Logger class
class Logger_:
    def __init__(self, name, package=None, module=None):
        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.DEBUG)

        # Avoid adding handlers more than once
        if self.logger.handlers:
            return

        script_path = os.path.basename(sys.path[0])
        if package:
            script_path = package

        self._log_file = f"{script_path}_log.log"
        self._issue_file = f"{script_path}_issue.log"
        self._error_file = f"{script_path}_error.log"
        self._json_file = f"{script_path}_json.log"

        self.logger.addHandler(ConsoleHandler().handler)
        self.logger.addHandler(DebugHandler(self._log_file, module=module).handler)

        # Optional separate files
        # self.logger.addHandler(IssueHandler(self._issue_file).handler)
        # self.logger.addHandler(ErrorHandler(self._error_file).handler)
        # self.logger.addHandler(JSONLogHandler(self._json_file).handler)

        self.logger.propagate = False  # avoid duplicate logs

    def get_logger(self):
        return self.logger


# import inspect
# import sys
# import logging
# import os
# from datetime import datetime
# from logging import FileHandler
# from logging.handlers import SMTPHandler
# from colorlog import ColoredFormatter
# from pythonjsonlogger import jsonlogger
# from .Read_config import config_read, Timezone
# from .Folder_creation import DirHandler
#
# cr = config_read()
# log_dirc = cr.log_dir("Log_Dir")
#
# # Timestamp
# now = datetime.now()
# date_time = now.strftime('%Y%m%d_%H%M%S')
# date = now.strftime('%Y%m%d')
#
#
# # Helper to make directories
# def make_dir(dir_path):
#     DirHandler.mkdir(dir_path)
#     return dir_path
#
#
# # Custom Email Handler
# class EmailHandler(SMTPHandler):
#     def __init__(self, mailhost, fromaddr, toaddrs, subject, credentials=None, secure=None, timeout=5.0):
#         super().__init__(mailhost, fromaddr, toaddrs, subject, credentials, secure, timeout)
#
#
# # Custom File Handler with timestamped filenames
# class TimestampedFileHandler(FileHandler):
#     def __init__(self, filename, mode='a', encoding='utf-8', delay=False):
#         base, ext = os.path.splitext(filename)
#         filename = f"{base}_{date_time}{ext}"
#         super().__init__(filename, mode, encoding, delay)
#
# class JSONFileHandler(FileHandler):
#     def __init__(self, filename, mode='a', encoding='utf-8', delay=False):
#         base, ext = os.path.splitext(filename)
#         filename = f"{base}_{date_time}{ext}"
#         super().__init__(filename)
#         self.setFormatter(jsonlogger.JsonFormatter())
#
# # Formatters
# class Formatter():
#     def __init__(self):
#         self.simple = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
#         self.detail = logging.Formatter(
#             "%(asctime)s | %(levelname)s | %(message)s"
#         )
#         self.json = None  # reserved for future use
#
#
# # Console Handler with color
# class ConsoleHandler():
#     def __init__(self):
#         self.handler = logging.StreamHandler(sys.stdout)
#         self.handler.setLevel(logging.DEBUG)
#         formatter = ColoredFormatter(
#             "%(log_color)s%(asctime)s | %(levelname)s | %(message)s",
#             log_colors={
#                 'DEBUG': 'cyan',
#                 'INFO': 'green',
#                 'WARNING': 'yellow',
#                 'ERROR': 'red',
#                 'CRITICAL': 'bold_red'
#             }
#         )
#         self.handler.setFormatter(formatter)
#
# #add logs to jsonlog
# class JSONLogHandler():
#     def __init__(self, json_log_file):
#         self.log_dir = make_dir(log_dirc)
#         self.log_date_dir = make_dir(os.path.join(log_dirc, date))
#         self.handler = JSONFileHandler(filename=os.path.join(self.log_date_dir, json_log_file))
#         self.handler.setLevel(logging.INFO)
#
# # Debug log handler (writes all logs)
# class DebugHandler():
#     def __init__(self, log_file):
#         log_dir = make_dir(log_dirc)
#         dated_dir = make_dir(os.path.join(log_dir, "Process_Log"))
#         self.handler = TimestampedFileHandler(filename=os.path.join(dated_dir, log_file))
#         self.handler.setLevel(logging.DEBUG)
#         self.handler.setFormatter(Formatter().detail)
#
#
# # Error log handler (only error and above)
# class ErrorHandler():
#     def __init__(self, error_file):
#         log_dir = make_dir(log_dirc)
#         dated_dir = make_dir(os.path.join(log_dir, date))
#         self.handler = TimestampedFileHandler(filename=os.path.join(dated_dir, error_file))
#         self.handler.setLevel(logging.ERROR)
#         self.handler.setFormatter(Formatter().detail)
#
#
# # Unified Logger class
# class Logger_():
#     def __init__(self, name, package=None):
#         self.logger = logging.getLogger(name)
#         self.logger.setLevel(logging.DEBUG)
#
#         # Avoid adding handlers more than once
#         if self.logger.handlers:  # If handlers already added, skip
#             return
#
#         script_path = os.path.basename(sys.path[0])
#         if package:
#             script_path = package
#
#         self._log_file = f"{script_path}_log.log"
#         self._error_file = f"{script_path}_error.log"
#         self._json_file = f"{script_path}_json.log"
#
#         self.logger.addHandler(ConsoleHandler().handler)
#         self.logger.addHandler(DebugHandler(self._log_file).handler)
#         # self.logger.addHandler(ErrorHandler(self._error_file).handler)
#         # self.logger.addHandler(JSONLogHandler(self._json_file).handler)
#
#         self.logger.propagate = False  # avoid duplicate logs
#
#     def get_logger(self):
#         return self.logger