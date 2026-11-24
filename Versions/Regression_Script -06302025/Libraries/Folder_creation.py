import logging
import os
import shutil
from dotenv import load_dotenv


# from .ClockDeco import clock
# from .LogConfig import Logger_

# logger = Logger_(__name__).logger


# User Defined Exceptions
class MoreFilesFound(Exception):
    """Raised when number of files are more than excepted"""

    def __init__(self, DirFileCount, ExceptFileCount, message=' files found, Excepted : '):
        self.DirFileCount = DirFileCount
        self.ExceptFileCount = ExceptFileCount
        self.message = message
        super().__init__(self.message)

    def __str__(self):
        return f'{self.DirFileCount} ->{self.message} {self.ExceptFileCount}'


class InvalidFileType(Exception):
    """Raised when number of files are more than excepted"""

    def __init__(self, message=' ERROR! Invalid File Type '):
        self.message = message
        super().__init__(self.message)


# User Defined Class
class DirHandler:
    """
        Directory/ Fldder  Handler
    """

    def __init__(self, packed_file_type, exceptedFiles):
        self.DataDir = os.getenv("data_dir")
        self.ArchiveDir = os.getenv("arch_dir")
        self.COMPRESS_TYPE = packed_file_type
        self.ZIP_COUNT: int = exceptedFiles

    BASE_DIR = os.path.dirname(__file__)

    def mkdir(path):
        try:
            # print(f'Dir Path : {path}')
            os.makedirs(path, exist_ok=True)  # Python>3.2
        except TypeError:
            print(TypeError)


# User Defined Class
class FileHandler:
    """
        File Handler
    """

    def __init__(self, packed_file_type, exceptedFiles):
        self.DataDir = os.getenv("data_dir")
        self.ArchiveDir = os.getenv("arch_dir")
        self.COMPRESS_TYPE = packed_file_type
        self.ZIP_COUNT: int = exceptedFiles

    @staticmethod
    def _get_file_type(filename: str) -> object:
        if filename.find('Full') != -1:
            return 'M'
        elif filename.find('Delta') != -1:
            return 'D'
        elif filename.find('Initial') != -1:
            return 'I'
        else:
            e = InvalidFileType
            print(hasattr(e, 'value') and e.value[1] or e)
            raise e

    def _unpack_file(self, file_name):
        shutil.unpack_archive(
            filename=self.DataDir + '\\' + file_name,
            extract_dir=self.DataDir,
            format=self.COMPRESS_TYPE
        )

    def _archive_file(self, file_name):
        shutil.move(
            src=file_name,
            dst=self.ArchiveDir
        )

    def _delete_files(self):
        for file in os.listdir(self.DataDir):
            os.remove(os.path.join(self.DataDir, file))

    def archive_files(self, FileName):
        """
            Arching files
        """
        for file in os.listdir(self.DataDir):
            try:
                if file == FileName:
                    print(f'Moving {file} to archive')
                    self._archive_file(os.path.join(self.DataDir, file))  # type: ignore
            except Exception as ERROR:
                print(hasattr(ERROR, 'value') and ERROR.value[1] or ERROR)  # type: ignore
                raise ERROR

    def get_file_name(self) -> object:
        """
        Get Zip File name to unpack the data
        :return:
        """
        _zip_file_count: int = 0
        file_path: str = ''
        zip_file: str = ''

        # For each file in the In data dir
        for file in os.listdir(self.DataDir):
            if file.endswith(f'.{self.COMPRESS_TYPE}'):
                _zip_file_count += 1
                file_path = os.path.join(self.DataDir, file)
                zip_file = file

        if _zip_file_count != self.ZIP_COUNT:
            er = MoreFilesFound(_zip_file_count, self.ZIP_COUNT)
            print(hasattr(er, 'value') and er.value[1] or er)
            raise er
        return zip_file, self._get_file_type(zip_file)

    def get_data_files(self):
        """
            Function for initial Stage
        """
        try:
            # start_time = datetime.datetime.now()
            load_dotenv()

            COMPRESS_FILE, FILE_TYPE = self.get_file_name()  # type: ignore
            self._unpack_file(COMPRESS_FILE)
            self.archive_files(COMPRESS_FILE)

            # elapsed_time = datetime.datetime.now() - start_time
            # print(f' Total time taken to unpack the files : {elapsed_time}')
            return FILE_TYPE

        except Exception as ERROR:
            print(hasattr(ERROR, 'value') and ERROR.value[1] or ERROR)  # type: ignore
            raise ERROR
