import os
import io
import re
import time
import uuid
import ftplib
import logging
import tempfile
import fnmatch
from pathlib import Path
from datetime import datetime

logger = logging.getLogger()


# ─────────────────────────────────────────────────────────────────────────────
# Supported retrieval types
# ─────────────────────────────────────────────────────────────────────────────
FILE_TYPES = {
    "CSV", "PIPE", "TSV", "FIXED_WIDTH", "EXCEL",
    "JSON", "XML", "HL7", "NCPDP_D0", "NCPDP_SCRIPT",
    "LOCAL_PATH", "UNC_PATH", "SFTP", "FTP",
    "S3", "AZURE_BLOB", "ORACLE_CLOB", "MONGODB_GRIDFS",
    "API_CALL", "AUTO"
}


# ─────────────────────────────────────────────────────────────────────────────
# Config helper
# ─────────────────────────────────────────────────────────────────────────────

def _get_config(credential_key: str) -> dict:
    """
    Reads all config values for a credential key from environment.ini.
    Returns dict of all key-value pairs under that section.
    """
    from .Read_config import config_read
    cr      = config_read()
    section = credential_key.upper()
    try:
        # Read all known connection fields
        fields = [
            "sftp_host", "sftp_port", "sftp_user", "sftp_password",
            "sftp_private_key", "sftp_passphrase", "sftp_verify_host",
            "sftp_connect_timeout_secs", "sftp_read_timeout_secs",
            "ftp_host", "ftp_port", "ftp_user", "ftp_password",
            "ftp_passive", "ftp_tls", "ftp_timeout_secs",
            "s3_access_key", "s3_secret_key", "s3_use_iam_role",
            "s3_role_arn", "s3_region", "s3_timeout_secs",
            "azure_connection_string", "azure_account", "azure_key",
            "azure_sas_token", "azure_use_managed_identity", "azure_timeout_secs",
            "api_url", "api_key", "api_token", "api_timeout_secs",
        ]
        config = {}
        for field in fields:
            try:
                config[field] = cr.get_conf_data(section, field)
            except Exception:
                config[field] = ""
        return config
    except Exception:
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# Auto-detection
# ─────────────────────────────────────────────────────────────────────────────

def detect_location_type(location: str, step: dict = None) -> str:
    """
    Auto-detects retrieval type from location value.
    Explicit Type in step always overrides.

    Returns one of: API_CALL, AZURE_BLOB, S3, SFTP, FTP,
                    UNC_PATH, LOCAL_PATH, ORACLE_CLOB,
                    MONGODB_GRIDFS, AUTO
    """
    location = (location or "").strip()
    step     = step or {}

    # Explicit type overrides auto-detection
    explicit = (step.get("Type") or "").upper()
    if explicit and explicit != "AUTO":
        return explicit

    # Azure Blob (check before https)
    if "blob.core.windows.net" in location:
        return "AZURE_BLOB"

    # HTTPS / HTTP — API call
    if location.startswith(("https://", "http://")):
        return "API_CALL"

    # S3
    if location.startswith("s3://"):
        return "S3"

    # SFTP explicit
    if location.startswith("sftp://"):
        return "SFTP"

    # FTP explicit
    if location.startswith(("ftp://", "ftps://")):
        return "FTP"

    # UNC path — Windows network share
    if location.startswith(("\\\\", "//")):
        return "UNC_PATH"

    # Windows local path — drive letter
    if len(location) > 1 and location[1] == ":":
        return "LOCAL_PATH"

    # Oracle CLOB reference
    if any(kw in location.upper() for kw in ["SELECT ", "WHERE ", "FROM "]):
        return "ORACLE_CLOB"

    # MongoDB GridFS
    if location.startswith("gridfs://"):
        return "MONGODB_GRIDFS"

    # Unix path — use SFTP if Host defined in step
    if location.startswith("/"):
        if step.get("Host") or step.get("CredentialKey"):
            return "SFTP"
        return "LOCAL_PATH"

    # Cannot determine — default to LOCAL_PATH
    logger.warning(
        f"File_fetcher: Cannot auto-detect location type for '{location}' — "
        f"defaulting to LOCAL_PATH."
    )
    return "LOCAL_PATH"


def detect_compression(content: bytes) -> str:
    """
    Detects compression format from magic bytes.
    Returns: ZIP, GZIP, BZIP2, TAR_GZ, or None
    """
    if not content or len(content) < 4:
        return None

    # ZIP
    if content[:4] == b"PK\x03\x04":
        return "ZIP"

    # GZIP
    if content[:2] == b"\x1f\x8b":
        return "GZIP"

    # BZIP2
    if content[:3] == b"BZh":
        return "BZIP2"

    return None


def detect_encryption(content: bytes) -> str:
    """
    Detects PGP encryption from magic bytes or ASCII armor.
    Returns: PGP or None
    """
    if not content:
        return None

    # ASCII armored PGP
    try:
        sample = content[:100].decode("utf-8", errors="ignore")
        if "-----BEGIN PGP" in sample:
            return "PGP"
    except Exception:
        pass

    # Binary PGP
    if len(content) >= 2 and content[0] in (0x85, 0xc1, 0xc2, 0xc3):
        return "PGP"

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Decompression
# ─────────────────────────────────────────────────────────────────────────────

def _decompress(content: bytes, compression: str,
                inner_filename: str = None) -> bytes:
    """
    Decompresses content using specified or auto-detected method.
    For ZIP with multiple files uses inner_filename to select correct file.
    """
    import zipfile
    import gzip
    import bz2

    compression = (compression or "").upper()

    if compression == "AUTO" or not compression:
        compression = detect_compression(content) or "NONE"

    if compression == "NONE":
        return content

    if compression == "ZIP":
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            names = zf.namelist()
            if not names:
                raise ValueError("ZIP file is empty.")

            # Select file inside ZIP
            if inner_filename:
                # Try exact match first
                if inner_filename in names:
                    target = inner_filename
                else:
                    # Try pattern match
                    matched = [
                        n for n in names
                        if fnmatch.fnmatch(n.lower(), inner_filename.lower())
                    ]
                    if not matched:
                        raise ValueError(
                            f"File '{inner_filename}' not found in ZIP. "
                            f"Available: {names}"
                        )
                    target = matched[0]
            elif len(names) == 1:
                target = names[0]
            else:
                # Multiple files — pick first non-directory
                data_files = [n for n in names if not n.endswith("/")]
                if not data_files:
                    raise ValueError("ZIP contains only directories.")
                if len(data_files) > 1:
                    raise ValueError(
                        f"ZIP contains multiple files — specify FileName to select: {data_files}"
                    )
                target = data_files[0]

            logger.info(f"File_fetcher: Extracting '{target}' from ZIP.")
            return zf.read(target)

    elif compression == "GZIP":
        return gzip.decompress(content)

    elif compression == "BZIP2":
        return bz2.decompress(content)

    elif compression == "TAR_GZ":
        import tarfile
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as tf:
            members = [m for m in tf.getmembers() if m.isfile()]
            if not members:
                raise ValueError("TAR.GZ archive is empty.")
            if len(members) == 1:
                f = tf.extractfile(members[0])
                return f.read()
            # Multiple files — use inner_filename
            if inner_filename:
                matched = [
                    m for m in members
                    if fnmatch.fnmatch(m.name, inner_filename)
                ]
                if matched:
                    f = tf.extractfile(matched[0])
                    return f.read()
            raise ValueError(
                f"TAR.GZ contains multiple files — specify FileName: "
                f"{[m.name for m in members]}"
            )

    else:
        raise ValueError(f"Unsupported compression format: {compression}")


# ─────────────────────────────────────────────────────────────────────────────
# Decryption
# ─────────────────────────────────────────────────────────────────────────────

def _decrypt(content: bytes, encryption_key: str) -> bytes:
    """
    Decrypts PGP encrypted content using private key from config.
    Requires pgpy library: pip install pgpy
    """
    if not encryption_key:
        return content

    try:
        import pgpy
    except ImportError:
        raise ImportError(
            "pgpy library required for PGP decryption. "
            "Install with: pip install pgpy"
        )

    from .Read_config import config_read
    cr = config_read()

    try:
        key_path   = cr.get_conf_data(encryption_key.upper(), "private_key_path")
        passphrase = cr.get_conf_data(encryption_key.upper(), "passphrase")
    except Exception:
        raise ValueError(
            f"PGP credentials not found for key '{encryption_key}'. "
            f"Add [{{encryption_key}}] section with private_key_path and passphrase "
            f"to environment.ini."
        )

    key, _ = pgpy.PGPKey.from_file(key_path)

    with key.unlock(passphrase):
        try:
            # Try ASCII armored
            message   = pgpy.PGPMessage.from_blob(content)
            decrypted = key.decrypt(message)
            result    = decrypted.message
            if isinstance(result, str):
                return result.encode("utf-8")
            return result
        except Exception as e:
            raise ValueError(f"PGP decryption failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Secure temp file handling
# ─────────────────────────────────────────────────────────────────────────────

class TempFileManager:
    """
    Manages temp files with secure deletion on cleanup.
    Used as context manager:

        with TempFileManager() as tmp:
            path = tmp.create("content_bytes")
            # use path
        # file securely deleted on exit
    """

    def __init__(self, secure_delete: bool = True, passes: int = 1):
        self._files        = []
        self._secure_delete = secure_delete
        self._passes       = passes

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.cleanup()

    def create(self, content: bytes, suffix: str = ".tmp") -> str:
        fd, path = tempfile.mkstemp(suffix=suffix, prefix="edi_work_")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(content)
        except Exception:
            os.close(fd)
            raise
        self._files.append(path)
        logger.info(
            f"File_fetcher: Temp file created — {path} "
            f"({len(content):,} bytes)"
        )
        return path

    def cleanup(self):
        for path in self._files:
            try:
                if os.path.exists(path):
                    if self._secure_delete:
                        self._overwrite(path)
                    os.remove(path)
                    logger.info(
                        f"File_fetcher: Temp file "
                        f"{'securely ' if self._secure_delete else ''}deleted — {path}"
                    )
            except Exception as e:
                logger.warning(
                    f"File_fetcher: Failed to delete temp file {path}: {e} — "
                    f"manual cleanup required."
                )
        self._files = []

    def _overwrite(self, path: str):
        size = os.path.getsize(path)
        for _ in range(self._passes):
            with open(path, "wb") as f:
                f.write(b"\x00" * size)


# ─────────────────────────────────────────────────────────────────────────────
# Retry decorator
# ─────────────────────────────────────────────────────────────────────────────

def _pick_strategy(source: dict) -> str:
    """
    Resolves PickStrategy, accepting it at either level:
        Source.PickStrategy              (top level)
        Source.FileConfig.PickStrategy   (nested — where users naturally put it)
    Top level wins when both are present. Defaults to LATEST.
    """
    top = (source.get("PickStrategy") or "").strip().upper()
    nested = ((source.get("FileConfig") or {}).get("PickStrategy") or "").strip().upper()
    if "ALL" in (top, nested):
        return "ALL"
    return top or nested or "LATEST"


# Errors that are permanent config/validation failures and should NOT be retried.
# Retrying these just wastes retry_wait_minutes per attempt.
_NO_RETRY_ERRORS = (
    ValueError,         # bad config: missing FileName, unsupported format, etc.
    FileNotFoundError,  # path does not exist — won't fix itself
    PermissionError,    # access denied — won't fix itself
    NotImplementedError,
)

def _with_retry(func, attempts: int = 2, wait_minutes: float = 5):
    """
    Retries func up to attempts times with wait_minutes gap.

    Permanent errors (ValueError, FileNotFoundError, PermissionError) are
    raised immediately without retrying — sleeping never fixes a config mistake.
    Transient errors (network timeouts, IOError) are retried with the delay.

    Raises last exception if all attempts fail.
    """
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            logger.info(f"File_fetcher: Fetch attempt {attempt}/{attempts}")
            return func()
        except _NO_RETRY_ERRORS as e:
            # Permanent failure — no point retrying
            logger.issue(f"File_fetcher: Permanent error — {e}")
            raise
        except Exception as e:
            last_error = e
            if attempt < attempts:
                wait_secs = wait_minutes * 60
                logger.issue(
                    f"File_fetcher: Attempt {attempt} failed — {e}. "
                    f"Retrying in {wait_minutes} minute(s)..."
                )
                time.sleep(wait_secs)
            else:
                logger.issue(
                    f"File_fetcher: All {attempts} attempt(s) failed — {e}"
                )

    raise last_error


# ─────────────────────────────────────────────────────────────────────────────
# File selection helpers
# ─────────────────────────────────────────────────────────────────────────────

def _select_file(files: list, filename: str = None,
                 file_pattern: str = None,
                 pick_strategy: str = "LATEST") -> list:
    """
    Selects file(s) from a list of (name, modified_dt, size) tuples.

    Rules:
        filename specified    → exact match
        file_pattern specified → pattern match, apply pick_strategy
        both specified        → filename takes priority
        neither               → error

    pick_strategy:
        LATEST → return single latest modified file
        ALL    → return all matching files

    Returns list of selected filenames.
    """
    if not files:
        raise ValueError("No files found in the specified location.")

    # Exact filename match
    if filename:
        matched = [f for f in files if f[0] == filename or
                   os.path.basename(f[0]) == filename]
        if not matched:
            raise FileNotFoundError(
                f"File '{filename}' not found. "
                f"Available: {[f[0] for f in files[:10]]}"
            )
        return [matched[0][0]]

    # Pattern match
    if file_pattern:
        matched = [
            f for f in files
            if fnmatch.fnmatch(
                os.path.basename(f[0]).lower(),
                file_pattern.lower()
            )
        ]
        if not matched:
            raise FileNotFoundError(
                f"No files matching pattern '{file_pattern}'. "
                f"Available: {[f[0] for f in files[:10]]}"
            )

        logger.info(
            f"File_fetcher: Pattern '{file_pattern}' matched "
            f"{len(matched)} file(s)"
        )

        if (pick_strategy or "LATEST").upper() == "ALL":
            # Sort by modified date for consistent ordering
            matched.sort(key=lambda f: f[1] or datetime.min, reverse=True)
            for f in matched:
                logger.info(
                    f"File_fetcher: Using '{f[0]}' "
                    f"(modified: {f[1]}, {f[2]:,} bytes)"
                )
            return [f[0] for f in matched]
        else:
            # LATEST — sort by modified date, pick newest
            # Handle None modified dates
            matched.sort(
                key=lambda f: f[1] or datetime.min,
                reverse=True
            )
            # Warn if tie
            if (len(matched) > 1 and
                    matched[0][1] and matched[1][1] and
                    matched[0][1] == matched[1][1]):
                logger.warning(
                    f"File_fetcher: Two files have same modified time — "
                    f"picking alphabetically last: {matched[0][0]}"
                )
            selected = matched[0]
            logger.info(
                f"File_fetcher: Selected latest — '{selected[0]}' "
                f"(modified: {selected[1]}, {selected[2]:,} bytes)"
            )
            return [selected[0]]

    raise ValueError(
        "Either FileName or FilePattern must be specified in Source."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Retrievers
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_local(source: dict) -> list:
    """
    Fetches file(s) from LOCAL_PATH or UNC_PATH.
    Returns list of (filename, bytes) tuples.
    """
    path         = source.get("Path", "")
    filename     = source.get("FileName", "")
    file_pattern = source.get("FilePattern", "")
    pick_strategy= _pick_strategy(source)

    if not path:
        raise ValueError("Source.Path is required for LOCAL_PATH / UNC_PATH.")

    # When neither FileName nor FilePattern is given, infer a default pattern
    # from FileConfig.Format so only the relevant file type is fetched.
    # PDF → "*.pdf", CSV → "*.csv", etc.  Falls back to "*" for unknown formats.
    if not filename and not file_pattern:
        fmt = (source.get("FileConfig") or {}).get("Format", "").upper()
        _ext_map = {
            "PDF"        : "*.pdf",
            "CSV"        : "*.csv",
            "TSV"        : "*.tsv",
            "EXCEL"      : "*.xlsx",
            "TXT"        : "*.txt",
            "TEXT"       : "*.txt",
            "DAT"        : "*.dat",
            "PIPE"       : "*.txt",
            "FIXED_WIDTH": "*.txt",
            "JSON"       : "*.json",
            "XML"        : "*.xml",
            "HL7"        : "*.hl7",
        }
        file_pattern = _ext_map.get(fmt, "*")
        logger.warning(
            f"File_fetcher: No FileName or FilePattern — "
            f"defaulting to '{file_pattern}' based on "
            f"FileConfig.Format='{fmt or 'unknown'}'."
        )

    base_path = Path(path)
    if not base_path.exists():
        raise FileNotFoundError(f"Path not found: {path}")

    # List files with metadata
    files = []
    for f in base_path.iterdir():
        if f.is_file():
            stat     = f.stat()
            modified = datetime.fromtimestamp(stat.st_mtime)
            files.append((f.name, modified, stat.st_size))

    selected = _select_file(files, filename, file_pattern, pick_strategy)

    result = []
    for name in selected:
        full_path = base_path / name
        logger.info(f"File_fetcher: Reading local file — {full_path}")
        content = full_path.read_bytes()
        result.append((name, content))

    return result


def _fetch_sftp(source: dict) -> list:
    """
    Fetches file(s) from SFTP server.
    Returns list of (filename, bytes) tuples.
    """
    import paramiko

    credential_key = source.get("CredentialKey", "")
    path           = source.get("Path", "")
    filename       = source.get("FileName", "")
    file_pattern   = source.get("FilePattern", "")
    pick_strategy  = _pick_strategy(source)
    host           = source.get("Host", "")
    port           = int(source.get("Port", 0) or 0)
    config         = _get_config(credential_key) if credential_key else {}

    # Connection details — step overrides config
    sftp_host    = host or config.get("sftp_host", "")
    sftp_port    = port or int(config.get("sftp_port", "22") or 22)
    sftp_user    = config.get("sftp_user", "")
    sftp_pass    = config.get("sftp_password", "")
    key_path     = config.get("sftp_private_key", "")
    passphrase   = config.get("sftp_passphrase", "")
    verify_host  = (config.get("sftp_verify_host", "true") or "true").lower() == "true"
    conn_timeout = int(config.get("sftp_connect_timeout_secs", "30") or 30)
    read_timeout = int(config.get("sftp_read_timeout_secs",    "300") or 300)

    if not sftp_host:
        raise ValueError(
            f"SFTP host not configured for CredentialKey '{credential_key}'. "
            f"Add sftp_host to [{credential_key}] in environment.ini."
        )

    # Build SSH client
    ssh = paramiko.SSHClient()

    if verify_host:
        ssh.load_system_host_keys()
        ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
    else:
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    # Auth — private key takes priority over password
    connect_kwargs = {
        "hostname": sftp_host,
        "port"    : sftp_port,
        "username": sftp_user,
        "timeout" : conn_timeout
    }

    if key_path:
        pkey = paramiko.RSAKey.from_private_key_file(
            key_path,
            password=passphrase or None
        )
        connect_kwargs["pkey"] = pkey
        logger.info(
            f"File_fetcher: SFTP connecting to {sftp_host}:{sftp_port} "
            f"(key auth)"
        )
    elif sftp_pass:
        connect_kwargs["password"] = sftp_pass
        logger.info(
            f"File_fetcher: SFTP connecting to {sftp_host}:{sftp_port} "
            f"(password auth)"
        )
    else:
        raise ValueError(
            f"No SFTP credentials configured for '{credential_key}'. "
            f"Add sftp_password or sftp_private_key to environment.ini."
        )

    ssh.connect(**connect_kwargs)
    sftp = ssh.open_sftp()
    sftp.sock.settimeout(read_timeout)

    try:
        # List files in remote directory
        remote_files = []
        for attr in sftp.listdir_attr(path):
            if not stat_is_dir(attr):
                modified = (
                    datetime.fromtimestamp(attr.st_mtime)
                    if attr.st_mtime else None
                )
                remote_files.append(
                    (attr.filename, modified, attr.st_size or 0)
                )

        selected = _select_file(
            remote_files, filename, file_pattern, pick_strategy
        )

        result = []
        for name in selected:
            remote_path = path.rstrip("/") + "/" + name
            logger.info(
                f"File_fetcher: SFTP downloading — {remote_path}"
            )
            buf = io.BytesIO()
            sftp.getfo(remote_path, buf)
            result.append((name, buf.getvalue()))

        return result

    finally:
        sftp.close()
        ssh.close()


def stat_is_dir(attr) -> bool:
    """Checks if paramiko SFTPAttributes is a directory."""
    import stat
    return stat.S_ISDIR(attr.st_mode) if attr.st_mode else False


def _fetch_ftp(source: dict) -> list:
    """
    Fetches file(s) from FTP server.
    Returns list of (filename, bytes) tuples.
    """
    credential_key = source.get("CredentialKey", "")
    path           = source.get("Path", "")
    filename       = source.get("FileName", "")
    file_pattern   = source.get("FilePattern", "")
    pick_strategy  = _pick_strategy(source)
    config         = _get_config(credential_key) if credential_key else {}

    ftp_host    = source.get("Host", "") or config.get("ftp_host", "")
    ftp_port    = int(source.get("Port", 0) or config.get("ftp_port", "21") or 21)
    ftp_user    = config.get("ftp_user", "")
    ftp_pass    = config.get("ftp_password", "")
    ftp_passive = (config.get("ftp_passive", "true") or "true").lower() == "true"
    ftp_tls     = (config.get("ftp_tls", "true") or "true").lower() == "true"
    ftp_timeout = int(config.get("ftp_timeout_secs", "300") or 300)

    if not ftp_host:
        raise ValueError(
            f"FTP host not configured for CredentialKey '{credential_key}'."
        )

    if ftp_tls:
        ftp = ftplib.FTP_TLS(timeout=ftp_timeout)
        ftp.connect(ftp_host, ftp_port)
        ftp.auth()
        ftp.login(ftp_user, ftp_pass)
        ftp.prot_p()
    else:
        ftp = ftplib.FTP(timeout=ftp_timeout)
        ftp.connect(ftp_host, ftp_port)
        ftp.login(ftp_user, ftp_pass)

    ftp.set_pasv(ftp_passive)

    try:
        ftp.cwd(path)

        # List files with modification times
        remote_files = []
        lines = []
        ftp.retrlines("LIST", lines.append)

        for line in lines:
            parts = line.split(None, 8)
            if len(parts) >= 9:
                name     = parts[8]
                size     = int(parts[4]) if parts[4].isdigit() else 0
                modified = None
                try:
                    # Try to parse modification time
                    date_str = " ".join(parts[5:8])
                    modified = datetime.strptime(date_str, "%b %d %H:%M")
                    modified = modified.replace(year=datetime.now().year)
                except Exception:
                    pass
                remote_files.append((name, modified, size))

        selected = _select_file(
            remote_files, filename, file_pattern, pick_strategy
        )

        result = []
        for name in selected:
            logger.info(f"File_fetcher: FTP downloading — {path}/{name}")
            buf = io.BytesIO()
            ftp.retrbinary(f"RETR {name}", buf.write)
            result.append((name, buf.getvalue()))

        return result

    finally:
        ftp.quit()


def _fetch_s3(source: dict) -> list:
    """
    Fetches file(s) from AWS S3.
    Returns list of (filename, bytes) tuples.
    """
    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError:
        raise ImportError(
            "boto3 library required for S3 access. "
            "Install with: pip install boto3"
        )

    credential_key = source.get("CredentialKey", "")
    bucket         = source.get("Bucket", "")
    path           = source.get("Path", "").strip("/")
    filename       = source.get("FileName", "")
    file_pattern   = source.get("FilePattern", "")
    pick_strategy  = _pick_strategy(source)
    config         = _get_config(credential_key) if credential_key else {}

    region          = config.get("s3_region", "us-east-1") or "us-east-1"
    access_key      = config.get("s3_access_key", "")
    secret_key      = config.get("s3_secret_key", "")
    use_iam         = (config.get("s3_use_iam_role", "false") or "false").lower() == "true"
    role_arn        = config.get("s3_role_arn", "")
    timeout         = int(config.get("s3_timeout_secs", "300") or 300)

    # Build S3 client
    if use_iam:
        s3 = boto3.client("s3", region_name=region)
    elif role_arn:
        sts      = boto3.client("sts")
        assumed  = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName=f"file_validator_{uuid.uuid4().hex[:8]}"
        )
        creds    = assumed["Credentials"]
        s3 = boto3.client(
            "s3",
            region_name=region,
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"]
        )
    elif access_key and secret_key:
        s3 = boto3.client(
            "s3",
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key
        )
    else:
        raise ValueError(
            f"No S3 credentials configured for '{credential_key}'. "
            f"Set s3_use_iam_role, s3_role_arn, or s3_access_key/s3_secret_key."
        )

    # Build prefix for listing
    prefix = f"{path}/" if path else ""

    # List objects
    paginator = s3.get_paginator("list_objects_v2")
    s3_files  = []

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key      = obj["Key"]
            name     = os.path.basename(key)
            modified = obj.get("LastModified")
            size     = obj.get("Size", 0)
            if name:
                s3_files.append((name, modified, size, key))

    # Filter by pattern
    if file_pattern:
        s3_files = [
            f for f in s3_files
            if fnmatch.fnmatch(f[0].lower(), file_pattern.lower())
        ]

    if not s3_files:
        raise FileNotFoundError(
            f"No files found in s3://{bucket}/{prefix} "
            f"matching pattern '{file_pattern}'."
        )

    # Select file(s)
    file_tuples = [(f[0], f[1], f[2]) for f in s3_files]
    selected_names = _select_file(
        file_tuples, filename, file_pattern, pick_strategy
    )

    result = []
    for name in selected_names:
        # Find full key for this filename
        matching_keys = [f[3] for f in s3_files if f[0] == name]
        if not matching_keys:
            continue
        key = matching_keys[0]
        logger.info(f"File_fetcher: S3 downloading — s3://{bucket}/{key}")
        response = s3.get_object(Bucket=bucket, Key=key)
        content  = response["Body"].read()
        result.append((name, content))

    return result


def _fetch_azure_blob(source: dict) -> list:
    """
    Fetches file(s) from Azure Blob Storage.
    Returns list of (filename, bytes) tuples.
    """
    try:
        from azure.storage.blob import BlobServiceClient
    except ImportError:
        raise ImportError(
            "azure-storage-blob library required for Azure Blob access. "
            "Install with: pip install azure-storage-blob"
        )

    credential_key = source.get("CredentialKey", "")
    path           = source.get("Path", "").strip("/")
    filename       = source.get("FileName", "")
    file_pattern   = source.get("FilePattern", "")
    pick_strategy  = _pick_strategy(source)
    config         = _get_config(credential_key) if credential_key else {}

    conn_str    = config.get("azure_connection_string", "")
    account     = config.get("azure_account", "")
    key         = config.get("azure_key", "")
    sas_token   = config.get("azure_sas_token", "")
    use_managed = (
        config.get("azure_use_managed_identity", "false") or "false"
    ).lower() == "true"

    # Parse container from path
    path_parts = path.strip("/").split("/", 1)
    container  = path_parts[0]
    blob_prefix = path_parts[1] if len(path_parts) > 1 else ""

    # Build client
    if use_managed:
        from azure.identity import ManagedIdentityCredential
        service_client = BlobServiceClient(
            account_url=f"https://{account}.blob.core.windows.net",
            credential=ManagedIdentityCredential()
        )
    elif conn_str:
        service_client = BlobServiceClient.from_connection_string(conn_str)
    elif sas_token:
        service_client = BlobServiceClient(
            account_url=f"https://{account}.blob.core.windows.net",
            credential=sas_token
        )
    elif account and key:
        service_client = BlobServiceClient(
            account_url=f"https://{account}.blob.core.windows.net",
            credential=key
        )
    else:
        raise ValueError(
            f"No Azure credentials configured for '{credential_key}'."
        )

    container_client = service_client.get_container_client(container)

    # List blobs
    blobs      = container_client.list_blobs(name_starts_with=blob_prefix)
    blob_files = []

    for blob in blobs:
        name     = os.path.basename(blob.name)
        modified = blob.last_modified
        size     = blob.size or 0
        if name:
            blob_files.append((name, modified, size, blob.name))

    if not blob_files:
        raise FileNotFoundError(
            f"No blobs found in container '{container}' "
            f"with prefix '{blob_prefix}'."
        )

    # Select file(s)
    file_tuples    = [(f[0], f[1], f[2]) for f in blob_files]
    selected_names = _select_file(
        file_tuples, filename, file_pattern, pick_strategy
    )

    result = []
    for name in selected_names:
        matching = [f[3] for f in blob_files if f[0] == name]
        if not matching:
            continue
        blob_name    = matching[0]
        blob_client  = container_client.get_blob_client(blob_name)
        logger.info(
            f"File_fetcher: Azure Blob downloading — "
            f"{container}/{blob_name}"
        )
        content = blob_client.download_blob().readall()
        result.append((name, content))

    return result


def _fetch_oracle_clob(source: dict) -> list:
    """
    Fetches file content from Oracle CLOB column.
    Returns list of (filename, bytes) tuples.
    """
    from .OracleDB import OracleConnection

    environment = (source.get("Environment") or "").upper()
    query       = source.get("Query", "")
    clob_column = source.get("CLOBColumn", "")
    chunk_size  = 65536

    if not query or not clob_column:
        raise ValueError(
            "Source.Query and Source.CLOBColumn are required for ORACLE_CLOB."
        )

    conn   = OracleConnection(environment)
    result = conn.fetchAllDict(query)

    if not result:
        raise ValueError(
            f"Oracle CLOB query returned no rows: {query}"
        )

    contents = []
    for i, row in enumerate(result):
        clob = row.get(clob_column) or row.get(clob_column.upper())
        if clob is None:
            raise ValueError(
                f"Column '{clob_column}' not found or null in row {i+1}."
            )

        # Stream CLOB content
        if hasattr(clob, "read"):
            # cx_Oracle LOB object — stream in chunks
            content = b""
            offset  = 1
            while True:
                chunk = clob.read(offset, chunk_size)
                if not chunk:
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")
                content += chunk
                offset  += chunk_size
        elif isinstance(clob, str):
            content = clob.encode("utf-8")
        elif isinstance(clob, bytes):
            content = clob
        else:
            content = str(clob).encode("utf-8")

        logger.info(
            f"File_fetcher: Oracle CLOB retrieved — "
            f"row {i+1}, {len(content):,} bytes"
        )
        contents.append((f"clob_row_{i+1}.tmp", content))

    return contents


def _fetch_mongodb_gridfs(source: dict) -> list:
    """
    Fetches file(s) from MongoDB GridFS.
    Returns list of (filename, bytes) tuples.
    """
    try:
        import gridfs
    except ImportError:
        raise ImportError(
            "pymongo required for GridFS access. "
            "Install with: pip install pymongo"
        )

    from .MongoDB import Collection

    environment = (source.get("Environment") or "").upper()
    db          = source.get("DB", "")
    bucket_name = source.get("Query", "fs") or "fs"
    chunk_size  = 65536

    # Build filter from DataFilters passed in context
    mongo_filter = source.get("_mongo_filter", {})

    from pymongo import MongoClient
    from .Read_config import config_read
    cr  = config_read()

    try:
        uri = cr.get_conf_data(environment, "mongodb_uri")
    except Exception:
        raise ValueError(
            f"MongoDB URI not configured for environment '{environment}'."
        )

    client    = MongoClient(uri)
    database  = client[db]
    fs        = gridfs.GridFS(database, collection=bucket_name)

    grid_files = list(fs.find(mongo_filter))

    if not grid_files:
        raise FileNotFoundError(
            f"No files found in GridFS bucket '{bucket_name}' "
            f"with filter {mongo_filter}."
        )

    result = []
    for gf in grid_files:
        logger.info(
            f"File_fetcher: GridFS downloading — "
            f"{gf.filename} ({gf.length:,} bytes)"
        )
        content = b""
        while True:
            chunk = gf.read(chunk_size)
            if not chunk:
                break
            content += chunk
        result.append((gf.filename or f"gridfs_{gf._id}.tmp", content))

    return result


def _fetch_api(source: dict) -> list:
    """
    Fetches file from REST API.
    Returns list of (filename, bytes) tuples.
    """
    import requests

    credential_key = source.get("CredentialKey", "")
    path           = source.get("Path", "")
    config         = _get_config(credential_key) if credential_key else {}

    api_url     = path or config.get("api_url", "")
    api_key     = config.get("api_key", "")
    api_token   = config.get("api_token", "")
    api_timeout = int(config.get("api_timeout_secs", "120") or 120)

    if not api_url:
        raise ValueError(
            f"API URL not configured. Set api_url in [{credential_key}] "
            f"or provide Path in Source."
        )

    headers = {}
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"
    elif api_key:
        headers["X-API-Key"] = api_key

    logger.info(f"File_fetcher: API GET ******")

    response = requests.get(api_url, headers=headers, timeout=api_timeout)

    if response.status_code != 200:
        raise ValueError(
            f"API returned HTTP {response.status_code}: "
            f"{response.text[:200]}"
        )

    # Extract filename from content-disposition
    disposition = response.headers.get("content-disposition", "")
    filename    = "api_download.tmp"
    if "filename=" in disposition:
        filename = (
            disposition.split("filename=")[1]
            .split(";")[0].strip().strip('"')
        )

    logger.info(
        f"File_fetcher: Downloaded '{filename}' — "
        f"{len(response.content):,} bytes."
    )

    return [(filename, response.content)]


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def fetch_file(source: dict,
               retry_attempts: int = 2,
               retry_wait_minutes: float = 5) -> list:
    """
    Fetches file(s) from any supported source type.
    Handles auto-detection, retry, decryption and decompression.

    Args:
        source             : Source dict from test case document
        retry_attempts     : Number of fetch attempts (default 2)
        retry_wait_minutes : Minutes between retry attempts (default 5)

    Returns:
        List of (filename, bytes) tuples — one per file fetched.
        For PickStrategy=LATEST — always single item list.
        For PickStrategy=ALL    — one item per matching file.

    Raises:
        ValueError, FileNotFoundError on failure after all retries.
    """
    source_type  = (source.get("Type") or "").upper()
    file_config  = source.get("FileConfig") or {}
    compression  = (file_config.get("Compression") or "").upper()
    encryption   = file_config.get("EncryptionKey", "")
    inner_file   = source.get("FileName", "")

    # Map format types to retrieval type when no explicit location type
    format_only_types = {
        "CSV", "PIPE", "TSV", "FIXED_WIDTH",
        "EXCEL", "JSON", "XML", "HL7",
        "NCPDP_D0", "NCPDP_SCRIPT"
    }
    if source_type in format_only_types:
        source_type = detect_location_type(
            source.get("Path", "") or source.get("Bucket", ""),
            source
        )

    # Fetcher dispatch
    fetcher_map = {
        "LOCAL_PATH"     : _fetch_local,
        "UNC_PATH"       : _fetch_local,
        "SFTP"           : _fetch_sftp,
        "FTP"            : _fetch_ftp,
        "S3"             : _fetch_s3,
        "AZURE_BLOB"     : _fetch_azure_blob,
        "ORACLE_CLOB"    : _fetch_oracle_clob,
        "MONGODB_GRIDFS" : _fetch_mongodb_gridfs,
        "API_CALL"       : _fetch_api,
    }

    # Handle AUTO — detect from location value
    if source_type == "AUTO":
        location    = source.get("Path", "") or source.get("Bucket", "")
        source_type = detect_location_type(location, source)

    fetcher = fetcher_map.get(source_type)
    if not fetcher:
        raise ValueError(
            f"Unsupported Source.Type '{source_type}'. "
            f"Supported: {sorted(fetcher_map.keys())}"
        )

    # Fetch with retry
    def _fetch():
        return fetcher(source)

    raw_files = _with_retry(
        _fetch,
        attempts=retry_attempts,
        wait_minutes=retry_wait_minutes
    )

    # Process each file — decrypt then decompress
    processed = []
    for filename, content in raw_files:
        # Auto-detect encryption
        if not encryption:
            enc_detected = detect_encryption(content)
            if enc_detected:
                logger.warning(
                    f"File_fetcher: File '{filename}' appears encrypted "
                    f"but no EncryptionKey specified — proceeding without decryption."
                )
        else:
            logger.info(
                f"File_fetcher: Decrypting '{filename}' "
                f"using key '{encryption}'"
            )
            content = _decrypt(content, encryption)
            logger.info(
                f"File_fetcher: Decryption successful — '{filename}'"
            )

        # Auto-detect compression
        if not compression or compression == "AUTO":
            comp_detected = detect_compression(content)
            if comp_detected:
                # xlsx / xlsm / xlsb are ZIP-based internally — skip ZIP extraction
                # and let the spreadsheet parser (openpyxl / pandas) read them directly
                _SPREADSHEET_EXTS = {".xlsx", ".xlsm", ".xlsb", ".xls", ".ods", ".pdf"}
                file_ext = Path(filename).suffix.lower()
                if comp_detected == "ZIP" and file_ext in _SPREADSHEET_EXTS:
                    logger.info(
                        f"File_fetcher: '{filename}' is a ZIP-based spreadsheet — "
                        f"skipping ZIP extraction, passing to parser directly."
                    )
                else:
                    logger.info(
                        f"File_fetcher: Compression detected — '{filename}' ({comp_detected})"
                    )
                    content = _decompress(content, comp_detected, inner_file)
                    logger.info(
                        f"File_fetcher: Decompressed '{filename}' — {len(content):,} bytes"
                    )
        elif compression and compression != "NONE":
            content = _decompress(content, compression, inner_file)
            logger.info(
                f"File_fetcher: Decompressed '{filename}' — {len(content):,} bytes"
            )

        processed.append((filename, content))

    return processed

# ─────────────────────────────────────────────────────────────────────────────
# PDF convenience fetcher — used by PDF_Data validation type
# ─────────────────────────────────────────────────────────────────────────────

def fetch_pdf_bytes(source: dict,
                    retry_attempts: int = 2,
                    retry_wait_minutes: float = 5) -> list:
    """
    Fetches ALL PDFs from any supported source location (UNC_PATH, SFTP,
    S3, AZURE_BLOB, MONGODB_GRIDFS, API_CALL, LOCAL_PATH).

    Returns a list of (filename, raw_bytes) tuples — one per PDF found.
    Use FileConfig.PickStrategy to control which files are returned:
      LATEST (default) — most recently modified file only
      ALL              — every matching file in the folder

    Raises ValueError if no files are returned.
    Used by Data_Validation._run_single when validationType == "PDF_Data".
    """
    raw_files = fetch_file(source, retry_attempts, retry_wait_minutes)
    if not raw_files:
        raise ValueError(
            f"fetch_pdf_bytes: no files returned from "
            f"{source.get('Type', '?')} source — check Path/Bucket and credentials."
        )
    valid = [(fn, rb) for fn, rb in raw_files if rb]
    if not valid:
        raise ValueError("fetch_pdf_bytes: all fetched files had empty content.")
    logger.info(
        f"File_fetcher: {len(valid)} PDF(s) fetched — "
        f"{sum(len(rb) for _, rb in valid):,} total bytes."
    )
    return valid